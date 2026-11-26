import json
import os
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote

import frappe
from document_management.search import semantic_backend, tantivy_backend
from document_management.search.query import (
    current_search_language,
    fold_text,
    make_excerpt,
    parse_search_syntax,
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


def _is_visible_field(field):
    try:
        permlevel = int(getattr(field, "permlevel", 0) or 0)
    except (TypeError, ValueError):
        permlevel = 0
    return permlevel == 0 and not bool(getattr(field, "hidden", False))


def _is_searchable_field(field):
    return (
        field.fieldtype in SEARCHABLE_FIELD_TYPES
        and _is_visible_field(field)
    )


def _parse_excluded_fields(raw_excluded):
    if not raw_excluded:
        return frozenset()
    if isinstance(raw_excluded, (list, tuple, set, frozenset)):
        items = raw_excluded
    else:
        items = str(raw_excluded).replace("\n", ",").split(",")
    return frozenset(str(item).strip() for item in items if str(item).strip())


def get_document_content(doc, excluded_fields=None):
    excluded = _parse_excluded_fields(excluded_fields)
    content_parts = []
    for field in doc.meta.fields:
        if field.fieldname in excluded:
            continue
        if _is_searchable_field(field):
            val = doc.get(field.fieldname)
            if val not in (None, ""):
                text = _plain_text(val, field.fieldtype)
                if text:
                    content_parts.append(
                        f"{field.label or field.fieldname}: {text}"
                    )
        elif field.fieldtype == "Table" and _is_visible_field(field):
            for row in doc.get(field.fieldname) or []:
                row_values = []
                for child_field in row.meta.fields:
                    if child_field.fieldname in excluded:
                        continue
                    if not _is_searchable_field(child_field):
                        continue
                    value = row.get(child_field.fieldname)
                    if value not in (None, ""):
                        text = _plain_text(value, child_field.fieldtype)
                        if text:
                            row_values.append(
                                f"{child_field.label or child_field.fieldname}: {text}"
                            )
                if row_values:
                    content_parts.append(" | ".join(row_values))
    return " ".join(content_parts)


def get_document_comments(doc_type, doc_names):
    names = list(dict.fromkeys(str(name) for name in doc_names if name))
    if not names:
        return {}

    comments = {name: [] for name in names}
    rows = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": doc_type,
            "reference_name": ["in", names],
            "comment_type": "Comment",
        },
        fields=["reference_name", "content"],
        order_by="creation asc, name asc",
    )
    for row in rows:
        reference_name = row.get("reference_name")
        content = _plain_text(row.get("content"), "Text Editor")
        if reference_name in comments and content:
            comments[reference_name].append(content)
    return comments


def _extract_pdf_attachments_text(doc_type, doc_name):
    files = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": doc_type,
            "attached_to_name": doc_name,
            "is_folder": 0,
        },
        fields=["file_url", "name"],
        limit_page_length=0,
    )
    if not files:
        return ""

    from frappe.utils.file_manager import get_file_path
    from document_management.frappe_document_management.utils.file_crypto import (
        decrypted_temp_file,
    )

    parts = []
    total_chars = 0
    pdfplumber_mod = None
    for file_data in files:
        if total_chars >= MAX_ATTACHMENT_CHARS:
            break
        file_url = (
            file_data.get("file_url")
            if hasattr(file_data, "get")
            else getattr(file_data, "file_url", None)
        )
        if not file_url:
            continue
        ext = os.path.splitext(file_url)[1].lower()
        if ext not in INDEXABLE_ATTACHMENT_EXTENSIONS:
            continue
        file_path = get_file_path(file_url)
        if not file_path or not os.path.exists(file_path):
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
    return "\n".join(part for part in parts if part)


def get_indexable_content(
    doc,
    comments=None,
    include_comments=True,
    include_attachments=False,
    excluded_fields=None,
):
    content_parts = [get_document_content(doc, excluded_fields=excluded_fields)]
    if include_comments:
        if comments is None:
            comments = get_document_comments(doc.doctype, [doc.name]).get(
                doc.name,
                [],
            )
        for comment in comments:
            text = _plain_text(comment, "Text Editor")
            if text:
                content_parts.append(f"Comment: {text}")
    if include_attachments:
        attachment_text = _extract_pdf_attachments_text(doc.doctype, doc.name)
        if attachment_text:
            content_parts.append(f"Attachment: {attachment_text}")
    return " ".join(part for part in content_parts if part)


def get_document_title(doc):
    title_field = getattr(doc.meta, "title_field", None)
    if not title_field:
        return doc.name
    try:
        field = doc.meta.get_field(title_field)
    except Exception:
        return doc.name
    if not field or not _is_visible_field(field):
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


INDEXING_CONFIG_CACHE_KEY = "document_management:indexing_config_v2"
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
    for row in settings.get("indexed_doctypes", []) or []:
        doctype = getattr(row, "document_type", None)
        if not doctype:
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


def _get_indexing_config():
    use_cache = not getattr(getattr(frappe, "flags", None), "in_test", False)
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
    semantic = bool(getattr(settings, "enable_semantic_search", False))
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
    if doc.doctype in SKIPPED_INDEX_DOCTYPES:
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
    if doc_type == "Document" and doc.get("is_deleted"):
        semantic_backend.remove_document(doc_type, doc_name)
        return

    rule = rules.get(doc_type, {})
    content = get_indexable_content(
        doc,
        include_comments=rule.get("index_comments", True),
        include_attachments=rule.get("index_attachments", False),
        excluded_fields=rule.get("excluded_fields"),
    )
    title = get_document_title(doc)

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

    if settings.enable_semantic_search and doc_type == "Document":
        try:
            semantic_backend.index_document(doc_type, doc_name, content)
        except Exception as exc:
            frappe.log_error(title="Semantic Indexing Error", message=str(exc))


def handle_doc_trash(doc, method=None):
    if doc.doctype in SKIPPED_INDEX_DOCTYPES:
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
    if doc.doctype in SKIPPED_INDEX_DOCTYPES:
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


def _record_result(hit, metadata, excerpt=None):
    return {
        "source": "full_text",
        "doc_type": hit["doc_type"],
        "doc_name": hit["doc_name"],
        "title": hit.get("title") or hit["doc_name"],
        "excerpt": hit.get("excerpt") or "" if excerpt is None else excerpt,
        "score": float(hit.get("score") or 0),
        "quality": int(hit.get("quality") or 0),
        "match_type": hit.get("match_type") or "partial",
        "matched_terms": int(hit.get("matched_terms") or 0),
        "total_terms": int(hit.get("total_terms") or 0),
        "coverage": float(hit.get("coverage") or 0),
        "modified": str(metadata.get("modified") or ""),
        "route": (
            f"/desk/{hit['doc_type'].lower().replace(' ', '-')}/"
            f"{quote(str(hit['doc_name']), safe='')}"
        ),
    }


def _check_custom_read_permission(doctype, name, custom_permission_hooks, user, cache):
    if user == "Administrator" or doctype not in custom_permission_hooks:
        return True
    key = (doctype, str(name))
    if key in cache:
        return cache[key]
    try:
        doc = frappe.get_doc(doctype, name)
        allowed = bool(
            frappe.has_permission(
                doctype,
                "read",
                doc=doc,
                user=user,
            )
        )
    except Exception:
        allowed = False
    cache[key] = allowed
    return allowed


def _permitted_metadata(
    hits,
    date_from=None,
    date_to=None,
    defer_custom_hooks=False,
):
    by_doctype = {}
    for hit in hits:
        by_doctype.setdefault(hit["doc_type"], []).append(hit["doc_name"])

    permitted = {}
    user = getattr(getattr(frappe, "session", None), "user", None)
    custom_permission_hooks = (
        {}
        if (defer_custom_hooks or user == "Administrator")
        else (frappe.get_hooks("has_permission") or {})
    )
    for doctype, names in by_doctype.items():
        unique_names = list(dict.fromkeys(names))
        list_filters = {"name": ["in", unique_names]}
        if date_from and date_to:
            list_filters["modified"] = [
                "between",
                [f"{date_from} 00:00:00", f"{date_to} 23:59:59"],
            ]
        elif date_from:
            list_filters["modified"] = [">=", f"{date_from} 00:00:00"]
        elif date_to:
            list_filters["modified"] = ["<=", f"{date_to} 23:59:59"]
        try:
            rows = frappe.get_list(
                doctype,
                filters=list_filters,
                fields=["name", "modified"],
                limit_page_length=len(unique_names),
            )
        except Exception:
            continue
        for row in rows:
            name = row.get("name") if hasattr(row, "get") else row.name
            if doctype in custom_permission_hooks:
                try:
                    doc = frappe.get_doc(doctype, name)
                    if not frappe.has_permission(
                        doctype,
                        "read",
                        doc=doc,
                        user=user,
                    ):
                        continue
                except Exception:
                    continue
            permitted[(doctype, str(name))] = row
    return permitted


def _materialize_excerpts(records, raw_contents, query, folded_terms):
    for row, raw_content in zip(records, raw_contents):
        if raw_content is not None and not row.get("excerpt"):
            row["excerpt"] = make_excerpt(
                raw_content,
                query,
                maximum=280,
                folded_terms=folded_terms,
            )


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
    query = (query or "").strip()
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

    enable_full_text, _, indexed_doctypes, _ = _get_indexing_config()
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
    folded_terms = tuple(dict.fromkeys(fold_text(term) for term in terms if term))

    results = {
        "exact": [],
        "facets": {},
        "facets_truncated": False,
        "fuzzy_fallback": False,
        "configured_doctypes": sorted(configured),
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
            facets = {}
            facets_truncated = False
            user = getattr(getattr(frappe, "session", None), "user", None)
            custom_permission_hooks = (
                {}
                if user == "Administrator"
                else (frappe.get_hooks("has_permission") or {})
            )
            custom_perm_cache = {}
            if sort_by == "relevance":
                authorized_seen = 0
                page_results = []
                page_raw_contents = []
                for batch in tantivy_backend.iter_search(
                    query,
                    doctypes=sorted(generic_filter),
                    batch_size=max(page_length * 2, 50),
                    include_excerpt=False,
                ):
                    filtered = [
                        hit
                        for hit in batch
                        if hit["doc_type"] != "Document"
                        and hit["doc_type"] in generic_filter
                    ]
                    permitted = _permitted_metadata(
                        filtered,
                        date_from=date_from,
                        date_to=date_to,
                        defer_custom_hooks=True,
                    )
                    for hit in filtered:
                        key = (hit["doc_type"], hit["doc_name"])
                        if key not in permitted:
                            continue
                        meta = permitted[key]
                        if not _matches_date_range(meta.get("modified"), date_from, date_to):
                            continue
                        if not _check_custom_read_permission(
                            hit["doc_type"],
                            hit["doc_name"],
                            custom_permission_hooks,
                            user,
                            custom_perm_cache,
                        ):
                            continue
                        facets[hit["doc_type"]] = facets.get(hit["doc_type"], 0) + 1
                        if authorized_seen < target_start:
                            authorized_seen += 1
                            continue
                        if len(page_results) <= page_length:
                            page_results.append(_record_result(hit, meta))
                            page_raw_contents.append(hit.get("_raw_content"))
                        authorized_seen += 1
                        if len(page_results) > page_length and custom_permission_hooks:
                            facets_truncated = True
                            break
                    if len(page_results) > page_length:
                        facets_truncated = True
                        break
                results["pagination"]["has_more"] = len(page_results) > page_length
                final_rows = page_results[:page_length]
                _materialize_excerpts(
                    final_rows,
                    page_raw_contents[:page_length],
                    query,
                    folded_terms,
                )
                results["exact"] = final_rows
            else:
                collected = []
                max_collect = max(target_start + page_length + 1, 500)
                for batch in tantivy_backend.iter_search(
                    query,
                    doctypes=sorted(generic_filter),
                    batch_size=max(page_length * 4, 100),
                    include_excerpt=False,
                ):
                    filtered = [
                        hit
                        for hit in batch
                        if hit["doc_type"] != "Document"
                        and hit["doc_type"] in generic_filter
                    ]
                    permitted = _permitted_metadata(
                        filtered,
                        date_from=date_from,
                        date_to=date_to,
                        defer_custom_hooks=True,
                    )
                    for hit in filtered:
                        key = (hit["doc_type"], hit["doc_name"])
                        if key not in permitted:
                            continue
                        meta = permitted[key]
                        if not _matches_date_range(meta.get("modified"), date_from, date_to):
                            continue
                        if not _check_custom_read_permission(
                            hit["doc_type"],
                            hit["doc_name"],
                            custom_permission_hooks,
                            user,
                            custom_perm_cache,
                        ):
                            continue
                        facets[hit["doc_type"]] = facets.get(hit["doc_type"], 0) + 1
                        collected.append((_record_result(hit, meta), hit.get("_raw_content")))
                        if len(collected) >= max_collect:
                            facets_truncated = True
                            break
                    if len(collected) >= max_collect:
                        break
                ordered_records = _sort_records([item[0] for item in collected], sort_by)
                raw_by_key = {
                    (item[0]["doc_type"], item[0]["doc_name"]): item[1]
                    for item in collected
                }
                page_slice = ordered_records[target_start : target_start + page_length + 1]
                results["pagination"]["has_more"] = len(page_slice) > page_length
                final_rows = page_slice[:page_length]
                _materialize_excerpts(
                    final_rows,
                    [raw_by_key.get((row["doc_type"], row["doc_name"])) for row in final_rows],
                    query,
                    folded_terms,
                )
                results["exact"] = final_rows

            results["facets"] = facets
            results["facets_truncated"] = facets_truncated
            results["fuzzy_fallback"] = bool(
                results["exact"]
                and all(row.get("match_type") == "fuzzy" for row in results["exact"])
            )
            results["pagination"]["from"] = (
                target_start + 1 if results["exact"] else 0
            )
            results["pagination"]["to"] = (
                target_start + len(results["exact"])
            )
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


@frappe.whitelist()
def get_search_options():
    settings = frappe.get_single("Document Management Settings")
    options_dict = {}
    for row in settings.get("indexed_doctypes", []):
        doctype = row.document_type
        if not doctype or doctype == "Document":
            continue
        try:
            if frappe.has_permission(doctype, "read"):
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
    configured = sorted(
        {
            row.document_type
            for row in (settings.get("indexed_doctypes", []) or [])
            if row.document_type and row.document_type != "Document"
        }
    )
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

    if not indexed_doctypes and not settings.enable_semantic_search:
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
                        yield {
                            "doc_type": dt,
                            "doc_name": row.name,
                            "title": get_document_title(doc),
                            "content": get_indexable_content(
                                doc,
                                comments=comments.get(row.name, []),
                                include_comments=include_comments,
                                include_attachments=include_attachments,
                                excluded_fields=excluded_fields,
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
    if settings.enable_semantic_search:
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
        from document_management.frappe_document_management.page.document_management_console.document_management_console import (
            reprocess_ocr,
        )

        return reprocess_ocr(doc_name)

    extracted_text = _extract_pdf_attachments_text(doc_type, doc_name)
    settings = frappe.get_single("Document Management Settings")
    rules = _doctype_rules_from_settings(settings)
    rule = rules.get(doc_type, {})
    base_content = get_indexable_content(
        doc,
        include_comments=rule.get("index_comments", True),
        include_attachments=False,
        excluded_fields=rule.get("excluded_fields"),
    )
    full_content = f"{base_content}\n\n--- OCR TEXT ---\n{extracted_text}"

    title = get_document_title(doc)
    if (
        settings.enable_full_text_search
        and doc_type != "Document"
        and doc_type in rules
    ):
        try:
            tantivy_backend.index_document(
                doc_type,
                doc_name,
                title,
                full_content,
            )
        except tantivy_backend.IndexBusy:
            frappe.throw(
                frappe._(
                    "The search index is rebuilding. "
                    "Try OCR indexing again shortly."
                )
            )
        except tantivy_backend.IndexRebuildRequired:
            frappe.throw(
                frappe._(
                    "The search index must be rebuilt before OCR content "
                    "can be indexed."
                )
            )

    return {"status": "success", "extracted_chars": len(extracted_text)}
