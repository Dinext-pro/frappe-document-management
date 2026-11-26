import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import quote

import frappe
from frappe.utils import add_days, cint, get_datetime, now_datetime
from frappe.utils.file_manager import get_file_path

from document_management.frappe_document_management.utils.file_crypto import (
    INLINE_UNSAFE_EXTENSIONS,
    read_path_bytes,
)


SHARE_RATE_LIMIT_PER_MINUTE = 60
SHARE_UNLOCK_RATE_LIMIT_PER_MINUTE = 10
SHARE_UNLOCK_COOKIE = "dm_share_unlock"
SHARE_UNLOCK_TTL_SECONDS = 3600
SHARE_VIEW_DEDUP_SECONDS = 120


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _passcode_hash(passcode, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(passcode).encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    ).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def _verify_passcode(stored_hash, passcode):
    if not stored_hash:
        return True
    if not passcode:
        return False
    parts = str(stored_hash).split("$")
    if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
        return False
    expected = _passcode_hash(passcode, salt=parts[1])
    return hmac.compare_digest(stored_hash, expected)


def _unlock_signing_key():
    try:
        from frappe.utils.password import get_encryption_key

        key = get_encryption_key()
        if key:
            return str(key).encode("utf-8")
    except Exception:
        pass
    conf = getattr(frappe.local, "conf", None) or {}
    fallback = conf.get("encryption_key") or getattr(frappe.local, "site", "") or "dm-share"
    return str(fallback).encode("utf-8")


def _sign_unlock_cookie(token_hash, passcode_hash, expires_ts=None):
    expires_ts = int(expires_ts or (time.time() + SHARE_UNLOCK_TTL_SECONDS))
    payload = f"{token_hash[:32]}:{expires_ts}"
    mac = hmac.new(
        _unlock_signing_key(),
        f"{payload}:{passcode_hash}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{mac}"


def _verify_unlock_cookie(cookie_value, token_hash, passcode_hash, now_ts=None):
    if not cookie_value or not token_hash or not passcode_hash:
        return False
    parts = str(cookie_value).split(":")
    if len(parts) != 3:
        return False
    prefix, exp_raw, signature = parts
    if not hmac.compare_digest(prefix, token_hash[:32]):
        return False
    try:
        expires_ts = int(exp_raw)
    except (TypeError, ValueError):
        return False
    current_ts = int(time.time() if now_ts is None else now_ts)
    if expires_ts <= current_ts:
        return False
    expected = _sign_unlock_cookie(token_hash, passcode_hash, expires_ts=expires_ts)
    return hmac.compare_digest(str(cookie_value), expected)


def _request_unlock_cookie():
    request = getattr(frappe.local, "request", None)
    cookies = getattr(request, "cookies", None) if request else None
    if cookies:
        return cookies.get(SHARE_UNLOCK_COOKIE)
    return None


def _request_ip():
    return getattr(frappe.local, "request_ip", None) or "unknown"


def _enforce_share_rate_limit(
    token_hash,
    bucket="rl",
    limit=SHARE_RATE_LIMIT_PER_MINUTE,
):
    try:
        cache = frappe.cache()
        ip = _request_ip()
        key = f"dm_share_{bucket}:{token_hash[:16]}:{ip}"
        current = int(cache.get_value(key) or 0)
        if current >= int(limit):
            frappe.throw(
                "Too many requests for this share link. Please try again shortly.",
                frappe.PermissionError,
            )
        cache.set_value(key, current + 1, expires_in_sec=60)
    except frappe.PermissionError:
        raise
    except Exception:
        pass


def _view_dedup_key(token_hash):
    return f"dm_share_view:{token_hash[:16]}:{_request_ip()}"


def _is_recent_view(token_hash):
    if not token_hash:
        return False
    try:
        return bool(frappe.cache().get_value(_view_dedup_key(token_hash)))
    except Exception:
        return False


def _mark_recent_view(token_hash):
    if not token_hash:
        return
    try:
        frappe.cache().set_value(
            _view_dedup_key(token_hash),
            1,
            expires_in_sec=SHARE_VIEW_DEDUP_SECONDS,
        )
    except Exception:
        pass


def _share_url(token):
    return f"{frappe.utils.get_url()}/document_share?token={quote(token)}"


def _shared_download_url(token, download=1):
    return (
        "/api/method/document_management.frappe_document_management.utils."
        "document_sharing.download_shared_document"
        f"?token={quote(token)}&download={int(bool(download))}"
    )


def _file_kind(file_url):
    extension = os.path.splitext(file_url or "")[1].lower()
    if extension == ".pdf":
        return "pdf"
    if extension in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"
    if extension == ".md":
        return "markdown"
    if extension == ".txt":
        return "text"
    return "download"


def _get_share_link(token, check_views=True, token_hash=None):
    if not token:
        frappe.throw("Share token is required.", frappe.PermissionError)
    token_hash = token_hash or _token_hash(token)
    link_name = frappe.db.get_value(
        "Document Share Link",
        {"token_hash": token_hash, "enabled": 1},
        "name",
    )
    if not link_name:
        frappe.throw("Share link is invalid or revoked.", frappe.PermissionError)
    link = frappe.get_doc("Document Share Link", link_name)
    if get_datetime(link.expires_at) <= now_datetime():
        frappe.throw("Share link has expired.", frappe.PermissionError)
    max_views = cint(link.get("max_views") or 0)
    access_count = cint(link.get("access_count") or 0)
    if (
        check_views
        and max_views > 0
        and access_count >= max_views
        and not _is_recent_view(token_hash)
    ):
        frappe.throw(
            "Share link has reached its maximum view limit.",
            frappe.PermissionError,
        )
    return link


def _consume_share_view(link, token_hash=None):
    if token_hash and _is_recent_view(token_hash):
        return True
    max_views = cint(link.get("max_views") or 0)
    sql_fn = getattr(frappe.db, "sql", None)
    if callable(sql_fn):
        sql_fn(
            """
            UPDATE `tabDocument Share Link`
            SET access_count = COALESCE(access_count, 0) + 1,
                last_accessed_at = %s
            WHERE name = %s
              AND enabled = 1
              AND (COALESCE(max_views, 0) = 0 OR COALESCE(access_count, 0) < max_views)
            """,
            (now_datetime(), link.name),
        )
        rows = sql_fn("SELECT ROW_COUNT()")
        affected = int(rows[0][0]) if rows and rows[0] else 0
        if max_views > 0 and affected == 0:
            frappe.throw(
                "Share link has reached its maximum view limit.",
                frappe.PermissionError,
            )
    else:
        try:
            current = cint(link.get("access_count") or 0)
            if max_views > 0 and current >= max_views:
                frappe.throw(
                    "Share link has reached its maximum view limit.",
                    frappe.PermissionError,
                )
            frappe.db.set_value(
                "Document Share Link",
                link.name,
                {
                    "access_count": current + 1,
                    "last_accessed_at": now_datetime(),
                },
                update_modified=False,
            )
        except frappe.PermissionError:
            raise
        except Exception:
            pass
    if token_hash:
        _mark_recent_view(token_hash)
    return True


def _is_authorized_for_passcode(link, token_hash, passcode=None):
    stored_passcode = link.get("passcode_hash") or ""
    if not stored_passcode:
        return True
    if _verify_unlock_cookie(_request_unlock_cookie(), token_hash, stored_passcode):
        return True
    if passcode and _verify_passcode(stored_passcode, passcode):
        return True
    return False


def get_shared_document_context(token, passcode=None):
    if not token:
        frappe.throw("Share token is required.", frappe.PermissionError)
    token_hash = _token_hash(token)
    _enforce_share_rate_limit(token_hash)
    link = _get_share_link(token, token_hash=token_hash)
    stored_passcode = link.get("passcode_hash") or ""
    if stored_passcode and not _is_authorized_for_passcode(
        link,
        token_hash,
        passcode=passcode,
    ):
        return {
            "requires_passcode": True,
            "passcode_error": bool(passcode),
            "link": link,
            "document": None,
            "file_url": "",
            "filename": "",
            "kind": "locked",
            "preview_url": "",
            "download_url": "",
            "show_download_button": False,
        }

    document = frappe.get_doc("Document", link.document)
    if document.is_deleted:
        frappe.throw("Shared document is no longer available.", frappe.PermissionError)
    version = document.get_current_version()
    if not version:
        frappe.throw("Shared file is unavailable.")
    file_url = (
        version.preview_attachment
        if link.file_version == "Searchable PDF"
        else version.attachment
    )
    if not file_url:
        frappe.throw("Shared file is unavailable.")
    path = get_file_path(file_url)
    if not path or not os.path.isfile(path):
        frappe.throw("Shared file is unavailable.")
    filename = os.path.basename(path)
    return {
        "requires_passcode": False,
        "passcode_error": False,
        "document": document,
        "link": link,
        "file_url": file_url,
        "filename": filename,
        "kind": _file_kind(file_url),
        "preview_url": _shared_download_url(token, download=0),
        "download_url": _shared_download_url(token, download=1),
        "show_download_button": not bool(cint(link.get("hide_download_button"))),
    }


@frappe.whitelist(allow_guest=True)
def unlock_shared_document(token, passcode):
    request = getattr(frappe.local, "request", None)
    if request and getattr(request, "method", "POST").upper() != "POST":
        frappe.throw("POST request is required to unlock this share link.", frappe.PermissionError)
    if not token:
        frappe.throw("Share token is required.", frappe.PermissionError)
    token_hash = _token_hash(token)
    _enforce_share_rate_limit(
        token_hash,
        bucket="unlock",
        limit=SHARE_UNLOCK_RATE_LIMIT_PER_MINUTE,
    )
    link = _get_share_link(token, token_hash=token_hash)
    stored_passcode = link.get("passcode_hash") or ""
    if not stored_passcode:
        return {"unlocked": True}
    if not _verify_passcode(stored_passcode, passcode):
        frappe.throw("Invalid passcode. Please try again.", frappe.PermissionError)

    signed = _sign_unlock_cookie(token_hash, stored_passcode)
    cookie_manager = getattr(frappe.local, "cookie_manager", None)
    if cookie_manager:
        is_secure = bool(
            request and (
                getattr(request, "is_secure", False)
                or (getattr(request, "headers", None) or {}).get("X-Forwarded-Proto") == "https"
            )
        )
        cookie_manager.set_cookie(
            SHARE_UNLOCK_COOKIE,
            signed,
            max_age=SHARE_UNLOCK_TTL_SECONDS,
            httponly=True,
            samesite="Lax",
            secure=is_secure,
        )
    return {"unlocked": True}


@frappe.whitelist()
def create_share_link(
    document,
    expiration_days=7,
    file_version="Original",
    hide_download_button=0,
    max_views=0,
    passcode=None,
):
    doc = frappe.get_doc("Document", document)
    doc.check_permission("share")
    if doc.is_deleted:
        frappe.throw("Restore the document before sharing it.")
    try:
        expiration_days = int(expiration_days)
    except (TypeError, ValueError):
        frappe.throw("Expiration days must be a number.")
    if expiration_days < 1 or expiration_days > 365:
        frappe.throw("Expiration days must be between 1 and 365.")
    try:
        max_views = max(int(max_views or 0), 0)
    except (TypeError, ValueError):
        frappe.throw("Maximum views must be a number.")
    if file_version not in {"Original", "Searchable PDF"}:
        frappe.throw("Invalid shared file version.")
    version = doc.get_current_version()
    if not version or not version.attachment:
        frappe.throw("The document has no file to share.")
    if file_version == "Searchable PDF" and not version.preview_attachment:
        frappe.throw("This document has no searchable PDF.")

    passcode_str = (str(passcode).strip() if passcode is not None else "")
    if passcode_str and len(passcode_str) < 4:
        frappe.throw("Passcode must be at least 4 characters long.")

    token = secrets.token_urlsafe(36)
    link_payload = {
        "doctype": "Document Share Link",
        "document": doc.name,
        "token_hash": _token_hash(token),
        "expires_at": add_days(now_datetime(), expiration_days),
        "file_version": file_version,
        "hide_download_button": cint(hide_download_button),
        "enabled": 1,
        "max_views": max_views,
        "access_count": 0,
    }
    if passcode_str:
        link_payload["passcode_hash"] = _passcode_hash(passcode_str)

    link = frappe.get_doc(link_payload)
    link.insert(ignore_permissions=True)
    return {
        "name": link.name,
        "url": _share_url(token),
        "expires_at": link.expires_at,
        "max_views": max_views,
        "has_passcode": bool(passcode_str),
    }


@frappe.whitelist()
def revoke_share_link(name):
    link = frappe.get_doc("Document Share Link", name)
    document = frappe.get_doc("Document", link.document)
    document.check_permission("share")
    link.db_set("enabled", 0)
    return {"name": link.name, "enabled": 0}


@frappe.whitelist()
def get_share_links(document):
    doc = frappe.get_doc("Document", document)
    doc.check_permission("share")
    rows = frappe.get_all(
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
            "access_count",
            "last_accessed_at",
            "max_views",
            "passcode_hash",
        ],
        order_by="creation desc",
    )
    for row in rows:
        row["has_passcode"] = bool(row.pop("passcode_hash", None))
    return rows


@frappe.whitelist(allow_guest=True)
def download_shared_document(token, download=1, passcode=None):
    token_hash = _token_hash(token) if token else ""
    context = get_shared_document_context(token, passcode=passcode)
    if context.get("requires_passcode"):
        frappe.throw(
            "A valid passcode is required to access this share link.",
            frappe.PermissionError,
        )
    download_requested = bool(cint(download))
    if download_requested and not context["show_download_button"]:
        frappe.throw(
            "Download is disabled for this share link.",
            frappe.PermissionError,
        )
    _consume_share_view(context["link"], token_hash=token_hash)
    path = get_file_path(context["file_url"])
    extension = os.path.splitext(context["filename"] or "")[1].lower()
    force_attachment = download_requested or extension in INLINE_UNSAFE_EXTENSIONS
    frappe.local.response.filecontent = read_path_bytes(path)
    frappe.local.response.filename = context["filename"]
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = (
        "attachment" if force_attachment else "inline"
    )


def cleanup_expired_share_links():
    expired = frappe.get_all(
        "Document Share Link",
        filters={"expires_at": ["<", now_datetime()]},
        pluck="name",
        limit_page_length=0,
    )
    for name in expired:
        frappe.delete_doc(
            "Document Share Link",
            name,
            ignore_permissions=True,
            force=True,
        )
    if expired:
        frappe.db.commit()
    return expired
