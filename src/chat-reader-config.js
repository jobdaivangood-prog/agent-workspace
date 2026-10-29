import path from 'node:path';
import {readJson,atomicJson,safePath} from './store.js';
import {connectCodexReadOnly,createCodexChatSource} from './chat-source.js';

export async function configureChatReader(store,{executable,codexHome}={}) {
  if(typeof executable!=='string'||!path.isAbsolute(executable))throw new Error('Specify the installed Codex executable by absolute path');
  await safePath(executable,{type:'file'});
  if(codexHome){if(!path.isAbsolute(codexHome))throw new Error('Codex home must be absolute');await safePath(codexHome,{type:'directory'});}
  // Only local executable locations, never auth files or keys. Not synchronized.
  await atomicJson(path.join(store.local,'codex-reader.json'),{executable,...(codexHome?{codexHome}:{})});
  return {status:'configured',scope:'Read-only local Codex chat history; no inference or native chat changes'};
}
export async function openChatReader(store) {
  const config=await readJson(path.join(store.local,'codex-reader.json')).catch(e=>{
    if(e.code==='ENOENT')throw Object.assign(new Error('Configure the local Codex executable with configure-chat-reader first'),{code:'CHAT_READER_NOT_CONFIGURED'});throw e;
  });
  await safePath(config.executable,{type:'file'});
  const rpc=await connectCodexReadOnly(config);
  return {source:createCodexChatSource({rpc}),close:()=>rpc.close()};
}
