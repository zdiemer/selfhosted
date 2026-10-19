const { test } = require('node:test');
const assert = require('node:assert/strict');
const { patch, replacement } = require('./patch-block-publish');
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
const check = new AsyncFunction('fs', 'normalized_staging_path', 'target_path', 'GrpcError', 'grpc', replacement);
async function validate(staged, published) {
  return check({promises: {stat: async p => p === 'stage' ? staged : published}},
    'stage', 'publish', class extends Error {}, {status: {FAILED_PRECONDITION: 9}});
}
const block = rdev => ({isBlockDevice: () => true, rdev});
test('accepts identical block devices regardless of devtmpfs source name', async () => {
  assert.deepEqual(await validate(block(2224), block(2224)), {});
});
test('rejects a different disk', async () => {
  await assert.rejects(validate(block(2224), block(2240)));
});
test('rejects regular files even if device numbers match', async () => {
  await assert.rejects(validate(block(2224), {isBlockDevice: () => false, rdev: 2224}));
});
test('missing paths fail closed', async () => {
  await assert.rejects(check({promises: {stat: async () => {throw Error('ENOENT')}}},
    'stage', 'publish', Error, {}));
});
test('upstream code changes and duplicate matches fail closed', () => {
  assert.throws(() => patch('changed source'));
  const needle = 'normalized_staging_device = "dev"; // special syntax for single file bind mounts';
  assert.throws(() => patch(needle + needle));
  assert.ok(patch(needle).includes('stagedBlock.rdev !== publishedBlock.rdev'));
});
