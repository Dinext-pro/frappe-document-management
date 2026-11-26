DOCUMENT_CHANGE_EVENT = "document_management_document_changed"


def publish_document_change(doc=None, document_name=None, deleted=False, user=None):
    import frappe

    name = document_name or getattr(doc, "name", None)
    if not name:
        return

    target_user = user
    if target_user is None and doc is not None and getattr(doc, "only_me", 0):
        target_user = getattr(doc, "owner", None)

    kwargs = {"after_commit": True}
    if target_user:
        kwargs["user"] = target_user

    frappe.publish_realtime(
        DOCUMENT_CHANGE_EVENT,
        {
            "doctype": "Document",
            "name": name,
            "deleted": bool(deleted),
        },
        **kwargs,
    )
