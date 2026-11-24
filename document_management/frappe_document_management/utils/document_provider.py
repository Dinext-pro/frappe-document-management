"""Separate documents owned by an external provider from the standalone catalog."""

from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe import _

OWNER_FIELD = "custom_document_management_provider"
_write_owner = ContextVar("document_management_write_owner", default=None)


def is_standalone(document):
    return not getattr(document, OWNER_FIELD, None)


def standalone_filters(filters=None):
    filters = dict(filters or {})
    if frappe.get_meta("Document").has_field(OWNER_FIELD):
        filters[OWNER_FIELD] = ("is", "not set")
    return filters


def standalone_condition():
    if not frappe.get_meta("Document").has_field(OWNER_FIELD):
        return ""
    return f"COALESCE(`tabDocument`.`{OWNER_FIELD}`, '') = '' AND "


def require_standalone(document):
    if not is_standalone(document):
        frappe.throw(_("Document is unavailable."), frappe.DoesNotExistError)
    return document


def get_standalone_document(name):
    return require_standalone(frappe.get_doc("Document", name))


@contextmanager
def provider_write(provider):
    """Allow an authorized provider to save its own row using the standard ORM."""
    token = _write_owner.set(provider)
    try:
        yield
    finally:
        _write_owner.reset(token)


def uses_standalone_controller(document):
    prior = document.get_doc_before_save()
    previous_owner = getattr(prior, OWNER_FIELD, None)
    owner = getattr(document, OWNER_FIELD, None)
    if not owner and not previous_owner:
        return True
    if owner and owner == _write_owner.get() and (document.is_new() or owner == previous_owner):
        return False
    frappe.throw(_("Document is unavailable."), frappe.PermissionError)
