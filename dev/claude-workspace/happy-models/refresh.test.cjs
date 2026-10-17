const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { catalog, mergeCatalog, encrypt, decrypt, updateSession } = require('./refresh-claude-models.cjs');
const choices = catalog(require('./codex-models.json'));
const key = crypto.randomBytes(32);
const record = { encryptionKey: key.toString('base64'), encryptionVariant: 'dataKey' };
const metadata = { machineId: 'this-machine', flavor: 'codex', currentModelCode: 'old-custom', permissionMode: 'default', arbitrary: { retained: true } };

test('rejects invalid configuration before publishing anything', () => {
  assert.throws(() => catalog({ version: 2, models: [] }));
  assert.throws(() => catalog({ version: 1, models: [{ id: 'x', name: 'X' }, { id: 'x', name: 'Y' }] }));
  assert.throws(() => catalog({ version: 1, models: [{ id: 'bad id', name: 'X' }] }));
});
test('adds choices without changing saved selection, permissions, or unrelated metadata', () => {
  const updated = mergeCatalog(metadata, choices);
  const { models, ...rest } = updated;
  assert.deepEqual(rest, metadata);
  assert(models.some(m => m.code === 'old-custom'));
  assert(models.some(m => m.code === 'gpt-6.1-sol' && m.value === 'Sol 6.1'));
  assert.equal(mergeCatalog(updated, choices), updated);
});
for (const variant of ['dataKey', 'legacy']) {
  test(`${variant} preserves metadata and rejects tampering`, () => {
    const encrypted = encrypt(metadata, key, variant);
    assert.deepEqual(decrypt(encrypted, key, variant), metadata);
    const corrupted = Buffer.from(encrypted, 'base64');
    corrupted[corrupted.length - 1] ^= 1;
    assert.throws(() => decrypt(corrupted.toString('base64'), key, variant));
  });
}
test('retries version conflicts against fresh metadata, retaining concurrent changes', async () => {
  let calls = 0;
  const concurrent = { ...metadata, currentModelCode: 'gpt-6-astra', permissionMode: 'plan', newerField: true };
  const raw = { id: 'test', metadataVersion: 1, metadata: encrypt(metadata, key, 'dataKey') };
  const changed = await updateSession(raw, record, choices, async (event, payload) => {
    assert.equal(event, 'update-metadata');
    if (++calls === 1) return { result: 'version-mismatch', version: 2, metadata: encrypt(concurrent, key, 'dataKey') };
    assert.equal(payload.expectedVersion, 2);
    const sent = decrypt(payload.metadata, key, 'dataKey');
    assert.equal(sent.permissionMode, 'plan');
    assert.equal(sent.currentModelCode, 'gpt-6-astra');
    assert.equal(sent.newerField, true);
    return { result: 'success', version: 3, metadata: payload.metadata };
  }, 'this-machine');
  assert.equal(changed, true);
  assert.equal(calls, 2);
});
test('does not write another machine or an already refreshed session', async () => {
  const emit = () => assert.fail('unexpected write');
  const raw = { id: 'test', metadataVersion: 1, metadata: encrypt(metadata, key, 'dataKey') };
  assert.equal(await updateSession(raw, record, choices, emit, 'other-machine'), false);
  raw.metadata = encrypt(mergeCatalog(metadata, choices), key, 'dataKey');
  assert.equal(await updateSession(raw, record, choices, emit, 'this-machine'), false);
});
test('reports rejection instead of claiming success', async () => {
  const raw = { id: 'test', metadataVersion: 1, metadata: encrypt(metadata, key, 'dataKey') };
  await assert.rejects(updateSession(raw, record, choices, async () => ({ result: 'error' }), 'this-machine'), /rejected/);
});
test('publishes Opus 5.5 for Claude without rewriting the existing model', async () => {
  const claude = { ...metadata, flavor: 'claude', currentModelCode: 'claude-opus-5' };
  const raw = { id: 'test-claude', metadataVersion: 1, metadata: encrypt(claude, key, 'dataKey') };
  const models = catalog(require('./claude-models.json'));
  assert.equal(await updateSession(raw, record, models, async (event, payload) => {
    const sent = decrypt(payload.metadata, key, 'dataKey');
    assert.equal(sent.currentModelCode, 'claude-opus-5');
    assert(sent.models.some(m => m.code === 'claude-opus-5-5' && m.value === 'Opus 5.5'));
    return { result: 'success', version: 2, metadata: payload.metadata };
  }, 'this-machine'), true);
});
