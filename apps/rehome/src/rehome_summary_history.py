"""Deterministic native chat projection of agent-authored session summaries.

This is an explicitly lossy representation, never evidence of original turns.
The caller provides verified source boundaries and structured summaries. No
model, chat scanning or source modification happens in this module.
"""
import copy
from pathlib import PureWindowsPath, PurePosixPath
import re
import uuid

import native_adapter as native
import readonly_source
import rehome_display_metadata as display
import rehome_result_links as links
import snapshot

FORMAT = 'rehome-summary-history/1'
POLICY = 'agent-authored-session-summary/1'
IDENTITY_FILE = 'rehome-session-identity.json'
NAMESPACE = uuid.UUID('35b750cc-42b5-4e95-a066-b0434521f426')
_SHA = re.compile(r'^[a-f0-9]{64}$')


def require(value, message='Invalid summarized history'):
    if not value:
        raise ValueError(message)


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields), 'Summary fields differ')


def text(value, *, empty=True):
    require(isinstance(value, str) and len(value) <= 12000 and '\x00' not in value and
            (empty or bool(value.strip())), 'Invalid summary text')
    return value


def lines(value):
    require(isinstance(value, list) and len(value) <= 200, 'Invalid summary list')
    for item in value:
        text(item)


def identity(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}', value),
            'Invalid canonical identity')
    return value


def stamp(value):
    require(type(value) is int and 0 <= value <= 253402300799000, 'Invalid summary timestamp')
    display.timestamp(value, milliseconds=True)
    return value


def native_id(workspace_id, canonical_chat_id):
    return str(uuid.uuid5(NAMESPACE, identity(workspace_id) + '/chat/' + identity(canonical_chat_id)))


def _validate_source(source, *, allow_unavailable=False):
    exact(source, {'workspace_id', 'created_at_ms', 'catalog', 'handoffs', 'results'})
    identity(source['workspace_id']); stamp(source['created_at_ms'])
    catalog = source['catalog']
    exact(catalog, {'projects', 'chats'})
    require(isinstance(catalog['projects'], list) and len(catalog['projects']) <= 10000)
    require(isinstance(catalog['chats'], list) and 0 < len(catalog['chats']) <= 10000)
    projects, chats = set(), set()
    for project in catalog['projects']:
        exact(project, {'id', 'name', 'created_at_ms', 'updated_at_ms'})
        identity(project['id']); text(project['name'], empty=False)
        require(project['id'] not in projects, 'Duplicate project')
        projects.add(project['id'])
        stamp(project['created_at_ms']); stamp(project['updated_at_ms'])
    for chat in catalog['chats']:
        exact(chat, {'canonical_chat_id', 'title', 'project_id', 'archived', 'is_pinned',
                     'created_at_ms', 'updated_at_ms'})
        identity(chat['canonical_chat_id']); text(chat['title'], empty=False)
        require(chat['canonical_chat_id'] not in chats, 'Duplicate canonical chat')
        chats.add(chat['canonical_chat_id'])
        require(chat['project_id'] is None or chat['project_id'] in projects, 'Unknown chat project')
        require(type(chat['archived']) is bool and type(chat['is_pinned']) is bool)
        stamp(chat['created_at_ms']); stamp(chat['updated_at_ms'])
    require(isinstance(source['handoffs'], list) and len(source['handoffs']) == len(chats),
            'Every catalog chat needs an explicit summary record')
    seen = set()
    for handoff in source['handoffs']:
        exact(handoff, {'canonical_chat_id', 'task_record_id', 'task_revision_id', 'source_cursor',
                        'coverage', 'goal', 'outcome', 'decisions', 'constraints', 'unfinished', 'next_steps'})
        cid = handoff['canonical_chat_id']
        require(cid in chats and cid not in seen, 'Summary/chat mapping differs'); seen.add(cid)
        identity(handoff['task_record_id']); identity(handoff['task_revision_id'])
        exact(handoff['source_cursor'], {'last_turn_id', 'visible_sha256'})
        identity(handoff['source_cursor']['last_turn_id'])
        require(isinstance(handoff['source_cursor']['visible_sha256'], str) and
                _SHA.fullmatch(handoff['source_cursor']['visible_sha256']), 'Invalid source boundary digest')
        require(handoff['coverage'] in ('complete', 'partial'), 'Unknown summary coverage')
        text(handoff['goal'], empty=False); text(handoff['outcome'])
        for field in ('constraints', 'unfinished', 'next_steps'):
            lines(handoff[field])
        require(isinstance(handoff['decisions'], list) and len(handoff['decisions']) <= 200)
        for decision in handoff['decisions']:
            exact(decision, {'text', 'reason'}); text(decision['text']); text(decision['reason'])
    require(isinstance(source['results'], list) and len(source['results']) <= links.MAX_FILES)
    result_ids, paths = set(), set()
    for result in source['results']:
        exact(result, {'result_id', 'version_id', 'project_id', 'chat_ids', 'name',
                       'source_path', 'relative_path', 'sha256', 'bytes'})
        identity(result['result_id']); identity(result['version_id'])
        require(result['result_id'] not in result_ids, 'More than one selected version of a logical result')
        result_ids.add(result['result_id'])
        text(result['name'], empty=False)
        require(result['project_id'] is None or result['project_id'] in projects)
        require(isinstance(result['chat_ids'], list) and len(result['chat_ids']) > 0 and
                len(set(result['chat_ids'])) == len(result['chat_ids']) and set(result['chat_ids']) <= chats)
        by_id = {chat['canonical_chat_id']:chat for chat in catalog['chats']}
        require(all(by_id[cid]['project_id'] == result['project_id'] for cid in result['chat_ids']),
                'Result attached to another project')
        require(links.source_key(result['source_path']) is not None, 'Unsupported result source path')
        relative = snapshot._validate_relative(result['relative_path'])
        parts = relative.split('/')
        require(len(parts) >= 4 and parts[:3] == ['Projects', result['project_id'] or '__projectless__', 'Results'],
                'Result payload path differs')
        require(relative not in paths, 'Duplicate result destination'); paths.add(relative)
        unavailable = allow_unavailable and result['sha256'] is None and result['bytes'] is None
        require(unavailable or isinstance(result['sha256'], str) and _SHA.fullmatch(result['sha256']))
        require(unavailable or type(result['bytes']) is int and result['bytes'] >= 0)
    snapshot._check_collisions(list(paths))


def _uri(path):
    pure = PureWindowsPath(path) if re.match(r'^[A-Za-z]:[/\\]', path) else PurePosixPath(path)
    return pure.as_uri()


def render_handoff(handoff, results):
    content = ['Перенесённая сводка ReHome. Исходная переписка в этот чат не включена.']
    if handoff['coverage'] != 'complete':
        content.append('Внимание: сводка составлена по неполной доступной истории.')
    content.extend(['', 'Цель', handoff['goal'], '', 'Результат', handoff['outcome'] or 'Итог пока не зафиксирован.'])
    for title, entries in (
        ('Решения и основания', [d['text'] + '\nОснование: ' + d['reason'] for d in handoff['decisions']]),
        ('Ограничения', handoff['constraints']), ('Не завершено', handoff['unfinished']),
        ('Следующие действия', handoff['next_steps'])):
        if entries:
            content.extend(['', title, *['- ' + entry for entry in entries]])
    if results:
        content.extend(['', 'Последние готовые результаты'])
        for result in results:
            name = result['name'].replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
            content.append('- [' + name + '](<' + _uri(result['source_path']) + '>)')
    return '\n'.join(content)


def build(source, *, allow_unavailable=False):
    """Only structured summaries, never arbitrary supplied native messages."""
    _validate_source(source, allow_unavailable=allow_unavailable)
    source = copy.deepcopy(source)
    created = source['created_at_ms']
    transfer = {'format':native.FORMAT, 'format_version':native.FORMAT_VERSION,
        'codex_version':native.CODEX_VERSION, 'schema_fingerprint':native.SCHEMA_FINGERPRINT,
        'synthetic_only':False, 'projects':[], 'sections':[], 'threads':[],
        'spawn_edges':[], 'omitted_nonmessage_items':0}
    metadata = {'format':display.FORMAT, 'projects':{}, 'sections':{}, 'threads':{}, 'project_order':[]}
    mapping = []
    for index, project in enumerate(source['catalog']['projects']):
        pid = project['id']
        transfer['projects'].append({'id':pid, 'name':project['name']})
        metadata['projects'][pid] = {'position':index, 'created_at_ms':project['created_at_ms'],
                                     'updated_at_ms':project['updated_at_ms']}
        metadata['project_order'].append(pid)
    handoffs = {h['canonical_chat_id']:h for h in source['handoffs']}
    for chat in source['catalog']['chats']:
        cid = chat['canonical_chat_id']; tid = native_id(source['workspace_id'], cid)
        handoff = handoffs[cid]
        turn_id = str(uuid.uuid5(NAMESPACE, tid + '/summary-turn'))
        message_id = str(uuid.uuid5(NAMESPACE, tid + '/summary-message'))
        results = [r for r in source['results'] if cid in r['chat_ids']]
        transfer['threads'].append({'id':tid, 'title':chat['title'], 'name':chat['title'],
            'project_id':chat['project_id'], 'section_id':None, 'archived':chat['archived'],
            'is_pinned':chat['is_pinned'], 'turns':[{'id':turn_id, 'status':'completed', 'messages':[
                {'id':message_id, 'role':'assistant', 'phase':'final_answer', 'text':render_handoff(handoff, results)}]}]})
        made, updated = chat['created_at_ms'], chat['updated_at_ms']
        metadata['threads'][tid] = {'journal_mtime_ns':updated * 1000000, 'values':{
            'created_at':made//1000, 'updated_at':updated//1000,
            'created_at_ms':made, 'updated_at_ms':updated, 'recency_at':updated//1000, 'recency_at_ms':updated,
            'archived_at':updated//1000 if chat['archived'] else None, 'section_position':None,
            'section_entered_at_ms':None}, 'turns':{turn_id:{
                'values':{'started_at':None,'completed_at':None,'duration_ms':None},
                'items':{message_id:{'created_at_ms':created,'started_at_ms':None,'completed_at_ms':None}}}}}
        mapping.append({'canonical_chat_id':cid, 'native_chat_id':tid,
                        'task_record_id':handoff['task_record_id'], 'task_revision_id':handoff['task_revision_id']})
    native._validate_transfer(transfer, synthetic_only=False)
    display.validate(transfer, metadata)
    return {'format':FORMAT, 'text_policy':POLICY, 'source_schema_version':readonly_source.CURRENT_SCHEMA_VERSION,
            'source':source, 'source_sha256':snapshot._digest(source), 'identity_map':mapping,
            'transfer':transfer, 'display_metadata':metadata}


def validate(candidate):
    exact(candidate, {'format','text_policy','source_schema_version','source','source_sha256',
                      'identity_map','transfer','display_metadata'})
    require(candidate['format'] == FORMAT and candidate['text_policy'] == POLICY)
    require(candidate == build(candidate['source']), 'Summary projection or provenance changed')
    threads = candidate['transfer']['threads']
    return {'projects':len(candidate['transfer']['projects']), 'threads':len(threads),
            'turns':len(threads), 'messages':len(threads),
            'partial_summaries':sum(h['coverage'] != 'complete' for h in candidate['source']['handoffs'])}


def identity_receipt(candidate, package_digest):
    if candidate.get('format') != FORMAT:
        return None
    # Candidate supplied here is the original pinned package, not relocated text.
    validate(candidate)
    require(isinstance(package_digest, str) and _SHA.fullmatch(package_digest))
    return {'format':'rehome-session-identity/1', 'workspace_id':candidate['source']['workspace_id'],
            'package_manifest_sha256':package_digest, 'source_sha256':candidate['source_sha256'],
            'identity_map':copy.deepcopy(candidate['identity_map'])}
