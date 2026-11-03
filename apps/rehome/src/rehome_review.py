"""Local static composition preview. No server, publishing or profile switching."""
from __future__ import annotations
import html
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import context_probe
import rehome_history_lineage as lineage
import snapshot


REVIEW_LIMITS = {'review-manifest.json': 64 * 1024, 'candidate.json': 32 * 1024 * 1024,
                 'results.json': 4 * 1024 * 1024, 'review.html': 64 * 1024 * 1024}
_CONTENT_FILES = ('candidate.json', 'results.json', 'review.html')


class ReviewIntegrityError(ValueError):
    """Prepared local review is incomplete, changed or outside its contract."""


def _require(condition, message='Prepared review contract differs'):
    if not condition:
        raise ReviewIntegrityError(message)


def _exact(value, keys):
    _require(isinstance(value, dict) and set(value) == set(keys))


def _sha(value):
    _require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None)


def _count(value):
    _require(type(value) is int and 0 <= value <= 2**63 - 1)


def _json_bytes(data):
    def reject_constant(_):
        raise ReviewIntegrityError('Nonfinite JSON values are prohibited')
    return json.loads(data.decode('utf-8'), object_pairs_hook=lineage.filtering.unique_object,
                      parse_constant=reject_constant)


def _directory_state(root, *, limits=None):
    limits = REVIEW_LIMITS if limits is None else limits
    snapshot._safe_path(root)
    info = root.lstat()
    _require(stat.S_ISDIR(info.st_mode), 'Prepared review is not a directory')
    _require({p.name for p in root.iterdir()} == set(limits), 'Prepared review must contain exactly four files')
    states = {}
    for name, limit in limits.items():
        path = snapshot._safe_path(root / name)
        item = path.lstat()
        _require(stat.S_ISREG(item.st_mode) and item.st_nlink == 1,
                 'Prepared review requires regular files without hard links')
        _require(0 < item.st_size <= limit, 'Prepared review file exceeds its size limit or is empty')
        states[name] = (snapshot._fingerprint(item), snapshot._content_state(item))
    return (snapshot._fingerprint(info), snapshot._content_state(info)), states


def _read_prepared(root, name, state, *, limits=None):
    limits = REVIEW_LIMITS if limits is None else limits
    path = snapshot._safe_path(root / name)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        _require(snapshot._fingerprint(opened) == state[0], 'Prepared review changed before reading')
        data = stream.read(limits[name] + 1)
        after = os.fstat(stream.fileno())
        _require(len(data) == opened.st_size and len(data) <= limits[name] and
                 snapshot._fingerprint(after) == snapshot._fingerprint(opened) and
                 snapshot._content_state(after) == snapshot._content_state(opened),
                 'Prepared review changed during reading')
    return data


def _validate_storage(storage, evidence):
    _exact(storage, {'representation', 'physical_size', 'physical_identity', 'physical_bytes_read',
                     'decoded_bytes', 'bytes_read', 'prefix_only', 'full_frame_validation',
                     'decoder_version', 'decoder_sha256'})
    for key in ('physical_size', 'physical_bytes_read', 'decoded_bytes', 'bytes_read'):
        _count(storage[key])
    _require(0 < storage['physical_bytes_read'] <= storage['physical_size'])
    _require(storage['bytes_read'] == storage['decoded_bytes'] == evidence['bytes_read'])
    _require(type(storage['prefix_only']) is bool and storage['prefix_only'] == evidence['prefix_only'])
    identity = storage['physical_identity']
    _require(isinstance(identity, list) and len(identity) == 4 and all(type(n) is int for n in identity)
             and identity[1] > 0 and identity[2] == storage['physical_size'])
    compressed = storage['representation'] == 'zstd'
    _require(storage['representation'] in ('plain', 'zstd') and
             storage['full_frame_validation'] is (compressed and not evidence['prefix_only']))
    if compressed:
        _require(isinstance(storage['decoder_version'], str) and bool(storage['decoder_version']))
        _sha(storage['decoder_sha256'])
    else:
        _require(storage['decoder_version'] is None and storage['decoder_sha256'] is None)


def _validate_historical_partial(evidence, statuses=None):
    rows=evidence['historical_partial_turns']
    _require(isinstance(rows,list) and bool(rows));seen=set()
    for item in rows:
        _exact(item,{'turn_id','source_status','source_terminal_event_absent_in_checked_prefix','historical_partial',
                     'frozen_before_ordinal','checked_end_ordinal_exclusive','receiver_status'})
        _require(isinstance(item['turn_id'],str) and bool(item['turn_id']) and item['turn_id'] not in seen)
        lineage.filtering.check_text(item['turn_id']);seen.add(item['turn_id'])
        _require(item['source_status']=='inProgress' and item['source_terminal_event_absent_in_checked_prefix'] is True and
                 item['historical_partial'] is True and item['receiver_status']=='interrupted')
        for key in ('frozen_before_ordinal','checked_end_ordinal_exclusive'):_count(item[key])
        _require(evidence['start_ordinal']<item['frozen_before_ordinal']<item['checked_end_ordinal_exclusive']==evidence['end_ordinal_exclusive'])
        if statuses is not None:_require(statuses.get(item['turn_id'])=='interrupted')


def _validate_frozen_segment_metadata(evidence):
    if 'open_prefix_turn' in evidence:
        item=evidence['open_prefix_turn']
        _exact(item,{'turn_id','start_ordinal','checked_end_ordinal_exclusive','checked_end_byte_offset',
            'source_status_at_observation','source_end_ordinal_at_observation','source_terminal_event_absent_in_checked_prefix',
            'visible_item_count_before_cutoff'})
        _require(isinstance(item['turn_id'],str) and bool(item['turn_id']));lineage.filtering.check_text(item['turn_id'])
        for key in ('start_ordinal','checked_end_ordinal_exclusive','checked_end_byte_offset','visible_item_count_before_cutoff'):_count(item[key])
        _require(evidence['prefix_only'] is True and item['source_terminal_event_absent_in_checked_prefix'] is True and
            evidence['start_ordinal']<=item['start_ordinal']<item['checked_end_ordinal_exclusive']==evidence['end_ordinal_exclusive'] and
            item['checked_end_byte_offset']==evidence['bytes_read'])
        status,end=item['source_status_at_observation'],item['source_end_ordinal_at_observation']
        _require(status=='inProgress' and end is None or status in lineage.native.TERMINAL_TURN_STATUSES and
                 type(end) is int and end>=item['checked_end_ordinal_exclusive'])
    if 'terminal_only_abort_events' in evidence:
        rows=evidence['terminal_only_abort_events'];_require(isinstance(rows,list) and bool(rows));seen=set()
        for item in rows:
            _exact(item,{'turn_id','ordinal','reason','before_local_started_or_visible','sql_start_ordinal','sql_end_ordinal',
                         'total_item_count','visible_item_count'})
            _require(isinstance(item['turn_id'],str) and bool(item['turn_id']) and item['turn_id'] not in seen)
            lineage.filtering.check_text(item['turn_id']);seen.add(item['turn_id'])
            for key in ('ordinal','sql_start_ordinal','sql_end_ordinal','total_item_count','visible_item_count'):_count(item[key])
            _require(item['reason']=='interrupted' and type(item['before_local_started_or_visible']) is bool and
                item['ordinal']==item['sql_start_ordinal']==item['sql_end_ordinal'] and item['total_item_count']==item['visible_item_count']==0 and
                evidence['start_ordinal']<=item['ordinal']<evidence['end_ordinal_exclusive'])


def _validate_frozen_joins(chain,joins,turns=None):
    _require(isinstance(joins,list));index={row['source_thread_id']:i for i,row in enumerate(chain)}
    _require(len(index)==len(chain));pending={row['source_thread_id']:row['open_prefix_turn'] for row in chain if 'open_prefix_turn' in row}
    seen=set();turnmap=None if turns is None else {turn['id']:turn for turn in turns}
    for join in joins:
        _exact(join,{'format','parent_storage_id','child_storage_id','turn_id','parent_start_ordinal',
            'parent_cutoff_ordinal_exclusive','parent_cutoff_byte_offset','child_abort_ordinal','reason','source_status_at_observation',
            'source_end_ordinal_at_observation','source_terminal_event_absent_in_checked_prefix','fork_snapshot_partial',
            'result_status','basis','message_count_preserved','child_visible_item_count'})
        parent,child=join['parent_storage_id'],join['child_storage_id']
        _require(parent in pending and parent not in seen and child in index and index[child]==index[parent]+1)
        seen.add(parent);prefix=pending[parent];parent_evidence=chain[index[parent]];child_evidence=chain[index[child]]
        _validate_frozen_segment_metadata(parent_evidence);_validate_frozen_segment_metadata(child_evidence)
        events=[row for row in child_evidence.get('terminal_only_abort_events',[]) if row['turn_id']==join['turn_id']]
        _require(join['format']=='rehome-frozen-prefix-turn-join/1' and join['turn_id']==prefix['turn_id'] and
            join['parent_start_ordinal']==prefix['start_ordinal'] and join['parent_cutoff_ordinal_exclusive']==prefix['checked_end_ordinal_exclusive'] and
            join['parent_cutoff_byte_offset']==prefix['checked_end_byte_offset'] and child_evidence['start_ordinal']==prefix['checked_end_ordinal_exclusive'] and
            join['source_status_at_observation']==prefix['source_status_at_observation'] and
            join['source_end_ordinal_at_observation']==prefix['source_end_ordinal_at_observation'] and
            join['source_terminal_event_absent_in_checked_prefix'] is True and join['fork_snapshot_partial'] is True and
            join['result_status']=='interrupted' and join['basis']=='child_terminal_event' and join['reason']=='interrupted' and
            len(events)==1 and events[0]['before_local_started_or_visible'] is True and events[0]['ordinal']==join['child_abort_ordinal'])
        _count(join['message_count_preserved']);_count(join['child_visible_item_count'])
        _require(join['message_count_preserved']==prefix['visible_item_count_before_cutoff'] and join['child_visible_item_count']==0)
        if turnmap is not None:
            _require(join['turn_id'] in turnmap and turnmap[join['turn_id']]['status']=='interrupted' and
                     len(turnmap[join['turn_id']]['messages'])==join['message_count_preserved'])
    _require(seen==set(pending),'Frozen prefix joins do not cover exact pending source turns')
    return len(joins)


def _validate_candidate(candidate):
    """Legacy restricted review contract; never accepts a backup candidate."""
    return _validate_history_candidate(candidate, backup=False)


def validate_backup_history(candidate):
    """Validate the explicit verbatim contract, without granting profile apply."""
    return _validate_history_candidate(candidate, backup=True)


def _validate_history_candidate(candidate, *, backup):
    _exact(candidate, {'format', 'status', 'publication_ready', 'real_profile_apply_enabled',
                       'source_schema_version', 'selected_thread_ids', 'source_inventory_token',
                       'source_instance_id', 'excluded_spawn_edges', 'dependency_ids', 'lineage_mode',
                       'lineage', 'filtering', 'transfer', 'omitted_service_items'}|
                      ({'lineage_joins'} if 'lineage_joins' in candidate else set()) |
                      ({'display_metadata'} if backup and 'display_metadata' in candidate else set()) |
                      ({'closed_source_snapshot'} if backup and 'closed_source_snapshot' in candidate else set()) |
                      ({'text_policy'} if backup else set()))
    expected_format = 'rehome-backup-history/1' if backup else 'rehome-history-review-candidate/1'
    if backup:
        _require(candidate['text_policy'] == lineage.filtering.VERBATIM_TEXT)
    closed=candidate.get('closed_source_snapshot')
    if closed is not None:
        _exact(closed,{'format','manifest_sha256','file_count','quiescence_scope','scope'})
        import rehome_closed_snapshot
        _require(backup and closed['format']==rehome_closed_snapshot.FORMAT and closed['quiescence_scope']==rehome_closed_snapshot.SCOPE and
                 closed['scope']=='private_native_profile_snapshot' and type(closed['file_count']) is int and 3<=closed['file_count']<=100000)
        _sha(closed['manifest_sha256'])
    _require(candidate['format'] == expected_format and candidate['status'] == 'review_required'
             and candidate['publication_ready'] is False and candidate['real_profile_apply_enabled'] is False
             and candidate['source_schema_version'] == lineage.source.CURRENT_SCHEMA_VERSION
             and candidate['lineage_mode'] == 'materialized_visible_history_with_separate_provenance'
             and candidate['omitted_service_items'] == 'Excluded; raw service payloads not serialized. Exact count not claimed.')
    for key in ('source_inventory_token', 'source_instance_id'):
        _sha(candidate[key])
    transfer = candidate['transfer']
    _require(isinstance(transfer, dict) and transfer.get('synthetic_only') is False
             and type(transfer.get('format_version')) is int)
    # The candidate is deliberately not an importable synthetic bundle. Validate
    # structure with a temporary top-level copy; never change its safety flag.
    lineage.native._validate_bundle({**transfer, 'synthetic_only': True})
    if 'display_metadata' in candidate:
        import rehome_display_metadata
        rehome_display_metadata.validate(transfer,candidate['display_metadata'])
    threads = transfer['threads']
    selected = candidate['selected_thread_ids']
    _require(isinstance(selected, list) and selected == [t['id'] for t in threads])
    _require(isinstance(candidate['lineage'], dict) and set(candidate['lineage']) == set(selected))
    if 'lineage_joins' in candidate:_require(isinstance(candidate['lineage_joins'],dict) and set(candidate['lineage_joins'])==set(selected))
    dependencies, expected_reports, omitted_items = set(), [], 0
    totals = {'threads': len(threads), 'projects': len(transfer['projects']), 'turns': 0, 'messages': 0}
    chain_counts = {}
    for thread in threads:
        tid = thread['id']
        for text in (thread['title'], thread['name']):
            if text is not None and not backup:
                lineage.filtering.check_text(text)
        chain = candidate['lineage'][tid]
        _require(isinstance(chain, list) and 1 <= len(chain) <= 128)
        origins, previous, turn_count, message_count = set(), None, 0, 0
        for index, evidence in enumerate(chain):
            _exact(evidence, {'source_thread_id', 'start_ordinal', 'end_ordinal_exclusive', 'bytes_read',
                              'prefix_sha256', 'visible_projection_verified', 'prefix_only', 'storage',
                              'turn_count', 'message_count', 'omitted_projected_items'} |
                             ({'fork_reference_roles'} if 'fork_reference_roles' in evidence else set()) |
                             ({'storage_binding'} if 'storage_binding' in evidence else set()) |
                             ({'historical_partial_turns'} if 'historical_partial_turns' in evidence else set())|
                             ({'open_prefix_turn'} if 'open_prefix_turn' in evidence else set())|
                             ({'closed_snapshot_tail'} if backup and 'closed_snapshot_tail' in evidence else set())|
                             ({'terminal_only_abort_events'} if 'terminal_only_abort_events' in evidence else set()))
            _validate_frozen_segment_metadata(evidence)
            if 'closed_snapshot_tail' in evidence:
                tail=evidence['closed_snapshot_tail']
                _exact(tail,{'turn_id','start_ordinal','end_ordinal_exclusive','bytes_read','writer_version',
                    'source_status_at_snapshot','source_end_ordinal','terminal_event_absent_at_eof','receiver_status','basis',
                    'snapshot_sha256','message_count_preserved'})
                _require(closed is not None and index==len(chain)-1 and evidence['prefix_only'] is False and
                    tail['snapshot_sha256']==closed['manifest_sha256'] and tail['writer_version'] in lineage.CLOSED_TAIL_WRITERS and
                    tail['source_status_at_snapshot']=='inProgress' and tail['source_end_ordinal'] is None and
                    tail['terminal_event_absent_at_eof'] is True and tail['receiver_status']=='interrupted' and tail['basis']=='closed_profile_snapshot' and
                    tail['end_ordinal_exclusive']==evidence['end_ordinal_exclusive'] and tail['bytes_read']==evidence['bytes_read'])
                for key in ('start_ordinal','end_ordinal_exclusive','bytes_read','message_count_preserved'):_count(tail[key])
                _require(evidence['start_ordinal']<=tail['start_ordinal']<tail['end_ordinal_exclusive'] and thread['turns'] and
                    thread['turns'][-1]['id']==tail['turn_id'] and thread['turns'][-1]['status']=='interrupted' and
                    len(thread['turns'][-1]['messages'])==tail['message_count_preserved'])
            if 'historical_partial_turns' in evidence:
                _validate_historical_partial(evidence,{turn['id']:turn['status'] for turn in thread['turns']})
            if 'storage_binding' in evidence:
                binding=evidence['storage_binding']
                _exact(binding,{'format','physical_rollout_id','logical_session_id','projection_storage_key','header_sha256'})
                _require(binding['format']=='rehome-storage-source-role/1' and binding['physical_rollout_id']==evidence['source_thread_id'] and
                         binding['projection_storage_key']==binding['physical_rollout_id'] and
                         binding['logical_session_id']!=binding['physical_rollout_id'])
                for key in ('physical_rollout_id','logical_session_id'):lineage.native._thread_ident(binding[key])
                _sha(binding['header_sha256'])
            if 'fork_reference_roles' in evidence:
                roles = evidence['fork_reference_roles']
                _exact(roles, {'format', 'writer_version', 'provenance_thread_id', 'storage_base_rollout_id',
                               'end_ordinal_exclusive', 'end_byte_offset', 'provenance_is_history_dependency'})
                _require(roles['format'] == 'rehome-fork-reference-roles/1' and
                         roles['writer_version'] in (lineage.SPLIT_FORK_REFERENCE_WRITERS | lineage.LEGACY_SPLIT_REFERENCE_WRITERS) and
                         roles['provenance_is_history_dependency'] is False and
                         roles['provenance_thread_id'] != roles['storage_base_rollout_id'])
                for key in ('provenance_thread_id', 'storage_base_rollout_id'): lineage.native._thread_ident(roles[key])
                for key in ('end_ordinal_exclusive', 'end_byte_offset'):
                    _count(roles[key]); _require(roles[key] > 0)
                _require(roles['end_ordinal_exclusive'] == evidence['start_ordinal'])
                _require(index > 0 and roles['storage_base_rollout_id'] == chain[index-1]['source_thread_id'] and
                         roles['end_byte_offset'] == chain[index-1]['bytes_read'])
            origin = evidence['source_thread_id']
            lineage.native._thread_ident(origin)
            _require(origin not in origins and evidence['visible_projection_verified'] is True and
                     evidence['prefix_only'] is (index < len(chain) - 1))
            origins.add(origin)
            dependencies.add(origin)
            for key in ('start_ordinal', 'end_ordinal_exclusive', 'bytes_read', 'turn_count',
                        'message_count', 'omitted_projected_items'):
                _count(evidence[key])
            _require(evidence['start_ordinal'] == (0 if previous is None else previous) and
                     evidence['end_ordinal_exclusive'] > evidence['start_ordinal'] and evidence['bytes_read'] > 0)
            previous = evidence['end_ordinal_exclusive']
            _sha(evidence['prefix_sha256'])
            _validate_storage(evidence['storage'], evidence)
            turn_count += evidence['turn_count']
            message_count += evidence['message_count']
            omitted_items += evidence['omitted_projected_items']
            chain_counts[(tid, origin)] = evidence['message_count']
        joined=_validate_frozen_joins(chain,candidate.get('lineage_joins',{}).get(tid,[]),thread['turns'])
        head=chain[-1]
        _require((head['source_thread_id'] == tid or
                  (backup and head.get('storage_binding',{}).get('logical_session_id')==tid)) and
                 turn_count-joined == len(thread['turns']))
        actual_messages = sum(len(turn['messages']) for turn in thread['turns'])
        _require(actual_messages == message_count, 'Prepared review message counts differ')
        totals['turns'] += len(thread['turns'])
        totals['messages'] += actual_messages
        for turn in thread['turns']:
            _require('status' in turn)
            for message in turn['messages']:
                expected_reports.append((tid, origins, turn['id'], message))
    if not backup:
        for item in transfer['projects'] + transfer['sections']:
            lineage.filtering.check_text(item['name'])
    _require(candidate['dependency_ids'] == sorted(dependencies - set(selected)) and
             transfer['omitted_nonmessage_items'] == omitted_items)
    reports = candidate['filtering']
    _require(isinstance(reports, list) and len(reports) == len(expected_reports))
    excluded_count = 0
    for report, (tid, origins, turn_id, message) in zip(reports, expected_reports):
        _exact(report, {'target_thread_id', 'source_thread_id', 'turn_id', 'message_id', 'message_sha256',
                        'omitted_attachment_blocks', 'excluded_text_ranges'} |
                       ({'visible_card_projection'} if 'visible_card_projection' in report else set()))
        if 'visible_card_projection' in report:
            card=report['visible_card_projection']
            _exact(card,{'kind','question_count','option_count','interactive_restoration'})
            _require(card['kind']=='historical_question_card_as_text' and card['interactive_restoration'] is False and message['role']=='assistant')
            _count(card['question_count']);_count(card['option_count'])
        if not backup:
            lineage.filtering.check_projected_message(message['text'],role=message['role'],
                    question_count=report.get('visible_card_projection',{}).get('question_count',0))
        else:
            _require(report['excluded_text_ranges'] == 0, 'Verbatim history contains manual exclusions')
        _require(report['target_thread_id'] == tid and report['source_thread_id'] in origins and
                 report['turn_id'] == turn_id and report['message_id'] == message['id'])
        _sha(report['message_sha256'])
        for key in ('omitted_attachment_blocks', 'excluded_text_ranges'):
            _count(report[key])
        _require(report['omitted_attachment_blocks'] == message.get('omitted_attachments', 0))
        chain_counts[(tid, report['source_thread_id'])] -= 1
        excluded_count += report['omitted_attachment_blocks'] + report['excluded_text_ranges']
    _require(all(value == 0 for value in chain_counts.values()), 'Prepared review origin counts differ')
    _require(isinstance(candidate['excluded_spawn_edges'], list))
    edges = set()
    for edge in candidate['excluded_spawn_edges']:
        _exact(edge, {'parent', 'child'})
        lineage.native._thread_ident(edge['parent'])
        lineage.native._thread_ident(edge['child'])
        pair = (edge['parent'], edge['child'])
        _require(pair not in edges and edge['parent'] != edge['child'] and
                 ((edge['parent'] in selected) != (edge['child'] in selected)))
        edges.add(pair)
    totals['exclusions'] = excluded_count
    return totals


def verify_prepared(destination, expected_manifest_sha256):
    """Verify one caller-pinned local preview; grants no approval or publication.

    Reads only its four bounded files. No source profile, ready result payload,
    native process, network or persistent state is accessed or changed. A pin
    is the SHA256 of exact manifest bytes returned by prepare, not its JSON.
    The result describes this completed read; later use must verify again.
    """
    try:
        _sha(expected_manifest_sha256)
        root = Path(destination)
        _require(root.is_absolute() and '..' not in root.parts and not str(root).startswith(('\\\\', '//'))
                 and all(':' not in part and not part.endswith((' ', '.')) for part in root.parts[1:]),
                 'Prepared review requires an explicit local absolute directory')
        initial = _directory_state(root)
        manifest_bytes = _read_prepared(root, 'review-manifest.json', initial[1]['review-manifest.json'])
        _require(hashlib.sha256(manifest_bytes).hexdigest() == expected_manifest_sha256,
                 'Prepared review manifest differs from the caller pin')
        manifest = _json_bytes(manifest_bytes)
        _exact(manifest, {'format', 'synthetic_only', 'publication_ready', 'profile_applied', 'files', 'candidate_sha256'})
        _require(manifest['format'] == 'rehome-local-review/1' and manifest['synthetic_only'] is True and
                 manifest['publication_ready'] is False and manifest['profile_applied'] is False)
        _sha(manifest['candidate_sha256'])
        _require(isinstance(manifest['files'], list) and len(manifest['files']) == 3)
        hashes = {}
        for entry in manifest['files']:
            _exact(entry, {'path', 'sha256'})
            _require(entry['path'] in _CONTENT_FILES and entry['path'] not in hashes)
            _sha(entry['sha256'])
            hashes[entry['path']] = entry['sha256']
        contents = {name: _read_prepared(root, name, initial[1][name]) for name in _CONTENT_FILES}
        _require(all(hashlib.sha256(contents[name]).hexdigest() == hashes[name] for name in _CONTENT_FILES),
                 'Prepared review file checksum differs')
        candidate, results = _json_bytes(contents['candidate.json']), _json_bytes(contents['results.json'])
        _require(lineage.filtering.digest(candidate) == manifest['candidate_sha256'],
                 'Prepared review canonical candidate checksum differs')
        counts = _validate_candidate(candidate)
        _require(isinstance(results, list))
        for item in results:
            _exact(item, {'display_name', 'sha256', 'bytes'})
            snapshot._validate_relative(item['display_name'])
            _sha(item['sha256'])
            _count(item['bytes'])
        snapshot._check_collisions([item['display_name'] for item in results])
        counts['results'] = len(results)
        _require(render(candidate, results).encode('utf-8') == contents['review.html'],
                 'Prepared review page differs from its verified composition')
        _require(_directory_state(root) == initial, 'Prepared review changed during verification')
        return {'status': 'local_review_verified', 'synthetic_only': True, 'publication_ready': False,
                'profile_applied': False, 'source_freshness_verified': False,
                'result_sources_rechecked': False, 'user_approved': False,
                'manifest_sha256': expected_manifest_sha256,
                'candidate_sha256': manifest['candidate_sha256'], 'file_sha256': hashes,
                'selected_thread_ids': candidate['selected_thread_ids'], 'dependency_ids': candidate['dependency_ids'],
                'source_instance_id': candidate['source_instance_id'],
                'source_inventory_token': candidate['source_inventory_token'], 'counts': counts}
    except ReviewIntegrityError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ReviewIntegrityError('Prepared review cannot be verified') from None


def _escape(value):
    return html.escape(str(value), quote=True)


def render(candidate, results, *, synthetic_only=True):
    notice = ('<div class="notice"><strong>Тестовый предварительный просмотр</strong><br>Здесь только искусственные данные. Отправка в Synology и применение профиля отключены.</div>'
              if synthetic_only else
              '<div class="notice"><strong>Локальная проверка выбранных реальных чатов</strong><br>Это выборочная копия для проверки. Отправка в Synology и применение профиля отключены. Согласование пользователя отсутствует.</div>')
    threads = candidate['transfer']['threads']
    messages = sum(len(turn['messages']) for thread in threads for turn in thread['turns'])
    omitted = sum(r['omitted_attachment_blocks'] + r['excluded_text_ranges'] for r in candidate['filtering'])
    cards = []
    historical_cards={(row['target_thread_id'],row['turn_id'],row['message_id']) for row in candidate['filtering']
                      if 'visible_card_projection' in row}
    for thread in threads:
        chain = candidate['lineage'][thread['id']]
        historical_turns={row['turn_id'] for entry in chain for row in entry.get('historical_partial_turns',[])}
        fork_partial_turns={row['turn_id'] for row in candidate.get('lineage_joins',{}).get(thread['id'],[])}
        closed_tail_turns={entry['closed_snapshot_tail']['turn_id'] for entry in chain if 'closed_snapshot_tail' in entry}
        parts = []
        for i, turn in enumerate(thread['turns'], 1):
            state = {'completed': 'Завершён', 'failed': 'Ошибка', 'interrupted': 'Прерван'}[turn['status']]
            parts.append('<h4>Ход ' + str(i) + ' · ' + state + '</h4>')
            if turn['id'] in historical_turns:
                parts.append('<p class="muted">В источнике этот ход остался незавершённым; последующие сообщения записаны отдельным ходом. В восстановленной истории он отмечается прерванным. Исходное состояние сохранено в сведениях о происхождении.</p>')
            if turn['id'] in fork_partial_turns:
                parts.append('<p class="muted">Унаследован фрагмент хода до точки ветвления. Его прерывание записано в дочерней ветке; поздние сообщения родителя не включены.</p>')
            if turn['id'] in closed_tail_turns:
                parts.append('<p class="muted">На момент закрытого снимка ход оставался незавершённым, завершающего события в полном журнале нет. Видимые сообщения сохранены; при восстановлении ход отмечается прерванным. Исходный статус не изменён.</p>')
            for message in turn['messages']:
                label = 'Вы' if message['role'] == 'user' else 'Ассистент'
                parts.append('<div class="message"><strong>' + label + '</strong><pre>' + _escape(message['text']) + '</pre></div>')
                if (thread['id'],turn['id'],message['id']) in historical_cards:
                    parts.append('<p class="muted">Историческая карточка вопроса сохранена текстом. Интерактивные варианты не восстанавливаются.</p>')
                if message.get('omitted_attachments', 0):
                    parts.append('<p class="muted">Вложения исключены: ' + str(message['omitted_attachments']) + '</p>')
        cards.append('<article><div class="row"><h2>' + _escape(thread['name'] or thread['title']) +
                     '</h2><span class="tag">' + ('Архив' if thread['archived'] else 'Обычный чат') + '</span></div>' +
                     '<p>' + str(len(thread['turns'])) + ' ходов · ' + str(len(chain) - 1) + ' родительских веток</p>' +
                     '<p class="muted">Восстановится полная видимая история до точки ветвления и сообщения этой ветки.</p>' +
                     '<details><summary>Посмотреть сообщения</summary>' + ''.join(parts) + '</details></article>')
    rows = ''.join('<tr><td>' + _escape(r['display_name']) + '</td><td>' + str(r['bytes']) +
                   '</td><td><code>' + _escape(r['sha256'][:16]) + '…</code></td></tr>' for r in results)
    data = {'candidate_sha256': lineage.filtering.digest(candidate), 'results': results}
    fingerprint = lineage.filtering.digest(data)
    return '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>ReHome — состав копии</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f7;color:#202a36;font:16px/1.55 Segoe UI,Arial,sans-serif}
main{max-width:1000px;margin:40px auto;padding:0 24px}header{margin-bottom:26px}h1{font-size:32px;margin:6px 0}h2{font-size:20px;margin:0}
.muted{color:#566371;font-size:14px}.notice{background:#eaf2ff;border-left:4px solid #426eaa;padding:16px 20px;border-radius:8px}
.stats{display:flex;gap:16px;flex-wrap:wrap;margin:24px 0}.stat{background:white;border:1px solid #dae1e7;border-radius:12px;padding:18px 24px;flex:1;min-width:150px}
.stat b{font-size:27px;display:block}article,section{padding:22px;background:white;border:1px solid #dae1e7;border-radius:12px;margin:16px 0}
.row{display:flex;align-items:center;justify-content:space-between;gap:16px}.tag{padding:4px 12px;border-radius:20px;background:#eff2f5;white-space:nowrap;font-size:13px}
summary{cursor:pointer;color:#315e99;padding:8px 0}.message{padding:12px 16px;background:#f6f8fa;border-radius:8px;margin:12px 0}
pre{font:15px/1.55 Segoe UI,Arial,sans-serif;white-space:pre-wrap;overflow-wrap:anywhere;margin:6px 0}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid #e5e9ed;overflow-wrap:anywhere}
code{font-size:12px}footer{padding:20px 0 40px;overflow-wrap:anywhere}
</style><main><header><div class="muted">ReHome · локальная проверка</div><h1>Состав будущей копии</h1>
<p>Просмотрите историю выбранных чатов и готовые файлы.</p></header>
''' + notice + '''
<div class="stats">''' + ''.join('<div class="stat"><b>' + str(value) + '</b>' + label + '</div>' for value, label in (
        (len(threads), 'выбранных чатов'), (messages, 'сообщений'), (len(results), 'готовых файлов'), (omitted, 'исключений'))) + '''</div>
<h2>Чаты и история</h2>''' + ''.join(cards) + '''<section><h2>Готовые файлы</h2>
<p class="muted">Только явно выбранные результаты. Исходные материалы в этот список не включаются.</p>
<table><thead><tr><th>Файл</th><th>Байты</th><th>Контрольная сумма</th></tr></thead><tbody>''' + rows + '''</tbody></table></section>
<section><h2>Что останется за пределами копии</h2><ul><li>Вложения и явно исключённые фрагменты исходных материалов.</li>
<li>Скрытые рассуждения, вывод инструментов и данные авторизации.</li><li>Сообщения родителей, написанные после точки ветвления.</li></ul>
<p class="muted">Нераспознанные исходники или секреты внутри обычного текста требуют отдельной проверки. Эта страница не подтверждает готовность переноса реальных данных.</p></section>
<footer class="muted">Идентификатор состава: <code>''' + fingerprint + '</code></footer></main></html>'


def prepare(profile, selected_ids, destination, *, ready_results=(), exclusions=None):
    # This public preparation route remains synthetic-only until real-data review
    # and publication rules are complete. The reader itself performs no writes.
    profile = context_probe._guard_profile_for_launch(profile)
    destination = snapshot._safe_path(Path(destination).absolute(), allow_missing=True)
    if destination.exists() or destination.is_relative_to(profile):
        raise ValueError('Use a new preview folder outside the source profile')
    candidate = lineage.export_candidate(profile, selected_ids, exclusions=exclusions)
    results = []
    for item in ready_results:
        if not isinstance(item, dict) or set(item) != {'path', 'display_name'}:
            raise ValueError('Select explicit finished files')
        name = snapshot._validate_relative(item['display_name'])
        path = snapshot._safe_path(Path(item['path']).absolute())
        if not path.is_file():
            raise ValueError('Finished result is not a file')
        digest, size = snapshot._hash_file(path)
        results.append({'display_name': name, 'sha256': digest, 'bytes': size})
    snapshot._check_collisions([r['display_name'] for r in results])
    page = render(candidate, results)
    destination.mkdir(parents=True)
    snapshot._write_json(destination / 'candidate.json', candidate)
    snapshot._write_json(destination / 'results.json', results)
    (destination / 'review.html').write_bytes(page.encode('utf-8'))
    files = [{'path': name, 'sha256': snapshot._hash_file(destination / name)[0]} for name in ('candidate.json', 'results.json', 'review.html')]
    manifest = {'format': 'rehome-local-review/1', 'synthetic_only': True, 'publication_ready': False,
                'profile_applied': False, 'files': files, 'candidate_sha256': lineage.filtering.digest(candidate)}
    snapshot._write_json(destination / 'review-manifest.json', manifest)
    return {'status': 'local_review_prepared', 'path': str(destination / 'review.html'), 'manifest': manifest,
            'manifest_sha256': snapshot._hash_file(destination / 'review-manifest.json')[0]}
