import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import environment_adapter
import environment_archive

import native_adapter as native
import rehome_backup_module as module
import rehome_history_filter as filtering
import rehome_history_lineage as lineage
import rehome_review as review
import rehome_review_sources as managed
import snapshot
from tests import test_history_lineage as fixtures


TEXT = ('# Files mentioned by the user:\n<attached_file>Исходный текст 😀</attached_file>\n'
        'api_key=FAKE_TEST_VALUE_ONLY\nC:\\old-computer\\project\\result.py\n')


class TextPolicyTests(unittest.TestCase):
    def user(self):
        return {'type': 'userMessage', 'id': 'u', 'content': [
            {'type': 'text', 'text': TEXT, 'text_elements': []},
            {'type': 'image', 'url': 'OMITTED_IMAGE_BYTES'},
            {'type': 'localImage', 'path': 'OMITTED_SOURCE_PATH'}]}

    def test_full_text_exact_while_structured_attachments_are_omitted(self):
        message, evidence = filtering.visible_message(self.user(), text_policy=filtering.VERBATIM_TEXT)
        self.assertEqual(TEXT, message['text'])
        self.assertEqual(2, message['omitted_attachments'])
        self.assertEqual(0, evidence['excluded_text_ranges'])
        self.assertNotIn('OMITTED_', json.dumps([message, evidence]))
        with self.assertRaises(filtering.HistoryReviewRequired):
            filtering.visible_message(self.user())

    def test_assistant_full_text_does_not_copy_internal_fields(self):
        item = {'type': 'agentMessage', 'id': 'a', 'text': TEXT, 'phase': 'final_answer',
                'internal': 'HIDDEN_REASONING_SENTINEL'}
        message, evidence = filtering.visible_message(item, text_policy=filtering.VERBATIM_TEXT)
        self.assertEqual(TEXT, message['text'])
        self.assertNotIn('HIDDEN_REASONING', json.dumps([message, evidence]))

    def test_unknown_blocks_and_annotations_still_stop(self):
        for part in ({'type': 'futureFile'}, {'type': 'text', 'text': 'x', 'unknown': True},
                     {'type': 'text', 'text': 'x', 'text_elements': [{}]}):
            item = self.user()
            item['content'] = [part]
            with self.subTest(part=part), self.assertRaises(filtering.HistoryReviewRequired):
                filtering.visible_message(item, text_policy=filtering.VERBATIM_TEXT)

    def test_policy_typos_and_old_exclusions_cannot_silently_change_text(self):
        for policy in (None, 'full', True, ''):
            with self.subTest(policy=policy), self.assertRaises(filtering.HistoryReviewRequired):
                filtering.visible_message(self.user(), text_policy=policy)
        with self.assertRaisesRegex(filtering.HistoryReviewRequired, 'manual exclusions'):
            filtering.visible_message(self.user(), {}, text_policy=filtering.VERBATIM_TEXT)

    def test_question_cards_keep_visible_full_text_but_validate_their_protocol(self):
        item = {'type': 'agentMessage', 'id': 'a', 'text': TEXT, 'phase': 'final_answer',
                'delivery': 'async', 'questions': [{'title': TEXT, 'options': [TEXT]}]}
        message, report = filtering.visible_message(item, text_policy=filtering.VERBATIM_TEXT)
        self.assertEqual(3, message['text'].count(TEXT))
        self.assertFalse(report['visible_card_projection']['interactive_restoration'])
        item['questions'][0]['unexpected'] = True
        with self.assertRaises(filtering.HistoryReviewRequired):
            filtering.visible_message(item, text_policy=filtering.VERBATIM_TEXT)


class BackupModuleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LineageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.profile = self.fixture.root, self.fixture.profile
        self.tid = self.fixture.grandchild
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            for table in ('thread_items', 'thread_turns', 'thread_history_projection_state'):
                db.execute('DELETE FROM ' + table + ' WHERE thread_id=?', (self.tid,))
        self.fixture.make_journal(self.tid, [TEXT])
        with native._connect(self.profile / 'state_5.sqlite', readonly=True) as db:
            self.pid = db.execute('SELECT project_id FROM threads WHERE id=?', (self.tid,)).fetchone()[0]
        self.result = self.root / 'final.py'
        self.result.write_text('print("Created result, not an input")\n', encoding='utf-8')
        self.input = self.root / 'input.docx'
        self.input.write_bytes(b'ORIGINAL_INPUT_MUST_NOT_COPY')
        self.relative = 'Projects/' + self.pid + '/Results/final.py'

    def create(self, destination=None, **options):
        return module.create_backup(self.profile, [self.tid], destination or self.root / 'backup',
            ready_files={self.relative: {'path': str(self.result), 'classification': managed.CLASSIFICATION}}, runtime_version=module.SUPPORTED_RUNTIME,
            device_id='synthetic-pc1', **options)

    def candidate(self):
        return lineage.export_candidate(self.profile, [self.tid], text_policy=filtering.VERBATIM_TEXT)

    def test_contract_separation_and_zero_manual_ranges(self):
        candidate = self.candidate()
        self.assertEqual('rehome-backup-history/1', candidate['format'])
        self.assertFalse(candidate['transfer']['synthetic_only'])
        self.assertEqual(2, review.validate_backup_history(candidate)['messages'])
        with self.assertRaises(review.ReviewIntegrityError):
            review._validate_candidate(candidate)
        for change in ({'format': 'rehome-history-review-candidate/1'}, {'text_policy': 'restricted_visible_text'}):
            bad = {**candidate, **change}
            with self.assertRaises(review.ReviewIntegrityError):
                review.validate_backup_history(bad)
        candidate['filtering'][0]['excluded_text_ranges'] = 1
        with self.assertRaises(review.ReviewIntegrityError):
            review.validate_backup_history(candidate)
        with self.assertRaisesRegex(lineage.LineageError, 'manual exclusions'):
            lineage.export_candidate(self.profile, [self.tid], exclusions={'old': {}}, text_policy=filtering.VERBATIM_TEXT)

    def test_full_layout_includes_empty_projects_and_sections_without_source_files(self):
        with native._connect(self.profile/'state_5.sqlite') as db:
            db.execute("INSERT INTO projects VALUES('empty-project','Empty project','{}',50,0,0)")
            db.execute("INSERT INTO thread_sections VALUES('empty-section','Empty section',NULL)")
        result=self.create(include_all_layout=True)
        candidate=backup_candidate=snapshot._read_json(self.root/'backup'/snapshot.CHAT_BUNDLE_PATH)
        self.assertIn({'id':'empty-project','name':'Empty project'},candidate['transfer']['projects'])
        self.assertIn({'id':'empty-section','name':'Empty section'},candidate['transfer']['sections'])
        self.assertEqual(2,result['counts']['projects'])

    def test_create_and_stage_only_explicit_ready_files_preserving_source(self):
        before = {str(p): hashlib.sha256(p.read_bytes()).digest()
                  for p in self.profile.rglob('*') if p.is_file()}
        info = self.create()
        target = self.root / 'received'
        received = module.stage_restore(self.root / 'backup', target,
            expected_digest=info['manifest_digest'], receiving_runtime_version=module.SUPPORTED_RUNTIME)
        self.assertEqual('backup_staged', received['status'])
        self.assertFalse(received['profile_applied'])
        self.assertFalse(received['native_profile_created'])
        self.assertEqual(self.result.read_bytes(), (target / self.relative).read_bytes())
        candidate = snapshot._read_json(target / snapshot.CHAT_BUNDLE_PATH)
        self.assertEqual(TEXT, candidate['transfer']['threads'][0]['turns'][0]['messages'][0]['text'])
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).digest()
                                 for p in self.profile.rglob('*') if p.is_file()})
        self.assertFalse(any(p.name == 'input.docx' for p in target.rglob('*')))
        self.assertEqual(1, info['counts']['results'])

    def test_synthetic_native_roundtrip_preserves_text_order_archive_and_projects(self):
        # Test-only provenance: this entire source is created by LineageTests.
        # Production module never turns a real candidate into a synthetic import.
        native._marker(self.profile)
        candidate = lineage.export_candidate(self.profile, [self.fixture.child, self.tid],
                                               text_policy=filtering.VERBATIM_TEXT)
        bundle = copy.deepcopy(candidate['transfer'])
        bundle['synthetic_only'] = True
        target = self.root / 'synthetic-native'
        native.import_profile(bundle, target, {self.pid: str(self.root / 'project-on-pc2')})
        restored = native.export_profile(target)
        expected = {t['id']: t for t in bundle['threads']}
        self.assertEqual(expected, {t['id']: t for t in restored['threads']})
        self.assertEqual(bundle['projects'], restored['projects'])
        self.assertEqual(TEXT, expected[self.tid]['turns'][0]['messages'][0]['text'])

    def test_corruption_and_missing_payload_stop_before_creating_target(self):
        info = self.create()
        (self.root / 'backup' / self.relative).write_text('Corrupt')
        target = self.root / 'not-applied'
        with self.assertRaises(ValueError):
            module.stage_restore(self.root / 'backup', target, expected_digest=info['manifest_digest'],
                                 receiving_runtime_version=module.SUPPORTED_RUNTIME)
        self.assertFalse(target.exists())

    def test_policy_pin_version_and_existing_target_stop(self):
        info = self.create()
        target = self.root / 'not-applied'
        for options in ({'expected_digest': '0' * 64}, {'expected_digest': None},
                        {'receiving_runtime_version': '0.999.0'}, {'expected_text_policy': 'restricted_visible_text'}):
            args = {'expected_digest': info['manifest_digest'], 'receiving_runtime_version': module.SUPPORTED_RUNTIME}
            args.update(options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                module.stage_restore(self.root / 'backup', target, **args)
            self.assertFalse(target.exists())
        target.mkdir()
        (target / 'keep.txt').write_text('Keep')
        with self.assertRaises(ValueError):
            module.stage_restore(self.root / 'backup', target, expected_digest=info['manifest_digest'],
                                 receiving_runtime_version=module.SUPPORTED_RUNTIME)
        self.assertEqual('Keep', (target / 'keep.txt').read_text())

    def test_descriptor_tamper_is_rejected_even_with_resealed_outer_snapshot(self):
        self.create()
        root = self.root / 'backup'
        candidate = snapshot._read_json(root / snapshot.CHAT_BUNDLE_PATH)
        descriptor = snapshot._read_json(root / module.DESCRIPTOR)
        descriptor['text_policy'] = 'restricted_visible_text'
        source = self.root / 'bad-descriptor.json'
        source.write_text(json.dumps(descriptor), encoding='utf-8')
        bad = self.root / 'resealed'
        snapshot.create_snapshot(bad, version=1, device_id='synthetic',
            results={module.DESCRIPTOR: source, self.relative: self.result}, chat_bundle=candidate)
        with self.assertRaisesRegex(module.BackupError, 'contract differs'):
            module.verify_backup(bad)

    def test_results_must_belong_to_a_selected_project_and_source_is_not_scanned(self):
        for relative in ('input.docx', 'Projects/unknown/Results/a.txt', '../escape.txt'):
            with self.subTest(path=relative), self.assertRaises(ValueError):
                module.create_backup(self.profile, [self.tid], self.root / 'bad',
                    ready_files={relative: {'path': str(self.input), 'classification': managed.CLASSIFICATION}},
                    runtime_version=module.SUPPORTED_RUNTIME, device_id='test')
        self.assertFalse((self.root / 'bad').exists())

    def test_profile_credentials_and_unclassified_inputs_cannot_masquerade_as_results(self):
        auth = self.profile / 'auth.json'
        auth.write_text('{"secret":"FAKE_ONLY"}')
        external = self.root / 'auth.json'
        external.write_text('{"secret":"FAKE_ONLY"}')
        secret = self.root / 'report.txt'
        secret.write_text('api_key="THIS_IS_A_SYNTHETIC_TEST_VALUE"')
        hidden = self.root / '.aws/renamed.txt'
        hidden.parent.mkdir()
        hidden.write_text('Credentials fixture')
        for record in (str(self.result), {'path': str(self.input), 'classification': 'original'},
                       *({'path': str(p), 'classification': managed.CLASSIFICATION} for p in (auth, external, secret, hidden))):
            with self.subTest(record=record), self.assertRaises(ValueError):
                module.create_backup(self.profile, [self.tid], self.root / 'bad-result',
                    ready_files={self.relative: record}, runtime_version=module.SUPPORTED_RUNTIME, device_id='test')
            self.assertFalse((self.root / 'bad-result').exists())

    def test_builder_size_limits_checked_before_first_payload_copy(self):
        engine = environment_adapter.load_existing_engine(module.ENGINE_PATH)
        for limit in ('MAX_FILE', 'MAX_TOTAL'):
            with self.subTest(limit=limit), patch.object(engine, limit, 1), \
                 patch.object(module.environment_adapter, 'load_existing_engine', return_value=engine), \
                 patch.object(module.snapshot, '_copy_verified') as copier:
                with self.assertRaises(module.BackupError):
                    self.create()
                copier.assert_not_called()
                self.assertFalse((self.root / 'backup').exists())

    def test_existing_environment_package_survives_backup_and_staging(self):
        skill = self.profile / 'skills/demo'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: demo\ndescription: Synthetic\n---\nUse the reference.\n')
        (skill / 'reference.txt').write_text('Complete synthetic reference')
        # Environment exporter requires staging outside its synthetic source home.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='rehome-environment-') as temporary:
            environment = Path(temporary) / 'environment'
            environment_adapter.export_environment(self.root, self.profile, environment, engine_path=module.ENGINE_PATH)
            info = self.create(environment_stage=environment)
            target = self.root / 'received-with-environment'
            module.stage_restore(self.root / 'backup', target, expected_digest=info['manifest_digest'],
                                 receiving_runtime_version=module.SUPPORTED_RUNTIME)
            unpacked = Path(temporary) / 'unpacked'
            environment_archive.unpack_environment(target / module.ENVIRONMENT, unpacked)
            self.assertEqual('Complete synthetic reference',
                (unpacked / 'payload/user-skills/demo/reference.txt').read_text())
            self.assertEqual('synthetic_resources_only', info['environment'])


if __name__ == '__main__':
    unittest.main()
