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
    context.isolated_pdf = shared.get("isolated_pdf", False)
    if context.isolated_pdf:
        context.template = "templates/includes/document_share_isolated.html"
        title = shared["title"]
    else:
        title = shared["document"].title
    context.title = title or shared["filename"]
    context.document_title = context.title
    context.filename = shared["filename"]
    context.kind = shared["kind"]
    context.preview_url = shared["preview_url"]
    context.download_url = shared["download_url"]
    context.show_download_button = shared["show_download_button"]
    context.expires_at = shared["expires_at"] if context.isolated_pdf else shared["link"].expires_at
    return context
