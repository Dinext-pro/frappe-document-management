import shutil
import time
from pathlib import Path

import frappe
from frappe import _

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
    source_path = Path(source).expanduser().resolve()
    done_path = Path(
        (settings.get("ingestion_done_folder_path") or "").strip()
        or source_path / "done"
    ).expanduser().resolve()
    error_path = Path(
        (settings.get("ingestion_error_folder_path") or "").strip()
        or source_path / "error"
    ).expanduser().resolve()
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


def _unique_target(directory, filename):
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    frappe.throw(f"Unable to allocate a destination filename for {filename}.")


def _is_eligible_file(path, now, min_age):
    if path.name.startswith(".") or path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size <= 0:
        return False
    if min_age and (now - stat.st_mtime) < min_age:
        return False
    return True


def _candidate_files(
    source_path,
    done_path,
    error_path,
    limit,
    min_file_age_seconds=0,
    map_subfolders_to_category=False,
):
    if not source_path.is_dir():
        frappe.logger("document_ingestion").warning(
            "Document ingestion source folder does not exist: %s",
            source_path,
        )
        return []
    skip_dirs = {done_path, error_path}
    now = time.time()
    min_age = max(float(min_file_age_seconds or 0), 0.0)
    candidates = []

    for entry in sorted(source_path.iterdir(), key=lambda item: item.name.lower()):
        resolved = entry.resolve()
        if resolved in skip_dirs:
            continue
        if entry.is_dir():
            if not map_subfolders_to_category or entry.name.startswith("."):
                continue
            subfolder_category = entry.name
            for child in sorted(entry.iterdir(), key=lambda item: item.name.lower()):
                if child.is_dir() or not _is_eligible_file(child, now, min_age):
                    continue
                candidates.append((child, subfolder_category))
                if len(candidates) >= limit:
                    return candidates
            continue
        if _is_eligible_file(entry, now, min_age):
            candidates.append((entry, None))
            if len(candidates) >= limit:
                return candidates
    return candidates


def _move_to(directory, path):
    target = _unique_target(directory, path.name)
    shutil.move(str(path), str(target))
    return target


def _write_error_note(error_path, failed_path, error):
    note = _unique_target(error_path, f"{failed_path.name}.error.txt")
    note.write_text(str(error)[-4000:], encoding="utf-8")
    return note


def _resolve_category(subfolder_category, default_category):
    if subfolder_category and frappe.db.exists("Document Category", subfolder_category):
        return subfolder_category
    if default_category and frappe.db.exists("Document Category", default_category):
        return default_category
    return None


def ingest_file(path, category=None):
    path = Path(path).expanduser().resolve()
    content = path.read_bytes()
    return create_document_from_bytes(
        filename=path.name,
        content=content,
        title=path.stem,
        category=category,
    )


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
            moved_target = None
            dest_done = done_path / subfolder_category if subfolder_category else done_path
            dest_error = error_path / subfolder_category if subfolder_category else error_path
            try:
                if not target_user:
                    frappe.throw(_("Select an enabled folder ingestion owner."))
                resolved_category = _resolve_category(subfolder_category, configured["default_category"])
                if handlers:
                    if len(handlers) != 1:
                        frappe.throw(_("Configure a single folder ingestion provider."))
                    result = frappe.get_attr(handlers[0])(path, resolved_category)
                    # A provider retries by source identity, including after an interrupted move.
                    frappe.db.commit()
                else:
                    result = ingest_file(path, category=resolved_category)
                moved_target = _move_to(dest_done, path)
                if not handlers:
                    frappe.db.commit()
                imported.append(
                    {
                        "file": path.name,
                        "document": result["docname"],
                        "category": resolved_category,
                        "destination": str(moved_target),
                    }
                )
            except Exception as exc:
                frappe.db.rollback()
                if moved_target and moved_target.exists() and not path.exists():
                    try:
                        shutil.move(str(moved_target), str(path))
                    except Exception:
                        frappe.log_error(
                            title="Document Folder Ingestion Rollback Move Error",
                            message=frappe.get_traceback(),
                        )
                try:
                    target = _move_to(dest_error, path)
                    note = _write_error_note(
                        dest_error,
                        target,
                        frappe.get_traceback() or exc,
                    )
                except Exception:
                    target = path
                    note = None
                    frappe.log_error(
                        title="Document Folder Ingestion Move Error",
                        message=frappe.get_traceback(),
                    )
                failed.append(
                    {
                        "file": path.name,
                        "destination": str(target),
                        "error_note": str(note) if note else None,
                        "error": str(exc),
                    }
                )
                frappe.log_error(
                    title="Document Folder Ingestion Error",
                    message=frappe.get_traceback(),
                )
    finally:
        frappe.set_user(original_user)

    return {
        "processed": len(imported) + len(failed),
        "imported": imported,
        "failed": failed,
    }
