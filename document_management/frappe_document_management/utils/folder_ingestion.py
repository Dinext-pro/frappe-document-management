import os
import stat
from contextlib import contextmanager
from io import BytesIO
import time

import frappe
from frappe import _

from document_management.frappe_document_management.utils import folder_files as files

from document_management.frappe_document_management.page.document_management_console.document_management_console import (
    SUPPORTED_EXTENSIONS,
    create_document_from_bytes,
)


SKIP_SUFFIXES = {".part", ".tmp", ".crdownload"}


def _settings():
    try:
        settings = frappe.get_single("Document Management Settings")
    except Exception:
        return None
    if not int(settings.get("enable_folder_ingestion") or 0):
        return None
    source = (settings.get("ingestion_folder_path") or "").strip()
    if not source:
        return None
    source_path = files.absolute(source)
    done_path = files.absolute(
        (settings.get("ingestion_done_folder_path") or "").strip()
        or source_path / "done"
    )
    error_path = files.absolute(
        (settings.get("ingestion_error_folder_path") or "").strip()
        or source_path / "error"
    )
    default_owner = (settings.get("ingestion_default_owner") or "").strip() or None
    default_category = (settings.get("ingestion_default_category") or "").strip() or None
    try:
        min_age = max(int(settings.get("ingestion_min_file_age_seconds") or 0), 0)
    except (TypeError, ValueError):
        min_age = 0
    map_subfolders = bool(int(settings.get("ingestion_map_subfolders_to_category") or 0))
    return {
        "source_path": source_path,
        "done_path": done_path,
        "error_path": error_path,
        "default_owner": default_owner,
        "default_category": default_category,
        "min_file_age_seconds": min_age,
        "map_subfolders_to_category": map_subfolders,
    }


def _is_eligible_file(path, info, now, min_age):
    return (
        not path.name.startswith(".")
        and path.suffix.lower() not in SKIP_SUFFIXES
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and stat.S_ISREG(info.st_mode) and info.st_size > 0
        and (not min_age or now - info.st_mtime >= min_age)
    )


def _candidate_files(source_path, done_path, error_path, limit, min_file_age_seconds=0, map_subfolders_to_category=False):
    source_path = files.absolute(source_path)
    skip_dirs = {files.absolute(done_path), files.absolute(error_path)}
    now = time.time()
    age = max(float(min_file_age_seconds or 0), 0.0)
    candidates = []
    try:
        with files.directory(source_path) as root:
            for name, info in files.entries(root):
                path = source_path / name
                if path in skip_dirs:
                    continue
                if _is_eligible_file(path, info, now, age):
                    candidates.append((path, None))
                elif map_subfolders_to_category and stat.S_ISDIR(info.st_mode):
                    try:
                        with files.directory(path) as child:
                            if not os.path.samestat(os.fstat(child), info):
                                continue
                            for filename, child_info in files.entries(child):
                                candidate = path / filename
                                if _is_eligible_file(candidate, child_info, now, age):
                                    candidates.append((candidate, name))
                                    if len(candidates) >= limit:
                                        return candidates
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                if len(candidates) >= limit:
                    break
    except (FileNotFoundError, NotADirectoryError):
        frappe.logger("document_ingestion").warning("Document ingestion source folder is unavailable: %s", source_path)
    return candidates


def _resolve_category(subfolder_category, default_category):
    if subfolder_category and frappe.db.exists("Document Category", subfolder_category):
        return subfolder_category
    if default_category and frappe.db.exists("Document Category", default_category):
        return default_category
    return None


@contextmanager
def input_stream(path, source=None):
    """Direct callers are restricted to the same configured root as the scheduler."""
    if source is not None:
        yield source
        return
    configured = _settings()
    if not configured:
        raise ValueError("Folder ingestion is not configured.")
    path = files.absolute(path)
    relative = path.relative_to(files.absolute(configured["source_path"]))
    depth = 2 if configured["map_subfolders_to_category"] else 1
    excluded = (files.absolute(configured["done_path"]), files.absolute(configured["error_path"]))
    if (not 1 <= len(relative.parts) <= depth or any(part.startswith(".") for part in relative.parts)
            or any(path.is_relative_to(folder) for folder in excluded)):
        raise ValueError("The input is outside the configured folder.")
    with files.directory(path.parent) as parent, files.regular_file(parent, path.name) as stream:
        yield stream


def ingest_file(path, category=None, source=None):
    path = files.absolute(path)
    with input_stream(path, source) as stream:
        return create_document_from_bytes(filename=path.name, content=stream.read(), title=path.stem, category=category)


def _process_file(item, category, configured, owner, handlers):
    path = item.path
    done = configured["done_path"] / category if category else configured["done_path"]
    error = configured["error_path"] / category if category else configured["error_path"]
    try:
        if not owner:
            frappe.throw(_("Select an enabled folder ingestion owner."))
        resolved = _resolve_category(category, configured["default_category"])
        if handlers:
            if len(handlers) != 1:
                frappe.throw(_("Configure a single folder ingestion provider."))
            result = frappe.get_attr(handlers[0])(path, resolved, item.stream)
        else:
            result = ingest_file(path, resolved, item.stream)
        target, identity = item.publish(done)
        try:
            frappe.db.commit()
        except Exception:
            files.retract(target, identity)
            raise
    except Exception as exc:
        frappe.db.rollback()
        trace = frappe.get_traceback()
        target, note = path, None
        try:
            target, _identity = item.publish(error)
            note, _identity = files.publish(BytesIO((trace or str(exc))[-4000:].encode()), error, f"{target.name}.error.txt")
            item.discard()
        except OSError:
            frappe.log_error(title="Document Folder Ingestion Move Error", message=frappe.get_traceback())
        frappe.log_error(title="Document Folder Ingestion Error", message=trace)
        return False, {"file": path.name, "destination": str(target), "error_note": str(note) if note else None,
                       "error": str(exc)}
    item.discard()
    return True, {"file": path.name, "document": result["docname"], "category": resolved, "destination": str(target)}


def ingest_configured_folder(limit=50, min_file_age_seconds=None):
    configured = _settings()
    if not configured:
        return {"processed": 0, "imported": [], "failed": []}

    source_path = configured["source_path"]
    done_path = configured["done_path"]
    error_path = configured["error_path"]
    effective_min_age = (
        configured["min_file_age_seconds"]
        if min_file_age_seconds is None
        else min_file_age_seconds
    )
    try:
        limit = min(max(int(limit or 50), 1), 500)
    except (TypeError, ValueError):
        limit = 50

    handlers = frappe.get_hooks("document_management_folder_ingestion")
    target_user = configured["default_owner"]
    active_owner = target_user and (not handlers or target_user != "Guest") and frappe.db.exists(
        "User", {"name": target_user, "enabled": 1},
    )
    if not active_owner:
        target_user = None if handlers else "Administrator"

    imported = []
    failed = []
    original_user = frappe.session.user
    if target_user:
        frappe.set_user(target_user)
    try:
        for path, subfolder_category in _candidate_files(
            source_path,
            done_path,
            error_path,
            limit,
            min_file_age_seconds=effective_min_age,
            map_subfolders_to_category=configured["map_subfolders_to_category"],
        ):
            try:
                with files.claimed_file(source_path, path) as item:
                    ok, record = _process_file(item, subfolder_category, configured, target_user, handlers)
            except FileNotFoundError:
                # Another worker accepted this candidate first.
                continue
            except (OSError, ValueError) as exc:
                ok = False
                record = {"file": path.name, "destination": str(path), "error_note": None, "error": str(exc)}
                frappe.log_error(title="Document Folder Ingestion Error", message=frappe.get_traceback())
            (imported if ok else failed).append(record)
    finally:
        frappe.set_user(original_user)

    return {
        "processed": len(imported) + len(failed),
        "imported": imported,
        "failed": failed,
    }
