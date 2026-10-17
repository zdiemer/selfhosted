#!/usr/bin/env node
'use strict';

// Publish configurable model choices through Happy's encrypted metadata API.
// No agent prompts, session creation, runtime bundle patches, or model changes.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { createRequire } = require('node:module');
class RefreshError extends Error {}
const HAPPY_ROOT = process.env.HAPPY_PACKAGE_ROOT || '/usr/local/lib/node_modules/happy';
const happyRequire = createRequire(path.join(HAPPY_ROOT, 'package.json'));

function catalog(config) {
  if (config?.version !== 1 || !Array.isArray(config.models) || !config.models.length)
    throw new RefreshError('Catalog requires version: 1 and a nonempty models array');
  const seen = new Set();
  return config.models.map(({ id, name }) => {
    if (typeof id !== 'string' || !/^[a-zA-Z0-9._\[\]-]+$/.test(id) ||
        typeof name !== 'string' || !name.trim() || seen.has(id))
      throw new RefreshError('Invalid or duplicate model entry');
    seen.add(id);
    return { code: id, value: name };
  });
}

function mergeCatalog(metadata, choices) {
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata))
    throw new RefreshError('Invalid decrypted session metadata');
  const existing = Array.isArray(metadata.models) ? metadata.models : [];
  const ids = new Set(choices.map(m => m.code));
  // Preserve choices already supplied by the agent and a saved selection.
  const models = [...choices, ...existing.filter(m => m && typeof m.code === 'string' && !ids.has(m.code))];
  for (const id of [metadata.currentModelCode, metadata.modelMode]) {
    if (typeof id === 'string' && id && !models.some(m => m.code === id))
      models.push({ code: id, value: id });
  }
  if (JSON.stringify(existing) === JSON.stringify(models)) return metadata;
  return { ...metadata, models };
}

function decrypt(encoded, key, variant) {
  const bytes = Buffer.from(encoded, 'base64');
  if (key.length !== 32) throw new RefreshError('Invalid session key');
  let plaintext;
  if (variant === 'dataKey') {
    if (bytes.length < 29 || bytes[0] !== 0) throw new RefreshError('Unknown encrypted metadata format');
    const cipher = crypto.createDecipheriv('aes-256-gcm', key, bytes.subarray(1, 13));
    cipher.setAuthTag(bytes.subarray(-16));
    plaintext = Buffer.concat([cipher.update(bytes.subarray(13, -16)), cipher.final()]);
  } else if (variant === 'legacy') {
    const nacl = happyRequire('tweetnacl');
    plaintext = nacl.secretbox.open(bytes.subarray(24), bytes.subarray(0, 24), key);
    if (!plaintext) throw new RefreshError('Metadata authentication failed');
  } else throw new RefreshError('Unknown encryption variant');
  return JSON.parse(Buffer.from(plaintext).toString('utf8'));
}

function encrypt(metadata, key, variant) {
  const plaintext = Buffer.from(JSON.stringify(metadata));
  if (variant === 'dataKey') {
    const nonce = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv('aes-256-gcm', key, nonce);
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    return Buffer.concat([Buffer.from([0]), nonce, ciphertext, cipher.getAuthTag()]).toString('base64');
  }
  if (variant !== 'legacy') throw new RefreshError('Unknown encryption variant');
  const nonce = crypto.randomBytes(24);
  return Buffer.concat([nonce, Buffer.from(happyRequire('tweetnacl').secretbox(plaintext, nonce, key))]).toString('base64');
}

async function updateSession(raw, record, choices, emit, machineId) {
  const key = Buffer.from(record.encryptionKey, 'base64');
  const variant = record.encryptionVariant;
  let version = raw.metadataVersion;
  let metadata = decrypt(raw.metadata, key, variant);
  for (let attempt = 0; attempt < 4; attempt++) {
    // Re-check ownership after each optimistic-concurrency conflict too.
    if (metadata.machineId !== machineId || !['claude', 'codex'].includes(metadata.flavor)) return false;
    const next = mergeCatalog(metadata, choices);
    if (next === metadata) return false;
    const answer = await emit('update-metadata', {
      sid: raw.id, expectedVersion: version, metadata: encrypt(next, key, variant),
    });
    if (answer?.result === 'success') {
      const saved = decrypt(answer.metadata, key, variant);
      if (JSON.stringify(saved.models) !== JSON.stringify(next.models))
        throw new RefreshError('Server did not preserve the model catalog');
      return true;
    }
    if (answer?.result !== 'version-mismatch') throw new RefreshError('Server rejected model metadata update');
    version = answer.version;
    metadata = decrypt(answer.metadata, key, variant);
  }
  throw new RefreshError('Session kept changing; retry the refresh');
}

async function refresh({ home, server, dryRun = false }) {
  const pkg = JSON.parse(fs.readFileSync(path.join(HAPPY_ROOT, 'package.json'), 'utf8'));
  if (pkg.version !== '1.2.5') throw new RefreshError('Refresh protocol tested with Happy 1.2.5; review before upgrading');
  const url = new URL(server);
  if (url.protocol !== 'https:' || url.username || url.password) throw new RefreshError('Use an HTTPS Happy server URL');
  const read = name => JSON.parse(fs.readFileSync(path.join(home, name), 'utf8'));
  // Missing local overrides use the versioned examples next to this script.
  const catalogs = Object.fromEntries(['claude', 'codex'].map(agent => {
    const file = `${agent}-models.json`;
    const config = fs.existsSync(path.join(home, file)) ? read(file) : JSON.parse(fs.readFileSync(path.join(__dirname, file), 'utf8'));
    return [agent, catalog(config)];
  }));
  const credentials = read('access.key');
  const { machineId } = read('settings.json');
  const records = read('sessions.json').sessions;
  if (typeof credentials.token !== 'string' || !machineId || !records || Array.isArray(records))
    throw new RefreshError('Missing Happy login, machine, or local session records');
  const getSessions = async () => {
    const response = await fetch(`${url.origin}/v1/sessions`, {
      headers: { Authorization: `Bearer ${credentials.token}` },
      signal: AbortSignal.timeout(10000), redirect: 'error',
    });
    if (!response.ok) throw new RefreshError(`Happy session listing failed: HTTP ${response.status}`);
    const body = await response.json();
    if (!Array.isArray(body.sessions)) throw new RefreshError('Unexpected Happy session-list format');
    return body.sessions;
  };
  const sessions = await getSessions();
  const candidates = [];
  for (const raw of sessions) {
    const record = records[raw.id];
    if (!record) continue; // Other machines' keys and private sessions are untouched.
    const metadata = decrypt(raw.metadata, Buffer.from(record.encryptionKey, 'base64'), record.encryptionVariant);
    if (metadata.machineId !== machineId || !catalogs[metadata.flavor]) continue;
    candidates.push({ raw, record, flavor: metadata.flavor });
  }
  const counts = { claude: 0, codex: 0, updated: 0, verified: 0 };
  for (const c of candidates) counts[c.flavor]++;
  if (dryRun || !candidates.length) return counts;
  const { io } = happyRequire('socket.io-client');
  const socket = io(url.origin, {
    path: '/v1/updates', transports: ['websocket'], reconnection: false, autoConnect: false,
    // Account scope does not send a session-alive heartbeat or register RPCs.
    auth: { token: credentials.token, clientType: 'user-scoped', appState: 'background' },
  });
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new RefreshError('Happy connection timed out')), 10000);
      const done = fn => value => { clearTimeout(timer); fn(value); };
      socket.once('connect', done(resolve));
      socket.once('connect_error', done(() => reject(new RefreshError('Happy socket authentication/connection failed'))));
      socket.connect();
    });
    for (const c of candidates) {
      if (await updateSession(c.raw, c.record, catalogs[c.flavor],
        (event, payload) => socket.timeout(10000).emitWithAck(event, payload), machineId)) counts.updated++;
    }
    // Read the encrypted record back independently of the socket acknowledgement.
    const saved = new Map((await getSessions()).map(s => [s.id, s]));
    for (const c of candidates) {
      const raw = saved.get(c.raw.id);
      if (!raw) throw new RefreshError('Session disappeared during verification');
      const m = decrypt(raw.metadata, Buffer.from(c.record.encryptionKey, 'base64'), c.record.encryptionVariant);
      if (catalogs[c.flavor].some(choice => !m.models?.some(v => v.code === choice.code && v.value === choice.value)))
        throw new RefreshError('Model catalog verification failed');
      counts.verified++;
    }
    return counts;
  } finally { socket.close(); }
}

if (require.main === module) {
  const args = process.argv.slice(2);
  if (args.some(a => a !== '--dry-run')) { console.error('Usage: node refresh-claude-models.cjs [--dry-run]'); process.exitCode = 1; }
  else {
    const server = process.env.HAPPY_SERVER_URL;
    if (!server) { console.error('Set HAPPY_SERVER_URL to your existing relay URL'); process.exitCode = 1; }
    else refresh({ home: process.env.HAPPY_HOME_DIR || path.join(os.homedir(), '.happy'), server, dryRun: args.includes('--dry-run') })
      .then(counts => console.log(JSON.stringify(counts)))
      // Never print caught request objects, metadata, credentials, or session IDs.
      .catch(error => { console.error(error instanceof RefreshError ? error.message : 'Happy catalog refresh failed: check configuration, keys and relay connectivity'); process.exitCode = 1; });
  }
}
module.exports = { catalog, mergeCatalog, decrypt, encrypt, updateSession, refresh };
