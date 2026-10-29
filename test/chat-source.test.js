import test from 'node:test';
import assert from 'node:assert/strict';
import {listCodexThreads, readCodexIncrement, visibleTurn, createCodexChatSource} from '../src/chat-source.js';

const thread = {id:'chat-a',title:'Example',project_id:'project-a',status:'idle',archived:false};
const turn = (id,text='answer') => ({id,status:'completed',items:[{id:id+'-user',type:'userMessage',content:[{type:'text',text:'Question'}]},{id:id+'-assistant',type:'agentMessage',phase:'final_answer',text}]});
const fake = responses => ({calls:[], async request(method,params) {this.calls.push({method,params}); const next=responses.shift(); assert.ok(next,'Unexpected RPC request'); return next;}});

test('Codex inventory pages active and archived user threads without subagents', async () => {
  const rpc=fake([
    {data:[{id:'a',name:'A',source:'appServer',status:{type:'idle'},updatedAt:1}],nextCursor:'next'},
    {data:[{id:'child',name:'Child',source:{subAgent:{}},parentThreadId:'a'},{id:'b',preview:'B',source:'cli'}],nextCursor:null},
    {data:[{id:'c',name:'C',source:'vscode'}],nextCursor:null}
  ]);
  const result=await listCodexThreads(rpc);
  assert.deepEqual(result.threads.map(t=>[t.id,t.archived]),[['a',false],['b',false],['c',true]]);
  assert.equal(result.complete,true);
  assert.ok(rpc.calls.every(call=>call.params.useStateDbOnly===true));
  assert.equal(rpc.calls[2].params.archived,true);
});

test('visible history excludes reasoning, tools, commentary and images', () => {
  const source=turn('t');
  source.items.push({id:'r',type:'reasoning',summary:['hidden']},{id:'x',type:'commandExecution',aggregatedOutput:'secret'},{id:'c',type:'agentMessage',phase:'commentary',text:'progress'});
  source.items[0].content.push({type:'image',url:'data:secret'});
  const result=visibleTurn(source);
  assert.deepEqual(result.messages.map(m=>m.text),['Question','answer']);
  assert.equal(JSON.stringify(result).includes('secret'),false);
});

test('unchanged chat reads exactly one latest turn, ignoring updated_at noise', async () => {
  const initial=await readCodexIncrement(fake([{data:[turn('t')],nextCursor:null}]),thread);
  const rpc=fake([{data:[turn('t')],nextCursor:'older'}]);
  const unchanged=await readCodexIncrement(rpc,{...thread,updated_at:500},initial.checkpoint);
  assert.equal(unchanged.status,'unchanged');
  assert.deepEqual(unchanged.turns,[]);
  assert.equal(rpc.calls.length,1);
  assert.equal(rpc.calls[0].params.limit,1);
});

test('increment reads only newer turns through a matching saved anchor', async () => {
  const initial=await readCodexIncrement(fake([{data:[turn('old')],nextCursor:null}]),thread);
  const rpc=fake([{data:[turn('new')],nextCursor:'page2'},{data:[turn('middle'),turn('old'),turn('older')],nextCursor:'page3'}]);
  const increment=await readCodexIncrement(rpc,thread,initial.checkpoint);
  assert.equal(increment.status,'changed');
  assert.equal(increment.coverage.complete_increment,true);
  assert.deepEqual(increment.turns.map(t=>t.id),['middle','new']);
  assert.equal(rpc.calls.length,2);
});

test('continued last turn is detected by its hash and retained', async () => {
  const initial=await readCodexIncrement(fake([{data:[turn('t','before')],nextCursor:null}]),thread);
  const increment=await readCodexIncrement(fake([{data:[turn('t','after')],nextCursor:'older'}]),thread,initial.checkpoint);
  assert.equal(increment.status,'anchor_changed');
  assert.equal(increment.turns[0].messages.at(-1).text,'after');
  assert.equal(increment.coverage.complete_increment,false);
  assert.equal(increment.coverage.requires_review,true);
});

test('missing anchor and bounded initial imports are never reported as complete', async () => {
  const initial=await readCodexIncrement(fake([{data:[turn('t')],nextCursor:null}]),thread);
  const missing=await readCodexIncrement(fake([{data:[turn('different')],nextCursor:null}]),thread,initial.checkpoint);
  assert.equal(missing.status,'anchor_missing');
  assert.equal(missing.coverage.complete_increment,false);
  const partial=await readCodexIncrement(fake([{data:[turn('new','long answer')],nextCursor:'older'}]),thread,null,{maxTurns:1,maxCharacters:4});
  assert.equal(partial.status,'partial');
  assert.equal(partial.coverage.truncated,true);
  assert.equal(partial.coverage.complete_increment,false);
});

test('metadata changes are detected without rereading older turns', async () => {
  const initial=await readCodexIncrement(fake([{data:[turn('t')],nextCursor:null}]),thread);
  const rpc=fake([{data:[turn('t')],nextCursor:'older'}]);
  const changed=await readCodexIncrement(rpc,{...thread,title:'Renamed'},initial.checkpoint);
  assert.equal(changed.status,'changed');
  assert.equal(changed.coverage.complete_increment,true);
  assert.deepEqual(changed.turns,[]);
  assert.equal(rpc.calls.length,1);
});

test('source interface enumerates and inspects using the same metadata snapshot', async () => {
  const rpc=fake([
    {data:[{id:'a',name:'Chat A',source:'appServer',status:{type:'notLoaded'}}],nextCursor:null},
    {data:[],nextCursor:null},
    {data:[turn('t')],nextCursor:null}
  ]);
  const source=createCodexChatSource({rpc});
  assert.equal((await source.enumerate()).threads.length,1);
  const inspected=await source.inspect('a');
  assert.equal(inspected.unchanged,false);
  assert.equal(inspected.snapshot.session_id,'a');
  assert.equal(inspected.snapshot.title,'Chat A');
  assert.equal(inspected.snapshot.latest_turn_id,'t');
  assert.equal(inspected.snapshot.last_item_id,'t-assistant');
  assert.deepEqual(inspected.snapshot.public_messages.map(message=>message.role),['user','assistant']);
  assert.match(inspected.snapshot.fingerprint,/^[a-f0-9]{64}$/);
  assert.equal(rpc.calls.length,3);
});

test('full audit traverses an unchanged newest anchor and detects an older edit', async () => {
  const baseline=await readCodexIncrement(fake([
    {data:[turn('new')],nextCursor:'older'},
    {data:[turn('old','before')],nextCursor:null}
  ]),thread,null,{full_audit:true});
  assert.equal(baseline.coverage.complete_history,true);
  assert.match(baseline.checkpoint.history_hash,/^[a-f0-9]{64}$/);
  const unchangedRpc=fake([
    {data:[turn('new')],nextCursor:'older'},
    {data:[turn('old','before')],nextCursor:null}
  ]);
  const unchanged=await readCodexIncrement(unchangedRpc,thread,baseline.checkpoint,{full_audit:true});
  assert.equal(unchanged.status,'unchanged');
  assert.deepEqual(unchanged.turns,[]);
  assert.equal(unchangedRpc.calls.length,2);
  const changed=await readCodexIncrement(fake([
    {data:[turn('new')],nextCursor:'older'},
    {data:[turn('old','after')],nextCursor:null}
  ]),thread,baseline.checkpoint,{full_audit:true});
  assert.equal(changed.status,'history_changed');
  assert.equal(changed.coverage.complete_history,true);
  assert.equal(changed.coverage.requires_review,true);
  assert.equal(changed.coverage.complete_increment,false);
  assert.equal(changed.turns[0].messages.at(-1).text,'after');
});

test('empty chat checkpoints are stable and a rollback to empty requires review', async () => {
  const initial=await readCodexIncrement(fake([{data:[],nextCursor:null}]),thread);
  assert.equal(initial.status,'changed');
  assert.equal(initial.checkpoint.last_turn_id,null);
  const second=await readCodexIncrement(fake([{data:[],nextCursor:null}]),thread,initial.checkpoint);
  assert.equal(second.status,'unchanged');
  const populated=await readCodexIncrement(fake([{data:[turn('old')],nextCursor:null}]),thread);
  const rollback=await readCodexIncrement(fake([{data:[],nextCursor:null}]),thread,populated.checkpoint);
  assert.equal(rollback.status,'anchor_missing');
  assert.equal(rollback.coverage.requires_review,true);
  assert.equal(rollback.coverage.complete_increment,false);
});

test('large latest turn reaches character limit without requesting older pages', async () => {
  const rpc=fake([{data:[turn('large','a'.repeat(100))],nextCursor:'older'}]);
  const partial=await readCodexIncrement(rpc,thread,null,{maxTurns:30,maxCharacters:24});
  assert.equal(partial.status,'partial');
  assert.equal(partial.coverage.bounded,true);
  assert.equal(partial.coverage.truncated,true);
  assert.equal(rpc.calls.length,1);
  assert.equal(partial.turns.flatMap(t=>t.messages).reduce((n,m)=>n+m.text.length,0),24);
});

test('legacy assistant phases are identified instead of asserted to be finals', () => {
  const legacy={id:'legacy',status:'completed',items:[{id:'public',type:'agentMessage',phase:null,text:'Old public reply'}]};
  const result=visibleTurn(legacy);
  assert.equal(result.messages[0].legacy_unphased,true);
  assert.equal(result.messages[0].text,'Old public reply');
});

test('saved inventory metadata avoids another listing or metadata read and retains archive flag', async () => {
  const rpc=fake([{data:[turn('t')],nextCursor:null}]);
  const source=createCodexChatSource({rpc});
  const metadata={...thread,archived:true};
  const result=await source.inspect(thread.id,null,{thread_metadata:metadata,fullAudit:true});
  assert.equal(result.snapshot.archived,true);
  assert.equal(result.snapshot.coverage.full_audit,true);
  assert.equal(result.snapshot.coverage.complete_history,true);
  assert.deepEqual(rpc.calls.map(call=>call.method),['thread/turns/list']);
  await assert.rejects(()=>source.inspect(thread.id,null,{thread_metadata:{...metadata,id:'other'}}),{code:'CODEX_METADATA_MISMATCH'});
});
