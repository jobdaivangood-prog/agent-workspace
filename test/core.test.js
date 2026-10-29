// Non-destructive observations: all synthetic input/output is retained under work.
import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {Store,readJson,hash} from '../src/store.js';
import {taskSchema} from '../src/schema.js';

const observations=path.resolve('work','core-observations');
async function setup(){await fs.mkdir(observations,{recursive:true});const base=await fs.mkdtemp(path.join(observations,'run-'));const shared=path.join(base,'shared-a');const a=new Store(path.join(base,'local-a'));await a.connect(shared,{create:true});return {base,shared,a};}
const task=(project_id,changes={})=>({project_id,title:'Тестовая задача',goal:'Продолжение по проверенной записи',task_status:'in_progress',constraints:['Исходники остаются локально'],next_steps:['Проверить следующий этап'],...changes});
async function pair(){const s=await setup();const project=await s.a.createProject('Синтетический проект');const first=await s.a.commit({kind:'task',data:task(project.record_id)});const sharedB=path.join(s.base,'shared-b');await fs.cp(s.shared,sharedB,{recursive:true});const b=new Store(path.join(s.base,'local-b'));await b.connect(sharedB);await b.startup();return {...s,b,sharedB,project,first};}

test('new device can read a complete accepted handoff and bootstrap',async()=>{
  const {a,b,first,shared}=await pair();assert.equal((await a.startup()).status,'ready');assert.equal((await b.get(first.record_id)).state.data.goal,'Продолжение по проверенной записи');assert.match(await fs.readFile(path.join(shared,'START_HERE.md'),'utf8'),/workspace_review/);
});

test('verified object digests are reused only for the same file within one operation',async()=>{
  const {a,base}=await setup();const source=path.join(base,'ready.txt');
  await fs.writeFile(source,'original');const object=await a.putFile(source);const ctx=await a.context();
  await a.verifyArtifacts(ctx,[object,object]);assert.equal(ctx.verifiedObjects.size,1);
  const fresh=await a.context();assert.equal(fresh.verifiedObjects.size,0);
  const stored=a.objectPath(ctx.root,object.sha256);await fs.writeFile(stored,'modified');
  const changed=new Date(Date.now()+10000);await fs.utimes(stored,changed,changed);
  await assert.rejects(a.verifyArtifacts(ctx,[object]),e=>e.code==='CORRUPT_OBJECT');
});
test('unchanged normalized task does not publish or rewrite a revision',async()=>{
  const {a,shared}=await setup();const project=await a.createProject('P');const data=task(project.record_id);
  const first=await a.commit({kind:'task',data});const recordDir=path.join(shared,'records',first.record_id);
  const ctx=await a.context(),selectionFile=path.join(ctx.localDir,'selected.json');const selectionBefore=await fs.stat(selectionFile);
  await a.startup();assert.equal((await fs.stat(selectionFile)).mtimeMs,selectionBefore.mtimeMs,'An unchanged startup must not rewrite the selection map');
  const revisionDir=path.join(recordDir,first.revision_id);const files=['state.json','summary.md','manifest.json'];
  const before=await Promise.all(files.map(async name=>({name,bytes:await fs.readFile(path.join(revisionDir,name)),stat:await fs.stat(path.join(revisionDir,name))})));
  // Omitted defaults and JSON property order do not change the normalized handoff.
  const normalized=(await a.get(first.record_id)).state.data;
  const reordered=Object.fromEntries(Object.entries(normalized).reverse());
  const second=await a.commit({record_id:first.record_id,kind:'task',data:reordered,expected_revision:first.revision_id});
  assert.deepEqual(second,{record_id:first.record_id,revision_id:first.revision_id,kind:'task',status:'unchanged'});
  assert.deepEqual(await fs.readdir(recordDir),[first.revision_id]);
  for(const previous of before){assert.deepEqual(await fs.readFile(path.join(revisionDir,previous.name)),previous.bytes);assert.equal((await fs.stat(path.join(revisionDir,previous.name))).mtimeMs,previous.stat.mtimeMs);}
});
test('a changed task creates a revision and stale identical data is rejected',async()=>{
  const {a,shared}=await setup();const project=await a.createProject('P');const first=await a.commit({kind:'task',data:task(project.record_id)});
  const changed=task(project.record_id,{completed:['Проверенный этап']});
  const next=await a.commit({record_id:first.record_id,kind:'task',data:changed,expected_revision:first.revision_id});
  assert.notEqual(next.revision_id,first.revision_id);assert.equal((await a.get(first.record_id)).state.parents[0],first.revision_id);
  await assert.rejects(a.commit({record_id:first.record_id,kind:'task',data:changed,expected_revision:first.revision_id}),e=>e.code==='STALE_BASE');
  assert.equal((await fs.readdir(path.join(shared,'records',first.record_id))).length,2);
});
test('unchanged task data still publishes a changed verified artifact set',async()=>{
  const {a,base}=await setup();const project=await a.createProject('P');const data=task(project.record_id);
  const first=await a.commit({kind:'task',data});const file=path.join(base,'explicit-result.txt');await fs.writeFile(file,'Synthetic artifact observation');
  const artifact=await a.putFile(file);const next=await a.commit({record_id:first.record_id,kind:'task',data,artifacts:[artifact],expected_revision:first.revision_id});
  assert.notEqual(next.revision_id,first.revision_id);assert.equal((await a.get(first.record_id)).state.artifacts[0].sha256,artifact.sha256);
  const repeated=await a.commit({record_id:first.record_id,kind:'task',data,artifacts:[{name:artifact.name,size:artifact.size,sha256:artifact.sha256}],expected_revision:next.revision_id});
  assert.equal(repeated.status,'unchanged');assert.equal(repeated.revision_id,next.revision_id);
});
test('every incoming change to an accepted record needs explicit choice',async()=>{
  const {a,b,shared,sharedB,project,first}=await pair();const next=await a.commit({record_id:first.record_id,kind:'task',data:task(project.record_id,{completed:['Этап A']})});await fs.cp(shared,sharedB,{recursive:true,force:true});
  assert.equal((await b.startup()).status,'choice_required');assert.equal((await b.get(first.record_id)).state.revision_id,first.revision_id);
  const review=await b.review(first.record_id);assert.equal(review.recommendation.action,'take_revision');
  await assert.rejects(b.choose({review_id:review.review_id,choice:'take_revision',revision_id:next.revision_id}),e=>e.code==='CONFIRMATION_REQUIRED');
  await b.choose({review_id:review.review_id,choice:'take_revision',revision_id:next.revision_id,user_confirmed:true});assert.deepEqual((await b.get(first.record_id)).state.data.completed,['Этап A']);assert.equal((await b.get(first.record_id,first.revision_id)).state.revision_id,first.revision_id);
});
test('parallel histories are both retained after an approved merge',async()=>{
  const {a,b,shared,sharedB,project,first}=await pair();const left=await a.commit({record_id:first.record_id,kind:'task',data:task(project.record_id,{completed:['A']})});const right=await b.commit({record_id:first.record_id,kind:'task',data:task(project.record_id,{completed:['B']})});await fs.cp(sharedB,shared,{recursive:true,force:true});
  const r=await a.review(first.record_id);assert.equal(r.candidates.length,2);await a.choose({review_id:r.review_id,choice:'merge',merged_data:task(project.record_id,{completed:['A','B']}),user_confirmed:true});assert.deepEqual((await a.get(first.record_id)).state.parents.sort(),[left.revision_id,right.revision_id].sort());
});
test('a new arrival invalidates a previously shown review',async()=>{
  const {a,b,shared,sharedB,project,first}=await pair();const r=await b.review(first.record_id);await a.commit({record_id:first.record_id,kind:'task',data:task(project.record_id,{completed:['Позднее изменение']})});await fs.cp(shared,sharedB,{recursive:true,force:true});await assert.rejects(b.choose({review_id:r.review_id,choice:'keep_local',user_confirmed:true}),e=>e.code==='STALE_REVIEW');
});
test('partial manifests are not activated',async()=>{
  const {a,b,shared,sharedB,project,first}=await pair();const next=await a.commit({record_id:first.record_id,kind:'task',data:task(project.record_id,{completed:['Новая запись']})});const dest=path.join(sharedB,'records',first.record_id,next.revision_id);await fs.mkdir(dest,{recursive:true});await fs.copyFile(path.join(shared,'records',first.record_id,next.revision_id,'manifest.json'),path.join(dest,'manifest.json'));
  assert.equal((await b.startup()).status,'sync_incomplete');assert.equal((await b.get(first.record_id)).state.revision_id,first.revision_id);
});
test('changed contents are detected even when metadata is otherwise readable',async()=>{
  const {a,shared}=await setup();const p=await a.createProject('P');await fs.appendFile(path.join(shared,'records',p.record_id,p.revision_id,'summary.md'),'Changed synthetic summary');assert.equal((await a.startup()).status,'sync_incomplete');
});
test('finished files deduplicate and restore into new local directories',async()=>{
  const {a,base}=await setup();const p=await a.createProject('P');const f=path.join(base,'final.txt');await fs.writeFile(f,'Готовый тестовый результат');const first=await a.publishResult(f,p.record_id);const second=await a.publishResult(f,p.record_id);assert.equal((await a.get(first.record_id)).state.artifacts[0].sha256,(await a.get(second.record_id)).state.artifacts[0].sha256);
  const restored=path.join(base,'restored');await a.restorePackage(first.record_id,restored);assert.equal(await fs.readFile(path.join(restored,'final.txt'),'utf8'),'Готовый тестовый результат');await assert.rejects(a.restorePackage(first.record_id,restored),e=>e.code==='DESTINATION_EXISTS');
});
test('changing the synchronized folder keeps the workspace identity',async()=>{
  const {a,base,shared}=await setup();const p=await a.createProject('P');const moved=path.join(base,'other-provider');await fs.cp(shared,moved,{recursive:true});await a.connect(moved);assert.equal((await a.get(p.record_id)).state.data.name,'P');
});
test('closed sessions, stage completion and overall completion remain distinct',()=>{
  const value=task('project-1',{session_status:'closed',current_stage:'s1',plan:[{id:'s1',title:'Первый этап',done_criterion:'Есть проверяемый результат',status:'in_progress'}]});assert.equal(taskSchema.parse(value).task_status,'in_progress');assert.throws(()=>taskSchema.parse({...value,task_status:'completed',done_criteria:['Готово'],verification:[{check:'Проверка',result:'Проверено',status:'passed',evidence:'test'}]}));
  assert.throws(()=>taskSchema.parse(task('project-1',{claims:[{id:'c1',text:'Работает',evidence_status:'confirmed',reason:'Кажется'}]})));
});
test('path traversal and overlapping local/shared storage are rejected',async()=>{
  const {a,shared}=await setup();await assert.rejects(a.commit({record_id:'../escape',kind:'project',data:{name:'P'}}));const bad=new Store(path.join(shared,'local'));await assert.rejects(bad.connect(shared),e=>e.code==='LOCAL_IN_SHARED');
});

test('junctions cannot redirect object, revision or restore writes',async()=>{
  const {a,base,shared}=await setup();const outside=path.join(base,'redirect-target');await fs.mkdir(outside);
  const input=path.join(base,'junction-check.txt');const contents='Synthetic junction observation';await fs.writeFile(input,contents);
  const prefix=path.join(shared,'objects',hash(contents).slice(0,2));await fs.symlink(outside,prefix,process.platform==='win32'?'junction':'dir');
  await assert.rejects(a.putFile(input),e=>e.code==='PATH_ESCAPE');assert.deepEqual(await fs.readdir(outside),[]);
  const record=randomUUID();await fs.symlink(outside,path.join(shared,'records',record),process.platform==='win32'?'junction':'dir');
  await assert.rejects(a.commit({record_id:record,kind:'project',data:{name:'Redirect must fail'}}),e=>e.code==='PATH_ESCAPE');assert.deepEqual(await fs.readdir(outside),[]);
  // Use an independent clean replica to inspect the restore destination guard.
  const clean=await setup();const p=await clean.a.createProject('P');const f=path.join(clean.base,'final.txt');await fs.writeFile(f,'Final observation');const result=await clean.a.publishResult(f,p.record_id);
  const redirect=path.join(clean.base,'redirect');const target=path.join(clean.base,'target');await fs.mkdir(target);await fs.symlink(target,redirect,process.platform==='win32'?'junction':'dir');
  await assert.rejects(clean.a.restorePackage(result.record_id,path.join(redirect,'new')),e=>e.code==='PATH_ESCAPE');assert.deepEqual(await fs.readdir(target),[]);
});

test('changes to an accepted ancestor invalidate the shown comparison',async()=>{
  const {a,b,shared,sharedB,project,first}=await pair();const next=await a.commit({record_id:first.record_id,kind:'task',data:task(project.record_id,{completed:['A']})});await fs.cp(shared,sharedB,{recursive:true,force:true});
  const review=await b.review(first.record_id);const dir=path.join(sharedB,'records',first.record_id,first.revision_id);const state=await readJson(path.join(dir,'state.json'));state.data.title='Changed ancestor';const content=JSON.stringify(state,null,2)+'\n';await fs.writeFile(path.join(dir,'state.json'),content);const manifest=await readJson(path.join(dir,'manifest.json'));manifest.files['state.json']=hash(content);await fs.writeFile(path.join(dir,'manifest.json'),JSON.stringify(manifest,null,2)+'\n');
  await assert.rejects(b.choose({review_id:review.review_id,choice:'take_revision',revision_id:next.revision_id,user_confirmed:true}),e=>e.code==='STALE_REVIEW');
});

test('invalid package paths are rejected before creating a restore directory',async()=>{
  const {a,base}=await setup();const input=path.join(base,'input.txt');await fs.writeFile(input,'Package observation');const object=await a.putFile(input);
  const saved=await a.commit({kind:'mcp',data:{name:'Conflicting paths',files:[{relative_path:'a',sha256:object.sha256,size:object.size},{relative_path:'a/b.txt',sha256:object.sha256,size:object.size}]},artifacts:[object]});
  const target=path.join(base,'must-not-exist');await assert.rejects(a.restorePackage(saved.record_id,target),e=>e.code==='DUPLICATE_PATH');await assert.rejects(fs.stat(target),e=>e.code==='ENOENT');
});

test('invalid task merge and artifact limits cannot publish a revision',async()=>{
  const {a,base,shared}=await setup();const p=await a.createProject('P');const other=await a.createProject('Other');const t=await a.commit({kind:'task',data:task(p.record_id)});const f=path.join(base,'result.txt');await fs.writeFile(f,'Other project result');const r=await a.publishResult(f,other.record_id);const review=await a.review(t.record_id);const revisions=await fs.readdir(path.join(shared,'records',t.record_id));
  await assert.rejects(a.choose({review_id:review.review_id,choice:'merge',merged_data:task(p.record_id,{result_records:[r.record_id]}),user_confirmed:true}),e=>e.code==='INVALID_RESULT');assert.deepEqual(await fs.readdir(path.join(shared,'records',t.record_id)),revisions);
  const count=(await fs.readdir(path.join(shared,'records'))).length;await assert.rejects(a.commit({kind:'project',data:{name:'Too many artifacts'},artifacts:Array.from({length:2001},()=>({}))}),e=>e.code==='INVALID_ARTIFACTS');assert.equal((await fs.readdir(path.join(shared,'records'))).length,count);
});

test('stage plan is present in the human-readable handoff',async()=>{
  const {a}=await setup();const p=await a.createProject('P');const saved=await a.commit({kind:'task',data:task(p.record_id,{current_stage:'s2',plan:[{id:'s1',title:'Согласование',done_criterion:'Концепция принята',status:'completed',verification:'Есть согласование пользователя'},{id:'s2',title:'Реализация',done_criterion:'Проверенный пакет',status:'in_progress'}]})});const value=await a.get(saved.record_id);assert.match(value.summary,/Текущий этап: s2/);assert.match(value.summary,/Концепция принята/);assert.match(value.summary,/\[in_progress\] s2/);
});
