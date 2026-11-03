import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import environment_adapter as adapter
import environment_archive as archive


class EnvironmentArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'user'
        self.profile = self.home / 'codex-profile'
        self.profile.mkdir(parents=True)
        (self.profile / adapter.MARKER).write_text(json.dumps({'kind': 'synthetic-only', 'schema_version': 1}), encoding='utf-8')
        skill = self.profile / 'skills/.system/test-skill'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: test-skill\ndescription: Synthetic\n---\nRead the reference.', encoding='utf-8')
        (skill / 'references').mkdir()
        (skill / 'references/check.txt').write_text('Synthetic reference', encoding='utf-8')
        self.source = self.root / 'environment'
        adapter.export_environment(self.home, self.profile, self.source,
                 engine_path=Path(__file__).resolve().parents[1] / 'src/vendor/codex_backup/backup.py')
        self.zip = self.root / 'Environment.zip'

    def test_hidden_resources_survive_inside_deterministic_archive(self):
        result = archive.pack_environment(self.source, self.zip)
        second = self.root / 'again.zip'
        archive.pack_environment(self.source, second)
        self.assertEqual(hashlib.sha256(self.zip.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
        self.assertEqual('verified', result['status'])
        destination = self.root / 'unpacked'
        archive.unpack_environment(self.zip, destination)
        self.assertEqual('Synthetic reference', (destination / 'payload/user-skills/.system/test-skill/references/check.txt').read_text(encoding='utf-8'))
        self.assertFalse((destination / 'config.toml').exists())
        self.assertFalse((destination / 'auth.json').exists())

    def test_traversal_rejected_before_creating_destination(self):
        with zipfile.ZipFile(self.zip, 'w') as z:
            z.writestr('environment.json', '{}')
            z.writestr('../escape.txt', 'Wrong')
        target = self.root / 'target'
        with self.assertRaises(ValueError):
            archive.unpack_environment(self.zip, target)
        self.assertFalse(target.exists())
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_zip_symlink_rejected(self):
        member = zipfile.ZipInfo('payload/link')
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.zip, 'w') as z:
            z.writestr('environment.json', '{}')
            z.writestr(member, 'outside')
        with self.assertRaises(ValueError):
            archive.unpack_environment(self.zip, self.root / 'target')
        self.assertFalse((self.root / 'target').exists())

    def test_invalid_archive_is_a_handled_failure(self):
        self.zip.write_bytes(b'Not a ZIP')
        with self.assertRaisesRegex(ValueError, 'Invalid environment archive'):
            archive.verify_environment_archive(self.zip)

    def test_existing_destination_not_overwritten(self):
        archive.pack_environment(self.source, self.zip)
        target = self.root / 'target'
        target.mkdir()
        sentinel = target / 'sentinel.txt'
        sentinel.write_text('Keep')
        with self.assertRaises(ValueError):
            archive.unpack_environment(self.zip, target)
        self.assertEqual('Keep', sentinel.read_text())

    def test_internal_resource_hash_mismatch_is_rejected(self):
        archive.pack_environment(self.source, self.zip)
        bad = self.root / 'changed.zip'
        with zipfile.ZipFile(self.zip) as original, zipfile.ZipFile(bad, 'w') as changed:
            for member in original.infolist():
                data = original.read(member)
                if member.filename.endswith('check.txt'):
                    data = b'Changed'
                changed.writestr(member, data)
        with self.assertRaises(ValueError):
            archive.verify_environment_archive(bad)

    def test_size_limit_checked_before_extraction(self):
        archive.pack_environment(self.source, self.zip)
        with patch.object(archive, 'MAX_FILE', 4):
            with self.assertRaises(ValueError):
                archive.unpack_environment(self.zip, self.root / 'target')
        self.assertFalse((self.root / 'target').exists())

    def test_live_profile_path_rejected_before_writes(self):
        archive.pack_environment(self.source, self.zip)
        target = self.root / '.codex/new'
        with self.assertRaises(ValueError):
            archive.unpack_environment(self.zip, target)
        self.assertFalse((self.root / '.codex').exists())
