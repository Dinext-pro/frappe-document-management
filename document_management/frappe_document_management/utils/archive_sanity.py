import os
from pathlib import Path
from urllib.parse import unquote

import frappe
from document_management.frappe_document_management.utils.document_provider import standalone_filters
from frappe.utils.file_manager import get_file_path

from document_management.frappe_document_management.utils.file_crypto import (
    file_encryption_enabled,
    is_encrypted_path,
    sha256_path,
)


def _sha256_file(path, block_size=1024 * 1024):
    return sha256_path(path)


def _issue(issues, severity, code, message, **context):
    issues.append(
        {
            "severity": severity,
            "code": code,
            "message": message,
            **context,
        }
    )


def _check_stored_file(
    issues,
    *,
    file_url,
    expected_checksum,
    document,
    version,
    kind,
    check_encryption=False,
):
    if not file_url:
        return
    try:
        path = get_file_path(file_url)
    except Exception:
        path = ""
    context = {
        "document": document,
        "version": version,
        "file_url": file_url,
    }
    if not path or not os.path.isfile(path):
        _issue(
            issues,
            "error",
            f"{kind}_missing",
            f"{kind.title()} file does not exist.",
            **context,
        )
        return
    if check_encryption and not is_encrypted_path(path):
        _issue(
            issues,
            "warning",
            f"{kind}_unencrypted",
            f"{kind.title()} file is stored unencrypted while file encryption is enabled.",
            **context,
        )
    if not expected_checksum:
        _issue(
            issues,
            "warning",
            f"{kind}_checksum_missing",
            f"{kind.title()} checksum is missing.",
            **context,
        )
        return
    try:
        actual_checksum = _sha256_file(path)
    except Exception as exc:
        _issue(
            issues,
            "error",
            f"{kind}_decryption_failed",
            f"{kind.title()} file could not be read or decrypted: {exc}",
            **context,
        )
        return
    if actual_checksum != expected_checksum:
        _issue(
            issues,
            "error",
            f"{kind}_checksum_mismatch",
            f"{kind.title()} checksum does not match the stored value.",
            expected_checksum=expected_checksum,
            actual_checksum=actual_checksum,
            **context,
        )


def _check_file_references(issues, document_names, versions):
    version_names = set(versions)
    expected_by_version = {}
    expected_by_document = {}
    matched_version_urls = set()
    for version_name, version in versions.items():
        expected_urls = {
            url
            for url in (
                getattr(version, "attachment", None),
                getattr(version, "preview_attachment", None),
            )
            if url
        }
        expected_by_version[version_name] = expected_urls
        expected_by_document.setdefault(version.parent, set()).update(expected_urls)

    files = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": ["in", ["Document", "Document Version"]],
        },
        fields=[
            "name",
            "file_url",
            "attached_to_doctype",
            "attached_to_name",
        ],
        limit_page_length=0,
    )
    for file_row in files:
        if file_row.attached_to_doctype == "Document":
            target_exists = file_row.attached_to_name in document_names
            expected_urls = expected_by_document.get(
                file_row.attached_to_name,
                set(),
            )
        else:
            target_exists = file_row.attached_to_name in version_names
            expected_urls = expected_by_version.get(
                file_row.attached_to_name,
                set(),
            )
        if not target_exists:
            _issue(
                issues,
                "warning",
                "orphaned_file_reference",
                "File references a document record that does not exist.",
                file=file_row.name,
                file_url=file_row.file_url,
                attached_to_doctype=file_row.attached_to_doctype,
                attached_to_name=file_row.attached_to_name,
            )
            continue
        if file_row.file_url not in expected_urls:
            _issue(
                issues,
                "warning",
                "stale_file_reference",
                "File is attached to a document record but is not a current original or preview.",
                file=file_row.name,
                file_url=file_row.file_url,
                attached_to_doctype=file_row.attached_to_doctype,
                attached_to_name=file_row.attached_to_name,
            )
            continue

        if file_row.attached_to_doctype == "Document Version":
            matched_version_urls.add(
                (file_row.attached_to_name, file_row.file_url)
            )
        else:
            for version_name, version in versions.items():
                if (
                    version.parent == file_row.attached_to_name
                    and file_row.file_url in expected_by_version[version_name]
                ):
                    matched_version_urls.add((version_name, file_row.file_url))

    for version_name, expected_urls in expected_by_version.items():
        version = versions[version_name]
        for file_url in expected_urls:
            if (version_name, file_url) in matched_version_urls:
                continue
            _issue(
                issues,
                "warning",
                "file_record_missing",
                "Version attachment has no matching File record.",
                document=version.parent,
                version=version_name,
                file_url=file_url,
            )


def _check_orphaned_derived_records(issues, document_names, version_names):
    if frappe.db.exists("DocType", "Document Page"):
        pages = frappe.get_all(
            "Document Page",
            fields=["name", "document", "document_version"],
            limit_page_length=0,
        )
        for page in pages:
            if page.document not in document_names or (
                page.document_version and page.document_version not in version_names
            ):
                _issue(
                    issues,
                    "warning",
                    "orphaned_document_page",
                    "Document Page references a missing document or version.",
                    page=page.name,
                    document=page.document,
                    version=page.document_version,
                )

    if frappe.db.exists("DocType", "Document Chunk"):
        chunks = frappe.get_all(
            "Document Chunk",
            fields=["name", "document"],
            limit_page_length=0,
        )
        for chunk in chunks:
            if chunk.document not in document_names:
                _issue(
                    issues,
                    "warning",
                    "orphaned_document_chunk",
                    "Document Chunk references a missing document.",
                    chunk=chunk.name,
                    document=chunk.document,
                )


def _physical_file_url(path, root, private):
    relative = path.relative_to(root).as_posix()
    prefix = "/private/files" if private else "/files"
    return f"{prefix}/{relative}"


def _check_physical_orphans(issues):
    known_urls = {
        unquote(row.file_url)
        for row in frappe.get_all(
            "File",
            filters={"is_folder": 0},
            fields=["file_url"],
            limit_page_length=0,
        )
        if row.file_url
    }
    # Scope to private/files where Document Management stores all attachments
    # to avoid false positives from public site/website assets of other apps.
    private_root = Path(frappe.get_site_path("private", "files"))
    if not private_root.is_dir():
        return
    for path in private_root.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        file_url = _physical_file_url(path, private_root, True)
        if unquote(file_url) in known_urls:
            continue
        _issue(
            issues,
            "warning",
            "physical_file_orphaned",
            "Physical file has no matching File record.",
            file_url=file_url,
            path=str(path),
        )


def check_document_archive():
    issues = []
    encryption_expected = file_encryption_enabled()
    documents = frappe.get_all(
        "Document",
        filters=standalone_filters(),
        fields=["name", "ocr_status", "is_deleted"],
        limit_page_length=0,
    )
    versions = frappe.get_all(
        "Document Version",
        fields=[
            "name",
            "parent",
            "version_number",
            "attachment",
            "file_checksum",
            "preview_attachment",
            "preview_checksum",
            "ocr_status",
        ],
        limit_page_length=0,
    )
    empty_completed_ocr = set(
        frappe.db.sql(
            """
            SELECT name FROM `tabDocument Version`
            WHERE ocr_status = 'Completed'
              AND TRIM(COALESCE(ocr_content, '')) = ''
            """,
            pluck="name",
        )
    )
    document_names = {row.name for row in documents}
    versions_by_name = {row.name: row for row in versions}
    version_numbers = {}

    for version in versions:
        context = {
            "document": version.parent,
            "version": version.name,
        }
        if version.parent not in document_names:
            _issue(
                issues,
                "error",
                "orphaned_version",
                "Document version has no parent document.",
                **context,
            )
        number_key = (version.parent, str(version.version_number or "").strip())
        if not number_key[1]:
            _issue(
                issues,
                "error",
                "version_number_missing",
                "Document version has no version number.",
                **context,
            )
        elif number_key in version_numbers:
            _issue(
                issues,
                "error",
                "duplicate_version_number",
                "Document contains duplicate version numbers.",
                duplicate_of=version_numbers[number_key],
                version_number=number_key[1],
                **context,
            )
        else:
            version_numbers[number_key] = version.name

        _check_stored_file(
            issues,
            file_url=version.attachment,
            expected_checksum=version.file_checksum,
            kind="original",
            check_encryption=encryption_expected,
            **context,
        )
        _check_stored_file(
            issues,
            file_url=version.preview_attachment,
            expected_checksum=version.preview_checksum,
            kind="preview",
            check_encryption=encryption_expected,
            **context,
        )
        if version.ocr_status == "Completed" and version.name in empty_completed_ocr:
            _issue(
                issues,
                "warning",
                "completed_ocr_empty",
                "OCR is marked completed but contains no text.",
                **context,
            )

    _check_file_references(issues, document_names, versions_by_name)
    _check_orphaned_derived_records(issues, document_names, set(versions_by_name))
    _check_physical_orphans(issues)
    counts = {
        "error": sum(issue["severity"] == "error" for issue in issues),
        "warning": sum(issue["severity"] == "warning" for issue in issues),
    }
    return {
        "documents_checked": len(documents),
        "versions_checked": len(versions),
        "issues": issues,
        "counts": counts,
        "ok": not issues,
    }


@frappe.whitelist()
def run_document_archive_sanity_check():
    if "System Manager" not in frappe.get_roles():
        frappe.throw(
            "System Manager role is required.",
            frappe.PermissionError,
        )
    return check_document_archive()


def scheduled_archive_sanity_check():
    result = check_document_archive()
    if not result["issues"]:
        return
    log_result = {
        **result,
        "issues": result["issues"][:100],
        "issues_truncated": max(len(result["issues"]) - 100, 0),
    }
    frappe.log_error(
        title="Document Archive Sanity Check",
        message=frappe.as_json(log_result, indent=2),
    )
