"""Two-action ReHome service. User starts it outside Codex from Explorer."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid
import functools
import ctypes
import traceback
from ctypes import wintypes

import environment_adapter
import rehome_apply_transaction as transaction
import rehome_backup_module as backup
import rehome_history_lineage as lineage
import rehome_native_apply as apply
import rehome_native_staging as staging
import rehome_review_sources as managed
import rehome_selection as selection
import snapshot
import trial_guard
import rehome_journal
import rehome_closed_snapshot


class WorkflowError(ValueError):
    def __init__(self,code,message):
        super().__init__(message);self.code=code;self.public_message=message


class ProfileLock:
    """Windows-session OS mutex bound to the profile, independent of settings."""
    def __init__(self,profile):
        self.profile=managed._local(profile);self.handle=None
    def __enter__(self):
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR];self.api.CreateMutexW.restype=wintypes.HANDLE
        self.api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];self.api.WaitForSingleObject.restype=wintypes.DWORD
        self.api.ReleaseMutex.argtypes=[wintypes.HANDLE];self.api.CloseHandle.argtypes=[wintypes.HANDLE]
        key=hashlib.sha256(os.path.normcase(str(self.profile)).encode()).hexdigest()
        self.handle=self.api.CreateMutexW(None,False,'Local\\ReHomeProfile-'+key)
        if not self.handle:raise WorkflowError('lock_unavailable','Не удалось получить блокировку профиля.')
        result=self.api.WaitForSingleObject(self.handle,0)
        if result not in (0,0x80):
            self.api.CloseHandle(self.handle);self.handle=None
            raise WorkflowError('already_running','С этим профилем уже работает другое окно ReHome.')
        return self
    def __exit__(self,*args):
        if self.handle:
            self.api.ReleaseMutex(self.handle);self.api.CloseHandle(self.handle);self.handle=None


def _profile_operation(function):
    @functools.wraps(function)
    def wrapped(settings,*args,**kwargs):
        with ProfileLock(settings['source_profile']):
            try:return function(settings,*args,**kwargs)
            except Exception as error:
                journal_failure = None
                if isinstance(error,rehome_journal.JournalError):
                    # Public explanations are fixed strings, never journal text
                    # or arbitrary exception payloads.
                    journal_failure = {
                        'Unsupported journal location': ('journal_location_unsupported','Формат пути одного журнала истории пока не поддерживается.'),
                        'Journal is missing': ('journal_missing','Не найден файл истории одного из чатов.'),
                        'Ambiguous journal representations': ('journal_ambiguous','Найдены две версии одного журнала истории; требуется проверка.'),
                        'An explicitly pinned Zstandard decoder is required': ('journal_decoder_required','Для сжатой истории недоступен проверенный декодер.'),
                    }.get(str(error),('journal_read_failed','Не удалось проверить журнал истории одного из чатов.'))
                path=Path(settings['local_root'])/('operation-stopped-'+uuid.uuid4().hex+'.json')
                code=journal_failure[0] if journal_failure else getattr(error,'code',None)
                if code is None and isinstance(error,lineage.LineageError):code='history_requires_adapter'
                try:
                    snapshot._write_json(path,{'format':'rehome-operation-stop/1','operation':function.__name__,
                        'error_type':type(error).__name__,'error_code':code,
                        'code_frames':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                                       for frame in traceback.extract_tb(error.__traceback__)[-6:]],
                        'profile_applied_not_asserted':True,'message_payloads_saved':False})
                except (OSError,ValueError):pass
                if isinstance(error,WorkflowError):raise
                if journal_failure:
                    raise WorkflowError(journal_failure[0],journal_failure[1]+' Отчёт: '+str(path)) from None
                if code=='history_requires_adapter':
                    raise WorkflowError(code,'Полная история пока не прошла проверку. Отчёт: '+str(path)) from None
                if code=='windows_path_too_long':
                    raise WorkflowError(code,'Служебный путь ReHome слишком длинный для этого Windows. '
                        'Используйте короткую локальную папку для операций. Рабочие данные сохранены; '
                        'при незавершённой операции доступен откат. Отчёт: '+str(path)) from None
                raise WorkflowError('operation_stopped','Операция остановлена. Отчёт: '+str(path)+' При незавершённом восстановлении доступен возврат прежних данных.') from None
    return wrapped


def _recovery_path(settings):
    profile=managed._local(settings['source_profile'])
    name=hashlib.sha256(os.path.normcase(str(profile)).encode()).hexdigest()
    return managed._local(profile.parent/'ReHome-Recovery'/ (name+'.json'),missing=True)


def _receipt_path(settings):
    shared=_recovery_path(settings)
    return shared if shared.exists() else Path(settings['local_root'])/'latest-transaction.json'


def recovery_available(settings):
    return _receipt_path(settings).exists()


def _recovery_first(settings):
    latest=_receipt_path(settings)
    if latest.exists():
        receipt=backup._read(latest)
        _,plan,journal=transaction._load(receipt['job'],receipt['transaction_digest'])
        backup._require(Path(plan['target_profile'])==Path(settings['source_profile']),'Recovery receipt refers to another profile')
        if journal['phase'] not in {'active','rolled_back'}:
            raise WorkflowError('recovery_required','Сначала нажмите «Вернуть прежнее состояние»: предыдущая операция не завершена.')


def load_settings(path):
    settings=backup._read(managed._local(path))
    backup._require(settings.get('format')=='rehome-two-action-settings/1','Invalid ReHome settings')
    for key in ('source_home','source_profile','local_root','backup_root','projects_root','runtime_executable','schema_template'):
        managed._local(settings[key],missing=key in {'local_root','backup_root','projects_root','runtime_executable','schema_template'})
    # Version/template checks belong to backup/restore, never the recovery UI.
    profile=Path(settings['source_profile']);local=Path(settings['local_root']);output=Path(settings['backup_root'])
    backup._require(profile.is_relative_to(Path(settings['source_home'])) and not any(staging._overlap(a,b)
                    for a,b in ((profile,local),(profile,output),(local,output))),'ReHome settings paths overlap')
    local.mkdir(parents=True,exist_ok=True)
    return settings


def _runtime(settings):
    backup._runtime(settings['runtime_version'])
    try:
        actual=snapshot._hash_file(Path(settings['runtime_executable']))[0]
    except (OSError,snapshot.SnapshotError) as error:
        raise WorkflowError('codex_runtime_unavailable',
            'Не найден проверенный движок Codex. Эта beta поддерживает 0.166.0-alpha.5.1. '
            'Данные не заменены; возврат прежнего состояния остаётся доступен.') from error
    if not staging._pin(settings['runtime_sha256']) or actual!=settings['runtime_sha256']:
        raise WorkflowError('codex_runtime_unverified',
            'Версия Codex не прошла проверку. Эта beta поддерживает 0.166.0-alpha.5.1. '
            'Возврат прежнего состояния доступен независимо от версии Codex.')


def set_backup_folder(settings,folder,settings_path=None):
    folder=managed._local(folder)
    backup._require(folder.is_dir(),'Backup destination must be a directory')
    for protected in (settings['source_profile'],settings['local_root']):
        backup._require(not staging._overlap(folder,managed._local(protected,missing=True)),
                        'Backup destination overlaps active data')
    updated={**settings,'backup_root':str(folder)}
    if settings_path is not None:
        transaction._atomic(managed._local(settings_path),updated)
    return updated


def _wait(settings,cancel,events,*,recovery=False):
    events.put(('progress','Закройте Codex вручную. Оставьте ReHome открытым.'))
    return trial_guard.wait_for_quiet(cancel,timeout_seconds=3600,check_environment=not recovery,
        on_update=lambda data: events.put(('processes',data)))


def _check(cancel,guard,settings=None):
    if Path(cancel).exists():raise WorkflowError('cancelled','Операция отменена до следующего изменения.')
    guard.assert_quiet(check_environment=settings is not None)
    if settings is not None:_runtime(settings)


def stable_source(profile,destination,guard,decoder=None):
    """Private local SQLite backup + exact journals; never auth or input files."""
    profile=managed._local(profile);destination=managed._local(destination,missing=True)
    backup._require(not destination.exists(),'Stable source target exists')
    guard()
    destination.mkdir()
    with lineage.source._snapshot(profile,lineage.source.CURRENT_SCHEMA_VERSION) as (source,global_state,_):
        required,_=_user_cohort(profile,decoder)
        required=set(required)
        rows=source.execute('SELECT id,rollout_path FROM threads').fetchall()
        backup._require(len(rows)<=100000,'Stable source chat limit exceeded')
        watched={profile/name for name in (*staging.DATABASES,*apply.SIDECARS,'.codex-global-state.json')}
        journals=[]
        for row in rows:
            try:logical,physical,_=rehome_journal.resolve_journal(profile,row['rollout_path'])
            except rehome_journal.JournalError:
                if row['id'] in required:raise
                continue
            watched.update((logical,Path(str(logical)+'.zst')))
            journals.append((row['id'],logical,physical))
        # A resumed HEAD can reference older physical rollouts which no longer
        # occupy any current state.threads pointer. Copy the verified closure.
        import rehome_rollout_resolver as resolver
        for tid in sorted(required):
            head=lineage.current_storage_id(source,profile,tid,decoder=decoder)
            seen=set();key=head;cutoff=None
            while key is not None:
                backup._require(key not in seen and len(seen)<128,'Cyclic or excessive source storage dependencies')
                seen.add(key)
                binding=resolver.resolve(profile,key,journal_decoder=decoder)
                if cutoff is not None:
                    backup._require(binding['header_ordinal']<cutoff['end_ordinal_exclusive']<=binding['projection_next_ordinal'] and
                                    cutoff['end_byte_offset']<=binding['projection_next_byte_offset'],'Source storage prefix exceeds projection')
                physical=profile/binding['relative_physical_path']
                logical,physical,_=rehome_journal.resolve_journal(profile,physical)
                watched.update((logical,Path(str(logical)+'.zst')))
                if not any(p==physical for _,_,p in journals):journals.append((None,logical,physical))
                cutoff=binding['history_base'];key=None if cutoff is None else cutoff['thread_id']
        def binding(path):
            if not path.exists():return None
            digest,size=snapshot._hash_file(path);info=path.stat()
            return (digest,size,info.st_mtime_ns,info.st_dev,info.st_ino)
        before={path:binding(path) for path in watched}
        for name,store in ((staging.DATABASES[0],'main'),(staging.DATABASES[1],'history')):
            with closing(sqlite3.connect(destination/name)) as target:
                source.backup(target,name=store)
                target.execute('PRAGMA journal_mode=DELETE')
        for _,_,physical in journals:
            target=destination/physical.relative_to(profile)
            if not target.exists():
                snapshot._copy_verified(physical,target)
                info=physical.stat();os.utime(target,ns=(info.st_mtime_ns,info.st_mtime_ns))
        guard()
        after={path:binding(path) for path in watched}
        backup._require(before==after,'Source files changed while freezing the profile')
    # Observer transactions above also reject commits/root replacements across
    # both databases and changes to the original global-state bytes.
    for key in ('app-server-project-id-by-legacy-project-id-by-host','app-server-projects-migration-by-host'):
        if key in global_state and 'local:'+str(profile) in global_state[key]:
            global_state[key]['local:'+str(destination)]=global_state[key].pop('local:'+str(profile))
    snapshot._write_json(destination/'.codex-global-state.json',global_state)
    with closing(sqlite3.connect(destination/'state_5.sqlite')) as db:
        for tid,logical,_ in journals:
            if tid is None:continue
            target=destination/logical.relative_to(profile)
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(target),tid))
        db.commit()
    guard()
    return destination


def _results(settings,profile):
    results={}
    for item in settings.get('finished_results',[]):
        backup._require(set(item)=={'project_id','path','display_name','classification'} and
                        item['classification']==managed.CLASSIFICATION,'Finished result classification required')
        snapshot._validate_relative(item['display_name'])
        selection._file_allowed(item['path'],profile)
        relative='Projects/'+item['project_id']+'/Results/'+item['display_name']
        backup._require(relative not in results,'Finished result path collision')
        results[relative]={'path':item['path'],'classification':managed.CLASSIFICATION}
    return results


def project_choices(settings):
    """Metadata-only choices for explicitly selected generated result files."""
    with lineage.source._snapshot(settings['source_profile'],lineage.source.CURRENT_SCHEMA_VERSION) as (db,_,__):
        projects=lineage.source._project_tables(db)
        return [{'id':pid,'name':p['name'],'roots':p['roots']} for pid,p in projects.items()]


def infer_project(path,choices):
    file=Path(path)
    matches=[]
    for project in choices:
        for root in project['roots']:
            if isinstance(root,str) and root.startswith('\\\\?\\'):root=root[4:]
            root=Path(root)
            if root.is_absolute() and file.is_relative_to(root):matches.append((len(root.parts),project['id']))
    if not matches:return None
    depth=max(v[0] for v in matches);ids={pid for size,pid in matches if size==depth}
    return next(iter(ids)) if len(ids)==1 else None


def _user_cohort(profile,decoder=None):
    """Current native source kinds: visible cli/vscode; internal subagents omit.

    The installed application's active+archive pagination was checked against
    this rule. Unknown kinds stop full coverage instead of being guessed.
    """
    ids=[];internal=0;empty=0
    with closing(lineage.source._open_ro(Path(profile)/'state_5.sqlite')) as db, closing(lineage.source._open_ro(Path(profile)/'thread_history_1.sqlite')) as history:
        materialized={r[0] for r in history.execute('SELECT DISTINCT thread_id FROM thread_turns')}
        for row in db.execute('SELECT id,source,has_user_event,rollout_path,archived,name,title FROM threads ORDER BY id'):
            value=row['source']
            try:
                parsed=json.loads(value)
                if isinstance(parsed,dict):
                    if set(parsed)!={'subagent'}:
                        raise WorkflowError('unknown_chat_source','Найден неизвестный тип чата. Для полной копии требуется адаптация.')
                    kind='subagent'
                else:kind=parsed
            except ValueError:kind=value
            if not isinstance(kind,str):
                raise WorkflowError('unknown_chat_source','Найден неизвестный тип чата. Для полной копии требуется адаптация.')
            if kind=='subagent':internal+=1;continue
            if kind not in {'cli','vscode'}:
                raise WorkflowError('unknown_chat_source','Найден неизвестный тип чата. Для полной копии требуется адаптация.')
            if row['id'] not in materialized and not row['has_user_event'] and not row['archived'] and not row['name'] and not row['title']:
                header,_=rehome_journal.read_header(Path(profile),row['rollout_path'],decoder=decoder)
                record=json.loads(header)
                if record.get('type')!='session_meta' or not isinstance(record.get('payload'),dict):
                    raise WorkflowError('unknown_chat_header','Метаданные истории требуют адаптации для полной копии.')
                if lineage._base(record['payload']) is None:empty+=1;continue
            ids.append(row['id'])
    return ids,{'user_chats':len(ids),'internal_subagent_threads_omitted':internal,'empty_unmaterialized_rows_omitted':empty}


@_profile_operation
def create_backup(settings,cancel,events):
    _recovery_first(settings)
    _runtime(settings)
    guard=_wait(settings,cancel,events)
    check=lambda: _check(cancel,guard,settings)
    check()
    output=managed._local(settings['backup_root'],missing=True)
    output.mkdir(parents=True,exist_ok=True)
    name='ReHome-'+uuid.uuid4().hex
    events.put(('progress','Создаю стабильную копию истории и собираю среду.'))
    with tempfile.TemporaryDirectory(prefix='rehome-source-') as temporary:
        temporary=Path(temporary)
        decoder=_decoder(settings)
        source=stable_source(settings['source_profile'],temporary/'Profile',check,decoder)
        ids,coverage=_user_cohort(source,decoder)
        if not ids:raise WorkflowError('no_chats','В выбранном профиле не найдено чатов для копии.')
        closed=rehome_closed_snapshot.seal(source,original_profile=settings['source_profile'],guard=check)
        environment_adapter.export_backup_environment(settings['source_home'],settings['source_profile'],temporary/'Environment',
                                                      engine_path=backup.ENGINE_PATH)
        check()
        result=backup.create_backup(source,ids,output/name,ready_files=_results(settings,settings['source_profile']),
            runtime_version=settings['runtime_version'],device_id='rehome-local',environment_stage=temporary/'Environment',
            journal_decoder=decoder,include_all_layout=True,preserve_display=True,closed_snapshot=closed)
    check()
    candidate=backup._read(output/name/snapshot.CHAT_BUNDLE_PATH)
    unfinished=sum('closed_snapshot_tail' in row for chain in candidate['lineage'].values() for row in chain)
    receipt={**result,'backup_path':str(output/name),'all_user_visible_chats_included':True,
             'chat_coverage':coverage,
             'finished_results_selection_supplied':bool(settings.get('finished_results')),
             'unfinished_source_turns':unfinished,
             'nas_delivery_verified':False,'message':'Копия создана и проверена. Доставка Synology проверяется отдельно.'+
                (f' Сохранено незавершённых ходов: {unfinished}; при восстановлении они будут отмечены как прерванные.' if unfinished else '')}
    snapshot._write_json(Path(settings['local_root'])/('backup-receipt-'+uuid.uuid4().hex+'.json'),receipt)
    # Receipt sits beside, not inside, the outer-pinned backup folder.
    snapshot._write_json(output/(name+'-receipt.json'),receipt)
    return receipt


@_profile_operation
def restore_backup(settings,backup_path,cancel,events):
    _recovery_first(settings)
    _runtime(settings)
    try:
        staging._template(settings['schema_template'],settings['template_pins'])
    except (ValueError, OSError, sqlite3.DatabaseError) as error:
        # Only fixed text crosses the UI boundary. Recovery remains available
        # independently of these restore-only resources.
        raise WorkflowError('restore_resources_invalid',
            'Шаблон восстановления ReHome отсутствует или повреждён. Распакуйте полный ZIP-комплект '
            'в обычную локальную папку и откройте его снова. Прежние данные можно вернуть кнопкой отката.') from error
    info=backup.verify_backup(backup_path)
    if 'display_metadata' not in backup._read(Path(backup_path)/snapshot.CHAT_BUNDLE_PATH):
        raise WorkflowError('display_metadata_required','Копия создана ранней проверочной версией без дат истории. Создайте новую копию через ReHome.')
    guard=_wait(settings,cancel,events)
    check=lambda: _check(cancel,guard,settings)
    recovery_check=lambda: guard.assert_quiet(check_environment=False)
    check()
    events.put(('progress','Проверяю копию и готовлю восстановление с откатом.'))
    local=Path(settings['local_root'])
    staged=staging.prepare_native(backup_path,local/'Staging',expected_digest=info['manifest_digest'],
        runtime_version=settings['runtime_version'],schema_template=settings['schema_template'],
        template_pins=settings['template_pins'],project_map=_project_map(backup_path))
    snapshot._write_json(local/('stage-receipt-'+uuid.uuid4().hex+'.json'),staged)
    check()
    identity=uuid.uuid4().hex
    candidate=apply.prepare(staged['job'],local/('Apply-'+identity),expected_digest=info['manifest_digest'],
        expected_stage_digest=staged['stage_digest'],target_profile=settings['source_profile'],projects_root=settings['projects_root'],
        target_home=settings['source_home'])
    snapshot._write_json(local/('apply-receipt-'+identity+'.json'),candidate)
    check()
    prepared=transaction.prepare(candidate['candidate'],local/('Transaction-'+identity),
        expected_apply_digest=candidate['apply_digest'],guard=check)
    pointer=_recovery_path(settings);pointer.parent.mkdir(parents=True,exist_ok=True)
    transaction._atomic(pointer,prepared)
    transaction._atomic(local/'latest-transaction.json',prepared)
    events.put(('progress','Восстанавливаю данные. Пока не открывайте Codex.'))
    result=transaction.activate(prepared['job'],transaction_digest=prepared['transaction_digest'],guard=check,rollback_guard=recovery_check)
    return {**result,'message':'История и проекты восстановлены. Прежнее состояние сохранено для отката. Среда подготовлена; настройки и плагины требуют проверки.',
            'environment_activated':False,'nas_delivery_verified':False}


def _project_map(path):
    bundle=backup._read(Path(path)/snapshot.CHAT_BUNDLE_PATH)['transfer']
    mapping={p['id']:'Projects/'+p['id'] for p in bundle['projects']}
    if any(t['project_id'] is None for t in bundle['threads']):mapping['__projectless__']='Projects/Projectless'
    return mapping


def _decoder(settings):
    dependency=settings.get('decoder')
    if dependency is None:return None
    backup._require(set(dependency)=={'path','sha256'},'Invalid decoder settings')
    return rehome_journal.ZstdDecoder(Path(dependency['path']),dependency['sha256'])


@_profile_operation
def rollback(settings,cancel,events):
    receipt=backup._read(_receipt_path(settings))
    _,plan,_=transaction._load(receipt['job'],receipt['transaction_digest'])
    backup._require(Path(plan['target_profile'])==Path(settings['source_profile']),'Recovery receipt refers to another profile')
    guard=_wait(settings,cancel,events,recovery=True)
    result=transaction.rollback(receipt['job'],transaction_digest=receipt['transaction_digest'],
                                guard=lambda: guard.assert_quiet(check_environment=False))
    return {**result,'message':'Прежнее состояние возвращено. Данные, созданные после восстановления, сохранены отдельно.'}
