// Read-only adapter for Codex app-server protocol 0.162.1.
// No inference, resume, login, archive or native database mutations are requested.
import {spawn as spawnProcess} from 'node:child_process';
import {createHash} from 'node:crypto';
import path from 'node:path';

const READ_METHODS = new Set(['thread/list', 'thread/read', 'thread/turns/list']);
const SOURCE_KINDS = ['cli', 'vscode', 'appServer', 'exec', 'unknown'];
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const error = (code, message) => Object.assign(new Error(message), {code});
export const HISTORY_LIMITATION = 'The protocol has no complete-history revision token. A change before an unchanged last-turn anchor may require an explicit full rescan.';

// The executable must be supplied by the installer/user, never by a chat payload.
// Keep this separate from pure readers so tests never create child processes.
export async function connectCodexReadOnly({executable, codexHome, timeoutMs = 30000, maxMessageBytes = 64 * 1024 * 1024, spawn = spawnProcess} = {}) {
  if (!executable || !path.isAbsolute(executable)) throw error('CODEX_PATH_REQUIRED', 'Supply the absolute path to the installed Codex executable');
  if (codexHome && !path.isAbsolute(codexHome)) throw error('CODEX_HOME_INVALID', 'Codex home must be absolute');
  const environment = {};
  for (const name of ['SystemRoot','WINDIR','PATH','PATHEXT','USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','LOCALAPPDATA','APPDATA','TEMP','TMP','LANG']) {
    if (process.env[name] !== undefined) environment[name] = process.env[name];
  }
  if (codexHome) environment.CODEX_HOME = codexHome;
  const child = spawn(executable, ['app-server', '--listen', 'stdio://', '-c', 'analytics.enabled=false'], {
    stdio: ['pipe','pipe','pipe'], windowsHide: true, shell: false, env: environment
  });
  let sequence = 0, buffer = '', closed = false;
  const pending = new Map();
  const rejectAll = failure => {
    for (const {reject, timer} of pending.values()) {clearTimeout(timer); reject(failure);}
    pending.clear();
  };
  child.on('error', cause => rejectAll(error('CODEX_UNAVAILABLE', cause.message)));
  child.on('exit', () => {closed = true; rejectAll(error('CODEX_CLOSED', 'Read-only Codex app-server exited'));});
  // Drain diagnostics without retaining or returning possibly sensitive content.
  child.stderr.on('data', () => {});
  child.stdin.on('error', () => {});
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', chunk => {
    buffer += chunk;
    if (Buffer.byteLength(buffer, 'utf8') > maxMessageBytes) {
      rejectAll(error('CODEX_RESPONSE_TOO_LARGE', 'Codex response exceeds the configured limit'));
      child.kill(); return;
    }
    let boundary;
    while ((boundary = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, boundary).trim(); buffer = buffer.slice(boundary + 1);
      if (!line) continue;
      let message;
      try {message = JSON.parse(line);} catch {rejectAll(error('CODEX_PROTOCOL', 'Invalid app-server JSON')); child.kill(); return;}
      if (message.method && message.id !== undefined) {
        // Refuse approvals, authentication refresh, elicitation and all callbacks.
        child.stdin.write(JSON.stringify({jsonrpc:'2.0', id:message.id, error:{code:-32601, message:'Read-only client does not handle server requests'}}) + '\n');
      } else if (message.id !== undefined) {
        const waiter = pending.get(message.id);
        if (!waiter) continue;
        pending.delete(message.id); clearTimeout(waiter.timer);
        if (message.error) waiter.reject(error('CODEX_RPC_ERROR', String(message.error.message || 'Codex RPC error')));
        else waiter.resolve(message.result);
      }
    }
  });
  const send = (method, params) => new Promise((resolve, reject) => {
    if (closed) return reject(error('CODEX_CLOSED', 'Read-only Codex app-server is closed'));
    const id = ++sequence;
    const timer = setTimeout(() => {pending.delete(id); reject(error('CODEX_TIMEOUT', 'Read-only Codex request timed out'));}, timeoutMs);
    pending.set(id, {resolve, reject, timer});
    child.stdin.write(JSON.stringify({jsonrpc:'2.0', id, method, params}) + '\n');
  });
  const close = async () => {
    if (closed) return;
    child.stdin.end();
    await new Promise(resolve => {
      const timer = setTimeout(() => {if (!closed) child.kill(); resolve();}, 500);
      child.once('exit', () => {clearTimeout(timer); resolve();});
    });
    rejectAll(error('CODEX_CLOSED', 'Read-only Codex app-server closed'));
  };
  try {
    await send('initialize', {clientInfo:{name:'agent_workspace_readonly', title:'Agent Workspace read-only history', version:'0.3.0'}, capabilities:{experimentalApi:false}});
    child.stdin.write(JSON.stringify({jsonrpc:'2.0', method:'initialized', params:{}}) + '\n');
  } catch (failure) {await close(); throw failure;}
  return {
    request(method, params) {
      if (!READ_METHODS.has(method)) return Promise.reject(error('READ_ONLY_METHOD', 'This adapter permits only thread history reads'));
      return send(method, method === 'thread/list' ? {...params, useStateDbOnly:true} : params);
    },
    close
  };
}

function sourceKind(source) {return typeof source === 'string' ? source : source && typeof source === 'object' && Object.keys(source)[0] || 'unknown';}
function describeThread(thread, archived) {
  if (!thread || typeof thread.id !== 'string' || !thread.id) throw error('CODEX_PROTOCOL', 'Thread is missing its ID');
  return {
    id:thread.id,
    title:String(thread.name || thread.preview || thread.id).slice(0, 300),
    project_id:typeof thread.projectId === 'string' ? thread.projectId : null,
    updated_at:Number.isFinite(thread.updatedAt) ? thread.updatedAt : null,
    status:typeof thread.status === 'string' ? thread.status : thread.status?.type || 'unknown',
    archived,
    source_kind:sourceKind(thread.source)
  };
}

export async function listCodexThreads(rpc, {includeArchived = true, pageSize = 100, maxPages = 1000} = {}) {
  const records = new Map();
  let pages = 0;
  for (const archived of includeArchived ? [false, true] : [false]) {
    let cursor = null;
    const cursors = new Set();
    do {
      if (++pages > maxPages) throw error('CODEX_SCAN_LIMIT', 'Thread inventory exceeded its page limit; no complete inventory is available');
      const response = await rpc.request('thread/list', {cursor, limit:pageSize, sortKey:'updated_at', sortDirection:'desc', archived, sourceKinds:SOURCE_KINDS, useStateDbOnly:true});
      if (!Array.isArray(response?.data)) throw error('CODEX_PROTOCOL', 'Thread listing is not an array');
      for (const thread of response.data) {
        const kind = sourceKind(thread.source);
        if (thread.parentThreadId || kind.startsWith('subAgent') || thread.ephemeral) continue;
        const record = describeThread(thread, archived);
        if (records.has(record.id)) throw error('CODEX_INVENTORY_CHANGED', 'A thread appeared twice during inventory; rescan before saving');
        records.set(record.id, record);
      }
      cursor = response.nextCursor || null;
      if (cursor && cursors.has(cursor)) throw error('CODEX_PROTOCOL', 'Repeated thread inventory cursor');
      if (cursor) cursors.add(cursor);
    } while (cursor);
  }
  return {threads:[...records.values()], complete:true, pages, scope:'This local Codex installation, active and archived user threads; remote hosts and ChatGPT are separate sources.'};
}

// Only explicit public-facing message types are retained. Reasoning, tools,
// compaction internals, attachments, stderr and command output are never emitted.
// Legacy agentMessage entries without phase remain public assistant messages;
// their original logs cannot prove whether they were final or progress replies.
export function visibleTurn(turn) {
  if (!turn || typeof turn.id !== 'string' || !Array.isArray(turn.items)) throw error('CODEX_PROTOCOL', 'Turn has no stable ID or items');
  const messages = [];
  for (const item of turn.items) {
    if (item.type === 'userMessage' && Array.isArray(item.content)) {
      const text = item.content.filter(part => part.type === 'text' && typeof part.text === 'string').map(part => part.text).join('\n');
      if (text) messages.push({id:String(item.id || ''), role:'user', text});
    }
    if (item.type === 'agentMessage' && (item.phase === 'final_answer' || item.phase === null || item.phase === undefined) && typeof item.text === 'string') {
      if (item.text) messages.push({id:String(item.id || ''), role:'assistant', text:item.text, ...(item.phase !== 'final_answer' ? {legacy_unphased:true} : {})});
    }
  }
  return {id:turn.id, status:turn.status || 'unknown', messages};
}

function metadataHash(thread) {return hash({id:thread.id,title:thread.title,project_id:thread.project_id,archived:thread.archived});}
function makeCheckpoint(thread, newest) {
  return {version:1, thread_id:thread.id, last_turn_id:newest?.id || null, last_turn_hash:newest ? hash(newest) : null, last_turn_status:newest?.status || null, last_item_id:newest?.messages.at(-1)?.id || null, metadata_hash:metadataHash(thread)};
}
function limitedTurns(turns, maxCharacters) {
  let available = maxCharacters, truncated = false;
  const selected = [];
  // Preserve the most recent text if a single old turn contains huge content.
  for (let ti = turns.length - 1; ti >= 0; ti--) {
    const turn = turns[ti], messages = [];
    for (let mi = turn.messages.length - 1; mi >= 0; mi--) {
      const message = turn.messages[mi];
      if (available <= 0) {truncated = true; continue;}
      const text = message.text.length > available ? message.text.slice(-available) : message.text;
      if (text.length !== message.text.length) truncated = true;
      available -= text.length; messages.unshift({...message, text});
    }
    if (messages.length) selected.unshift({...turn, messages});
  }
  return {turns:selected, truncated};
}

// Reads the newest turn first; unchanged chats cost one turn, not full history.
// Changed chats are paged only as far back as the previously saved turn anchor.
export async function readCodexIncrement(rpc, thread, checkpoint = null, options = {}) {
  const {maxTurns = 500, maxCharacters = 100000, pageSize = 25} = options;
  const full_audit = options.fullAudit ?? options.full_audit ?? false;
  if (typeof full_audit !== 'boolean') throw error('CODEX_SCAN_LIMIT', 'fullAudit must be boolean');
  if (checkpoint && checkpoint.thread_id !== thread.id) throw error('CODEX_CHECKPOINT_MISMATCH', 'Checkpoint belongs to another thread');
  for (const [name,value] of Object.entries({maxTurns,maxCharacters,pageSize})) {
    if (!Number.isSafeInteger(value) || value < 1) throw error('CODEX_SCAN_LIMIT', name + ' must be a positive integer');
  }
  const first = await rpc.request('thread/turns/list', {threadId:thread.id, limit:1, sortDirection:'desc', itemsView:'full'});
  if (!Array.isArray(first?.data)) throw error('CODEX_PROTOCOL', 'Turn listing is not an array');
  if (first.data.length > 1) throw error('CODEX_PROTOCOL', 'Server exceeded the requested latest-turn page size');
  const latest = first.data.length ? visibleTurn(first.data[0]) : null;
  const nextCheckpoint = makeCheckpoint(thread, latest);
  const latestMatches = checkpoint && checkpoint.last_turn_id === nextCheckpoint.last_turn_id && checkpoint.last_turn_hash === nextCheckpoint.last_turn_hash && checkpoint.metadata_hash === nextCheckpoint.metadata_hash;
  if (latestMatches && !full_audit) {
    if (checkpoint.history_hash) nextCheckpoint.history_hash = checkpoint.history_hash;
    return {status:'unchanged', checkpoint:nextCheckpoint, turns:[], coverage:{complete_increment:true, turns_read:first.data.length, full_audit:false, limitation:HISTORY_LIMITATION}};
  }
  let cursor = first.nextCursor || null, count = first.data.length;
  const newestFirst = latest ? [latest] : [];
  let anchor = checkpoint?.last_turn_id ? newestFirst.find(turn => turn.id === checkpoint.last_turn_id) : null;
  const textSize = turn => turn.messages.reduce((total,message) => total + message.text.length, 0);
  let characters = latest ? textSize(latest) : 0;
  const cursors = new Set();
  const turnIds = new Set(newestFirst.map(turn=>turn.id));
  while (cursor && (full_audit || !anchor)) {
    if (count >= maxTurns || characters >= maxCharacters) break;
    if (cursors.has(cursor)) throw error('CODEX_PROTOCOL', 'Repeated turn cursor');
    cursors.add(cursor);
    const response = await rpc.request('thread/turns/list', {threadId:thread.id, cursor, limit:Math.min(pageSize,maxTurns-count), sortDirection:'desc', itemsView:'full'});
    if (!Array.isArray(response?.data)) throw error('CODEX_PROTOCOL', 'Turn listing is not an array');
    for (const raw of response.data) {
      if (count >= maxTurns) throw error('CODEX_PROTOCOL', 'Server exceeded the requested turn page size');
      const turn = visibleTurn(raw); count++;
      if (turnIds.has(turn.id)) throw error('CODEX_PROTOCOL', 'Repeated turn ID during history scan');
      turnIds.add(turn.id);
      if (checkpoint?.last_turn_id === turn.id) {anchor = turn; if (!full_audit) break;}
      newestFirst.push(turn);
      characters += textSize(turn);
    }
    cursor = response.nextCursor || null;
  }
  const anchorChanged = !!anchor && hash(anchor) !== checkpoint?.last_turn_hash;
  // Include the anchor only if its public text/status changed during a continued turn.
  if (anchorChanged && !newestFirst.some(turn => turn.id === anchor.id)) newestFirst.push(anchor);
  if (anchor && !anchorChanged && !full_audit) {
    const index = newestFirst.findIndex(turn => turn.id === anchor.id);
    if (index >= 0) newestFirst.splice(index,1);
  }
  const ordered = newestFirst.reverse();
  const limited = limitedTurns(ordered,maxCharacters);
  const missingAnchor = !!checkpoint?.last_turn_id && !anchor;
  const completeHistory = !cursor && !limited.truncated;
  const complete = !limited.truncated && (full_audit ? !cursor : !cursor || !!anchor) && !missingAnchor && !anchorChanged;
  let status = anchorChanged ? 'anchor_changed' : missingAnchor ? 'anchor_missing' : !complete ? 'partial' : 'changed';
  if (full_audit && completeHistory) {
    nextCheckpoint.history_hash = hash(ordered);
    if (latestMatches && checkpoint.history_hash === nextCheckpoint.history_hash) status = 'unchanged';
    else if (latestMatches && checkpoint.history_hash) status = 'history_changed';
  }
  return {
    status, checkpoint:nextCheckpoint, turns:status === 'unchanged' ? [] : limited.turns,
    coverage:{complete_increment:complete && status!=='history_changed', complete_history:full_audit ? completeHistory : undefined, full_audit, initial_import:!checkpoint, anchor_found:!!anchor, requires_review:anchorChanged || missingAnchor || status==='history_changed', truncated:limited.truncated, bounded:!!cursor && (full_audit || !anchor), legacy_unphased_assistant:ordered.some(turn=>turn.messages.some(message=>message.legacy_unphased)), turns_read:count, older_cursor:cursor, limitation:HISTORY_LIMITATION}
  };
}

// Adapter consumed by the shared-workspace synchronizer. Native thread metadata
// is cached only for one inventory run, never treated as a remote version token.
export function createCodexChatSource({rpc}) {
  const inventory = new Map();
  return {
    async enumerate(options) {
      const result = await listCodexThreads(rpc, options);
      inventory.clear();
      for (const thread of result.threads) inventory.set(thread.id, thread);
      return result;
    },
    async inspect(threadId, checkpoint = null, options = {}) {
      let thread = options.thread_metadata || inventory.get(threadId);
      if (thread && (thread.id !== threadId || typeof thread.title !== 'string' || typeof thread.archived !== 'boolean' || !(thread.project_id === null || thread.project_id === undefined || typeof thread.project_id === 'string'))) {
        throw error('CODEX_METADATA_MISMATCH', 'Supplied thread metadata does not match the requested chat');
      }
      if (!thread) {
        const response = await rpc.request('thread/read', {threadId, includeTurns:false});
        thread = describeThread(response.thread, options.archived === true);
      }
      const result = await readCodexIncrement(rpc, thread, checkpoint, options);
      const publicMessages = result.turns.flatMap(turn => turn.messages.map(message => ({role:message.role,text:message.text,turn_id:turn.id,item_id:message.id, ...(message.legacy_unphased ? {legacy_unphased:true} : {})})));
      return {
        unchanged:result.status === 'unchanged',
        snapshot:{
          session_id:thread.id, title:thread.title, project_id:thread.project_id,
          archived:thread.archived,
          active:thread.status === 'active' || result.checkpoint.last_turn_status === 'inProgress',
          activity_scope:'Persisted turn status and this read-only process metadata; another running Codex client can advance after the snapshot.',
          latest_turn_id:result.checkpoint.last_turn_id,
          last_item_id:result.checkpoint.last_item_id,
          fingerprint:hash(result.checkpoint), public_messages:publicMessages,
          coverage:result.coverage, change_type:result.status, checkpoint:result.checkpoint
        }
      };
    }
  };
}
