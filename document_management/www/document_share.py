import frappe

from document_management.frappe_document_management.utils.document_sharing import (
    get_shared_document_context,
)


no_cache = 1


def get_context(context):
    token = frappe.form_dict.get("token")
    shared = get_shared_document_context(token)

    context.no_cache = 1
    context.show_sidebar = False
    context.token = token or ""
    context.requires_passcode = bool(shared.get("requires_passcode"))
    context.passcode_error = bool(shared.get("passcode_error"))
    if context.requires_passcode:
        context.title = frappe._("Protected Document")
        context.document_title = frappe._("Protected Document")
        context.filename = ""
        context.kind = "locked"
        context.preview_url = ""
        context.download_url = ""
        context.show_download_button = False
        context.expires_at = shared["link"].expires_at
        return context

    context.title = shared["document"].title or shared["filename"]
    context.document_title = shared["document"].title or shared["filename"]
    context.filename = shared["filename"]
    context.kind = shared["kind"]
    context.preview_url = shared["preview_url"]
    context.download_url = shared["download_url"]
    context.show_download_button = shared["show_download_button"]
    context.expires_at = shared["link"].expires_at
    return context
