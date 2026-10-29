import * as fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {VERSION} from '../src/schema.js';
import {fileHash} from '../src/store.js';

// Layout and checksum verification only. No subprocess, network, app execution or cleanup.
const output=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../output');
const release=path.join(output,`agent-workspace-${VERSION}-win-x64`);
const checks=JSON.parse(await fs.readFile(path.join(release,'checksums.json'),'utf8'));
if(checks.version!==VERSION||!Array.isArray(checks.files))throw new Error('Unexpected release manifest');
const names=new Set();
for(const entry of checks.files){
  if(typeof entry.file!=='string'||entry.file.split('/').some(s=>!s||s==='.'||s==='..'||s.includes('\\'))||path.isAbsolute(entry.file))throw new Error('Unsafe manifest path');
  const key=entry.file.toLowerCase();if(names.has(key))throw new Error('Duplicate manifest entry');names.add(key);
  const file=path.join(release,entry.file);const st=await fs.lstat(file);
  if(!st.isFile()||st.isSymbolicLink()||st.size!==entry.bytes||await fileHash(file)!==entry.sha256)throw new Error('Release checksum mismatch: '+entry.file);
}
for(const name of ['app.mjs','runtime/node.exe','runtime/FolderPicker.exe','START_HERE.md','Setup.ps1','Setup.cmd','ainfo.md','agent-workspace.cmd','plugin.json','mcp.json','NODE_LICENSE.txt','THIRD_PARTY_NOTICES.txt','skills/workspace-handoff/SKILL.md'])if(!names.has(name.toLowerCase()))throw new Error('Missing runtime file: '+name);
const walk=async(dir,prefix='')=>{for(const entry of await fs.readdir(dir,{withFileTypes:true})){const rel=prefix?prefix+'/'+entry.name:entry.name;if(entry.isSymbolicLink())throw new Error('Release contains a symlink');if(entry.isDirectory())await walk(path.join(dir,entry.name),rel);else if(rel!=='checksums.json'&&!names.has(rel.toLowerCase()))throw new Error('Unlisted release file: '+rel);}};
await walk(release);
const plugin=JSON.parse(await fs.readFile(path.join(release,'plugin.json'),'utf8'));
if(plugin.version!==VERSION)throw new Error('Wrong plugin version');
const mcp=JSON.parse(await fs.readFile(path.join(release,'mcp.json'),'utf8'));
if(!JSON.stringify(mcp).includes('${PLUGIN_ROOT}'))throw new Error('MCP manifest is not relocatable');
const report={version:VERSION,verified_at:new Date().toISOString(),release,checks:{manifest_hashes:checks.files.length,unlisted_files:false,portable_layout:true,plugin_version:true},execution_checks:'Separate CLI and stdio observations are required; this script does not execute the release.',real_synology_drive_test:false,real_user_codex_settings_changed:false};
await fs.writeFile(path.join(output,`agent-workspace-${VERSION}-layout-validation.json`),JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report,null,2));
