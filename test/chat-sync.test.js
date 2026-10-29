// Pure fake Codex source; synthetic workspace observations are retained locally.
// No native history, subprocesses, network, or cleanup deletion is used.
import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import {Store,hash,readJson,atomicJson} from '../src/store.js';
import {ChatSync,chatIds} from '../src/chat-sync.js';

const observations=path.resolve('work','chat-sync-observations');
const thread=(id,extra={})=>({id,title:'Synthetic '+id,project_id:null,archived:false,status:'idle',...extra});
const snapshot=(session,version='one',extra={})=>({
  session_id:session,title:'Synthetic '+session,project_id:null,archived:false,active:false,
  latest_turn_id:'turn-'+version,last_item_id:'item-'+version,fingerprint:hash([session,version]),
  checkpoint:{version:1,thread_id:session,last_turn_id:'turn-'+version,last_turn_hash:hash(version)},
  coverage:{complete_increment:true,initial_import:true,turns_read:1},change_type:'changed',
  public_messages:[{role:'user',text:'A synthetic request',turn_id:'turn-'+version,item_id:'user-'+version},{role:'assistant',text:'A synthetic reply',turn_id:'turn-'+version,item_id:'item-'+version}],
  ...extra
});
const handoff=(project_id,session,extra={})=>({project_id,title:'Synthetic handoff '+session,goal:'Continue the checked synthetic task',task_status:'in_progress',source_session:{provider:'codex',session_id:session},next_steps:['Inspect the next stage'],...extra});
function fakeSource(threads,snapshots=new Map()) {
  return {
    enumerations:0,inspections:[],threads,snapshots,
    async enumerate(){this.enumerations++;return {threads:structuredClone(this.threads),complete:true,scope:'synthetic source'};},
    async inspect(session,checkpoint,options){
      this.inspections.push({session,checkpoint,options});
      const value=structuredClone(this.snapshots.get(session)||snapshot(session));
      return {snapshot:value,unchanged:checkpoint?.last_turn_hash===value.checkpoint.last_turn_hash};
    }
  };
}
async function setup(threads=[thread('chat-a')]) {
  await fs.mkdir(observations,{recursive:true});const base=await fs.mkdtemp(path.join(observations,'run-'));
  const store=new Store(path.join(base,'local'));const shared=path.join(base,'shared');await store.connect(shared,{create:true});
  const project=await store.createProject('Synthetic chat project');const source=fakeSource(threads);const sync=new ChatSync(store,source);
  return {base,shared,store,project,source,sync};
}
const revisions=async(shared,record)=>fs.readdir(path.join(shared,'records',record)).catch(e=>{if(e.code==='ENOENT')return [];throw e;});
const folderEntries=async folder=>fs.readdir(folder).catch(e=>{if(e.code==='ENOENT')return [];throw e;});

test('chat scan reuses startup index; unchanged chat writes no scan token or revision',async()=>{
  const {store,shared,project,source,sync}=await setup();let scans=0;
  const originalScan=store.scan.bind(store);store.scan=async(...args)=>{scans++;return originalScan(...args);};
  const first=await sync.scan();assert.equal(first.counts.needs_summary,1);assert.equal(scans,1,'Only startup scans the existing project');
  const saved=await sync.save({scan_id:first.scan_id,session_id:'chat-a',task:handoff(project.record_id,'chat-a')});
  const ctx=await store.context();const tokensBefore=await folderEntries(path.join(ctx.localDir,'chat-scans'));
  const runFilesBefore=await folderEntries(path.join(ctx.localDir,'chat-runs'));const next=await sync.scan();
  assert.equal(next.counts.unchanged,1);assert.equal(next.scan_id,null);assert.equal(next.run_id,null);
  assert.deepEqual(await folderEntries(path.join(ctx.localDir,'chat-scans')),tokensBefore);
  assert.deepEqual(await folderEntries(path.join(ctx.localDir,'chat-runs')),runFilesBefore);
  assert.equal((await revisions(shared,saved.record_id)).length,1);
  assert.equal((await revisions(shared,chatIds('chat-a').cursor)).length,1);
  assert.equal(source.inspections.at(-1).checkpoint.last_turn_id,'turn-one');
});

test('frozen inventory pagination does not repeat enumeration or shift offsets',async()=>{
  const {store,source,sync}=await setup([thread('a'),thread('b'),thread('c')]);
  let startups=0;const original=store.startup.bind(store);store.startup=async(...args)=>{startups++;return original(...args);};
  const first=await sync.scan({limit:1});assert.equal(first.next_offset,1);assert.ok(first.run_id);
  source.threads.unshift(thread('newer-chat'));source.threads.reverse();
  const second=await sync.scan({run_id:first.run_id,offset:first.next_offset,limit:1});
  const third=await sync.scan({run_id:first.run_id,offset:second.next_offset,limit:1});
  assert.deepEqual([first,second,third].flatMap(page=>page.chats.map(chat=>chat.session_id)),['a','b','c']);
  assert.equal(source.enumerations,1);assert.equal(startups,1);assert.equal(third.next_offset,null);
  assert.equal(source.inspections[1].options.thread_metadata.id,'b');
  await assert.rejects(sync.scan({offset:1,limit:1}),e=>e.code==='RUN_REQUIRED');
});

test('known active chats are skipped before their history is inspected',async()=>{
  const {source,sync}=await setup([thread('a',{status:'active'}),thread('b'),thread('current',{status:'active'})]);
  const result=await sync.scan({current_session_id:'current',active_session_ids:['b']});
  assert.equal(result.counts.active_pending,2);assert.equal(result.counts.needs_summary,1);
  assert.deepEqual(source.inspections.map(item=>item.session),['current']);
});

test('partial initial history produces pending_history and cannot advance a cursor',async()=>{
  const {source,sync,store,shared,project}=await setup();
  source.snapshots.set('chat-a',snapshot('chat-a','partial',{coverage:{complete_increment:false,truncated:true},change_type:'partial'}));
  const partial=await sync.scan({inspect_options:{maxTurns:2,maxCharacters:40,fullAudit:true}});
  assert.equal(partial.counts.pending_history,1);assert.equal(partial.scan_id,null);assert.equal(partial.run_id,null);
  assert.equal(source.inspections[0].options.fullAudit,true);assert.equal(source.inspections[0].options.maxCharacters,40);
  assert.deepEqual(await revisions(shared,chatIds('chat-a').task),[]);
  source.snapshots.set('chat-a',snapshot('chat-a'));
  const complete=await sync.scan();const ctx=await store.context();const tokenPath=path.join(ctx.localDir,'chat-scans',complete.scan_id+'.json');
  const token=await readJson(tokenPath);token.entries[0].snapshot.coverage.complete_increment=false;await atomicJson(tokenPath,token);
  await assert.rejects(sync.save({scan_id:complete.scan_id,session_id:'chat-a',task:handoff(project.record_id,'chat-a')}),e=>e.code==='PENDING_HISTORY');
  assert.deepEqual(await revisions(shared,chatIds('chat-a').cursor),[]);
});

test('legacy incomplete coverage cannot become unchanged without a complete history audit',async()=>{
  const {store,project,source,sync}=await setup();const first=await sync.scan();
  await sync.save({scan_id:first.scan_id,session_id:'chat-a',task:handoff(project.record_id,'chat-a')});
  const cursorId=chatIds('chat-a').cursor;const cursor=(await store.get(cursorId)).state;
  await store.commit({record_id:cursorId,kind:'profile',expected_revision:cursor.revision_id,data:{...cursor.data,coverage:{complete_increment:false,truncated:true}}});
  const pending=await sync.scan();assert.equal(pending.counts.pending_history,1);assert.equal(pending.counts.unchanged,0);assert.equal(pending.scan_id,null);
  source.snapshots.set('chat-a',snapshot('chat-a','one',{coverage:{complete_increment:true,complete_history:true}}));
  const repaired=await sync.scan({inspect_options:{fullAudit:true}});assert.equal(repaired.counts.needs_summary,1);assert.ok(repaired.scan_id);
});

test('partial cursor failure retries the same task once and completed retry makes no writes',async()=>{
  const {store,shared,project,sync}=await setup();const scanned=await sync.scan();const task=handoff(project.record_id,'chat-a');
  const original=store._commit.bind(store);let failCursor=true;
  store._commit=async(ctx,args)=>{if(args.kind==='profile'&&failCursor){failCursor=false;throw Object.assign(new Error('Synthetic cursor failure'),{code:'SYNTHETIC_FAILURE'});}return original(ctx,args);};
  await assert.rejects(sync.save({scan_id:scanned.scan_id,session_id:'chat-a',task}),e=>e.code==='SYNTHETIC_FAILURE');
  assert.equal((await revisions(shared,chatIds('chat-a').task)).length,1);assert.deepEqual(await revisions(shared,chatIds('chat-a').cursor),[]);
  await assert.rejects(sync.save({scan_id:scanned.scan_id,session_id:'chat-a',task:{...task,goal:'Different text'}}),e=>e.code==='RETRY_CONTENT_CHANGED');
  const recovered=await sync.save({scan_id:scanned.scan_id,session_id:'chat-a',task});assert.equal(recovered.status,'saved');
  const ctx=await store.context();const receiptFile=path.join(ctx.localDir,'chat-scans',scanned.scan_id+'-receipt-'+hash('chat-a').slice(0,48)+'.json');const before=await fs.stat(receiptFile);
  const repeated=await sync.save({scan_id:scanned.scan_id,session_id:'chat-a',task:Object.fromEntries(Object.entries(task).reverse())});
  assert.equal(repeated.status,'unchanged');assert.equal(repeated.revision_id,recovered.revision_id);assert.equal(repeated.cursor_revision,recovered.cursor_revision);
  assert.equal((await revisions(shared,chatIds('chat-a').task)).length,1);assert.equal((await revisions(shared,chatIds('chat-a').cursor)).length,1);
  assert.equal((await fs.stat(receiptFile)).mtimeMs,before.mtimeMs);
});

test('a later task change blocks retry instead of replacing its expected base',async()=>{
  const {store,project,sync}=await setup();const scanned=await sync.scan();const task=handoff(project.record_id,'chat-a');
  const original=store._commit.bind(store);let failCursor=true;
  store._commit=async(ctx,args)=>{if(args.kind==='profile'&&failCursor){failCursor=false;throw Object.assign(new Error('Synthetic interruption'),{code:'SYNTHETIC_FAILURE'});}return original(ctx,args);};
  await assert.rejects(sync.save({scan_id:scanned.scan_id,session_id:'chat-a',task}),e=>e.code==='SYNTHETIC_FAILURE');
  const accepted=(await store.get(chatIds('chat-a').task)).state;
  await store.commit({record_id:accepted.record_id,kind:'task',expected_revision:accepted.revision_id,data:{...task,completed:['A later checked change']}});
  await assert.rejects(sync.save({scan_id:scanned.scan_id,session_id:'chat-a',task}),e=>e.code==='STALE_BASE');
  assert.deepEqual((await store.get(accepted.record_id)).state.data.completed,['A later checked change']);
});

test('a changed cursor base blocks task publication; task and cursor share one writer lock',async()=>{
  const {store,shared,project,sync}=await setup();const scanned=await sync.scan();const ids=chatIds('chat-a');
  await store.commit({record_id:ids.cursor,kind:'profile',expected_revision:null,data:{type:'codex-chat-cursor-v1',provider:'codex',session_id:'chat-a',task_record_id:ids.task,fingerprint:hash('other')}});
  let locks=0;const original=store.locked.bind(store);store.locked=async fn=>{locks++;return original(fn);};
  await assert.rejects(sync.save({scan_id:scanned.scan_id,session_id:'chat-a',task:handoff(project.record_id,'chat-a')}),e=>e.code==='STALE_BASE');
  assert.equal(locks,1);assert.deepEqual(await revisions(shared,ids.task),[]);
});

test('a scan from a frozen run keeps the original task base after another page saves',async()=>{
  const {store,project,source,sync}=await setup([thread('a'),thread('b')]);
  const first=await sync.scan({limit:1});const saved=await sync.save({scan_id:first.scan_id,session_id:'a',task:handoff(project.record_id,'a')});
  const second=await sync.scan({run_id:first.run_id,offset:1,limit:1});assert.equal(second.chats[0].session_id,'b');
  assert.equal(source.enumerations,1);assert.equal((await store.get(saved.record_id)).state.revision_id,saved.revision_id);
  await sync.save({scan_id:second.scan_id,session_id:'b',task:handoff(project.record_id,'b')});
});

test('historical identities cannot silently rebind an accepted task to an old chat',async()=>{
  const {store,project,sync}=await setup([thread('a')]);const old=await store.commit({kind:'task',data:handoff(project.record_id,'a')});
  await store.commit({record_id:old.record_id,kind:'task',expected_revision:old.revision_id,data:handoff(project.record_id,'different-chat')});
  const scan=await sync.scan();assert.equal(scan.counts.blocked,1);assert.equal(scan.chats[0].error,'BINDING_CONFLICT');assert.equal(scan.scan_id,null);
});

test('missing anchor after a complete audit requires explicit human history review',async()=>{
  const {store,source,project,sync}=await setup();const first=await sync.scan();const task=handoff(project.record_id,'chat-a');
  await sync.save({scan_id:first.scan_id,session_id:'chat-a',task});
  source.snapshots.set('chat-a',snapshot('chat-a','replacement',{change_type:'anchor_missing',coverage:{complete_increment:false,complete_history:true,requires_review:true}}));
  const changed=await sync.scan({inspect_options:{fullAudit:true}});assert.equal(changed.counts.needs_history_review,1);assert.ok(changed.scan_id);
  await assert.rejects(sync.save({scan_id:changed.scan_id,session_id:'chat-a',task}),e=>e.code==='HISTORY_REVIEW_REQUIRED');
  const accepted=await sync.save({scan_id:changed.scan_id,session_id:'chat-a',task,history_reviewed_by_user:true});
  assert.equal(accepted.status,'cursor_saved');const cursor=(await store.get(chatIds('chat-a').cursor)).state.data;
  assert.equal(cursor.coverage.complete_increment,true);assert.equal(cursor.coverage.reviewed_history,true);
  source.snapshots.set('chat-a',snapshot('chat-a','replacement',{coverage:{complete_increment:true}}));
  assert.equal((await sync.scan()).counts.unchanged,1);
});

test('a missing legacy task blocks duplicate creation while complete known chats remain usable',async()=>{
  const {base,store,shared,project,source,sync}=await setup([thread('legacy-chat'),thread('known-chat')]);
  const legacy=await store.commit({kind:'task',data:handoff(project.record_id,'legacy-chat')});
  const known=await store.commit({kind:'task',data:handoff(project.record_id,'known-chat')});
  // Preserve the full original record outside shared storage, modeling incomplete
  // provider delivery without deleting any input or test observation.
  await fs.rename(path.join(shared,'records',legacy.record_id),path.join(base,'preserved-legacy-task'));
  const result=await sync.scan();const missing=result.chats.find(chat=>chat.session_id==='legacy-chat');
  assert.equal(missing.status,'blocked');assert.equal(missing.error,'SOURCE_IDENTITY_INCOMPLETE');
  assert.deepEqual(await revisions(shared,chatIds('legacy-chat').task),[]);
  assert.equal(result.chats.find(chat=>chat.session_id==='known-chat').status,'needs_summary');
  assert.deepEqual(source.inspections.map(item=>item.session),['known-chat']);
  const saved=await sync.save({scan_id:result.scan_id,session_id:'known-chat',task:handoff(project.record_id,'known-chat')});
  assert.equal(saved.record_id,known.record_id);assert.equal(saved.revision_id,known.revision_id);
});
