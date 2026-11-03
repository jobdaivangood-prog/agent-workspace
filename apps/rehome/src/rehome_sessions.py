"""One Finish/Start coordinator. Handoffs are authored by the chat agent first.

Accepted ReHome state is the transactionally installed identity receipt, not
Workspace selected.json. Existing Workspace records are optional provenance.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import uuid

import environment_adapter
import rehome_backup_module as backup
import rehome_backup_workflow as workflow
import rehome_apply_transaction as transaction
import rehome_publications as publications
import rehome_session_source as source_reader
import rehome_summary_history as summaries
import rehome_native_apply as native_apply
from rehome_session_report import Timings, make_report, render_html
import snapshot
import trial_guard


def configure(settings_path, *, starting=False):
    settings_path = snapshot._safe_path(Path(settings_path))
    settings = workflow.load_settings(settings_path)
    root = Path(settings['backup_root'])
    if settings.get('backup_root_selected') is False or not root.is_dir():
        import tkinter as tk
        from tkinter import filedialog
        window=tk.Tk(); window.withdraw()
        try:
            folder=filedialog.askdirectory(title='ReHome: выберите папку копий',mustexist=True)
        finally:
            window.destroy()
        backup._require(folder,'Папка копий не выбрана')
        settings=workflow.set_backup_folder(settings,folder,settings_path)
        root=Path(folder)
    if not settings.get('session_workspace_id'):
        found=set()
        for child in root.glob('ReHome-*/Publication.json'):
            try:
                data=publications._validate(publications._read(child))
                publications.verify(child.parent,workspace_id=data['workspace_id'])
                found.add(data['workspace_id'])
            except (ValueError,OSError,snapshot.SnapshotError):
                continue
        backup._require(len(found)<=1,'Папка содержит разные пространства ReHome')
        backup._require(found or not starting,'В папке нет полностью полученной публикации ReHome')
        settings['session_workspace_id']=next(iter(found)) if found else uuid.uuid4().hex
    settings['session_policy']=publications.POLICY
    settings.setdefault('session_device_id',uuid.uuid4().hex)
    transaction._atomic(settings_path,settings)
    return settings


def _cache(settings,package,digest):
    destination=Path(settings['local_root'])/'SessionPackages'/digest
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():
        backup.verify_backup(destination,expected_digest=digest)
    else:
        snapshot.copy_snapshot(package,destination,expected_digest=digest)
        backup.verify_backup(destination,expected_digest=digest)
    return destination


def _previous(settings):
    identity_file=Path(settings['source_profile'])/summaries.IDENTITY_FILE
    if not identity_file.exists():
        return None
    identity=backup._read(snapshot._safe_path(identity_file))
    digest=identity.get('package_manifest_sha256')
    backup._require(isinstance(digest,str) and snapshot._HEX.fullmatch(digest),'Invalid accepted package pin')
    package=Path(settings['local_root'])/'SessionPackages'/digest
    backup.verify_backup(package,expected_digest=digest)
    candidate=backup._read(package/snapshot.CHAT_BUNDLE_PATH)
    backup._require(summaries.identity_receipt(candidate,digest)==identity,'Accepted identity differs from cached publication')
    return candidate,digest


def _result_locations(settings, previous):
    if previous is None:return {}
    mapping_receipt=Path(settings['local_root'])/'SessionMappings'/(previous[1]+'.json')
    receipt=backup._read(mapping_receipt if mapping_receipt.exists() else workflow._receipt_path(settings))
    _,plan,journal=transaction._load(receipt['job'],receipt['transaction_digest'])
    backup._require(journal['phase']=='active' and plan['backup_manifest_sha256']==previous[1] and
                    Path(plan['target_profile'])==Path(settings['source_profile']),
                    'Result mapping does not belong to the installed publication')
    candidate=Path(plan['candidate'])
    backup._require(snapshot._hash_file(candidate/native_apply.READY)[0]==plan['apply_digest'],
                    'Result mapping pin changed')
    ready=backup._read(candidate/native_apply.READY)
    pin=next(row for row in ready['files'] if row['path']==native_apply.PLAN)
    backup._require(snapshot._hash_file(candidate/native_apply.PLAN)==(pin['sha256'],pin['size']),
                    'Result mapping changed')
    mapping=backup._read(candidate/native_apply.PLAN)
    backup._require(mapping['backup_manifest_sha256']==previous[1],'Result mapping package differs')
    roots=mapping['project_roots'];bindings={}
    for row in previous[0]['source']['results']:
        parts=row['relative_path'].split('/')
        pid=row['project_id'] or '__projectless__'
        path=Path(roots[pid]).joinpath(*parts[2:])
        backup._require(any(e['kind']=='project' and e['target']==roots[pid]
                            for e in plan['entries']),'Uninstalled result project')
        snapshot._safe_path(path,allow_missing=True)
        bindings[row['result_id']]={'source_path':str(path),'original_source_path':row['source_path'],
                                    'version_id':row['version_id']}
    return bindings


def inspect(settings):
    workflow._recovery_first(settings)
    workflow._runtime(settings)
    previous=_previous(settings)
    inspection=source_reader.inspect(settings['source_profile'],workspace_id=settings['session_workspace_id'],
                                      previous_candidate=previous,decoder=workflow._decoder(settings))
    context=previous
    if context is None:
        saved=Path(settings['local_root'])/'session-finish-current.json'
        if saved.exists():
            checkpoint=backup._read(saved)
            if checkpoint.get('workspace_id')==settings['session_workspace_id'] and checkpoint.get('source_profile')==settings['source_profile']:
                pin=checkpoint.get('package_manifest_sha256')
                backup._require(isinstance(pin,str) and snapshot._HEX.fullmatch(pin),'Invalid local Finish checkpoint')
                package=Path(settings['local_root'])/'SessionPackages'/pin
                backup.verify_backup(package,expected_digest=pin)
                context=backup._read(package/snapshot.CHAT_BUNDLE_PATH),pin
    return {'inspection':inspection,'accepted_summary_source':context[0]['source'] if context else None,
            'local_result_bindings':_result_locations(settings,previous),
            'instruction':'Prepare structured summaries from actual visible history; preserve accepted decisions and add new messages. Never copy raw transcripts or sources.'}


def prepare(settings,settings_path,input_data):
    """Seal the agent-authored source and its separate local observation boundary."""
    summaries.exact(input_data,{'inspection','source','include_environment'})
    input_data=copy.deepcopy(input_data)
    inspection=input_data['inspection']; source=input_data['source']
    bindings=_result_locations(settings,_previous(settings))
    for row in source['results']:
        binding=bindings.get(row['result_id'])
        if binding and row['source_path']==binding['original_source_path'] and row['version_id']==binding['version_id']:
            row['source_path']=binding['source_path']
    summaries.build(source,allow_unavailable=True)
    backup._require(type(input_data['include_environment']) is bool,'Explicit environment selection required')
    backup._require(source['workspace_id']==settings['session_workspace_id']==inspection['workspace_id'] and
                    source['catalog']==inspection['catalog'],'Source summary catalog differs from inspected inventory')
    # Validate exactly the observed source prefixes. Appended messages are
    # handled explicitly by Finish after the desktop writer has stopped.
    source_reader.verify_boundaries(settings['source_profile'],inspection,decoder=workflow._decoder(settings))
    by_id={r['canonical_chat_id']:r for r in inspection['boundaries']}
    for handoff in source['handoffs']:
        row=by_id[handoff['canonical_chat_id']]
        backup._require(handoff['source_cursor']=={'last_turn_id':row['last_turn_id'] or 'no-visible-turns',
                                                  'visible_sha256':row['visible_sha256']},
                        'Summary was not bound to the supplied observation')
        backup._require(row['item_count']>0 or handoff['coverage']=='partial','Unavailable history cannot be complete')
    local=Path(settings['local_root']); requests=local/'SessionRequests';requests.mkdir(parents=True,exist_ok=True)
    request=requests/(uuid.uuid4().hex+'.json')
    value={'format':'rehome-session-request/1','settings_path':str(Path(settings_path).absolute()),
           'settings_sha256':snapshot._hash_file(Path(settings_path))[0],**copy.deepcopy(input_data)}
    snapshot._write_json(request,value)
    return {'status':'prepared','request':str(request),'request_sha256':snapshot._hash_file(request)[0],
            'next':'Finish consumes this pinned request; it does not generate summaries after Codex exits.'}


def request_close():
    assets=Path(__file__).resolve().parent.parent/'Assets/Close-Codex.ps1'
    if not assets.exists():
        assets=Path(__file__).resolve().parent.parent/'resources/Close-Codex.ps1'
    snapshot._safe_path(assets)
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                           '-File',str(assets)],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,
                          timeout=45,check=False)
    backup._require(result.returncode==0,'Не удалось запросить штатное закрытие Codex')


def _row(category,ident,name,project_id=None,chat_id=None):
    return {'category':category,'id':ident,'name':name,'project_id':project_id,'chat_id':chat_id}


def _outcome(item,status='included',reason=None):
    return {'category':item['category'],'id':item['id'],'status':status,'reason':reason}


def _semantic(source):
    value=copy.deepcopy(source)
    value.pop('created_at_ms')
    for handoff in value['handoffs']:
        handoff.pop('source_cursor')
    for result in value['results']:
        result.pop('source_path')
    return snapshot._digest(value)


def _content_signature(source, environment, inventory, outcomes):
    env=None
    if environment is not None:
        env=backup._read(environment/environment_adapter.MANIFEST)
        namespace=env.pop('token_namespace');env.pop('created_at')
        # Resource tokens are randomized for collision isolation, not content.
        env=json.loads(json.dumps(env,ensure_ascii=False).replace(namespace,'0'*32))
        for row in env['files']:
            data=(environment/row['path']).read_bytes().replace(namespace.encode(),b'0'*32)
            row['sha256']=__import__('hashlib').sha256(data).hexdigest();row['size']=len(data)
    return snapshot._digest({'source':_semantic(source),'environment':env,
                             'inventory':inventory,'outcomes':outcomes})


def finish(request_path,request_digest,*,close_apps=True):
    request_path=snapshot._safe_path(Path(request_path))
    backup._require(snapshot._hash_file(request_path)[0]==request_digest,'Prepared Finish request changed')
    request=backup._read(request_path)
    summaries.exact(request,{'format','settings_path','settings_sha256','inspection','source','include_environment'})
    backup._require(request['format']=='rehome-session-request/1','Unsupported Finish request')
    settings_path=snapshot._safe_path(Path(request['settings_path']))
    backup._require(snapshot._hash_file(settings_path)[0]==request['settings_sha256'],'Local settings changed since preparation')
    settings=workflow.load_settings(settings_path)
    backup._require(settings.get('session_policy')==publications.POLICY and
                    settings.get('session_workspace_id')==request['source']['workspace_id'],'Session policy or workspace differs')
    source=copy.deepcopy(request['source']); summaries.build(source,allow_unavailable=True)
    backup._require(source['catalog']==request['inspection']['catalog'] and
                    source['workspace_id']==request['inspection']['workspace_id'],'Prepared catalog changed')
    timings=Timings(); inventory=[];outcomes=[]
    local=Path(settings['local_root']); job=local/('SessionFinish-'+uuid.uuid4().hex);job.mkdir()
    with workflow.ProfileLock(settings['source_profile']):
        workflow._recovery_first(settings);workflow._runtime(settings)
        # Check space before asking the desktop to exit. Environment limits
        # and final packet size are checked again after materialization.
        minimum=64*1024*1024+sum(r['bytes'] or 0 for r in source['results'])*3
        backup._require(shutil.disk_usage(local).free>minimum and
                        shutil.disk_usage(settings['backup_root']).free>minimum,'Недостаточно места для копии и временных файлов')
        with timings.measure('Ожидание закрытия Codex'):
            if close_apps:request_close()
            guard=trial_guard.wait_for_quiet(job/'cancel',timeout_seconds=120,check_environment=True)
        check=lambda:workflow._check(job/'cancel',guard,settings)
        with timings.measure('Проверка границ сводок'):
            observed=source_reader.inspect(settings['source_profile'],workspace_id=settings['session_workspace_id'],
                                           previous_candidate=_previous(settings),decoder=workflow._decoder(settings))
            # Recency may change with an appended tail, which is reported below.
            def layout(catalog):
                return {'projects':catalog['projects'],'chats':[
                    {k:v for k,v in chat.items() if k!='updated_at_ms'} for chat in catalog['chats']]}
            backup._require(layout(observed['catalog'])==layout(source['catalog']),
                            'Catalog changed after summary preparation; inspect again')
            tails=source_reader.verify_boundaries(settings['source_profile'],request['inspection'],decoder=workflow._decoder(settings))
            tail_ids={r['chat_id'] for r in tails}
            for h in source['handoffs']:
                if h['canonical_chat_id'] in tail_ids:
                    h['coverage']='partial'
            for p in source['catalog']['projects']:
                item=_row('projects',p['id'],p['name'],p['id']);inventory.append(item);outcomes.append(_outcome(item))
            handoffs={h['canonical_chat_id']:h for h in source['handoffs']}
            for chat in source['catalog']['chats']:
                cid=chat['canonical_chat_id'];item=_row('chats',cid,chat['title'],chat['project_id'],cid)
                inventory.append(item)
                outcomes.append(_outcome(item,'included' if handoffs[cid]['coverage']=='complete' else 'error',
                                         None if handoffs[cid]['coverage']=='complete' else 'summary_is_partial'))
        with timings.measure('Подготовка последних файлов'):
            selected=[];engine=environment_adapter.load_existing_engine(backup.ENGINE_PATH)
            for row in source['results']:
                item=_row('results',row['result_id'],row['name'],row['project_id'],row['chat_ids'][0]);inventory.append(item)
                try:
                    bound=backup._result_bindings({row['relative_path']:{'path':row['source_path'],
                        'classification':backup.managed.CLASSIFICATION}},settings['source_profile'])[row['relative_path']]
                    path=Path(bound['path']);digest,size=bound['sha256'],bound['bytes']
                    backup._require(size<=engine.MAX_FILE,'Result exceeds file size limit')
                    data=environment_adapter._read_stable(path,engine.MAX_FILE)
                    backup._require(not engine.sensitive(path,data),'Sensitive result omitted')
                    selected.append({**row,'sha256':digest,'bytes':size})
                    outcomes.append(_outcome(item))
                except (OSError,ValueError,snapshot.SnapshotError):
                    outcomes.append(_outcome(item,'skipped','result_missing_unreadable_or_excluded'))
            source['results']=selected
        environment_stage=None
        if request['include_environment']:
            with timings.measure('Сбор переносимой среды'):
                environment_stage=job/'Environment'
                environment_adapter.export_backup_environment(settings['source_home'],settings['source_profile'],
                    environment_stage,engine_path=backup.ENGINE_PATH)
                environment=backup._read(environment_stage/environment_adapter.MANIFEST)
                for category in ('skills','agents','plugins'):
                    for index,entry in enumerate(environment[category]):
                        name=entry.get('name') or entry.get('id') or entry.get('path') or category
                        item=_row(category,category+'-'+str(index),str(name));inventory.append(item)
                        if category=='skills':
                            status=('skipped' if not entry.get('file_count') else
                                    'included' if entry.get('status')=='copied' else 'pending')
                            reason=None if status=='included' else 'skill_resources_incomplete_or_local_setup_required'
                        elif category=='plugins':
                            status='pending' if entry.get('manifest_path') else 'skipped'
                            reason='plugin_reinstall_required'
                        else:status,reason='included','resources_only_local_setup_required'
                        outcomes.append(_outcome(item,status,reason))
                for name in environment['config']['selected']:
                    item=_row('settings',name,name);inventory.append(item)
                    outcomes.append(_outcome(item,'included','portable_materials_not_active_configuration'))
                for index,entry in enumerate(environment['excluded']):
                    item=_row('settings','excluded-'+str(index),str(entry.get('relative_path') or entry.get('path') or entry.get('component') or 'Ресурс среды'))
                    inventory.append(item);outcomes.append(_outcome(item,'skipped',str(entry.get('reason') or 'environment_resource_excluded')))
        check()
        signature=_content_signature(source,environment_stage,inventory,outcomes)
        checkpoint_path=local/'session-finish-current.json'
        checkpoint=backup._read(checkpoint_path) if checkpoint_path.exists() else {}
        latest=publications.scan(settings['backup_root'],workspace_id=settings['session_workspace_id'])['latest']
        repeat=(latest and checkpoint.get('content_signature')==signature and
                checkpoint.get('package_manifest_sha256')==latest['publication']['package_manifest_sha256'])
        if repeat:
            published={**latest,'status':'unchanged'}
            package=Path(latest['package']);result={'manifest_digest':latest['publication']['package_manifest_sha256']}
        else:
            with timings.measure('Сборка и проверка пакета'):
                package=job/'Package'
                result=backup.create_summary_backup(source,package,environment_stage=environment_stage,
                                                    source_profile=settings['source_profile'],device_id=settings['session_device_id'])
            check()
            with timings.measure('Публикация'):
                published=publications.publish(package,settings['backup_root'],expected_package_digest=result['manifest_digest'],
                    workspace_id=settings['session_workspace_id'],publisher_device_id=settings['session_device_id'],
                    report=make_report(inventory,outcomes,timings.entries))
        _cache(settings,package,result['manifest_digest'])
        transaction._atomic(local/'session-finish-current.json',{
            'workspace_id':settings['session_workspace_id'],'source_profile':settings['source_profile'],
            'package_manifest_sha256':result['manifest_digest'],'inspection':request['inspection'],
            'content_signature':signature,'publication':published['publication']})
        report=make_report(inventory,outcomes,timings.entries)
        report.update(publication=published,tail_omissions=tails,nas_delivery_verified=False)
        snapshot._write_json(job/'Report.json',report)
        (job/'Report.html').write_text(render_html(report),encoding='utf-8')
        return {'status':report['status'],'publication':published,'report':str(job/'Report.html'),
                'message':'Копия сводок создана. Доставка Synology выполняется отдельно.'}


@workflow._profile_operation
def start(settings,*,close_apps=True,reopen=True):
    backup._require(settings.get('session_policy')==publications.POLICY,'Session policy is not configured locally')
    workflow._recovery_first(settings);workflow._runtime(settings)
    timings=Timings()
    with timings.measure('Проверка полученных публикаций'):
        found=publications.scan(settings['backup_root'],workspace_id=settings['session_workspace_id'])
        latest=found['latest'];backup._require(latest is not None,'Нет полностью полученной публикации')
        digest=latest['publication']['package_manifest_sha256']
        previous=_previous(settings)
        checkpoint_path=Path(settings['local_root'])/'session-finish-current.json'
        checkpoint=backup._read(checkpoint_path) if checkpoint_path.exists() else {}
        if checkpoint.get('workspace_id')==settings['session_workspace_id'] and checkpoint.get('source_profile')==settings['source_profile']:
            last_published=publications._validate(checkpoint['publication'])
            pin=checkpoint['package_manifest_sha256']
            backup._require(last_published['package_manifest_sha256']==pin,'Finish publication context differs')
            backup.verify_backup(Path(settings['local_root'])/'SessionPackages'/pin,expected_digest=pin)
            if publications.order(latest['publication'])<publications.order(last_published):
                return {'status':'unchanged','unavailable':found['unavailable'],
                        'message':'На этом ПК сохранена более новая публикация. Дождитесь синхронизации папки копий.'}
        accepted_dir=Path(settings['local_root'])/'SessionPublications'
        if previous:
            accepted_path=accepted_dir/(previous[1]+'.json')
            backup._require(accepted_path.exists(),'Installed publication order is unavailable')
            accepted=publications._validate(backup._read(accepted_path))
            backup._require(accepted['package_manifest_sha256']==previous[1] and
                            accepted['workspace_id']==settings['session_workspace_id'],'Installed publication context differs')
            if publications.order(latest['publication'])<publications.order(accepted):
                return {'status':'unchanged','unavailable':found['unavailable'],
                        'message':'Установленная публикация новее полученных копий. Понижение версии не выполняется.'}
        package=_cache(settings,latest['package'],digest)
        accepted_dir.mkdir(parents=True,exist_ok=True)
        transaction._atomic(accepted_dir/(digest+'.json'),latest['publication'])
        if previous and previous[1]==digest:
            return {'status':'unchanged','publication':latest,'unavailable':found['unavailable'],
                    'message':'Эта публикация уже установлена. Местные изменения сохранены.'}
        # On the publishing PC an unchanged original profile need not be
        # replaced by its own summary projection just to record acceptance.
        if previous is None and checkpoint_path.exists():
            checkpoint=backup._read(checkpoint_path)
            if checkpoint.get('source_profile')==settings['source_profile'] and checkpoint.get('package_manifest_sha256')==digest:
                source_reader.verify_boundaries(settings['source_profile'],checkpoint['inspection'])
                return {'status':'unchanged','publication':latest,'unavailable':found['unavailable'],
                        'message':'На этом ПК сохранён источник последней публикации. История остаётся на месте.'}
    with timings.measure('Закрытие Codex и восстановление'):
        if close_apps:request_close()
        result=workflow.restore_backup(settings,str(package),Path(settings['local_root'])/('cancel-'+uuid.uuid4().hex),queue.Queue(),session_publication=True)
    backup._require(_previous(settings)[1]==digest,'Accepted session identity was not installed')
    receipt={'status':'restored','publication':latest,'rollback':result,'unavailable':found['unavailable'],
             'timings':timings.entries,'workspace_selection_changed':False}
    snapshot._write_json(Path(settings['local_root'])/('session-start-'+uuid.uuid4().hex+'.json'),receipt)
    if reopen:
        # The CLI runtime is updated under LocalAppData; it is not adjacent to
        # the MSIX desktop executable. Launch the installed application's ID.
        explorer=Path(os.environ['SystemRoot'])/'explorer.exe'
        snapshot._safe_path(explorer)
        subprocess.Popen([str(explorer),'shell:AppsFolder\\OpenAI.Codex_2p2nqsd0c76g0!App'])
    return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description='ReHome Finish/Start session coordinator')
    parser.add_argument('action',choices=['inventory','prepare','finish','start','install'])
    parser.add_argument('--settings',required=True)
    parser.add_argument('--launcher')
    parser.add_argument('--task-name')
    parser.add_argument('--input');parser.add_argument('--pin');parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    backup._require(args.task_name is None or re.fullmatch(r'ReHome-Session-[a-f0-9]{32}',args.task_name),
                    'Unexpected worker task identity')
    output=snapshot._safe_path(Path(args.output),allow_missing=True)
    backup._require(output.is_absolute() and not output.exists(),'Use a new output report path')
    output.parent.mkdir(parents=True,exist_ok=True)
    try:
        if args.action=='install':
            from rehome_install_commands import install
            result=install(Path.home()/'.codex',args.launcher,Path(args.settings).parent)
        elif args.action=='finish':
            backup._require(args.input and args.pin,'Finish requires a pinned prepared request')
            result=finish(args.input,args.pin)
        else:
            settings=configure(args.settings,starting=args.action=='start')
            if args.action=='inventory':result=inspect(settings)
            elif args.action=='prepare':result=prepare(settings,args.settings,backup._read(snapshot._safe_path(Path(args.input))))
            else:result=start(settings)
        snapshot._write_json(output,result)
    except Exception as error:
        snapshot._write_json(output,{'status':'failed','error_type':type(error).__name__,
            'message':'Операция ReHome не завершена. Проверьте подготовку сводок, доступность источников и состояние восстановления.',
            'profile_success_not_asserted':True})
        raise
    finally:
        if args.task_name:
            subprocess.run(['schtasks.exe','/Delete','/TN',args.task_name,'/F'],capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW,timeout=20,check=False)


if __name__=='__main__':
    main()
