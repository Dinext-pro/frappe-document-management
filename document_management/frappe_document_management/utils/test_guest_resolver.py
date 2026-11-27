from unittest import TestCase
from unittest.mock import patch

import frappe
from werkzeug.wrappers import Request, Response

from document_management.frappe_document_management.utils import document_sharing as sharing


class TestGuestShareResolver(TestCase):
    def setUp(self):
        self.link = frappe._dict(
            name="test-share",
            document="test-document",
            expires_at="2099-01-01",
        )
        self.document = frappe._dict(
            name=self.link.document,
            custom_document_management_provider="test_provider",
        )
        flags = getattr(frappe.local, "flags", None)
        if flags is None:
            frappe.local.flags = frappe._dict(in_test=True)
            self.addCleanup(delattr, frappe.local, "flags")
        else:
            self.enterContext(patch.dict(flags, {"in_test": True}))
        self.enterContext(patch.object(frappe, "_", side_effect=str))
        self.counter = self.enterContext(patch.object(sharing, "_consume_share_view"))
        self.resolver = self.enterContext(patch.object(frappe, "get_attr")).return_value
        self.enterContext(patch.object(sharing, "_get_share_link", return_value=self.link))
        self.enterContext(patch.object(frappe, "get_doc", return_value=self.document))
        self.hooks = self.enterContext(patch.object(
            frappe, "get_hooks", return_value={"test_provider": ["test.resolve"]}
        ))
        self.enterContext(patch.object(frappe, "throw", side_effect=frappe.PermissionError))

    def test_guest_page_uses_only_the_resolver_for_the_documents_provider(self):
        self.resolver.return_value = {
            "title": "Pinned revision", "filename": "Pinned revision.pdf", "kind": "pdf"
        }
        context = sharing.get_shared_document_context("test-token")

        self.resolver.assert_called_once_with(self.link, self.document, None)
        self.assertEqual(context["kind"], "pdf")
        self.assertEqual(context["filename"], "Pinned revision.pdf")
        self.assertFalse(context["show_download_button"])
        self.assertEqual(context["download_url"], "")
        self.assertTrue(context["isolated_pdf"])

    def test_provider_without_exactly_one_resolver_cannot_fall_back_to_standalone(self):
        for hooks in (
            {"other_provider": ["other.resolve"]},
            {"test_provider": ["test.resolve", "other.resolve"]},
        ):
            self.hooks.return_value = hooks
            with self.subTest(hooks=hooks), self.assertRaises(frappe.PermissionError):
                sharing.get_shared_document_context("test-token")
        self.resolver.assert_not_called()

    def test_provider_file_response_is_private_and_rechecks_the_requested_mode(self):
        for mode, disposition, mime_type in (
            (0, "inline", "application/pdf"), (1, "attachment", "application/octet-stream"),
        ):
            self.resolver.return_value = {
                "content": b"data", "filename": "Pinned revision.pdf", "content_type": mime_type,
            }
            with self.subTest(mode=mode):
                response = sharing.download_shared_document("test-token", download=mode)
                self.resolver.assert_called_with(self.link, self.document, bool(mode))
                self.counter.assert_called_with(self.link, token_hash=sharing._token_hash("test-token"))
                self.assertEqual(response.content_type, mime_type)
                self.assertEqual(response.get_data(), b"data")
                self.assertTrue(response.headers["Content-Disposition"].startswith(disposition))
                self.assertEqual(response.headers["Cache-Control"], "private, no-store")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                self.assertIn("sandbox", response.headers["Content-Security-Policy"])

    def test_deleted_parent_is_rejected_before_resolving_the_share(self):
        self.document.is_deleted = 1
        with self.assertRaises(frappe.PermissionError):
            sharing.get_shared_document_context("test-token")
        with self.assertRaises(frappe.PermissionError):
            sharing.download_shared_document("test-token")
        self.resolver.assert_not_called()
        self.counter.assert_not_called()

    def test_portal_response_never_caches_or_sends_the_token_in_a_referrer(self):
        response = Response()
        sharing.set_guest_share_page_headers(
            Request.from_values("/document_share?token=test-token"), response
        )
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
