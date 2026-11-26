import os
import shutil
from pathlib import Path

import frappe
from frappe.model.document import Document

from document_management.frappe_document_management.utils.storage_paths import (
    validate_storage_template,
)


class DocumentManagementSettings(Document):
    def on_update(self):
        from document_management.search.indexer import clear_indexing_config_cache

        clear_indexing_config_cache()

    def validate(self):
        self._validate_indexed_doctypes()
        if int(self.trash_retention_days or 0) < 0:
            frappe.throw("Trash Retention (Days) cannot be negative.")
        if int(self.export_retention_hours or 0) < 0:
            frappe.throw("Export Retention (Hours) cannot be negative.")
        if int(self.get("expiry_reminder_days") or 0) < 0:
            frappe.throw("Expiry & Review Reminder Window (Days) cannot be negative.")
        if int(self.get("ingestion_min_file_age_seconds") or 0) < 0:
            frappe.throw("Minimum File Age (Seconds) cannot be negative.")
        chunk_size = int(self.get("rag_chunk_size") or 650)
        chunk_overlap = int(self.get("rag_chunk_overlap") or 0)
        if chunk_size < 100:
            frappe.throw("RAG Chunk Size must be at least 100.")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            frappe.throw(
                "RAG Chunk Overlap must be at least 0 and smaller than RAG Chunk Size."
            )
        if (
            self.get("chat_rate_limit_per_minute") not in (None, "")
            and int(self.get("chat_rate_limit_per_minute") or 0) < 1
        ):
            frappe.throw("Chat Rate Limit per Minute must be at least 1.")
        validate_storage_template(self.get("file_storage_path_template"))
        self._validate_folder_ingestion()
        if (
            self.ocr_processing_timeout_minutes
            and int(self.ocr_processing_timeout_minutes) < 5
        ):
            frappe.throw("OCR Processing Timeout must be at least 5 minutes.")
        if self.chat_provider == "OpenAI Compatible" and not (
            self.chat_endpoint or ""
        ).strip():
            frappe.throw(
                "Chat Endpoint is required for OpenAI Compatible."
            )
        if self.embedding_provider == "OpenAI Compatible" and not (
            self.embedding_endpoint or ""
        ).strip():
            frappe.throw(
                "Embedding Endpoint is required for OpenAI Compatible."
            )

    def _validate_folder_ingestion(self):
        if not self.get("enable_folder_ingestion"):
            return
        source = (self.get("ingestion_folder_path") or "").strip()
        if not source:
            frappe.throw("Ingestion Folder Path is required when ingestion is enabled.")
        source_path = Path(source).expanduser()
        if not source_path.is_dir():
            frappe.throw("Ingestion Folder Path must be an existing directory.")

        source_resolved = source_path.resolve()
        for fieldname, label in (
            ("ingestion_done_folder_path", "Done Folder Path"),
            ("ingestion_error_folder_path", "Error Folder Path"),
        ):
            value = (self.get(fieldname) or "").strip()
            if not value:
                continue
            resolved = Path(value).expanduser().resolve()
            if resolved == source_resolved:
                frappe.throw(f"{label} cannot be the ingestion folder itself.")

    def _validate_indexed_doctypes(self):
        seen = set()
        for row in self.get("indexed_doctypes") or []:
            doctype = (row.document_type or "").strip()
            if not doctype:
                continue
            if doctype in seen:
                frappe.throw(f"Indexed DocType {frappe.bold(doctype)} is duplicated.")
            seen.add(doctype)

            meta = frappe.get_meta(doctype)
            if meta.istable:
                frappe.throw(
                    f"Child table {frappe.bold(doctype)} cannot be indexed directly."
                )
            if meta.issingle:
                frappe.throw(
                    f"Single DocType {frappe.bold(doctype)} cannot be indexed."
                )


def _check_folder_ingestion_health(settings):
    if not int(settings.get("enable_folder_ingestion") or 0):
        return {"enabled": False, "ok": True, "detail": "Disabled"}
    source = (settings.get("ingestion_folder_path") or "").strip()
    if not source:
        return {"enabled": True, "ok": False, "error": "Ingestion folder path is not set."}
    source_path = Path(source).expanduser()
    if not source_path.is_dir():
        return {
            "enabled": True,
            "ok": False,
            "path": str(source_path),
            "error": "Ingestion folder does not exist.",
        }
    readable = os.access(source_path, os.R_OK)
    writable = os.access(source_path, os.W_OK)
    return {
        "enabled": True,
        "ok": readable and writable,
        "path": str(source_path.resolve()),
        "readable": readable,
        "writable": writable,
        "error": "" if (readable and writable) else "Insufficient read/write permissions.",
    }


@frappe.whitelist()
def check_system_health():
    if "System Manager" not in frappe.get_roles():
        frappe.throw("System Manager role is required.", frappe.PermissionError)

    from document_management.frappe_document_management.rag.config import get_rag_config
    from document_management.frappe_document_management.rag.index import index_status
    from document_management.frappe_document_management.rag.providers import (
        _is_loopback_endpoint,
        _validated_endpoint,
    )
    from document_management.search import tantivy_backend

    settings = frappe.get_single("Document Management Settings")
    binaries = {
        name: bool(shutil.which(name))
        for name in ("ocrmypdf", "tesseract", "libreoffice", "pdftoppm")
    }

    endpoints = {}
    try:
        config = get_rag_config()
        for label, provider, endpoint in (
            ("chat", config.chat_provider, config.chat_endpoint),
            ("embedding", config.embedding_provider, config.embedding_endpoint),
        ):
            if not endpoint:
                endpoints[label] = {"provider": provider, "ok": True, "detail": "Local"}
                continue
            try:
                allow_internal = config.allow_internal_endpoints or (
                    provider in {"Ollama", "LM Studio"}
                    and _is_loopback_endpoint(endpoint)
                )
                _validated_endpoint(endpoint, allow_internal=allow_internal)
                endpoints[label] = {"provider": provider, "endpoint": endpoint, "ok": True}
            except Exception as exc:
                endpoints[label] = {
                    "provider": provider,
                    "endpoint": endpoint,
                    "ok": False,
                    "error": str(exc),
                }
    except Exception as exc:
        endpoints["error"] = str(exc)

    rag_status = {}
    try:
        rag_status = index_status()
    except Exception as exc:
        rag_status = {"error": str(exc)}

    return {
        "binaries": binaries,
        "tantivy_ready": tantivy_backend.index_exists(),
        "rag_index": rag_status,
        "endpoints": endpoints,
        "folder_ingestion": _check_folder_ingestion_health(settings),
        "encryption_enabled": bool(int(settings.get("enable_file_encryption") or 0)),
    }
