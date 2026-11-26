import os

import frappe
from document_management.frappe_document_management.utils.document_provider import (
    OWNER_FIELD, get_standalone_document, standalone_filters,
)
from frappe.utils import add_days, add_to_date, now_datetime

from document_management.frappe_document_management.utils.archive_portability import (
    EXPORT_ATTACHMENT_DOCTYPE,
    EXPORT_ATTACHMENT_NAME,
)
from document_management.frappe_document_management.utils.storage_paths import (
    private_file_path,
    prune_empty_parent_dirs,
)


def _document_files(document_name):
    version_names = frappe.get_all(
        "Document Version",
        filters={"parent": document_name},
        pluck="name",
    )
    files = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Document",
            "attached_to_name": document_name,
        },
        fields=["name", "file_url", "content_hash"],
    )
    if version_names:
        files.extend(
            frappe.get_all(
                "File",
                filters={
                    "attached_to_doctype": "Document Version",
                    "attached_to_name": ["in", version_names],
                },
                fields=["name", "file_url", "content_hash"],
            )
        )
    return files


def _physical_cleanup_paths(files):
    file_names = {row.name for row in files}
    paths = []
    for row in files:
        path = private_file_path(row.file_url)
        if not path:
            continue
        shared = frappe.db.exists(
            "File",
            {
                "file_url": row.file_url,
                "name": ["not in", list(file_names)],
            },
        )
        if shared:
            continue
        paths.append(path)
    return list(dict.fromkeys(paths))


def _delete_physical_files(paths):
    for path in paths:
        try:
            if path.exists():
                os.remove(path)
            prune_empty_parent_dirs(path)
        except Exception:
            frappe.log_error(
                title="Document Physical File Cleanup Error",
                message=frappe.get_traceback(),
            )


def permanently_delete_document(document_name):
    get_standalone_document(document_name)
    files = _document_files(document_name)
    file_names = [row.name for row in files]
    cleanup_paths = _physical_cleanup_paths(files)
    if file_names:
        frappe.db.delete("File", {"name": ["in", file_names]})
    frappe.delete_doc(
        "Document",
        document_name,
        ignore_permissions=True,
        force=True,
    )
    if cleanup_paths:
        frappe.db.after_commit.add(lambda: _delete_physical_files(cleanup_paths))


def purge_expired_trash():
    retention_days = int(
        frappe.db.get_single_value(
            "Document Management Settings",
            "trash_retention_days",
        )
        or 0
    )
    if retention_days <= 0:
        return []
    cutoff = add_days(now_datetime(), -retention_days)
    expired = frappe.get_all(
        "Document",
        filters=standalone_filters({
            "is_deleted": 1,
            "deleted_at": ["<=", cutoff],
        }),
        pluck="name",
        limit_page_length=0,
    )
    deleted = []
    for document_name in expired:
        try:
            permanently_delete_document(document_name)
            frappe.db.commit()
            deleted.append(document_name)
        except Exception:
            frappe.db.rollback()
            frappe.log_error(
                title="Document Trash Retention Error",
                message=frappe.get_traceback(),
            )
    return deleted


def purge_expired_exports():
    retention_hours = int(
        frappe.db.get_single_value(
            "Document Management Settings",
            "export_retention_hours",
        )
        or 0
    )
    if retention_hours <= 0:
        return []
    cutoff = add_to_date(now_datetime(), hours=-retention_hours)
    expired = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": EXPORT_ATTACHMENT_DOCTYPE,
            "attached_to_name": EXPORT_ATTACHMENT_NAME,
            "creation": ["<=", cutoff],
        },
        pluck="name",
        limit_page_length=0,
    )
    deleted = []
    for file_name in expired:
        try:
            frappe.delete_doc(
                "File",
                file_name,
                ignore_permissions=True,
                force=True,
            )
            frappe.db.commit()
            deleted.append(file_name)
        except Exception:
            frappe.db.rollback()
            frappe.log_error(
                title="Document Export Retention Error",
                message=frappe.get_traceback(),
            )
    return deleted


def _expiry_reminder_settings(days_ahead=None):
    configured_days = days_ahead
    send_email = False
    try:
        if configured_days is None:
            raw_days = frappe.db.get_single_value(
                "Document Management Settings",
                "expiry_reminder_days",
            )
            configured_days = 30 if raw_days in (None, "") else int(raw_days)
        raw_email = frappe.db.get_single_value(
            "Document Management Settings",
            "send_expiry_email_reminders",
        )
        send_email = bool(int(raw_email or 0))
    except Exception:
        if configured_days is None:
            configured_days = 30
    return int(configured_days or 0), send_email


def notify_expiring_documents(days_ahead=None):
    """Send Notification Log (and optional email) reminders for expiring, due, or overdue documents."""
    from frappe.utils import getdate, today

    reminder_days, send_email = _expiry_reminder_settings(days_ahead=days_ahead)
    if reminder_days <= 0:
        return []

    today_date = getdate(today())
    cutoff_date = getdate(add_days(today_date, reminder_days))
    notified = []

    has_provider = frappe.get_meta("Document").has_field(OWNER_FIELD)
    for fieldname, upcoming, overdue in (
        ("expiry_date", "Document {0} is expiring on {1}", "Document {0} is expired on {1}"),
        ("review_date", "Document {0} is due for review on {1}", "Document {0} is overdue for review on {1}"),
    ):
        fields = ["name", "title", "owner", fieldname]
        if has_provider:
            fields.append(OWNER_FIELD)
        docs = frappe.get_all(
            "Document",
            filters={
                "is_deleted": 0,
                "status": ["!=", "Obsolete"],
                fieldname: ["<=", str(cutoff_date)],
            },
            fields=fields,
            limit_page_length=0,
        )
        for row in docs:
            raw_target = row.get(fieldname)
            if not raw_target:
                continue
            owner = row.get("owner")
            if not owner or owner == "Guest":
                continue
            doc_name = row["name"]
            target_date = getdate(raw_target)
            template = overdue if target_date < today_date else upcoming
            doc_title = row.get("title") or doc_name
            if provider := row.get(OWNER_FIELD):
                handlers = frappe.get_hooks("document_management_reminder_permission", default={}).get(provider, ())
                if len(handlers) != 1:
                    continue
                try:
                    # The provider returns the title its owner can currently read.
                    doc_title = frappe.get_attr(handlers[0])(doc_name, owner)
                    if not doc_title:
                        continue
                    language = frappe.db.get_value("User", owner, "language")
                    template = frappe._(template, lang=language)
                except Exception:
                    frappe.log_error(title="Document Reminder Permission Error", message=frappe.get_traceback())
                    continue
            subject = template.format(doc_title, target_date.isoformat())
            already_sent = frappe.db.exists(
                "Notification Log",
                {
                    "document_type": "Document",
                    "document_name": doc_name,
                    "for_user": owner,
                    "subject": subject,
                },
            )
            if already_sent:
                continue
            try:
                frappe.get_doc(
                    {
                        "doctype": "Notification Log",
                        "subject": subject,
                        "type": "Alert",
                        "document_type": "Document",
                        "document_name": doc_name,
                        "for_user": owner,
                    }
                ).insert(ignore_permissions=True)
                if send_email:
                    try:
                        email = frappe.db.get_value("User", owner, "email") or owner
                        if email and "@" in str(email):
                            frappe.sendmail(
                                recipients=[email],
                                subject=subject,
                                message=subject,
                                reference_doctype="Document",
                                reference_name=doc_name,
                            )
                    except Exception:
                        frappe.log_error(
                            title="Document Expiry Email Reminder Error",
                            message=frappe.get_traceback(),
                        )
                notified.append(doc_name)
            except Exception:
                frappe.log_error(
                    title="Document Expiry Notification Error",
                    message=frappe.get_traceback(),
                )
    if notified:
        frappe.db.commit()
    return notified
