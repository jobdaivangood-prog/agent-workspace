import * as fs from 'node:fs/promises';
import { constants, createReadStream } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { randomUUID, createHash } from 'node:crypto';
import { pipeline } from 'node:stream/promises';
import { Transform } from 'node:stream';
import { createWriteStream } from 'node:fs';
import { VERSION, SCHEMA_VERSION, id, validatePayload, kindSchema } from './schema.js';

export const hash = value => createHash('sha256').update(typeof value === 'string' || Buffer.isBuffer(value) ? value : JSON.stringify(value)).digest('hex');
const json = value => JSON.stringify(value, null, 2) + '\n';
const canonicalJson = value => JSON.stringify(value, (_key, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
const canonicalArtifacts = artifacts => artifacts.map(canonicalJson).sort();
const fail = (code, message) => Object.assign(new Error(message), {code});
export const defaultLocal = () => path.join(process.env.LOCALAPPDATA || process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local', 'state'), 'AgentWorkspace');
const exists = async p => !!(await fs.lstat(p).catch(e => { if (e.code === 'ENOENT') return null; throw e; }));
const inside = (root, target) => { const r = path.relative(root, target); return r === '' || (!r.startsWith('..' + path.sep) && r !== '..' && !path.isAbsolute(r)); };
// Check every existing component, including ancestors of the supplied root. A lexical
// containment check alone does not protect against directory symlinks or junctions.
// These checks reduce accidental redirection; they are not a filesystem transaction
// against another process replacing a directory between a check and an operation.
export async function safePath(target, {root, allowMissing = false, type} = {}) {
  const absolute = path.resolve(target), boundary = root === undefined ? null : path.resolve(root);
  if (boundary && !inside(boundary, absolute)) throw fail('PATH_ESCAPE', 'Path is outside its expected root');
  const volume = path.parse(absolute).root;
  const components = [volume, ...path.relative(volume, absolute).split(path.sep).filter(Boolean)];
  let current = '', realBoundary, stat, real;
  for (let i = 0; i < components.length; i++) {
    current = i === 0 ? components[i] : path.join(current, components[i]);
    stat = await fs.lstat(current).catch(e => {if (e.code === 'ENOENT' && allowMissing) return null; throw e;});
    if (!stat) return null;
    if (stat.isSymbolicLink()) throw fail('PATH_ESCAPE', 'Symlinks and junctions are not allowed in a storage path: ' + current);
    if ((i < components.length - 1 || type === 'directory' || (boundary && path.relative(boundary, current) === '')) && !stat.isDirectory()) throw fail('INVALID_DIRECTORY', 'Expected a directory: ' + current);
    if (i === components.length - 1 && type === 'file' && !stat.isFile()) throw fail('INVALID_FILE', 'Expected a regular file: ' + current);
    real = await fs.realpath(current);
    if (boundary && path.relative(boundary, current) === '') realBoundary = real;
    if (boundary && inside(boundary, current) && (!realBoundary || !inside(realBoundary, real))) throw fail('PATH_ESCAPE', 'Path resolves outside its expected root');
  }
  return {path: absolute, realPath: real, stat};
}
export async function ensureDirectory(directory, root) {
  const target = path.resolve(directory), boundary = root === undefined ? path.parse(target).root : path.resolve(root);
  if (!inside(boundary, target)) throw fail('PATH_ESCAPE', 'Directory is outside its expected root');
  await safePath(boundary, {type: 'directory'});
  let current = boundary;
  for (const part of path.relative(boundary, target).split(path.sep).filter(Boolean)) {
    await safePath(current, {root: boundary, type: 'directory'});
    current = path.join(current, part);
    if (!(await safePath(current, {root: boundary, allowMissing: true, type: 'directory'}))) {
      await fs.mkdir(current).catch(e => {if (e.code !== 'EEXIST') throw e;});
    }
    await safePath(current, {root: boundary, type: 'directory'});
  }
  return target;
}
export async function prospectiveDirectory(directory) {
  let current = path.resolve(directory);
  const missing = [];
  for (;;) {
    const existing = await safePath(current, {allowMissing: true, type: 'directory'});
    if (existing) return path.join(existing.realPath, ...missing);
    missing.unshift(path.basename(current));
    const parent = path.dirname(current);
    if (parent === current) throw fail('INVALID_DIRECTORY', 'No existing directory ancestor');
    current = parent;
  }
}
async function writeNewFile(root, target, contents) {
  await safePath(path.dirname(target), {root, type: 'directory'});
  await safePath(target, {root, allowMissing: true, type: 'file'});
  await fs.writeFile(target, contents, {flag: 'wx'});
}
export async function readJson(p, max = 2 * 1024 * 1024) {
  const {stat: st} = await safePath(p, {type: 'file'});
  if (st.size > max) throw fail('INVALID_FILE', 'JSON file exceeds size limit: ' + p);
  return JSON.parse((await fs.readFile(p, 'utf8')).replace(/^\uFEFF/, ''));
}
export async function atomicJson(p, value) {
  await ensureDirectory(path.dirname(p));
  await safePath(p, {allowMissing: true, type: 'file'});
  const temp = p + '.' + randomUUID() + '.tmp';
  await fs.writeFile(temp, json(value), {flag: 'wx', mode: 0o600});
  try {
    await safePath(temp, {type: 'file'});
    await safePath(p, {allowMissing: true, type: 'file'});
    await fs.rename(temp, p);
  } finally {
    await safePath(temp, {allowMissing: true, type: 'file'});
    await fs.rm(temp, {force: true});
  }
}
export async function fileHash(p) {
  await safePath(p, {type: 'file'});
  const h = createHash('sha256');
  for await (const chunk of createReadStream(p)) h.update(chunk);
  return h.digest('hex');
}
function differences(a, b, prefix = '', output = []) {
  if (JSON.stringify(a) === JSON.stringify(b)) return output;
  if (a && b && !Array.isArray(a) && !Array.isArray(b) && typeof a === 'object' && typeof b === 'object') {
    for (const k of [...new Set([...Object.keys(a), ...Object.keys(b)])].sort()) differences(a[k], b[k], prefix ? prefix + '.' + k : k, output);
  } else output.push({field: prefix || '$', local: a ?? null, candidate: b ?? null});
  return output;
}
function summary(state) {
  const d = state.data;
  const out = [`# ${d.title || d.name || state.kind}`, '', `Запись: ${state.record_id}`, `Ревизия: ${state.revision_id}`, `Устройство: ${state.device_id}`, `Дата UTC: ${state.created_at}`, ''];
  if (state.kind !== 'task') return out.join('\n') + (state.kind === 'credentials' ? 'Зашифрованные переносимые учётные данные. Значения не входят в сводку.\n' : json(d));
  out.push(`Цель: ${d.goal}`, `Сессия: ${d.session_status}; задача: ${d.task_status}`, '');
  if (d.current_stage) out.push(`Текущий этап: ${d.current_stage}`, '');
  if (d.plan?.length) {
    out.push('## Этапы');
    for (const stage of d.plan) out.push(`- [${stage.status}] ${stage.id}: ${stage.title}`, `  Критерий готовности: ${stage.done_criterion}`, `  Проверка: ${stage.verification || 'не записана'}`);
    out.push('');
  }
  for (const [title, key] of [['Критерии готовности', 'done_criteria'], ['Ограничения', 'constraints'], ['Сделано', 'completed'], ['Не завершено', 'unfinished'], ['Следующие действия', 'next_steps']]) {
    out.push('## ' + title, ...(d[key].length ? d[key].map(x => '- ' + x) : ['Нет записей.']), '');
  }
  out.push('## Аргументы и гипотезы');
  for (const c of d.claims) out.push(`- [${c.evidence_status}; ${c.confidence || 'не оценена'}] ${c.text}`, `  Основание: ${c.reason}`, `  Доказательства: ${c.supporting_evidence.join('; ') || 'нет'}`, `  Возражения: ${c.counter_evidence.join('; ') || 'нет'}`, `  Проверить: ${c.next_check || 'не указано'}`);
  out.push('', '## Решения');
  for (const d1 of d.decisions) out.push(`- [${d1.status}] ${d1.text}`, `  Почему: ${d1.rationale}`, `  Альтернативы: ${d1.alternatives.join('; ') || 'нет'}`, `  Основания: ${d1.evidence.join('; ') || 'не указаны'}`);
  out.push('', '## Проверки');
  for (const v of d.verification) out.push(`- [${v.status}] ${v.check}: ${v.result}; основание: ${v.evidence}`);
  return out.join('\n') + '\n';
}

export class Store {
  constructor(local = defaultLocal()) { this.local = path.resolve(local); }
  async locked(fn) {
    await ensureDirectory(this.local);
    const lock = path.join(this.local, 'writer.lock');
    await safePath(lock, {root: this.local, allowMissing: true, type: 'file'});
    let handle;
    try { handle = await fs.open(lock, 'wx'); }
    catch (e) { if (e.code === 'EEXIST') throw fail('BUSY', 'Another local operation is running; do not delete a live lock'); throw e; }
    try { await handle.writeFile(String(process.pid)); return await fn(); }
    finally { await handle.close(); await safePath(lock, {root: this.local, allowMissing: true, type: 'file'}); await fs.rm(lock, {force: true}); }
  }
  async config() {
    const config = await readJson(path.join(this.local, 'device.json')).catch(e => {if (e.code === 'ENOENT') return null; throw e;});
    if (!config) return null;
    if (typeof config.root !== 'string' || !path.isAbsolute(config.root)) throw fail('INVALID_CONFIG', 'Expected an absolute workspace root');
    id.parse(config.device_id); id.parse(config.workspace_id);
    return config;
  }
  async connect(root, {create = false} = {}) {
    const target = path.resolve(root);
    if (inside(target, this.local) || inside(this.local, target)) throw fail('LOCAL_IN_SHARED', 'Local runtime state and synchronized data must be in separate folders');
    const candidateRoot = await prospectiveDirectory(target), candidateLocal = await prospectiveDirectory(this.local);
    if (inside(candidateRoot, candidateLocal) || inside(candidateLocal, candidateRoot)) throw fail('LOCAL_IN_SHARED', 'Canonical local and synchronized paths must be separate before initialization');
    return this.locked(async () => {
      await safePath(target, {allowMissing: true, type: 'directory'});
      let marker;
      if (!(await exists(path.join(target, 'workspace.json')))) {
        if (!create) throw fail('NOT_WORKSPACE', 'workspace.json not found');
        const entries = await fs.readdir(target).catch(e => { if (e.code === 'ENOENT') return []; throw e; });
        if (entries.length) throw fail('NOT_EMPTY', 'Initialization requires a new or empty folder');
        await ensureDirectory(target);
        marker = {schema_version: SCHEMA_VERSION, workspace_id: randomUUID(), app_version: VERSION, format: 'agent-workspace'};
        await ensureDirectory(path.join(target, 'records'), target);
        await ensureDirectory(path.join(target, 'objects'), target);
        await ensureDirectory(path.join(target, 'instructions'), target);
        await writeNewFile(target, path.join(target, 'START_HERE.md'), bootstrap());
        await writeNewFile(target, path.join(target, 'instructions', 'common.md'), '# Общие инструкции\n\nСохраняйте цель и ограничения пользователя. Не считайте закрытие сессии завершением задачи.\nСложную задачу делите на этапы с проверяемым результатом. Фиксируйте текущий этап и следующий шаг; завершение одного этапа не означает завершения всей задачи. Уже согласованные этапы продолжайте без повторного разрешения.\nПодтверждённые утверждения должны иметь проверяемые основания.\nПри расхождении версий показывайте сравнение и ждите выбора пользователя.\nИсходники проектов остаются на исходном ПК; в общее хранилище публикуются готовые результаты.\n');
        // Publish the marker last so a partial initialization cannot be attached.
        await writeNewFile(target, path.join(target, 'workspace.json'), json(marker));
      } else marker = await readJson(path.join(target, 'workspace.json'));
      this.checkMarker(marker);
      const realRoot = await fs.realpath(target);
      const realLocal = await fs.realpath(this.local);
      if (inside(realRoot, realLocal) || inside(realLocal, realRoot)) throw fail('LOCAL_IN_SHARED', 'Symlink aliases cannot put local state inside shared data');
      const old = await this.config();
      await atomicJson(path.join(this.local, 'device.json'), {device_id: old?.device_id || randomUUID(), root: realRoot, workspace_id: marker.workspace_id});
      return {status: 'connected', workspace_id: marker.workspace_id, root: realRoot};
    });
  }
  checkMarker(marker) {
    if (marker.format !== 'agent-workspace' || marker.schema_version !== SCHEMA_VERSION || typeof marker.workspace_id !== 'string') throw fail('UNSUPPORTED_SCHEMA', 'Unsupported workspace format; do not overwrite it');
    id.parse(marker.workspace_id);
  }
  async context() {
    const config = await this.config();
    if (!config) throw fail('NOT_CONFIGURED', 'Specify the synchronized folder with init or attach');
    const root = (await safePath(config.root, {type: 'directory'})).realPath;
    const local = (await safePath(this.local, {type: 'directory'})).realPath;
    if (inside(root, local) || inside(local, root)) throw fail('LOCAL_IN_SHARED', 'Local runtime state and synchronized data must remain separate');
    const marker = await readJson(path.join(config.root, 'workspace.json'));
    this.checkMarker(marker);
    if (marker.workspace_id !== config.workspace_id) throw fail('WORKSPACE_CHANGED', 'Different workspace at configured path; attach explicitly');
    const localDir = path.join(this.local, 'workspaces', marker.workspace_id);
    await ensureDirectory(localDir, this.local);
    const selected = await readJson(path.join(localDir, 'selected.json')).catch(e => { if (e.code === 'ENOENT') return {}; throw e; });
    if (!selected || typeof selected !== 'object' || Array.isArray(selected)) throw fail('INVALID_SELECTION', 'Expected a revision selection map');
    for (const [recordId, revisionId] of Object.entries(selected)) {id.parse(recordId); id.parse(revisionId);}
    return {...config, root, localDir, selected, verifiedObjects:new Map()};
  }
  objectPath(root, digest) {
    if (!/^[0-9a-f]{64}$/.test(digest)) throw fail('INVALID_HASH', 'Invalid object hash');
    return path.join(root, 'objects', digest.slice(0, 2), digest);
  }
  async putFile(input) {
    const ctx = await this.context();
    const source = path.resolve(input);
    await safePath(source, {type: 'file'});
    const temp = path.join(this.local, 'object-' + randomUUID() + '.tmp');
    await safePath(temp, {root: this.local, allowMissing: true, type: 'file'});
    const h = createHash('sha256');
    let size = 0, tempCreated = false;
    try {
      const output = createWriteStream(temp, {flags: 'wx', mode: 0o600});
      output.once('open', () => {tempCreated = true;});
      await pipeline(createReadStream(source), new Transform({transform(chunk, enc, cb) { h.update(chunk); size += chunk.length; cb(null, chunk); }}), output);
      const digest = h.digest('hex');
      const dest = this.objectPath(ctx.root, digest);
      await ensureDirectory(path.dirname(dest), ctx.root);
      await safePath(dest, {root: ctx.root, allowMissing: true, type: 'file'});
      await fs.copyFile(temp, dest, constants.COPYFILE_EXCL).catch(async e => {
        if (e.code !== 'EEXIST') throw e;
        if (await fileHash(dest) !== digest) throw fail('CORRUPT_OBJECT', 'Existing object hash mismatch');
      });
      return {sha256: digest, size, name: path.basename(source)};
    } finally {
      if (tempCreated) {await safePath(temp, {root: this.local, allowMissing: true, type: 'file'}); await fs.rm(temp, {force: true});}
    }
  }
  async verifyArtifacts(ctx, artifacts) {
    if (!Array.isArray(artifacts) || artifacts.length > 2000) throw fail('INVALID_ARTIFACTS', 'Invalid artifacts');
    for (const a of artifacts) {
      if (!a || typeof a.name !== 'string' || !Number.isSafeInteger(a.size) || a.size < 0) throw fail('INVALID_ARTIFACTS', 'Invalid artifact metadata');
      const object = this.objectPath(ctx.root, a.sha256);
      const {stat} = await safePath(object, {root: ctx.root, type: 'file'});
      const signature=JSON.stringify([stat.dev,stat.ino,stat.mode,stat.size,stat.mtimeMs,stat.ctimeMs]);
      // Reuse a successful digest only inside this operation. Every reference
      // still checks its path/type/size and file identity; no cross-run cache.
      if (stat.size !== a.size || (ctx.verifiedObjects?.get(a.sha256)!==signature && await fileHash(object)!==a.sha256)) throw fail('CORRUPT_OBJECT', 'Result object unavailable or changed');
      ctx.verifiedObjects?.set(a.sha256,signature);
    }
  }
  checkPackageArtifacts(kind, data, artifacts) {
    if (!['skill', 'mcp'].includes(kind)) return;
    const declared = new Map(artifacts.map(a => [a.sha256, a.size]));
    for (const file of data.files) if (declared.get(file.sha256) !== file.size) throw fail('INVALID_ARTIFACTS', 'Package files must match declared artifacts');
  }
  async taskArtifacts(ctx, data, artifacts) {
    const projectScan = await this.scan(ctx, data.project_id);
    if (projectScan.problems.length || ![...projectScan.valid.values()].some(b => b.state.kind === 'project')) throw fail('INVALID_PROJECT', 'Task must refer to a complete existing project');
    const collected = [...artifacts];
    for (const recordId of data.result_records) {
      const resultScan = await this.scan(ctx, recordId), result = resultScan.valid.get(ctx.selected[recordId]);
      if (!result || result.state.kind !== 'result' || result.state.data.project_id !== data.project_id || resultScan.problems.length) throw fail('INVALID_RESULT', 'Task results must be accepted results of the same project');
      collected.push(...result.state.artifacts);
    }
    return [...new Map(collected.map(a => [a.sha256, a])).values()];
  }
  async loadRevision(ctx, recordId, revisionId) {
    id.parse(recordId); id.parse(revisionId);
    const dir = path.join(ctx.root, 'records', recordId, revisionId);
    await safePath(dir, {root: ctx.root, type: 'directory'});
    const manifest = await readJson(path.join(dir, 'manifest.json'));
    if (manifest.schema_version !== SCHEMA_VERSION) throw fail('UNSUPPORTED_SCHEMA', 'Unsupported revision format');
    if (manifest.record_id !== recordId || manifest.revision_id !== revisionId) throw fail('CORRUPT_REVISION', 'Manifest identity mismatch');
    for (const name of ['state.json', 'summary.md']) {
      if (await fileHash(path.join(dir, name)) !== manifest.files?.[name]) throw fail('CORRUPT_REVISION', 'Revision file hash mismatch: ' + name);
    }
    const state = await readJson(path.join(dir, 'state.json'));
    if (state.schema_version !== SCHEMA_VERSION || state.record_id !== recordId || state.revision_id !== revisionId) throw fail('CORRUPT_REVISION', 'State identity mismatch');
    id.parse(state.device_id);
    if (!Array.isArray(state.parents) || state.parents.length > 100) throw fail('INVALID_PARENTS', 'Invalid revision parents');
    state.parents.forEach(p => id.parse(p));
    state.data = validatePayload(state.kind, state.data);
    await this.verifyArtifacts(ctx, state.artifacts);
    this.checkPackageArtifacts(state.kind, state.data, state.artifacts);
    return {state, manifest, summary: await fs.readFile(path.join(dir, 'summary.md'), 'utf8')};
  }
  async scan(ctx, recordId) {
    id.parse(recordId);
    const dir = path.join(ctx.root, 'records', recordId);
    const available = await safePath(dir, {root: ctx.root, allowMissing: true, type: 'directory'});
    const entries = available ? await fs.readdir(dir, {withFileTypes: true}) : [];
    const valid = new Map(), problems = [];
    for (const e of entries) {
      if (!e.isDirectory() || e.isSymbolicLink()) { problems.push({revision_id: e.name, reason: 'Unexpected record entry'}); continue; }
      try { const bundle = await this.loadRevision(ctx, recordId, e.name); valid.set(e.name, bundle); }
      catch (error) { problems.push({revision_id: e.name, reason: error.code || error.message}); }
    }
    // Reject orphans and cycles instead of treating partial provider delivery as a new baseline.
    const visiting = new Set(), good = new Set();
    const visit = rid => {
      if (good.has(rid)) return true;
      if (visiting.has(rid) || !valid.has(rid)) return false;
      visiting.add(rid);
      const okay = valid.get(rid).state.parents.every(visit);
      visiting.delete(rid);
      if (okay) good.add(rid);
      return okay;
    };
    for (const rid of valid.keys()) if (!visit(rid)) problems.push({revision_id: rid, reason: 'Missing ancestor or cycle'});
    const usable = new Map([...valid].filter(([rid]) => good.has(rid)));
    const kinds = new Set([...usable.values()].map(b => b.state.kind));
    if (kinds.size > 1) problems.push({reason: 'Record kind differs across revisions'});
    const referenced = new Set([...usable.values()].flatMap(b => b.state.parents));
    const heads = [...usable.keys()].filter(rid => !referenced.has(rid)).sort();
    return {valid: usable, heads, problems};
  }
  async cache(ctx, bundle) {
    await atomicJson(path.join(ctx.localDir, 'cache', bundle.state.record_id + '-' + bundle.state.revision_id + '.json'), bundle);
  }
  async saveSelection(ctx) { await atomicJson(path.join(ctx.localDir, 'selected.json'), ctx.selected); }
  async startup({includeChatIndex=false}={}) {
    return this.locked(async () => {
      let ctx;
      try { ctx = await this.context(); }
      catch (e) {
        if (e.code === 'NOT_CONFIGURED') return {status: 'not_configured', message: e.message};
        if (e.code === 'ENOENT' || e.code === 'ENOTDIR') {
          const c = await this.config();
          const selected = c ? await readJson(path.join(this.local, 'workspaces', c.workspace_id, 'selected.json')).catch(() => ({})) : {};
          return {status: 'offline', selected, message: 'Shared folder unavailable. Local accepted summaries remain readable; shared changes are not being made.'};
        }
        throw e;
      }
      const recordsDir = path.join(ctx.root, 'records');
      await safePath(recordsDir, {root: ctx.root, type: 'directory'});
      const entries = await fs.readdir(recordsDir, {withFileTypes: true});
      const selectionBefore=canonicalJson(ctx.selected);
      const records = [], chatIndex = [];
      for (const e of entries) {
        if (!e.isDirectory() || e.isSymbolicLink()) continue;
        const recordId = e.name;
        const scan = await this.scan(ctx, recordId);
        let selected = ctx.selected[recordId] || null;
        let status;
        if (scan.problems.length) status = 'sync_incomplete';
        else if (!selected && scan.heads.length === 1) {
          selected = scan.heads[0]; ctx.selected[recordId] = selected;
          await this.cache(ctx, scan.valid.get(selected)); status = 'loaded';
        } else if (scan.heads.length === 1 && selected === scan.heads[0]) status = 'current';
        else status = 'choice_required';
        records.push({record_id: recordId, kind: scan.valid.values().next().value?.state.kind, selected_revision: selected, candidate_revisions: scan.heads, status, problems: scan.problems});
        if(includeChatIndex) {
          const sessions=new Set(); let isCursor=false;
          for(const {state} of scan.valid.values()) {
            if(state.kind==='task'&&state.data.source_session?.provider==='codex') sessions.add(state.data.source_session.session_id);
            if(state.kind==='profile'&&state.data.type==='codex-chat-cursor-v1') {sessions.add(state.data.session_id);isCursor=true;}
          }
          if(sessions.size)chatIndex.push({record_id:recordId,source_sessions:[...sessions],is_cursor:isCursor,accepted_data:scan.valid.get(selected)?.state.data||null});
        }
      }
      // A missing record directory must not silently erase a locally accepted record.
      for (const recordId of Object.keys(ctx.selected)) if (!records.some(r => r.record_id === recordId)) records.push({record_id: recordId, selected_revision: ctx.selected[recordId], status: 'sync_incomplete', candidate_revisions: [], problems: [{reason: 'Accepted record is absent from shared folder'}]});
      if(canonicalJson(ctx.selected)!==selectionBefore)await this.saveSelection(ctx);
      return {status: records.some(r => r.status === 'sync_incomplete') ? 'sync_incomplete' : records.some(r => r.status === 'choice_required') ? 'choice_required' : 'ready', workspace_id: ctx.workspace_id, records, ...(includeChatIndex?{chat_index:chatIndex}:{}), freshness: 'Latest complete revisions observed locally; provider-wide synchronization is not asserted.'};
    });
  }
  async get(recordId, revisionId) {
    id.parse(recordId);
    const config = await this.config();
    if (!config) throw fail('NOT_CONFIGURED', 'Configure a workspace first');
    let ctx;
    try { ctx = await this.context(); }
    catch (e) {
      if (e.code !== 'ENOENT' && e.code !== 'ENOTDIR') throw e;
      const localDir = path.join(this.local, 'workspaces', config.workspace_id);
      const selected = await readJson(path.join(localDir, 'selected.json'));
      const rid = revisionId || selected[recordId]; id.parse(rid);
      const cached = await readJson(path.join(localDir, 'cache', recordId + '-' + rid + '.json'));
      return {...cached, offline: true, freshness: 'Cached accepted state; results may be unavailable'};
    }
    const chosen = revisionId || ctx.selected[recordId];
    if (!chosen) throw fail('CHOICE_REQUIRED', 'Run startup and review before reading a primary revision');
    return this.loadRevision(ctx, recordId, chosen);
  }
  async _publish(ctx, {recordId, kind, data, parents, artifacts = [], resolution}) {
    id.parse(recordId); kindSchema.parse(kind); id.parse(ctx.device_id);
    if (!Array.isArray(parents)) throw fail('INVALID_PARENTS', 'Invalid revision parents');
    parents.forEach(p => id.parse(p));
    parents = [...new Set(parents)].sort();
    if (parents.length > 100) throw fail('INVALID_PARENTS', 'Maximum 100 revision parents');
    data = validatePayload(kind, data);
    if (!Array.isArray(artifacts)) throw fail('INVALID_ARTIFACTS', 'Invalid artifacts');
    if (kind === 'task') artifacts = await this.taskArtifacts(ctx, data, artifacts);
    await this.verifyArtifacts(ctx, artifacts);
    this.checkPackageArtifacts(kind, data, artifacts);
    const revisionId = randomUUID();
    const state = {schema_version: SCHEMA_VERSION, record_id: recordId, revision_id: revisionId, parents, kind, created_at: new Date().toISOString(), device_id: ctx.device_id, data, artifacts, ...(resolution ? {resolution} : {})};
    const stateText = json(state), summaryText = summary(state);
    if (Buffer.byteLength(stateText) > 1024 * 1024) throw fail('TOO_LARGE', 'A checkpoint may not exceed 1 MiB; reference files instead');
    const manifest = {schema_version: SCHEMA_VERSION, record_id: recordId, revision_id: revisionId, files: {'state.json': hash(stateText), 'summary.md': hash(summaryText)}};
    const dir = path.join(ctx.root, 'records', recordId, revisionId);
    await ensureDirectory(dir, ctx.root);
    await writeNewFile(ctx.root, path.join(dir, 'state.json'), stateText);
    await writeNewFile(ctx.root, path.join(dir, 'summary.md'), summaryText);
    // Cloud clients can still deliver this first: receivers validate all declared files and objects.
    await writeNewFile(ctx.root, path.join(dir, 'manifest.json'), json(manifest));
    const bundle = await this.loadRevision(ctx, recordId, revisionId);
    ctx.selected[recordId] = revisionId;
    await this.cache(ctx, bundle); await this.saveSelection(ctx);
    return {record_id: recordId, revision_id: revisionId, kind};
  }
  async commit({record_id: recordId = randomUUID(), kind, data, expected_revision, artifacts = []}) {
    id.parse(recordId); data = validatePayload(kind, data);
    return this.locked(async () => this._commit(await this.context(),{record_id:recordId,kind,data,expected_revision,artifacts}));
  }
  // Internal: caller must hold this Store's writer lock. Allows a chat handoff
  // and its cursor to share base checks without nested or interleaved writers.
  async _commit(ctx,{record_id:recordId,kind,data,expected_revision,artifacts=[]}) {
      id.parse(recordId); data=validatePayload(kind,data);
      const scan = await this.scan(ctx, recordId);
      const selected = ctx.selected[recordId] || null;
      if (expected_revision !== undefined && expected_revision !== selected) throw fail('STALE_BASE', 'Selected revision changed; review again');
      if (scan.problems.length) throw fail('SYNC_INCOMPLETE', 'Wait for complete, validated delivery');
      if (scan.valid.size && (scan.heads.length !== 1 || selected !== scan.heads[0])) throw fail('CHOICE_REQUIRED', 'Review differences and obtain the user choice first');
      if (scan.valid.size && scan.valid.values().next().value.state.kind !== kind) throw fail('KIND_CHANGED', 'Record kind is immutable');
      if (selected && !scan.valid.has(selected)) throw fail('SYNC_INCOMPLETE', 'Accepted ancestor is unavailable');
      // Skip an unchanged handoff only after validating its base and all referenced
      // results. A stale writer or unresolved history must still require review.
      if (kind === 'task' && selected) {
        if (!Array.isArray(artifacts)) throw fail('INVALID_ARTIFACTS', 'Invalid artifacts');
        const resolvedArtifacts = await this.taskArtifacts(ctx, data, artifacts);
        await this.verifyArtifacts(ctx, resolvedArtifacts);
        const accepted = scan.valid.get(selected).state;
        if (canonicalJson(data) === canonicalJson(accepted.data)
          && canonicalJson(canonicalArtifacts(resolvedArtifacts)) === canonicalJson(canonicalArtifacts(accepted.artifacts))) {
          return {record_id: recordId, revision_id: selected, kind, status: 'unchanged'};
        }
      }
      return this._publish(ctx, {recordId, kind, data, artifacts, parents: selected ? [selected] : []});
  }
  fingerprint(scan, selected) {
    return hash({selected, heads: scan.heads, manifests: [...scan.valid.keys()].sort().map(r => scan.valid.get(r).manifest), problems: scan.problems});
  }
  async review(recordId) {
    id.parse(recordId);
    return this.locked(async () => {
      const ctx = await this.context(), scan = await this.scan(ctx, recordId);
      if (scan.problems.length) throw fail('SYNC_INCOMPLETE', 'Resolve incomplete or corrupt delivery before choosing a version');
      if (!scan.heads.length) throw fail('NOT_FOUND', 'Record not found');
      const selected = ctx.selected[recordId] || null;
      const local = selected ? scan.valid.get(selected) : null;
      if (selected && !local) throw fail('SYNC_INCOMPLETE', 'Locally selected revision is absent');
      const reviewId = randomUUID();
      const ancestor = (a, b, seen = new Set()) => a === b || (!seen.has(b) && (seen.add(b), (scan.valid.get(b)?.state.parents || []).some(p => ancestor(a, p, seen))));
      let recommendation;
      if (scan.heads.length === 1 && selected === scan.heads[0]) recommendation = {action: 'keep_local', reason: 'Local and shared revisions are identical'};
      else if (scan.heads.length === 1 && (!selected || ancestor(selected, scan.heads[0]))) recommendation = {action: 'take_revision', revision_id: scan.heads[0], reason: 'The complete candidate continues the accepted revision. User selection is required before replacing it.'};
      else recommendation = {action: 'merge_after_review', reason: 'Parallel histories exist. Compare verified results, constraints and decisions; timestamps do not select the primary version.'};
      const visibleData = b => b?.state.kind === 'credentials' ? {name:b.state.data.name,secret_ids:b.state.data.secret_ids,encryption:b.state.data.envelope?.encryption} : b?.state.data;
      const report = {review_id: reviewId, record_id: recordId, selected_revision: selected, candidates: scan.heads.map(r => ({revision_id: r, created_at: scan.valid.get(r).state.created_at, device_id: scan.valid.get(r).state.device_id, differences: differences(visibleData(local), visibleData(scan.valid.get(r))), artifact_changes: differences(local?.state.artifacts, scan.valid.get(r).state.artifacts)})), recommendation};
      await atomicJson(path.join(ctx.localDir, 'reviews', reviewId + '.json'), {fingerprint: this.fingerprint(scan, selected), report});
      return report;
    });
  }
  async choose({review_id: reviewId, choice, revision_id: revisionId, merged_data, user_confirmed = false}) {
    id.parse(reviewId);
    if (user_confirmed !== true) throw fail('CONFIRMATION_REQUIRED', 'A recommendation is not consent; obtain an explicit user choice');
    return this.locked(async () => {
      const ctx = await this.context();
      const saved = await readJson(path.join(ctx.localDir, 'reviews', reviewId + '.json'));
      const recordId = saved.report.record_id, scan = await this.scan(ctx, recordId);
      const selected = ctx.selected[recordId] || null;
      if (this.fingerprint(scan, selected) !== saved.fingerprint) throw fail('STALE_REVIEW', 'New revisions arrived; show a fresh comparison');
      const baseId = choice === 'keep_local' ? selected : revisionId;
      let chosen = scan.valid.get(baseId);
      if (choice === 'merge') chosen = scan.valid.get(selected || scan.heads[0]);
      if (!['keep_local', 'take_revision', 'merge'].includes(choice) || !chosen) throw fail('INVALID_CHOICE', 'Select an existing revision or supply a reviewed merge');
      if (choice === 'merge' && !merged_data) throw fail('MERGE_REQUIRED', 'Supply the complete reviewed merged state');
      if (choice === 'merge' && !['project','task','profile'].includes(chosen.state.kind)) throw fail('PACKAGE_MERGE', 'Select one binary/credential package, or publish a separately prepared merged package');
      const artifacts = choice !== 'merge' ? chosen.state.artifacts : chosen.state.kind === 'task' ? [] : [...new Map([...scan.valid.values()].flatMap(b => b.state.artifacts).map(a => [a.sha256, a])).values()];
      const result = await this._publish(ctx, {recordId, kind: chosen.state.kind, data: choice === 'merge' ? merged_data : chosen.state.data, artifacts, parents: [...new Set([...scan.heads, ...(selected ? [selected] : [])])], resolution: {choice, source_revision: baseId || null, review_id: reviewId, user_confirmed: true}});
      await safePath(path.join(ctx.localDir, 'reviews', reviewId + '.json'), {root: ctx.localDir, type: 'file'});
      await fs.rm(path.join(ctx.localDir, 'reviews', reviewId + '.json'));
      return result;
    });
  }
  async createProject(name, description = '', sourcePath) {
    const result = await this.commit({kind: 'project', data: {name, description}});
    if (sourcePath) await this.locked(async () => {
      const ctx = await this.context();
      const p = path.join(ctx.localDir, 'source-paths.json');
      const map = await readJson(p).catch(e => {if (e.code === 'ENOENT') return {}; throw e;});
      map[result.record_id] = path.resolve(sourcePath); await atomicJson(p, map);
    });
    return result;
  }
  async publishResult(file, projectId, description = '') {
    id.parse(projectId);
    const project = await this.get(projectId);
    if (project.state.kind !== 'project') throw fail('INVALID_PROJECT', 'Expected a project record');
    const object = await this.putFile(file);
    return this.commit({kind: 'result', data: {name: object.name, project_id: projectId, description}, artifacts: [object]});
  }
  async publishFolder(folder, kind, name) {
    if (!['skill', 'mcp'].includes(kind)) throw fail('INVALID_PACKAGE', 'Publish a ready skill or MCP distribution, not project sources');
    const root = path.resolve(folder), selectedFiles = [], files = [], artifacts = [];
    await safePath(root, {type: 'directory'});
    const walk = async (dir, prefix = '') => {
      await safePath(dir, {root, type: 'directory'});
      for (const e of await fs.readdir(dir, {withFileTypes: true})) {
        if (e.isSymbolicLink()) throw fail('SYMLINK_PACKAGE', 'Ready distributions must contain files, not machine-specific symlinks');
        if (['.git', 'node_modules', '__pycache__', '.venv'].includes(e.name)) throw fail('DEVELOPMENT_FOLDER', 'Select the ready distribution rather than its development folder');
        const rel = prefix ? prefix + '/' + e.name : e.name;
        if (e.isDirectory()) await walk(path.join(dir, e.name), rel);
        else if (e.isFile()) {
          if (selectedFiles.length >= 2000) throw fail('PACKAGE_TOO_LARGE', 'Maximum 2000 runtime files');
          const source = path.join(dir, e.name);
          await safePath(source, {root, type: 'file'});
          selectedFiles.push({source, relative_path: rel});
        }
      }
    };
    await walk(root);
    if (kind === 'skill' && !selectedFiles.some(f => f.relative_path === 'SKILL.md')) throw fail('INVALID_SKILL', 'SKILL.md is required');
    if (!selectedFiles.length) throw fail('EMPTY_PACKAGE', 'No runtime files found');
    // Reject an invalid/development tree before publishing even its first object.
    for (const selected of selectedFiles) {
      const object = await this.putFile(selected.source);
      files.push({relative_path: selected.relative_path, sha256: object.sha256, size: object.size}); artifacts.push(object);
    }
    return this.commit({kind, data: {name: name || path.basename(root), files}, artifacts});
  }
  async restorePackage(recordId, destination) {
    const ctx = await this.context(), bundle = await this.get(recordId);
    if (!['skill', 'mcp', 'result'].includes(bundle.state.kind)) throw fail('INVALID_PACKAGE', 'Only results and ready runtime packages can be restored');
    const target = path.resolve(destination);
    if (inside(ctx.root, target)) throw fail('RESTORE_IN_SHARED', 'Restore into a separate local folder');
    const parent = await safePath(path.dirname(target), {type: 'directory'});
    if (inside(ctx.root, path.join(parent.realPath, path.basename(target)))) throw fail('RESTORE_IN_SHARED', 'Restore into a separate local folder');
    if (await safePath(target, {allowMissing: true, type: 'directory'})) throw fail('DESTINATION_EXISTS', 'Choose a new destination; existing files are never overwritten');
    const files = bundle.state.kind === 'result' ? bundle.state.artifacts.map(a => ({relative_path: a.name, sha256: a.sha256})) : bundle.state.data.files;
    for (const f of files) {
      const segments=typeof f.relative_path === 'string' ? f.relative_path.split('/') : [];
      if (!segments.length || segments.some(s => !s || s==='.' || s==='..' || /[<>:"\\|?*\x00-\x1f]/.test(s) || /[. ]$/.test(s) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(s)) || !inside(target, path.resolve(target, f.relative_path)) || path.isAbsolute(f.relative_path)) throw fail('PATH_ESCAPE', 'Invalid or nonportable package path');
    }
    const paths = new Set(files.map(f => f.relative_path.toLocaleLowerCase('en-US')));
    if (paths.size !== files.length) throw fail('DUPLICATE_PATH', 'Package paths collide on case-insensitive filesystems');
    for (const filename of paths) {
      const parts = filename.split('/');
      for (let i = 1; i < parts.length; i++) if (paths.has(parts.slice(0, i).join('/'))) throw fail('DUPLICATE_PATH', 'A package path cannot be both a file and a directory');
    }
    // Validate everything before any destination write.
    for (const f of files) {
      const source = this.objectPath(ctx.root, f.sha256);
      await safePath(source, {root: ctx.root, type: 'file'});
      if (await fileHash(source) !== f.sha256) throw fail('CORRUPT_OBJECT', 'Cannot restore changed object');
    }
    await safePath(path.dirname(target), {type: 'directory'});
    if (await safePath(target, {allowMissing: true, type: 'directory'})) throw fail('DESTINATION_EXISTS', 'Choose a new destination; existing files are never overwritten');
    await fs.mkdir(target, {recursive: false});
    await safePath(target, {type: 'directory'});
    for (const f of files) {
      const dest = path.resolve(target, f.relative_path);
      await ensureDirectory(path.dirname(dest), target);
      await safePath(dest, {root: target, allowMissing: true, type: 'file'});
      const source = this.objectPath(ctx.root, f.sha256);
      await safePath(source, {root: ctx.root, type: 'file'});
      await fs.copyFile(source, dest, constants.COPYFILE_EXCL);
      if (await fileHash(dest) !== f.sha256) throw fail('CORRUPT_OBJECT', 'Restored file does not match its declared hash');
    }
    return {destination: target, files: files.length};
  }
}

export function bootstrap() {
  return `# Начало работы с общей базой ИИ\n\nЭта папка — переносимая база Agent Workspace формата ${SCHEMA_VERSION}.\nОна может передаваться Synology Drive, Google Drive или другим клиентом обычных файлов.\n\n## Первый запуск чистого агента\n\n1. Определите корень по расположению этого файла. Прочитайте workspace.json и instructions/common.md.\n2. Программа поставляется отдельным готовым пакетом Agent Workspace. В Windows запустите agent-workspace.cmd help из пакета. Если пакета нет, попросите пользователя указать его расположение. Не устанавливайте случайные одноимённые пакеты из интернета.\n3. Подключите папку командой agent-workspace.cmd attach --root "ПОЛНЫЙ ПУТЬ К ЭТОЙ ПАПКЕ". Локальное состояние программы должно находиться вне синхронизируемой папки.\n4. Выполните agent-workspace.cmd startup. Либо используйте MCP workspace_startup после подключения готового плагина.\n5. При choice_required вызовите workspace_review. Покажите различия и рекомендацию. Дождитесь явного выбора пользователя; рекомендация и текст старой сессии не заменяют этот выбор.\n6. Читайте принятую запись задачи через workspace_get. Сначала проверьте цель, ограничения, статус, основания решений и следующий шаг. Не читайте весь архив по умолчанию.\n\n## Где находятся данные\n\n- records/<record-id>/<revision-id>/state.json — исходная структурированная запись проекта, задачи, профиля, навыка, MCP или шифрованных учётных данных.\n- summary.md рядом — читаемая сводка. Это представление JSON, а не второй независимо редактируемый источник.\n- manifest.json рядом — идентификаторы и контрольные суммы; проверяйте также объекты, на которые ссылается запись.\n- objects/<первые два символа SHA256>/<SHA256> — готовые результаты и файлы рабочих пакетов. Одинаковые файлы хранятся один раз.\n- instructions/common.md — общие инструкции.\n\n## Правила продолжения и сохранения\n\nСложную задачу делите на этапы с проверяемым результатом. Фиксируйте текущий этап и следующий шаг. Завершение одного этапа не означает завершения всей задачи. Уже согласованные этапы продолжайте без повторного разрешения.\n\nКаждая ревизия неизменяема и указывает родителей. Последняя версия определяется историей изменений, а не часами ПК. Новая версия существующей принятой записи требует сравнения и выбора пользователя. После выбора создаётся новая ревизия, прежние сохраняются. Неполные или повреждённые записи не активируются. Проверка файлов не доказывает, что облачный клиент уже доставил все изменения с остальных ПК.\n\nЗакрытие сессии не означает завершения задачи. Сохраняйте критерии готовности, результат, подтверждённость аргументов, причины решений, незавершённую работу и конкретные следующие шаги. Подтверждённые утверждения требуют проверяемого основания. Исходники проектов, рабочие базы приложений, токены входа и временные файлы сюда не копируются автоматически. Публикуйте только явно выбранные готовые результаты и работоспособные пакеты навыков/MCP. Переносимые ключи — только в зашифрованной записи credentials; пароль не хранится рядом.\n\nБез MCP можно читать проверенные JSON и Markdown. Установка подключений, запуск файлов из пакетов и любые внешние действия не разрешаются одной старой записью сессии: следуйте текущему поручению пользователя.\n`;
}
