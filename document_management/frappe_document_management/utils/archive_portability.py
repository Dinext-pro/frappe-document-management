import hashlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import PurePosixPath

import frappe
from document_management.frappe_document_management.utils.document_provider import get_standalone_document
from frappe.utils import now_datetime
from frappe.utils.file_manager import get_file_path

from document_management.frappe_document_management.utils.document_access import (
    department_doctype_exists,
)
from document_management.frappe_document_management.utils.file_crypto import (
    decrypted_temp_file,
    encrypt_file_doc,
    read_path_bytes,
    sha256_path,
)
from document_management.frappe_document_management.utils.storage_paths import (
    prune_empty_parent_dirs,
)


ARCHIVE_FORMAT = "frappe-document-management"
ARCHIVE_VERSION = 2
MAX_ARCHIVE_MEMBERS = 10000
MAX_ARCHIVE_SIZE = 512 * 1024 * 1024
MAX_UNCOMPRESSED_SIZE = 2 * 1024 * 1024 * 1024
MAX_DOCUMENTS = 1000
EXPORT_ATTACHMENT_DOCTYPE = "Document Management Settings"
EXPORT_ATTACHMENT_NAME = "Document Management Settings"
PARENT_FIELDS = (
    "document_code",
    "title",
    "status",
    "category",
    "folder",
    "department",
    "party_type",
    "party_name",
    "description",
    "expiry_date",
    "review_date",
    "only_me",
)
VERSION_FIELDS = (
    "version_number",
    "release_date",
    "change_log",
    "is_markdown",
    "ocr_status",
    "ocr_content",
)
PAGE_FIELDS = (
    "version_number",
    "page_number",
    "content",
    "content_hash",
    "extraction_method",
)
TAG_FIELDS = (
    "tag_name",
    "color",
    "is_active",
    "matching_algorithm",
    "match_pattern",
)


def _sha256_file(path, block_size=1024 * 1024):
    checksum = sha256_path(path)
    with decrypted_temp_file(path=path) as readable_path:
        size = os.path.getsize(readable_path)
    return checksum, size


def _archive_file(zf, document_name, version, kind, file_url, checksum):
    if not file_url:
        return None
    path = get_file_path(file_url)
    if not path or not os.path.isfile(path):
        frappe.throw(f"Archive source file is missing: {file_url}")
    with decrypted_temp_file(file_url=file_url, path=path) as readable_path:
        with open(readable_path, "rb") as source:
            return write_archive_member(
                zf, document_name, version.version_number, kind, os.path.basename(path), source, checksum
            )


def write_archive_member(zf, document_name, version_number, kind, filename, content, checksum=None):
    """Write one version member and calculate its manifest descriptor from the bytes written."""
    path = (PurePosixPath("documents") / document_name / str(version_number) / kind / filename).as_posix()
    source = io.BytesIO(content) if isinstance(content, bytes) else content
    digest = hashlib.sha256()
    size = 0
    with zf.open(path, "w") as target:
        while block := source.read(1024 * 1024):
            digest.update(block)
            size += len(block)
            target.write(block)
    actual = digest.hexdigest()
    if checksum and checksum != actual:
        frappe.throw("Archive source checksum mismatch.")
    return {"path": path, "filename": filename, "checksum": actual, "size": size}


def _category_definition(name):
    category = frappe.get_doc("Document Category", name)
    return {
        "name": category.name,
        "owner": category.owner,
        "only_me": category.only_me,
        "roles_with_access": [
            row.role for row in category.get("roles_with_access") or []
        ],
        "departments_with_access": [
            row.department
            for row in category.get("departments_with_access") or []
        ],
    }


def _tag_definition(name):
    tag = frappe.get_doc("Document Tag", name)
    return {field: tag.get(field) for field in TAG_FIELDS}


def _document_shares(document_name):
    fields = ["user", "read", "write", "share"]
    return frappe.get_all(
        "DocShare",
        filters={
            "share_doctype": "Document",
            "share_name": document_name,
            "everyone": 0,
        },
        fields=fields,
    )


def build_document_archive_file(document_names, target_path=None):
    names = list(dict.fromkeys(document_names or []))
    if not names:
        frappe.throw("Select at least one document.")
    if len(names) > MAX_DOCUMENTS:
        frappe.throw(f"An archive can contain at most {MAX_DOCUMENTS} documents.")

    if not target_path:
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        target_path = handle.name
        handle.close()

    manifest = {
        "format": ARCHIVE_FORMAT,
        "version": ARCHIVE_VERSION,
        "exported_at": str(now_datetime()),
        "dependencies": {"categories": [], "tags": []},
        "documents": [],
    }
    categories = {}
    tags = {}
    try:
        with zipfile.ZipFile(
            target_path,
            "w",
            zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as zf:
            for document_name in names:
                doc = get_standalone_document(document_name)
                doc.check_permission("read")
                if doc.is_deleted:
                    frappe.throw("Restore documents before exporting them.")
                entry = {
                    "source_name": doc.name,
                    "owner": doc.owner,
                    "creation": str(doc.creation),
                    "modified": str(doc.modified),
                    "metadata": {
                        field: doc.get(field)
                        for field in PARENT_FIELDS
                    },
                    "tags": [row.tag for row in doc.get("tags") or []],
                    "roles_with_access": [
                        row.role for row in doc.get("roles_with_access") or []
                    ],
                    "departments_with_access": [
                        row.department
                        for row in doc.get("departments_with_access") or []
                    ],
                    "shares": _document_shares(doc.name),
                    "pages": frappe.get_all(
                        "Document Page",
                        filters={"document": doc.name},
                        fields=list(PAGE_FIELDS),
                        order_by="page_number asc",
                    ),
                    "versions": [],
                }
                if doc.category and doc.category not in categories:
                    categories[doc.category] = _category_definition(doc.category)
                for tag_name in entry["tags"]:
                    if tag_name not in tags:
                        tags[tag_name] = _tag_definition(tag_name)
                for version in doc.get("versions") or []:
                    version_entry = {
                        field: version.get(field)
                        for field in VERSION_FIELDS
                    }
                    version_entry["original"] = _archive_file(
                        zf,
                        doc.name,
                        version,
                        "original",
                        version.attachment,
                        version.file_checksum,
                    )
                    version_entry["preview"] = _archive_file(
                        zf,
                        doc.name,
                        version,
                        "preview",
                        version.preview_attachment,
                        version.preview_checksum,
                    )
                    entry["versions"].append(version_entry)
                manifest["documents"].append(entry)
            manifest["dependencies"]["categories"] = list(categories.values())
            manifest["dependencies"]["tags"] = list(tags.values())
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=True, indent=2, default=str),
            )
        if os.path.getsize(target_path) > MAX_ARCHIVE_SIZE:
            frappe.throw("The generated document archive is too large.")
        return target_path, manifest
    except Exception:
        if os.path.isfile(target_path):
            os.remove(target_path)
        raise


def build_document_archive(document_names):
    path, manifest = build_document_archive_file(document_names)
    try:
        with open(path, "rb") as handle:
            return handle.read(), manifest
    finally:
        if os.path.isfile(path):
            os.remove(path)


def _safe_info(zf, member):
    path = PurePosixPath(member)
    if path.is_absolute() or ".." in path.parts:
        frappe.throw("Archive contains an unsafe file path.")
    try:
        return zf.getinfo(member)
    except KeyError:
        frappe.throw(f"Archive member is missing: {member}")


def read_archive_manifest(zf):
    _validate_zip(zf)
    info = _safe_info(zf, "manifest.json")
    if info.file_size > 25 * 1024 * 1024:
        frappe.throw("Archive manifest is too large.")
    manifest = json.loads(zf.read(info))
    if not isinstance(manifest, dict) or manifest.get("format") != ARCHIVE_FORMAT or manifest.get("version") != ARCHIVE_VERSION:
        frappe.throw("Unsupported document archive format.")
    return manifest


def _validate_zip(zf):
    members = zf.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        frappe.throw("Document archive contains too many files.")
    total_size = 0
    seen = set()
    for info in members:
        path = PurePosixPath(info.filename)
        if (
            path.is_absolute() or ".." in path.parts or "\\" in info.filename
            or info.filename != path.as_posix() or info.orig_filename != info.filename
            or info.filename in seen or info.flag_bits & 1
        ):
            frappe.throw("Archive contains an invalid or repeated file path.")
        seen.add(info.filename)
        total_size += info.file_size
        if total_size > MAX_UNCOMPRESSED_SIZE:
            frappe.throw("Document archive is too large.")


def _required_values(manifest):
    users = set()
    roles = set()
    departments = set()
    has_department = department_doctype_exists()
    for category in manifest.get("dependencies", {}).get("categories", []):
        users.add(category.get("owner"))
        roles.update(category.get("roles_with_access") or [])
        if has_department:
            departments.update(category.get("departments_with_access") or [])
    for entry in manifest.get("documents") or []:
        users.add(entry.get("owner"))
        users.update(share.get("user") for share in entry.get("shares") or [])
        roles.update(entry.get("roles_with_access") or [])
        if has_department:
            departments.update(entry.get("departments_with_access") or [])
        metadata = entry.get("metadata") or {}
        if has_department and metadata.get("department"):
            departments.add(metadata["department"])
    return {
        "User": {value for value in users if value},
        "Role": {value for value in roles if value},
        "Department": {value for value in departments if value},
    }


def _validate_prerequisites(manifest):
    missing = []
    for doctype, values in _required_values(manifest).items():
        for value in sorted(values):
            if not frappe.db.exists(doctype, value):
                missing.append(f"{doctype}: {value}")
    for entry in manifest.get("documents") or []:
        metadata = entry.get("metadata") or {}
        if metadata.get("party_type") and metadata.get("party_name"):
            if not frappe.db.exists(
                metadata["party_type"],
                metadata["party_name"],
            ):
                missing.append(
                    f'{metadata["party_type"]}: {metadata["party_name"]}'
                )
    if missing:
        frappe.throw(
            "Archive prerequisites are missing. Nothing was imported:\n"
            + "\n".join(missing)
        )


def _restore_dependencies(manifest):
    dependencies = manifest.get("dependencies") or {}
    has_department = department_doctype_exists()
    for definition in dependencies.get("categories") or []:
        name = definition["name"]
        if frappe.db.exists("Document Category", name):
            existing = frappe.get_doc("Document Category", name)
            existing_signature = {
                "owner": existing.owner,
                "only_me": existing.only_me,
                "roles_with_access": sorted(
                    row.role for row in existing.roles_with_access
                ),
                "departments_with_access": (
                    sorted(row.department for row in existing.departments_with_access)
                    if has_department
                    else []
                ),
            }
            expected_signature = {
                "owner": definition["owner"],
                "only_me": definition.get("only_me") or 0,
                "roles_with_access": sorted(
                    definition.get("roles_with_access") or []
                ),
                "departments_with_access": (
                    sorted(definition.get("departments_with_access") or [])
                    if has_department
                    else []
                ),
            }
            if existing_signature != expected_signature:
                frappe.throw(
                    f"Document Category security differs on target: {name}"
                )
            continue
        category = frappe.new_doc("Document Category")
        category.category_name = name
        category.owner = definition["owner"]
        category.only_me = definition.get("only_me") or 0
        for role in definition.get("roles_with_access") or []:
            category.append("roles_with_access", {"role": role})
        if has_department:
            for department in definition.get("departments_with_access") or []:
                category.append(
                    "departments_with_access",
                    {"department": department},
                )
        category.insert(ignore_permissions=True)

    for definition in dependencies.get("tags") or []:
        name = definition["tag_name"]
        if frappe.db.exists("Document Tag", name):
            continue
        tag = frappe.new_doc("Document Tag")
        for field in TAG_FIELDS:
            tag.set(field, definition.get(field))
        tag.insert(ignore_permissions=True)


def copy_archive_member(zf, descriptor, target=None, remaining_budget=None):
    """Verify a member while copying it, bounded by its descriptor and the remaining budget."""
    info = _safe_info(zf, descriptor["path"])
    expected_size = int(descriptor["size"])
    if expected_size < 0 or expected_size > MAX_UNCOMPRESSED_SIZE:
        frappe.throw("Archive file size exceeds maximum allowed limit.")
    if remaining_budget is not None and expected_size > remaining_budget:
        frappe.throw("Document archive exceeds maximum uncompressed size.")
    if info.file_size != expected_size:
        frappe.throw("Archive file size does not match its manifest.")
    size = 0
    digest = hashlib.sha256()
    with zf.open(info) as source:
        while block := source.read(1024 * 1024):
            size += len(block)
            if size > expected_size:
                frappe.throw("Archive member decompressed size exceeds declared limit.")
            digest.update(block)
            if target is not None:
                target.write(block)
    if size != expected_size or digest.hexdigest() != descriptor["checksum"]:
        frappe.throw("Archive file checksum does not match its manifest.")
    return size


def _extract_blob(zf, descriptor, version_name, fieldname, remaining_budget=None):
    if not descriptor:
        return None, None, 0
    extension = os.path.splitext(descriptor["filename"])[1]
    filename = f"archive-{frappe.generate_hash(length=20)}{extension}"
    destination = frappe.get_site_path("private", "files", filename)
    target = open(destination, "xb")
    try:
        with target:
            size = copy_archive_member(zf, descriptor, target, remaining_budget)
    except Exception:
        os.remove(destination)
        raise
    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": filename,
            "file_url": f"/private/files/{filename}",
            "attached_to_doctype": "Document Version",
            "attached_to_name": version_name,
            "attached_to_field": fieldname,
            "is_private": 1,
        }
    )
    try:
        file_doc.insert(ignore_permissions=True)
        encrypt_file_doc(file_doc)
    except Exception:
        if os.path.isfile(destination):
            os.remove(destination)
        raise
    return file_doc, destination, size


def _restore_shares(document_name, shares):
    for share in shares or []:
        if not share.get("user"):
            continue
        share_doc = frappe.get_doc(
            {
                "doctype": "DocShare",
                "share_doctype": "Document",
                "share_name": document_name,
                "user": share["user"],
                "read": share.get("read") or 0,
                "write": share.get("write") or 0,
                "share": share.get("share") or 0,
            }
        )
        share_doc.insert(ignore_permissions=True, ignore_if_duplicate=True)


def restore_document_archive(content):
    if len(content) > MAX_ARCHIVE_SIZE:
        frappe.throw("Uploaded document archive is too large.")
    created_documents = []
    created_file_records = {}
    archive_path = None
    remaining_budget = MAX_UNCOMPRESSED_SIZE
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            handle.write(content)
            archive_path = handle.name
        with zipfile.ZipFile(archive_path) as zf:
            manifest = read_archive_manifest(zf)
            _validate_prerequisites(manifest)
            has_department = department_doctype_exists()
            _restore_dependencies(manifest)
            for entry in manifest.get("documents") or []:
                metadata = entry.get("metadata") or {}
                if not has_department:
                    metadata = {**metadata, "department": None}
                doc = frappe.new_doc("Document")
                for field in PARENT_FIELDS:
                    doc.set(field, metadata.get(field))
                doc.owner = entry.get("owner") or frappe.session.user
                for tag in entry.get("tags") or []:
                    doc.append("tags", {"tag": tag})
                for role in entry.get("roles_with_access") or []:
                    doc.append("roles_with_access", {"role": role})
                if has_department:
                    for department in entry.get("departments_with_access") or []:
                        doc.append(
                            "departments_with_access",
                            {"department": department},
                        )

                version_blobs = []
                for archived_version in entry.get("versions") or []:
                    original = archived_version.get("original")
                    if not original:
                        frappe.throw(
                            "Archived document version has no original file."
                        )
                    extension = os.path.splitext(original["filename"])[1]
                    version = doc.append(
                        "versions",
                        {
                            field: archived_version.get(field)
                            for field in VERSION_FIELDS
                        },
                    )
                    version.attachment = (
                        f"/private/files/archive-import-"
                        f"{frappe.generate_hash(length=12)}{extension}"
                    )
                    version.file_checksum = original["checksum"]
                    version.file_size = original["size"]
                    version_blobs.append(
                        (
                            version,
                            original,
                            archived_version.get("preview"),
                        )
                    )
                doc.insert(ignore_permissions=True)
                created_documents.append(doc.name)
                versions_by_number = {
                    str(row.version_number): row for row in doc.versions
                }

                doc_file_names = []
                for version, original, preview in version_blobs:
                    original_file, original_path, orig_bytes = _extract_blob(
                        zf,
                        original,
                        version.name,
                        "attachment",
                        remaining_budget=remaining_budget,
                    )
                    remaining_budget -= orig_bytes
                    created_file_records[original_file.name] = original_path
                    doc_file_names.append(original_file.name)
                    values = {"attachment": original_file.file_url}
                    if preview:
                        preview_file, preview_path, prev_bytes = _extract_blob(
                            zf,
                            preview,
                            version.name,
                            "preview_attachment",
                            remaining_budget=remaining_budget,
                        )
                        remaining_budget -= prev_bytes
                        created_file_records[preview_file.name] = preview_path
                        doc_file_names.append(preview_file.name)
                        values.update(
                            {
                                "preview_attachment": preview_file.file_url,
                                "preview_checksum": preview["checksum"],
                            }
                        )
                    frappe.db.set_value("Document Version", version.name, values)

                for page in entry.get("pages") or []:
                    version = versions_by_number.get(str(page["version_number"]))
                    if not version:
                        frappe.throw(
                            "Archive page references an unknown document version."
                        )
                    page_doc = frappe.new_doc("Document Page")
                    page_doc.document = doc.name
                    page_doc.document_version = version.name
                    for field in PAGE_FIELDS:
                        if field != "version_number":
                            page_doc.set(field, page.get(field))
                    page_doc.version_number = version.version_number
                    page_doc.insert(ignore_permissions=True)
                _restore_shares(doc.name, entry.get("shares"))
                doc.reload()
                doc.save(ignore_permissions=True)
                # Refresh physical paths after organize_document_files() may have moved them
                for fname in doc_file_names:
                    current_url = frappe.db.get_value("File", fname, "file_url")
                    if current_url:
                        resolved = get_file_path(current_url)
                        if resolved:
                            created_file_records[fname] = resolved
                frappe.db.set_value(
                    "Document",
                    doc.name,
                    {
                        "owner": doc.owner,
                        "creation": entry.get("creation"),
                        "modified": entry.get("modified"),
                    },
                    update_modified=False,
                )
        return {"documents": created_documents, "count": len(created_documents)}
    except Exception:
        frappe.db.rollback()
        for path in created_file_records.values():
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                    prune_empty_parent_dirs(path)
                except OSError:
                    pass
        raise
    finally:
        if archive_path and os.path.isfile(archive_path):
            os.remove(archive_path)


def _register_export_file(path, filename):
    destination = frappe.get_site_path("private", "files", filename)
    shutil.move(path, destination)
    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": filename,
            "file_url": f"/private/files/{filename}",
            "attached_to_doctype": EXPORT_ATTACHMENT_DOCTYPE,
            "attached_to_name": EXPORT_ATTACHMENT_NAME,
            "is_private": 1,
        }
    )
    try:
        file_doc.insert(ignore_permissions=True)
    except Exception:
        if os.path.isfile(destination):
            os.remove(destination)
        raise
    return file_doc


@frappe.whitelist()
def export_document_archive(documents):
    from urllib.parse import quote

    if isinstance(documents, str):
        documents = json.loads(documents)
    filename = f"document-archive-{frappe.generate_hash(length=12)}.zip"
    path, manifest = build_document_archive_file(documents)
    file_doc = _register_export_file(path, filename)
    download_url = (
        "/api/method/document_management.frappe_document_management.utils."
        "archive_portability.download_document_archive"
        f"?file_name={quote(str(file_doc.name))}"
    )
    return {
        "file_name": file_doc.name,
        "file_url": file_doc.file_url,
        "download_url": download_url,
        "documents": len(manifest["documents"]),
    }


@frappe.whitelist()
def download_document_archive(file_name):
    if not file_name:
        frappe.throw("Archive file name is required.")
    file_doc = frappe.get_doc("File", file_name)
    if (
        file_doc.attached_to_doctype != EXPORT_ATTACHMENT_DOCTYPE
        or file_doc.attached_to_name != EXPORT_ATTACHMENT_NAME
    ):
        frappe.throw(
            "Requested file is not a document archive export.",
            frappe.PermissionError,
        )
    user = frappe.session.user
    if (
        user != "Administrator"
        and file_doc.owner != user
        and "System Manager" not in frappe.get_roles(user)
    ):
        frappe.throw(
            "You do not have permission to download this archive.",
            frappe.PermissionError,
        )
    path = get_file_path(file_doc.file_url)
    if not path or not os.path.isfile(path):
        frappe.throw("Archive file is no longer available.")
    frappe.local.response.filecontent = read_path_bytes(path)
    frappe.local.response.filename = file_doc.file_name or os.path.basename(path)
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = "attachment"
