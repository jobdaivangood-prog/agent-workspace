"""Read-only, bounded fork-prefix reconstruction for the pinned native format.

Parents are dependencies, not implicitly selected chats. Only the inherited
prefix is read. Raw instructions/tool data are never returned or serialized.
Accepts completed/failed/interrupted native turns with item_completed projection;
unknown turn protocols, legacy rollouts and attached-agent histories stop.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import uuid

import native_adapter as native
import readonly_source as source
import rehome_history_boundary as boundary
import rehome_history_filter as filtering
import rehome_journal as journal


class LineageError(ValueError):
    pass


# Exact official 0.165 tag source separates requested-fork provenance from the
# physical history_base rollout; native 0.166 inherited-turn forks reproduce it.
# This gate does not relax ordinal, byte-boundary, projection or source checks.
SPLIT_FORK_REFERENCE_WRITERS = frozenset(('0.165.0-alpha.1.2', '0.166.0-alpha.5.1'))
TYPED_VISIBLE_WRITERS = SPLIT_FORK_REFERENCE_WRITERS | {'0.164.0-alpha.11.1'}
# Separate protocol-feature gates: adding a version to one does not grant
# support for its other historical/fork/tail semantics.
LEGACY_SERVICE_WRITERS = frozenset(('0.162.0-alpha.17.2','0.163.0-alpha.10.2',
                                  '0.163.0-alpha.11','0.163.0-alpha.11.1','0.163.0-alpha.11.2'))
LEGACY_CARD_WRITERS = frozenset(('0.162.0-alpha.17.2','0.163.0-alpha.10.2',
                               '0.163.0-alpha.11','0.163.0-alpha.11.1','0.163.0-alpha.11.2'))
LEGACY_SPLIT_REFERENCE_WRITERS = frozenset(('0.163.0-alpha.10.2',))
FROZEN_PREFIX_WRITERS = TYPED_VISIBLE_WRITERS | {'0.162.0-alpha.17.2','0.163.0-alpha.10.2','0.163.0-alpha.11','0.163.0-alpha.11.1','0.163.0-alpha.11.2'}
SEQUENTIAL_OPEN_WRITERS = TYPED_VISIBLE_WRITERS | {'0.163.0-alpha.10.2','0.163.0-alpha.11','0.163.0-alpha.11.1','0.163.0-alpha.11.2'}
CLOSED_TAIL_WRITERS = TYPED_VISIBLE_WRITERS | {'0.163.0-alpha.10.2'}


def _uuid(value):
    if not isinstance(value, str):
        raise LineageError('Invalid dependency identity')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise LineageError('Invalid dependency identity') from None
    return value


def _base(meta):
    base = meta.get('history_base')
    if base is None:
        if meta.get('forked_from_ordinal_exclusive') is not None:
            raise LineageError('Fork cutoff without an exact byte boundary is unsupported')
        # Provenance-only forks have a self-contained transcript.
        if meta.get('forked_from_id') is not None:
            _uuid(meta['forked_from_id'])
        return None
    if not isinstance(base, dict) or set(base) != {'thread_id', 'end_ordinal_exclusive', 'end_byte_offset'}:
        raise LineageError('Unknown fork dependency metadata')
    ident = _uuid(base['thread_id'])
    for key in ('end_ordinal_exclusive', 'end_byte_offset'):
        if type(base[key]) is not int or base[key] <= 0:
            raise LineageError('Invalid fork cutoff')
    provenance = meta.get('forked_from_id', ident)
    _uuid(provenance)
    if meta.get('forked_from_ordinal_exclusive', base['end_ordinal_exclusive']) != base['end_ordinal_exclusive']:
        raise LineageError('Conflicting fork references')
    if provenance != ident and meta.get('cli_version') not in (SPLIT_FORK_REFERENCE_WRITERS | LEGACY_SPLIT_REFERENCE_WRITERS):
        raise LineageError('Conflicting fork references')
    return dict(base)


def _raw_visible(item, *, writer_version=None):
    if not isinstance(item, dict):
        raise LineageError('Malformed native item')
    kind = item.get('type')
    if kind == 'UserMessage':
        if set(item) - {'type', 'id', 'content', 'client_id'}:
            raise LineageError('Unknown native user-message metadata')
        client_id = item.get('client_id')
        if client_id is not None and not isinstance(client_id, str):
            raise LineageError('Invalid native client-message identity')
        return {'type': 'userMessage', 'id': item.get('id'), 'content': _user_content(item.get('content')),
                'clientId': client_id}
    if kind == 'AgentMessage':
        if set(item) - {'type', 'id', 'content', 'phase','memory_citation','delivery','questions'}:
            raise LineageError('Unknown native assistant metadata')
        content = item.get('content')
        if not isinstance(content, list) or any(not isinstance(p, dict) or set(p) != {'type', 'text'} or p['type'] != 'Text' or not isinstance(p['text'], str) for p in content):
            raise LineageError('Unsupported native assistant content')
        phase = item.get('phase')
        phase = {'Commentary': 'commentary', 'FinalAnswer': 'final_answer'}.get(phase, phase)
        normalized={'type':'agentMessage','id':item.get('id'),'text':''.join(p['text'] for p in content),'phase':phase,
            'memoryCitation':item.get('memory_citation'),'delivery':item.get('delivery'),'questions':item.get('questions')}
        if (normalized['delivery'] is not None or normalized['questions'] is not None) and writer_version not in (TYPED_VISIBLE_WRITERS | LEGACY_CARD_WRITERS):
            raise LineageError('Async assistant metadata writer requires a decoder')
        normalized.update(filtering.agent_metadata(normalized))
        return normalized
    if kind == 'Extension':
        if writer_version in (TYPED_VISIBLE_WRITERS | LEGACY_SERVICE_WRITERS) and item.get('kind') in ('web.search','clock.sleep','image_gen.generation'):
            return None
        raise LineageError('Unknown native extension item requires a decoder')
    if kind in ('Reasoning', 'CommandExecution', 'FileChange', 'McpToolCall',
                'WebSearch', 'ImageView', 'ImageGeneration', 'CollabAgentToolCall',
                'SubAgentActivity', 'Sleep', 'ContextCompaction', 'FunctionCallOutput'):
        return None
    raise LineageError('Unknown native completed-item type requires a decoder')


def _user_content(content):
    """Exact core snake_case -> public camelCase attachment projection defaults.

    Reference fields are compared in memory, then the existing visible filter
    omits the known supplied attachment. No attachment payload is serialized.
    """
    if not isinstance(content,list):raise LineageError('Unsupported user content')
    normalized=[]
    for block in content:
        if not isinstance(block,dict):raise LineageError('Unsupported user content block')
        kind=block.get('type')
        if kind=='text':
            if set(block)-{'type','text','text_elements'}:raise LineageError('Unknown user text metadata')
            normalized.append({'type':'text','text':block.get('text'),'text_elements':block.get('text_elements',[])})
        elif kind in ('local_image','localImage'):
            if set(block)-{'type','path','detail'} or not isinstance(block.get('path'),str):raise LineageError('Unknown local image metadata')
            normalized.append({'type':'localImage','path':block['path'],'detail':block.get('detail')})
        elif kind=='image':
            if set(block)-{'type','image_url','url','file_id','detail'}:raise LineageError('Unknown image metadata')
            refs=[key for key in ('image_url','url','file_id') if key in block]
            if len(refs)!=1 or not isinstance(block[refs[0]],str):raise LineageError('Unknown image reference')
            normalized.append({'type':'image','url' if refs[0]=='image_url' else refs[0]:block[refs[0]],'detail':block.get('detail')})
        else:raise LineageError('Unknown visible user content variant')
    return normalized


def _comparable(item):
    if item.get('type') == 'userMessage':
        return {**{k:item.get(k) for k in ('type','id','clientId')},'content':_user_content(item.get('content'))}
    return {**{k:item.get(k) for k in ('type','id','text','phase')},**filtering.agent_metadata(item)}


def historical_open_counts(conn,projection_key):
    """SQL-only candidates for historical-open versus active/unsupported tail.

    Does not approve their histories. The journal reader still verifies each
    writer, event, source status, noninterleaving and frozen prefix exactly.
    """
    rows=list(conn.execute('SELECT turn_id,status,rollout_ordinal,rollout_end_ordinal FROM history.thread_turns WHERE thread_id=? ORDER BY rollout_ordinal',(projection_key,)))
    historical,active=0,0
    for index,row in enumerate(rows):
        if row['status']!='inProgress':continue
        following=None if index+1>=len(rows) else rows[index+1]
        last=conn.execute('SELECT MAX(rollout_ordinal),MAX(updated_at_ordinal) FROM history.thread_items WHERE thread_id=? AND turn_id=?',(projection_key,row['turn_id'])).fetchone()
        supported=(row['rollout_end_ordinal'] is None and following is not None and
            following['turn_id']!=row['turn_id'] and following['rollout_ordinal']>row['rollout_ordinal'] and
            following['status'] in native.TERMINAL_TURN_STATUSES and type(following['rollout_end_ordinal']) is int and
            following['rollout_end_ordinal']>=following['rollout_ordinal'] and
            all(value is None or value<following['rollout_ordinal'] for value in last))
        if supported:historical+=1
        else:active+=1
    return {'historical_open_count':historical,'active_tail_count':active}


def current_storage_id(conn, root, logical_id, *, decoder=None):
    """Bind a current logical pointer to its exact physical projection key."""
    row=conn.execute('SELECT rollout_path FROM threads WHERE id=?',(logical_id,)).fetchone()
    if row is None:raise LineageError('Selected chat is missing')
    logical,physical,_=journal.resolve_journal(root,row[0])
    match=re.search(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$',logical.name)
    if match is None:raise LineageError('Current storage filename has no exact identity')
    key=match[1]
    if key==logical_id:return key
    import rehome_rollout_resolver as resolver
    resolved=resolver.resolve(root,key,journal_decoder=decoder)
    if (resolved['logical_session_id']!=logical_id or
            root/resolved['relative_physical_path']!=physical):
        raise LineageError('Current storage owner or pointer differs')
    return key


def segment(conn, root, tid, cutoff=None, *, decoder=None, head_owner=None, closed_snapshot=None,backup_records=False):
    """Read exactly one local segment/prefix, compare visible projection rows."""
    _uuid(tid)
    storage_binding = None
    if cutoff is not None or head_owner is not None:
        # Dependencies name immutable physical rollout IDs, never a logical
        # thread's possibly newer current rollout_path pointer.
        import rehome_rollout_resolver as resolver
        try: resolved = resolver.resolve(root, tid, journal_decoder=decoder)
        except resolver.RolloutResolutionError:
            raise LineageError('Missing or unsupported storage dependency') from None
        expected_owner = resolved['logical_session_id']
        if head_owner is not None and (cutoff is not None or expected_owner!=head_owner):
            raise LineageError('Current storage owner differs')
        row = conn.execute('SELECT rollout_path,history_mode,attached_thread_parent_id FROM threads WHERE id=?', (expected_owner,)).fetchone()
        path = root / resolved['relative_physical_path']
        if path.suffix == '.zst': path = type(path)(str(path)[:-4])
        if tid != expected_owner:
            storage_binding = {'format':'rehome-storage-source-role/1', 'physical_rollout_id':tid,
                'logical_session_id':expected_owner, 'projection_storage_key':tid,
                'header_sha256':resolved['header_evidence']['header_sha256']}
    else:
        expected_owner = tid
        row = conn.execute('SELECT rollout_path,history_mode,attached_thread_parent_id FROM threads WHERE id=?', (tid,)).fetchone()
        path = None if row is None else journal.resolve_journal(root,row['rollout_path'])[0]
    if row is None or row['history_mode'] != 'paginated' or row['attached_thread_parent_id'] is not None:
        raise LineageError('Missing or unsupported dependency history')
    projection = conn.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM history.thread_history_projection_state WHERE thread_id=?', (tid,)).fetchone()
    if projection is None:
        raise LineageError('Dependency projection missing')
    byte_limit = projection[0] if cutoff is None else cutoff['end_byte_offset']
    ordinal_limit = projection[1] if cutoff is None else cutoff['end_ordinal_exclusive']
    if (type(byte_limit) is not int or type(ordinal_limit) is not int or
            not 0 < byte_limit <= journal.MAX_BYTES or
            projection[0] < byte_limit or projection[1] < ordinal_limit or
            ordinal_limit <= 0):
        raise LineageError('Incomplete journal or projection boundary')
    if conn.execute('SELECT 1 FROM history.thread_realtime_items WHERE thread_id=? AND rollout_ordinal<? LIMIT 1', (tid, ordinal_limit)).fetchone():
        raise LineageError('Realtime history requires a separate decoder')
    raw_items, raw_turns, active, dependency, fork_reference_roles, writer_version = [], [], None, None, None, None
    historical_partial=[];frozen_turns=set();terminal_only_aborts=[];started_any=False;open_prefix=None;closed_tail=None
    digest, position, previous, records, first_ordinal = hashlib.sha256(), 0, None, 0, None
    scope = {'expected_size': byte_limit} if cutoff is None else {'byte_limit': byte_limit}
    with journal.open_journal(root, path, decoder=decoder, **scope) as stream:
        while position < byte_limit:
            record_limit=(16 if backup_records else 8)*1024*1024
            line=bytearray()
            while position+len(line)<byte_limit and not line.endswith(b'\n'):
                piece=stream.readline(min(byte_limit-position-len(line),journal.MAX_LINE_READ,record_limit-len(line)+1))
                if not piece:break
                if len(line)+len(piece)>record_limit:raise LineageError('Native journal record exceeds the backup record limit')
                line.extend(piece)
            if not line or not line.endswith(b'\n'):
                raise LineageError('Fork byte cutoff splits a record or record is oversized')
            try:
                event = json.loads(line, object_pairs_hook=filtering.unique_object)
            except (ValueError, UnicodeError):
                raise LineageError('Invalid journal record') from None
            if not isinstance(event, dict) or type(event.get('ordinal')) is not int or not isinstance(event.get('type'), str):
                raise LineageError('Unknown native journal protocol')
            ordinal = event['ordinal']
            if ordinal < 0 or ordinal >= ordinal_limit or (previous is not None and ordinal != previous + 1):
                raise LineageError('Noncontiguous or mismatched fork ordinal cutoff')
            payload = event.get('payload')
            if records == 0:
                if event['type'] != 'session_meta' or not isinstance(payload, dict) or payload.get('id') != expected_owner or payload.get('history_mode') != 'paginated':
                    raise LineageError('Dependency journal identity differs')
                dependency = _base(payload)
                writer_version = payload.get('cli_version')
                if dependency is not None and payload.get('forked_from_id', dependency['thread_id']) != dependency['thread_id']:
                    fork_reference_roles = {'format': 'rehome-fork-reference-roles/1',
                        'writer_version': payload['cli_version'], 'provenance_thread_id': payload['forked_from_id'],
                        'storage_base_rollout_id': dependency['thread_id'],
                        'end_ordinal_exclusive': dependency['end_ordinal_exclusive'],
                        'end_byte_offset': dependency['end_byte_offset'],
                        'provenance_is_history_dependency': False}
                first_ordinal = ordinal
                if ordinal != (0 if dependency is None else dependency['end_ordinal_exclusive']):
                    raise LineageError('Fork start does not match inherited prefix')
            elif event['type'] == 'session_meta':
                raise LineageError('Repeated session metadata')
            elif event['type'] == 'event_msg' and isinstance(payload, dict):
                kind = payload.get('type')
                if payload.get('turn_id') in frozen_turns and kind in ('task_started','task_complete','turn_aborted','item_completed'):
                    raise LineageError('Historical unfinished turn has interleaved continuation')
                if kind == 'task_started':
                    started_any=True
                    if active is not None or not isinstance(payload.get('turn_id'), str):
                        prior=None if active is None else conn.execute('SELECT status,rollout_ordinal,rollout_end_ordinal FROM history.thread_turns WHERE thread_id=? AND turn_id=?',(tid,active['id'])).fetchone()
                        later=conn.execute('SELECT status,rollout_ordinal,rollout_end_ordinal FROM history.thread_turns WHERE thread_id=? AND turn_id=?',(tid,payload.get('turn_id'))).fetchone()
                        last_item=None if active is None else conn.execute('SELECT MAX(rollout_ordinal),MAX(updated_at_ordinal) FROM history.thread_items WHERE thread_id=? AND turn_id=?',(tid,active['id'])).fetchone()
                        supported=(writer_version in SEQUENTIAL_OPEN_WRITERS and active is not None and
                            isinstance(payload.get('turn_id'),str) and payload['turn_id']!=active['id'] and prior is not None and
                            tuple(prior)==('inProgress',active['start'],None) and later is not None and
                            later[0] in native.TERMINAL_TURN_STATUSES and later[1]==ordinal and
                            type(later[2]) is int and ordinal<=later[2]<ordinal_limit and
                            all(value is None or value<ordinal for value in last_item))
                        if supported:
                            _uuid(payload['turn_id']);frozen_turns.add(active['id'])
                            raw_turns.append({**active,'end':None,'status':'inProgress'})
                            historical_partial.append({'turn_id':active['id'],'source_status':'inProgress',
                                'source_terminal_event_absent_in_checked_prefix':True,'historical_partial':True,
                                'frozen_before_ordinal':ordinal,'checked_end_ordinal_exclusive':ordinal_limit,
                                'receiver_status':'interrupted'})
                            active={'id':payload['turn_id'],'start':ordinal}
                            digest.update(line);position+=len(line);previous,records=ordinal,records+1
                            continue
                        error=LineageError('Overlapping or unidentified native turn')
                        states=[]
                        for identity in (None if active is None else active['id'],payload.get('turn_id')):
                            row=conn.execute('SELECT status,rollout_ordinal,rollout_end_ordinal FROM history.thread_turns WHERE thread_id=? AND turn_id=?',(tid,identity)).fetchone()
                            states.append(None if row is None else {'status':row[0] if row[0] in ('completed','failed','interrupted','inProgress') else 'unknown',
                                'start_ordinal':row[1],'end_ordinal':row[2]})
                        error.contract_metadata={'protocol_case':'overlapping_task_started','ordinal':ordinal,
                            'prior_start_ordinal':None if active is None else active['start'],
                            'same_turn_id':active is not None and payload.get('turn_id')==active['id'],
                            'incoming_turn_id_type':type(payload.get('turn_id')).__name__,'sql_boundary_pair':states}
                        raise error
                    active = {'id': payload['turn_id'], 'start': ordinal}
                elif kind == 'task_complete':
                    if active is None or payload.get('turn_id') != active['id']:
                        raise LineageError('Native completed-turn boundary mismatch')
                    error = payload.get('error')
                    if error is not None and not isinstance(error, dict):
                        raise LineageError('Unknown native failure representation')
                    raw_turns.append({**active, 'end': ordinal, 'status': 'completed' if error is None else 'failed'})
                    active = None
                elif kind == 'turn_aborted':
                    if payload.get('reason') != 'interrupted':
                        raise LineageError('Unknown native interrupted-turn boundary')
                    if active is None:
                        # Exact 0.165 stateless native projector emits an
                        # interrupted turn even without a durable task_started.
                        # Accept only the independently projected zero-item,
                        # start=end boundary; never invent or ignore a turn.
                        turn_id = _uuid(payload.get('turn_id'))
                        row = conn.execute('SELECT status,rollout_ordinal,rollout_end_ordinal FROM history.thread_turns WHERE thread_id=? AND turn_id=?',
                                           (tid, turn_id)).fetchone()
                        count = conn.execute('SELECT COUNT(*) FROM history.thread_items WHERE thread_id=? AND turn_id=?', (tid,turn_id)).fetchone()[0]
                        if (writer_version not in FROZEN_PREFIX_WRITERS or row is None or
                                tuple(row) != ('interrupted',ordinal,ordinal) or count != 0):
                            raise LineageError('Unknown native interrupted-turn boundary')
                        terminal_only_aborts.append({'turn_id':turn_id,'ordinal':ordinal,'reason':'interrupted',
                            'before_local_started_or_visible':not started_any and not raw_turns and not raw_items,
                            'sql_start_ordinal':ordinal,'sql_end_ordinal':ordinal,'total_item_count':0,'visible_item_count':0})
                        raw_turns.append({'id':turn_id,'start':ordinal,'end':ordinal,'status':'interrupted'})
                    else:
                        if payload.get('turn_id') != active['id']:
                            raise LineageError('Unknown native interrupted-turn boundary')
                        raw_turns.append({**active, 'end': ordinal, 'status': 'interrupted'})
                    active = None
                elif kind == 'item_completed':
                    item = _raw_visible(payload.get('item'),writer_version=writer_version)
                    if item is not None:
                        if active is None or payload.get('turn_id') != active['id'] or payload.get('thread_id') != expected_owner:
                            raise LineageError('Visible item attribution mismatch')
                        raw_items.append((active['id'], ordinal, item))
                elif kind in ('task_aborted', 'error'):
                    raise LineageError('Unknown terminal-event representation')
            digest.update(line)
            position += len(line)
            previous, records = ordinal, records + 1
    if active is not None and cutoff is not None and writer_version in FROZEN_PREFIX_WRITERS:
        row=conn.execute('SELECT status,rollout_ordinal,rollout_end_ordinal FROM history.thread_turns WHERE thread_id=? AND turn_id=?',(tid,active['id'])).fetchone()
        if row is not None and row[1]==active['start'] and ((row[0]=='inProgress' and row[2] is None) or
                row[0] in native.TERMINAL_TURN_STATUSES and type(row[2]) is int and row[2]>=ordinal_limit):
            open_prefix={'turn_id':active['id'],'start_ordinal':active['start'],'checked_end_ordinal_exclusive':ordinal_limit,
                'checked_end_byte_offset':byte_limit,'source_status_at_observation':row[0],
                'source_end_ordinal_at_observation':row[2],'source_terminal_event_absent_in_checked_prefix':True}
            raw_turns.append({**active,'end':row[2],'status':row[0]});active=None
    if active is not None and cutoff is None and closed_snapshot is not None and writer_version in CLOSED_TAIL_WRITERS:
        import rehome_closed_snapshot
        if not isinstance(closed_snapshot,rehome_closed_snapshot.ClosedSnapshot):raise LineageError('Invalid closed snapshot evidence')
        closed_snapshot.assert_root(root)
        row=conn.execute('SELECT status,rollout_ordinal,rollout_end_ordinal FROM history.thread_turns WHERE thread_id=? AND turn_id=?',(tid,active['id'])).fetchone()
        extra=conn.execute('SELECT 1 FROM history.thread_turns WHERE thread_id=? AND rollout_ordinal>=? LIMIT 1',(tid,ordinal_limit)).fetchone()
        extra_items=conn.execute('SELECT 1 FROM history.thread_items WHERE thread_id=? AND (rollout_ordinal>=? OR updated_at_ordinal>=?) LIMIT 1',(tid,ordinal_limit,ordinal_limit)).fetchone()
        if previous==ordinal_limit-1 and row is not None and tuple(row)==('inProgress',active['start'],None) and extra is None and extra_items is None:
            closed_tail={'turn_id':active['id'],'start_ordinal':active['start'],
                'end_ordinal_exclusive':ordinal_limit,'bytes_read':byte_limit,'writer_version':writer_version,
                'source_status_at_snapshot':'inProgress','source_end_ordinal':None,'terminal_event_absent_at_eof':True,
                'receiver_status':'interrupted','basis':'closed_profile_snapshot','snapshot_sha256':closed_snapshot.digest}
            raw_turns.append({**active,'end':None,'status':'inProgress'});active=None
    if previous != ordinal_limit - 1 or active is not None:
        error=LineageError('Changing journal or cutoff inside a turn')
        if active is not None:
            row=conn.execute('SELECT status,rollout_ordinal,rollout_end_ordinal FROM history.thread_turns WHERE thread_id=? AND turn_id=?',(tid,active['id'])).fetchone()
            counts=conn.execute("SELECT SUM(rollout_ordinal<?),SUM(rollout_ordinal>=?) FROM history.thread_items WHERE thread_id=? AND turn_id=? AND item_type IN ('userMessage','agentMessage')",(ordinal_limit,ordinal_limit,tid,active['id'])).fetchone()
            error.contract_metadata={'protocol_case':'cutoff_open_turn','cutoff_is_exact_ordinal':previous==ordinal_limit-1,
                'checked_end_ordinal_exclusive':ordinal_limit,'active_start_ordinal':active['start'],
                'source_sql_status':None if row is None else row[0] if row[0] in ('completed','failed','interrupted','inProgress') else 'unknown',
                'source_sql_end_ordinal':None if row is None else row[2],
                'visible_items_in_checked_prefix':counts[0] or 0,'visible_items_after_checked_prefix':counts[1] or 0}
        raise error
    sql_turns = list(conn.execute('SELECT turn_id,status,rollout_ordinal,rollout_end_ordinal FROM history.thread_turns WHERE thread_id=? AND rollout_ordinal<? ORDER BY rollout_ordinal', (tid, ordinal_limit)))
    if len(sql_turns) != len(raw_turns):
        raise LineageError('Native/projected turn counts differ')
    for sql, raw in zip(sql_turns, raw_turns):
        if (sql['turn_id'], sql['status'], sql['rollout_ordinal'], sql['rollout_end_ordinal']) != (raw['id'], raw['status'], raw['start'], raw['end']):
            raise LineageError('Native/projected turn boundaries differ')
    sql_items = list(conn.execute("SELECT turn_id,item_id,item_type,item_json,rollout_ordinal,updated_at_ordinal FROM history.thread_items WHERE thread_id=? AND rollout_ordinal<? AND item_type IN ('userMessage','agentMessage') ORDER BY rollout_ordinal", (tid, ordinal_limit)))
    if len(sql_items) != len(raw_items):
        raise LineageError('Native/projected visible message counts differ')
    messages = {turn['id']: [] for turn in raw_turns}
    for sql, (turn_id, ordinal, raw) in zip(sql_items, raw_items):
        item = json.loads(sql['item_json'], object_pairs_hook=filtering.unique_object)
        if (not isinstance(item, dict) or sql['item_type'] != item.get('type') or sql['item_id'] != item.get('id') or
                sql['turn_id'] != turn_id or sql['rollout_ordinal'] != ordinal or sql['updated_at_ordinal'] >= ordinal_limit or
                _comparable(item) != raw):
            error=LineageError('Native/projected visible message content differs')
            comparison=_comparable(item) if isinstance(item,dict) else {}
            error.contract_metadata={'item_kind':sql['item_type'] if sql['item_type'] in ('userMessage','agentMessage') else 'unknown',
                'turn_id_matches':sql['turn_id']==turn_id,'item_id_matches':sql['item_id']==item.get('id'),
                'ordinal_matches':sql['rollout_ordinal']==ordinal,'updated_inside_prefix':sql['updated_at_ordinal']<ordinal_limit,
                'different_normalized_fields':[key for key in ('type','id','text','phase','clientId','content','delivery','questions','memoryCitation')
                                               if comparison.get(key)!=raw.get(key)]}
            if sql['item_type']=='userMessage' and isinstance(comparison.get('content'),list) and isinstance(raw.get('content'),list):
                left,right=comparison['content'],raw['content']
                shapes=[]
                for index,(a,b) in enumerate(zip(left,right)):
                    if a==b:continue
                    if not isinstance(a,dict) or not isinstance(b,dict):continue
                    known={'type','text','text_elements','image_url','url','path','detail'}
                    shapes.append({'block_index':index,
                        'sql_type':a.get('type') if a.get('type') in ('text','image','localImage') else 'unknown',
                        'raw_type':b.get('type') if b.get('type') in ('text','image','localImage') else 'unknown',
                        'sql_known_keys':sorted(set(a)&known),'raw_known_keys':sorted(set(b)&known),
                        'sql_unknown_key_count':len(set(a)-known),'raw_unknown_key_count':len(set(b)-known),
                        'text_equal':a.get('text')==b.get('text'),
                        'text_elements_equal':a.get('text_elements')==b.get('text_elements')})
                error.contract_metadata['content_block_shapes']={'sql_count':len(left),'raw_count':len(right),'different_blocks':shapes}
            raise error
        messages[turn_id].append(item)
    omitted = conn.execute("SELECT COUNT(*) FROM history.thread_items WHERE thread_id=? AND rollout_ordinal<? AND item_type NOT IN ('userMessage','agentMessage')", (tid, ordinal_limit)).fetchone()[0]
    if open_prefix is not None:open_prefix['visible_item_count_before_cutoff']=len(messages[open_prefix['turn_id']])
    if closed_tail is not None:closed_tail['message_count_preserved']=len(messages[closed_tail['turn_id']])
    return {'dependency': dependency, 'turns': [{'id': t['id'], 'status': 'open_prefix' if open_prefix is not None and t['id']==open_prefix['turn_id'] else
                'interrupted' if t['id'] in frozen_turns or closed_tail is not None and t['id']==closed_tail['turn_id'] else t['status'], 'items': messages[t['id']]} for t in raw_turns],
            'evidence': {'source_thread_id': tid, 'start_ordinal': first_ordinal,
                'end_ordinal_exclusive': ordinal_limit, 'bytes_read': byte_limit, 'prefix_sha256': digest.hexdigest(),
                'visible_projection_verified': True, 'prefix_only': cutoff is not None,
                'storage': stream.evidence,
                'turn_count': len(raw_turns), 'message_count': len(raw_items), 'omitted_projected_items': omitted,
                **({'fork_reference_roles': fork_reference_roles} if fork_reference_roles is not None else {}),
                **({'storage_binding':storage_binding} if storage_binding is not None else {}),
                **({'historical_partial_turns':historical_partial} if historical_partial else {}),
                **({'terminal_only_abort_events':terminal_only_aborts} if terminal_only_aborts else {}),
                **({'open_prefix_turn':open_prefix} if open_prefix is not None else {}),
                **({'closed_snapshot_tail':closed_tail} if closed_tail is not None else {})}}


def assemble_chain(chain):
    """Materialize one proven chain; only an adjacent exact child abort closes a frozen prefix."""
    turns,joins,seen=[],[],set();pending=None
    for index,(origin,part) in enumerate(chain):
        if pending is not None and not part['turns']:raise LineageError('Frozen prefix has no adjacent closing child turn')
        for turn_index,turn in enumerate(part['turns']):
            if pending is not None:
                parent_id,parent_part,prefix=pending
                base=part['dependency']
                matches=[event for event in part['evidence'].get('terminal_only_abort_events',[]) if event['turn_id']==prefix['turn_id']]
                expected={'thread_id':parent_id,'end_ordinal_exclusive':prefix['checked_end_ordinal_exclusive'],
                          'end_byte_offset':prefix['checked_end_byte_offset']}
                if (index==0 or chain[index-1][0]!=parent_id or turn_index!=0 or base!=expected or
                        turn['id']!=prefix['turn_id'] or turn['status']!='interrupted' or turn['items'] or
                        len(matches)!=1 or matches[0]['before_local_started_or_visible'] is not True or
                        matches[0]['total_item_count']!=0 or matches[0]['visible_item_count']!=0):
                    raise LineageError('Frozen prefix child terminal does not match exact adjacent boundary')
                turns[-1]['status']='interrupted'
                joins.append({'format':'rehome-frozen-prefix-turn-join/1','parent_storage_id':parent_id,'child_storage_id':origin,
                    'turn_id':prefix['turn_id'],'parent_start_ordinal':prefix['start_ordinal'],
                    'parent_cutoff_ordinal_exclusive':prefix['checked_end_ordinal_exclusive'],
                    'parent_cutoff_byte_offset':prefix['checked_end_byte_offset'],'child_abort_ordinal':matches[0]['ordinal'],
                    'reason':'interrupted','source_status_at_observation':prefix['source_status_at_observation'],
                    'source_end_ordinal_at_observation':prefix['source_end_ordinal_at_observation'],
                    'source_terminal_event_absent_in_checked_prefix':True,'fork_snapshot_partial':True,
                    'result_status':'interrupted','basis':'child_terminal_event','message_count_preserved':len(turns[-1]['items']),
                    'child_visible_item_count':0})
                pending=None;continue
            if turn['id'] in seen:raise LineageError('Duplicate inherited turn identity')
            seen.add(turn['id']);turns.append({**turn,'source_thread_id':origin})
            if turn['status']=='open_prefix':
                prefix=part['evidence'].get('open_prefix_turn')
                if prefix is None or turn['id']!=prefix['turn_id'] or turn_index!=len(part['turns'])-1:
                    raise LineageError('Malformed frozen prefix turn')
                pending=(origin,part,prefix)
    if pending is not None:raise LineageError('Frozen prefix is not closed by a proven child terminal')
    return turns,joins


def export_candidate(profile, selected_ids, *, exclusions=None, journal_decoder=None,
                     text_policy=filtering.RESTRICTED_TEXT,include_all_layout=False,preserve_display=False,
                     resolve_current_storage=False,closed_snapshot=None):
    filtering.validate_text_policy(text_policy)
    if type(include_all_layout) is not bool or type(preserve_display) is not bool:
        raise LineageError('Explicit backup layout/display options required')
    if preserve_display and text_policy!=filtering.VERBATIM_TEXT:
        raise LineageError('Display metadata is confined to the backup contract')
    if type(resolve_current_storage) is not bool or ((resolve_current_storage or closed_snapshot is not None) and text_policy!=filtering.VERBATIM_TEXT):
        raise LineageError('Current storage and closed snapshots are confined to the backup contract')
    if closed_snapshot is not None:
        import rehome_closed_snapshot
        if not isinstance(closed_snapshot,rehome_closed_snapshot.ClosedSnapshot):raise LineageError('Invalid closed snapshot evidence')
        closed_snapshot.verify(profile)
    if not isinstance(selected_ids, list) or not selected_ids or len(selected_ids) != len(set(selected_ids)):
        raise LineageError('Select unique chat identities')
    for tid in selected_ids:
        _uuid(tid)
    exclusions = {} if exclusions is None else exclusions
    if not isinstance(exclusions, dict):
        raise LineageError('Invalid exclusions')
    if text_policy == filtering.VERBATIM_TEXT and exclusions:
        raise LineageError('Verbatim history cannot apply manual exclusions')
    used, evidence, filtering_reports, chains, cache, joins_by_selected = set(), [], [], {}, {}, {}
    def label(value):
        if text_policy==filtering.VERBATIM_TEXT:
            if not isinstance(value,str):raise LineageError('Invalid visible label')
            return value
        return filtering.check_text(value)
    with source._snapshot(profile, source.CURRENT_SCHEMA_VERSION) as (conn, glob, root):
        source_instance = source._digest({'root': source._normal_path(str(root)), 'identity': source._source_identity(root)})
        inventory, rows, projects = source._metadata(conn, glob, root, source.CURRENT_SCHEMA_VERSION)
        entries = {t['id']: t for t in inventory['threads']}
        by_id = {r['id']: r for r in rows}
        sections = dict(conn.execute('SELECT id,name FROM thread_sections'))
        threads, used_projects, used_sections = [], set(), set()
        for selected in selected_ids:
            head=current_storage_id(conn,root,selected,decoder=journal_decoder) if resolve_current_storage else selected
            if resolve_current_storage:
                row=by_id[selected]
                pid,mapping=source._thread_project(row,glob,projects,source._host_mapping(glob,root))
                counts=dict(conn.execute('SELECT status,COUNT(*) FROM history.thread_turns WHERE thread_id=? GROUP BY status',(head,)))
                if set(counts)-native.TERMINAL_TURN_STATUSES-{'inProgress'}:raise LineageError('Unknown current storage turn status')
                entry={**entries[selected],'project_id':pid,'mapping':mapping,'turn_status_counts':counts,
                       'status':'in_progress' if 'inProgress' in counts else 'ready','reason':None}
                if row['has_user_event'] and not counts:entry.update(status='blocked',reason='User thread has no available paginated turn history')
                entries[selected]=entry
            inherited_only = (selected in entries and entries[selected]['status'] == 'blocked' and
                              entries[selected]['reason'] == 'User thread has no available paginated turn history')
            historical_only=(selected in entries and entries[selected]['status']=='in_progress' and
                             historical_open_counts(conn,head)['active_tail_count']==0)
            frozen_tail=(closed_snapshot is not None and selected in entries and entries[selected]['status']=='in_progress')
            if selected not in entries or (entries[selected]['status'] != 'ready' and not inherited_only and not historical_only and not frozen_tail):
                raise LineageError('Selected chat is not ready')
            chain, visiting, tid, cutoff = [], set(), head, None
            while True:
                if tid in visiting or len(visiting) >= 128:
                    raise LineageError('Cyclic or excessive parent dependencies')
                visiting.add(tid)
                owner=selected if cutoff is None and head!=selected else None
                key = (tid, None if cutoff is None else cutoff['end_ordinal_exclusive'], None if cutoff is None else cutoff['end_byte_offset'],owner)
                if key not in cache:
                    cache[key] = segment(conn, root, tid, cutoff, decoder=journal_decoder,head_owner=owner,closed_snapshot=closed_snapshot,backup_records=resolve_current_storage)
                current = cache[key]
                chain.append((tid, current))
                dependency = current['dependency']
                if dependency is None:
                    break
                tid, cutoff = dependency['thread_id'], dependency
            chain.reverse()
            chains[selected] = [s['evidence'] for _, s in chain]
            assembled,joins=assemble_chain(chain);joins_by_selected[selected]=joins
            turns=[]
            for turn in assembled:
                origin=turn['source_thread_id'];messages=[]
                for item in turn['items']:
                    key=origin+'/'+turn['id']+'/'+item['id']
                    message,detail=filtering.visible_message(item,exclusions.get(key),text_policy=text_policy)
                    if key in exclusions:used.add(key)
                    messages.append(message)
                    filtering_reports.append({'target_thread_id':selected,'source_thread_id':origin,'turn_id':turn['id'],**detail})
                turns.append({'id':turn['id'],'status':turn['status'],'messages':messages})
            if inherited_only and (len(chain) == 1 or not turns):
                raise LineageError('Missing local history has no verified inherited turns')
            row, entry = by_id[selected], entries[selected]
            pid, sid = entry['project_id'], row['thread_section_id']
            if pid is not None:
                used_projects.add(pid)
            if sid is not None:
                if sid not in sections:
                    raise LineageError('Selected section missing')
                used_sections.add(sid)
            threads.append({'id': selected, 'title': label(row['title']),
                'name': None if row['name'] is None else label(row['name']),
                'project_id': pid, 'section_id': sid, 'is_pinned': bool(row['is_pinned']),
                'archived': bool(row['archived']), 'turns': turns})
        if used != set(exclusions):
            raise LineageError('Unused exclusion plan')
        for (tid, ordinal, offset,owner), cached in cache.items():
            cutoff = None if ordinal is None else {'end_ordinal_exclusive': ordinal, 'end_byte_offset': offset}
            if segment(conn, root, tid, cutoff, decoder=journal_decoder,head_owner=owner,closed_snapshot=closed_snapshot,backup_records=resolve_current_storage) != cached:
                raise LineageError('Dependency changed during export')
            evidence.append(cached['evidence'])
        selected_set = set(selected_ids)
        spawn_edges, excluded_spawn_edges = [], []
        for edge in conn.execute('SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges'):
            parent, child = edge['parent_thread_id'], edge['child_thread_id']
            if parent in selected_set and child in selected_set:
                spawn_edges.append({'parent': parent, 'child': child})
            elif parent in selected_set or child in selected_set:
                excluded_spawn_edges.append({'parent': parent, 'child': child})
        transfer = {'format': native.FORMAT, 'format_version': native.FORMAT_VERSION,
            'codex_version': native.CODEX_VERSION, 'schema_fingerprint': native.SCHEMA_FINGERPRINT,
            'synthetic_only': False, 'projects': [{'id': pid, 'name': label(projects[pid]['name'])} for pid in sorted(projects if include_all_layout else used_projects)],
            'sections': [{'id': sid, 'name': label(sections[sid])} for sid in sorted(sections if include_all_layout else used_sections)],
            'threads': threads, 'spawn_edges': spawn_edges,
            'omitted_nonmessage_items': sum(e['omitted_projected_items'] for chain in chains.values() for e in chain)}
        # Preserve real selected spawn edges; fork provenance remains separate.
        validated = copy.deepcopy(transfer)
        validated['synthetic_only'] = True
        native._validate_bundle(validated)
        display={}
        if preserve_display:
            import rehome_display_metadata
            display={'display_metadata':rehome_display_metadata.capture(conn,glob,root,transfer,chains)}
    if closed_snapshot is not None:closed_snapshot.verify(profile)
    contract = ({'format': 'rehome-backup-history/1', 'text_policy': text_policy}
                if text_policy == filtering.VERBATIM_TEXT else {'format': 'rehome-history-review-candidate/1'})
    return {**contract, **display, **({'closed_source_snapshot':closed_snapshot.summary()} if closed_snapshot is not None else {}), 'status': 'review_required',
        'publication_ready': False, 'real_profile_apply_enabled': False,
        'source_schema_version': source.CURRENT_SCHEMA_VERSION, 'selected_thread_ids': list(selected_ids),
        'source_inventory_token': inventory['inventory_token'],
        'source_instance_id': source_instance,
        'excluded_spawn_edges': excluded_spawn_edges,
        'dependency_ids': sorted({e['source_thread_id'] for e in evidence} - set(selected_ids)),
        'lineage_mode': 'materialized_visible_history_with_separate_provenance',
        'lineage': chains, **({'lineage_joins':joins_by_selected} if any(joins_by_selected.values()) else {}),
        'filtering': filtering_reports, 'transfer': transfer,
        'omitted_service_items': 'Excluded; raw service payloads not serialized. Exact count not claimed.'}
