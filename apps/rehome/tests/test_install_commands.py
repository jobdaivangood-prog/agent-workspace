"""Conservative installer tests for the managed ReHome session-command block."""

from pathlib import Path
import tempfile
import unittest

import rehome_install_commands as installer


class InstallCommandsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rehome-install-commands-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.codex.mkdir()
        self.data = self.root / "distribution"
        self.data.mkdir()
        self.launcher = self.data / "Start-ReHome.ps1"
        self.launcher.write_text("# synthetic launcher", encoding="utf-8")
        self.rules = self.codex / "AGENTS.md"

    def test_replaces_only_marked_block_and_backs_up_exact_original(self):
        prefix = b"# Local rules\r\nKeep this text and its line endings.\r\n\r\n"
        old_block = (installer.OLD_START + "\r\nOld route text\r\n" + installer.OLD_END).encode("utf-8")
        suffix = b"\r\n\r\n# More rules\r\nDo not alter this text.\r\n"
        original = prefix + old_block + suffix
        self.rules.write_bytes(original)

        first = installer.install(self.codex, self.launcher, self.data)
        self.assertEqual(first["status"], "installed")
        self.assertTrue(first["restart_or_reload_instructions_required"])
        self.assertFalse(first["workspace_database_changed"])
        self.assertEqual(Path(first["previous_rules"]).read_bytes(), original)
        updated = self.rules.read_bytes()
        self.assertIn(prefix, updated)
        self.assertIn(suffix.rstrip(), updated)
        self.assertNotIn(old_block, updated)
        self.assertIn(installer.START.encode(), updated)
        self.assertIn(installer.END.encode(), updated)

        backup_dir = self.data / "AgentRulesBackups"
        backups_before_repeat = sorted(backup_dir.iterdir())
        repeated = installer.install(self.codex, self.launcher, self.data)
        self.assertIsNone(repeated["previous_rules"])
        self.assertEqual(self.rules.read_bytes(), updated)
        self.assertEqual(sorted(backup_dir.iterdir()), backups_before_repeat)

    def test_malformed_markers_reject_before_any_write(self):
        original = ("# Rules\n" + installer.START + "\nunterminated managed section\n").encode("utf-8")
        self.rules.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            installer.install(self.codex, self.launcher, self.data)
        self.assertEqual(self.rules.read_bytes(), original)
        self.assertFalse((self.data / "AgentRulesBackups").exists())
        self.assertFalse((self.codex / "rehome-session-location.json").exists())

        duplicate = (installer.START + "\nfirst\n" + installer.END + "\n" +
                     installer.START + "\nsecond\n" + installer.END + "\n").encode("utf-8")
        self.rules.write_bytes(duplicate)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            installer.install(self.codex, self.launcher, self.data)
        self.assertEqual(self.rules.read_bytes(), duplicate)
        self.assertFalse((self.data / "AgentRulesBackups").exists())


if __name__ == "__main__":
    unittest.main()
