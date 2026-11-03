import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import environment_adapter as adapter


ENGINE = Path(__file__).resolve().parents[1] / "src" / "vendor" / "codex_backup" / "backup.py"


@unittest.skipUnless(ENGINE.is_file(), "Explicit existing backup engine is unavailable")
class EnvironmentAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="environment-adapter-")
        self.root = Path(self.temp.name)
        self.home = self.root / "synthetic-user"
        self.profile = self.home / "codex-profile"
        self.profile.mkdir(parents=True)
        (self.profile / adapter.MARKER).write_text(json.dumps({"kind": "synthetic-only", "schema_version": 1}))
        self.skill = self.profile / "skills" / "complete-skill"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: complete-skill\ndescription: Synthetic workflow\n---\nHome: " + self.home.as_posix(), encoding="utf-8")
        for folder, name, content in (("scripts", "run.py", "print('synthetic')\n"), ("assets", "sample.txt", "synthetic asset"), ("references", "help.md", "synthetic reference")):
            (self.skill / folder).mkdir()
            (self.skill / folder / name).write_text(content, encoding="utf-8")
        (self.skill / "paths.json").write_text(json.dumps({"home": str(self.home), "profile": str(self.profile)}), encoding="utf-8")
        (self.profile / "AGENTS.md").write_text("Synthetic agent instructions", encoding="utf-8")
        (self.profile / "agents").mkdir()
        (self.profile / "agents" / "reviewer.toml").write_text('model = "synthetic-model"\n', encoding="utf-8")
        plugin = self.profile / "plugins" / "cache" / "synthetic" / "demo" / "1.2.3" / ".codex-plugin"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text(json.dumps({"name": "demo-plugin", "version": "1.2.3"}), encoding="utf-8")
        self.stage = self.root / "environment-stage"

    def tearDown(self):
        self.temp.cleanup()

    def export(self):
        return adapter.export_environment(self.home, self.profile, self.stage, engine_path=ENGINE)

    def test_existing_engine_is_reused_without_running_backup_or_vault(self):
        engine = adapter.load_existing_engine(ENGINE)
        self.assertTrue(callable(engine.sources))
        self.assertTrue(callable(engine.sensitive))
        with patch.object(adapter, "load_existing_engine", return_value=engine), \
             patch.object(engine, "create_backup", side_effect=AssertionError("must not run")), \
             patch.object(engine, "prune_backups", side_effect=AssertionError("must not run")), \
             patch.object(engine, "encrypt_vault", side_effect=AssertionError("must not run")):
            inventory = self.export()
        self.assertEqual(hashlib.sha256(ENGINE.read_bytes()).hexdigest(), inventory["engine_sha256"])
        self.assertFalse(list(self.root.rglob("*.cbs")))
        self.assertFalse(list(self.root.rglob("*.zip")))

    def test_complete_skill_resources_and_agent_definitions_survive(self):
        inventory = self.export()
        skill = next(item for item in inventory["skills"] if item["name"] == "complete-skill")
        self.assertEqual(5, skill["file_count"])
        self.assertEqual(["assets", "references", "scripts"], skill["resource_dirs"])
        self.assertEqual("copied", skill["status"])
        restored = adapter.restore_environment(self.stage, self.root / "restored")
        codex = Path(restored["codex_home"])
        self.assertTrue((codex / "skills/complete-skill/scripts/run.py").is_file())
        self.assertTrue((codex / "AGENTS.md").is_file())
        self.assertTrue((codex / "agents/reviewer.toml").is_file())
        self.assertEqual("Synthetic agent instructions", (codex / "AGENTS.md").read_text())
        self.assertFalse((codex / "config.toml").exists())

    def test_legacy_windows_restore_uses_short_temporary_name(self):
        import rehome_windows_paths as paths
        self.export()
        parent=self.root/('p'*(150-len(str(self.root))-1))
        parent.mkdir()
        destination=parent/'EnvironmentMaterials'
        with patch.object(paths,'legacy_limits_required',return_value=True):
            restored=adapter.restore_environment(self.stage,destination)
        self.assertEqual('prepared',restored['status'])
        self.assertEqual("print('synthetic')\n",
            (Path(restored['codex_home'])/'skills/complete-skill/scripts/run.py').read_text(encoding='utf-8'))

    def test_known_absolute_roots_are_portable_and_rebound(self):
        self.export()
        payload = self.stage / "payload/user-skills/complete-skill/paths.json"
        self.assertNotIn(str(self.home), payload.read_text(encoding="utf-8"))
        restored = adapter.restore_environment(self.stage, self.root / "restored")
        paths = json.loads((Path(restored["codex_home"]) / "skills/complete-skill/paths.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["home"], paths["home"])
        self.assertEqual(restored["codex_home"], paths["profile"])

    def test_root_tokenization_requires_a_complete_path_boundary(self):
        home = Path("C:/Users/A")
        codex = home / "codex-profile"
        original = json.dumps({"exact": str(home), "child": str(home / "files" / "a.txt"),
                               "other_user": str(Path("C:/Users/Alice") / "files" / "a.txt")})
        data, tokens = adapter._tokenize_roots(original.encode(), home, codex)
        text = data.decode()
        self.assertTrue(tokens)
        self.assertIn("${HOME_", text)
        self.assertNotIn("${HOME_JSON}lice", text)
        self.assertNotIn("${HOME_NATIVE}lice", text)
        self.assertNotIn("${HOME_POSIX}lice", text)
        self.assertIn("Alice", text)
        unresolved_data, refs = adapter._tokenize_unknown_paths(data, "user-skills", "example.json")
        self.assertTrue(refs)
        self.assertIn("${LOCAL_BINDING_", unresolved_data.decode())
        self.assertNotIn("Alice", unresolved_data.decode())
        forward = b"C:/Users/A/file C:/Users/Alice/file C:/Users/A-old/file"
        rewritten, _ = adapter._tokenize_roots(forward, home, codex)
        self.assertIn(b"C:/Users/Alice/file", rewritten)
        self.assertIn(b"C:/Users/A-old/file", rewritten)
        self.assertIn(b"${HOME_POSIX}/file", rewritten)

    def test_unknown_resource_paths_require_local_binding_and_binaries_stay_exact(self):
        unknown = self.skill / "scripts" / "external.py"
        unknown.write_text('TOOL = "Z:/unavailable/tools/helper.exe"\n', encoding="utf-8")
        binary = b"\x00Z:/unavailable/tools/helper.exe\xff"
        (self.skill / "assets" / "binary.dat").write_bytes(binary)
        inventory = self.export()
        output = self.stage / "payload/user-skills/complete-skill/scripts/external.py"
        self.assertNotIn("Z:/unavailable", output.read_text())
        self.assertIn("${LOCAL_BINDING_", output.read_text())
        self.assertEqual(binary, (self.stage / "payload/user-skills/complete-skill/assets/binary.dat").read_bytes())
        self.assertTrue(any(item["reason"] == "unresolved_resource_paths" for item in inventory["dependencies"]))
        skill = next(item for item in inventory["skills"] if item["name"] == "complete-skill")
        self.assertEqual("requires_local_setup", skill["status"])

    def test_config_opaque_values_urls_and_secrets_are_references(self):
        config = '''model = "synthetic-model"
model_reasoning_effort = "high"
[mcp_servers.synthetic]
command = "C:/private/tools/server.exe"
args = ["SYNTHETIC_OPAQUE_VALUE", "C:/private/argument.json"]
url = "https://user:SYNTHETIC_PASSWORD@host.invalid/private-token?key=SYNTHETIC_QUERY"
[mcp_servers.synthetic.env]
API_KEY = "SYNTHETIC_API_KEY_SENTINEL"
[mcp_servers.synthetic.http_headers]
Authorization = "SYNTHETIC_AUTHORIZATION_SENTINEL"
[agents.reviewer]
config_file = "agents/reviewer.toml"
[plugins."demo-plugin@synthetic"]
enabled = true
'''
        (self.profile / "config.toml").write_text(config, encoding="utf-8")
        inventory = self.export()
        exported = (self.stage / adapter.MANIFEST).read_text(encoding="utf-8")
        for value in ("SYNTHETIC_OPAQUE_VALUE", "SYNTHETIC_PASSWORD", "SYNTHETIC_QUERY", "SYNTHETIC_API_KEY_SENTINEL", "SYNTHETIC_AUTHORIZATION_SENTINEL", "C:/private", "host.invalid"):
            self.assertNotIn(value, exported)
        self.assertEqual("high", inventory["config"]["selected"]["model_reasoning_effort"])
        self.assertTrue(inventory["config"]["secret_refs"])
        self.assertTrue(inventory["config"]["local_bindings"])
        self.assertFalse(inventory["config"]["authorization_exported"])
        self.assertFalse(any(item["relative_path"] == "config.toml" for item in inventory["files"]))

    def test_sensitive_resources_and_credential_urls_are_excluded(self):
        (self.skill / ".env").write_text("SYNTHETIC_ONLY_NOT_REAL", encoding="utf-8")
        (self.skill / "secret.txt").write_text('password = "SYNTHETIC_SECRET_SENTINEL"', encoding="utf-8")
        (self.skill / "endpoint.txt").write_text("https://host.invalid/opaque?value=SYNTHETIC_CREDENTIAL", encoding="utf-8")
        inventory = self.export()
        for file in self.stage.rglob("*"):
            if file.is_file():
                data = file.read_bytes()
                self.assertNotIn(b"SYNTHETIC_SECRET_SENTINEL", data)
                self.assertNotIn(b"SYNTHETIC_CREDENTIAL", data)
                self.assertNotIn(b"SYNTHETIC_ONLY_NOT_REAL", data)
        self.assertEqual(3, sum(item["reason"] == "sensitive_content_omitted" for item in inventory["excluded"]))
        self.assertEqual("requires_local_setup", inventory["skills"][0]["status"])

    def test_plugin_cache_is_reference_only_and_missing_dependencies_are_explicit(self):
        (self.skill / ".venv").mkdir()
        (self.skill / ".venv" / "runtime.txt").write_text("not copied")
        (self.profile / "config.toml").write_text('[agents.missing]\nconfig_file="missing.toml"\n', encoding="utf-8")
        inventory = self.export()
        plugin = next(item for item in inventory["plugins"] if item["id"] == "demo-plugin")
        self.assertEqual("1.2.3", plugin["version"])
        self.assertEqual("reference-only", plugin["restore_mode"])
        self.assertTrue(plugin["install_required"])
        self.assertTrue(any(item["reason"] == "dependency_excluded" for item in inventory["dependencies"]))
        self.assertTrue(any(item["reason"] == "agent_definition_missing" for item in inventory["dependencies"]))
        report = adapter.restore_environment(self.stage, self.root / "restored")
        self.assertFalse(report["plugin_installation_verified"])
        self.assertTrue(list((Path(report["destination"]) / "references").rglob("plugin.json")))

    def test_plugin_and_project_reference_skills_are_not_counted_as_copy_ready(self):
        plugin_skill = self.profile / "plugins/cache/synthetic/demo/1.2.3/skills/plugin-skill"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text("---\nname: plugin-skill\n---\nSynthetic plugin workflow", encoding="utf-8")
        project = self.home / "synthetic-project"
        project_skill = project / ".agents/skills/project-skill"
        project_skill.mkdir(parents=True)
        (project_skill / "SKILL.md").write_text("---\nname: project-skill\n---\nSynthetic project workflow", encoding="utf-8")
        (project / "raw-project-source.txt").write_text("MUST_NOT_COLLECT", encoding="utf-8")
        (self.profile / "config.toml").write_text(f'[projects."{project.as_posix()}"]\ntrust_level="trusted"\n', encoding="utf-8")
        inventory = self.export()
        by_name = {item["name"]: item for item in inventory["skills"]}
        for name in ("plugin-skill", "project-skill"):
            self.assertEqual("reference-only", by_name[name]["restore_mode"])
            self.assertEqual("reference-only", by_name[name]["status"])
            self.assertFalse(by_name[name]["discoverable_after_copy"])
        self.assertEqual({"discovered": 3, "copy_ready": 1, "requires_local_setup": 0, "reference_only": 2}, inventory["skill_counts"])
        restored = adapter.restore_environment(self.stage, self.root / "restored")
        self.assertEqual(1, restored["skills"])
        self.assertEqual(3, restored["skills_discovered"])
        self.assertEqual(2, restored["skills_reference_only"])
        self.assertFalse((Path(restored["codex_home"]) / "skills/plugin-skill").exists())
        self.assertFalse((Path(restored["home"]) / ".agents/skills/project-skill").exists())
        self.assertTrue(list((Path(restored["destination"]) / "references").rglob("SKILL.md")))
        self.assertFalse(any(item["relative_path"] == "raw-project-source.txt" for item in inventory["files"]))

    def test_incomplete_payload_and_existing_target_are_rejected(self):
        inventory = self.export()
        target = self.root / "restored"
        target.mkdir()
        with self.assertRaises(adapter.EnvironmentError):
            adapter.restore_environment(self.stage, target)
        payload = self.stage / inventory["files"][0]["path"]
        payload.write_bytes(b"corrupt")
        with self.assertRaises(adapter.EnvironmentError):
            adapter.restore_environment(self.stage, self.root / "other-target")
        self.assertFalse((self.root / "other-target").exists())

    def test_reference_traversal_is_rejected_before_restore(self):
        self.export()
        manifest = self.stage / adapter.MANIFEST
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["files"][0]["relative_path"] = "../escape.txt"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            adapter.restore_environment(self.stage, self.root / "restored")
        self.assertFalse((self.root / "escape.txt").exists())

    def test_forged_targets_cannot_write_active_configuration_or_authorization(self):
        self.export()
        manifest = self.stage / adapter.MANIFEST
        original = manifest.read_text(encoding="utf-8")
        for index, target in enumerate(("codex/config.toml", "codex/auth.json", "home/.codex/config.toml", "CoDeX/AUTH.JSON", "home/unexpected/file.txt")):
            with self.subTest(target=target):
                forged = json.loads(original)
                forged["files"][0]["target"] = target
                manifest.write_text(json.dumps(forged), encoding="utf-8")
                destination = self.root / f"rejected-{index}"
                with self.assertRaises(adapter.EnvironmentError):
                    adapter.restore_environment(self.stage, destination)
                self.assertFalse(destination.exists())
                self.assertFalse(list(self.root.glob(".environment-restore-*")))

    def test_reference_component_cannot_be_promoted_to_installed_files(self):
        self.export()
        manifest = self.stage / adapter.MANIFEST
        forged = json.loads(manifest.read_text(encoding="utf-8"))
        entry = next(item for item in forged["files"] if item["component"] == "plugin-packages")
        entry["target"] = "codex/skills/injected/SKILL.md"
        manifest.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaises(adapter.EnvironmentError):
            adapter.restore_environment(self.stage, self.root / "rejected")
        self.assertFalse(list(self.root.glob(".environment-restore-*")))

    def test_secret_basename_and_output_collisions_are_rejected_before_stage(self):
        self.export()
        manifest = self.stage / adapter.MANIFEST
        original = manifest.read_text(encoding="utf-8")
        forged = json.loads(original)
        entry = next(item for item in forged["files"] if item["component"] == "user-skills")
        entry.update(relative_path="AUTH.JSON", path="payload/user-skills/AUTH.JSON", target="codex/skills/AUTH.JSON")
        manifest.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaises(adapter.EnvironmentError):
            adapter.restore_environment(self.stage, self.root / "secret-rejected")
        self.assertFalse(list(self.root.glob(".environment-restore-*")))
        forged = json.loads(original)
        forged["files"].append(dict(forged["files"][0]))
        manifest.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaises(adapter.EnvironmentError):
            adapter.restore_environment(self.stage, self.root / "duplicate-rejected")
        self.assertFalse(list(self.root.glob(".environment-restore-*")))
        forged = json.loads(original)
        entries = [item for item in forged["files"] if item["component"] == "user-skills"][:2]
        for entry, relative in zip(entries, ("complete-skill/prefix", "complete-skill/prefix/child.txt")):
            entry.update(relative_path=relative, path="payload/user-skills/" + relative, target="codex/skills/" + relative)
        manifest.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            adapter.restore_environment(self.stage, self.root / "prefix-rejected")
        self.assertIn("both file and directory", str(raised.exception))
        self.assertFalse(list(self.root.glob(".environment-restore-*")))

    def test_real_profiles_and_unmarked_sources_are_rejected(self):
        (self.profile / adapter.MARKER).unlink()
        with self.assertRaises(adapter.EnvironmentError):
            self.export()
        self.assertFalse(self.stage.exists())


if __name__ == "__main__":
    unittest.main()
