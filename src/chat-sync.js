// Chat history stays local. Only an agent-authored handoff and a content cursor
// enter the shared workspace; native messages are never published by this module.
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {hash, readJson, atomicJson} from './store.js';
import {id, taskSchema} from './schema.js';

const fail = (code, message) => Object.assign(new Error(message), {code});
const LOCAL_LIMIT = 20 * 1024 * 1024;
const canonical = value => JSON.stringify(value, (_key,item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map(key=>[key,item[key]])) : item);
const digest = value => hash(canonical(value));
async function saveLocal(file,value) {
  if(Buffer.byteLength(JSON.stringify(value),'utf8')>LOCAL_LIMIT)throw fail('SCAN_TOO_LARGE','Local scan exceeds 20 MiB; use smaller batches');
  await atomicJson(file,value);
}
const optionalJson = file => readJson(file,LOCAL_LIMIT).catch(e=>{if(e.code==='ENOENT')return null;throw e;});
export const chatIds = session => ({task: 'chat-' + hash(['codex',session]).slice(0,48), cursor: 'cursor-' + hash(['codex',session]).slice(0,48)});

// Defense in depth for the LOCAL preview sent to an agent. This does not certify
// arbitrary prose as secret-free; the author must omit credentials from handoffs.
export function redactPreview(text) {
  return String(text).replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----/g,'[PRIVATE KEY OMITTED]')
    .replace(/\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/g,'[SECRET OMITTED]')
    .replace(/\b(Bearer\s+)[A-Za-z0-9_.~+\/-]+/gi,'$1[SECRET OMITTED]')
    .replace(/((?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret|пароль)\s*[=:]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)/gi,'$1[SECRET OMITTED]');
}

export class ChatSync {
  constructor(store, source) { this.store=store; this.source=source; }
  async catalog(startup=null,ctx=null) {
    startup ||= await this.store.startup({includeChatIndex:true});
    if (!['ready','choice_required','sync_incomplete'].includes(startup.status)) throw fail('WORKSPACE_UNAVAILABLE',startup.status);
    ctx ||= await this.store.context();
    if(!Array.isArray(startup.chat_index))throw fail('CHAT_INDEX_UNAVAILABLE','This runtime must supply a verified chat index');
    const tasks=new Map(),cursors=new Map(),records=new Map(startup.records.map(row=>[row.record_id,row]));
    const indexedRecords=new Set(startup.chat_index.map(item=>item.record_id));
    // A missing or unreadable handoff may already own a chat under a legacy,
    // nondeterministic ID. Its unknown identity must not create a duplicate task.
    const unknownAcceptedIdentity=startup.records.some(record=>record.status==='sync_incomplete'
      &&(!record.kind||(['task','profile'].includes(record.kind)&&!indexedRecords.has(record.record_id))));
    // Startup already validated all revisions and candidate identities. Reuse that
    // pass instead of rereading every handoff and large result object a second time.
    for(const indexed of startup.chat_index) {
      const record=records.get(indexed.record_id);
      if(!record)throw fail('INVALID_CHAT_INDEX','Chat index refers to an unknown record');
      for(const session of indexed.source_sessions) {
        const target=indexed.is_cursor?cursors:tasks;
        const values=target.get(session)||[];
        values.push({record,accepted_data:indexed.accepted_data}); target.set(session,values);
      }
    }
    return {startup,ctx,tasks,cursors,unknownAcceptedIdentity};
  }
  async scan({run_id,offset=0,limit=10,current_session_id,active_session_ids=[],bindings=[],inspect_options={}}={}) {
    if(!Number.isSafeInteger(offset)||offset<0||!Number.isSafeInteger(limit)||limit<1||limit>50)throw fail('INVALID_RANGE','Use offset >= 0 and limit 1..50');
    let catalog,inventory,run;
    if(run_id) {
      id.parse(run_id);
      const ctx=await this.store.context();
      run=await readJson(path.join(ctx.localDir,'chat-runs',run_id+'.json'),LOCAL_LIMIT);
      if(run.workspace_id!==ctx.workspace_id||run.run_id!==run_id)throw fail('WORKSPACE_CHANGED','Inventory belongs to another workspace');
      if(bindings.length&&canonical(bindings)!==canonical(run.bindings))throw fail('RUN_OPTIONS_CHANGED','Bindings are fixed for this inventory run');
      catalog=await this.catalog(run.startup,ctx);inventory=run.inventory;
      current_session_id ||= run.current_session_id;
      active_session_ids=[...new Set([...(run.active_session_ids||[]),...active_session_ids])];
      inspect_options={...run.inspect_options,...inspect_options};bindings=run.bindings;
    } else {
      if(offset!==0)throw fail('RUN_REQUIRED','Continue pagination with the returned run_id');
      catalog=await this.catalog();inventory=await this.source.enumerate();
      run_id=randomUUID();
      run={version:1,workspace_id:catalog.ctx.workspace_id,run_id,startup:catalog.startup,inventory,bindings,current_session_id,active_session_ids,inspect_options};
    }
    const entries=[],previews=[],bindingsMap=new Map(bindings.map(b=>[b.session_id,b]));
    if(bindingsMap.size!==bindings.length)throw fail('BINDING_CONFLICT','A chat has more than one explicit binding');
    const activeSessions=new Set(active_session_ids);
    const threads=inventory.threads.slice(offset,offset+limit);
    for(const thread of threads) {
      const session=thread.id, ids=chatIds(session);
      try {
        const matches=catalog.tasks.get(session)||[], cursorMatches=catalog.cursors.get(session)||[];
        if(matches.length>1||cursorMatches.length>1)throw fail('AMBIGUOUS_CHAT','Multiple workspace records refer to this chat; review them first');
        const binding=bindingsMap.get(session);
        let match=matches[0];
        if(binding) {
          id.parse(binding.record_id);
          if(match && match.record.record_id!==binding.record_id)throw fail('BINDING_CONFLICT','Chat already belongs to another task');
          const row=catalog.startup.records.find(r=>r.record_id===binding.record_id);
          if(!row||row.kind!=='task')throw fail('INVALID_BINDING','Binding must refer to an existing task');
          const indexed=catalog.startup.chat_index.find(item=>item.record_id===row.record_id);
          const acceptedData=indexed?.accepted_data||(await this.store.get(row.record_id,row.selected_revision)).state.data;
          if(acceptedData.source_session && (acceptedData.source_session.provider!=='codex'||acceptedData.source_session.session_id!==session))throw fail('BINDING_CONFLICT','Task belongs to another source session');
          if(binding.expected_revision!==undefined&&binding.expected_revision!==row.selected_revision)throw fail('STALE_BASE','Binding base changed');
          match={record:row,accepted_data:acceptedData};
        }
        const cursorMatch=cursorMatches[0];
        if(!match&&!cursorMatch&&catalog.unknownAcceptedIdentity)throw fail('SOURCE_IDENTITY_INCOMPLETE','An incomplete workspace record has an unknown chat identity; restore its delivery before creating an unbound task');
        for(const item of [match,cursorMatch].filter(Boolean))if(!['current','loaded'].includes(item.record.status))throw fail(item.record.status==='choice_required'?'CHOICE_REQUIRED':'SYNC_INCOMPLETE','Review this chat record before saving');
        if(match?.accepted_data?.source_session && (match.accepted_data.source_session.provider!=='codex'||match.accepted_data.source_session.session_id!==session))throw fail('BINDING_CONFLICT','Accepted task has another source identity');
        if(cursorMatch && (!match||cursorMatch.accepted_data?.type!=='codex-chat-cursor-v1'||cursorMatch.accepted_data.session_id!==session||cursorMatch.accepted_data.task_record_id!==match.record.record_id))throw fail('BINDING_CONFLICT','Cursor and task disagree');
        const cursorData=cursorMatch?.accepted_data;
        if(session!==current_session_id&&(activeSessions.has(session)||thread.status==='active')) {previews.push({session_id:session,title:thread.title,status:'active_pending'});continue;}
        const result=await this.source.inspect(session,cursorData?.checkpoint,{...inspect_options,thread_metadata:thread});
        const snapshot=result.snapshot;
        if(!snapshot||snapshot.session_id!==session||!/^[a-f0-9]{64}$/.test(snapshot.fingerprint))throw fail('INVALID_SNAPSHOT','Invalid source snapshot identity or fingerprint');
        const recordId=match?.record.record_id||cursorData?.task_record_id||ids.task;
        // An unbound deterministic record may have arrived incompletely; never
        // silently replace it with a new record or omit expected_revision.
        for(const candidateId of [recordId,ids.cursor]) {
          const row=catalog.startup.records.find(r=>r.record_id===candidateId);
          if(row && ![match?.record.record_id,cursorMatch?.record.record_id].includes(candidateId))throw fail('BINDING_CONFLICT','Existing record needs an explicit identity review');
        }
        if(snapshot.active && session!==current_session_id) {previews.push({session_id:session,title:thread.title,status:'active_pending'});continue;}
        const incomplete=snapshot.coverage?.complete_increment!==true
          ||(cursorData&&cursorData.coverage?.complete_increment!==true&&snapshot.coverage?.complete_history!==true);
        const historyReview=snapshot.coverage?.complete_history===true&&snapshot.coverage?.requires_review===true;
        if(incomplete&&!historyReview) {
          previews.push({session_id:session,title:thread.title,status:'pending_history',record_id:recordId,coverage:snapshot.coverage,change_type:snapshot.change_type,error:'PENDING_HISTORY',message:'History coverage is incomplete; increase read limits or request a full audit before saving.',public_messages:(snapshot.public_messages||[]).map(m=>({...m,text:redactPreview(m.text)}))});
          continue;
        }
        if(!historyReview && result.unchanged && cursorData?.coverage?.complete_increment===true && cursorData.fingerprint===snapshot.fingerprint && match) {previews.push({session_id:session,title:thread.title,status:'unchanged',record_id:recordId,revision_id:match.record.selected_revision});continue;}
        const entry={session_id:session,record_id:recordId,expected_revision:match?.record.selected_revision||null,cursor_record_id:cursorMatch?.record.record_id||ids.cursor,cursor_revision:cursorMatch?.record.selected_revision||null,snapshot,history_review_required:historyReview,existing_task:match?.accepted_data||null};
        entries.push(entry);
        previews.push({session_id:session,title:thread.title,status:historyReview?'needs_history_review':'needs_summary',record_id:recordId,expected_revision:entry.expected_revision,coverage:snapshot.coverage,change_type:snapshot.change_type,latest_turn_id:snapshot.latest_turn_id,public_messages:snapshot.public_messages.map(m=>({...m,text:redactPreview(m.text)})),existing_task:entry.existing_task});
      }catch(e){previews.push({session_id:session,title:thread.title,status:'blocked',error:e.code||'SOURCE_READ_FAILED',message:e.message});}
    }
    const nextOffset=offset+threads.length<inventory.threads.length?offset+threads.length:null;
    if(!run.persisted&&(nextOffset!==null||entries.length)) {
      run.persisted=true;await saveLocal(path.join(catalog.ctx.localDir,'chat-runs',run_id+'.json'),run);
    }
    // Unchanged, active and incomplete-only pages do not create empty scan files.
    const scanId=entries.length?randomUUID():null;
    if(scanId)await saveLocal(path.join(catalog.ctx.localDir,'chat-scans',scanId+'.json'),{version:1,workspace_id:catalog.ctx.workspace_id,scan_id:scanId,entries});
    return {scan_id:scanId,run_id:run.persisted?run_id:null,total:inventory.threads.length,offset,next_offset:nextOffset,complete_inventory:inventory.complete,scope:inventory.scope,chats:previews,counts:Object.fromEntries(['needs_summary','needs_history_review','unchanged','active_pending','pending_history','blocked'].map(s=>[s,previews.filter(p=>p.status===s).length])),limitation:'Newest turn and saved anchor are checked. Older edits before an unchanged anchor require an explicit full audit. Native transcripts remain local.'};
  }
  async save({scan_id,session_id,task,history_reviewed_by_user=false}) {
    id.parse(scan_id);
    return this.store.locked(async()=>{
    const ctx=await this.store.context();
    const token=await readJson(path.join(ctx.localDir,'chat-scans',scan_id+'.json'),LOCAL_LIMIT);
    if(token.workspace_id!==ctx.workspace_id)throw fail('WORKSPACE_CHANGED','Scan belongs to another workspace');
    const entry=token.entries.find(e=>e.session_id===session_id);
    if(!entry)throw fail('INVALID_SCAN','Chat is not a changed item of this scan');
    const historyReview=entry.snapshot.coverage?.complete_history===true&&entry.snapshot.coverage?.requires_review===true;
    if(historyReview&&history_reviewed_by_user!==true)throw fail('HISTORY_REVIEW_REQUIRED','Show the complete history summary and obtain explicit human approval before replacing its anchor');
    if(entry.snapshot.coverage?.complete_increment!==true&&!(historyReview&&history_reviewed_by_user===true))throw fail('PENDING_HISTORY','Incomplete source history cannot advance the saved cursor');
    task=taskSchema.parse(task);
    if(task.source_session?.provider!=='codex'||task.source_session.session_id!==session_id)throw fail('SOURCE_MISMATCH','Handoff must retain the scanned source identity');
    if(entry.existing_task && task.project_id!==entry.existing_task.project_id)throw fail('PROJECT_CHANGED','Do not move an existing task while saving a chat checkpoint');
    // Do not replace a rich existing handoff with generic machine extraction.
    // The caller must write a real summary and retain the original evidence.
    const taskDigest=digest(task),receiptPath=path.join(ctx.localDir,'chat-scans',scan_id+'-receipt-'+hash(session_id).slice(0,48)+'.json');
    const receipt=await optionalJson(receiptPath);
    if(receipt&&(receipt.workspace_id!==ctx.workspace_id||receipt.session_id!==session_id||receipt.task_digest!==taskDigest))throw fail('RETRY_CONTENT_CHANGED','Retry must submit the identical normalized handoff');
    const cursorScan=await this.store.scan(ctx,entry.cursor_record_id),cursorSelected=ctx.selected[entry.cursor_record_id]||null;
    if(cursorScan.problems.length)throw fail('SYNC_INCOMPLETE','Incomplete cursor delivery');
    if(cursorSelected&&!cursorScan.valid.has(cursorSelected))throw fail('SYNC_INCOMPLETE','Accepted cursor revision is unavailable');
    if(cursorScan.valid.size&&(cursorScan.heads.length!==1||cursorScan.heads[0]!==cursorSelected))throw fail('CHOICE_REQUIRED','Review cursor branches first');
    let saved=receipt?.task_saved;
    if(saved) {
      if(saved.record_id!==entry.record_id)throw fail('INVALID_RECEIPT','Receipt belongs to another task');
      const taskScan=await this.store.scan(ctx,entry.record_id);
      if(taskScan.problems.length)throw fail('SYNC_INCOMPLETE','Incomplete saved task delivery');
      if((ctx.selected[entry.record_id]||null)!==saved.revision_id)throw fail('STALE_BASE','Task changed after the partial save');
      if(taskScan.heads.length!==1||taskScan.heads[0]!==saved.revision_id)throw fail('CHOICE_REQUIRED','Review task branches before retrying');
      if(digest(taskScan.valid.get(saved.revision_id)?.state.data)!==taskDigest)throw fail('INVALID_RECEIPT','Saved task content no longer matches the receipt');
    } else {
      if(cursorSelected!==entry.cursor_revision)throw fail('STALE_BASE','Chat cursor changed since the scan');
      saved=await this.store._commit(ctx,{record_id:entry.record_id,kind:'task',data:task,expected_revision:entry.expected_revision});
      const readback=await this.store.loadRevision(ctx,saved.record_id,saved.revision_id);
      if(readback.state.revision_id!==saved.revision_id||digest(readback.state.data)!==taskDigest)throw fail('READBACK_FAILED','Saved handoff was not read back');
      await saveLocal(receiptPath,{version:1,workspace_id:ctx.workspace_id,session_id,task_digest:taskDigest,task_saved:saved,history_reviewed_by_user:historyReview});
    }
    const acceptedCoverage=historyReview?{...entry.snapshot.coverage,complete_increment:true,reviewed_history:true}:entry.snapshot.coverage;
    const cursorData={type:'codex-chat-cursor-v1',provider:'codex',session_id,task_record_id:saved.record_id,task_revision:saved.revision_id,fingerprint:entry.snapshot.fingerprint,checkpoint:entry.snapshot.checkpoint,coverage:acceptedCoverage};
    let cursor,unchanged=false;
    if(cursorSelected&&canonical(cursorScan.valid.get(cursorSelected)?.state.data)===canonical(cursorData)) {
      // Handles a retry after cursor publication succeeded but its receipt write
      // or response failed. No blind revision substitution and no extra files.
      cursor={record_id:entry.cursor_record_id,revision_id:cursorSelected};unchanged=true;
    } else {
      if(cursorSelected!==entry.cursor_revision)throw fail('STALE_BASE','Chat cursor changed after the partial save');
      cursor=await this.store._commit(ctx,{record_id:entry.cursor_record_id,kind:'profile',expected_revision:entry.cursor_revision,data:cursorData});
    }
    const cursorReadback=await this.store.loadRevision(ctx,cursor.record_id,cursor.revision_id);
    if(cursorReadback.state.revision_id!==cursor.revision_id||canonical(cursorReadback.state.data)!==canonical(cursorData))throw fail('READBACK_FAILED','Saved cursor was not read back');
    if(!receipt?.cursor_saved)await saveLocal(receiptPath,{version:1,workspace_id:ctx.workspace_id,session_id,task_digest:taskDigest,task_saved:saved,cursor_saved:cursor,history_reviewed_by_user:historyReview});
    return {...saved,cursor_revision:cursor.revision_id,source_boundary:entry.snapshot.latest_turn_id,status:unchanged?'unchanged':saved.status==='unchanged'?'cursor_saved':'saved',freshness:'Snapshot boundary saved. Later chat messages remain pending; NAS delivery is not asserted.'};
    });
  }
}
