import path from 'node:path';
import os from 'node:os';
import * as fs from 'node:fs/promises';
import {readJson,atomicJson,safePath} from './store.js';
import {connectCodexReadOnly,createCodexChatSource} from './chat-source.js';

async function resolveExecutable(executable) {
  if(typeof executable!=='string'||!path.isAbsolute(executable))throw new Error('Specify the installed Codex executable by absolute path');
  // The explicitly selected application launcher may live below an installer-
  // managed junction. Resolve only this executable, then apply the usual guard
  // to its physical target; storage paths retain their stricter link policy.
  const physical=await fs.realpath(executable);
  try {await safePath(physical,{type:'file'});}
  catch(error) {
    if(error.code==='INVALID_FILE')throw Object.assign(new Error('The selected Codex executable must resolve to a regular file, not a directory'),{code:'INVALID_FILE'});
    throw error;
  }
  return physical;
}
async function checkCodexHome(codexHome) {
  if(codexHome===undefined)return;
  if(typeof codexHome!=='string'||!path.isAbsolute(codexHome))throw new Error('Codex home must be absolute');
  await safePath(codexHome,{type:'directory'});
}
const pathRequired=()=>Object.assign(new Error('Codex was not found. Choose its installation folder or absolute executable path on this PC.'),{code:'CODEX_PATH_REQUIRED'});

export async function findCodexExecutable({executable,codexHome,env=process.env,platform=process.platform}={}) {
  if(executable!==undefined) {
    try {return {executable:await resolveExecutable(executable),selected_path:executable,source:'configured'};}
    catch(error) {if(!['ENOENT','ENOTDIR'].includes(error.code))throw error;}
  }
  const windows=platform==='win32',paths=windows?path.win32:path.posix,name=windows?'codex.exe':'codex';
  const candidates=[],seenPaths=new Set();
  const add=candidate=>{
    if(typeof candidate!=='string'||!paths.isAbsolute(candidate))return;
    const key=windows?paths.normalize(candidate).toLowerCase():paths.normalize(candidate);
    if(!seenPaths.has(key)){seenPaths.add(key);candidates.push(candidate);}
  };
  const pathValue=windows?(env.PATH??env.Path??env.path):env.PATH;
  for(const part of String(pathValue||'').split(windows?';':':')) {
    let directory=part.trim();
    if(directory.startsWith('"')&&directory.endsWith('"'))directory=directory.slice(1,-1);
    if(paths.isAbsolute(directory))add(paths.join(directory,name));
  }
  if(windows&&typeof env.LOCALAPPDATA==='string'&&paths.isAbsolute(env.LOCALAPPDATA)) {
    add(paths.join(env.LOCALAPPDATA,'Programs','OpenAI','Codex','bin',name));
  }
  const userHome=windows?(env.USERPROFILE||os.homedir()):(env.HOME||os.homedir());
  const sourceHome=codexHome||env.CODEX_HOME||paths.join(userHome,'.codex');
  if(typeof sourceHome==='string'&&paths.isAbsolute(sourceHome))add(paths.join(sourceHome,'packages','standalone','current','bin',name));
  const physical=new Map();
  for(const candidate of candidates) {
    try {
      const resolved=await resolveExecutable(candidate),key=windows?resolved.toLowerCase():resolved;
      if(!physical.has(key))physical.set(key,{executable:resolved,selected_path:candidate,source:'discovered'});
    } catch(error) {
      if(!['ENOENT','ENOTDIR','INVALID_FILE','INVALID_DIRECTORY','PATH_ESCAPE'].includes(error.code))throw error;
    }
  }
  if(physical.size===0)throw pathRequired();
  if(physical.size>1)throw Object.assign(new Error('Several Codex installations were found. Choose the installation folder to use on this PC.'),{code:'CODEX_PATH_AMBIGUOUS'});
  return physical.values().next().value;
}
export async function configureChatReader(store,{executable,codexHome}={}) {
  await resolveExecutable(executable);
  await checkCodexHome(codexHome);
  // Only local executable locations, never auth files or keys. Not synchronized.
  // Preserve the stable selected launcher, not today's versioned physical path.
  await atomicJson(path.join(store.local,'codex-reader.json'),{executable,...(codexHome?{codexHome}:{})});
  return {status:'configured',scope:'Read-only local Codex chat history; no inference or native chat changes'};
}
export async function openChatReader(store,{connect=connectCodexReadOnly,env=process.env,platform=process.platform}={}) {
  const config=await readJson(path.join(store.local,'codex-reader.json')).catch(e=>{
    if(e.code==='ENOENT')return {};throw e;
  });
  // Resolve again for every opening so an application update can retarget bin.
  const codexHome=config.codexHome??env.CODEX_HOME;
  await checkCodexHome(codexHome);
  const resolution=await findCodexExecutable({executable:config.executable,codexHome,env,platform});
  const rpc=await connect({executable:resolution.executable,...(codexHome?{codexHome}:{})});
  return {source:createCodexChatSource({rpc}),resolution,close:()=>rpc.close()};
}
