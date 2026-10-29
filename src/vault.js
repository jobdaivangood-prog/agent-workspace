import {randomBytes, scryptSync, createCipheriv, createDecipheriv} from 'node:crypto';
import {readJson, atomicJson} from './store.js';
import path from 'node:path';

const derive = (password, salt) => {
  if (typeof password !== 'string' || password.length < 12) throw new Error('Use a vault passphrase of at least 12 characters');
  return scryptSync(password, salt, 32, {N: 32768, r: 8, p: 1, maxmem: 128 * 1024 * 1024});
};
export function encryptSecrets(secrets, password) {
  if (!secrets || typeof secrets !== 'object' || Array.isArray(secrets) || Object.values(secrets).some(v => typeof v !== 'string')) throw new Error('Secrets must be a string-to-string map');
  const salt = randomBytes(16), iv = randomBytes(12), key = derive(password, salt);
  try {
    const cipher = createCipheriv('aes-256-gcm', key, iv);
    cipher.setAAD(Buffer.from('agent-workspace-credentials-v1'));
    const ciphertext = Buffer.concat([cipher.update(JSON.stringify(secrets), 'utf8'), cipher.final()]);
    return {encryption: 'aes-256-gcm+scrypt-v1', salt: salt.toString('base64'), iv: iv.toString('base64'), tag: cipher.getAuthTag().toString('base64'), ciphertext: ciphertext.toString('base64')};
  } finally {key.fill(0);}
}
export function decryptSecrets(envelope, password) {
  if (envelope.encryption !== 'aes-256-gcm+scrypt-v1') throw new Error('Unsupported vault format');
  const salt = Buffer.from(envelope.salt, 'base64'), iv = Buffer.from(envelope.iv, 'base64');
  if (salt.length !== 16 || iv.length !== 12) throw new Error('Invalid vault metadata');
  const key = derive(password, salt);
  try {
    const decipher = createDecipheriv('aes-256-gcm', key, iv);
    decipher.setAAD(Buffer.from('agent-workspace-credentials-v1'));
    decipher.setAuthTag(Buffer.from(envelope.tag, 'base64'));
    return JSON.parse(Buffer.concat([decipher.update(Buffer.from(envelope.ciphertext, 'base64')), decipher.final()]).toString('utf8'));
  } finally {key.fill(0);}
}
export async function publishVault(store, secrets, password, recordId) {
  return store.commit({record_id: recordId, kind: 'credentials', data: {name: 'Переносимые ключи', secret_ids: Object.keys(secrets).sort(), envelope: encryptSecrets(secrets, password)}});
}
export async function restoreVault(store, recordId, password) {
  const bundle = await store.get(recordId);
  if (bundle.state.kind !== 'credentials') throw new Error('Expected a credentials record');
  const secrets = decryptSecrets(bundle.state.data.envelope, password);
  const ctx = await store.context();
  // Verify the passphrase, but persist only the authenticated encrypted envelope.
  // Plaintext keys are materialized in memory only when applying an approved profile.
  await atomicJson(path.join(ctx.localDir, 'credentials.vault.json'), {format:'encrypted-cache-v1',secret_ids:Object.keys(secrets).sort(),envelope:bundle.state.data.envelope});
  return {status: 'restored_encrypted_locally', secret_ids: Object.keys(secrets).sort()};
}
export async function localSecrets(store, password = process.env.AI_WORKSPACE_VAULT_PASSWORD) {
  const ctx = await store.context();
  const cached = await readJson(path.join(ctx.localDir, 'credentials.vault.json')).catch(e => {if(e.code==='ENOENT')return null;throw e;});
  if(!cached)return {};
  if(cached.format!=='encrypted-cache-v1')throw new Error('Unsupported local credential format');
  if(!password)throw new Error('A local vault passphrase is required to apply protected connection settings');
  return decryptSecrets(cached.envelope,password);
}
