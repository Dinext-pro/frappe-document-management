import hashlib
import os
import secrets
from urllib.parse import quote

import frappe
from frappe.utils import add_days, cint, get_datetime, now_datetime
from frappe.utils.file_manager import get_file_path
from werkzeug.wrappers import Response

from document_management.frappe_document_management.utils.document_provider import (
    OWNER_FIELD,
    get_standalone_document,
)

from document_management.frappe_document_management.utils.file_crypto import (
    read_path_bytes,
)


_GUEST_DOWNLOAD_METHOD = f"{__name__}.download_shared_document"
_GUEST_RESOLVER_HOOK = "document_management_guest_share_resolver"


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _share_url(token):
    return f"{frappe.utils.get_url()}/document_share?token={quote(token)}"


def _shared_download_url(token, download=1):
    return (
        f"/api/method/{_GUEST_DOWNLOAD_METHOD}?token={quote(token)}&download={int(bool(download))}"
    )


def _file_kind(file_url):
    extension = os.path.splitext(file_url or "")[1].lower()
    if extension == ".pdf":
        return "pdf"
    if extension in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return "image"
    if extension == ".md":
        return "markdown"
    if extension == ".txt":
        return "text"
    return "download"


def _get_share_link(token):
    if not token:
        frappe.throw("Share token is required.", frappe.PermissionError)
    link_name = frappe.db.get_value(
        "Document Share Link",
        {"token_hash": _token_hash(token), "enabled": 1},
        "name",
    )
    if not link_name:
        frappe.throw("Share link is invalid or revoked.", frappe.PermissionError)
    link = frappe.get_doc("Document Share Link", link_name)
    if not link.enabled:
        frappe.throw("Share link is invalid or revoked.", frappe.PermissionError)
    if not link.expires_at or get_datetime(link.expires_at) <= now_datetime():
        frappe.throw("Share link has expired.", frappe.PermissionError)
    return link


def _get_shared_document(token):
    link = _get_share_link(token)
    try:
        document = frappe.get_doc("Document", link.document)
    except frappe.DoesNotExistError:
        frappe.clear_last_message()
        frappe.throw("Shared document is no longer available.", frappe.PermissionError)
    if document.is_deleted:
        frappe.throw("Shared document is no longer available.", frappe.PermissionError)
    return link, document


def _resolve_provider_share(token, link, document, provider, *, download=None):
    resolvers = frappe.get_hooks(_GUEST_RESOLVER_HOOK, default={}).get(provider, ())
    if len(resolvers) != 1:
        frappe.throw("Shared document is no longer available.", frappe.PermissionError)
    data = frappe.get_attr(resolvers[0])(link, document, download)
    if download is not None:
        response = Response(data["content"], content_type=data["content_type"])
        disposition = "attachment" if download else "inline"
        filename = quote(data["filename"], safe="")
        response.headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{filename}"
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        _set_guest_cache_headers(response)
        return response

    show_download = bool(data.get("show_download_button"))
    return {
        "title": data["title"],
        "filename": data["filename"],
        "kind": data["kind"],
        "preview_url": _shared_download_url(token, download=0) if data["kind"] == "pdf" else "",
        "download_url": _shared_download_url(token, download=1) if show_download else "",
        "show_download_button": show_download,
        "expires_at": link.expires_at,
        "isolated_pdf": True,
    }


def _set_guest_cache_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def set_guest_share_page_headers(request, response):
    if response is None:
        return
    route = request.path.strip("/")
    if route == "document_share" or route in {
        f"api/method/{_GUEST_DOWNLOAD_METHOD}",
        f"api/v1/method/{_GUEST_DOWNLOAD_METHOD}",
        f"api/v2/method/{_GUEST_DOWNLOAD_METHOD}",
    }:
        _set_guest_cache_headers(response)


def get_shared_document_context(token):
    link, document = _get_shared_document(token)
    provider = document.get(OWNER_FIELD)
    if provider:
        return _resolve_provider_share(token, link, document, provider)
    return _standalone_share_context(token, link, document)


def _standalone_share_context(token, link, document):
    version = document.get_current_version()
    file_url = (
        version.preview_attachment
        if link.file_version == "Searchable PDF"
        else version.attachment
    )
    path = get_file_path(file_url)
    if not path or not os.path.isfile(path):
        frappe.throw("Shared file is unavailable.")
    filename = os.path.basename(path)
    return {
        "document": document,
        "link": link,
        "file_url": file_url,
        "filename": filename,
        "kind": _file_kind(file_url),
        "preview_url": _shared_download_url(token, download=0),
        "download_url": _shared_download_url(token, download=1),
        "show_download_button": not bool(cint(link.get("hide_download_button"))),
    }


@frappe.whitelist()
def create_share_link(
    document,
    expiration_days=7,
    file_version="Original",
    hide_download_button=0,
):
    doc = get_standalone_document(document)
    doc.check_permission("share")
    if doc.is_deleted:
        frappe.throw("Restore the document before sharing it.")
    try:
        expiration_days = int(expiration_days)
    except (TypeError, ValueError):
        frappe.throw("Expiration days must be a number.")
    if expiration_days < 1 or expiration_days > 365:
        frappe.throw("Expiration days must be between 1 and 365.")
    if file_version not in {"Original", "Searchable PDF"}:
        frappe.throw("Invalid shared file version.")
    version = doc.get_current_version()
    if not version or not version.attachment:
        frappe.throw("The document has no file to share.")
    if file_version == "Searchable PDF" and not version.preview_attachment:
        frappe.throw("This document has no searchable PDF.")

    token = secrets.token_urlsafe(36)
    link = frappe.get_doc(
        {
            "doctype": "Document Share Link",
            "document": doc.name,
            "token_hash": _token_hash(token),
            "expires_at": add_days(now_datetime(), expiration_days),
            "file_version": file_version,
            "hide_download_button": cint(hide_download_button),
            "enabled": 1,
        }
    )
    link.insert(ignore_permissions=True)
    return {
        "name": link.name,
        "url": _share_url(token),
        "expires_at": link.expires_at,
    }


@frappe.whitelist()
def revoke_share_link(name):
    link = frappe.get_doc("Document Share Link", name)
    document = get_standalone_document(link.document)
    document.check_permission("share")
    link.db_set("enabled", 0)
    return {"name": link.name, "enabled": 0}


@frappe.whitelist()
def get_share_links(document):
    doc = get_standalone_document(document)
    doc.check_permission("share")
    return frappe.get_all(
        "Document Share Link",
        filters={"document": doc.name},
        fields=[
            "name",
            "expires_at",
            "file_version",
            "hide_download_button",
            "enabled",
            "creation",
            "owner",
        ],
        order_by="creation desc",
    )


@frappe.whitelist(allow_guest=True)
def download_shared_document(token, download=1):
    link, document = _get_shared_document(token)
    download_requested = bool(cint(download))
    provider = document.get(OWNER_FIELD)
    if provider:
        return _resolve_provider_share(token, link, document, provider, download=download_requested)
    context = _standalone_share_context(token, link, document)
    if download_requested and not context["show_download_button"]:
        frappe.throw(
            "Download is disabled for this share link.",
            frappe.PermissionError,
        )
    path = get_file_path(context["file_url"])
    frappe.local.response.filecontent = read_path_bytes(path)
    frappe.local.response.filename = context["filename"]
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = (
        "attachment" if download_requested else "inline"
    )


def cleanup_expired_share_links():
    links = frappe.get_all(
        "Document Share Link",
        filters={"expires_at": ["<", now_datetime()]},
        fields=["name", "document"],
        limit_page_length=0,
    )
    has_provider = frappe.get_meta("Document").has_field(OWNER_FIELD)
    expired = []
    for link in links:
        if has_provider and frappe.db.get_value("Document", link.document, OWNER_FIELD):
            continue
        name = link.name
        frappe.delete_doc(
            "Document Share Link",
            name,
            ignore_permissions=True,
            force=True,
        )
        expired.append(name)
    if expired:
        frappe.db.commit()
    return expired
