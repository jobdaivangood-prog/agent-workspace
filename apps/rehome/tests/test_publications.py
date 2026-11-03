import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import native_adapter as native
import rehome_backup_module as backup
import rehome_publications as publications
import rehome_backup_workflow as workflow
import rehome_sessions as sessions
import rehome_summary_history as summaries
import snapshot
from tests import test_summary_history as summary_fixtures


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rehome-publications-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shared = self.root / 'Shared'
        self.shared.mkdir()
        self.source = self.root / 'finished.txt'
        self.source.write_text('Finished result', encoding='utf-8')
        self.source_profile = native.create_synthetic_profile(self.root / 'synthetic-source', seed=False)
        self.package = self.root / 'Prepared'
        source = summary_fixtures._source()
        self.info = backup.create_summary_backup(source, self.package, source_profile=self.source_profile)

    def publish(self, root=None, device='pc1', workspace='workspace_1', package=None, info=None):
        package = package or self.package
        info = info or self.info
        return publications.publish(package, root or self.shared,
            expected_package_digest=info['manifest_digest'], workspace_id=workspace,
            publisher_device_id=device)

    def scan(self):
        return publications.scan(self.shared, workspace_id='workspace_1')

    def assert_start_refuses_before_apply(self, root, workspace='workspace_1'):
        local = self.root / ('local-' + workspace)
        local.mkdir(exist_ok=True)
        settings = {'session_policy':publications.POLICY, 'session_workspace_id':workspace,
                    'source_profile':str(self.source_profile), 'local_root':str(local),
                    'backup_root':str(root)}
        with patch.object(workflow, '_runtime'), patch.object(sessions, 'request_close') as close, \
             patch.object(workflow, 'restore_backup') as restore:
            with self.assertRaises(ValueError):
                sessions.start(settings, close_apps=True, reopen=False)
        close.assert_not_called()
        restore.assert_not_called()

    def test_roundtrip_and_repeat_has_no_new_publication(self):
        first = self.publish()
        second = self.publish()
        self.assertEqual(first['manifest_digest'], second['manifest_digest'])
        self.assertEqual('unchanged', second['status'])
        self.assertEqual(1, len(self.scan()['complete']))
        verified = publications.verify(first['path'], workspace_id='workspace_1')
        self.assertEqual(summaries.POLICY,
                         backup.verify_backup(first['package'])['text_policy'])
        self.assertEqual(1, verified['counts']['messages'])

    def test_ready_marker_does_not_hide_missing_payload(self):
        item = self.publish()
        (Path(item['package'])/snapshot.CHAT_BUNDLE_PATH).unlink()
        found = self.scan()
        self.assertIsNone(found['latest'])
        self.assertEqual(1, len(found['unavailable']))

    def test_changed_bytes_and_cross_workspace_are_rejected(self):
        item = self.publish()
        with self.assertRaises(ValueError):
            publications.verify(item['path'], workspace_id='another', channel_id='main')
        history = Path(item['package']) / snapshot.CHAT_BUNDLE_PATH
        history.write_text(history.read_text(encoding='utf-8') + ' ', encoding='utf-8')
        self.assertIsNone(self.scan()['latest'])

    def test_parallel_sequence_is_deterministic_regardless_delivery_order(self):
        other = self.root / 'Other'
        other.mkdir()
        first = self.publish(device='pc-a')
        second = self.publish(root=other, device='pc-z')
        Path(second['path']).rename(self.shared / Path(second['path']).name)
        self.assertEqual(1, first['publication']['logical_sequence'])
        self.assertEqual(1, second['publication']['logical_sequence'])
        self.assertEqual(second['publication']['publication_id'], self.scan()['latest']['publication']['publication_id'])
        self.assertGreater(publications.order(second['publication']), publications.order(first['publication']))

    def test_incomplete_newer_publication_does_not_replace_complete_copy(self):
        first = self.publish()
        folder = self.shared / 'ReHome-incomplete'
        folder.mkdir()
        metadata = {**first['publication'], 'publication_id':'incomplete', 'logical_sequence':99}
        (folder/publications.MANIFEST).write_text(json.dumps(metadata), encoding='utf-8')
        self.assertEqual(first['publication']['publication_id'], self.scan()['latest']['publication']['publication_id'])
        self.assertEqual(1, len(self.scan()['unavailable']))

    def test_changed_prepared_pin_and_unsafe_identity_rejected_before_creation(self):
        with self.assertRaises(ValueError):
            publications.publish(self.package, self.shared, expected_package_digest='0'*64,
                workspace_id='workspace_1', publisher_device_id='pc')
        with self.assertRaises(ValueError):
            publications.publish(self.package, self.shared, expected_package_digest=self.info['manifest_digest'],
                workspace_id='../escape', publisher_device_id='pc')
        self.assertEqual([], list(self.shared.iterdir()))

    def test_legacy_snapshot_publication_is_rejected_before_staging(self):
        legacy_result = self.root / 'legacy-result.txt'
        legacy_result.write_text('legacy payload', encoding='utf-8')
        legacy = self.root / 'LegacySnapshot'
        legacy_info = snapshot.create_snapshot(legacy, version=1, device_id='legacy',
            results={'Results/legacy.txt':legacy_result})
        with self.assertRaises((ValueError, OSError)):
            publications.publish(legacy, self.shared, expected_package_digest=legacy_info['manifest_digest'],
                                 workspace_id='workspace_1', publisher_device_id='pc')
        self.assertEqual([], list(self.shared.iterdir()))
        with self.assertRaises((ValueError, OSError)):
            backup.verify_backup(legacy, expected_text_policy=summaries.POLICY)

    def test_foreign_workspace_summary_is_rejected_by_local_verification(self):
        foreign_root = self.root / 'Foreign'
        foreign_root.mkdir()
        foreign_source = summary_fixtures._source()
        foreign_source['workspace_id'] = 'foreign-workspace'
        foreign_package = self.root / 'ForeignPackage'
        foreign_info = backup.create_summary_backup(foreign_source, foreign_package,
                                                     source_profile=self.source_profile)
        foreign_publication = self.publish(root=foreign_root, workspace='foreign-workspace',
                                           package=foreign_package, info=foreign_info)
        with self.assertRaises(ValueError):
            publications.verify(foreign_publication['path'], workspace_id='workspace_1')
        received = publications.scan(foreign_root, workspace_id='workspace_1')
        self.assertIsNone(received['latest'])
        self.assertEqual(1, len(received['unavailable']))
        self.assert_start_refuses_before_apply(foreign_root)

    def test_forged_legacy_publication_is_rejected_even_with_valid_outer_markers(self):
        legacy_result = self.root / 'old.txt'
        legacy_result.write_text('old', encoding='utf-8')
        legacy = self.root / 'Legacy'
        legacy_info = snapshot.create_snapshot(legacy, version=1, device_id='legacy',
            results={'Results/old.txt':legacy_result})
        publication_id = 'legacy-publication'
        folder = self.shared / ('ReHome-' + publication_id)
        folder.mkdir()
        manifest = {'format':publications.FORMAT, 'policy':publications.POLICY,
            'publication_id':publication_id, 'workspace_id':'workspace_1', 'channel_id':'main',
            'publisher_device_id':'legacy-device', 'logical_sequence':1, 'parents':[],
            'package_manifest_sha256':legacy_info['manifest_digest'], 'report':None}
        snapshot._write_json(folder / publications.MANIFEST, manifest)
        snapshot.copy_snapshot(legacy, folder / 'Package', expected_digest=legacy_info['manifest_digest'])
        manifest_digest = snapshot._hash_file(folder / publications.MANIFEST)[0]
        snapshot._write_json(folder / publications.READY,
                             {'format':publications.FORMAT, 'manifest_sha256':manifest_digest})
        with self.assertRaises((ValueError, OSError)):
            publications.verify(folder, workspace_id='workspace_1')
        self.assertFalse((self.root / 'Applied').exists())
        self.assert_start_refuses_before_apply(self.shared)


if __name__ == '__main__':
    unittest.main()
