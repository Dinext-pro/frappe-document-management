import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from document_management.frappe_document_management.utils import folder_ingestion as ingestion


class TestIngestionFiles(TestCase):
    def test_candidates_include_only_regular_files_in_real_category_directories(self):
        with TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            category = inbox / "Legal"
            category.mkdir(parents=True)
            (category / "notice.txt").write_text("ordinary document")
            (inbox / "shortcut.txt").symlink_to(category / "notice.txt")
            (inbox / "shortcut-category").symlink_to(category, target_is_directory=True)
            os.mkfifo(inbox / "queue.txt")
            files = ingestion._candidate_files(inbox, inbox / "done", inbox / "error", 50,
                                               map_subfolders_to_category=True)
            self.assertEqual(files, [(category / "notice.txt", "Legal")])

    def test_new_input_at_the_same_name_survives_archiving_the_open_file(self):
        from document_management.frappe_document_management.utils import folder_files as files

        with TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            source = inbox / "notice.txt"
            source.write_text("first")
            with files.claimed_file(inbox, source) as item:
                source.write_text("next")
                self.assertEqual(item.stream.read(), b"first")
                output, _ = item.publish(inbox / "done")
                item.discard()
            self.assertEqual(output.read_text(), "first")
            self.assertEqual(source.read_text(), "next")

    def test_interrupted_claim_can_resume_and_destination_directory_must_be_real(self):
        from document_management.frappe_document_management.utils import folder_files as files

        with TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            source = inbox / "notice.txt"
            source.write_text("resume")
            unrelated = Path(tmp) / "other"
            unrelated.mkdir()
            (inbox / "done").symlink_to(unrelated, target_is_directory=True)
            with files.claimed_file(inbox, source) as item, self.assertRaises(OSError):
                item.publish(inbox / "done")
            self.assertEqual(list(unrelated.iterdir()), [])
            self.assertEqual(ingestion._candidate_files(inbox, inbox / "done", inbox / "error", 50),
                             [(source, None)])
            with files.claimed_file(inbox, source) as item:
                output, _ = item.publish(inbox / "safe")
                item.discard()
            self.assertEqual(output.read_text(), "resume")

    def test_direct_read_obeys_the_configured_root_and_excludes_completed_outputs(self):
        with TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "input"
            done = inbox / "done"
            done.mkdir(parents=True)
            outside = Path(tmp) / "other.txt"
            outside.write_text("separate fixture")
            completed = done / "completed.txt"
            completed.write_text("finished")
            config = {"source_path": inbox, "done_path": done, "error_path": inbox / "error",
                      "map_subfolders_to_category": True}
            with patch.object(ingestion, "_settings", return_value=config):
                for path in (outside, completed):
                    with self.subTest(path=path), self.assertRaises(ValueError), ingestion.input_stream(path):
                        self.fail("Unexpected input stream")

    def test_publication_preserves_existing_output_names_and_rollback_preserves_the_next_entry(self):
        from document_management.frappe_document_management.utils import folder_files as files

        with TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "input"
            done = Path(tmp) / "done"
            inbox.mkdir()
            done.mkdir()
            source = inbox / "notice.txt"
            source.write_text("document")
            alias_target = done / "other.txt"
            alias_target.write_text("separate fixture")
            (done / source.name).symlink_to(alias_target)
            with files.claimed_file(inbox, source) as item:
                output, identity = item.publish(done)
                self.assertEqual(output.name, "notice-1.txt")
                output.rename(done / "archived.txt")
                output.write_text("next completed document")
                with self.assertRaises(OSError):
                    files.retract(output, identity)
                self.assertEqual(output.read_text(), "next completed document")
                self.assertEqual(alias_target.read_text(), "separate fixture")
                item.discard()
