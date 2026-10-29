// Only the interactive Windows setup launches this GUI helper. No command shell,
// PowerShell script, application settings, or credentials are passed to it.
import {spawn} from 'node:child_process';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export function selectDirectory({helper=path.join(path.dirname(fileURLToPath(import.meta.url)),'runtime','FolderPicker.exe')}={}) {
  if(process.platform!=='win32')throw new Error('Выбор папки в окне доступен в Windows. Используйте --root PATH.');
  // The native dialog needs normal Windows/profile paths, not a copy of the
  // caller's environment containing possible API keys or a vault passphrase.
  const env={};
  for(const name of ['SystemRoot','WINDIR','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP'])if(process.env[name]!==undefined)env[name]=process.env[name];
  return new Promise((resolve,reject)=>{
    const child=spawn(helper,[],{shell:false,windowsHide:false,stdio:['ignore','pipe','pipe'],env});
    let output='',errorText='',finished=false;
    const fail=error=>{if(!finished){finished=true;reject(error);}};
    child.stdout?.setEncoding('utf8');child.stderr?.setEncoding('utf8');
    child.stdout?.on('data',chunk=>{output+=chunk;if(output.length>131072){fail(new Error('Некорректный ответ окна выбора папки.'));child.kill();}});
    child.stderr?.on('data',chunk=>{errorText=(errorText+chunk).slice(0,8192);});
    child.on('error',()=>fail(new Error('Не удалось открыть окно выбора папки. Распакуйте весь пакет и запустите Setup.cmd.')));
    child.on('close',code=>{
      if(finished)return;
      finished=true;
      if(code===2){resolve(null);return;}
      if(code!==0){reject(new Error('Ошибка окна выбора папки: '+(errorText.trim()||String(code))));return;}
      const selected=output.replace(/\r?\n$/,'');
      if(!selected||/[\r\n\0]/.test(selected)){reject(new Error('Окно не вернуло корректный путь к папке.'));return;}
      resolve(selected);
    });
  });
}
