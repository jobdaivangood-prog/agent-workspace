import * as fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {build} from 'esbuild';
import {VERSION} from '../src/schema.js';
import {fileHash} from '../src/store.js';

const source=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
let nodeLicense;
const explicitLicense=process.env.AGENT_WORKSPACE_NODE_LICENSE;
if(explicitLicense){if(!path.isAbsolute(explicitLicense))throw new Error('Node license path must be absolute');nodeLicense=await fs.readFile(explicitLicense,'utf8');}
for(const candidate of [path.resolve(path.dirname(process.execPath),'../LICENSE'),path.resolve(path.dirname(process.execPath),'../../LICENSE')]){if(nodeLicense)break;try{nodeLicense=await fs.readFile(candidate,'utf8');}catch{}}
if(!nodeLicense){const response=await fetch('https://raw.githubusercontent.com/nodejs/node/v'+process.versions.node+'/LICENSE');if(!response.ok)throw new Error('Cannot obtain the Node.js license');nodeLicense=await response.text();}
const output=path.join(source,'output');
const destination=path.join(output,`agent-workspace-${VERSION}-win-x64`);
if(process.platform!=='win32'||process.arch!=='x64')throw new Error('The ready runtime build requires Windows x64. Core source remains portable.');
await fs.mkdir(output,{recursive:true});
await fs.mkdir(destination); // Preserve prior outputs instead of overwriting them.
await fs.mkdir(path.join(destination,'runtime'));
await build({absWorkingDir:source,entryPoints:['./src/app.js'],tsconfigRaw:{compilerOptions:{}},bundle:true,platform:'node',target:'node22',format:'esm',outfile:path.join(destination,'app.mjs'),banner:{js:'import { createRequire as __createRequire } from "node:module"; const require = __createRequire(import.meta.url);'},legalComments:'eof'});
await fs.copyFile(process.execPath,path.join(destination,'runtime/node.exe'));
const compiler=path.join(process.env.SystemRoot||'C:\\Windows','Microsoft.NET','Framework64','v4.0.30319','csc.exe');
await fs.access(compiler);
await promisify(execFile)(compiler,['/nologo','/target:winexe','/platform:x64','/optimize+','/utf8output','/codepage:65001','/out:'+path.join(destination,'runtime/FolderPicker.exe'),path.join(source,'native/FolderPicker.cs')],{cwd:source,windowsHide:true});
await fs.writeFile(path.join(destination,'agent-workspace.cmd'),'@echo off\r\n"%~dp0runtime\\node.exe" "%~dp0app.mjs" %*\r\n');

const plugin={$schema:'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json',name:'agent-workspace',version:VERSION,description:'Local project handoffs, reviewed revisions and selected finished results.',extensions:{'com.openai':{interface:{displayName:'Agent Workspace',shortDescription:'Проекты и контекст между ПК',longDescription:'Переносимое состояние задач в синхронизируемой папке с явным выбором основной версии.',developerName:'Personal Workspace',category:'Productivity',capabilities:[],defaultPrompt:'Проверь общую базу и помоги продолжить выбранную задачу.'}}}};
const mcp={$schema:'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json',mcpServers:{'agent-workspace':{type:'stdio',command:'${PLUGIN_ROOT}/runtime/node.exe',args:['${PLUGIN_ROOT}/app.mjs','serve'],cwd:'${PLUGIN_ROOT}'}}};
await fs.writeFile(path.join(destination,'plugin.json'),JSON.stringify(plugin,null,2)+'\n');
await fs.writeFile(path.join(destination,'mcp.json'),JSON.stringify(mcp,null,2)+'\n');
for(const [input,target] of [['README.md','README_RU.md'],['docs/INSTALLATION.md','INSTALLATION_RU.md'],['examples/task.json','example-task.json']])await fs.copyFile(path.join(source,input),path.join(destination,target));
const metadata=JSON.parse(await fs.readFile(path.join(source,'package.json'),'utf8'));
const codeBase=metadata.repository.url.replace(/\.git$/,'')+'/blob/v'+VERSION+'/';
const passport=(await fs.readFile(path.join(source,'ainfo.md'),'utf8')).replace(/\]\(((?:src|native|packaging|scripts)\/[^)]+)\)/g,(_match,target)=>']('+codeBase+target+')');
await fs.writeFile(path.join(destination,'ainfo.md'),passport);
await fs.mkdir(path.join(destination,'docs'));
for(const name of ['INSTALLATION.md','ARCHITECTURE.md','VALIDATION.md','OPTIMIZATION.md'])await fs.copyFile(path.join(source,'docs',name),path.join(destination,'docs',name));
await fs.writeFile(path.join(destination,'START_HERE.md'),`# Agent Workspace ${VERSION}\n\nГотовый Windows x64 runtime. Сначала прочитайте README_RU.md и INSTALLATION_RU.md. Общая база находится отдельно; готовая программа не заменяет существующую AI-Workspace.\n\nРекомендуемый простой комплект содержит описание, отдельные инструкции настройки агента и установки MCP. Для ручного запуска Setup.cmd выбирает папку и подключает/создаёт базу, но не добавляет MCP в Codex. Для существующей базы используйте attach.\n\nПосле регистрации MCP перезапустите клиент, вызовите workspace_startup и прочитайте принятую задачу. При расхождениях покажите workspace_review и дождитесь выбора человека. Окончание сессии не означает завершение задачи.\n\nФункциональный паспорт — ainfo.md. Конфигурационные примеры используют обозначения путей, которые нужно заменить на реальные пути этого ПК.\n`);
for(const name of ['Setup.ps1','Setup.cmd']){
  const body=(await fs.readFile(path.join(source,'packaging',name),'utf8')).replace(/^\uFEFF/,'').replace(/\r\n|\r|\n/g,'\r\n');
  await fs.writeFile(path.join(destination,name),(name==='Setup.ps1'?'\uFEFF':'')+body,'utf8');
}
// Portable examples deliberately contain no absolute path of the build machine.
await fs.writeFile(path.join(destination,'client-configs.txt'),`Agent Workspace ${VERSION}\n\nReplace ABSOLUTE_APP_DIR and ABSOLUTE_LOCAL_STATE with real absolute paths on this PC; they are not auto-expanded variables. Preserve other configuration entries.\n\n[mcp_servers.agent-workspace]\ncommand = "ABSOLUTE_APP_DIR/runtime/node.exe"\nargs = ["ABSOLUTE_APP_DIR/app.mjs", "serve", "--local", "ABSOLUTE_LOCAL_STATE"]\n\nThe plugin mcp.json uses the supported PLUGIN_ROOT placeholder and can be relocated with its directory.\n`);
await fs.mkdir(path.join(destination,'skills/workspace-handoff'),{recursive:true});
const instructions=(await fs.readFile(path.join(source,'packaging/simple/1_Установка_агента.md'),'utf8')).match(/<!-- agent-workspace-session-commands:v1:start -->[\s\S]*?<!-- agent-workspace-session-commands:v1:end -->/)?.[0];
const alternate=(await fs.readFile(path.join(source,'packaging/simple/2_Установка_MCP.md'),'utf8')).match(/<!-- agent-workspace-session-commands:v1:start -->[\s\S]*?<!-- agent-workspace-session-commands:v1:end -->/)?.[0];
if(!instructions||instructions!==alternate)throw new Error('Installation session instructions differ');
await fs.writeFile(path.join(destination,'skills/workspace-handoff/SKILL.md'),'---\nname: workspace-handoff\ndescription: Resume a shared task or save all changed Codex chat handoffs through Agent Workspace.\n---\n\n'+instructions+'\n');

await fs.writeFile(path.join(destination,'NODE_LICENSE.txt'),nodeLicense);
const depRoot=path.join(source,'node_modules/.pnpm'),notices=[];
for(const pkgDir of await fs.readdir(depRoot)){
  if(pkgDir==='node_modules')continue;
  const modules=path.join(depRoot,pkgDir,'node_modules');let names=[];
  try{names=await fs.readdir(modules);}catch{continue;}
  for(const name of names){
    const parent=path.join(modules,name);
    const items=name.startsWith('@')?await fs.readdir(parent).then(v=>v.map(n=>path.join(parent,n))).catch(()=>[]):[parent];
    for(const item of items){
      let stat,p;try{stat=await fs.lstat(item);if(stat.isSymbolicLink()||!stat.isDirectory())continue;p=JSON.parse(await fs.readFile(path.join(item,'package.json'),'utf8'));}catch{continue;}
      if(notices.some(n=>n.name===p.name&&n.version===p.version))continue;
      const license=(await fs.readdir(item)).find(n=>/^licen[sc]e(?:\.|$)/i.test(n));
      notices.push({name:p.name,version:p.version,text:license?await fs.readFile(path.join(item,license),'utf8'):'See package license metadata: '+String(p.license)});
    }
  }
}
await fs.writeFile(path.join(destination,'THIRD_PARTY_NOTICES.txt'),notices.map(n=>`${n.name}@${n.version}\n${n.text}`).join('\n\n----------------\n\n'));
const files=[];
async function walk(dir,prefix=''){
  for(const entry of await fs.readdir(dir,{withFileTypes:true})){
    const relative=prefix?prefix+'/'+entry.name:entry.name;
    if(entry.isSymbolicLink())throw new Error('Build contains a link');
    if(entry.isDirectory())await walk(path.join(dir,entry.name),relative);
    else files.push({file:relative,bytes:(await fs.stat(path.join(dir,entry.name))).size,sha256:await fileHash(path.join(dir,entry.name))});
  }
}
await walk(destination);
files.sort((a,b)=>a.file.localeCompare(b.file));
await fs.writeFile(path.join(destination,'checksums.json'),JSON.stringify({version:VERSION,files},null,2)+'\n');
console.log(JSON.stringify({destination,files:files.length,total_bytes:files.reduce((sum,file)=>sum+file.bytes,0)},null,2));
