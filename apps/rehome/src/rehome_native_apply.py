"""New pinned local apply candidate. Never edits the sealed native stage.

Only history/UI components and new project result directories are publishable.
Authorization, active config, installed MCP/AGENTS and plugin caches stay local.
Environment activation remains explicitly reported as prepared/local setup.
"""
import hashlib
import json
import os
from pathlib import Path
import uuid

import native_adapter as native
import rehome_backup_module as backup
import rehome_native_staging as staging
import rehome_review_sources as managed
import snapshot
import environment_adapter as environment
from rehome_environment_contract import resource_bytes

READY='ApplyREADY.json'
PLAN='apply-plan.json'
CORE=('state_5.sqlite','thread_history_1.sqlite','.codex-global-state.json','session_index.jsonl','sessions','archived_sessions','import-report.json')
SIDECARS=tuple(database+suffix for database in staging.DATABASES for suffix in ('-wal','-shm','-journal'))


def _environment_groups(stage,home,profile,inventory=None):
    source=stage/'Environment'
    if not source.is_dir():return [],0,None
    if inventory is None:inventory=environment.verify_backup_environment(source)
    groups=[];deferred=0
    for skill in inventory['skills']:
        component=skill['component'];folder=skill['path']
        if component not in {'user-skills','agents-skills'} or not folder or folder.split('/')[0]=='.system' or skill['status']!='copied':
            deferred+=1;continue
        target=(profile/'skills' if component=='user-skills' else home/'.agents/skills')/folder
        prefix=folder+'/'
        entries=[e for e in inventory['files'] if e['component']==component and e['relative_path'].startswith(prefix)]
        groups.append((target,[(e,e['relative_path'][len(prefix):]) for e in entries]))
    for entry in inventory['files']:
        if entry['component'] not in {'codex-agents','home-agent-definitions'}:continue
        if entry['unresolved_bindings']:
            deferred+=1;continue
        target=(profile/'agents' if entry['component']=='codex-agents' else home/'.agents/agents')/entry['relative_path']
        groups.append((target,[(entry,None)]))
    disjoint=[]
    for group in sorted(groups,key=lambda g:(len(g[0].parts),str(g[0]).casefold())):
        if any(group[0].is_relative_to(existing[0]) for existing in disjoint):continue
        disjoint.append(group)
    return disjoint,deferred,inventory


def _project_roots(bundle,projects,generation):
    roots={}
    for p in bundle['projects']:
        label=''.join(c if c.isalnum() or c in ' -_' else '_' for c in p['name']).strip(' .')[:50] or 'Проект'
        identity=hashlib.sha256(p['id'].encode()).hexdigest()[:16]
        roots[p['id']]=str(projects/(label+' - '+identity+' - '+generation[:8]))
    if any(t['project_id'] is None for t in bundle['threads']):roots['__projectless__']=str(projects/('Без проекта - '+generation[:8]))
    snapshot._check_collisions([Path(p).relative_to(projects).as_posix() for p in roots.values()])
    return roots


def prepare(stage, destination, *, expected_digest, expected_stage_digest, target_profile, projects_root,target_home=None):
    stage=managed._local(stage)
    staging.verify_native_stage(stage,expected_digest=expected_digest,expected_stage_digest=expected_stage_digest)
    target=managed._local(target_profile)
    home=managed._local(target.parent if target_home is None else target_home)
    backup._require(target.is_relative_to(home),'Target profile outside explicit home')
    projects=managed._local(projects_root,missing=True)
    destination=managed._local(destination,missing=True)
    backup._require(not destination.exists(),'Apply candidate destination exists')
    backup._require(not any(staging._overlap(a,b) for a,b in ((destination,stage),(destination,target),
                    (projects,stage),(projects,target),(projects,destination))),'Apply paths overlap')
    # Use exact current schema from the sealed stage, without reading live DBs.
    destination.mkdir(parents=True)
    template=destination/'Schema'
    template.mkdir()
    native.mark_synthetic_profile(template)
    for filename in staging.DATABASES:
        with native._connect(stage/'Profile'/filename,readonly=True) as source, native._connect(template/filename) as output:
            objects=source.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name").fetchall()
            for row in objects:
                backup._require(native._schema_object_allowed(row),'Unexpected native apply schema')
                output.execute(row['sql'])
            rows=source.execute('SELECT * FROM _sqlx_migrations').fetchall()
            if rows: output.executemany('INSERT INTO _sqlx_migrations VALUES('+','.join('?' for _ in rows[0])+')',rows)
    pins={f:snapshot._hash_file(template/f)[0] for f in staging.DATABASES}
    staging._template(template,pins)
    candidate=backup._read(stage/'Package'/snapshot.CHAT_BUNDLE_PATH)
    bundle=candidate['transfer']
    generation=uuid.uuid4().hex
    roots=_project_roots(bundle,projects,generation)
    native._write_profile(bundle,destination/'Profile',roots,schema_template=template,
                         synthetic_only=False,runtime_version=backup.SUPPORTED_RUNTIME,display_metadata=candidate.get('display_metadata'))
    profile=destination/'Profile'
    with native._connect(profile/'state_5.sqlite') as db:
        for row in db.execute('SELECT id,rollout_path FROM threads').fetchall():
            relative=Path(row['rollout_path']).relative_to(profile)
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(target/relative),row['id']))
    state=backup._read(profile/'.codex-global-state.json')
    for key in ('app-server-project-id-by-legacy-project-id-by-host','app-server-projects-migration-by-host'):
        state[key]={'local:'+str(target):next(iter(state[key].values()))}
    (profile/'.codex-global-state.json').write_text(native._json(state),encoding='utf-8')
    report=backup._read(profile/'import-report.json');report['destination']=str(target)
    (profile/'import-report.json').write_text(native._json(report),encoding='utf-8')
    changes=[]
    for name in (*CORE,*SIDECARS):
        changes.append({'target':str(target/name),'candidate':'Profile/'+name if (profile/name).exists() else None,
                        'kind':'native'})
    for pid,root in roots.items():
        source=destination/'Projects'/pid
        source.mkdir(parents=True)
        for relative in backup._read(stage/'Package'/backup.DESCRIPTOR)['result_paths']:
            parts=relative.split('/')
            if parts[1]==pid:
                snapshot._copy_verified(stage/'Package'/relative,source.joinpath(*parts[2:]))
        changes.append({'target':root,'candidate':'Projects/'+pid,'kind':'project'})
    groups,deferred,inventory=_environment_groups(stage,home,target)
    for index,(final,entries) in enumerate(groups):
        relative='Environment/'+str(index)
        output=destination/relative
        for entry,child in entries:
            path=output/child if child is not None else output
            path.parent.mkdir(parents=True,exist_ok=True)
            data=resource_bytes(environment._read_stable(stage/'Environment'/entry['path'],entry['size']),entry,
                                inventory['token_namespace'],home=home,codex_home=target)
            with path.open('xb') as stream:stream.write(data)
        changes.append({'target':str(final),'candidate':relative,'kind':'environment'})
    plan={'format':'rehome-native-apply/1','synthetic_only':False,'generation':generation,
        'stage':str(stage),'backup_manifest_sha256':expected_digest,'stage_digest':expected_stage_digest,
        'target_profile':str(target),'target_home':str(home),'projects_root':str(projects),'project_roots':roots,'changes':changes,
        'environment_groups_prepared':len(groups),'environment_groups_deferred':deferred,
        'profile_applied':False,'environment_activated':False,'runtime_version':backup.SUPPORTED_RUNTIME}
    snapshot._write_json(destination/PLAN,plan)
    staging._readback(profile,bundle,roots,logical_profile=target,display_metadata=candidate.get('display_metadata'))
    snapshot._write_json(destination/READY,{'format':'rehome-native-apply-ready/1','files':_inventory(destination)})
    digest=snapshot._hash_file(destination/READY)[0]
    return verify(destination,expected_apply_digest=digest)


def _inventory(root):
    result=[]
    for p in sorted(root.rglob('*')):
        snapshot._safe_path(p)
        relative=p.relative_to(root).as_posix()
        if relative==READY:continue
        backup._require(len(result)<staging.MAX_STAGE_FILES,'Apply candidate entry limit exceeded')
        if p.is_dir():result.append({'path':relative,'type':'directory'})
        else:
            backup._require(p.stat().st_nlink==1,'Linked apply resource prohibited')
            digest,size=snapshot._hash_file(p)
            result.append({'path':relative,'type':'file','sha256':digest,'size':size})
    return result


def verify(root, *, expected_apply_digest):
    root=managed._local(root)
    backup._require(staging._pin(expected_apply_digest) and snapshot._hash_file(root/READY)[0]==expected_apply_digest,
                    'Apply candidate differs from external pin')
    ready=backup._read(root/READY)
    backup._require(ready=={'format':'rehome-native-apply-ready/1','files':_inventory(root)},'Apply candidate changed')
    plan=backup._read(root/PLAN)
    backup._require(set(plan)=={'format','synthetic_only','generation','stage','backup_manifest_sha256','stage_digest',
        'target_profile','target_home','projects_root','project_roots','changes','environment_groups_prepared','environment_groups_deferred',
        'profile_applied','environment_activated','runtime_version'} and plan['format']=='rehome-native-apply/1' and
        plan['synthetic_only'] is False and plan['profile_applied'] is False and plan['environment_activated'] is False and
        plan['runtime_version']==backup.SUPPORTED_RUNTIME and len(plan['generation'])==32 and
        all(c in '0123456789abcdef' for c in plan['generation']),
                    'Invalid native apply contract')
    staging.verify_native_stage(plan['stage'],expected_digest=plan['backup_manifest_sha256'],expected_stage_digest=plan['stage_digest'])
    history=backup._read(Path(plan['stage'])/'Package'/snapshot.CHAT_BUNDLE_PATH)
    bundle=history['transfer']
    backup._require(plan['project_roots']==_project_roots(bundle,Path(plan['projects_root']),plan['generation']),
                    'Apply project roots differ')
    staging._readback(root/'Profile',bundle,plan['project_roots'],logical_profile=Path(plan['target_profile']),display_metadata=history.get('display_metadata'))
    groups,deferred,inventory=_environment_groups(Path(plan['stage']),Path(plan['target_home']),Path(plan['target_profile']))
    expected_changes=[{'target':str(Path(plan['target_profile'])/name),
                       'candidate':'Profile/'+name if (root/'Profile'/name).exists() else None,'kind':'native'} for name in (*CORE,*SIDECARS)]
    expected_changes.extend({'target':path,'candidate':'Projects/'+pid,'kind':'project'} for pid,path in
                            _project_roots(bundle,Path(plan['projects_root']),plan['generation']).items())
    for change in plan['changes']:
        target=managed._local(change['target'],missing=True)
        if change['kind']=='native':
            backup._require(target.parent==Path(plan['target_profile']) and target.name in (*CORE,*SIDECARS),
                            'Native apply target outside fixed policy')
        elif change['kind']=='project':
            backup._require(change['kind']=='project' and str(target) in plan['project_roots'].values() and
                            target.parent==Path(plan['projects_root']) and not target.exists(),'Project destination exists or escaped')
        else:
            backup._require(change['kind']=='environment' and str(target) in {str(p) for p,_ in groups},
                            'Environment target outside fixed policy')
        if change['candidate'] is not None:
            snapshot._validate_relative(change['candidate'])
            managed._local(root/change['candidate'])
    expected=[{'target':str(final),'candidate':'Environment/'+str(index),'kind':'environment'} for index,(final,_) in enumerate(groups)]
    backup._require(plan['changes']==expected_changes+expected and
                    plan['environment_groups_prepared']==len(groups) and plan['environment_groups_deferred']==deferred,
                    'Environment activation targets differ')
    paths=[managed._local(c['target'],missing=True) for c in plan['changes']]
    common=Path(os.path.commonpath(paths))
    snapshot._check_collisions([p.relative_to(common).as_posix() for p in paths])
    for relative in backup._read(Path(plan['stage'])/'Package'/backup.DESCRIPTOR)['result_paths']:
        backup._require(snapshot._hash_file(root/relative)==snapshot._hash_file(Path(plan['stage'])/'Package'/relative),
                        'Apply finished result differs')
    if groups:
        source=Path(plan['stage'])/'Environment'
        for index,(_,entries) in enumerate(groups):
            for entry,child in entries:
                file=root/'Environment'/str(index)
                if child is not None:file=file/child
                data=resource_bytes(environment._read_stable(source/entry['path'],entry['size']),entry,inventory['token_namespace'],
                    home=Path(plan['target_home']),codex_home=Path(plan['target_profile']))
                backup._require(snapshot._hash_file(file)==(hashlib.sha256(data).hexdigest(),len(data)),'Apply environment resource differs')
    backup._require(snapshot._hash_file(root/READY)[0]==expected_apply_digest,'Apply pin changed during validation')
    return {'status':'apply_candidate_prepared','candidate':str(root),'apply_digest':expected_apply_digest,
            'target_profile':plan['target_profile'],'changes':len(plan['changes']),'profile_applied':False,
            'environment_activated':False}


def verify_installed(candidate):
    """Semantic readback at final paths, after moves and before success."""
    plan=backup._read(Path(candidate)/PLAN)
    history=backup._read(Path(plan['stage'])/'Package'/snapshot.CHAT_BUNDLE_PATH)
    staging._readback(Path(plan['target_profile']),history['transfer'],plan['project_roots'],display_metadata=history.get('display_metadata'))
