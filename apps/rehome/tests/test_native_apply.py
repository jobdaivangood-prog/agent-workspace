import hashlib
import json
from pathlib import Path
import queue
import unittest
from unittest.mock import patch

import native_adapter as native
import environment_adapter as environment
import rehome_native_apply as apply
import rehome_apply_transaction as transaction
import rehome_backup_workflow as workflow
import rehome_native_staging as staging
import snapshot
from tests import test_native_staging as fixtures


class AbruptExit(BaseException):pass


class NativeApplyTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.NativeStagingTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root
        self.target=self.root/'real-user/.codex';self.target.mkdir(parents=True)
        (self.target/'auth.json').write_text('LOCAL_AUTH_MUST_STAY')
        (self.target/'config.toml').write_text('EXISTING_MCP_MUST_STAY')
        (self.target/'AGENTS.md').write_text('EXISTING_INSTRUCTIONS_MUST_STAY')
        (self.target/'state_5.sqlite').write_bytes(b'ORIGINAL_STATE')
        (self.target/'state_5.sqlite-wal').write_bytes(b'ORIGINAL_WAL')
        self.stage=self.fixture.prepare()

    def candidate(self):
        return apply.prepare(self.stage['job'],self.root/'Apply',expected_digest=self.stage['backup_manifest_sha256'],
            expected_stage_digest=self.stage['stage_digest'],target_profile=self.target,projects_root=self.root/'RestoredProjects')

    def prepared(self):
        candidate=self.candidate()
        prepared=transaction.prepare(candidate['candidate'],self.root/'Transaction',
            expected_apply_digest=candidate['apply_digest'],guard=lambda:None)
        return candidate,prepared

    def test_candidate_uses_final_paths_without_changing_sealed_stage(self):
        before=staging._inventory(Path(self.stage['job']))
        result=self.candidate()
        self.assertFalse(result['profile_applied'])
        apply.verify(result['candidate'],expected_apply_digest=result['apply_digest'])
        with native._connect(Path(result['candidate'])/'Profile/state_5.sqlite',readonly=True) as db:
            paths=[r[0] for r in db.execute('SELECT rollout_path FROM threads')]
        self.assertTrue(all(Path(p).is_relative_to(self.target) for p in paths))
        self.assertEqual(before,staging._inventory(Path(self.stage['job'])))
        self.assertEqual(b'ORIGINAL_STATE',(self.target/'state_5.sqlite').read_bytes())

    def test_wrong_pin_existing_project_or_changed_candidate_prevents_transaction(self):
        result=self.candidate()
        with self.assertRaises(ValueError):apply.verify(result['candidate'],expected_apply_digest='0'*64)
        plan=json.loads((Path(result['candidate'])/apply.PLAN).read_text())
        Path(next(iter(plan['project_roots'].values()))).mkdir(parents=True)
        with self.assertRaises(ValueError):
            transaction.prepare(result['candidate'],self.root/'Transaction',expected_apply_digest=result['apply_digest'],guard=lambda:None)
        self.assertFalse((self.root/'Transaction').exists())

    def test_apply_and_rollback_preserve_auth_config_instructions_and_new_changes(self):
        _,receipt=self.prepared()
        transaction.activate(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=lambda:None)
        self.assertNotEqual(b'ORIGINAL_STATE',(self.target/'state_5.sqlite').read_bytes())
        (self.target/'state_5.sqlite').write_bytes(b'NEW_LOCAL_STATE')
        (self.target/'state_5.sqlite-wal').write_bytes(b'NEW_LOCAL_WAL')
        result=transaction.rollback(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=lambda:None)
        self.assertEqual('rolled_back',result['status'])
        self.assertEqual(b'ORIGINAL_STATE',(self.target/'state_5.sqlite').read_bytes())
        self.assertEqual(b'ORIGINAL_WAL',(self.target/'state_5.sqlite-wal').read_bytes())
        preserved=[p.read_bytes() for p in (Path(receipt['job'])/'Changed').rglob('*') if p.is_file()]
        self.assertIn(b'NEW_LOCAL_STATE',preserved)
        self.assertEqual('LOCAL_AUTH_MUST_STAY',(self.target/'auth.json').read_text())
        self.assertEqual('EXISTING_MCP_MUST_STAY',(self.target/'config.toml').read_text())
        self.assertEqual('EXISTING_INSTRUCTIONS_MUST_STAY',(self.target/'AGENTS.md').read_text())

    def test_failure_at_each_move_automatically_restores_original(self):
        # Exercise every rename edge with a separate owned transaction.
        for fail_at in range(1,11):
            with self.subTest(fail_at=fail_at):
                fixture=NativeApplyTests();fixture.setUp()
                try:
                    _,receipt=fixture.prepared();original=transaction._rename_no_replace;counter=[0]
                    def rename(source,target):
                        counter[0]+=1
                        if counter[0]==fail_at:raise OSError('Injected move failure')
                        return original(source,target)
                    with patch.object(transaction,'_rename_no_replace',side_effect=rename):
                        try:transaction.activate(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=lambda:None)
                        except OSError:pass
                    transaction.rollback(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=lambda:None)
                    self.assertEqual(b'ORIGINAL_STATE',(fixture.target/'state_5.sqlite').read_bytes())
                    self.assertEqual(b'ORIGINAL_WAL',(fixture.target/'state_5.sqlite-wal').read_bytes())
                finally:fixture.doCleanups()

    def test_restart_after_rename_before_completed_journal_recovers_from_intent(self):
        _,receipt=self.prepared();original=transaction._atomic;raised=[]
        def save(path,data):
            if data.get('phase')=='applying' and len(data.get('completed_moves',[]))==1 and not raised:
                raised.append(True);raise AbruptExit()
            return original(path,data)
        with patch.object(transaction,'_atomic',side_effect=save):
            with self.assertRaises(AbruptExit):
                transaction.activate(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=lambda:None)
        transaction.rollback(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=lambda:None)
        self.assertEqual(b'ORIGINAL_STATE',(self.target/'state_5.sqlite').read_bytes())

    def test_new_writer_or_cancel_before_move_prevents_activation(self):
        _,receipt=self.prepared()
        def blocked():raise ValueError('new writer')
        with self.assertRaises(ValueError):
            transaction.activate(receipt['job'],transaction_digest=receipt['transaction_digest'],guard=blocked)
        self.assertEqual(b'ORIGINAL_STATE',(self.target/'state_5.sqlite').read_bytes())

    def test_stable_source_copies_only_native_databases_and_journals(self):
        profile=self.fixture.fixture.profile
        (profile/'auth.json').write_text('NO_AUTH_IN_STABLE_COPY')
        (profile/'input.docx').write_text('NO_ORIGINAL_IN_STABLE_COPY')
        destination=self.root/'Stable'
        workflow.stable_source(profile,destination,lambda:None)
        self.assertFalse((destination/'auth.json').exists())
        self.assertFalse((destination/'input.docx').exists())
        with native._connect(destination/'state_5.sqlite',readonly=True) as db:
            paths=[r[0] for r in db.execute('SELECT rollout_path FROM threads')]
        self.assertTrue(all(Path(p).is_relative_to(destination) and Path(p).is_file() for p in paths))

    def _two_group_environment(self):
        stage=Path(self.stage['job'])
        files=[];groups=[];payloads={}
        for component,folder,content in (
            ('user-skills','skill-one',b'first environment resource'),
            ('agents-skills','skill-two',b'second environment resource')):
            relative=folder+'/data.txt'
            digest=hashlib.sha256(content).hexdigest()
            entry={'path':'payload/'+component+'/'+relative,'relative_path':relative,
                'component':component,'size':len(content),'sha256':digest,'unresolved_bindings':[],
                'rebindings':[],'original_size':len(content),'original_sha256':digest,'tokens':[]}
            files.append(entry)
            payloads[entry['path']]=content
            target=(self.target/'skills'/folder if component=='user-skills'
                    else self.target.parent/'.agents'/'skills'/folder)
            groups.append((target,[(entry,'data.txt')]))
        inventory={'token_namespace':'test-token-namespace','files':files}
        return stage,groups,inventory,payloads

    def test_verify_reuses_environment_groups_and_inventory_but_still_rejects_tampering(self):
        stage,groups,inventory,payloads=self._two_group_environment()
        def group_lookup(*_args):return groups,0,inventory
        def read_payload(path,_max_file):return payloads[Path(path).as_posix().split('/Environment/',1)[-1].replace('\\','/')]
        with patch.object(apply,'_environment_groups',side_effect=group_lookup), \
             patch.object(environment,'_read_stable',side_effect=read_payload):
            candidate=apply.prepare(stage,self.root/'ApplyWithEnvironment',
                expected_digest=self.stage['backup_manifest_sha256'],expected_stage_digest=self.stage['stage_digest'],
                target_profile=self.target,projects_root=self.root/'RestoredProjects')

        group_calls=[]
        def counted_groups(*_args):
            group_calls.append(True)
            return groups,0,inventory
        with patch.object(apply,'_environment_groups',side_effect=counted_groups), \
             patch.object(environment,'_read_stable',side_effect=read_payload):
            apply.verify(candidate['candidate'],expected_apply_digest=candidate['apply_digest'])
        self.assertEqual(1,len(group_calls))

        root=Path(candidate['candidate'])
        plan=json.loads((root/apply.PLAN).read_text(encoding='utf-8'))
        environment_change=next(change for change in plan['changes'] if change['kind']=='environment')
        payload=root/environment_change['candidate']/'data.txt'
        payload.write_bytes(b'tampered environment resource')
        ready_path=root/apply.READY
        ready={'format':'rehome-native-apply-ready/1','files':apply._inventory(root)}
        ready_path.write_text(native._json(ready),encoding='utf-8')
        resealed_digest=snapshot._hash_file(ready_path)[0]
        group_calls=[]
        def counted_groups(*_args):
            group_calls.append(True)
            return groups,0,inventory
        with patch.object(apply,'_environment_groups',side_effect=counted_groups), \
             patch.object(environment,'_read_stable',side_effect=read_payload):
            with self.assertRaisesRegex(ValueError,'Apply environment resource differs'):
                apply.verify(root,expected_apply_digest=resealed_digest)
        self.assertEqual(1,len(group_calls))


if __name__=='__main__':unittest.main()
