"""Focused UI-to-service result enrichment contract tests."""

import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import rehome_backup_workflow as workflow
import rehome_review_sources as managed
from tests import test_backup_app as app_fixtures


class ResultUiContractTests(unittest.TestCase):
    def test_enrich_worker_forwards_exact_reviewed_source_pin(self):
        app = app_fixtures.app
        reviewed_pin = "ab" * 32
        service = Mock()
        service.enrich_backup.return_value = {"status": "backup_verified"}

        class Lock:
            def acquire(self):
                return self

            def release(self):
                pass

        class SynchronousThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with tempfile.TemporaryDirectory(prefix="rehome-result-ui-") as directory:
            root = Path(directory)
            ui = app.BackupApp.__new__(app.BackupApp)
            ui.worker = None
            ui.settings = {"local_root": str(root)}
            ui.service = service
            ui.cancel_path = None
            ui.events = queue.Queue()
            ui.lock_factory = lambda _path: Lock()
            ui.lock = None
            ui.status = Mock()
            ui._set_busy = Mock()
            ui.result_source_pin = reviewed_pin
            source_backup = root / "reviewed-source"
            with patch.object(app.threading, "Thread", SynchronousThread):
                ui._start("enrich", source_backup)

        service.enrich_backup.assert_called_once()
        args, kwargs = service.enrich_backup.call_args
        self.assertEqual(ui.settings, args[0])
        self.assertEqual(source_backup, args[1])
        self.assertEqual(reviewed_pin, kwargs["expected_digest"])
        self.assertEqual(("result", {"status": "backup_verified"}), ui.events.get_nowait())
        service.enrich_backup.assert_called_once_with(
            ui.settings, source_backup, service.enrich_backup.call_args.args[2], ui.events,
            expected_digest=reviewed_pin,
        )

    def test_results_use_full_paths_for_same_basename_and_preserve_aliases(self):
        with tempfile.TemporaryDirectory(prefix="rehome-result-selection-") as directory:
            root = Path(directory)
            profile = root / "profile"
            profile.mkdir()
            first = root / "one" / "final.py"
            second = root / "two" / "final.py"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("first result", encoding="utf-8")
            second.write_text("second result", encoding="utf-8")
            alias_a, alias_b = "C:/previous/a.py", "C:/previous/b.py"
            settings = {"finished_results": [
                {"project_id": "p", "path": str(first), "display_name": "final.py",
                 "classification": managed.CLASSIFICATION, "aliases": [alias_a]},
                {"project_id": "p", "path": str(second), "display_name": "final.py",
                 "classification": managed.CLASSIFICATION, "aliases": [alias_b]},
            ]}

            results = workflow._results(settings, profile)

        self.assertEqual(2, len(results))
        self.assertEqual({alias_a, alias_b}, {item["aliases"][0] for item in results.values()})
        self.assertEqual({str(first), str(second)}, {item["path"] for item in results.values()})
        self.assertEqual(2, len({key.split("/")[3] for key in results}))
        self.assertTrue(all(key.startswith("Projects/p/Results/") and key.endswith("/final.py")
                            for key in results))


if __name__ == "__main__":
    unittest.main()
