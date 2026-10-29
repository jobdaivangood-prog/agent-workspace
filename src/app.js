// Reviewed command dispatcher. Only interactive Windows setup uses a GUI child.
// No background service, credential discovery or recursive cleanup is performed.
import {parseArgs} from 'node:util';
import {createInterface} from 'node:readline/promises';
import path from 'node:path';
import {Store,readJson,defaultLocal} from './store.js';
import {serve} from './server.js';
import {exportCodex,applyCodex} from './codex.js';
import {publishVault,restoreVault} from './vault.js';
import {VERSION} from './schema.js';
import {selectDirectory} from './folder-picker.js';

const {values:options,positionals} = parseArgs({allowPositionals:true,options:{
  local:{type:'string'},root:{type:'string'},manual:{type:'boolean'},input:{type:'string'},file:{type:'string'},folder:{type:'string'},kind:{type:'string'},name:{type:'string'},description:{type:'string'},source:{type:'string'},project:{type:'string'},record:{type:'string'},revision:{type:'string'},review:{type:'string'},choice:{type:'string'},destination:{type:'string'},home:{type:'string'},'include-secrets':{type:'boolean'},'password-env':{type:'string'},confirm:{type:'boolean'}
}});
const store = new Store(options.local || process.env.AI_WORKSPACE_LOCAL || defaultLocal());
function required(key){if(!options[key])throw new Error('Missing --'+key);return options[key];}
function passphrase(requiredValue=true){
  const name=options['password-env']||'AI_WORKSPACE_VAULT_PASSWORD';
  const value=process.env[name];
  if(requiredValue&&!value)throw new Error('Set a local passphrase environment variable '+name+'; never put it in command arguments or chat');
  return value;
}
function help(){return {name:'Agent Workspace',version:VERSION,local_state:store.local,commands:[
  'setup [--root PATH] [--local PATH] [--manual]   Choose a folder in Windows or attach',
  'init --root PATH [--local PATH]                 Create a new empty shared folder',
  'attach --root PATH                            Attach or relocate an existing shared folder',
  'startup                                       Validate locally received revisions',
  'project --name TEXT [--source LOCAL_PATH]      Register without uploading sources',
  'get --record ID [--revision ID]                Read accepted state or an explicit candidate',
  'save-task --input LOCAL_JSON [--record ID]     Save task state and the stage plan',
  'review --record ID                            Compare revisions and recommend a choice',
  'choose --review ID --choice keep_local --confirm',
  'choose --review ID --choice take_revision --revision ID --confirm',
  'choose --review ID --choice merge --input LOCAL_JSON --confirm',
  'result --file FINAL_FILE --project ID          Publish a finished file',
  'package --folder READY_FOLDER --kind skill|mcp Publish a working runtime package',
  'restore --record ID --destination NEW_FOLDER  Restore without replacing existing files',
  'export-codex --home CODEX_HOME [--include-secrets --password-env ENV_NAME]',
  'vault-save --input LOCAL_SECRET_MAP_JSON --password-env ENV_NAME',
  'vault-restore --record ID --password-env ENV_NAME',
  'apply-codex --record PROFILE_ID --home LOCAL_CODEX_HOME --confirm [--password-env ENV_NAME]',
  'serve                                         Serve MCP over stdio'
],notes:['The cloud client is configured separately.','Version choices require an explicit human decision.','Only interactive Windows setup launches a native folder picker; MCP operations do not launch it.','OAuth token caches and auth.json are never discovered or copied automatically.']};}
async function setup(){
  let root=options.root;
  let picked=false;
  if(root===undefined&&process.platform==='win32'&&!options.manual){
    process.stdout.write('Выберите пустую папку AI-Workspace или существующую общую базу. Новую папку можно создать в открывшемся окне.\n');
    root=await selectDirectory();
    if(root===null)return {status:'cancelled',message:'Настройка отменена: папка не выбрана, подключение не изменено.'};
    picked=true;
  }
  if(root===undefined){
    const prompt=createInterface({input:process.stdin,output:process.stdout});
    try{root=await prompt.question('Полный путь к новой пустой папке общей базы или существующей базе: ');}finally{prompt.close();}
  }
  if(!picked){
    root=root.trim();
    if(root.startsWith('"')&&root.endsWith('"'))root=root.slice(1,-1);
  }
  const fullPath=process.platform==='win32' ? /^[a-z]:[\\/]/i.test(root)||/^[\\/]{2}[^\\/]+[\\/][^\\/]+(?:[\\/]|$)/.test(root) : path.isAbsolute(root);
  if(!root||!fullPath)throw new Error('Укажите полный путь к папке, например D:\\SynologyDrive\\AI-Workspace');
  const connected=await store.connect(root,{create:true});
  const startup=await store.startup();
  return {status:'configured',shared_folder:connected.root,local_state:store.local,workspace_status:startup.status,next_steps:['Добавьте общую папку в клиент синхронизации.','Подключите локальный MCP по START_HERE.md. При расхождениях сначала сравните версии.']};
}
async function run(){
  switch(positionals[0]||'help'){
    case 'help':return help();
    case 'setup':return setup();
    case 'init':return store.connect(required('root'),{create:true});
    case 'attach':return store.connect(required('root'));
    case 'startup':return store.startup();
    case 'project':return store.createProject(required('name'),options.description||'',options.source);
    case 'get':return store.get(required('record'),options.revision);
    case 'save-task':return store.commit({record_id:options.record,kind:'task',data:await readJson(required('input'))});
    case 'review':return store.review(required('record'));
    case 'choose':return store.choose({review_id:required('review'),choice:required('choice'),revision_id:options.revision,merged_data:options.input?await readJson(options.input):undefined,user_confirmed:options.confirm===true});
    case 'result':return store.publishResult(required('file'),required('project'),options.description||'');
    case 'package':return store.publishFolder(required('folder'),required('kind'),options.name);
    case 'restore':return store.restorePackage(required('record'),required('destination'));
    case 'export-codex':return exportCodex(store,required('home'),{includeSecrets:options['include-secrets']===true,password:options['include-secrets']?passphrase():undefined});
    case 'vault-save':return publishVault(store,await readJson(required('input')),passphrase(),options.record);
    case 'vault-restore':return restoreVault(store,required('record'),passphrase());
    case 'apply-codex':return applyCodex(store,required('record'),required('home'),{userConfirmed:options.confirm===true,password:passphrase(false)});
    case 'serve':await serve(store);return undefined;
    default:throw new Error('Unknown command: '+positionals[0]);
  }
}
try{const result=await run();if(result!==undefined)process.stdout.write(JSON.stringify(result,null,2)+'\n');}
catch(error){process.stderr.write(JSON.stringify({error:error.code||'FAILED',message:error.message})+'\n');process.exitCode=1;}
