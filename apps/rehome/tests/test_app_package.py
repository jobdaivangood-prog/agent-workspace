import copy
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import rehome_app_package as app_package
import rehome_backup_module as backup
import rehome_backup_workflow as workflow
import rehome_native_staging as staging
import snapshot


class AppPackageConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rehome-app-package-")
        self.root = Path(self.temp.name)
        self.data_root = self.root / "settings"
        self.data_root.mkdir()
        self.home = self.root / "home"
        self.profile = self.home / ".codex"
        self.profile.mkdir(parents=True)
        self.old_program = self.make_program(self.root / "Restore-02" / "Program", b"old")
        self.new_program = self.make_program(self.root / "Restore-03" / "Program", b"new")
        identity = hashlib.sha256(str(Path.home()).casefold().encode()).hexdigest()[:12]
        self.settings_path = self.data_root / ("settings-" + socket.gethostname() + "-" + identity + ".json")
        old_manifest = snapshot._read_json(self.old_program / "manifest.json")
        old_pins = {entry["path"].split("/", 1)[1]: entry["sha256"]
                    for entry in old_manifest["files"] if entry["path"].startswith("Schema/")}
        self.original = {
            "format": "rehome-two-action-settings/1",
            "source_home": str(self.home),
            "source_profile": str(self.profile),
            "local_root": str(self.root / "local"),
            "backup_root": str(self.root / "backups"),
            "projects_root": str(self.root / "projects"),
            "runtime_executable": str(self.root / "missing-runtime" / "codex.exe"),
            "runtime_sha256": "a" * 64,
            "runtime_version": backup.SUPPORTED_RUNTIME,
            "schema_template": str(self.old_program / "Schema"),
            "template_pins": old_pins,
            "decoder": {"path": str(self.old_program / "Dependencies" / "libzstd.dll"),
                        "sha256": next(e["sha256"] for e in old_manifest["files"]
                                       if e["path"] == "Dependencies/libzstd.dll")},
            "finished_results": [{"project_id": "project-choice", "path": str(self.root / "kept-result.txt"),
                                  "display_name": "Results/kept.txt", "classification": "finished"}],
            "manual_choice": {"restore_mode": "review-first", "label": "keep-me"},
        }
        snapshot._write_json(self.settings_path, self.original)

    def tearDown(self):
        self.temp.cleanup()

    def make_program(self, program, prefix):
        source = self.root / (program.parent.name + "-source")
        (source / "Schema").mkdir(parents=True)
        (source / "Dependencies").mkdir()
        files = {}
        for name in staging.DATABASES:
            path = source / "Schema" / name
            path.write_bytes(prefix + b"-" + name.encode("ascii"))
            files["Schema/" + name] = path
        decoder = source / "Dependencies" / "libzstd.dll"
        decoder.write_bytes(prefix + b"-decoder")
        files["Dependencies/libzstd.dll"] = decoder
        program.parent.mkdir(parents=True, exist_ok=True)
        snapshot.create_snapshot(program, version=1, device_id="test-package", results=files)
        return program

    def read_settings(self):
        return snapshot._read_json(self.settings_path)

    def test_relocation_updates_only_package_bound_fields_and_manifest_pins(self):
        expected = copy.deepcopy(self.original)
        self.old_program.rename(self.root / "Restore-02" / "Program-moved")
        relocated = self.root / "Restore-03" / "Program"

        result = app_package.configure(relocated, self.data_root)
        actual = self.read_settings()
        current_manifest = snapshot._read_json(relocated / "manifest.json")
        expected["schema_template"] = str(relocated / "Schema")
        expected["template_pins"] = {
            entry["path"].split("/", 1)[1]: entry["sha256"]
            for entry in current_manifest["files"] if entry["path"].startswith("Schema/")
        }
        expected["decoder"] = {
            "path": str(relocated / "Dependencies" / "libzstd.dll"),
            "sha256": next(entry["sha256"] for entry in current_manifest["files"]
                           if entry["path"] == "Dependencies/libzstd.dll"),
        }
        self.assertEqual(self.settings_path, result)
        self.assertEqual(expected, actual)
        self.assertEqual(self.original["runtime_executable"], actual["runtime_executable"])
        self.assertEqual(self.original["runtime_sha256"], actual["runtime_sha256"])
        self.assertEqual(self.original["runtime_version"], actual["runtime_version"])

    def test_missing_package_resources_and_runtime_leave_settings_loadable(self):
        (self.new_program / "Schema" / staging.DATABASES[0]).unlink()
        (self.new_program / "Dependencies" / "libzstd.dll").unlink()
        before = self.settings_path.read_bytes()

        app_package.configure(self.new_program, self.data_root)
        loaded = workflow.load_settings(self.settings_path)

        self.assertEqual(self.original, loaded)
        self.assertEqual(before, self.settings_path.read_bytes())
        self.assertFalse(Path(loaded["runtime_executable"]).exists())

    def test_repeated_relocation_is_idempotent_without_rewriting_settings(self):
        app_package.configure(self.new_program, self.data_root)
        first_bytes = self.settings_path.read_bytes()
        first_mtime = self.settings_path.stat().st_mtime_ns

        app_package.configure(self.new_program, self.data_root)

        self.assertEqual(first_bytes, self.settings_path.read_bytes())
        self.assertEqual(first_mtime, self.settings_path.stat().st_mtime_ns)
        self.assertFalse(list(self.data_root.glob("*.tmp")))

    def test_fresh_configuration_keeps_recovery_available_without_action_files(self):
        for name in staging.DATABASES:
            (self.new_program/'Schema'/name).unlink()
        (self.new_program/'Dependencies/libzstd.dll').unlink()
        with patch.object(app_package.Path,'home',return_value=self.home):
            path=app_package.configure(self.new_program,self.data_root)
        settings=workflow.load_settings(path)
        self.assertEqual(str(self.home/'ReHome'),settings['local_root'])
        self.assertEqual(str(self.profile),settings['source_profile'])
        self.assertEqual(str(self.new_program/'Schema'),settings['schema_template'])
        self.assertFalse(Path(settings['decoder']['path']).exists())
        # Availability detects the profile-bound pointer; journal validation is
        # independently exercised by the real transaction/recovery tests.
        pointer=workflow._recovery_path(settings);pointer.parent.mkdir(parents=True)
        pointer.write_text('{}',encoding='utf-8')
        self.assertTrue(workflow.recovery_available(settings))
        with self.assertRaises(ValueError):
            staging._template(settings['schema_template'],settings['template_pins'])

    def test_malformed_settings_fail_closed_without_replacement(self):
        self.settings_path.write_text('{"format":', encoding="utf-8")
        before = self.settings_path.read_bytes()

        with self.assertRaises(snapshot.SnapshotError):
            app_package.configure(self.new_program, self.data_root)

        self.assertEqual(before, self.settings_path.read_bytes())

    def test_fresh_configuration_finds_recovery_with_corrupt_action_files(self):
        for name in staging.DATABASES:
            (self.new_program/'Schema'/name).write_bytes(b'corrupt database')
        (self.new_program/'Dependencies/libzstd.dll').write_bytes(b'corrupt decoder')
        with patch.object(app_package.Path,'home',return_value=self.home):
            path=app_package.configure(self.new_program,self.data_root)
        settings=workflow.load_settings(path)
        pointer=workflow._recovery_path(settings);pointer.parent.mkdir(parents=True)
        pointer.write_text('{}',encoding='utf-8')
        self.assertTrue(workflow.recovery_available(settings))
        with self.assertRaises(ValueError):
            staging._template(settings['schema_template'],settings['template_pins'])

    def test_corrupt_action_resource_preserves_existing_settings_for_recovery(self):
        decoder = self.new_program / "Dependencies" / "libzstd.dll"
        decoder.write_bytes(b"rewritten decoder")
        before = self.settings_path.read_bytes()

        app_package.configure(self.new_program, self.data_root)
        loaded = workflow.load_settings(self.settings_path)

        self.assertEqual(before, self.settings_path.read_bytes())
        self.assertEqual(self.original, loaded)

    def test_recovery_settings_without_old_template_pins_or_decoder_remain_loadable(self):
        settings = copy.deepcopy(self.original)
        settings.pop("template_pins")
        settings.pop("decoder")
        self.settings_path.write_text(json.dumps(settings),encoding='utf-8')
        (self.new_program / "Schema" / staging.DATABASES[0]).write_bytes(b"corrupt template")
        (self.new_program / "Dependencies" / "libzstd.dll").write_bytes(b"corrupt decoder")
        before = self.settings_path.read_bytes()

        app_package.configure(self.new_program, self.data_root)
        loaded = workflow.load_settings(self.settings_path)

        self.assertEqual(before, self.settings_path.read_bytes())
        self.assertNotIn("template_pins", loaded)
        self.assertNotIn("decoder", loaded)
        self.assertEqual(settings, loaded)


if __name__ == "__main__":
    unittest.main()
