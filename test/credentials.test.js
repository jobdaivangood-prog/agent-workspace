// Synthetic credentials only. No real .codex, network, subprocesses or cleanup.
import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import TOML from '@iarna/toml';
import {Store,readJson} from '../src/store.js';
import {exportCodex,applyCodex} from '../src/codex.js';
import {publishVault,restoreVault,localSecrets} from '../src/vault.js';

async function setup(){const observations=path.resolve('work','credential-observations');await fs.mkdir(observations,{recursive:true});const base=await fs.mkdtemp(path.join(observations,'run-'));const shared=path.join(base,'shared');const store=new Store(path.join(base,'local'));await store.connect(shared,{create:true});return {base,shared,store};}
test('an opaque positional key never appears in a public Codex profile',async()=>{
  const {base,store}=await setup();const home=path.join(base,'synthetic-codex');await fs.mkdir(home);await fs.writeFile(path.join(home,'config.toml'),'model="test-model"\n[mcp_servers.example]\ncommand="node"\nargs=["server.js","opaque-positional-value-1234"]\n[mcp_servers.example.http_headers]\nX-Custom="opaque-header-value-5678"\n');
  const result=await exportCodex(store,home);const bundle=await store.get(result.profile.record_id);const visible=JSON.stringify(bundle);assert.ok(!visible.includes('opaque-positional-value-1234'));assert.ok(!visible.includes('opaque-header-value-5678'));assert.ok(!visible.includes('server.js'));assert.equal(bundle.state.data.export_policy,2);
});
test('unexpected auth data is rejected rather than copied',async()=>{
  const {base,store}=await setup();const home=path.join(base,'synthetic-codex');await fs.mkdir(home);await fs.writeFile(path.join(home,'config.toml'),'[mcp_servers.example]\nurl="https://example.invalid/mcp"\nauth="unexpected-secret-value"\n');await assert.rejects(exportCodex(store,home),/Unsupported MCP auth/);
});

test('endpoint URLs are protected even when a token is in the path or fragment',async()=>{
  const {base,store}=await setup();const home=path.join(base,'synthetic-codex');await fs.mkdir(home);await fs.writeFile(path.join(home,'config.toml'),'[mcp_servers.example]\nurl="https://example.invalid/opaque-url-path-key#opaque-fragment-key"\n');const exported=await exportCodex(store,home);const visible=JSON.stringify(await store.get(exported.profile.record_id));assert.ok(!visible.includes('opaque-url-path-key'));assert.ok(!visible.includes('opaque-fragment-key'));assert.ok(exported.secret_ids.includes('mcp.example.url'));
});
test('portable and local vaults remain encrypted; applying a profile is explicit',async()=>{
  const {base,store}=await setup();const password='synthetic passphrase 2026';const secrets={'mcp.example.arg.0':'opaque-local-test-key'};
  const saved=await publishVault(store,secrets,password);const bundle=await store.get(saved.record_id);assert.ok(!JSON.stringify(bundle).includes(secrets['mcp.example.arg.0']));await assert.rejects(restoreVault(store,saved.record_id,'incorrect synthetic password'));
  await restoreVault(store,saved.record_id,password);const ctx=await store.context();const cached=await readJson(path.join(ctx.localDir,'credentials.vault.json'));assert.ok(!JSON.stringify(cached).includes(secrets['mcp.example.arg.0']));assert.deepEqual(await localSecrets(store,password),secrets);await assert.rejects(localSecrets(store,'incorrect synthetic password'));
  await assert.rejects(applyCodex(store,'missing-profile',path.join(base,'new-home')),/confirmation/i);
});

test('a profile restores synthetic arguments and preserves unrelated local configuration',async()=>{
  const {base,store}=await setup();const source=path.join(base,'source-codex');const target=path.join(base,'target-codex');await fs.mkdir(source);await fs.mkdir(target);const password='synthetic roundtrip passphrase';
  await fs.writeFile(path.join(source,'config.toml'),'model="test-model"\n[mcp_servers.example]\ncommand="node"\nargs=["server.js","opaque-test-value"]\n[mcp_servers.example.env]\nTEST_KEY="synthetic-key-only"\n');
  await fs.writeFile(path.join(target,'config.toml'),'unrelated_setting="preserve-me"\n[mcp_servers.other]\nurl="https://other.invalid/mcp"\n');const exported=await exportCodex(store,source,{includeSecrets:true,password});
  const before=await fs.readFile(path.join(target,'config.toml'),'utf8');const missing=await applyCodex(store,exported.profile.record_id,target,{userConfirmed:true,password});assert.equal(missing.changed,false);assert.equal(await fs.readFile(path.join(target,'config.toml'),'utf8'),before);
  await restoreVault(store,exported.credentials.record_id,password);const result=await applyCodex(store,exported.profile.record_id,target,{userConfirmed:true,password});assert.equal(result.status,'applied');assert.equal(await fs.readFile(result.backup,'utf8'),before);const config=TOML.parse(await fs.readFile(path.join(target,'config.toml'),'utf8'));assert.deepEqual(config.mcp_servers.example.args,['server.js','opaque-test-value']);assert.equal(config.mcp_servers.example.env.TEST_KEY,'synthetic-key-only');assert.equal(config.unrelated_setting,'preserve-me');assert.equal(config.mcp_servers.other.url,'https://other.invalid/mcp');
});

test('applying a profile cannot redirect configuration writes through a junction',async()=>{
  const {base,store,shared}=await setup();const profile=await store.commit({kind:'profile',data:{application:'codex',export_policy:2,common:{},mcp_servers:{},secret_ids:[]}});const redirect=path.join(base,'redirect-to-shared');await fs.symlink(shared,redirect,process.platform==='win32'?'junction':'dir');
  await assert.rejects(applyCodex(store,profile.record_id,redirect,{userConfirmed:true}));await assert.rejects(fs.stat(path.join(shared,'config.toml')),e=>e.code==='ENOENT');
});
