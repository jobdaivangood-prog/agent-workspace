// Real filesystem paths and synthetic text executables only. The injected
// connector never launches Codex; all fixtures remain available for inspection.
import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import {Store,readJson,atomicJson,safePath} from '../src/store.js';
import {configureChatReader,openChatReader,findCodexExecutable} from '../src/chat-reader-config.js';

async function fixture() {
  const observations=path.resolve('work','chat-reader-config-observations');
  await fs.mkdir(observations,{recursive:true});
  const base=await fs.mkdtemp(path.join(observations,'run-'));
  const store=new Store(path.join(base,'local'));
  const program=path.join(base,'program');await fs.mkdir(program);
  const home=path.join(base,'codex-home');await fs.mkdir(home);
  const executable=path.join(program,process.platform==='win32'?'codex.exe':'codex');
  await fs.writeFile(executable,'Synthetic non-executable fixture; never run.');
  const env={PATH:'',CODEX_HOME:home,LOCALAPPDATA:path.join(base,'appdata'),USERPROFILE:base,HOME:base};
  return {base,store,program,home,executable,env,config:path.join(store.local,'codex-reader.json')};
}
function connector() {
  const calls=[];let closes=0;
  return {calls,get closes(){return closes;},connect:async options=>{
    calls.push(options);
    return {request:async()=>{throw new Error('No history request expected');},close:async()=>{closes++;}};
  }};
}
const within=(base,target)=>{const relative=path.relative(base,target);assert.ok(relative&&!relative.startsWith('..')&&!path.isAbsolute(relative),'Fixture operation must remain inside its run folder');};

test('reader configuration preserves sibling files and opens the checked physical executable',async()=>{
  const {store,home,executable,config}=await fixture();await fs.mkdir(store.local);
  const sibling=path.join(store.local,'unrelated-config.json'),bytes=Buffer.from('{"preserve":"unchanged"}\n');
  await fs.writeFile(sibling,bytes);
  assert.equal((await configureChatReader(store,{executable,codexHome:home})).status,'configured');
  assert.deepEqual(await readJson(config),{executable,codexHome:home});
  const fake=connector(),reader=await openChatReader(store,{connect:fake.connect});
  assert.deepEqual(fake.calls,[{executable:await fs.realpath(executable),codexHome:home}]);
  await reader.close();assert.equal(fake.closes,1);
  assert.deepEqual(await fs.readFile(sibling),bytes);
});

test('installer junction retargeting keeps the saved launcher stable and opens the new target',async()=>{
  const {base,store,program,executable,config}=await fixture();
  const nextProgram=path.join(base,'program-next');await fs.mkdir(nextProgram);
  const nextExecutable=path.join(nextProgram,'codex.exe');await fs.writeFile(nextExecutable,'Second synthetic executable, never run.');
  const launcher=path.join(base,'bin'),oldLauncher=path.join(base,'bin.previous');
  await fs.symlink(program,launcher,process.platform==='win32'?'junction':'dir');
  const selected=path.join(launcher,'codex.exe');
  await assert.rejects(safePath(selected,{type:'file'}),error=>error.code==='PATH_ESCAPE','Generic storage guard must still reject junctions');
  await configureChatReader(store,{executable:selected});
  const saved=await fs.readFile(config),fake=connector();
  await (await openChatReader(store,{connect:fake.connect})).close();
  assert.equal(fake.calls[0].executable,await fs.realpath(executable));
  // Move only our fixture link, after checking the absolute source/destination
  // and its physical target. No directory tree is deleted or moved recursively.
  within(base,launcher);within(base,oldLauncher);within(base,await fs.realpath(launcher));
  await fs.rename(launcher,oldLauncher);
  await fs.symlink(nextProgram,launcher,process.platform==='win32'?'junction':'dir');
  await (await openChatReader(store,{connect:fake.connect})).close();
  assert.equal(fake.calls[1].executable,await fs.realpath(nextExecutable));
  assert.deepEqual(await fs.readFile(config),saved,'Opening must not rewrite the stable configuration');
  assert.equal((await readJson(config)).executable,selected);
  await assert.rejects(safePath(launcher,{type:'directory'}),error=>error.code==='PATH_ESCAPE');
});

test('directory executable targets are rejected before configuration or connection',async()=>{
  const {base,store,config}=await fixture(),directory=path.join(base,'codex-directory.exe');await fs.mkdir(directory);
  await assert.rejects(configureChatReader(store,{executable:directory}),error=>error.code==='INVALID_FILE'&&/regular file/.test(error.message));
  await assert.rejects(fs.stat(config),error=>error.code==='ENOENT');
  await atomicJson(config,{executable:directory});const fake=connector();
  await assert.rejects(openChatReader(store,{connect:fake.connect}),error=>error.code==='INVALID_FILE'&&/regular file/.test(error.message));
  assert.equal(fake.calls.length,0);
});

test('Codex home remains a checked directory and cannot use the launcher junction exception',async()=>{
  const {base,store,home,executable,config}=await fixture();
  const linkedHome=path.join(base,'home-link');await fs.symlink(home,linkedHome,process.platform==='win32'?'junction':'dir');
  await assert.rejects(configureChatReader(store,{executable,codexHome:linkedHome}),error=>error.code==='PATH_ESCAPE');
  await assert.rejects(configureChatReader(store,{executable,codexHome:executable}),error=>error.code==='INVALID_DIRECTORY');
  await atomicJson(config,{executable,codexHome:linkedHome});const fake=connector();
  await assert.rejects(openChatReader(store,{connect:fake.connect}),error=>error.code==='PATH_ESCAPE');
  assert.equal(fake.calls.length,0);
});

test('relative and missing selected executables fail without searching or connecting',async()=>{
  const {base,store,config,env}=await fixture(),fake=connector();
  await assert.rejects(configureChatReader(store,{executable:'codex.exe'}),/absolute path/);
  const missing=path.join(base,'not-installed','codex.exe');
  await assert.rejects(configureChatReader(store,{executable:missing}),error=>error.code==='ENOENT');
  await atomicJson(config,{executable:missing});
  await assert.rejects(openChatReader(store,{connect:fake.connect,env}),error=>error.code==='CODEX_PATH_REQUIRED');
  assert.equal(fake.calls.length,0);
});

test('a valid configured launcher wins without consulting fallback locations',async()=>{
  const {executable}=await fixture();
  const unavailableEnvironment=new Proxy({}, {get(){throw new Error('Fallback environment must not be read');}});
  const found=await findCodexExecutable({executable,env:unavailableEnvironment});
  assert.equal(found.executable,await fs.realpath(executable));
  assert.equal(found.source,'configured');assert.equal(found.selected_path,executable);
});

test('missing configured launcher falls back to a unique PATH executable without rewriting config',async()=>{
  const {base,store,program,executable,home,config,env}=await fixture();
  const moved=path.join(base,'old-install','codex.exe');
  await atomicJson(config,{executable:moved,codexHome:home});
  const before=await fs.readFile(config),fake=connector();
  const reader=await openChatReader(store,{connect:fake.connect,env:{...env,PATH:program}});
  assert.equal(reader.resolution.source,'discovered');
  assert.equal(reader.resolution.executable,await fs.realpath(executable));
  assert.equal(fake.calls[0].executable,await fs.realpath(executable));
  await reader.close();assert.deepEqual(await fs.readFile(config),before);
});

test('a new PC can discover its own installation without creating a machine config',async()=>{
  const {store,program,config,env}=await fixture(),fake=connector();
  const reader=await openChatReader(store,{connect:fake.connect,env:{...env,PATH:program}});
  assert.equal(reader.resolution.source,'discovered');assert.equal(fake.calls.length,1);
  await reader.close();await assert.rejects(fs.stat(config),error=>error.code==='ENOENT');
});

test('multiple distinct PATH installations are ambiguous while aliases of one target are deduplicated',async()=>{
  const {base,program,executable,env}=await fixture(),second=path.join(base,'second-install');await fs.mkdir(second);
  await fs.writeFile(path.join(second,path.basename(executable)),'Second non-executable fixture.');
  await assert.rejects(findCodexExecutable({env:{...env,PATH:[program,second].join(path.delimiter)}}),error=>error.code==='CODEX_PATH_AMBIGUOUS');
  const alias=path.join(base,'path-alias');await fs.symlink(program,alias,process.platform==='win32'?'junction':'dir');
  const found=await findCodexExecutable({env:{...env,PATH:[program,alias,program].join(path.delimiter)}});
  assert.equal(found.executable,await fs.realpath(executable));
});

test('discovery ignores relative PATH entries and reports that an installation must be chosen',async()=>{
  const {env}=await fixture();
  await assert.rejects(findCodexExecutable({env:{...env,PATH:['relative-directory','.','..',''].join(path.delimiter)}}),error=>error.code==='CODEX_PATH_REQUIRED'&&/Choose/.test(error.message));
});

test('known install locations follow this PC environment rather than a fixed user path',async()=>{
  const {base,env}=await fixture(),name=process.platform==='win32'?'codex.exe':'codex';
  const otherHome=path.join(base,'another-user-home'),standalone=path.join(otherHome,'.codex','packages','standalone','current','bin');
  await fs.mkdir(standalone,{recursive:true});const executable=path.join(standalone,name);await fs.writeFile(executable,'Per-user standalone fixture.');
  const perUser={...env,HOME:otherHome,USERPROFILE:otherHome};delete perUser.CODEX_HOME;
  const found=await findCodexExecutable({env:perUser});
  assert.equal(found.executable,await fs.realpath(executable));
  const customHome=path.join(base,'portable-codex-home'),customBin=path.join(customHome,'packages','standalone','current','bin');
  await fs.mkdir(customBin,{recursive:true});const custom=path.join(customBin,name);await fs.writeFile(custom,'Custom CODEX_HOME fixture.');
  const overridden=await findCodexExecutable({env:{...perUser,CODEX_HOME:customHome}});
  assert.equal(overridden.executable,await fs.realpath(custom));
});

test('Windows local app installation is found from LOCALAPPDATA without a PATH entry', {skip:process.platform!=='win32'},async()=>{
  const {base,env}=await fixture(),local=path.join(base,'another-user-local'),bin=path.join(local,'Programs','OpenAI','Codex','bin');
  await fs.mkdir(bin,{recursive:true});const executable=path.join(bin,'codex.exe');await fs.writeFile(executable,'Windows installer fixture.');
  const found=await findCodexExecutable({env:{...env,LOCALAPPDATA:local}});
  assert.equal(found.executable,await fs.realpath(executable));
});

test('a configured invalid file is never replaced by a discovered valid executable',async()=>{
  const {base,program,env}=await fixture(),directory=path.join(base,'not-executable');await fs.mkdir(directory);
  await assert.rejects(findCodexExecutable({executable:directory,env:{...env,PATH:program}}),error=>error.code==='INVALID_FILE');
});
