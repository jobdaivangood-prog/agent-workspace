import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import native_adapter as native
import rehome_history_lineage as lineage
import rehome_review as review


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rh-lineage-')
        self.root = Path(self.temp.name)
        seed = native.create_synthetic_profile(self.root / 'seed')
        bundle = native.export_profile(seed)
        base = bundle['threads'][0]
        self.parent = base['id']
        self.child, self.grandchild = str(uuid.uuid4()), str(uuid.uuid4())
        for ident in (self.child, self.grandchild):
            thread = copy.deepcopy(base)
            thread.update(id=ident, title='Branch', name='Branch', archived=ident == self.child)
            bundle['threads'].append(thread)
        self.profile = self.root / 'profile'
        native.import_profile(bundle, self.profile, {bundle['projects'][0]['id']: str(self.root)})
        with native._connect(self.profile / 'state_5.sqlite') as db:
            db.execute('ALTER TABLE threads ADD COLUMN attached_thread_parent_id TEXT')
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            db.execute('DELETE FROM thread_items')
            db.execute('DELETE FROM thread_turns')
            db.execute('DELETE FROM thread_history_projection_state')
        first_cutoff = self.make_journal(self.parent, ['PARENT_INHERITED', 'PARENT_LATER_EXCLUDED'])[0]
        child_cutoff = self.make_journal(self.child, ['CHILD_OWN'], {'thread_id': self.parent, **first_cutoff})[-1]
        self.make_journal(self.grandchild, ['GRANDCHILD_OWN'], {'thread_id': self.child, **child_cutoff})

    def tearDown(self):
        self.temp.cleanup()

    def path(self, tid):
        with native._connect(self.profile / 'state_5.sqlite', readonly=True) as db:
            return Path(db.execute('SELECT rollout_path FROM threads WHERE id=?', (tid,)).fetchone()[0])

    def make_journal(self, tid, texts, dependency=None):
        ordinal = 0 if dependency is None else dependency['end_ordinal_exclusive']
        meta = {'id': tid, 'history_mode': 'paginated'}
        if dependency:
            meta.update(history_base=dependency, forked_from_id=dependency['thread_id'],
                        forked_from_ordinal_exclusive=dependency['end_ordinal_exclusive'])
        events = [{'type': 'session_meta', 'ordinal': ordinal, 'payload': meta}]
        ordinal += 1
        cutoffs = []
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            for index, text in enumerate(texts):
                turn, user_id, agent_id = str(uuid.uuid4()), 'u-' + tid + '-' + str(index), 'a-' + tid + '-' + str(index)
                start = ordinal
                events.append({'type': 'event_msg', 'ordinal': ordinal, 'payload': {'type': 'task_started', 'turn_id': turn}})
                ordinal += 1
                native_items = [
                    {'type': 'UserMessage', 'id': user_id, 'content': [{'type': 'text', 'text': text, 'text_elements': []}]},
                    {'type': 'AgentMessage', 'id': agent_id, 'content': [{'type': 'Text', 'text': 'Ready'}], 'phase': 'final_answer'}]
                sql_items = [
                    {'type': 'userMessage', 'id': user_id, 'content': [{'type': 'text', 'text': text, 'text_elements': []}]},
                    {'type': 'agentMessage', 'id': agent_id, 'text': 'Ready', 'phase': 'final_answer'}]
                for raw, item in zip(native_items, sql_items):
                    events.append({'type': 'event_msg', 'ordinal': ordinal, 'payload': {'type': 'item_completed', 'thread_id': tid, 'turn_id': turn, 'item': raw}})
                    db.execute('INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal) VALUES(?,?,?,?,?,?,?,?)',
                               (tid, turn, item['id'], ordinal, 0, json.dumps(item), item['type'], ordinal))
                    ordinal += 1
                events.append({'type': 'event_msg', 'ordinal': ordinal, 'payload': {'type': 'task_complete', 'turn_id': turn}})
                db.execute('INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,rollout_end_ordinal,status) VALUES(?,?,?,?,?)',
                           (tid, turn, start, ordinal, 'completed'))
                ordinal += 1
                data = ''.join(json.dumps(e) + '\n' for e in events).encode()
                cutoffs.append({'end_ordinal_exclusive': ordinal, 'end_byte_offset': len(data)})
            path = self.path(tid)
            path.write_bytes(data)
            db.execute('INSERT INTO thread_history_projection_state VALUES(?,?,?)', (tid, len(data), ordinal))
        return cutoffs

    def change_header(self, tid, change):
        path = self.path(tid)
        lines = path.read_text(encoding='utf-8').splitlines()
        header = json.loads(lines[0])
        change(header['payload'])
        lines[0] = json.dumps(header)
        path.write_bytes(('\n'.join(lines) + '\n').encode('utf-8'))
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            db.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?', (path.stat().st_size, tid))

    def test_child_inherits_only_prefix_without_exporting_parent_as_chat(self):
        result = lineage.export_candidate(self.profile, [self.child])
        self.assertEqual([self.parent], result['dependency_ids'])
        self.assertEqual([self.child], [t['id'] for t in result['transfer']['threads']])
        self.assertEqual(2, len(result['transfer']['threads'][0]['turns']))
        self.assertNotIn('PARENT_LATER_EXCLUDED', json.dumps(result))
        self.assertFalse(result['publication_ready'])

    def test_nested_forks_materialize_each_selected_history_once(self):
        result = lineage.export_candidate(self.profile, [self.child, self.grandchild])
        self.assertEqual([2, 3], [len(t['turns']) for t in result['transfer']['threads']])
        self.assertEqual(3, len(result['lineage'][self.grandchild]))
        self.assertEqual([], result['transfer']['spawn_edges'])

    def test_explicit_parent_selection_keeps_parent_later_messages_only_there(self):
        result = lineage.export_candidate(self.profile, [self.parent, self.child])
        self.assertIn('PARENT_LATER_EXCLUDED', json.dumps(result['transfer']['threads'][0]))
        self.assertNotIn('PARENT_LATER_EXCLUDED', json.dumps(result['transfer']['threads'][1]))

    def test_missing_parent_and_conflicting_reference_stop(self):
        self.change_header(self.child, lambda m: m.update(forked_from_id=str(uuid.uuid4())))
        with self.assertRaisesRegex(lineage.LineageError, 'Conflicting'):
            lineage.export_candidate(self.profile, [self.child])

    def test_proven_writer_separates_typed_provenance_from_storage_base(self):
        # Fork C from B at inherited A turn: physical prefix points at A;
        # provenance names B. Do not read B's own later turn as inherited C.
        with native._connect(self.profile / 'thread_history_1.sqlite', readonly=True) as db:
            cutoff = dict(db.execute('SELECT next_rollout_byte_offset AS end_byte_offset,next_rollout_ordinal AS end_ordinal_exclusive FROM thread_history_projection_state WHERE thread_id=?',
                                     (self.parent,)).fetchone())
        parent_first = json.loads(self.path(self.child).read_bytes().splitlines()[0])['payload']['history_base']
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            db.execute('DELETE FROM thread_items WHERE thread_id=?', (self.grandchild,))
            db.execute('DELETE FROM thread_turns WHERE thread_id=?', (self.grandchild,))
            db.execute('DELETE FROM thread_history_projection_state WHERE thread_id=?', (self.grandchild,))
        self.make_journal(self.grandchild, ['GRANDCHILD_OWN'], parent_first)
        self.change_header(self.grandchild, lambda meta: meta.update(cli_version='0.166.0-alpha.5.1', forked_from_id=self.child))
        result = lineage.export_candidate(self.profile, [self.grandchild])
        texts = [message['text'] for turn in result['transfer']['threads'][0]['turns'] for message in turn['messages']]
        self.assertNotIn('CHILD_OWN', texts); self.assertNotIn('PARENT_LATER_EXCLUDED', texts)
        roles = result['lineage'][self.grandchild][-1]['fork_reference_roles']
        self.assertEqual(roles['provenance_thread_id'], self.child)
        self.assertEqual(roles['storage_base_rollout_id'], self.parent)
        self.assertFalse(roles['provenance_is_history_dependency'])
        review._validate_candidate(result)
        self.change_header(self.grandchild, lambda meta: meta.update(forked_from_ordinal_exclusive=1))
        with self.assertRaisesRegex(lineage.LineageError, 'Conflicting'): lineage.export_candidate(self.profile, [self.grandchild])

    def test_unknown_writer_still_rejects_split_fork_reference(self):
        self.change_header(self.child, lambda meta: meta.update(cli_version='0.999-unknown', forked_from_id=self.grandchild))
        with self.assertRaisesRegex(lineage.LineageError, 'Conflicting'): lineage.export_candidate(self.profile, [self.child])

    def test_terminal_only_interrupted_turn_requires_exact_zero_item_sql_boundary(self):
        tid=self.grandchild; path=self.path(tid); header=json.loads(path.read_bytes().splitlines()[0])
        header['payload']['cli_version']='0.165.0-alpha.1.2'
        turn=str(uuid.uuid4()); ordinal=header['ordinal']+1
        event={'type':'event_msg','ordinal':ordinal,'payload':{'type':'turn_aborted','turn_id':turn,'reason':'interrupted'}}
        data=''.join(json.dumps(row)+'\n' for row in (header,event)).encode(); path.write_bytes(data)
        with native._connect(self.profile/'thread_history_1.sqlite') as db:
            db.execute('DELETE FROM thread_items WHERE thread_id=?',(tid,));db.execute('DELETE FROM thread_turns WHERE thread_id=?',(tid,))
            db.execute('INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,rollout_end_ordinal,status) VALUES(?,?,?,?,?)',
                       (tid,turn,ordinal,ordinal,'interrupted'))
            db.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?',
                       (len(data),ordinal+1,tid))
        result=lineage.export_candidate(self.profile,[tid]); last=result['transfer']['threads'][0]['turns'][-1]
        self.assertEqual(last,{'id':turn,'status':'interrupted','messages':[]});review._validate_candidate(result)
        with native._connect(self.profile/'thread_history_1.sqlite') as db:
            db.execute('UPDATE thread_turns SET rollout_ordinal=? WHERE thread_id=?',(ordinal-1,tid))
        with self.assertRaisesRegex(lineage.LineageError,'interrupted-turn'):lineage.export_candidate(self.profile,[tid])

    def test_dependency_physical_alias_ignores_newer_current_logical_pointer(self):
        physical_id=str(uuid.uuid4()); parent_path=self.path(self.parent)
        old_path=parent_path.with_name('rollout-2030-01-01T00-00-00-'+self.parent+'_'+physical_id+'.jsonl')
        parent_path.rename(old_path)
        current_id=str(uuid.uuid4()); newer=parent_path.with_name('rollout-2030-01-01T00-00-01-'+self.parent+'_'+current_id+'.jsonl')
        newer.write_bytes(old_path.read_bytes().replace(b'PARENT_INHERITED',b'PARENT_CURRENT_DIFFERENT'))
        with native._connect(self.profile/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(newer),self.parent))
        with native._connect(self.profile/'thread_history_1.sqlite') as db:
            for table in ('thread_items','thread_turns','thread_history_projection_state'):
                db.execute('UPDATE '+table+' SET thread_id=? WHERE thread_id=?',(physical_id,self.parent))
        self.change_header(self.child,lambda meta:meta.update(cli_version='0.165.0-alpha.1.2',
            history_base={**meta['history_base'],'thread_id':physical_id}))
        result=lineage.export_candidate(self.profile,[self.child]);review._validate_candidate(result)
        texts=[m['text'] for t in result['transfer']['threads'][0]['turns'] for m in t['messages']]
        self.assertIn('PARENT_INHERITED',texts);self.assertNotIn('PARENT_CURRENT_DIFFERENT',texts)
        binding=result['lineage'][self.child][0]['storage_binding']
        self.assertEqual(binding['physical_rollout_id'],physical_id);self.assertEqual(binding['logical_session_id'],self.parent)

    def test_dependency_uuid_that_is_state_id_still_uses_old_physical_file(self):
        old=self.path(self.parent)
        newer=old.with_name('rollout-2030-01-01T00-00-01-'+self.parent+'_'+str(uuid.uuid4())+'.jsonl')
        newer.write_bytes(old.read_bytes().replace(b'PARENT_INHERITED',b'PARENT_CURRENT_DIFFERENT'))
        with native._connect(self.profile/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(newer),self.parent))
        result=lineage.export_candidate(self.profile,[self.child]);review._validate_candidate(result)
        texts=[m['text'] for t in result['transfer']['threads'][0]['turns'] for m in t['messages']]
        self.assertIn('PARENT_INHERITED',texts);self.assertNotIn('PARENT_CURRENT_DIFFERENT',texts)

    def test_absent_parent_stops(self):
        absent = str(uuid.uuid4())
        def change(m):
            m['history_base']['thread_id'] = absent
            m['forked_from_id'] = absent
        self.change_header(self.child, change)
        with self.assertRaisesRegex(lineage.LineageError, 'Missing'):
            lineage.export_candidate(self.profile, [self.child])

    def test_byte_cutoff_inside_record_stops(self):
        self.change_header(self.child, lambda m: m['history_base'].update(end_byte_offset=m['history_base']['end_byte_offset'] - 1))
        with self.assertRaisesRegex(lineage.LineageError, 'cutoff'):
            lineage.export_candidate(self.profile, [self.child])

    def test_sql_message_rewrite_without_journal_change_stops(self):
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            row = db.execute("SELECT item_json FROM thread_items WHERE thread_id=? AND item_type='userMessage' ORDER BY rollout_ordinal LIMIT 1", (self.parent,)).fetchone()
            item = json.loads(row[0]); item['content'][0]['text'] = 'CHANGED'
            db.execute('UPDATE thread_items SET item_json=? WHERE thread_id=? AND item_id=?', (json.dumps(item), self.parent, item['id']))
        with self.assertRaisesRegex(lineage.LineageError, 'content differs'):
            lineage.export_candidate(self.profile, [self.child])

    def test_missing_sql_message_and_turn_status_stop(self):
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            db.execute("DELETE FROM thread_items WHERE thread_id=? AND item_type='agentMessage'", (self.child,))
        with self.assertRaisesRegex(lineage.LineageError, 'counts differ'):
            lineage.export_candidate(self.profile, [self.child])

    def test_dependency_cycle_is_bounded(self):
        original = lineage.segment
        def cyclic(conn, root, tid, cutoff=None, *, decoder=None, **options):
            value = original(conn, root, tid, cutoff, decoder=decoder, **options)
            if tid == self.parent:
                value['dependency'] = {'thread_id': self.child, 'end_ordinal_exclusive': 1, 'end_byte_offset': 1}
            return value
        with patch.object(lineage, 'segment', cyclic):
            with self.assertRaisesRegex(lineage.LineageError, 'Cyclic'):
                lineage.export_candidate(self.profile, [self.child])

    def test_unknown_completed_item_does_not_disappear_silently(self):
        with self.assertRaisesRegex(lineage.LineageError, 'Unknown native'):
            lineage._raw_visible({'type': 'FutureVisibleCard', 'id': 'future'})

    def test_typed_service_extensions_only_for_proven_writer_and_subtypes(self):
        for kind in ('web.search','clock.sleep','image_gen.generation'):
            self.assertIsNone(lineage._raw_visible({'type':'Extension','kind':kind,'payload':'PRIVATE_TOOL_SENTINEL'},
                                                  writer_version='0.165.0-alpha.1.2'))
        for item,writer in (({'type':'Extension','kind':'future.interactive.card'},'0.165.0-alpha.1.2'),
                            ({'type':'Extension','kind':'web.search'},'0.999-unknown'),
                            ({'type':'Extension'},'0.165.0-alpha.1.2')):
            with self.assertRaises(lineage.LineageError):lineage._raw_visible(item,writer_version=writer)

    def test_optional_native_client_identity_is_checked(self):
        item = {'type': 'UserMessage', 'id': 'user', 'content': []}
        self.assertIsNone(lineage._raw_visible(item)['clientId'])
        self.assertEqual('client', lineage._raw_visible({**item, 'client_id': 'client'})['clientId'])
        with self.assertRaisesRegex(lineage.LineageError, 'client-message'):
            lineage._raw_visible({**item, 'client_id': ['client']})
        with self.assertRaisesRegex(lineage.LineageError, 'Unknown native'):
            lineage._raw_visible({**item, 'future': 'unexpected'})

    def test_native_client_identity_matches_sql_but_is_not_message_text(self):
        path = self.path(self.child)
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        item = next(e['payload']['item'] for e in records
                    if e['payload'].get('type') == 'item_completed'
                    and e['payload']['item']['type'] == 'UserMessage')
        item['client_id'] = 'CLIENT_METADATA_SENTINEL'
        data = ''.join(json.dumps(e) + '\n' for e in records).encode('utf-8')
        path.write_bytes(data)
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            db.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?',
                       (len(data), self.child))
            sql = json.loads(db.execute('SELECT item_json FROM thread_items WHERE thread_id=? AND item_id=?',
                                       (self.child, item['id'])).fetchone()[0])
            sql['clientId'] = item['client_id']
            db.execute('UPDATE thread_items SET item_json=? WHERE thread_id=? AND item_id=?',
                       (json.dumps(sql), self.child, item['id']))
        candidate = lineage.export_candidate(self.profile, [self.child])
        self.assertNotIn('CLIENT_METADATA_SENTINEL', json.dumps(candidate))
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            sql['clientId'] = 'different'
            db.execute('UPDATE thread_items SET item_json=? WHERE thread_id=? AND item_id=?',
                       (json.dumps(sql), self.child, item['id']))
        with self.assertRaisesRegex(lineage.LineageError, 'content differs'):
            lineage.export_candidate(self.profile, [self.child])

    def test_realtime_items_require_explicit_handling(self):
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            db.execute('INSERT INTO thread_realtime_items VALUES(?,?,?,?,?,?)',
                       (self.child, 'realtime', 7, 0, 'audio', '{}'))
        with self.assertRaisesRegex(lineage.LineageError, 'Realtime'):
            lineage.export_candidate(self.profile, [self.child])

    def rewrite_terminal(self, tid, status):
        path = self.path(tid)
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        event = next(e for e in reversed(records) if e['type'] == 'event_msg' and e['payload'].get('type') == 'task_complete')
        if status == 'failed':
            event['payload']['error'] = {'message': 'PRIVATE_ERROR_SENTINEL', 'codex_error_info': 'other'}
        else:
            event['payload'].update(type='turn_aborted', reason='interrupted')
        path.write_bytes(''.join(json.dumps(r) + '\n' for r in records).encode('utf-8'))
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            db.execute('UPDATE thread_turns SET status=? WHERE thread_id=? AND turn_id=?', (status, tid, event['payload']['turn_id']))
            db.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?', (path.stat().st_size, tid))

    def test_failed_and_interrupted_statuses_preserved_without_error_text(self):
        self.rewrite_terminal(self.parent, 'failed')
        self.rewrite_terminal(self.child, 'interrupted')
        result = lineage.export_candidate(self.profile, [self.parent, self.child])
        self.assertEqual(['completed', 'failed'], [t['status'] for t in result['transfer']['threads'][0]['turns']])
        self.assertEqual(['completed', 'interrupted'], [t['status'] for t in result['transfer']['threads'][1]['turns']])
        self.assertNotIn('PRIVATE_ERROR_SENTINEL', json.dumps(result))
        self.assertEqual(64, len(result['source_instance_id']))
        self.assertIn('Прерван', review.render(result, []))

    def test_conflicting_terminal_status_blocks(self):
        self.rewrite_terminal(self.child, 'failed')
        with native._connect(self.profile / 'thread_history_1.sqlite') as db:
            db.execute("UPDATE thread_turns SET status='completed' WHERE thread_id=?", (self.child,))
        with self.assertRaisesRegex(lineage.LineageError, 'boundaries differ'):
            lineage.export_candidate(self.profile, [self.child])

    def test_existing_selected_spawn_edges_preserved(self):
        with native._connect(self.profile / 'state_5.sqlite') as db:
            db.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', (self.child, self.grandchild, 'completed'))
            db.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', (self.parent, self.child, 'completed'))
        result = lineage.export_candidate(self.profile, [self.child, self.grandchild])
        self.assertEqual([{'parent': self.child, 'child': self.grandchild}], result['transfer']['spawn_edges'])
        self.assertEqual([{'parent': self.parent, 'child': self.child}], result['excluded_spawn_edges'])

    def test_late_parent_text_is_not_parsed(self):
        # A valid inherited prefix remains usable even if an unrelated later
        # parent record is malformed. The parent's full chat would be blocked.
        path = self.path(self.parent)
        data = path.read_bytes()
        old = b'PARENT_LATER_EXCLUDED'
        path.write_bytes(data.replace(old, b'\xff' * len(old)))
        result = lineage.export_candidate(self.profile, [self.child])
        self.assertEqual(2, len(result['transfer']['threads'][0]['turns']))
        with self.assertRaises(lineage.LineageError):
            lineage.export_candidate(self.profile, [self.parent])

    def test_review_escapes_text_and_has_no_active_publishing(self):
        with native._connect(self.profile / 'state_5.sqlite') as db:
            db.execute('UPDATE threads SET name=? WHERE id=?', ('<script>alert(1)</script>', self.child))
        candidate = lineage.export_candidate(self.profile, [self.child])
        page = review.render(candidate, [])
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn("default-src 'none'", page)
        self.assertNotIn('PARENT_LATER_EXCLUDED', page)

    def test_preview_pins_results_without_copying_or_changing_source(self):
        final = self.root / 'Latest.txt'; final.write_text('Finished result', encoding='utf-8')
        output = self.root / 'preview'
        result = review.prepare(self.profile, [self.child], output,
                                ready_results=[{'path': str(final), 'display_name': 'Results/Latest.txt'}])
        self.assertEqual('local_review_prepared', result['status'])
        self.assertFalse(result['manifest']['publication_ready'])
        self.assertFalse((output / 'Latest.txt').exists())
        with self.assertRaises(ValueError):
            review.prepare(self.profile, [self.child], output)

    def test_preview_rejects_authorization_profile_and_unsafe_result_name(self):
        final = self.root / 'Latest.txt'; final.write_text('Finished result', encoding='utf-8')
        with self.assertRaises(ValueError):
            review.prepare(self.profile, [self.child], self.root / 'preview',
                           ready_results=[{'path': str(final), 'display_name': '../escape.txt'}])
        (self.profile / 'auth.json').write_text('FAKE_AUTH', encoding='utf-8')
        with self.assertRaises(Exception):
            review.prepare(self.profile, [self.child], self.root / 'preview')


if __name__ == '__main__':
    unittest.main()
