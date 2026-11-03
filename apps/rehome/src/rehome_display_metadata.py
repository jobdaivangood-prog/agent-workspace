"""Versioned source display metadata for the non-synthetic restore route.

Only typed timestamps/order/appearance fields are carried. No opaque native
payload, config or authorization is admitted. Missing/zero times stay as such.
"""
from datetime import datetime,timezone
import json
import os
from pathlib import Path

FORMAT='rehome-native-display/1'
THREAD_FIELDS=('created_at','updated_at','created_at_ms','updated_at_ms','recency_at','recency_at_ms',
               'archived_at','section_position','section_entered_at_ms')
PROJECT_FIELDS=('position','created_at_ms','updated_at_ms')
TURN_FIELDS=('started_at','completed_at','duration_ms')
ITEM_FIELDS=('created_at_ms','started_at_ms','completed_at_ms')


def require(value,message='Invalid native display metadata'):
    if not value:raise ValueError(message)


def capture(db,glob,root,bundle,chains):
    import readonly_source as source
    result={'format':FORMAT,'threads':{},'projects':{},'sections':{},'project_order':[]}
    for project in bundle['projects']:
        row=db.execute('SELECT '+','.join(PROJECT_FIELDS)+' FROM projects WHERE id=?',(project['id'],)).fetchone()
        require(row is not None);result['projects'][project['id']]=dict(row)
    for section in bundle['sections']:
        result['sections'][section['id']]={'appearance':db.execute('SELECT appearance FROM thread_sections WHERE id=?',(section['id'],)).fetchone()[0]}
    projects=source._project_tables(db);migration=source._host_mapping(glob,root)
    for legacy in glob.get('project-order',[]):
        try:pid,_=source._resolve_legacy(legacy,glob.get('local-projects',{}),projects,migration)
        except source.SourceError:continue
        if pid in result['projects'] and pid not in result['project_order']:result['project_order'].append(pid)
    result['project_order'].extend(pid for pid in sorted(result['projects'],key=lambda p:(result['projects'][p]['position'],p))
                                   if pid not in result['project_order'])
    for thread in bundle['threads']:
        tid=thread['id']
        row=db.execute('SELECT '+','.join(THREAD_FIELDS)+' FROM threads WHERE id=?',(tid,)).fetchone()
        import rehome_journal
        path=db.execute('SELECT rollout_path FROM threads WHERE id=?',(tid,)).fetchone()[0]
        physical=rehome_journal.resolve_journal(root,path)[1]
        data={'values':dict(row),'turns':{},'journal_mtime_ns':physical.stat().st_mtime_ns}
        origins=[e['source_thread_id'] for e in chains[tid]]
        for turn in thread['turns']:
            rows=[]
            for origin in origins:
                row=db.execute('SELECT '+','.join(TURN_FIELDS)+' FROM history.thread_turns WHERE thread_id=? AND turn_id=?',
                               (origin,turn['id'])).fetchone()
                if row is not None:rows.append(dict(row))
            require(rows,'Source turn display timing is unavailable')
            values={**rows[-1],'started_at':rows[0]['started_at']}
            items={}
            for message in turn['messages']:
                matches=[]
                for origin in origins:
                    row=db.execute('SELECT '+','.join(ITEM_FIELDS)+' FROM history.thread_items WHERE thread_id=? AND turn_id=? AND item_id=?',
                        (origin,turn['id'],message['id'])).fetchone()
                    if row is not None:matches.append(dict(row))
                require(matches,'Source message display timing is unavailable')
                items[message['id']]=matches[0]
            data['turns'][turn['id']]={'values':values,'items':items}
        result['threads'][tid]=data
    validate(bundle,result)
    return result


def _numbers(data,fields,nullable=()):
    require(isinstance(data,dict) and set(data)==set(fields))
    for key,value in data.items():
        require((value is None and key in nullable) or (type(value) is int and -(2**63)<=value<2**63))


def validate(bundle,metadata):
    require(isinstance(metadata,dict) and set(metadata)=={'format','threads','projects','sections','project_order'} and metadata['format']==FORMAT)
    for collection in ('threads','projects','sections'):
        require(isinstance(metadata[collection],dict) and set(metadata[collection])=={p['id'] for p in bundle[collection]})
    require(isinstance(metadata['project_order'],list) and len(metadata['project_order'])==len(metadata['projects']) and
            set(metadata['project_order'])==set(metadata['projects']))
    for values in metadata['projects'].values():_numbers(values,PROJECT_FIELDS)
    for values in metadata['sections'].values():
        require(isinstance(values,dict) and set(values)=={'appearance'} and
                (values['appearance'] is None or isinstance(values['appearance'],str) and len(values['appearance'])<=8192))
    for thread in bundle['threads']:
        data=metadata['threads'][thread['id']]
        require(isinstance(data,dict) and set(data)=={'values','turns','journal_mtime_ns'} and
                type(data['journal_mtime_ns']) is int and -(2**63)<=data['journal_mtime_ns']<2**63)
        _numbers(data['values'],THREAD_FIELDS,{'created_at_ms','updated_at_ms','archived_at','section_position','section_entered_at_ms'})
        timestamp(data['values']['created_at'])
        timestamp(data['values']['updated_at_ms'],milliseconds=True) if data['values']['updated_at_ms'] is not None else timestamp(data['values']['updated_at'])
        require(isinstance(data['turns'],dict) and set(data['turns'])=={t['id'] for t in thread['turns']})
        for turn in thread['turns']:
            timing=data['turns'][turn['id']]
            require(isinstance(timing,dict) and set(timing)=={'values','items'})
            _numbers(timing['values'],TURN_FIELDS,TURN_FIELDS)
            require(isinstance(timing['items'],dict) and set(timing['items'])=={m['id'] for m in turn['messages']})
            for item in timing['items'].values():
                _numbers(item,ITEM_FIELDS,{'started_at_ms','completed_at_ms'})
                timestamp(item['created_at_ms'],milliseconds=True)


def timestamp(value,*,milliseconds=False):
    require(type(value) is int)
    seconds,remainder=divmod(value,1000) if milliseconds else (value,0)
    try:
        stamp=datetime.fromtimestamp(seconds,tz=timezone.utc).replace(microsecond=remainder*1000)
    except (ValueError,OverflowError,OSError):raise ValueError('Unsupported source display timestamp') from None
    return stamp.isoformat(timespec='milliseconds' if stamp.microsecond else 'seconds').replace('+00:00','Z')


def journal_relative(thread,metadata=None):
    stamp='1970-01-01T00-00-00' if metadata is None else timestamp(metadata['threads'][thread['id']]['values']['created_at']).rstrip('Z').replace(':','-')
    return Path('archived_sessions' if thread['archived'] else 'sessions')/('rollout-'+stamp+'-'+thread['id']+'.jsonl')


def apply_to_profile(root,logical_root,bundle,metadata):
    import native_adapter as native
    validate(bundle,metadata)
    with native._connect(root/'state_5.sqlite') as db, native._connect(root/'thread_history_1.sqlite') as history:
        for pid,values in metadata['projects'].items():
            db.execute('UPDATE projects SET '+','.join(f'{k}=?' for k in PROJECT_FIELDS)+' WHERE id=?',(*[values[k] for k in PROJECT_FIELDS],pid))
        for sid,values in metadata['sections'].items():db.execute('UPDATE thread_sections SET appearance=? WHERE id=?',(values['appearance'],sid))
        for thread in bundle['threads']:
            tid=thread['id'];data=metadata['threads'][tid];values=data['values']
            db.execute('UPDATE threads SET '+','.join(f'{k}=?' for k in THREAD_FIELDS)+' WHERE id=?',(*[values[k] for k in THREAD_FIELDS],tid))
            # Native timestamp triggers must not invent milliseconds for an
            # explicitly null source field; set those columns independently.
            db.execute('UPDATE threads SET created_at_ms=?,updated_at_ms=? WHERE id=?',(values['created_at_ms'],values['updated_at_ms'],tid))
            old_path=Path(db.execute('SELECT rollout_path FROM threads WHERE id=?',(tid,)).fetchone()[0]).relative_to(logical_root)
            new_path=journal_relative(thread,metadata)
            journal=root/old_path
            records=[json.loads(line) for line in journal.read_bytes().splitlines()]
            created=timestamp(values['created_at']);records[0]['timestamp']=created;records[0]['payload']['timestamp']=created
            flat=[(turn,message) for turn in thread['turns'] for message in turn['messages']]
            stamps={message['id']:timestamp(data['turns'][turn['id']]['items'][message['id']]['created_at_ms'],milliseconds=True)
                    for turn,message in flat}
            for record in records[1:]:
                payload=record['payload']
                item=payload.get('item',{}) if record['type']=='event_msg' else payload
                if item.get('id') in stamps:record['timestamp']=stamps[item['id']]
            journal.write_text(''.join(native._json(r)+'\n' for r in records),encoding='utf-8')
            if old_path!=new_path:
                journal.rename(root/new_path);journal=root/new_path
            os.utime(journal,ns=(data['journal_mtime_ns'],data['journal_mtime_ns']))
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(logical_root/new_path),tid))
            history.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?',(journal.stat().st_size,tid))
            for turn_id,timing in data['turns'].items():
                values=timing['values']
                history.execute('UPDATE thread_turns SET '+','.join(f'{k}=?' for k in TURN_FIELDS)+' WHERE thread_id=? AND turn_id=?',
                                (*[values[k] for k in TURN_FIELDS],tid,turn_id))
                for item_id,values in timing['items'].items():
                    history.execute('UPDATE thread_items SET '+','.join(f'{k}=?' for k in ITEM_FIELDS)+' WHERE thread_id=? AND turn_id=? AND item_id=?',
                                    (*[values[k] for k in ITEM_FIELDS],tid,turn_id,item_id))
    state=json.loads((root/'.codex-global-state.json').read_text(encoding='utf-8'))
    for pid,values in metadata['projects'].items():
        state['local-projects'][pid].update(createdAt=values['created_at_ms'],updatedAt=values['updated_at_ms'])
    state['project-order']=metadata['project_order']
    (root/'.codex-global-state.json').write_text(native._json(state),encoding='utf-8')
    index=[]
    for thread in bundle['threads']:
        values=metadata['threads'][thread['id']]['values']
        updated=timestamp(values['updated_at_ms'],milliseconds=True) if values['updated_at_ms'] is not None else timestamp(values['updated_at'])
        index.append({'id':thread['id'],'thread_name':thread['name'] or thread['title'],'updated_at':updated})
    (root/'session_index.jsonl').write_text(''.join(native._json(row)+'\n' for row in index),encoding='utf-8')


def verify_sql(state,history,bundle,metadata):
    validate(bundle,metadata)
    for table,collection,fields in (('projects','projects',PROJECT_FIELDS),('thread_sections','sections',('appearance',))):
        for ident,expected in metadata[collection].items():
            require(dict(state.execute('SELECT '+','.join(fields)+' FROM '+table+' WHERE id=?',(ident,)).fetchone())==expected,
                    'Installed display metadata differs')
    for tid,data in metadata['threads'].items():
        require(dict(state.execute('SELECT '+','.join(THREAD_FIELDS)+' FROM threads WHERE id=?',(tid,)).fetchone())==data['values'],
                'Installed thread dates/order differ')
