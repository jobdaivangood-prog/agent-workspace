import importlib.util
from pathlib import Path
import queue
import sys
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_app_module():
    # The workflow is supplied by the coordinating implementation; unit tests
    # keep the module import independent of that service and any real profile.
    stub = types.ModuleType("rehome_backup_workflow")
    stub.load_settings = Mock(return_value={"local_root": str(ROOT), "backup_root": str(ROOT)})
    stub.create_backup = Mock()
    stub.restore_backup = Mock()
    stub.rollback = Mock()
    stub.recovery_available = Mock(return_value=False)
    stub.project_choices = Mock(return_value=[])
    stub.infer_project = Mock(return_value=None)
    with patch.dict(sys.modules, {"rehome_backup_workflow": stub}):
        spec = importlib.util.spec_from_file_location("rehome_backup_app_tested", ROOT / "src/rehome_backup_app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


app = load_app_module()


class BackupAppPureTests(unittest.TestCase):
    def test_error_message_uses_only_safe_contract_fields(self):
        self.assertEqual(app.safe_error_text({"code": "WAIT_TIMEOUT", "message": "Ожидание завершено"}),
                         "Ожидание завершено (WAIT_TIMEOUT)")
        self.assertEqual(app.safe_error_text({"message": "", "secret": "must not leak"}), app.FALLBACK_ERROR)
        self.assertEqual(app.safe_error_text(RuntimeError("secret")), app.FALLBACK_ERROR)

    def test_result_text_is_concise_and_handles_unknown_result(self):
        self.assertEqual(app.BackupApp._result_text({"status": "success"}), "Операция завершена.")
        self.assertEqual(app.BackupApp._result_text({"status": "partial"}),
                         "Операция завершена; проверьте итоговый статус в отчёте.")
        self.assertEqual(app.BackupApp._result_text({"message": " Готово "}), "Готово")
        self.assertEqual(app.BackupApp._result_text(None), "Операция завершена.")

    def test_settings_argument_requires_absolute_path_before_service_call(self):
        with patch.object(app.workflow, "load_settings") as load, patch.object(app.tk, "Tk"):
            with self.assertRaises(SystemExit):
                app.main(["--settings", "relative.json"])
            load.assert_not_called()

    def test_absolute_settings_path_is_passed_to_workflow(self):
        root = Mock()
        root.mainloop = Mock()
        with patch.object(app.workflow, "load_settings", return_value={"local_root": str(ROOT), "backup_root": str(ROOT)}) as load, \
             patch.object(app.tk, "Tk", return_value=root), patch.object(app, "BackupApp") as ui:
            app.main(["--settings", str(ROOT / "settings.json")])
        load.assert_called_once_with(ROOT / "settings.json")
        ui.assert_called_once_with(root, {"local_root": str(ROOT), "backup_root": str(ROOT)})

    def test_duplicate_start_does_not_acquire_another_lock(self):
        ui = app.BackupApp.__new__(app.BackupApp)
        ui.worker = Mock()
        ui.worker.is_alive.return_value = True
        ui.lock_factory = Mock()
        ui._start("backup")
        ui.lock_factory.assert_not_called()

    def test_cancel_and_close_create_sentinel_and_wait_for_worker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ui = app.BackupApp.__new__(app.BackupApp)
            ui.worker = Mock()
            ui.worker.is_alive.return_value = True
            ui.cancel_path = Path(directory) / ".rehome-cancel-unique"
            ui.status = Mock()
            ui.root = Mock()
            ui.close_requested = False
            ui.close()
            self.assertEqual(ui.cancel_path.read_text(encoding="utf-8"), "cancel\n")
            self.assertTrue(ui.close_requested)
            ui.root.destroy.assert_not_called()
            ui.cancel()
            self.assertEqual(ui.cancel_path.read_text(encoding="utf-8"), "cancel\n")

    def test_recovery_button_is_shown_after_error_when_service_reports_recovery(self):
        ui = app.BackupApp.__new__(app.BackupApp)
        ui.service = Mock()
        ui.service.recovery_available.return_value = True
        ui.settings = {}
        ui.events = queue.Queue()
        ui.events.put(("error", {"code": "failed", "message": "Не выполнено"}))
        ui.status = Mock()
        ui.recovery_button = Mock()
        ui.cancel_button = Mock()
        ui.root = Mock()
        ui.worker = None
        ui._poll()
        ui.recovery_button.pack.assert_called_once()

    def test_explicit_finished_files_get_unique_inferred_project_binding(self):
        ui = app.BackupApp.__new__(app.BackupApp)
        ui.settings = {"finished_results": []}
        ui.service = Mock()
        ui.service.project_choices.return_value = [{"id": "p1", "name": "Проект", "roots": []}]
        ui.service.infer_project.return_value = "p1"
        ui.status = Mock()
        ui.finished_count = Mock()
        with patch.object(app.filedialog, "askopenfilenames", return_value=(r"C:\results\final.ifc",)):
            ui.choose_finished_files()
        self.assertEqual(ui.settings["finished_results"], [{
            "project_id": "p1", "path": r"C:\results\final.ifc", "display_name": "final.ifc",
            "classification": "selected_finished_generated",
        }])
        ui.finished_count.set.assert_called_once_with("Выбрано готовых файлов: 1")


if __name__ == "__main__":
    unittest.main()
