import ctypes
import json
from pathlib import Path
import queue
import threading
import unittest
from unittest.mock import Mock,patch

import rehome_backup_workflow as workflow
import rehome_backup_module as backup
import rehome_native_apply as apply
import rehome_apply_transaction as transaction
import snapshot
from tests import test_native_apply as fixtures
from tests import test_journal as journal_fixtures


class WorkflowSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.NativeApplyTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root

    def test_recovery_settings_load_without_old_runtime_or_template(self):
        settings={'format':'rehome-two-action-settings/1','source_home':str(self.fixture.target.parent),
            'source_profile':str(self.fixture.target),'local_root':str(self.root/'Local'),
            'backup_root':str(self.root/'Backups'),'projects_root':str(self.root/'Projects'),
            'runtime_executable':str(self.root/'missing.exe'),'runtime_sha256':'0'*64,
            'runtime_version':backup.SUPPORTED_RUNTIME,'schema_template':str(self.root/'missing-template'),'template_pins':{}}
        path=self.root/'settings.json';path.write_text(json.dumps(settings))
        loaded=workflow.load_settings(path)
        self.assertEqual(settings,loaded)

    def test_pending_recovery_is_found_with_different_local_root(self):
        _,receipt=self.fixture.prepared()
        settings={'source_profile':str(self.fixture.target),'local_root':str(self.root/'OtherLocal')}
        pointer=workflow._recovery_path(settings);pointer.parent.mkdir()
        transaction._atomic(pointer,receipt)
        with self.assertRaisesRegex(workflow.WorkflowError,'предыдущая операция'):
            workflow._recovery_first(settings)
        self.assertTrue(workflow.recovery_available(settings))

    def test_incomplete_restore_resources_report_fixed_error_before_backup_or_apply(self):
        template=self.fixture.fixture.template
        settings={'source_profile':str(self.fixture.target),'local_root':str(self.root/'Diagnostics'),
                  'schema_template':str(template),'template_pins':self.fixture.fixture.pins}
        for name in (workflow.staging.native.MARKER,'state_5.sqlite'):
            file=template/name;original=file.read_bytes()
            try:
                file.unlink()
                with patch.object(workflow,'_runtime'),patch.object(backup,'verify_backup') as verify:
                    with self.assertRaises(workflow.WorkflowError) as caught:
                        workflow.restore_backup(settings,self.root/'missing-backup',self.root/'cancel',queue.Queue())
                self.assertEqual('restore_resources_invalid',caught.exception.code)
                self.assertIn('ZIP',caught.exception.public_message)
                verify.assert_not_called()
                self.assertEqual(b'ORIGINAL_STATE',(self.fixture.target/'state_5.sqlite').read_bytes())
            finally:
                file.write_bytes(original)

    def test_profile_mutex_blocks_other_thread_independent_of_local_root(self):
        result=[]
        def enter():
            try:
                with workflow.ProfileLock(self.fixture.target):result.append('unexpected')
            except workflow.WorkflowError as error:result.append(error.code)
        with workflow.ProfileLock(self.fixture.target):
            worker=threading.Thread(target=enter);worker.start();worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(['already_running'],result)
        with workflow.ProfileLock(self.fixture.target):pass

    def test_source_mutation_during_freeze_is_rejected(self):
        source=self.fixture.fixture.fixture.profile
        original=snapshot._copy_verified;changed=[]
        def copy(path,target):
            result=original(path,target)
            if not changed:
                changed.append(True)
                with (source/'.codex-global-state.json').open('a') as stream:stream.write('\n')
            return result
        with patch.object(snapshot,'_copy_verified',side_effect=copy):
            with self.assertRaisesRegex(ValueError,'Source.*changed'):
                workflow.stable_source(source,self.root/'Stable',lambda:None)

    def test_same_inode_candidate_change_after_preflight_rolls_back(self):
        candidate,receipt=self.fixture.prepared();calls=[0];changed=[]
        file=Path(candidate['candidate'])/'Profile/session_index.jsonl'
        def guard():
            calls[0]+=1
            if calls[0]==4 and not changed:
                file.write_bytes(b'CHANGED_SAME_INODE');changed.append(True)
        with self.assertRaisesRegex(ValueError,'Installed bytes'):
            transaction.activate(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=guard)
        self.assertEqual(b'ORIGINAL_STATE',(self.fixture.target/'state_5.sqlite').read_bytes())

    def test_colliding_project_id_prefixes_generate_distinct_roots(self):
        bundle={'projects':[{'id':'samefirst-a','name':'Name'},{'id':'samefirst-b','name':'Name'}],'threads':[]}
        roots=apply._project_roots(bundle,self.root/'Projects','a'*32)
        self.assertEqual(2,len(set(roots.values())))

    def test_insufficient_journal_space_prevents_preparation(self):
        candidate=self.fixture.candidate()
        with patch.object(transaction.shutil,'disk_usage',return_value=Mock(free=0)):
            with self.assertRaisesRegex(ValueError,'space'):
                transaction.prepare(candidate['candidate'],self.root/'Transaction',expected_apply_digest=candidate['apply_digest'],guard=lambda:None)
        self.assertFalse((self.root/'Transaction').exists())

    @unittest.skipUnless(journal_fixtures.LIBRARY.is_file(),'Pinned local decoder unavailable')
    def test_full_backup_workflow_freezes_compressed_journals_and_forwards_decoder(self):
        self._compressed_workflow(explicit_pointer=False)

    @unittest.skipUnless(journal_fixtures.LIBRARY.is_file(),'Pinned local decoder unavailable')
    def test_full_backup_workflow_accepts_explicit_compressed_database_pointer(self):
        self._compressed_workflow(explicit_pointer=True)

    def _compressed_workflow(self, *, explicit_pointer):
        source=self.fixture.fixture.fixture.profile
        decoder=workflow._decoder({'decoder':{'path':str(journal_fixtures.LIBRARY),'sha256':journal_fixtures.LIBRARY_SHA256}})
        decoder.lib.ZSTD_compressBound.argtypes=[ctypes.c_size_t];decoder.lib.ZSTD_compressBound.restype=ctypes.c_size_t
        decoder.lib.ZSTD_compress.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]
        decoder.lib.ZSTD_compress.restype=ctypes.c_size_t
        path=self.fixture.fixture.fixture.fixture.path(self.fixture.fixture.fixture.tid)
        data=path.read_bytes();incoming=ctypes.create_string_buffer(data)
        compressed=ctypes.create_string_buffer(decoder.lib.ZSTD_compressBound(len(data)))
        length=decoder.checked(decoder.lib.ZSTD_compress(compressed,len(compressed),incoming,len(data),1))
        Path(str(path)+'.zst').write_bytes(compressed.raw[:length]);path.unlink()
        if explicit_pointer:
            import native_adapter as native
            with native._connect(source/'state_5.sqlite') as db:
                db.execute('UPDATE threads SET rollout_path=? WHERE rollout_path=?',(str(path)+'.zst',str(path)))
        local=self.root/'Local';local.mkdir()
        settings={'source_profile':str(source),'source_home':str(self.root),'local_root':str(local),
            'backup_root':str(self.root/'Backups'),'runtime_version':backup.SUPPORTED_RUNTIME,
            'decoder':{'path':str(journal_fixtures.LIBRARY),'sha256':journal_fixtures.LIBRARY_SHA256},'finished_results':[]}
        guard=Mock(spec=['assert_quiet']);guard.assert_quiet.return_value=None
        with patch.object(workflow,'_runtime'),patch.object(workflow,'_wait',return_value=guard):
            receipt=workflow.create_backup(settings,self.root/'cancel',queue.Queue())
        self.assertTrue(receipt['all_user_visible_chats_included'])
        self.assertEqual(3,receipt['counts']['threads'])
        payload=Path(receipt['backup_path'])
        self.assertFalse(any(p.name=='auth.json' for p in payload.rglob('*')))

    def test_journal_failures_are_actionable_without_echoing_exception_payload(self):
        local=self.root/'Diagnostics';local.mkdir()
        settings={'source_profile':str(self.fixture.target),'local_root':str(local)}
        for reason,code in [('Unsupported journal location','journal_location_unsupported'),
                            ('Journal is missing','journal_missing'),
                            ('PRIVATE_FAKE_MESSAGE_PAYLOAD','journal_read_failed')]:
            @workflow._profile_operation
            def operation(settings):
                raise workflow.rehome_journal.JournalError(reason)
            with self.subTest(code=code),self.assertRaises(workflow.WorkflowError) as caught:
                operation(settings)
            self.assertEqual(code,caught.exception.code)
            self.assertIn('Отчёт:',caught.exception.public_message)
            self.assertNotIn('PRIVATE_FAKE_MESSAGE_PAYLOAD',caught.exception.public_message)
        reports=[json.loads(p.read_text()) for p in local.glob('operation-stopped-*.json')]
        self.assertEqual(3,len(reports))
        self.assertNotIn('PRIVATE_FAKE_MESSAGE_PAYLOAD',json.dumps(reports))

    def test_user_cohort_excludes_internal_subagents_and_unmaterialized_rows(self):
        source=self.fixture.fixture.fixture.profile
        import native_adapter as native
        with native._connect(source/'state_5.sqlite') as db:
            ids=[r[0] for r in db.execute('SELECT id FROM threads ORDER BY id')]
            db.execute('UPDATE threads SET source=? WHERE id=?',(json.dumps({'subagent':{'kind':'internal'}}),ids[0]))
            db.execute("UPDATE threads SET has_user_event=0,archived=0,name=NULL,title='' WHERE id=?",(ids[1],))
            path=Path(db.execute('SELECT rollout_path FROM threads WHERE id=?',(ids[1],)).fetchone()[0])
        with native._connect(source/'thread_history_1.sqlite') as db:
            for table in ('thread_items','thread_turns','thread_history_projection_state'):
                db.execute('DELETE FROM '+table+' WHERE thread_id=?',(ids[1],))
        path.write_text(json.dumps({'type':'session_meta','ordinal':0,'payload':{'id':ids[1],'history_mode':'paginated'}})+'\n')
        selected,coverage=workflow._user_cohort(source)
        self.assertEqual([ids[2]],selected)
        self.assertEqual(1,coverage['internal_subagent_threads_omitted'])
        self.assertEqual(1,coverage['empty_unmaterialized_rows_omitted'])

    def test_mixed_source_union_is_not_silently_classified_as_internal(self):
        source=self.fixture.fixture.fixture.profile
        import native_adapter as native
        with native._connect(source/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET source=?',(json.dumps({'subagent':{},'future':{}}),))
        with self.assertRaises(workflow.WorkflowError):workflow._user_cohort(source)


if __name__=='__main__':unittest.main()
