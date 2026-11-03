import copy
import json
import os
from pathlib import Path
import unittest

import native_adapter as native
import rehome_backup_module as backup
import rehome_native_staging as staging
import rehome_native_apply as apply
import rehome_apply_transaction as transaction
import rehome_display_metadata as display
import rehome_review as review
import snapshot
import rehome_backup_workflow as workflow
import rehome_closed_snapshot as closed
from tests import test_native_staging as fixtures
from tests.dated_fixture import dated_fixture


class DisplayMetadataTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.NativeStagingTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root
        self.ids=dated_fixture(self.fixture.fixture)
        self.path=self.root/'DatedBackup'
        self.created=backup.create_backup(self.fixture.fixture.profile,self.ids,self.path,ready_files={},
            runtime_version=backup.SUPPORTED_RUNTIME,device_id='synthetic-dates',include_all_layout=True,preserve_display=True)
        self.history=backup._read(self.path/snapshot.CHAT_BUNDLE_PATH)

    def prepare(self):
        return staging.prepare_native(self.path,self.root/'DateStage',expected_digest=self.created['manifest_digest'],
            runtime_version=backup.SUPPORTED_RUNTIME,schema_template=self.fixture.template,template_pins=self.fixture.pins,
            project_map={p['id']:'Projects/'+p['id'] for p in self.history['transfer']['projects']})

    def test_dates_nulls_project_order_section_positions_and_journals_roundtrip(self):
        result=self.prepare();profile=Path(result['profile']);meta=self.history['display_metadata']
        self.assertEqual(['empty-project',self.fixture.fixture.pid],meta['project_order'])
        self.assertIsNone(meta['threads'][self.ids[0]]['values']['created_at_ms'])
        staging._readback(profile,self.history['transfer'],backup._read(Path(result['job'])/staging.MARKER)['project_roots'],display_metadata=meta)
        with native._connect(profile/'state_5.sqlite',readonly=True) as db:
            row=db.execute('SELECT created_at,section_position FROM threads WHERE id=?',(self.ids[0],)).fetchone()
        self.assertEqual((1700000000,10),tuple(row))
        index=[json.loads(line) for line in (profile/'session_index.jsonl').read_bytes().splitlines()]
        self.assertTrue(all(not item['updated_at'].startswith('1970') for item in index))

    def test_bad_types_extra_fields_or_ids_rejected(self):
        for mutation in ('bool','extra','missing'):
            bad=copy.deepcopy(self.history)
            if mutation=='bool':bad['display_metadata']['threads'][self.ids[0]]['values']['created_at']=True
            if mutation=='extra':bad['display_metadata']['auth']='not allowed'
            if mutation=='missing':bad['display_metadata']['threads'].pop(self.ids[0])
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):review.validate_backup_history(bad)

    def test_true_writer_rejects_display_metadata_before_writing(self):
        bundle=copy.deepcopy(self.history['transfer']);bundle['synthetic_only']=True
        with self.assertRaisesRegex(ValueError,'confined'):
            native._write_profile(bundle,self.root/'Synthetic',{},schema_template=self.fixture.template,synthetic_only=True,
                runtime_version=native.CODEX_VERSION,display_metadata=self.history['display_metadata'])
        self.assertFalse((self.root/'Synthetic').exists())

    def test_journal_mtime_change_fails_stage_verification(self):
        result=self.prepare();profile=Path(result['profile'])
        file=next((profile/'sessions').glob('*.jsonl'));before=file.stat().st_mtime_ns
        os.utime(file,ns=(before+1000000000,before+1000000000))
        with self.assertRaisesRegex(ValueError,'modification time'):
            staging.verify_native_stage(result['job'],expected_digest=self.created['manifest_digest'],expected_stage_digest=result['stage_digest'])

    def test_dated_final_apply_and_rollback_keep_display_metadata(self):
        staged=self.prepare();home=self.root/'Receiver';target=home/'.codex';target.mkdir(parents=True)
        (target/'state_5.sqlite').write_bytes(b'ORIGINAL')
        candidate=apply.prepare(staged['job'],self.root/'Apply',expected_digest=self.created['manifest_digest'],
            expected_stage_digest=staged['stage_digest'],target_profile=target,target_home=home,projects_root=self.root/'Projects')
        receipt=transaction.prepare(candidate['candidate'],self.root/'Transaction',expected_apply_digest=candidate['apply_digest'],guard=lambda:None)
        transaction.activate(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=lambda:None)
        apply.verify_installed(candidate['candidate'])
        with native._connect(target/'state_5.sqlite',readonly=True) as db:
            self.assertEqual(1700000000,db.execute('SELECT created_at FROM threads WHERE id=?',(self.ids[0],)).fetchone()[0])
        transaction.rollback(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=lambda:None)
        self.assertEqual(b'ORIGINAL',(target/'state_5.sqlite').read_bytes())

    def test_closed_unfinished_backup_stages_applies_and_rolls_back_with_null_timings(self):
        source=self.fixture.fixture.profile;tid=self.fixture.fixture.tid
        path=self.fixture.fixture.fixture.path(tid)
        records=[json.loads(line) for line in path.read_bytes().splitlines()]
        records[0]['payload']['cli_version']='0.166.0-alpha.5.1'
        terminal=records.pop();turn_id=terminal['payload']['turn_id']
        data=''.join(json.dumps(row)+'\n' for row in records).encode();path.write_bytes(data)
        with native._connect(source/'thread_history_1.sqlite') as db:
            db.execute("UPDATE thread_turns SET status='inProgress',rollout_end_ordinal=NULL,completed_at=NULL,duration_ms=NULL WHERE thread_id=? AND turn_id=?",(tid,turn_id))
            db.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?',(len(data),terminal['ordinal'],tid))
        frozen=workflow.stable_source(source,self.root/'ClosedProfile',lambda:None)
        proof=closed.seal(frozen,original_profile=source,guard=lambda:None)
        path=self.root/'ClosedBackup'
        created=backup.create_backup(frozen,self.ids,path,ready_files={},runtime_version=backup.SUPPORTED_RUNTIME,
            device_id='synthetic-closed',include_all_layout=True,preserve_display=True,closed_snapshot=proof)
        history=backup._read(path/snapshot.CHAT_BUNDLE_PATH);review.validate_backup_history(history)
        staged=staging.prepare_native(path,self.root/'ClosedStage',expected_digest=created['manifest_digest'],
            runtime_version=backup.SUPPORTED_RUNTIME,schema_template=self.fixture.template,template_pins=self.fixture.pins,
            project_map={p['id']:'Projects/'+p['id'] for p in history['transfer']['projects']})
        home=self.root/'ClosedReceiver';target=home/'.codex';target.mkdir(parents=True)
        (target/'state_5.sqlite').write_bytes(b'ORIGINAL');(target/'auth.json').write_text('EXISTING_AUTH')
        candidate=apply.prepare(staged['job'],self.root/'ClosedApply',expected_digest=created['manifest_digest'],
            expected_stage_digest=staged['stage_digest'],target_profile=target,target_home=home,projects_root=self.root/'ClosedProjects')
        receipt=transaction.prepare(candidate['candidate'],self.root/'ClosedTransaction',expected_apply_digest=candidate['apply_digest'],guard=lambda:None)
        transaction.activate(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=lambda:None)
        apply.verify_installed(candidate['candidate'])
        with native._connect(target/'thread_history_1.sqlite',readonly=True) as db:
            self.assertEqual(('interrupted',None,None),tuple(db.execute('SELECT status,completed_at,duration_ms FROM thread_turns WHERE thread_id=? AND turn_id=?',(tid,turn_id)).fetchone()))
        proof.verify(frozen)
        transaction.rollback(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=lambda:None)
        self.assertEqual(b'ORIGINAL',(target/'state_5.sqlite').read_bytes());self.assertEqual('EXISTING_AUTH',(target/'auth.json').read_text())


if __name__=='__main__':unittest.main()
