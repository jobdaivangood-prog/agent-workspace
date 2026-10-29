import * as fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import TOML from '@iarna/toml';
import {atomicJson, readJson, safePath, ensureDirectory, prospectiveDirectory} from './store.js';
import {publishVault, localSecrets} from './vault.js';

const absolute = s => typeof s === 'string' && (/^[a-z]:[\\/]/i.test(s) || s.startsWith('/') || s.startsWith('\\\\'));
const inside = (root, target) => {const relative = path.relative(root, target); return relative === '' || (relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative));};
async function configText(home, allowMissing = false) {
  const file = path.join(home, 'config.toml');
  const checked = await safePath(file, {root: home, allowMissing, type: 'file'});
  return {exists: !!checked, text: checked ? await fs.readFile(file, 'utf8') : ''};
}
function protectedUrl(value, key, secrets) {
  const u = new URL(value);
  if (!['https:', 'http:'].includes(u.protocol)) throw new Error('Unsupported MCP URL scheme');
  // A credential can also be embedded in the path or fragment. Do not infer
  // whether an endpoint is public from its syntax; protect the complete value.
  const ref=`mcp.${key}.url`;secrets[ref]=value;return {secret_ref:ref};
}
export async function exportCodex(store, codexHome, {includeSecrets = false, password} = {}) {
  const home = (await safePath(path.resolve(codexHome), {type: 'directory'})).realPath;
  const ctx = await store.context(), shared = (await safePath(ctx.root, {type: 'directory'})).realPath;
  if (inside(shared, home)) throw new Error('Codex home must remain outside the synchronized workspace');
  const config = TOML.parse((await configText(home)).text);
  const secrets = {}, bindings = {}, servers = {};
  for (const [name, s] of Object.entries(config.mcp_servers || {})) {
    const exported = {};
    if (s.url) exported.url = protectedUrl(s.url, name, secrets);
    if (s.command) {
      if(typeof s.command!=='string')throw new Error('MCP command must be a string');
      exported.command = absolute(s.command) ? null : s.command;
      exported.args = (s.args || []).map((a, i) => {
        if(typeof a!=='string')throw new Error('MCP command arguments must be strings');
        // No heuristic can distinguish every positional secret from an ordinary argument.
        // Keep the entire opaque value out of public records. Known local paths stay local;
        // all other arguments are portable only through the explicitly requested vault.
        if(absolute(a)){bindings[name+'.args.'+i]=a;return {local_binding:name+'.args.'+i};}
        const ref=`mcp.${name}.arg.${i}`;secrets[ref]=a;return {secret_ref:ref};
      });
      if (absolute(s.command)) bindings[name + '.command'] = s.command;
      if (s.cwd) {bindings[name + '.cwd'] = s.cwd; exported.cwd={local_binding:name+'.cwd'};}
      exported.requires_local_binding = !!(s.cwd || absolute(s.command) || exported.args.some(a => typeof a === 'object'));
    }
    if(s.auth!==undefined){if(!['oauth','chatgpt'].includes(s.auth))throw new Error('Unsupported MCP auth selector');exported.auth=s.auth;}
    for (const key of ['enabled', 'required', 'scopes', 'startup_timeout_sec', 'tool_timeout_sec', 'enabled_tools', 'disabled_tools', 'env_vars', 'bearer_token_env_var', 'env_http_headers']) if (s[key] !== undefined) exported[key] = s[key];
    exported.env = {};
    for (const [key, value] of Object.entries(s.env || {})) {
      const ref = `mcp.${name}.env.${key}`;
      exported.env[key] = {secret_ref: ref}; secrets[ref] = String(value);
    }
    exported.http_headers = {};
    for (const [key, value] of Object.entries(s.http_headers || {})) {
      const ref = `mcp.${name}.header.${key}`; exported.http_headers[key] = {secret_ref: ref}; secrets[ref] = String(value);
    }
    if (s.oauth) {
      exported.oauth = {};
      for (const key of ['client_id', 'callback_port']) if (s.oauth[key] !== undefined) exported.oauth[key] = s.oauth[key];
      if (s.oauth.client_secret) {const ref = `mcp.${name}.oauth.client_secret`; secrets[ref] = s.oauth.client_secret; exported.oauth.client_secret = {secret_ref: ref};}
    }
    servers[name] = exported;
  }
  const common = {};
  for (const key of ['model', 'model_reasoning_effort', 'personality', 'model_verbosity']) if (config[key] !== undefined) common[key] = config[key];
  if(includeSecrets && Object.keys(secrets).length && (typeof password !== 'string' || password.length<12))throw new Error('A passphrase of at least 12 characters is required before exporting credentials');
  const profile = await store.commit({kind: 'profile', data: {name: 'Codex', application: 'codex', export_policy:2, common, mcp_servers: servers, plugins: Object.keys(config.plugins || {}), secret_ids: Object.keys(secrets).sort(), note: 'Complete endpoint URLs, opaque arguments, environment and headers are protected references. OAuth sessions and auth.json are not automatically exported.'}});
  await atomicJson(path.join(ctx.localDir, 'bindings-' + profile.record_id + '.json'), bindings);
  let credentials;
  if (includeSecrets && Object.keys(secrets).length) credentials = await publishVault(store, secrets, password);
  return {profile, ...(credentials ? {credentials} : {}), secret_ids: Object.keys(secrets).sort(), omitted: ['auth.json', 'OAuth refresh/access token caches', 'workspace trust entries', 'absolute source paths', 'managed plugin caches']};
}
export async function applyCodex(store, profileId, codexHome, {userConfirmed = false, password} = {}) {
  if (!userConfirmed) throw new Error('Explicit confirmation required to apply a profile');
  const ctx = await store.context(), bundle = await store.get(profileId);
  if (bundle.state.kind !== 'profile' || bundle.state.data.application !== 'codex') throw new Error('Expected a Codex profile');
  if(bundle.state.data.export_policy!==2)throw new Error('Re-export this legacy profile using the reviewed export policy before applying it');
  const shared = (await safePath(ctx.root, {type: 'directory'})).realPath;
  const home = await prospectiveDirectory(path.resolve(codexHome));
  if (inside(shared, home)) throw new Error('Codex home must remain outside the synchronized workspace');
  const configPath = path.join(home, 'config.toml');
  const initial = await configText(home, true), original = initial.text;
  const config = original ? TOML.parse(original) : {};
  const bindings = await readJson(path.join(ctx.localDir, 'bindings-' + profileId + '.json')).catch(e => {if(e.code === 'ENOENT') return {}; throw e;});
  const secrets = bundle.state.data.secret_ids?.length ? await localSecrets(store,password) : {}, issues = [], resolved = {};
  const resolveValue = (value, label) => {
    if (value && typeof value === 'object' && !Array.isArray(value) && value.secret_ref) {
      if (!Object.hasOwn(secrets, value.secret_ref)) {issues.push('Missing local secret: ' + value.secret_ref); return null;}
      return secrets[value.secret_ref];
    }
    if (value && typeof value === 'object' && value.local_binding) {
      if (!bindings[value.local_binding]) issues.push('Missing local path binding: ' + value.local_binding);
      return bindings[value.local_binding] || null;
    }
    if (Array.isArray(value)) return value.map((v, i) => resolveValue(v, label + '.' + i));
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) => [k, resolveValue(v, label + '.' + k)]));
    return value;
  };
  for (const [name, data] of Object.entries(bundle.state.data.mcp_servers)) {
    const server = resolveValue(data, name);
    delete server.requires_local_binding;
    if (data.requires_local_binding) {
      if (!server.command) {server.command = bindings[name + '.command']; if (!server.command) issues.push('Missing local executable binding: ' + name);}
      if (bindings[name + '.cwd']) server.cwd = bindings[name + '.cwd'];
    }
    resolved[name] = server;
  }
  if (issues.length) return {status: 'needs_local_setup', issues, changed: false};
  for(const key of ['model','model_reasoning_effort','personality','model_verbosity']) if(bundle.state.data.common[key]!==undefined) config[key]=bundle.state.data.common[key];
  config.mcp_servers = {...(config.mcp_servers || {}), ...resolved};
  const rendered = TOML.stringify(config);
  TOML.parse(rendered);
  await ensureDirectory(home);
  const actualHome = (await safePath(home, {type: 'directory'})).realPath;
  if (inside(shared, actualHome) || path.relative(home, actualHome) !== '') throw new Error('Codex home changed or resolves inside the synchronized workspace');
  const current = await configText(home, true);
  if (current.exists !== initial.exists || current.text !== original) throw new Error('Codex config changed during profile preparation; review and apply again');
  const backup = path.join(ctx.localDir, 'backups', 'config-' + Date.now() + '-' + randomUUID() + '.toml');
  await ensureDirectory(path.dirname(backup), ctx.localDir);
  await safePath(backup, {root: ctx.localDir, allowMissing: true, type: 'file'});
  await fs.writeFile(backup, original, {flag: 'wx', mode: 0o600});
  await safePath(backup, {root: ctx.localDir, type: 'file'});
  const temp = configPath + '.agent-workspace-' + randomUUID() + '.tmp';
  let tempCreated = false;
  try {
    await safePath(temp, {root: home, allowMissing: true, type: 'file'});
    const handle = await fs.open(temp, 'wx', 0o600);
    tempCreated = true;
    try {await handle.writeFile(rendered, 'utf8');} finally {await handle.close();}
    await safePath(temp, {root: home, type: 'file'});
    const latest = await configText(home, true);
    if (latest.exists !== initial.exists || latest.text !== original) throw new Error('Codex config changed during profile preparation; review and apply again');
    await fs.rename(temp, configPath);
    tempCreated = false;
  } finally {
    // An EEXIST from open('wx') must never remove a pre-existing temporary file.
    if (tempCreated) {await safePath(temp, {root: home, allowMissing: true, type: 'file'}); await fs.rm(temp, {force: true});}
  }
  return {status: 'applied', backup, restart_required: true};
}
