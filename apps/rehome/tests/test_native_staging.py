import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import context_probe
import native_adapter as native
import rehome_backup_module as backup
import rehome_native_staging as staging
import snapshot
from tests import test_backup_module as fixtures


class NativeStagingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BackupModuleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.template = native.create_synthetic_profile(self.root / 'empty-template', seed=False)
        with native._connect(self.template / 'state_5.sqlite') as db:
            db.execute('ALTER TABLE threads ADD COLUMN attached_thread_parent_id TEXT')
        # Explicit test fixture metadata, never claimed to come from native runtime.
        for filename in staging.DATABASES:
            with native._connect(self.template / filename) as db:
                db.execute('CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY, checksum BLOB)')
                db.execute('INSERT INTO _sqlx_migrations VALUES(1,?)', (b'fixture-only',))
        self.pins = self.template_pins()
        self.created = self.fixture.create()
        self.mapping = {self.fixture.pid: 'Projects/Restored project'}

    def template_pins(self):
        return {f: snapshot._hash_file(self.template / f)[0] for f in staging.DATABASES}

    def prepare(self, **changes):
        args = dict(expected_digest=self.created['manifest_digest'], runtime_version=backup.SUPPORTED_RUNTIME,
                    schema_template=self.template, template_pins=self.pins, project_map=self.mapping)
        args.update(changes)
        return staging.prepare_native(self.root / 'backup', self.root / 'Staging', **args)

    def verify(self, result, **changes):
        args = dict(expected_digest=self.created['manifest_digest'], expected_stage_digest=result['stage_digest'])
        args.update(changes)
        return staging.verify_native_stage(result['job'], **args)

    def reseal(self, result):
        job = Path(result['job'])
        (job / staging.READY).write_text(native._json({'format': 'rehome-native-staging-ready/1',
            'backup_manifest_sha256': self.created['manifest_digest'], 'files': staging._inventory(job)}), encoding='utf-8')
        result['stage_digest'] = snapshot._hash_file(job / staging.READY)[0]

    def test_roundtrip_external_receipt_and_non_synthetic_provenance(self):
        result = self.prepare()
        self.assertEqual(result, self.verify(result))
        profile = Path(result['profile'])
        self.assertFalse((profile / native.MARKER).exists())
        self.assertFalse(json.loads((profile / 'import-report.json').read_text())['synthetic_only'])
        self.assertEqual(self.fixture.result.read_bytes(),
            (Path(result['job']) / self.mapping[self.fixture.pid] / 'Results/final.py').read_bytes())
        for key in ('profile_applied', 'native_launch_verified', 'environment_activated', 'nas_delivery_verified'):
            self.assertIs(False, result[key])
        with self.assertRaises(context_probe.ProbeError):
            context_probe._guard_profile(profile)
        bundle = backup._read(self.root / 'backup' / snapshot.CHAT_BUNDLE_PATH)['transfer']
        with self.assertRaises(native.AdapterError):
            native.import_profile(bundle, self.root / 'old-import', {self.fixture.pid: str(self.root)})
        with self.assertRaises(native.AdapterError):
            native._validate_bundle(bundle)
        self.assertFalse((self.root / 'old-import').exists())

    def test_attachment_count_does_not_append_to_verbatim_text(self):
        bundle = backup._read(self.root / 'backup' / snapshot.CHAT_BUNDLE_PATH)['transfer']
        bundle['threads'][0]['turns'][0]['messages'][0]['omitted_attachments'] = 3
        profile = self.root / 'typed-writer'
        native._write_profile(bundle, profile, {self.fixture.pid: str(self.root / 'mapped')},
            schema_template=self.template, synthetic_only=False, runtime_version=backup.SUPPORTED_RUNTIME)
        staging._readback(profile, bundle, {self.fixture.pid: str(self.root / 'mapped')})

    def test_projectless_multiple_projects_links_cards_and_terminal_statuses(self):
        bundle = backup._read(self.root / 'backup' / snapshot.CHAT_BUNDLE_PATH)['transfer']
        first = bundle['threads'][0]
        first['archived'], first['is_pinned'] = True, True
        first['turns'][0]['status'] = 'failed'
        first['turns'].append({'id': 'empty-interrupted', 'status': 'interrupted', 'messages': []})
        first['turns'][0]['messages'][1]['phase'] = 'commentary'
        second = copy.deepcopy(first)
        second.update(id='00000000-0000-4000-8000-000000000097', project_id=None, archived=False)
        third = copy.deepcopy(first)
        third.update(id='00000000-0000-4000-8000-000000000098', project_id='other-project')
        third['turns'][0]['status'] = 'completed'
        bundle['projects'].append({'id': 'other-project', 'name': 'Другой проект 😀'})
        bundle['threads'].extend([second, third])
        bundle['spawn_edges'] = [{'parent': first['id'], 'child': second['id']}]
        roots = {self.fixture.pid: str(self.root / 'one'), 'other-project': str(self.root / 'two'),
                 '__projectless__': str(self.root / 'three')}
        profile = self.root / 'rich-writer'
        native._write_profile(bundle, profile, roots, schema_template=self.template,
                             synthetic_only=False, runtime_version=backup.SUPPORTED_RUNTIME)
        staging._readback(profile, bundle, roots)

    def test_bad_runtime_and_external_pins_rejected_before_writing(self):
        for change in ({'runtime_version': 'future'}, {'expected_digest': None}, {'expected_digest': '0'*64},
                       {'template_pins': {}}, {'template_pins': {f: '0'*64 for f in staging.DATABASES}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare(**change)
            self.assertFalse((self.root / 'Staging').exists())

    def test_template_extra_files_sidecars_and_nonempty_records_rejected(self):
        for name in ('auth.json', 'config.toml', 'state_5.sqlite-wal', 'extra.txt'):
            path = self.template / name
            path.write_text('sentinel')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse((self.root / 'Staging').exists())
            path.unlink()
        with native._connect(self.template / 'state_5.sqlite') as db:
            db.execute("INSERT INTO projects VALUES('p','name','{}',0,0,0)")
        with self.assertRaisesRegex(ValueError, 'contains user'):
            self.prepare(template_pins=self.template_pins())
        self.assertFalse((self.root / 'Staging').exists())

    def test_static_or_empty_migrations_and_unknown_trigger_rejected(self):
        with native._connect(self.template / 'state_5.sqlite') as db:
            db.execute('CREATE TRIGGER unexpected AFTER DELETE ON projects BEGIN DELETE FROM threads; END')
        with self.assertRaisesRegex(ValueError, 'schema object'):
            self.prepare(template_pins=self.template_pins())
        self.assertFalse((self.root / 'Staging').exists())
        with native._connect(self.template / 'state_5.sqlite') as db:
            db.execute('DROP TRIGGER unexpected')
        with native._connect(self.template / 'state_5.sqlite') as db:
            db.execute('DELETE FROM _sqlx_migrations')
        with self.assertRaisesRegex(ValueError, 'metadata is empty'):
            self.prepare(template_pins=self.template_pins())
        with native._connect(self.template / 'state_5.sqlite') as db:
            db.execute('DROP TABLE _sqlx_migrations')
        with self.assertRaisesRegex(ValueError, 'SQLx'):
            self.prepare(template_pins=self.template_pins())
        self.assertFalse((self.root / 'Staging').exists())

    def test_old_schema_template_and_backup_runtime_are_rejected(self):
        with native._connect(self.template / 'state_5.sqlite') as db:
            db.execute('ALTER TABLE threads DROP COLUMN attached_thread_parent_id')
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            self.prepare(template_pins=self.template_pins())
        bundle = backup._read(self.root / 'backup' / snapshot.CHAT_BUNDLE_PATH)['transfer']
        with self.assertRaisesRegex(ValueError, 'runtime'):
            native._write_profile(bundle, self.root / 'bad-writer', {self.fixture.pid: str(self.root)},
                schema_template=self.template, synthetic_only=False, runtime_version=native.CODEX_VERSION)
        self.assertFalse((self.root / 'Staging').exists())
        self.assertFalse((self.root / 'bad-writer').exists())

    def test_hardlinked_template_rejected_before_writing(self):
        link = self.root / 'linked-database'
        os.link(self.template / 'state_5.sqlite', link)
        with self.assertRaisesRegex(ValueError, 'Linked'):
            self.prepare()
        self.assertFalse((self.root / 'Staging').exists())

    def test_existing_unowned_root_and_overlap_rejected_without_changes(self):
        target = self.root / 'Staging'
        target.mkdir()
        (target / 'keep').write_text('keep')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(['keep'], [p.name for p in target.iterdir()])
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            self.prepare(forbidden_profiles=[target])

    def test_project_map_exactness_collisions_and_unsafe_paths_precede_writes(self):
        for mapping in ({}, {**self.mapping, 'extra': 'Projects/extra'},
                        {self.fixture.pid: 'Elsewhere'}, {self.fixture.pid: 'Projects/../escape'},
                        {self.fixture.pid: 'Projects/CON'}, {self.fixture.pid: 'Projects/a:stream'}):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                self.prepare(project_map=mapping)
            self.assertFalse((self.root / 'Staging').exists())
        bundle = backup._read(self.root / 'backup' / snapshot.CHAT_BUNDLE_PATH)['transfer']
        bundle['projects'].append({'id': 'other', 'name': 'Other'})
        for value in ('Projects/RESTORED PROJECT', 'Projects/Restored project/child'):
            with self.assertRaises(ValueError):
                staging._project_map(bundle, {**self.mapping, 'other': value})

    def test_corrupt_backup_rejected_before_staging(self):
        (self.root / 'backup' / self.fixture.relative).write_text('corrupt')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse((self.root / 'Staging').exists())

    def test_wrong_native_pin_and_self_resealed_ready_rejected(self):
        result = self.prepare()
        for pin in (None, '', '0'*64):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                self.verify(result, expected_stage_digest=pin)
        (Path(result['profile']) / 'auth.json').write_text('{}')
        altered = copy.deepcopy(result)
        self.reseal(altered)
        with self.assertRaisesRegex(ValueError, 'external pin'):
            self.verify(result)
        with self.assertRaisesRegex(ValueError, 'staging files'):
            self.verify(altered)

    def test_resealed_result_global_state_index_report_and_empty_directory_rejected(self):
        changes = [('Projects/Restored project/Results/final.py', b'changed'),
                   ('Profile/.codex-global-state.json', b'{}'), ('Profile/session_index.jsonl', b''),
                   ('Profile/import-report.json', b'{}')]
        for relative, data in changes:
            with self.subTest(relative=relative):
                result = self.prepare()
                (Path(result['job']) / relative).write_bytes(data)
                self.reseal(result)
                with self.assertRaises(ValueError):
                    self.verify(result)
        result = self.prepare()
        (Path(result['job']) / 'unexpected-empty').mkdir()
        with self.assertRaises(ValueError):
            self.verify(result)

    def test_resealed_sql_journal_schema_and_projection_corruption_rejected(self):
        statements = [('state_5.sqlite', "UPDATE project_roots SET path='C:/escaped'"),
                      ('thread_history_1.sqlite', "UPDATE thread_items SET item_type='tool'"),
                      ('thread_history_1.sqlite', 'UPDATE thread_history_projection_state SET next_rollout_ordinal=999'),
                      ('thread_history_1.sqlite', "UPDATE thread_turns SET error_json='hidden'"),
                      ('state_5.sqlite', "UPDATE _sqlx_migrations SET checksum=X'00'")]
        for filename, sql in statements:
            with self.subTest(sql=sql):
                result = self.prepare()
                with native._connect(Path(result['profile']) / filename) as db:
                    db.execute(sql)
                self.reseal(result)
                with self.assertRaises(ValueError):
                    self.verify(result)
        result = self.prepare()
        journal = next((Path(result['profile']) / 'sessions').glob('*.jsonl'))
        records = [json.loads(line) for line in journal.read_bytes().splitlines()]
        records[1]['payload']['role'] = 'assistant'
        journal.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
        self.reseal(result)
        with self.assertRaises(ValueError):
            self.verify(result)

    def test_failed_postwrite_verification_has_no_ready_marker(self):
        with patch.object(staging, 'verify_native_stage', side_effect=ValueError('verification failed')):
            with self.assertRaises(ValueError):
                self.prepare()
        self.assertFalse(list((self.root / 'Staging').glob('*/NativeREADY.json')))

    def test_existing_generated_job_never_overwritten(self):
        result = self.prepare()
        job = Path(result['job'])
        before = staging._inventory(job)
        with patch.object(staging.uuid, 'uuid4', return_value=job.name):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertEqual(before, staging._inventory(job))

    def test_linked_root_rejected_before_write(self):
        link = self.root / 'linked'
        try:
            link.symlink_to(self.template, target_is_directory=True)
        except OSError:
            self.skipTest('Host does not permit synthetic symlink creation')
        with self.assertRaises(ValueError):
            self.prepare(schema_template=link)
        self.assertFalse((self.root / 'Staging').exists())


if __name__ == '__main__':
    unittest.main()
