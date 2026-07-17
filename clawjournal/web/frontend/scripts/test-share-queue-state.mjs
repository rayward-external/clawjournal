import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { Buffer } from 'node:buffer';
import ts from 'typescript';

const sourceUrl = new URL('../src/views/Share/queueState.ts', import.meta.url);
const source = await readFile(sourceUrl, 'utf8');
const output = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(output).toString('base64')}`;
const {
  queueSelectionFromSearchParams,
  syncQueueSelectionToSearchParams,
} = await import(moduleUrl);

const ids = Array.from({ length: 5_000 }, (_, index) => (
  `session-${index.toString().padStart(5, '0')}-${'a'.repeat(48)}`
));

const defaultParams = new URLSearchParams();
syncQueueSelectionToSearchParams(defaultParams, ids, ids);
assert.equal(defaultParams.toString(), 'selection=all');
assert.deepEqual(queueSelectionFromSearchParams(defaultParams, ids), ids);

const optedOut = ids.filter((id) => id !== ids[2] && id !== ids[4_999]);
const optedOutParams = new URLSearchParams();
syncQueueSelectionToSearchParams(optedOutParams, optedOut, ids);
assert.equal(optedOutParams.get('selection'), 'all');
assert.equal(optedOutParams.get('exclude'), `${ids[2]},${ids[4_999]}`);
assert.ok(optedOutParams.toString().length < 200);
assert.deepEqual(queueSelectionFromSearchParams(optedOutParams, ids), optedOut);

const explicit = [ids[9], ids[3]];
const explicitParams = new URLSearchParams();
syncQueueSelectionToSearchParams(explicitParams, explicit, ids);
assert.equal(explicitParams.get('ids'), explicit.join(','));
assert.deepEqual(queueSelectionFromSearchParams(explicitParams, ids), explicit);

const emptyParams = new URLSearchParams();
syncQueueSelectionToSearchParams(emptyParams, [], ids);
assert.equal(emptyParams.toString(), 'ids=');
assert.deepEqual(queueSelectionFromSearchParams(emptyParams, ids), []);

assert.equal(queueSelectionFromSearchParams(new URLSearchParams(), ids), null);
