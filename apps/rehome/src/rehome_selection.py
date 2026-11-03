"""Local chat/result selection and addressable exclusion review.

Metadata and draft files contain no message bodies. Selected history is inspected
only in memory using the existing verified segment reader. No publication,
profile apply, disk discovery or automatic privacy approval is implemented.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import sqlite3

import rehome_history_lineage as lineage
import rehome_review_sources as managed
import snapshot

LOCAL_AREA = Path(__file__).resolve().parent / 'output/ReHome/selections'
CLASSIFICATIONS = {'generated_finished', 'original', 'undetermined'}
REASONS = {'explicit_generated_finished', 'final_assistant_link_candidate',
           'original_supplied_material', 'requires_user_classification'}
REVIEW_BLOCK_LIMIT = 1_000_000
REVIEW_MEMORY_LIMIT = 8_000_000
KNOWN_CREDENTIAL_FILES = (Path.home()/'.codex/auth.json', Path.home()/'.aws/credentials',
                          Path.home()/'.config/gh/hosts.yml', Path.home()/'AppData/Roaming/GitHub CLI/hosts.yml')


class SelectionError(ValueError):
    def __init__(self, code='selection_invalid'):
        super().__init__(code)
        self.code = code


def _require(value, code='selection_invalid'):
    if not value: raise SelectionError(code)


def pin(value):
    return hashlib.sha256(snapshot._canonical(value)).hexdigest()


def _file_allowed(path, profile):
    path, profile = managed._local(path, missing=True), managed._local(profile, missing=True)
    _require(not path.is_relative_to(profile), 'profile_file_cannot_be_finished_result')
    _require(all(path != known for known in KNOWN_CREDENTIAL_FILES), 'credential_file_cannot_be_finished_result')


def _label(value, fallback):
    try:
        lineage.filtering.check_text(value)
        _require(not any(ord(c) < 32 and c not in '\n\t' for c in value))
        return value[:160]
    except (ValueError, TypeError):
        return fallback


def inventory(profile):
    """Readable metadata only. Unknown status values/reasons are never serialized."""
    profile = managed._local(profile)
    with lineage.source._snapshot(profile, lineage.source.CURRENT_SCHEMA_VERSION) as (conn, glob, root):
        data, _, _ = lineage.source._metadata(conn, glob, root, lineage.source.CURRENT_SCHEMA_VERSION)
        instance = lineage.source._digest({'root': lineage.source._normal_path(str(root)),
                                          'identity': lineage.source._source_identity(root)})
        projects = {p['id']: _label(p['name'], 'Проект: название требует проверки') for p in data['projects']}
        chats = []
        for number, entry in enumerate(data['threads'], 1):
            counts = entry['turn_status_counts']
            open_counts=lineage.historical_open_counts(conn,entry['id']) if counts.get('inProgress',0) else {'historical_open_count':0,'active_tail_count':0}
            inherited_only = (entry['status'] == 'blocked' and
                              entry['reason'] == 'User thread has no available paginated turn history')
            status = ('active' if open_counts['active_tail_count'] else
                      'unsupported' if entry['status'] == 'blocked' and not inherited_only else 'metadata_ready')
            chats.append({'number': number, 'id': entry['id'],
                'name': _label(entry['name'] or entry['title'], 'Чат: название требует проверки'),
                'project_id': entry['project_id'], 'project': projects.get(entry['project_id'], 'Без проекта'),
                'archived': entry['archived'], 'status': status,
                'turn_counts': {k: sum(v for s, v in counts.items() if s == k) for k in
                                ('completed', 'failed', 'interrupted', 'inProgress')},
                'unsupported_turn_count': sum(v for s, v in counts.items() if s not in
                    ('completed', 'failed', 'interrupted', 'inProgress')),
                **open_counts,
                'reason_code': {'active': 'active_deferred', 'unsupported': 'metadata_requires_adapter',
                                'metadata_ready': 'history_not_yet_checked'}[status]})
    return {'format': 'rehome-selection-inventory/1', 'profile': managed._normal(profile),
            'source_schema_version': lineage.source.CURRENT_SCHEMA_VERSION,
            'source_instance_id': instance, 'source_inventory_token': data['inventory_token'],
            'chats': chats, 'projects': [{'id': key, 'name': value} for key, value in projects.items()],
            'metadata_only': True, 'history_text_saved': False, 'publication_ready': False,
            'profile_applied': False, 'user_approved': False}


def _validate_catalog(catalog):
    managed.review._exact(catalog, {'format', 'profile', 'source_schema_version', 'source_instance_id',
        'source_inventory_token', 'chats', 'projects', 'metadata_only', 'history_text_saved',
        'publication_ready', 'profile_applied', 'user_approved'})
    _require(catalog['format'] == 'rehome-selection-inventory/1' and catalog['metadata_only'] is True and
             catalog['history_text_saved'] is False and all(catalog[k] is False for k in
                 ('publication_ready', 'profile_applied', 'user_approved')))
    _require(catalog['source_schema_version'] == lineage.source.CURRENT_SCHEMA_VERSION)
    for key in ('source_instance_id', 'source_inventory_token'): managed.review._sha(catalog[key])
    _require(isinstance(catalog['chats'], list) and isinstance(catalog['projects'], list))
    seen = set()
    for index, chat in enumerate(catalog['chats'], 1):
        managed.review._exact(chat, {'number', 'id', 'name', 'project_id', 'project', 'archived', 'status',
                                     'turn_counts', 'unsupported_turn_count', 'reason_code'}|
                                    ({'historical_open_count','active_tail_count'} if 'historical_open_count' in chat or 'active_tail_count' in chat else set()))
        if 'historical_open_count' in chat:
            for key in ('historical_open_count','active_tail_count'):managed.review._count(chat[key])
        _require(type(chat['number']) is int and chat['number'] == index and chat['id'] not in seen)
        lineage.native._thread_ident(chat['id']); seen.add(chat['id'])
        for key in ('name', 'project'): lineage.filtering.check_text(chat[key])
        _require(type(chat['archived']) is bool and chat['status'] in ('active', 'unsupported', 'metadata_ready'))
        managed.review._exact(chat['turn_counts'], {'completed', 'failed', 'interrupted', 'inProgress'})
        for count in chat['turn_counts'].values(): managed.review._count(count)
        managed.review._count(chat['unsupported_turn_count'])
        _require(chat['reason_code'] in ('active_deferred', 'metadata_requires_adapter', 'history_not_yet_checked'))
    for project in catalog['projects']:
        managed.review._exact(project, {'id', 'name'}); lineage.filtering.check_text(project['name'])
    managed._local(catalog['profile'], missing=True)
    return catalog


def draft(catalog, expected_catalog_pin, item_numbers, *, files=()):
    _validate_catalog(catalog); _require(pin(catalog) == expected_catalog_pin, 'catalog_pin_changed')
    _require(isinstance(item_numbers, list) and item_numbers and len(set(item_numbers)) == len(item_numbers))
    _require(all(type(n) is int and 1 <= n <= len(catalog['chats']) for n in item_numbers))
    selected = [catalog['chats'][n-1]['id'] for n in item_numbers]
    prepared_files = []
    for item in files:
        managed.review._exact(item, {'path', 'display_name', 'classification', 'reason'})
        _file_allowed(item['path'], catalog['profile'])
        entry = copy.deepcopy(item)
        entry['source_binding'] = (managed._result_binding({'path': item['path'], 'display_name': item['display_name'],
                                    'classification': managed.CLASSIFICATION}) if item['classification'] == 'generated_finished' else None)
        prepared_files.append(entry)
    value = {'format': 'rehome-selection/1', 'profile': catalog['profile'],
        'source_schema_version': catalog['source_schema_version'], 'source_instance_id': catalog['source_instance_id'],
        'source_inventory_token': catalog['source_inventory_token'], 'inventory_pin': expected_catalog_pin,
        'selected_thread_ids': selected, 'files': prepared_files, 'omitted_files': [], 'exclusions': {},
        'inspection': None, 'publication_ready': False, 'profile_applied': False, 'user_approved': False,
        'coverage': {'inventory_chats': len(catalog['chats']), 'selected_chats': len(selected),
                     'pending_outside_selection': len(catalog['chats']) - len(selected),
                     'active_in_inventory': sum(c['status'] == 'active' for c in catalog['chats']),
                     'unsupported_in_inventory': sum(c['status'] == 'unsupported' for c in catalog['chats']),
                     'complete_profile_coverage': False}}
    validate(value)
    return value


def validate(value):
    managed.review._exact(value, {'format', 'profile', 'source_schema_version', 'source_instance_id',
        'source_inventory_token', 'inventory_pin', 'selected_thread_ids', 'files', 'omitted_files', 'exclusions', 'inspection',
        'publication_ready', 'profile_applied', 'user_approved', 'coverage'})
    _require(value['format'] == 'rehome-selection/1' and value['source_schema_version'] == lineage.source.CURRENT_SCHEMA_VERSION)
    _require(all(value[k] is False for k in ('publication_ready', 'profile_applied', 'user_approved')))
    _require(value['profile'] == managed._normal(managed._local(value['profile'], missing=True)))
    for key in ('source_instance_id', 'source_inventory_token', 'inventory_pin'): managed.review._sha(value[key])
    selected = value['selected_thread_ids']
    _require(isinstance(selected, list) and selected and len(set(selected)) == len(selected))
    for ident in selected: lineage.native._thread_ident(ident)
    _require(isinstance(value['files'], list) and isinstance(value['omitted_files'], list))
    for item in value['files'] + value['omitted_files']:
        managed.review._exact(item, {'path', 'display_name', 'classification', 'reason', 'source_binding'})
        _require(item['classification'] in CLASSIFICATIONS and item['reason'] in REASONS)
        _require(item['path'] == managed._normal(managed._local(item['path'], missing=True)))
        _file_allowed(item['path'], value['profile'])
        snapshot._validate_relative(item['display_name'])
        binding = item['source_binding']
        if item['classification'] == 'generated_finished':
            managed.review._exact(binding, {'path', 'display_name', 'classification', 'physical_identity', 'bytes', 'sha256'})
            _require(binding['path'] == item['path'] and binding['display_name'] == item['display_name'] and
                     binding['classification'] == managed.CLASSIFICATION)
            managed.review._sha(binding['sha256']); managed.review._count(binding['bytes'])
            _require(isinstance(binding['physical_identity'], list) and len(binding['physical_identity']) == 2 and
                     all(type(n) is int for n in binding['physical_identity']) and binding['physical_identity'][1] > 0)
        else: _require(binding is None)
    _require(all(item['classification'] == 'original' for item in value['omitted_files']))
    snapshot._check_collisions([item['display_name'] for item in value['files']])
    managed._exclusions(value['exclusions'])
    coverage = value['coverage']
    managed.review._exact(coverage, {'inventory_chats', 'selected_chats', 'pending_outside_selection',
        'active_in_inventory', 'unsupported_in_inventory', 'complete_profile_coverage'})
    for key in set(coverage) - {'complete_profile_coverage'}: managed.review._count(coverage[key])
    _require(coverage['selected_chats'] == len(selected) and coverage['inventory_chats'] ==
             len(selected) + coverage['pending_outside_selection'] and coverage['complete_profile_coverage'] is False)
    if value['inspection'] is not None:
        _validate_inspection(value['inspection'])
        _require(value['inspection']['source_inventory_token']==value['source_inventory_token'] and
                 value['inspection']['source_instance_id']==value['source_instance_id'] and
                 set(value['inspection']['source_lineage'])==set(selected) and
                 set(value['inspection']['source_joins'])==set(selected),'inspection_source_binding_changed')
    return value


def _validate_inspection(value):
    managed.review._exact(value, {'format', 'source_inventory_token', 'source_instance_id', 'items','source_lineage','source_joins',
                                  'unresolved_count', 'status', 'privacy_approved'})
    _require(value['format'] == 'rehome-exclusion-inspection/1' and value['privacy_approved'] is False and
             value['status'] in ('resolved_for_local_filter', 'needs_review'))
    for key in ('source_inventory_token', 'source_instance_id'): managed.review._sha(value[key])
    managed.review._count(value['unresolved_count'])
    _require(isinstance(value['items'], list))
    _require(isinstance(value['source_lineage'],dict))
    for selected,chain in value['source_lineage'].items():
        lineage.native._thread_ident(selected)
        _require(isinstance(chain,list) and 1<=len(chain)<=128)
        for evidence in chain:
            managed.review._exact(evidence,{'source_thread_id','start_ordinal','end_ordinal_exclusive','bytes_read',
                'prefix_sha256','visible_projection_verified','prefix_only','storage','turn_count','message_count','omitted_projected_items'}|
                ({'fork_reference_roles'} if 'fork_reference_roles' in evidence else set())|
                ({'storage_binding'} if 'storage_binding' in evidence else set())|
                ({'historical_partial_turns'} if 'historical_partial_turns' in evidence else set())|
                ({'open_prefix_turn'} if 'open_prefix_turn' in evidence else set())|
                ({'terminal_only_abort_events'} if 'terminal_only_abort_events' in evidence else set()))
            managed.review._validate_frozen_segment_metadata(evidence)
            if 'historical_partial_turns' in evidence:managed.review._validate_historical_partial(evidence)
            lineage.native._thread_ident(evidence['source_thread_id']);managed.review._sha(evidence['prefix_sha256'])
            for key in ('start_ordinal','end_ordinal_exclusive','bytes_read','turn_count','message_count','omitted_projected_items'):
                managed.review._count(evidence[key])
            _require(evidence['visible_projection_verified'] is True and type(evidence['prefix_only']) is bool)
            managed.review._validate_storage(evidence['storage'],evidence)
            if 'fork_reference_roles' in evidence:
                roles=evidence['fork_reference_roles']
                managed.review._exact(roles,{'format','writer_version','provenance_thread_id','storage_base_rollout_id',
                    'end_ordinal_exclusive','end_byte_offset','provenance_is_history_dependency'})
                _require(roles['format']=='rehome-fork-reference-roles/1' and roles['writer_version'] in lineage.SPLIT_FORK_REFERENCE_WRITERS and
                         roles['provenance_is_history_dependency'] is False)
                for key in ('provenance_thread_id','storage_base_rollout_id'):lineage.native._thread_ident(roles[key])
                for key in ('end_ordinal_exclusive','end_byte_offset'):managed.review._count(roles[key])
            if 'storage_binding' in evidence:
                binding=evidence['storage_binding']
                managed.review._exact(binding,{'format','physical_rollout_id','logical_session_id','projection_storage_key','header_sha256'})
                _require(binding['format']=='rehome-storage-source-role/1')
                for key in ('physical_rollout_id','logical_session_id','projection_storage_key'):lineage.native._thread_ident(binding[key])
                managed.review._sha(binding['header_sha256'])
    _require(isinstance(value['source_joins'],dict) and set(value['source_joins'])==set(value['source_lineage']))
    for selected,joins in value['source_joins'].items():managed.review._validate_frozen_joins(value['source_lineage'][selected],joins)
    for number, item in enumerate(value['items'], 1):
        managed.review._exact(item, {'number', 'source_thread_id', 'turn_id', 'message_id',
                                     'message_sha256', 'role', 'blocks', 'omitted_attachment_count'})
        _require(item['number'] == number and type(item['number']) is int)
        lineage.native._thread_ident(item['source_thread_id']); managed.review._sha(item['message_sha256'])
        _require(all(isinstance(item[k], str) and item[k] for k in ('turn_id', 'message_id')) and
                 item['role'] in ('user', 'assistant') and isinstance(item['blocks'], list))
        managed.review._count(item['omitted_attachment_count'])
        for block in item['blocks']:
            managed.review._exact(block, {'index', 'length', 'problem_codes', 'action'})
            managed.review._count(block['index']); managed.review._count(block['length'])
            _require(block['problem_codes'] in ([], ['possible_secret'], ['embedded_source'], ['possible_secret', 'embedded_source']))
            _require(block['action'] in ('keep_visible', 'exclude_text', 'needs_review'))


def _current(value):
    catalog = inventory(value['profile'])
    _require(catalog['source_instance_id'] == value['source_instance_id'] and
             catalog['source_inventory_token'] == value['source_inventory_token'], 'source_inventory_changed')
    return catalog


def inspect(value, expected_pin, *, journal_decoder=None, private_memory_sink=None):
    """Addressable metadata only. Unknown protocols fail; never serialize raw errors."""
    if private_memory_sink is not None: private_memory_sink.clear()
    validate(value); _require(pin(value) == expected_pin, 'selection_pin_changed')
    catalog = _current(value)
    entries = {item['id']: item for item in catalog['chats']}
    _require(all(entries.get(tid, {}).get('status') == 'metadata_ready' for tid in value['selected_thread_ids']),
             'selected_active_or_unsupported')
    discovered, segments, used,chains,source_joins = {}, {}, set(),{},{}
    try:
        with lineage.source._snapshot(value['profile'], lineage.source.CURRENT_SCHEMA_VERSION) as (conn, _, root):
            for selected in value['selected_thread_ids']:
                seen, tid, cutoff,order,raw_chain = set(), selected, None,[],[]
                while True:
                    _require(tid not in seen and len(seen) < 128, 'unsupported_lineage')
                    seen.add(tid)
                    key = (tid, None if cutoff is None else cutoff['end_ordinal_exclusive'],
                           None if cutoff is None else cutoff['end_byte_offset'])
                    if key not in segments:
                        segments[key] = lineage.segment(conn, root, tid, cutoff, decoder=journal_decoder)
                    part = segments[key]
                    order.append(part['evidence'])
                    raw_chain.append((tid,part))
                    for turn in part['turns']:
                        for raw in turn['items']:
                            identity = tid + '/' + turn['id'] + '/' + raw['id']
                            if identity in discovered:
                                _require(discovered[identity][0] == lineage.filtering.digest(raw), 'changing_inherited_message')
                            else: discovered[identity] = (lineage.filtering.digest(raw), tid, turn['id'], raw)
                    base = part['dependency']
                    if base is None: break
                    tid, cutoff = base['thread_id'], base
                chains[selected]=list(reversed(order))
                _,source_joins[selected]=lineage.assemble_chain(list(reversed(raw_chain)))
            for (tid, ordinal, offset), part in segments.items():
                cutoff = None if ordinal is None else {'end_ordinal_exclusive': ordinal, 'end_byte_offset': offset}
                _require(lineage.segment(conn, root, tid, cutoff, decoder=journal_decoder) == part, 'source_changed_during_inspection')
        items, unresolved = [], 0
        captured, captured_characters = {}, 0
        for identity, (digest, origin, turn_id, raw) in discovered.items():
            exclusion = value['exclusions'].get(identity)
            if exclusion: used.add(identity)
            source_blocks = (raw['content'] if raw['type'] == 'userMessage' else
                             [{'type':'text','text':text} for text in lineage.filtering.agent_blocks(raw)[0]])
            blocks, attachments = [], 0
            for index, block in enumerate(source_blocks):
                if block['type'] in lineage.filtering.ATTACHMENT_TYPES:
                    attachments += 1; continue
                _require(block['type'] == 'text' and isinstance(block.get('text'), str), 'unsupported_visible_content')
                text = block['text']
                role = 'assistant' if raw['type']=='agentMessage' else 'user'
                problems = lineage.filtering.visible_block_problems(text,role=role,block_index=index)
                if private_memory_sink is not None:
                    captured_characters += len(text)
                    _require(len(text) <= REVIEW_BLOCK_LIMIT and captured_characters <= REVIEW_MEMORY_LIMIT,
                             'review_memory_limit')
                    captured[(origin, turn_id, raw['id'], index)] = text
                ranges = exclusion['text_ranges'].get(str(index)) if exclusion else None
                if ranges is not None:
                    _require(exclusion['message_sha256'] == digest, 'excluded_message_changed')
                    text = lineage.filtering._exclude(text, ranges)
                try:
                    lineage.filtering.check_visible_block(text,role=role,block_index=index)
                    action = 'exclude_text' if ranges else 'keep_visible'
                except lineage.filtering.HistoryReviewRequired:
                    action = 'needs_review'; unresolved += 1
                blocks.append({'index': index, 'length': len(block['text']), 'problem_codes': problems, 'action': action})
            if not any(block['action'] == 'needs_review' for block in blocks):
                # Enforce annotated/structured content contract, exact digest and
                # unused range rejection even when no plain-text trigger fires.
                lineage.filtering.visible_message(raw, exclusion)
            items.append({'number': len(items)+1, 'source_thread_id': origin, 'turn_id': turn_id,
                          'message_id': raw['id'], 'message_sha256': digest,
                          'role': 'user' if raw['type'] == 'userMessage' else 'assistant',
                          'blocks': blocks, 'omitted_attachment_count': attachments})
        _require(used == set(value['exclusions']), 'unused_exclusion')
        _current(value)
    except SelectionError: raise
    except lineage.LineageError as error:
        code = {'Conflicting fork references': 'fork_provenance_conflict_unsupported',
                'Repeated session metadata': 'repeated_session_metadata_unsupported'}.get(str(error), 'history_unsupported_or_changed')
        raise SelectionError(code) from None
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
        raise SelectionError('history_unsupported_or_changed') from None
    for item in value['files']:
        if item['classification'] != 'generated_finished': unresolved += 1
        else:
            _require(managed._result_binding({'path': item['path'], 'display_name': item['display_name'],
                                              'classification': managed.CLASSIFICATION}) == item['source_binding'],
                     'finished_result_changed')
    result = copy.deepcopy(value)
    result['inspection'] = {'format': 'rehome-exclusion-inspection/1',
        'source_inventory_token': value['source_inventory_token'], 'source_instance_id': value['source_instance_id'],
        'items': items,'source_lineage':chains,'source_joins':source_joins, 'unresolved_count': unresolved,
        'status': 'needs_review' if unresolved else 'resolved_for_local_filter', 'privacy_approved': False}
    validate(result)
    # The caller receives visible blocks only after ALL source/result/metadata
    # checks passed. Drafts, CLI returns and diagnostics never contain this cache.
    if private_memory_sink is not None: private_memory_sink.update(captured)
    return result


def validate_candidate_binding(value,candidate):
    """Exact inspected origin/digest/lineage coverage, deduplicating inherited messages."""
    validate(value)
    _require(value['inspection'] is not None,'inspection_required')
    for key in ('source_instance_id','source_schema_version','source_inventory_token','selected_thread_ids'):
        _require(value[key]==candidate[key],'candidate_inspection_source_differs')
    _require(value['inspection']['source_lineage']==candidate['lineage'],'candidate_inspection_lineage_differs')
    expected_joins=candidate.get('lineage_joins',{selected:[] for selected in value['selected_thread_ids']})
    _require(value['inspection']['source_joins']==expected_joins,'candidate_inspection_joins_differs')
    expected={}
    for item in value['inspection']['items']:
        key=tuple(item[k] for k in ('source_thread_id','turn_id','message_id'))
        _require(key not in expected,'duplicate_inspection_message')
        expected[key]=item
    roles={(thread['id'],turn['id'],message['id']):message['role'] for thread in candidate['transfer']['threads']
           for turn in thread['turns'] for message in turn['messages']}
    actual={}
    for row in candidate['filtering']:
        key=tuple(row[k] for k in ('source_thread_id','turn_id','message_id'))
        _require(key in expected,'candidate_inspection_coverage_differs')
        inspected=expected[key]
        _require(row['message_sha256']==inspected['message_sha256'] and
                 roles.get((row['target_thread_id'],row['turn_id'],row['message_id']))==inspected['role'] and
                 row['omitted_attachment_blocks']==inspected['omitted_attachment_count'],'candidate_inspection_message_differs')
        ranges=value['exclusions'].get('/'.join(key),{}).get('text_ranges',{})
        _require(row['excluded_text_ranges']==sum(len(items) for items in ranges.values()),'candidate_inspection_exclusion_differs')
        _require(key not in actual or actual[key]==row['message_sha256'],'inherited_message_digest_differs')
        actual[key]=row['message_sha256']
    _require(set(actual)==set(expected),'candidate_inspection_coverage_differs')
    return True


def exclude_block(value, expected_pin, message_number, block_index, *, journal_decoder=None):
    """Exclude one whole inspected text block; caller never computes a digest/range."""
    current = inspect(value, expected_pin, journal_decoder=journal_decoder)
    _require(type(message_number) is int and 1 <= message_number <= len(current['inspection']['items']))
    item = current['inspection']['items'][message_number-1]
    blocks = [block for block in item['blocks'] if block['index'] == block_index]
    _require(type(block_index) is int and len(blocks) == 1 and blocks[0]['length'] > 0)
    key = '/'.join(item[k] for k in ('source_thread_id', 'turn_id', 'message_id'))
    entry = current['exclusions'].setdefault(key, {'message_sha256': item['message_sha256'], 'text_ranges': {}})
    entry['text_ranges'][str(block_index)] = [[0, blocks[0]['length']]]
    return inspect(current, pin(current), journal_decoder=journal_decoder)


def read_inspected_block(value, expected_pin, message_number, block_index, *, journal_decoder=None):
    """One verified visible text block, returned only in memory to native editor.

    Caller must never serialize this return value or its text in diagnostics.
    Walk the same verified segments so inherited prefixes remain bounded.
    """
    current = inspect(value, expected_pin, journal_decoder=journal_decoder)
    _require(type(message_number) is int and 1 <= message_number <= len(current['inspection']['items']))
    target = current['inspection']['items'][message_number-1]
    matches = []
    with lineage.source._snapshot(value['profile'], lineage.source.CURRENT_SCHEMA_VERSION) as (conn, _, root):
        for selected in value['selected_thread_ids']:
            tid, cutoff, seen = selected, None, set()
            while True:
                _require(tid not in seen and len(seen) < 128, 'unsupported_lineage'); seen.add(tid)
                part = lineage.segment(conn, root, tid, cutoff, decoder=journal_decoder)
                if tid == target['source_thread_id']:
                    for turn in part['turns']:
                        for item in turn['items']:
                            if turn['id'] == target['turn_id'] and item['id'] == target['message_id']:
                                _require(lineage.filtering.digest(item) == target['message_sha256'], 'excluded_message_changed')
                                content = (item['content'] if item['type'] == 'userMessage' else
                                           [{'type':'text','text':text} for text in lineage.filtering.agent_blocks(item)[0]])
                                _require(type(block_index) is int and 0 <= block_index < len(content) and
                                         content[block_index]['type'] == 'text', 'text_block_required')
                                text = content[block_index]['text']; _require(len(text) <= 1_000_000, 'editor_block_limit')
                                matches.append(text)
                base = part['dependency']
                if base is None: break
                tid, cutoff = base['thread_id'], base
    _require(matches and all(text == matches[0] for text in matches), 'block_not_consistent')
    _current(value)
    return {'text': matches[0], 'message_sha256': target['message_sha256']}


def unicode_selection_range(original, prefix, selected):
    """Offsets are Python Unicode codepoints, including non-BMP characters."""
    _require(isinstance(original, str) and isinstance(prefix, str) and isinstance(selected, str) and selected and
             original.startswith(prefix + selected), 'invalid_text_selection')
    return [len(prefix), len(prefix) + len(selected)]


def exclude_ranges(value, expected_pin, message_number, block_index, ranges, expected_message_sha256,
                   *, journal_decoder=None):
    current = inspect(value, expected_pin, journal_decoder=journal_decoder)
    block = read_inspected_block(current, pin(current), message_number, block_index, journal_decoder=journal_decoder)
    _require(block['message_sha256'] == expected_message_sha256, 'excluded_message_changed')
    target = current['inspection']['items'][message_number-1]
    key = '/'.join(target[k] for k in ('source_thread_id', 'turn_id', 'message_id'))
    entry = current['exclusions'].setdefault(key, {'message_sha256': expected_message_sha256, 'text_ranges': {}})
    merged = sorted(entry['text_ranges'].get(str(block_index), []) + ranges)
    try: lineage.filtering._exclude(block['text'], merged)
    except ValueError: raise SelectionError('overlapping_or_invalid_ranges') from None
    entry['text_ranges'][str(block_index)] = merged
    return inspect(current, pin(current), journal_decoder=journal_decoder)


def classify_file(value, expected_pin, number, classification, reason):
    validate(value); _require(pin(value) == expected_pin, 'selection_pin_changed')
    _require(type(number) is int and 1 <= number <= len(value['files']))
    result = copy.deepcopy(value); result['files'][number-1].update(classification=classification, reason=reason)
    item = result['files'][number-1]
    item['source_binding'] = (managed._result_binding({'path': item['path'], 'display_name': item['display_name'],
                                'classification': managed.CLASSIFICATION}) if classification == 'generated_finished' else None)
    result['inspection'] = None; validate(result)
    return result


def omit_original(value, expected_pin, number):
    validate(value); _require(pin(value) == expected_pin, 'selection_pin_changed')
    _require(type(number) is int and 1 <= number <= len(value['files']))
    _require(value['files'][number-1]['classification'] == 'original', 'classify_original_first')
    result = copy.deepcopy(value)
    result['omitted_files'].append(result['files'].pop(number-1)); result['inspection'] = None
    return validate(result)


def reselect_chats(value, expected_pin, catalog, expected_catalog_pin, selected_ids, *, journal_decoder=None):
    """Explicit operator choice using stable IDs from one pinned readable catalog."""
    validate(value);_validate_catalog(catalog)
    _require(pin(value)==expected_pin and pin(catalog)==expected_catalog_pin,'selection_pin_changed')
    _require(catalog['profile']==value['profile'] and catalog['source_instance_id']==value['source_instance_id'],'source_instance_changed')
    _require(isinstance(selected_ids,list) and selected_ids and len(set(selected_ids))==len(selected_ids))
    by_id={row['id']:row for row in catalog['chats']};_require(set(selected_ids)<=set(by_id),'selected_chat_missing')
    files=[{key:item[key] for key in ('path','display_name','classification','reason')} for item in value['files']]
    revised=draft(catalog,expected_catalog_pin,[by_id[ident]['number'] for ident in selected_ids],files=files)
    _require([item['source_binding'] for item in revised['files']]==[item['source_binding'] for item in value['files']],'finished_result_changed')
    revised['omitted_files']=copy.deepcopy(value['omitted_files'])
    try:
        metadata=inspect(revised,pin(revised),journal_decoder=journal_decoder)
        visible={'/'.join(item[k] for k in ('source_thread_id','turn_id','message_id')):item['message_sha256']
                 for item in metadata['inspection']['items']}
        applicable={key:entry for key,entry in value['exclusions'].items() if key in visible}
        _require(all(entry['message_sha256']==visible[key] for key,entry in applicable.items()),'excluded_message_changed')
        revised['exclusions']=applicable
        return inspect(revised,pin(revised),journal_decoder=journal_decoder)
    except SelectionError as error:
        if error.code!='selected_active_or_unsupported':raise
        # Deferred selection keeps all prior exclusions; no source bodies can be
        # prepared until an inactive fresh inspection reconciles their addresses.
        revised['exclusions']=copy.deepcopy(value['exclusions']);revised['inspection']=None
        return validate(revised)


def add_file(value, expected_pin, path, display_name, classification):
    validate(value);_require(pin(value)==expected_pin,'selection_pin_changed')
    _require(classification in CLASSIFICATIONS)
    path=managed._local(path);_file_allowed(path,value['profile'])
    reason={'generated_finished':'explicit_generated_finished','original':'original_supplied_material',
            'undetermined':'requires_user_classification'}[classification]
    item={'path':managed._normal(path),'display_name':snapshot._validate_relative(display_name),
          'classification':classification,'reason':reason,'source_binding':None}
    if classification=='generated_finished':
        item['source_binding']=managed._result_binding({'path':item['path'],'display_name':display_name,'classification':managed.CLASSIFICATION})
    revised=copy.deepcopy(value)
    revised['omitted_files' if classification=='original' else 'files'].append(item)
    revised['inspection']=None
    return validate(revised)


def write_local(path, value):
    """Only new local selection files, outside profiles and known shared roots."""
    path = managed._local(path, missing=True)
    root = managed._local(LOCAL_AREA)
    _require(path.parent == root and not path.exists(), 'new_local_selection_path_required')
    _require(path.suffix == '.json' and not managed._overlap(root, managed._local(value['profile'], missing=True)))
    for forbidden in managed.KNOWN_EXCLUDED_ROOTS:
        _require(not managed._overlap(root, forbidden))
    data = snapshot._canonical(value); _require(len(data) <= 16*1024*1024)
    identity = managed._identity(root)
    with path.open('xb') as stream: stream.write(data)
    _require(managed._identity(root) == identity and snapshot._hash_file(path)[0] == hashlib.sha256(data).hexdigest(),
             'selection_changed_during_write')
    return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(), 'publication_ready': False,
            'profile_applied': False, 'user_approved': False}


def read_local(path, expected_pin):
    path = managed._local(path)
    _require(path.parent == managed._local(LOCAL_AREA), 'local_selection_area_required')
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= 16*1024*1024)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        _require(snapshot._fingerprint(opened) == snapshot._fingerprint(info))
        data = stream.read(16*1024*1024+1)
        after = os.fstat(stream.fileno())
        _require(len(data) == info.st_size and len(data) <= 16*1024*1024 and
                 snapshot._fingerprint(after) == snapshot._fingerprint(opened) and
                 snapshot._content_state(after) == snapshot._content_state(opened))
    _require(hashlib.sha256(data).hexdigest() == expected_pin)
    value = managed.review._json_bytes(data)
    _require(snapshot._canonical(value) == data, 'canonical_local_selection_required')
    managed._local(path)
    _require(snapshot._fingerprint(path.lstat()) == snapshot._fingerprint(info) and
             snapshot._content_state(path.lstat()) == snapshot._content_state(info))
    return value
