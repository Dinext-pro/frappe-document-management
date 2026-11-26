import json
import os
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
from pathlib import Path

import frappe
from frappe.utils import get_url_to_form
from document_management.frappe_document_management.rag.config import standalone_ai_disabled
from document_management.frappe_document_management.utils.document_provider import is_standalone
from document_management.search import semantic_backend, tantivy_backend
from document_management.search.query import (
    TextQuery,
    _term_matches_haystack,
    compile_match_plan,
    current_search_language,
    fold_text,
    make_excerpt,
    parse_search_syntax,
    search_match_details,
    significant_terms,
)


SEARCHABLE_FIELD_TYPES = {
    "Data",
    "Text",
    "Text Editor",
    "Small Text",
    "Long Text",
    "Link",
    "Dynamic Link",
    "Select",
    "Date",
    "Datetime",
    "Time",
    "Int",
    "Float",
    "Currency",
    "Percent",
    "Check",
}

PREVIEWABLE_FIELD_TYPES = SEARCHABLE_FIELD_TYPES | {
    "Read Only",
    "Phone",
    "Email",
    "URL",
    "Rating",
    "Color",
    "Duration",
    "Barcode",
    "Autocomplete",
}
MAX_PREVIEW_FIELD_CHARS = 2000
MAX_PREVIEW_CHILD_ROWS = 25

ALLOWED_SORT_MODES = frozenset({"relevance", "modified_desc", "modified_asc", "title_asc"})
INDEX_REBUILT_EVENT = "document_management:indexed_search_rebuilt"
MAX_ATTACHMENT_PAGES = 50
MAX_ATTACHMENT_CHARS = 100_000
MAX_ATTACHMENT_FILE_BYTES = 25 * 1024 * 1024
INDEXABLE_ATTACHMENT_EXTENSIONS = frozenset({".pdf", ".txt", ".md"})


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = frozenset(
        {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if data:
            self.parts.append(data)


def _plain_text(value, fieldtype=None):
    text = str(value or "")
    if fieldtype == "Text Editor" or ("<" in text and ">" in text):
        parser = _TextExtractor()
        try:
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts)
        except Exception:
            pass
    return " ".join(unescape(text).split())


# The private shared index nominates candidates. A response is built from the
# current record, using the reader's field grants, the current settings and files.
_INDEX_FIELDS = object()


def _permitted_fields(doc, reader):
    if reader is None or reader is _INDEX_FIELDS:
        return reader
    return set(doc.meta.get_permitted_fieldnames(
        parenttype=getattr(doc, "parenttype", None),
        user=reader,
        permission_type="read",
        with_virtual_fields=False,
    ))


def _is_visible_field(field, permitted=None):
    if getattr(field, "hidden", False) or getattr(field, "is_virtual", False):
        return False
    if permitted is _INDEX_FIELDS:
        return True
    if permitted is not None:
        return field.fieldname in permitted
    return not int(getattr(field, "permlevel", 0) or 0)


def _is_searchable_field(field, permitted=None):
    return field.fieldtype in SEARCHABLE_FIELD_TYPES and _is_visible_field(field, permitted)


def _is_previewable_field(field, permitted=None):
    return field.fieldtype in PREVIEWABLE_FIELD_TYPES and _is_visible_field(field, permitted)


def _format_preview_value(value, fieldtype, query="", folded_terms=()):
    if fieldtype == "Check":
        return "Yes" if int(value or 0) else "No"
    text = _plain_text(value, fieldtype)
    if len(text) > MAX_PREVIEW_FIELD_CHARS:
        if folded_terms:
            return make_excerpt(
                text,
                query,
                maximum=MAX_PREVIEW_FIELD_CHARS,
                folded_terms=folded_terms,
            )
        return text[:MAX_PREVIEW_FIELD_CHARS].rstrip() + "..."
    return text


def _matched_terms_in_text(text, plan):
    folded_terms = plan.get("folded_terms") or ()
    if not folded_terms or not text:
        return set(), False
    folded = fold_text(text)
    matched = {
        term
        for term in folded_terms
        if _term_matches_haystack(term, folded)
    }
    folded_query = plan.get("folded_query") or ""
    phrase_hit = bool(folded_query and len(folded_terms) > 1 and folded_query in folded)
    return matched, phrase_hit


def _collect_doc_preview_fields(doc, excluded, plan, query="", reader=None):
    permitted = _permitted_fields(doc, reader)
    fields_out = []
    matched_terms = set()
    phrase_hit = False
    for field in getattr(doc.meta, "fields", []) or []:
        if field.fieldname in excluded:
            continue
        if not _is_previewable_field(field, permitted):
            continue
        val = doc.get(field.fieldname)
        if val in (None, ""):
            continue
        if field.fieldtype == "Check" and not int(val or 0):
            continue
        formatted = _format_preview_value(
            val,
            field.fieldtype,
            query=query,
            folded_terms=plan.get("folded_terms") or (),
        )
        if not formatted:
            continue
        raw_text = _plain_text(val, field.fieldtype)
        field_terms, field_phrase = _matched_terms_in_text(raw_text, plan)
        matched_terms.update(field_terms)
        phrase_hit = phrase_hit or field_phrase
        fields_out.append(
            {
                "fieldname": field.fieldname,
                "label": field.label or field.fieldname,
                "fieldtype": field.fieldtype,
                "value": formatted,
                "matched": bool(field_terms or field_phrase),
            }
        )
    return fields_out, matched_terms, phrase_hit


def _desk_route(doc_type, doc_name):
    return urlsplit(get_url_to_form(doc_type, doc_name)).path


def _excluded_doctypes():
    additional = (frappe.get_hooks("document_management_excluded_index_doctypes") or ()
                  if getattr(frappe.local, "site", None) else ())
    return SKIPPED_INDEX_DOCTYPES.union(additional, {"Document", "Document Version", "Document Management Settings"})


def _indexable_source(doc, rules, blocked):
    """Provider-owned trees, including their comments and files, stay out of the generic index."""
    if doc.doctype in blocked:
        return False
    linked = None
    if doc.doctype == "File":
        linked = doc.get("attached_to_doctype")
    elif doc.doctype == "Comment":
        linked = doc.get("reference_doctype")
    if linked in blocked:
        return False
    if linked and (linked in ("File", "Comment") or frappe.get_meta(linked).istable):
        target = doc.get("attached_to_name") if doc.doctype == "File" else doc.get("reference_name")
        if linked in ("File", "Comment") or not target:
            return False
        ref = frappe.get_doc(linked, target)
        if not _indexable_source(ref, rules, blocked):
            return False
    if not getattr(doc.meta, "istable", False):
        return True
    if not doc.parent or not doc.parenttype or doc.parenttype in blocked:
        return False
    parent_meta = frappe.get_meta(doc.parenttype)
    table = parent_meta.get_field(doc.parentfield)
    excluded = rules.get(doc.parenttype, {}).get("excluded_fields", ())
    return bool(not parent_meta.istable and table and table.fieldtype in ("Table", "Table MultiSelect") and table.options == doc.doctype
                and not table.hidden and table.fieldname not in excluded)


def _listed(doc):
    parent = doc.parenttype if getattr(doc.meta, "istable", False) else None
    return bool(frappe.get_list(doc.doctype, filters={"name": doc.name}, parent_doctype=parent,
                                pluck="name", limit_page_length=1))


def _readable_children(doc, field, reader):
    rows = doc.get(field.fieldname) or []
    if not rows or reader is None or reader is _INDEX_FIELDS:
        return rows
    allowed = set(frappe.get_list(
        field.options, parent_doctype=doc.doctype,
        filters={"parenttype": doc.doctype, "parentfield": field.fieldname, "parent": doc.name},
        pluck="name", limit_page_length=0,
    ))
    return [row for row in rows if row.name in allowed and row.has_permission("read", user=reader)]


def _field_allowed(doc, fieldname, reader, rules):
    if not fieldname:
        return True
    field = doc.meta.get_field(fieldname)
    excluded = rules.get(doc.doctype, {}).get("excluded_fields", ())
    return bool(field and fieldname not in excluded and _is_visible_field(field, _permitted_fields(doc, reader)))


def _readable_source(doc, rules, reader, blocked):
    if not _indexable_source(doc, rules, blocked) or not doc.has_permission("read", user=reader):
        return None
    if getattr(doc.meta, "istable", False):
        parent = frappe.get_doc(doc.parenttype, doc.parent)
        if (not _listed(doc) or not _readable_source(parent, rules, reader, blocked)
                or not _field_allowed(parent, doc.parentfield, reader, rules)):
            return None
        return parent
    if not _listed(doc):
        return None
    if doc.doctype in ("File", "Comment"):
        ref_type, ref_name = (("attached_to_doctype", "attached_to_name") if doc.doctype == "File"
                              else ("reference_doctype", "reference_name"))
        if doc.get(ref_type) and doc.get(ref_name):
            ref = frappe.get_doc(doc.get(ref_type), doc.get(ref_name))
            if (ref.doctype in ("File", "Comment") or not _readable_source(ref, rules, reader, blocked)
                    or not _field_allowed(ref, doc.get("attached_to_field"), reader, rules)):
                return None
    return doc


def _record_exclusions(doc, rules):
    fields = _parse_excluded_fields(rules.get(doc.doctype, {}).get("excluded_fields"))
    if getattr(doc.meta, "istable", False):
        fields |= _parse_excluded_fields(rules.get(doc.parenttype, {}).get("excluded_fields"))
    return fields


@frappe.whitelist()
def get_search_result_preview(doc_type, doc_name, query=""):
    doc_type = str(doc_type or "").strip()
    doc_name = str(doc_name or "").strip()
    query = str(query or "").strip()[:4000]
    enabled, _, _, rules = _get_indexing_config(current=True)
    if not enabled or doc_type in _excluded_doctypes() or (doc_type not in rules and not frappe.get_meta(doc_type).istable):
        frappe.throw(frappe._("Search result is unavailable."), frappe.PermissionError)
    reader = frappe.session.user
    doc = frappe.get_doc(doc_type, doc_name)
    parent = _readable_source(doc, rules, reader, _excluded_doctypes())
    if not parent or (doc_type not in rules and parent.doctype not in rules):
        frappe.throw(frappe._("Search result is unavailable."), frappe.PermissionError)
    is_table_doc = bool(getattr(doc.meta, "istable", False))
    rule = rules.get(doc_type, rules.get(parent.doctype, {}))
    excluded = _record_exclusions(doc, rules)
    permitted = _permitted_fields(doc, reader)
    include_comments = rule.get("index_comments", True)
    search_language = current_search_language()
    plan = compile_match_plan(query, language=search_language)

    title = get_document_title(doc, excluded, reader)
    parent_fields, parent_terms, parent_phrase = _collect_doc_preview_fields(
        doc,
        excluded,
        plan,
        query=query,
        reader=reader,
    )
    name_terms, name_phrase = _matched_terms_in_text(doc.name, plan)
    title_terms, title_phrase = _matched_terms_in_text(title, plan)
    parent_terms.update(name_terms)
    parent_terms.update(title_terms)
    parent_phrase = parent_phrase or name_phrase or title_phrase

    if is_table_doc:
        parent_dt = getattr(doc, "parenttype", "") or ""
        parent_name = getattr(doc, "parent", "") or ""
        parent_route = _desk_route(parent_dt, parent_name) if (parent_dt and parent_name) else ""
        return {
            "match_source": "child_table",
            "doc_type": parent_dt or doc_type,
            "doc_name": parent_name or doc_name,
            "title": title,
            "modified": str(getattr(doc, "modified", "") or ""),
            "owner": str(getattr(doc, "owner", "") or ""),
            "status": str(doc.get("status") or "") if "status" not in excluded and _field_allowed(doc, "status", reader, rules) else "",
            "route": parent_route or _desk_route(doc_type, doc_name),
            "fields": [],
            "matched_child_rows": [
                {
                    "table_fieldname": getattr(doc, "parentfield", "") or "",
                    "table_label": doc_type,
                    "child_doctype": doc_type,
                    "row_name": doc.name,
                    "idx": int(getattr(doc, "idx", 0) or 0),
                    "matched_terms": len(parent_terms),
                    "fields": parent_fields,
                }
            ],
            "matched_comments": [],
            "matched_attachments": _matching_attachments(doc, excluded, rule, reader, query),
            "terms": list(plan.get("folded_terms") or ()),
        }

    matched_child_rows = []
    for field in getattr(doc.meta, "fields", []) or []:
        if field.fieldname in excluded:
            continue
        if (field.fieldtype not in ("Table", "Table MultiSelect") or field.options in _excluded_doctypes()
                or not _is_visible_field(field, permitted)):
            continue
        table_label = field.label or field.fieldname
        child_doctype = getattr(field, "options", "") or table_label
        for idx_fallback, row in enumerate(_readable_children(doc, field, reader), start=1):
            row_fields, row_terms, row_phrase = _collect_doc_preview_fields(
                row,
                excluded | _parse_excluded_fields(rules.get(child_doctype, {}).get("excluded_fields")),
                plan,
                query=query,
                reader=reader,
            )
            if not row_terms and not row_phrase:
                continue
            row_idx = int(getattr(row, "idx", 0) or idx_fallback)
            matched_child_rows.append(
                {
                    "table_fieldname": field.fieldname,
                    "table_label": table_label,
                    "child_doctype": child_doctype,
                    "row_name": str(getattr(row, "name", "") or ""),
                    "idx": row_idx,
                    "matched_terms": len(row_terms),
                    "_phrase_hit": row_phrase,
                    "fields": row_fields,
                }
            )

    matched_child_rows.sort(
        key=lambda item: (
            1 if item.get("_phrase_hit") else 0,
            int(item.get("matched_terms") or 0),
            -int(item.get("idx") or 0),
        ),
        reverse=True,
    )
    matched_child_rows = matched_child_rows[:MAX_PREVIEW_CHILD_ROWS]
    best_child_phrase = bool(matched_child_rows and matched_child_rows[0].get("_phrase_hit"))
    best_child_terms = (
        int(matched_child_rows[0].get("matched_terms") or 0)
        if matched_child_rows
        else 0
    )
    for item in matched_child_rows:
        item.pop("_phrase_hit", None)

    matched_comments = []
    if include_comments and plan.get("folded_terms"):
        comment_rows = _comment_rows(doc_type, [doc_name], reader=reader, limit=50, newest=True)
        for crow in comment_rows:
            clean_comment = _plain_text(crow.get("content"), "Text Editor")
            c_terms, c_phrase = _matched_terms_in_text(clean_comment, plan)
            if c_terms or c_phrase:
                matched_comments.append(
                    {
                        "name": crow.get("name") or "",
                        "owner": crow.get("owner") or "",
                        "creation": str(crow.get("creation") or ""),
                        "content": _format_preview_value(
                            clean_comment,
                            "Text",
                            query=query,
                            folded_terms=plan.get("folded_terms") or (),
                        ),
                    }
                )
                if len(matched_comments) >= 5:
                    break

    child_is_primary = bool(
        matched_child_rows
        and (
            (not parent_terms and not parent_phrase)
            or (best_child_phrase and not parent_phrase)
            or (best_child_terms > len(parent_terms) and not parent_phrase)
        )
    )

    return {
        "match_source": "child_table" if child_is_primary else "doctype",
        "doc_type": doc_type,
        "doc_name": doc_name,
        "title": title,
        "modified": str(getattr(doc, "modified", "") or ""),
        "owner": str(getattr(doc, "owner", "") or ""),
        "status": str(doc.get("status") or "") if "status" not in excluded and _field_allowed(doc, "status", reader, rules) else "",
        "route": _desk_route(doc_type, doc_name),
        "fields": parent_fields,
        "matched_child_rows": matched_child_rows,
        "matched_comments": matched_comments,
        "matched_attachments": _matching_attachments(doc, excluded, rule, reader, query),
        "terms": list(plan.get("folded_terms") or ()),
    }


def _parse_excluded_fields(raw_excluded):
    if not raw_excluded:
        return frozenset()
    if isinstance(raw_excluded, (list, tuple, set, frozenset)):
        items = raw_excluded
    else:
        items = str(raw_excluded).replace("\n", ",").split(",")
    return frozenset(str(item).strip() for item in items if str(item).strip())


def get_document_content(doc, excluded_fields=None, *, reader=None, rules=None):
    rules = rules or {}
    excluded = _parse_excluded_fields(excluded_fields)
    permitted = _permitted_fields(doc, reader)
    blocked = _excluded_doctypes() if reader is not None else ()
    content_parts = []
    for field in doc.meta.fields:
        if field.fieldname in excluded or not _is_visible_field(field, permitted):
            continue
        if field.fieldtype in SEARCHABLE_FIELD_TYPES:
            val = doc.get(field.fieldname)
            if val not in (None, ""):
                text = _plain_text(val, field.fieldtype)
                if text:
                    content_parts.append(f"{field.label or field.fieldname}: {text}")
        elif field.fieldtype in ("Table", "Table MultiSelect") and getattr(field, "options", None) not in blocked:
            children_excluded = excluded | _parse_excluded_fields(rules.get(getattr(field, "options", None), {}).get("excluded_fields"))
            for row in _readable_children(doc, field, reader):
                content_parts.append(get_document_content(row, children_excluded, reader=reader, rules=rules))
    return " ".join(part for part in content_parts if part)


def _comment_rows(doc_type, names, *, reader=None, limit=0, newest=False):
    rows = frappe.get_all(
        "Comment",
        filters={"reference_doctype": doc_type, "reference_name": ["in", names], "comment_type": "Comment"},
        fields=["name", "reference_name", "owner", "creation", "content"],
        order_by="creation desc, name desc" if newest else "creation asc, name asc",
        limit_page_length=limit,
    )
    if reader is None or reader is _INDEX_FIELDS:
        return rows
    permitted = []
    for row in rows:
        comment = frappe.get_doc("Comment", row["name"])
        if (comment.has_permission("read", user=reader) and _listed(comment)
                and "content" in _permitted_fields(comment, reader)):
            permitted.append(row)
    return permitted


def get_document_comments(doc_type, doc_names, *, reader=None):
    names = list(dict.fromkeys(str(name) for name in doc_names if name))
    if not names:
        return {}
    comments = {name: [] for name in names}
    for row in _comment_rows(doc_type, names, reader=reader):
        reference_name = row.get("reference_name")
        content = _plain_text(row.get("content"), "Text Editor")
        if reference_name in comments and content:
            comments[reference_name].append(content)
    return comments


def _attachment_texts(doc, excluded, reader=None):
    doc_type, doc_name = doc.doctype, doc.name
    files = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": doc_type,
            "attached_to_name": doc_name,
            "is_folder": 0,
        },
        fields=["file_url", "name", "attached_to_field", "is_private"],
        limit_page_length=0,
    )
    if not files:
        return

    from document_management.frappe_document_management.utils.file_crypto import (
        decrypted_temp_file,
    )

    total_chars = 0
    pdfplumber_mod = None
    for file_data in files:
        parts = []
        if total_chars >= MAX_ATTACHMENT_CHARS:
            break
        attached_field = file_data.get("attached_to_field")
        if attached_field:
            field = doc.meta.get_field(attached_field)
            if (attached_field in excluded or not field
                    or not _is_visible_field(field, _permitted_fields(doc, reader))):
                continue
        file_url = file_data.get("file_url") or ""
        prefix = "/private/files/" if file_data.get("is_private") else "/files/"
        ext = os.path.splitext(file_url)[1].lower()
        if not file_url.startswith(prefix) or ext not in INDEXABLE_ATTACHMENT_EXTENSIONS:
            continue
        file_doc = frappe.get_doc("File", file_data["name"])
        if reader is not None and reader is not _INDEX_FIELDS and (
            not file_doc.has_permission("read", user=reader) or not _listed(file_doc)
            or "file_url" not in _permitted_fields(file_doc, reader)
        ):
            continue
        root = Path(frappe.get_site_path("private" if file_doc.is_private else "public", "files")).resolve()
        try:
            file_path = Path(file_doc.get_full_path()).resolve()
        except (frappe.PermissionError, frappe.ValidationError, OSError):
            continue
        if not file_path.is_relative_to(root) or not file_path.is_file():
            continue
        try:
            if os.path.getsize(file_path) > MAX_ATTACHMENT_FILE_BYTES:
                continue
        except OSError:
            continue
        try:
            with decrypted_temp_file(file_url=file_url, path=file_path) as readable_path:
                if ext in {".txt", ".md"}:
                    with open(readable_path, "r", encoding="utf-8", errors="ignore") as handle:
                        raw_text = handle.read(MAX_ATTACHMENT_CHARS - total_chars).strip()
                    if raw_text:
                        parts.append(raw_text)
                        total_chars += len(raw_text)
                elif ext == ".pdf":
                    if pdfplumber_mod is None:
                        import pdfplumber as pdfplumber_mod
                    with pdfplumber_mod.open(readable_path) as pdf:
                        for page in pdf.pages[:MAX_ATTACHMENT_PAGES]:
                            if total_chars >= MAX_ATTACHMENT_CHARS:
                                break
                            text = (page.extract_text() or "").strip()
                            if text:
                                remaining = MAX_ATTACHMENT_CHARS - total_chars
                                chunk = text[:remaining]
                                parts.append(chunk)
                                total_chars += len(chunk)
        except Exception as exc:
            frappe.log_error(
                title="OCR Error",
                message=f"Error extracting attachment text for {doc_type} {doc_name}: {exc}",
            )
        if parts:
            yield file_doc, "\n".join(parts)


def _extract_pdf_attachments_text(doc, excluded, reader=None):
    return "\n".join(text for _, text in _attachment_texts(doc, excluded, reader))


def _matching_attachments(doc, excluded, rule, reader, query):
    if not query or not rule.get("index_attachments"):
        return []
    matcher = TextQuery(query)
    attachments = []
    for file, text in _attachment_texts(doc, excluded, reader):
        if matcher.matches("", "", text, relaxed=True, fuzzy=True):
            attachments.append({
                "title": (file.file_name if "file_name" in _permitted_fields(file, reader) else file.name),
                "route": _desk_route("File", file.name),
                "excerpt": make_excerpt(text, query, maximum=280),
            })
    return attachments


def get_indexable_content(
    doc,
    comments=None,
    include_comments=True,
    include_attachments=False,
    excluded_fields=None,
    *, reader=None, rules=None,
):
    content_parts = [get_document_content(doc, excluded_fields=excluded_fields, reader=reader, rules=rules)]
    if include_comments:
        if comments is None:
            comments = get_document_comments(doc.doctype, [doc.name], reader=reader).get(
                doc.name,
                [],
            )
        for comment in comments:
            text = _plain_text(comment, "Text Editor")
            if text:
                content_parts.append(f"Comment: {text}")
    if include_attachments:
        attachment_text = _extract_pdf_attachments_text(doc, _parse_excluded_fields(excluded_fields), reader)
        if attachment_text:
            content_parts.append(f"Attachment: {attachment_text}")
    return " ".join(part for part in content_parts if part)


def get_document_title(doc, excluded=(), reader=None):
    title_field = getattr(doc.meta, "title_field", None)
    if not title_field or title_field in excluded:
        return doc.name
    try:
        field = doc.meta.get_field(title_field)
    except Exception:
        return doc.name
    if not field or not _is_previewable_field(field, _permitted_fields(doc, reader)):
        return doc.name
    return _plain_text(doc.get(title_field), field.fieldtype) or doc.name


def _comment_reference(doc):
    if doc.doctype != "Comment":
        return None
    if doc.get("comment_type") != "Comment":
        return None
    doc_type = doc.get("reference_doctype")
    doc_name = doc.get("reference_name")
    if not doc_type or not doc_name:
        return None
    return doc_type, doc_name


def _attachment_reference(doc):
    if doc.doctype != "File":
        return None
    if doc.get("is_folder"):
        return None
    file_url = str(doc.get("file_url") or "")
    ext = os.path.splitext(file_url)[1].lower()
    if ext not in INDEXABLE_ATTACHMENT_EXTENSIONS:
        return None
    doc_type = doc.get("attached_to_doctype")
    doc_name = doc.get("attached_to_name")
    if not doc_type or not doc_name or doc_type in {"Document", "Document Version"}:
        return None
    return doc_type, doc_name


def _enqueue_indexing(doc_type, doc_name, queue="short", timeout=300):
    frappe.enqueue(
        "document_management.search.indexer.run_indexing_background",
        doc_type=doc_type,
        doc_name=doc_name,
        queue=queue,
        timeout=timeout,
        enqueue_after_commit=True,
        job_id=f"dm_idx:{doc_type}:{doc_name}",
        deduplicate=True,
    )


INDEXING_CONFIG_CACHE_KEY = "document_management:indexing_config_v3"
SKIPPED_INDEX_DOCTYPES = frozenset(
    {
        "Access Log",
        "Activity Log",
        "Document Chat Message",
        "Document Chat Session",
        "Document Chunk",
        "Document Page",
        "Document Saved View",
        "Document Share Link",
        "Email Queue",
        "Error Log",
        "Notification Log",
        "RQ Job",
        "Route History",
        "Scheduled Job Log",
        "Session",
        "Token Cache",
        "Version",
        "View Log",
    }
)


def clear_indexing_config_cache():
    try:
        frappe.cache().delete_value(INDEXING_CONFIG_CACHE_KEY)
        frappe.cache().delete_value("document_management:indexing_config")
    except Exception:
        pass
    tantivy_backend.clear_index_cache()


def _doctype_rules_from_settings(settings):
    rules = {}
    blocked = _excluded_doctypes()
    for row in settings.get("indexed_doctypes", []) or []:
        doctype = getattr(row, "document_type", None)
        if not doctype or doctype in blocked:
            continue
        raw_comments = getattr(row, "index_comments", None)
        index_comments = True if raw_comments is None else bool(int(raw_comments))
        index_attachments = bool(int(getattr(row, "index_attachments", 0) or 0))
        excluded = sorted(_parse_excluded_fields(getattr(row, "excluded_fields", "")))
        rules[doctype] = {
            "index_comments": index_comments,
            "index_attachments": index_attachments,
            "excluded_fields": excluded,
        }
    return rules


def _get_indexing_config(*, current=False):
    use_cache = not current and bool(getattr(frappe.local, "site", None)) and not frappe.flags.in_test
    if use_cache:
        try:
            cached = frappe.cache().get_value(INDEXING_CONFIG_CACHE_KEY)
            if isinstance(cached, dict) and "doctype_rules" in cached:
                rules = cached.get("doctype_rules") or {}
                return (
                    bool(cached.get("enable_full_text_search")),
                    bool(cached.get("enable_semantic_search")),
                    set(rules.keys()),
                    rules,
                )
        except Exception:
            pass

    settings = frappe.get_single("Document Management Settings")
    full_text = bool(getattr(settings, "enable_full_text_search", False))
    semantic = bool(getattr(settings, "enable_semantic_search", False)) and not standalone_ai_disabled()
    rules = _doctype_rules_from_settings(settings)
    if use_cache:
        try:
            frappe.cache().set_value(
                INDEXING_CONFIG_CACHE_KEY,
                {
                    "enable_full_text_search": full_text,
                    "enable_semantic_search": semantic,
                    "doctype_rules": rules,
                },
                expires_in_sec=300,
            )
        except Exception:
            pass
    return full_text, semantic, set(rules.keys()), rules


def handle_doc_save(doc, method=None):
    if doc.doctype in SKIPPED_INDEX_DOCTYPES or (doc.doctype == "Document" and not is_standalone(doc)):
        return
    try:
        enable_full_text, enable_semantic, indexed_doctypes, rules = _get_indexing_config()
    except Exception:
        return

    comment_reference = _comment_reference(doc)
    if comment_reference:
        doc_type, doc_name = comment_reference
        if (
            enable_full_text
            and doc_type in indexed_doctypes
            and rules.get(doc_type, {}).get("index_comments", True)
        ):
            _enqueue_indexing(doc_type, doc_name)
        return

    attachment_reference = _attachment_reference(doc)
    if attachment_reference:
        doc_type, doc_name = attachment_reference
        if (
            enable_full_text
            and doc_type in indexed_doctypes
            and rules.get(doc_type, {}).get("index_attachments", False)
        ):
            _enqueue_indexing(doc_type, doc_name, queue="long", timeout=600)
        return

    should_index_generic = (
        enable_full_text
        and doc.doctype != "Document"
        and doc.doctype in indexed_doctypes
    )
    should_index_rag = (
        doc.doctype == "Document"
        and enable_semantic
    )
    if not should_index_generic and not should_index_rag:
        return

    # Enqueue the heavy embedding process (or soft-delete removal) so it doesn't block the UI
    _enqueue_indexing(doc.doctype, doc.name)


def run_indexing_background(doc_type, doc_name):
    try:
        settings = frappe.get_single("Document Management Settings")
        doc = frappe.get_doc(doc_type, doc_name)
    except Exception:
        return

    rules = _doctype_rules_from_settings(settings)
    if doc_type == "Document":
        if not is_standalone(doc) or standalone_ai_disabled():
            return
        if doc.get("is_deleted"):
            semantic_backend.remove_document(doc_type, doc_name)
        elif settings.enable_semantic_search:
            semantic_backend.index_document(doc_type, doc_name)
        return
    if not settings.enable_full_text_search or doc_type not in rules or not _indexable_source(doc, rules, _excluded_doctypes()):
        return

    rule = rules[doc_type]
    excluded = _record_exclusions(doc, rules)
    content = get_indexable_content(
        doc,
        include_comments=rule["index_comments"],
        include_attachments=rule["index_attachments"],
        excluded_fields=excluded,
        reader=_INDEX_FIELDS,
        rules=rules,
    )
    title = get_document_title(doc, excluded, _INDEX_FIELDS)

    if (
        settings.enable_full_text_search
        and doc_type != "Document"
        and doc_type in rules
    ):
        try:
            tantivy_backend.index_document(doc_type, doc_name, title, content)
        except tantivy_backend.IndexBusy:
            frappe.enqueue(
                "document_management.search.indexer.run_indexing_background",
                doc_type=doc_type,
                doc_name=doc_name,
                queue="long",
                timeout=7200,
            )
            return
        except tantivy_backend.IndexRebuildRequired:
            return
        except Exception as exc:
            frappe.log_error(title="Tantivy Indexing Error", message=str(exc))


def handle_doc_trash(doc, method=None):
    if doc.doctype in SKIPPED_INDEX_DOCTYPES or (doc.doctype == "Document" and not is_standalone(doc)):
        return
    try:
        enable_full_text, enable_semantic, indexed_doctypes, rules = _get_indexing_config()
        comment_reference = _comment_reference(doc)
        if comment_reference:
            doc_type, doc_name = comment_reference
            if (
                enable_full_text
                and doc_type in indexed_doctypes
                and rules.get(doc_type, {}).get("index_comments", True)
            ):
                _enqueue_indexing(doc_type, doc_name)
            return
        attachment_reference = _attachment_reference(doc)
        if attachment_reference:
            doc_type, doc_name = attachment_reference
            if (
                enable_full_text
                and doc_type in indexed_doctypes
                and rules.get(doc_type, {}).get("index_attachments", False)
            ):
                _enqueue_indexing(doc_type, doc_name, queue="long", timeout=600)
            return
        if (
            enable_full_text
            and doc.doctype != "Document"
            and doc.doctype in indexed_doctypes
        ):
            frappe.enqueue(
                "document_management.search.indexer.remove_from_index_background",
                doc_type=doc.doctype,
                doc_name=doc.name,
                queue="short",
                timeout=300,
                enqueue_after_commit=True,
            )
        if enable_semantic and doc.doctype == "Document":
            frappe.enqueue(
                "document_management.search.indexer.remove_from_rag_background",
                doc_type=doc.doctype,
                doc_name=doc.name,
                queue="long",
                timeout=600,
                enqueue_after_commit=True,
            )
    except Exception:
        pass


def handle_doc_rename(doc, method=None, old_name=None, new_name=None, merge=False):
    if doc.doctype in SKIPPED_INDEX_DOCTYPES or (doc.doctype == "Document" and not is_standalone(doc)):
        return
    try:
        enable_full_text, enable_semantic, indexed_doctypes, _ = _get_indexing_config()
    except Exception:
        return

    final_name = new_name or doc.name
    if (
        enable_full_text
        and doc.doctype != "Document"
        and doc.doctype in indexed_doctypes
    ):
        if old_name and old_name != final_name:
            frappe.enqueue(
                "document_management.search.indexer.remove_from_index_background",
                doc_type=doc.doctype,
                doc_name=old_name,
                queue="short",
                timeout=300,
                enqueue_after_commit=True,
            )
        if final_name:
            _enqueue_indexing(doc.doctype, final_name)
    elif enable_semantic and doc.doctype == "Document":
        if old_name and old_name != final_name:
            frappe.enqueue(
                "document_management.search.indexer.remove_from_rag_background",
                doc_type="Document",
                doc_name=old_name,
                queue="long",
                timeout=600,
                enqueue_after_commit=True,
            )
        if final_name:
            _enqueue_indexing("Document", final_name)


def remove_from_index_background(doc_type, doc_name):
    try:
        tantivy_backend.remove_document(doc_type, doc_name)
    except tantivy_backend.IndexBusy:
        frappe.enqueue(
            "document_management.search.indexer.remove_from_index_background",
            doc_type=doc_type,
            doc_name=doc_name,
            queue="long",
            timeout=7200,
        )
    except tantivy_backend.IndexRebuildRequired:
        return


def remove_from_rag_background(doc_type, doc_name):
    try:
        semantic_backend.remove_document(doc_type, doc_name)
    except Exception:
        frappe.log_error(
            title="Semantic Index Removal Error",
            message=frappe.get_traceback(),
        )


def _parse_doctypes(doctypes):
    if not doctypes:
        return set()
    if isinstance(doctypes, str):
        try:
            doctypes = json.loads(doctypes)
        except (TypeError, ValueError):
            doctypes = [doctypes]
    if not isinstance(doctypes, (list, tuple, set)):
        return set()
    return {str(value).strip() for value in doctypes if str(value).strip()}


def _normalize_date_param(raw):
    value = str(raw or "").strip()[:10]
    if not value:
        return None
    try:
        from frappe.utils import getdate

        return getdate(value).isoformat()
    except Exception:
        return None


def _matches_date_range(modified_value, date_from, date_to):
    if not date_from and not date_to:
        return True
    date_part = str(modified_value or "")[:10]
    if len(date_part) != 10:
        return False
    if date_from and date_part < date_from:
        return False
    if date_to and date_part > date_to:
        return False
    return True


def _visible_result(hit, reader, rules, blocked, matcher, plan, date_from, date_to):
    """Use an index hit only for its identity. All returned data comes from a current, allowed source."""
    doctype, name = hit["doc_type"], hit["doc_name"]
    if doctype not in rules or doctype in blocked:
        return None
    try:
        doc = frappe.get_doc(doctype, name)
        parent = _readable_source(doc, rules, reader, blocked)
        if not parent or not _matches_date_range(doc.modified, date_from, date_to):
            return None
        rule = rules[doctype]
        excluded = _record_exclusions(doc, rules)
        title = get_document_title(doc, excluded, reader)
        content = get_indexable_content(
            doc, include_comments=rule.get("index_comments", True),
            include_attachments=rule.get("index_attachments", False),
            excluded_fields=excluded, reader=reader, rules=rules,
        )
    except (frappe.DoesNotExistError, frappe.PermissionError):
        return None
    fuzzy = False
    if not matcher.matches(name, title, content, relaxed=True):
        if not matcher.matches(name, title, content, relaxed=True, fuzzy=True):
            return None
        fuzzy = True
    details = search_match_details(matcher.query, name, title, content, plan=plan)
    if fuzzy:
        details["match_type"] = "fuzzy"
    route = _desk_route(parent.doctype, parent.name)
    child_row = None
    if parent is not doc:
        child_row = {"row_name": name, "idx": doc.idx, "table_fieldname": doc.parentfield}
        route += "?" + urlencode({"dm_child_field": doc.parentfield, "dm_child_row": name, "dm_child_idx": doc.idx})
    return {
        "source": "full_text", "doc_type": doctype, "doc_name": name, "title": title,
        "form_doctype": parent.doctype, "form_name": parent.name, "child_row": child_row, "route": route,
        "modified": str(doc.modified or ""), **details, "score": details["quality"] + details["coverage"],
        "excerpt": make_excerpt(content, matcher.query, maximum=280, folded_terms=plan["folded_terms"]),
    }


def _sort_records(records, sort_by):
    if sort_by == "modified_desc":
        return sorted(records, key=lambda r: r.get("modified") or "", reverse=True)
    if sort_by == "modified_asc":
        return sorted(records, key=lambda r: r.get("modified") or "")
    if sort_by == "title_asc":
        return sorted(
            records,
            key=lambda r: (
                str(r.get("title") or "").lower(),
                str(r.get("doc_name") or "").lower(),
            ),
        )
    return records


@frappe.whitelist()
def search(
    query,
    page=1,
    page_length=25,
    doctypes=None,
    limit=None,
    sort_by="relevance",
    date_from=None,
    date_to=None,
):
    started_at = time.perf_counter()
    query = str(query or "").strip()[:4000]
    sort_by = str(sort_by or "relevance").strip()
    if sort_by not in ALLOWED_SORT_MODES:
        sort_by = "relevance"
    date_from = _normalize_date_param(date_from)
    date_to = _normalize_date_param(date_to)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    if not query:
        return {
            "exact": [],
            "facets": {},
            "sort_by": sort_by,
            "date_from": date_from,
            "date_to": date_to,
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
            "pagination": {
                "page": 1,
                "page_length": 25,
                "has_previous": False,
                "has_more": False,
            },
        }
    try:
        page = max(int(page), 1)
    except (TypeError, ValueError):
        page = 1
    if limit is not None and page_length in (None, "", 25, "25"):
        page_length = limit
    try:
        page_length = min(max(int(page_length), 1), 100)
    except (TypeError, ValueError):
        page_length = 25

    enable_full_text, _, indexed_doctypes, rules = _get_indexing_config(current=True)
    configured = {dt for dt in indexed_doctypes if dt and dt != "Document"}
    requested = _parse_doctypes(doctypes)
    generic_filter = (requested & configured) if requested else configured

    parsed_syntax = parse_search_syntax(query)
    highlight_source = (
        f'{parsed_syntax["positive_text"]} {" ".join(parsed_syntax["phrases"])}'.strip()
        if (parsed_syntax["phrases"] or parsed_syntax["excluded"])
        else query
    )
    search_language = current_search_language()
    terms = significant_terms(highlight_source, language=search_language)

    results = {
        "exact": [],
        "facets": {},
        "facets_truncated": False,
        "fuzzy_fallback": False,
        "configured_doctypes": sorted(dt for dt in configured if _can_offer_doctype(dt)),
        "search_language": search_language,
        "terms": terms,
        "sort_by": sort_by,
        "date_from": date_from,
        "date_to": date_to,
        "pagination": {
            "page": page,
            "page_length": page_length,
            "has_previous": page > 1,
            "has_more": False,
        },
    }
    if not generic_filter:
        results["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
        return results

    if enable_full_text:
        try:
            target_start = (page - 1) * page_length
            limit_rows = target_start + page_length + 1
            if sort_by != "relevance":
                limit_rows = max(limit_rows, 500)
            matcher = TextQuery(query, language=search_language)
            plan = compile_match_plan(query, language=search_language)
            allowed, fuzzy = [], []
            blocked = _excluded_doctypes()
            for batch in tantivy_backend.iter_search(
                matcher.candidate_query, doctypes=sorted(generic_filter), batch_size=max(page_length * 2, 50),
                include_excerpt=False, include_fuzzy_candidates=True,
            ):
                for hit in batch:
                    if hit["doc_type"] not in generic_filter:
                        continue
                    row = _visible_result(hit, frappe.session.user, rules, blocked, matcher, plan, date_from, date_to)
                    if row is None:
                        continue
                    if row["match_type"] == "fuzzy":
                        if len(fuzzy) < limit_rows:
                            fuzzy.append(row)
                        continue
                    allowed.append(row)
                    if len(allowed) >= limit_rows:
                        break
                if len(allowed) >= limit_rows:
                    break
            rows = allowed or fuzzy
            page_rows = _sort_records(rows, sort_by)[target_start:target_start + page_length + 1]
            results["exact"] = page_rows[:page_length]
            for row in rows:
                key = row["doc_type"]
                results["facets"][key] = results["facets"].get(key, 0) + 1
            results["facets_truncated"] = len(rows) == limit_rows
            results["fuzzy_fallback"] = bool(fuzzy and not allowed)
            results["pagination"].update({
                "has_more": len(page_rows) > page_length,
                "from": target_start + 1 if results["exact"] else 0,
                "to": target_start + len(results["exact"]),
            })
        except tantivy_backend.IndexRebuildRequired as exc:
            results["exact_rebuild_required"] = True
            results["exact_error"] = str(exc)
        except Exception:
            frappe.log_error(
                title="Full Text Search Error",
                message=frappe.get_traceback(),
            )
            results["exact_error"] = "Full-text search is temporarily unavailable."

    results["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
    return results


def _can_offer_doctype(doctype):
    meta = frappe.get_meta(doctype)
    if not meta.istable:
        return frappe.has_permission(doctype, "read")
    # Only offer configured child tables whose parent table field can be read.
    for parent in frappe.get_all("DocField", filters={"options": doctype, "fieldtype": ["in", ["Table", "Table MultiSelect"]]}, pluck="parent"):
        if frappe.has_permission(parent, "read"):
            parent_meta = frappe.get_meta(parent)
            fields = parent_meta.get_permitted_fieldnames(user=frappe.session.user)
            if any(field.options == doctype and field.fieldname in fields and not field.hidden for field in parent_meta.get_table_fields()):
                return True
    return False


@frappe.whitelist()
def get_search_options():
    settings = frappe.get_single("Document Management Settings")
    options_dict = {}
    for row in settings.get("indexed_doctypes", []):
        doctype = row.document_type
        if not doctype or doctype in _excluded_doctypes():
            continue
        try:
            if _can_offer_doctype(doctype):
                options_dict[doctype] = {"value": doctype, "label": doctype}
        except Exception:
            continue
    options = list(options_dict.values())

    is_manager = "System Manager" in (frappe.get_roles() or [])
    indexed_count = 0 if is_manager else None
    if is_manager and settings.enable_full_text_search and tantivy_backend.index_exists():
        try:
            index = tantivy_backend.get_index()
            searcher = index.searcher()
            indexed_count = searcher.num_docs
        except Exception:
            pass

    return {
        "doctypes": sorted(options, key=lambda row: row["label"]),
        "full_text_enabled": bool(settings.enable_full_text_search),
        "generic_index_ready": tantivy_backend.index_exists(),
        "indexed_count": indexed_count,
    }


@frappe.whitelist()
def get_index_status():
    if "System Manager" not in (frappe.get_roles() or []):
        frappe.throw("System Manager role is required.", frappe.PermissionError)
    settings = frappe.get_single("Document Management Settings")
    configured = sorted(_doctype_rules_from_settings(settings))
    stats = tantivy_backend.get_index_stats(configured)
    return {
        "full_text_enabled": bool(settings.enable_full_text_search),
        "configured_doctypes": configured,
        **stats,
    }


@frappe.whitelist()
def enqueue_rebuild_index():
    frappe.has_permission("Document Management Settings", "write", throw=True)
    job = frappe.enqueue(
        "document_management.search.indexer.rebuild_index",
        queue="long",
        timeout=7200,
        enqueue_after_commit=True,
    )
    return {"job_id": getattr(job, "id", None)}


@frappe.whitelist()
def rebuild_index():
    frappe.has_permission("Document Management Settings", "write", throw=True)
    settings = frappe.get_single("Document Management Settings")
    rules = _doctype_rules_from_settings(settings)
    indexed_doctypes = [dt for dt in rules if dt != "Document"]

    semantic_enabled = settings.enable_semantic_search and not standalone_ai_disabled()
    if not indexed_doctypes and not semantic_enabled:
        return {"status": "error", "message": "No doctypes configured for indexing"}

    def records():
        for dt in indexed_doctypes:
            rule = rules.get(dt, {})
            include_comments = rule.get("index_comments", True)
            include_attachments = rule.get("index_attachments", False)
            excluded_fields = rule.get("excluded_fields")
            batch_size = 1000
            offset = 0
            while True:
                docs = frappe.get_all(
                    dt,
                    fields=["name"],
                    limit_start=offset,
                    limit_page_length=batch_size,
                    order_by="creation asc, name asc",
                )
                if not docs:
                    break
                comments = (
                    get_document_comments(dt, [row.name for row in docs])
                    if include_comments
                    else {}
                )
                for row in docs:
                    try:
                        doc = frappe.get_doc(dt, row.name)
                        if not _indexable_source(doc, rules, _excluded_doctypes()):
                            continue
                        excluded_fields = _record_exclusions(doc, rules)
                        yield {
                            "doc_type": dt,
                            "doc_name": row.name,
                            "title": get_document_title(doc, excluded_fields, _INDEX_FIELDS),
                            "content": get_indexable_content(
                                doc,
                                comments=comments.get(row.name, []),
                                include_comments=include_comments,
                                include_attachments=include_attachments,
                                excluded_fields=excluded_fields, reader=_INDEX_FIELDS, rules=rules,
                            ),
                        }
                    except Exception:
                        frappe.log_error(
                            title=f"Error indexing {dt} {row.name}",
                            message=frappe.get_traceback(),
                        )
                frappe.local.cache = {}
                if hasattr(frappe.local, "document_cache"):
                    frappe.local.document_cache = {}
                offset += batch_size

    generic_result = None
    if settings.enable_full_text_search:
        generic_result = tantivy_backend.rebuild(records())
    total_indexed = generic_result["count"] if generic_result else 0

    rag_result = None
    if semantic_enabled:
        from document_management.frappe_document_management.rag.index import rebuild_index as rebuild_rag

        rag_result = rebuild_rag()

    try:
        frappe.publish_realtime(
            INDEX_REBUILT_EVENT,
            {
                "count": total_indexed,
                "generation": generic_result.get("generation") if generic_result else None,
                "rebuilt_at": generic_result.get("rebuilt_at") if generic_result else None,
            },
            after_commit=True,
        )
    except Exception:
        pass

    return {
        "status": "success",
        "message": f"Rebuilt index for {total_indexed} documents.",
        "generation": generic_result.get("generation") if generic_result else None,
        "rebuilt_at": generic_result.get("rebuilt_at") if generic_result else None,
        "rag": rag_result,
    }


@frappe.whitelist()
def extract_and_index_ocr(doc_type, doc_name):
    doc = frappe.get_doc(doc_type, doc_name)
    doc.check_permission("write")
    if doc_type == "Document":
        if not is_standalone(doc):
            frappe.throw(frappe._("Document is unavailable."), frappe.PermissionError)
        from document_management.frappe_document_management.page.document_management_console.document_management_console import (
            reprocess_ocr,
        )

        return reprocess_ocr(doc_name)

    enabled, _, _, rules = _get_indexing_config(current=True)
    if not enabled or doc_type not in rules or not _readable_source(doc, rules, frappe.session.user, _excluded_doctypes()):
        frappe.throw(frappe._("Search result is unavailable."), frappe.PermissionError)
    extracted_text = ""
    if rules[doc_type]["index_attachments"]:
        extracted_text = _extract_pdf_attachments_text(doc, _record_exclusions(doc, rules), reader=frappe.session.user)
    _enqueue_indexing(doc_type, doc_name, queue="long", timeout=600)
    return {"status": "success", "extracted_chars": len(extracted_text)}
