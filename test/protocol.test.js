import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {Client, InMemoryTransport} from '@modelcontextprotocol/client';
import {createServer} from '../src/server.js';
import {Store,hash} from '../src/store.js';

// Run this single file directly when an in-process check is required:
//   node test/protocol.test.js
// No child processes, network listeners, real Codex data, or credentials are used.
// Test data is deliberately retained for inspection; there is no cleanup deletion.
test('MCP initialization, discovery and project access use a linked memory pair', {timeout: 15000}, async t => {
  const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const observations = path.join(projectRoot, 'work', 'protocol-observations');
  await fs.mkdir(observations, {recursive: true});
  const run = await fs.mkdtemp(path.join(observations, 'run-'));
  t.diagnostic('Preserved test data: ' + run);

  const store = new Store(path.join(run, 'local'));
  const snapshot={session_id:'protocol-chat',title:'Synthetic chat',archived:false,active:false,latest_turn_id:'turn-1',last_item_id:'item-1',fingerprint:hash('protocol-source'),public_messages:[{role:'user',text:'Preserve this synthetic goal',turn_id:'turn-1',item_id:'item-1'}],coverage:{complete_increment:true},change_type:'changed',checkpoint:{version:1,thread_id:'protocol-chat',last_turn_id:'turn-1'}};
  const source={enumerate:async()=>({threads:[{id:'protocol-chat',title:'Synthetic chat',archived:false}],complete:true,scope:'Synthetic protocol source'}),inspect:async(_session,checkpoint)=>({unchanged:!!checkpoint,snapshot})};
  const server = createServer(store,{chatSourceFactory:async()=>({source,close:async()=>{}})});
  const client = new Client({name: 'agent-workspace-protocol-check', version: '1.0.0'});
  // Both halves come from one SDK package; do not mix the server's and client's
  // separately bundled InMemoryTransport classes.
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();

  t.after(async () => {
    await Promise.allSettled([client.close(), server.close()]);
    // Only transports close. The test directory and all records remain intact.
  });

  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);
  assert.equal(client.getServerVersion()?.name, 'agent-workspace');
  const discovered = await client.listTools();
  const names = new Set(discovered.tools.map(tool => tool.name));
  for (const expected of ['workspace_startup', 'workspace_connect', 'workspace_create_project', 'workspace_get','workspace_scan_chats','workspace_save_chat']) {
    assert.ok(names.has(expected), 'Missing MCP tool: ' + expected);
  }

  const invoke = async (name, args = {}) => {
    const result = await client.callTool({name, arguments: args});
    assert.notEqual(result.isError, true, JSON.stringify(result.content));
    const text = result.content.find(block => block.type === 'text');
    assert.ok(text, 'Expected a JSON text response');
    return JSON.parse(text.text);
  };

  assert.equal((await invoke('workspace_startup')).status, 'not_configured');
  const shared = path.join(run, 'shared');
  assert.equal((await invoke('workspace_connect', {root: shared, create: true})).status, 'connected');

  const created = await invoke('workspace_create_project', {
    name: 'Внутрипроцессная проверка MCP',
    description: 'Синтетические данные проверки протокола'
  });
  assert.ok(created.record_id);
  assert.ok(created.revision_id);

  const startup = await invoke('workspace_startup');
  assert.equal(startup.status, 'ready');
  assert.ok(startup.records.some(record => record.record_id === created.record_id));

  const accepted = await invoke('workspace_get', {record_id: created.record_id});
  assert.equal(accepted.state.kind, 'project');
  assert.equal(accepted.state.revision_id, created.revision_id);
  assert.equal(accepted.state.data.name, 'Внутрипроцессная проверка MCP');
  assert.match(accepted.summary, /Внутрипроцессная проверка MCP/);

  const marker = JSON.parse(await fs.readFile(path.join(shared, 'workspace.json'), 'utf8'));
  assert.equal(marker.workspace_id, startup.workspace_id);
  const scan=await invoke('workspace_scan_chats');
  assert.equal(scan.counts.needs_summary,1);
  const chat=await invoke('workspace_save_chat',{scan_id:scan.scan_id,session_id:'protocol-chat',task:{project_id:created.record_id,title:'Synthetic chat',goal:'Preserve this synthetic goal',task_status:'in_progress',source_session:{provider:'codex',session_id:'protocol-chat'}}});
  assert.equal(chat.status,'saved');
  const repeated=await invoke('workspace_scan_chats');
  assert.equal(repeated.counts.unchanged,1);
  assert.equal(repeated.scan_id,null);
});
