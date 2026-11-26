import gc
import json
import os
import shutil
from pathlib import Path

import frappe
from filelock import FileLock, Timeout

from document_management.search.query import (
    build_natural_query,
    fold_text,
    make_excerpt,
    positive_query_text,
    search_match_details,
    significant_terms,
)


INDEX_VERSION = 3
SEARCH_FIELDS = {
    "doc_name": 6.0,
    "doc_name_normalized": 6.0,
    "title": 4.0,
    "title_normalized": 4.0,
    "content": 1.0,
    "content_normalized": 1.0,
}

_DEFAULT_BUILD_NATURAL_QUERY = build_natural_query
_INDEX_CACHE = {"path": None, "index": None}


class IndexRebuildRequired(RuntimeError):
    pass


class IndexBusy(RuntimeError):
    pass


def clear_index_cache():
    _INDEX_CACHE["path"] = None
    _INDEX_CACHE["index"] = None


def _root_path():
    settings = frappe.get_single("Document Management Settings")
    base_path = frappe.utils.get_site_path(
        "private",
        settings.index_path or "search_index",
    )
    return Path(base_path) / f"tantivy_v{INDEX_VERSION}"


def _generations_path():
    return _root_path() / "generations"


def _pointer_path():
    return _root_path() / "current.json"


def _lock_path():
    return _root_path() / "index.lock"


def _read_pointer():
    pointer = _pointer_path()
    if not pointer.exists():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        raise IndexRebuildRequired("The operational search index pointer is invalid.")
    if not isinstance(data, dict):
        raise IndexRebuildRequired("The operational search index pointer is invalid.")
    return data


def _active_generation_name():
    data = _read_pointer()
    if not data:
        return None
    generation = data.get("generation")
    if (
        not isinstance(generation, str)
        or not generation.startswith("gen-")
        or Path(generation).name != generation
    ):
        raise IndexRebuildRequired("The operational search index pointer is invalid.")
    return generation


def get_tantivy_index_path():
    generation = _active_generation_name()
    if not generation:
        raise IndexRebuildRequired(
            "The operational search index must be rebuilt before first use."
        )
    path = _generations_path() / generation / "tantivy"
    if not path.is_dir():
        raise IndexRebuildRequired(
            "The active operational search index generation is missing."
        )
    return str(path)


def index_exists():
    try:
        path = Path(get_tantivy_index_path())
        return any(path.iterdir())
    except (IndexRebuildRequired, OSError):
        return False


def get_schema():
    import tantivy

    schema_builder = tantivy.SchemaBuilder()
    schema_builder.add_text_field(
        "record_key",
        stored=True,
        tokenizer_name="raw",
    )
    schema_builder.add_text_field(
        "doc_type",
        stored=True,
        tokenizer_name="raw",
    )
    schema_builder.add_text_field(
        "doc_name",
        stored=True,
        tokenizer_name="default",
    )
    schema_builder.add_text_field(
        "doc_name_normalized",
        tokenizer_name="default",
    )
    schema_builder.add_text_field(
        "title",
        stored=True,
        tokenizer_name="default",
    )
    schema_builder.add_text_field(
        "title_normalized",
        tokenizer_name="default",
    )
    schema_builder.add_text_field(
        "content",
        stored=True,
        tokenizer_name="default",
    )
    schema_builder.add_text_field(
        "content_normalized",
        tokenizer_name="default",
    )
    return schema_builder.build()


def get_index():
    import tantivy

    try:
        path = get_tantivy_index_path()
        if _INDEX_CACHE["path"] == path and _INDEX_CACHE["index"] is not None:
            return _INDEX_CACHE["index"]
        index = tantivy.Index.open(path)
        _INDEX_CACHE["path"] = path
        _INDEX_CACHE["index"] = index
        return index
    except IndexRebuildRequired:
        clear_index_cache()
        raise
    except Exception as exc:
        clear_index_cache()
        raise IndexRebuildRequired(
            "The operational search index is incompatible or damaged. Rebuild it."
        ) from exc


def get_index_stats(doctypes=None):
    try:
        pointer = _read_pointer() or {}
        generation = _active_generation_name()
        if not generation or not index_exists():
            return {
                "ready": False,
                "generation": None,
                "index_version": INDEX_VERSION,
                "rebuilt_at": pointer.get("rebuilt_at"),
                "num_docs": 0,
                "by_doctype": {},
            }
        index = get_index()
        index.reload()
        searcher = index.searcher()
        by_doctype = {}
        for doctype in sorted(set(doctypes or [])):
            try:
                q = index.parse_query(json.dumps(str(doctype)), ["doc_type"])
                res = searcher.search(q, 1, count=True)
                by_doctype[doctype] = int(getattr(res, "count", 0) or 0)
            except Exception:
                by_doctype[doctype] = 0
        return {
            "ready": True,
            "generation": generation,
            "index_version": INDEX_VERSION,
            "rebuilt_at": pointer.get("rebuilt_at"),
            "num_docs": int(getattr(searcher, "num_docs", 0) or 0),
            "by_doctype": by_doctype,
        }
    except Exception as exc:
        return {
            "ready": False,
            "generation": None,
            "index_version": INDEX_VERSION,
            "rebuilt_at": None,
            "num_docs": 0,
            "by_doctype": {},
            "error": str(exc),
        }


def _new_document(record):
    import tantivy

    doc_type = str(record.get("doc_type") or "")
    doc_name = str(record.get("doc_name") or "")
    title = str(record.get("title") or "")
    content = str(record.get("content") or "")

    document = tantivy.Document()
    document.add_text("record_key", f"{doc_type}:{doc_name}")
    document.add_text("doc_type", doc_type)
    document.add_text("doc_name", doc_name)
    document.add_text("doc_name_normalized", fold_text(doc_name))
    document.add_text("title", title)
    document.add_text("title_normalized", fold_text(title))
    document.add_text("content", content)
    document.add_text("content_normalized", fold_text(content))
    return document


def index_document(doc_type, doc_name, title, content):
    record_key = f"{doc_type}:{doc_name}"
    _root_path().mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(_lock_path()), timeout=5):
            index = get_index()
            writer = index.writer()
            writer.delete_documents("record_key", record_key)
            writer.add_document(
                _new_document(
                    {
                        "doc_type": doc_type,
                        "doc_name": doc_name,
                        "title": title,
                        "content": content,
                    }
                )
            )
            writer.commit()
    except Timeout as exc:
        raise IndexBusy("The operational search index is rebuilding.") from exc


def remove_document(doc_type, doc_name):
    _root_path().mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(_lock_path()), timeout=5):
            index = get_index()
            writer = index.writer()
            writer.delete_documents("record_key", f"{doc_type}:{doc_name}")
            writer.commit()
    except Timeout as exc:
        raise IndexBusy("The operational search index is rebuilding.") from exc


def _search_result(searcher, query, limit, offset=0):
    return searcher.search(
        query,
        limit,
        count=True,
        offset=offset,
    )


def _apply_doctype_filter(index, query, doctypes):
    if not doctypes:
        return query
    import tantivy

    type_clauses = [
        (
            tantivy.Occur.Should,
            index.parse_query(json.dumps(str(doctype)), ["doc_type"]),
        )
        for doctype in doctypes
    ]
    type_query = (
        type_clauses[0][1]
        if len(type_clauses) == 1
        else tantivy.Query.boolean_query(type_clauses)
    )
    return tantivy.Query.boolean_query(
        [
            (tantivy.Occur.Must, query),
            (tantivy.Occur.Must, type_query),
        ]
    )


def _get_first_val(doc, field):
    try:
        val = doc[field]
        if isinstance(val, list) and val:
            return val[0]
        if val and not isinstance(val, list):
            return val
    except Exception:
        pass
    return ""


def _build_hit(searcher, query_str, score, doc_address):
    doc = searcher.doc(doc_address)
    doc_name = _get_first_val(doc, "doc_name")
    title = _get_first_val(doc, "title")
    content = _get_first_val(doc, "content")
    details = search_match_details(
        query_str,
        doc_name=doc_name,
        title=title,
        content=content,
    )
    return {
        "score": float(score or 0),
        "doc_type": _get_first_val(doc, "doc_type"),
        "doc_name": doc_name,
        "title": title,
        "excerpt": _excerpt(content, query_str),
        **details,
    }


def _rank_batch(hits):
    return sorted(
        hits,
        key=lambda hit: (
            int(hit.get("quality") or 0),
            float(hit.get("coverage") or 0),
            float(hit.get("score") or 0),
        ),
        reverse=True,
    )


def iter_search(query_str, doctypes=None, batch_size=100):
    index = get_index()
    index.reload()
    searcher = index.searcher()
    batch_size = max(int(batch_size), 1)
    seen_keys = set()
    yielded_any = False

    strict_query = build_natural_query(
        index,
        query_str,
        SEARCH_FIELDS,
        require_all=True,
    )
    strict_query = _apply_doctype_filter(index, strict_query, doctypes)
    strict_offset = 0
    while True:
        result = _search_result(
            searcher,
            strict_query,
            batch_size,
            offset=strict_offset,
        )
        if not result.hits:
            break
        batch = []
        for score, doc_address in result.hits:
            hit = _build_hit(searcher, query_str, score, doc_address)
            key = (hit["doc_type"], hit["doc_name"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            batch.append(hit)
        if batch:
            yielded_any = True
            yield _rank_batch(batch)
        strict_offset += len(result.hits)
        if strict_offset >= int(result.count or 0):
            break

    terms = significant_terms(positive_query_text(query_str))
    if len(terms) > 1:
        relaxed_query = build_natural_query(
            index,
            query_str,
            SEARCH_FIELDS,
            require_all=False,
        )
        relaxed_query = _apply_doctype_filter(index, relaxed_query, doctypes)
        relaxed_offset = 0
        while True:
            result = _search_result(
                searcher,
                relaxed_query,
                batch_size,
                offset=relaxed_offset,
            )
            if not result.hits:
                break
            candidates = []
            for score, doc_address in result.hits:
                hit = _build_hit(searcher, query_str, score, doc_address)
                key = (hit["doc_type"], hit["doc_name"])
                if key in seen_keys:
                    continue
                minimum_matches = max(1, (hit["total_terms"] + 1) // 2)
                if hit["matched_terms"] == hit["total_terms"]:
                    continue
                if hit["matched_terms"] < minimum_matches:
                    continue
                seen_keys.add(key)
                candidates.append(hit)
            if candidates:
                yielded_any = True
                yield _rank_batch(candidates)
            relaxed_offset += len(result.hits)
            if relaxed_offset >= int(result.count or 0):
                break

    # Typo-tolerant fuzzy fallback only when strict + relaxed produced zero results
    if (
        not yielded_any
        and build_natural_query is _DEFAULT_BUILD_NATURAL_QUERY
        and any(len(t) >= 4 for t in terms)
    ):
        try:
            fuzzy_query = build_natural_query(
                index,
                query_str,
                SEARCH_FIELDS,
                require_all=False,
                allow_prefix=False,
                fuzzy=True,
            )
            fuzzy_query = _apply_doctype_filter(index, fuzzy_query, doctypes)
            result = _search_result(searcher, fuzzy_query, batch_size, offset=0)
            fuzzy_hits = []
            for score, doc_address in result.hits:
                hit = _build_hit(searcher, query_str, score, doc_address)
                key = (hit["doc_type"], hit["doc_name"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                hit["match_type"] = "fuzzy"
                fuzzy_hits.append(hit)
            if fuzzy_hits:
                yield _rank_batch(fuzzy_hits)
        except Exception:
            pass


def search(query_str, limit=10, doctypes=None, offset=0):
    limit = max(int(limit), 0)
    offset = max(int(offset), 0)
    if not limit:
        return []

    results = []
    skipped = 0
    for batch in iter_search(query_str, doctypes=doctypes):
        for hit in batch:
            if skipped < offset:
                skipped += 1
                continue
            results.append(hit)
            if len(results) >= limit:
                return results
    return results


def _excerpt(content, query, maximum=280):
    return make_excerpt(content, query, maximum=maximum)


def _cleanup_generations(active_generation, keep=2):
    generations = _generations_path()
    if not generations.exists():
        return
    candidates = sorted(
        (
            path
            for path in generations.iterdir()
            if path.is_dir()
            and not path.name.startswith(".tmp-")
            and path.name != active_generation
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in candidates[max(keep - 1, 0) :]:
        shutil.rmtree(stale, ignore_errors=True)


def _cleanup_legacy_indexes():
    root = _root_path()
    for legacy in (root.parent / "tantivy_v2", root.parent / "tantivy"):
        if legacy.is_dir():
            shutil.rmtree(legacy, ignore_errors=True)


def rebuild(records):
    import tantivy

    root = _root_path()
    generations = _generations_path()
    root.mkdir(parents=True, exist_ok=True)
    generations.mkdir(parents=True, exist_ok=True)

    with FileLock(str(_lock_path())):
        temp_root = generations / f".tmp-{frappe.generate_hash(length=12)}"
        index_path = temp_root / "tantivy"
        index_path.mkdir(parents=True, exist_ok=False)
        count = 0
        try:
            index = tantivy.Index(get_schema(), path=str(index_path))
            writer = index.writer()
            for record in records:
                writer.add_document(_new_document(record))
                count += 1
            writer.commit()
            del writer, index
            gc.collect()

            generation = f"gen-{frappe.generate_hash(length=16)}"
            final_root = generations / generation
            os.replace(temp_root, final_root)

            rebuilt_at = None
            try:
                rebuilt_at = str(frappe.utils.now_datetime())
            except Exception:
                pass
            pointer_temp = root / f".current-{frappe.generate_hash(length=8)}.json"
            pointer_temp.write_text(
                json.dumps(
                    {
                        "generation": generation,
                        "index_version": INDEX_VERSION,
                        "rebuilt_at": rebuilt_at,
                    }
                ),
                encoding="utf-8",
            )
            os.replace(pointer_temp, _pointer_path())
            clear_index_cache()
            _cleanup_generations(generation)
            _cleanup_legacy_indexes()
            return {
                "count": count,
                "generation": generation,
                "rebuilt_at": rebuilt_at,
            }
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=True)
