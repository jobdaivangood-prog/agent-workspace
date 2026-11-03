"""Read-only catalog and fixed public-message boundaries for session Finish."""
import hashlib
import json
from pathlib import Path

import readonly_source as source
import rehome_summary_history as summaries
import snapshot
import rehome_journal as journal
import rehome_history_lineage as lineage


def _journal_boundary(db, root, tid, decoder=None):
    """Pin the physical stream as well as the SQL projection; emit no content."""
    try:
        row=db.execute('SELECT rollout_path FROM threads WHERE id=?',(tid,)).fetchone()
        _,path,compressed=journal.resolve_journal(root,row['rollout_path'])
        digest,size=snapshot._hash_file(path)
        verified=False
        try:
            segment=lineage.segment(db,root,tid,decoder=decoder,backup_records=True)
            verified=segment['dependency'] is None
        except (ValueError,OSError):
            pass
        summaries.require(snapshot._hash_file(path)==(digest,size),'Journal changed during inspection')
        return {'path':path.relative_to(root).as_posix(),'compressed':compressed,
                'sha256':digest,'bytes':size,'projection_verified':verified}
    except (ValueError,OSError):
        return None


def _items(db, tid):
    return db.execute("SELECT turn_id,item_id,item_json,rollout_ordinal FROM history.thread_items "
        "WHERE thread_id=? AND item_type IN ('userMessage','agentMessage') "
        "ORDER BY rollout_ordinal,item_id", (tid,))


def _boundary(db, tid, limit=None):
    digest = hashlib.sha256()
    count, last_item, last_turn, total = 0, None, None, 0
    for item in _items(db, tid):
        total += 1
        if limit is not None and count >= limit:
            continue
        # Parse and canonicalize JSON so database serialization alone is not a change.
        digest.update(snapshot._canonical({'turn_id':item['turn_id'], 'item_id':item['item_id'],
            'item':json.loads(item['item_json']), 'ordinal':item['rollout_ordinal']}))
        count += 1
        last_item, last_turn = item['item_id'], item['turn_id']
    return {'native_chat_id':tid, 'item_count':count, 'last_item_id':last_item,
            'last_turn_id':last_turn, 'visible_sha256':digest.hexdigest()}, total


def inspect(profile, *, workspace_id, previous_candidate=None, decoder=None):
    """No raw messages leave this function; an agent reads/summarizes separately."""
    summaries.identity(workspace_id)
    aliases = {}
    identity_file = Path(profile) / summaries.IDENTITY_FILE
    if identity_file.exists():
        summaries.require(previous_candidate is not None, 'Restored identity needs its pinned publication')
        candidate, digest = previous_candidate
        expected = summaries.identity_receipt(candidate, digest)
        summaries.require(expected is not None and expected['workspace_id'] == workspace_id and
                          snapshot._read_json(snapshot._safe_path(identity_file)) == expected,
                          'Source identity differs from accepted publication')
        aliases = {row['native_chat_id']:row['canonical_chat_id'] for row in expected['identity_map']}
    catalog = {'projects':[], 'chats':[]}
    boundaries, issues = [], []
    with source._snapshot(profile, source.CURRENT_SCHEMA_VERSION) as (db, glob, root):
        projects = source._project_tables(db)
        migration = source._host_mapping(glob, root)
        dates = {row['id']:dict(row) for row in db.execute('SELECT id,created_at_ms,updated_at_ms,position FROM projects')}
        for project in sorted(projects.values(), key=lambda p:(dates[p['id']]['position'],p['id'])):
            catalog['projects'].append({'id':project['id'], 'name':project['name'],
                'created_at_ms':dates[project['id']]['created_at_ms'], 'updated_at_ms':dates[project['id']]['updated_at_ms']})
        for row in db.execute('SELECT * FROM threads ORDER BY id'):
            kind = row['source']
            try:
                decoded = json.loads(kind)
                kind = next(iter(decoded)) if isinstance(decoded, dict) and len(decoded) == 1 else decoded
            except (ValueError, TypeError):
                pass
            if kind == 'subagent':
                continue
            boundary, _ = _boundary(db, row['id'])
            boundary['journal']=_journal_boundary(db,root,row['id'],decoder)
            if not boundary['item_count'] and not row['name'] and not row['title'] and not row['archived']:
                continue
            canonical = aliases.get(row['id'], row['id'])
            summaries.identity(canonical)
            try:
                pid, _ = source._thread_project(dict(row), glob, projects, migration)
            except (ValueError, source.SourceError):
                # Never silently relocate a chat into another project.
                raise ValueError('Chat project association is unavailable') from None
            if not boundary['item_count']:
                issues.append({'chat_id':canonical,'reason':'no_materialized_visible_history'})
            if not boundary['journal'] or not boundary['journal']['projection_verified']:
                issues.append({'chat_id':canonical,'reason':'journal_projection_not_proven'})
            made = row['created_at_ms'] if row['created_at_ms'] is not None else row['created_at'] * 1000
            updated = row['updated_at_ms'] if row['updated_at_ms'] is not None else row['updated_at'] * 1000
            catalog['chats'].append({'canonical_chat_id':canonical, 'title':row['name'] or row['title'] or 'Без названия',
                'project_id':pid, 'archived':bool(row['archived']), 'is_pinned':bool(row['is_pinned']),
                'created_at_ms':made, 'updated_at_ms':updated})
            boundaries.append({**boundary, 'canonical_chat_id':canonical})
    summaries.require(len({c['canonical_chat_id'] for c in catalog['chats']}) == len(catalog['chats']),
                      'Ambiguous original and restored chat identities')
    return {'format':'rehome-session-inspection/1','workspace_id':workspace_id,
            'catalog':catalog,'boundaries':boundaries,'issues':issues}


def verify_boundaries(profile, inspection, *, decoder=None):
    """Check exact saved prefixes after close; report, never hide, later messages."""
    summaries.exact(inspection, {'format','workspace_id','catalog','boundaries','issues'})
    summaries.require(inspection['format'] == 'rehome-session-inspection/1')
    expected_ids = {c['canonical_chat_id'] for c in inspection['catalog']['chats']}
    rows = inspection['boundaries']
    summaries.require(isinstance(rows,list) and len(rows)==len(expected_ids) and
                      {r['canonical_chat_id'] for r in rows}==expected_ids)
    tails=[]
    with source._snapshot(profile, source.CURRENT_SCHEMA_VERSION) as (db, _, root):
        for row in rows:
            summaries.exact(row, {'native_chat_id','canonical_chat_id','item_count','last_item_id','last_turn_id','visible_sha256','journal'})
            summaries.require(type(row['item_count']) is int and row['item_count'] >= 0)
            summaries.require(db.execute('SELECT 1 FROM threads WHERE id=?',(row['native_chat_id'],)).fetchone(),
                              'A selected chat disappeared after summary preparation')
            current,total = _boundary(db,row['native_chat_id'],row['item_count'])
            expected = {k:v for k,v in row.items() if k not in ('canonical_chat_id','journal')}
            summaries.require(current==expected,'Saved public history changed; summary must be refreshed')
            if total>row['item_count']:
                tails.append({'chat_id':row['canonical_chat_id'],'reason':'messages_after_summary_boundary',
                              'messages':total-row['item_count']})
            old=row['journal'];now=_journal_boundary(db,root,row['native_chat_id'],decoder)
            if old and now and old['path']==now['path'] and not old['compressed']:
                path=snapshot._safe_path(root/now['path'])
                digest=hashlib.sha256();remaining=old['bytes']
                with path.open('rb') as stream:
                    while remaining:
                        data=stream.read(min(remaining,1024*1024))
                        if not data:break
                        digest.update(data);remaining-=len(data)
                summaries.require(not remaining and digest.hexdigest()==old['sha256'],
                                  'Saved journal history changed; summary must be refreshed')
            unproven=not old or not now or not old['projection_verified'] or not now['projection_verified']
            changed=old!=now
            if (unproven or changed) and total<=row['item_count']:
                tails.append({'chat_id':row['canonical_chat_id'],
                              'reason':'journal_changed_or_projection_not_proven'})
    return tails
