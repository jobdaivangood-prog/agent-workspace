import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import rehome_backup_workflow as workflow
import rehome_closed_snapshot as closed
import rehome_history_filter as filtering
import rehome_history_lineage as lineage
import rehome_review as review
import snapshot
from tests import test_history_lineage as fixtures


class ClosedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.LineageTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.original=self.fixture.profile;self.root=self.fixture.root;self.tid=self.fixture.parent
        with lineage.native._connect(self.original/'state_5.sqlite') as db:
            db.execute("UPDATE threads SET source='subagent' WHERE id!=?",(self.tid,))
        self.fixture.change_header(self.tid,lambda meta:meta.update(cli_version='0.163.0-alpha.10.2'))
        path=self.fixture.path(self.tid);records=[json.loads(line) for line in path.read_bytes().splitlines()]
        terminal=records.pop();self.turn=terminal['payload']['turn_id']
        data=''.join(json.dumps(row)+'\n' for row in records).encode();path.write_bytes(data)
        with lineage.native._connect(self.original/'thread_history_1.sqlite') as db:
            db.execute("UPDATE thread_turns SET status='inProgress',rollout_end_ordinal=NULL WHERE thread_id=? AND turn_id=?",(self.tid,self.turn))
            db.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?',(len(data),terminal['ordinal'],self.tid))
        self.profile=workflow.stable_source(self.original,self.root/'Frozen',lambda:None)
        self.proof=closed.seal(self.profile,original_profile=self.original,guard=lambda:None)

    def export(self,**options):
        return lineage.export_candidate(self.profile,[self.tid],text_policy=filtering.VERBATIM_TEXT,
            resolve_current_storage=True,closed_snapshot=self.proof,**options)

    def test_exact_tail_preserves_messages_source_status_and_provenance(self):
        before={name:snapshot._hash_file(self.profile/name) for name in ('state_5.sqlite','thread_history_1.sqlite')}
        candidate=self.export();review.validate_backup_history(candidate)
        turn=candidate['transfer']['threads'][0]['turns'][-1];tail=candidate['lineage'][self.tid][-1]['closed_snapshot_tail']
        self.assertEqual(('interrupted',2),(turn['status'],len(turn['messages'])))
        self.assertEqual(('inProgress',None,True),(tail['source_status_at_snapshot'],tail['source_end_ordinal'],tail['terminal_event_absent_at_eof']))
        self.assertEqual(before,{name:snapshot._hash_file(self.profile/name) for name in before})
        with closing(lineage.source._open_ro(self.profile/'thread_history_1.sqlite')) as db:
            self.assertEqual(('inProgress',None),tuple(db.execute('SELECT status,rollout_end_ordinal FROM thread_turns WHERE thread_id=? AND turn_id=?',(self.tid,self.turn)).fetchone()))
        self.assertIn('На момент закрытого снимка',review.render(candidate,[],synthetic_only=False))

    def test_default_export_and_restricted_contract_still_reject_open_tail(self):
        with self.assertRaises(lineage.LineageError):lineage.export_candidate(self.profile,[self.tid],text_policy=filtering.VERBATIM_TEXT,resolve_current_storage=True)
        with self.assertRaises(lineage.LineageError):lineage.export_candidate(self.profile,[self.tid],closed_snapshot=self.proof)
        with self.assertRaises(lineage.LineageError):lineage.export_candidate(self.profile,[self.tid],text_policy=filtering.VERBATIM_TEXT,closed_snapshot={})

    def test_proof_is_not_reusable_for_another_or_live_profile(self):
        with self.assertRaises(ValueError):self.proof.verify(self.original)
        with self.assertRaises(ValueError):closed.seal(self.original,original_profile=self.original,guard=lambda:None)
        with self.assertRaises(ValueError):closed.seal(self.root/'.codex',original_profile=self.original,guard=lambda:None)
        with self.assertRaises(ValueError):closed.ClosedSnapshot(None,self.profile,{})

    def test_mutation_before_or_during_export_is_rejected(self):
        (self.profile/'.codex-global-state.json').write_text('{}')
        with self.assertRaises(ValueError):self.export()

    def test_mutation_during_export_is_rejected(self):
        original=filtering.visible_message;changed=[]
        def mutate(*args,**kwargs):
            result=original(*args,**kwargs)
            if not changed:
                changed.append(True)
                path=self.profile/'.codex-global-state.json';path.write_bytes(path.read_bytes()+b'\n')
            return result
        with patch.object(filtering,'visible_message',mutate),self.assertRaises((ValueError,lineage.source.SourceChangedError)):self.export()

    def test_provenance_tampering_is_rejected(self):
        candidate=self.export()
        for field,value in [('source_status_at_snapshot','completed'),('snapshot_sha256','0'*64),('message_count_preserved',999)]:
            changed=copy.deepcopy(candidate);changed['lineage'][self.tid][-1]['closed_snapshot_tail'][field]=value
            with self.subTest(field=field),self.assertRaises(review.ReviewIntegrityError):review.validate_backup_history(changed)
        changed=copy.deepcopy(candidate);del changed['closed_source_snapshot']
        with self.assertRaises(review.ReviewIntegrityError):review.validate_backup_history(changed)

    def test_unknown_writer_and_unprojected_journal_tail_are_rejected(self):
        with lineage.source._snapshot(self.profile,lineage.source.CURRENT_SCHEMA_VERSION) as (db,_,root):
            path=Path(db.execute('SELECT rollout_path FROM threads WHERE id=?',(self.tid,)).fetchone()[0])
        records=[json.loads(line) for line in path.read_bytes().splitlines()];records[0]['payload']['cli_version']='0.999-unknown'
        path.write_text(''.join(json.dumps(row)+'\n' for row in records))
        with lineage.native._connect(self.profile/'thread_history_1.sqlite') as db:db.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?',(path.stat().st_size,self.tid))
        self.proof=closed.seal(self.profile,original_profile=self.original,guard=lambda:None)
        with self.assertRaises(lineage.LineageError):self.export()

    def test_unprojected_bytes_after_eof_are_rejected_even_with_new_seal(self):
        with closing(lineage.source._open_ro(self.profile/'state_5.sqlite')) as db:
            path=Path(db.execute('SELECT rollout_path FROM threads WHERE id=?',(self.tid,)).fetchone()[0])
        with path.open('ab') as stream:stream.write(b'{"type":"event_msg","ordinal":999999,"payload":{}}\n')
        self.proof=closed.seal(self.profile,original_profile=self.original,guard=lambda:None)
        with self.assertRaises(lineage.journal.JournalError):self.export()


class WorkflowConnectionTests(unittest.TestCase):
    def test_cohort_closes_both_connections_on_return_and_exception(self):
        fixture=fixtures.LineageTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        original=lineage.source._open_ro;retained=[]
        def opened(path):
            conn=original(path);retained.append(conn);return conn
        for fail in (False,True):
            with lineage.native._connect(fixture.profile/'state_5.sqlite') as db:
                db.execute('UPDATE threads SET source=?',('future-kind' if fail else 'vscode',))
            with patch.object(lineage.source,'_open_ro',opened):
                if fail:
                    with self.assertRaises(workflow.WorkflowError):workflow._user_cohort(fixture.profile)
                else:workflow._user_cohort(fixture.profile)
            for conn in retained:
                with self.assertRaises(sqlite3.ProgrammingError):conn.execute('SELECT 1')

    def test_second_open_failure_closes_first_connection(self):
        fixture=fixtures.LineageTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        conn=lineage.source._open_ro(fixture.profile/'state_5.sqlite')
        with patch.object(lineage.source,'_open_ro',side_effect=[conn,OSError('unavailable')]),self.assertRaises(OSError):workflow._user_cohort(fixture.profile)
        with self.assertRaises(sqlite3.ProgrammingError):conn.execute('SELECT 1')


if __name__=='__main__':unittest.main()
