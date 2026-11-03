"""Restartable local file/directory transaction; no deletion or credential copy.

Original components are renamed to an owned job directory. Every move has a
flushed write-ahead intent and an identity check. Recovery never needs runtime
or configuration pins; it still requires all known writers to be stopped.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
import shutil

import rehome_backup_module as backup
import rehome_native_apply as apply
import rehome_review_sources as managed
import snapshot
import rehome_windows_paths as windows_paths


def _identity(path):
    path=managed._local(path)
    value=path.stat()
    backup._require(value.st_ino>0 and (path.is_dir() or stat.S_ISREG(value.st_mode)),'Ordinary transaction path required')
    return [value.st_dev,value.st_ino,'directory' if path.is_dir() else 'file']


def _tree(path):
    path=managed._local(path)
    if path.is_file():return {'kind':'file','binding':list(snapshot._hash_file(path))}
    entries=[]
    for child in sorted(path.rglob('*')):
        managed._local(child)
        backup._require(len(entries)<100000,'Installed tree entry limit exceeded')
        if child.is_dir():entries.append({'path':child.relative_to(path).as_posix(),'kind':'directory'})
        else:entries.append({'path':child.relative_to(path).as_posix(),'kind':'file','binding':list(snapshot._hash_file(child))})
    return {'kind':'directory','entries':entries}


def _atomic(path,data):
    managed._local(path,missing=True)
    temporary=path.parent/('.journal-'+uuid.uuid4().hex)
    with temporary.open('xb') as stream:
        stream.write(snapshot._canonical(data));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def _rename_no_replace(source,target):
    source=managed._local(source);target=managed._local(target,missing=True)
    backup._require(os.name=='nt','Atomic transaction implementation currently requires Windows')
    backup._require(not target.exists(),'Transaction destination exists')
    # Windows rename fails rather than replacing a destination that appears.
    os.rename(source,target)


def _guard(guard):
    backup._require(callable(guard),'Fresh process guard required')
    guard()


def prepare(candidate,job,*,expected_apply_digest,guard):
    _guard(guard)
    result=apply.verify(candidate,expected_apply_digest=expected_apply_digest)
    candidate=managed._local(candidate);job=managed._local(job,missing=True)
    backup._require(not job.exists() and not managed._overlap(job,candidate),'Transaction job must be new and separate')
    plan=backup._read(candidate/apply.PLAN)
    available=next(parent for parent in (job.parent,*job.parents) if parent.exists())
    backup._require(shutil.disk_usage(available).free>16*1024*1024+len(plan['changes'])*2048,
                    'Insufficient local space for transaction journal and recovery')
    target=managed._local(plan['target_profile'])
    entries=[]
    for change in plan['changes']:
        path=managed._local(change['target'],missing=True)
        backup._require(path.drive.casefold()==job.drive.casefold(),'Transaction paths must be on one local volume')
        entries.append({**change,'original_identity':_identity(path) if path.exists() else None,
                        'candidate_identity':_identity(candidate/change['candidate']) if change['candidate'] else None,
                        'installed_binding':_tree(candidate/change['candidate']) if change['candidate'] else None})
    # Check both activation and recovery addresses BEFORE the first move. The
    # Changed path includes the full UUID used when preserving restored data.
    for index, entry in enumerate(entries):
        entry_target = Path(entry['target'])
        if entry_target.exists():
            windows_paths.tree(entry_target, job/'Original'/str(index))
        if entry['candidate'] is not None:
            incoming = candidate/entry['candidate']
            windows_paths.tree(incoming, entry_target)
            windows_paths.tree(incoming, job/'Changed'/(str(index)+'-'+'0'*32))
    job.mkdir(parents=True)
    (job/'Original').mkdir();(job/'Changed').mkdir()
    immutable={'format':'rehome-apply-transaction/1','candidate':str(candidate),'apply_digest':expected_apply_digest,
        'target_profile':str(target),'profile_identity':_identity(target),'entries':entries,
        'generation':plan['generation'],'backup_manifest_sha256':plan['backup_manifest_sha256']}
    snapshot._write_json(job/'transaction-plan.json',immutable)
    digest=snapshot._hash_file(job/'transaction-plan.json')[0]
    _atomic(job/'journal.json',{'format':'rehome-transaction-journal/1','plan_digest':digest,'phase':'prepared',
        'completed_moves':[],'pending_move':None})
    return {'job':str(job),'transaction_digest':digest,'status':'prepared','profile_applied':False}


def _load(job,digest):
    job=managed._local(job)
    backup._require(snapshot._hash_file(job/'transaction-plan.json')[0]==digest,'Transaction plan differs from external pin')
    plan=backup._read(job/'transaction-plan.json');journal=backup._read(job/'journal.json')
    backup._require(plan['format']=='rehome-apply-transaction/1' and journal['plan_digest']==digest and
        _identity(plan['target_profile'])==plan['profile_identity'],'Transaction identity differs')
    backup._require(set(journal)=={'format','plan_digest','phase','completed_moves','pending_move'} and
        journal['format']=='rehome-transaction-journal/1' and isinstance(journal['completed_moves'],list) and
        journal['phase'] in {'prepared','applying','active','rolling_back','rolling_back_active','rolled_back','recovery_required'},
        'Invalid recovery journal')
    for move in [*journal['completed_moves'],*([journal['pending_move']] if journal['pending_move'] else [])]:
        backup._require(isinstance(move,dict) and set(move)=={'source','target','identity'} and isinstance(move['identity'],list),
                        'Invalid recovery move intent')
    # The external pin authenticates the exact target list that was validated
    # by the apply-candidate verifier before any transaction path was created.
    return job,plan,journal


def _reconcile(job,journal):
    move=journal['pending_move']
    if move is None:return
    source,target=Path(move['source']),Path(move['target'])
    if not source.exists() and target.exists() and _identity(target)==move['identity']:
        journal['completed_moves'].append(move)
    else:
        backup._require(source.exists() and _identity(source)==move['identity'] and not target.exists(),
                        'Uncertain interrupted move; preserve all paths for recovery')
    journal['pending_move']=None
    _atomic(job/'journal.json',journal)


def _move(job,journal,source,target,identity,guard):
    _guard(guard)
    source=managed._local(source);target=managed._local(target,missing=True)
    backup._require(_identity(source)==identity and not target.exists(),'Transaction path identity changed')
    target.parent.mkdir(parents=True,exist_ok=True)
    journal['pending_move']={'source':str(source),'target':str(target),'identity':identity}
    _atomic(job/'journal.json',journal)
    _guard(guard)
    backup._require(_identity(source)==identity,'Transaction source changed during process gate')
    _rename_no_replace(source,target)
    backup._require(_identity(target)==identity,'Renamed identity differs')
    journal['completed_moves'].append(journal['pending_move']);journal['pending_move']=None
    _atomic(job/'journal.json',journal)


def activate(job,*,transaction_digest,guard,rollback_guard=None):
    job,plan,journal=_load(job,transaction_digest)
    backup._require(journal['phase']=='prepared','Only a fresh prepared transaction may activate')
    apply.verify(plan['candidate'],expected_apply_digest=plan['apply_digest'])
    _guard(guard)
    journal['phase']='applying';_atomic(job/'journal.json',journal)
    try:
        for index,entry in enumerate(plan['entries']):
            target=Path(entry['target'])
            if entry['original_identity'] is not None:
                _move(job,journal,target,job/'Original'/str(index),entry['original_identity'],guard)
            else:
                backup._require(not target.exists(),'A target appeared after preparation')
            if entry['candidate'] is not None:
                _move(job,journal,Path(plan['candidate'])/entry['candidate'],target,entry['candidate_identity'],guard)
        for entry in plan['entries']:
            path=Path(entry['target'])
            backup._require((_tree(path) if path.exists() else None)==entry['installed_binding'],'Installed bytes or layout differ')
        apply.verify_installed(plan['candidate'])
        _guard(guard)
        journal['phase']='active';_atomic(job/'journal.json',journal)
        return {'status':'restored','profile_applied':True,'environment_activated':False,'job':str(job),
                'transaction_digest':transaction_digest,'rollback_available':True}
    except Exception:
        try: rollback(job,transaction_digest=transaction_digest,guard=rollback_guard or guard)
        except Exception:
            journal=backup._read(job/'journal.json');journal['phase']='recovery_required';_atomic(job/'journal.json',journal)
        raise


def rollback(job,*,transaction_digest,guard):
    job,plan,journal=_load(job,transaction_digest)
    _guard(guard)
    _reconcile(job,journal)
    if journal['phase']=='rolled_back':
        return {'status':'rolled_back','profile_applied':False,'job':str(job)}
    was_active=journal['phase'] in {'active','rolling_back_active'}
    journal['phase']='rolling_back_active' if was_active else 'rolling_back'
    _atomic(job/'journal.json',journal)
    for index in reversed(range(len(plan['entries']))):
        entry=plan['entries'][index];target=Path(entry['target']);original=job/'Original'/str(index)
        if target.exists() and entry['original_identity'] is not None and _identity(target)==entry['original_identity']:
            backup._require(not original.exists(),'Original exists twice')
            continue
        if target.exists():
            identity=_identity(target)
            if not was_active:
                backup._require(identity==entry['candidate_identity'],'Unowned target appeared during incomplete activation')
            changed=job/'Changed'/(str(index)+'-'+uuid.uuid4().hex)
            _move(job,journal,target,changed,identity,guard)
        if entry['original_identity'] is not None:
            backup._require(original.exists() and _identity(original)==entry['original_identity'],'Original recovery component missing')
            _move(job,journal,original,target,entry['original_identity'],guard)
    journal['phase']='rolled_back';_atomic(job/'journal.json',journal)
    return {'status':'rolled_back','profile_applied':False,'job':str(job),'new_state_preserved_in':str(job/'Changed')}
