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
  MAX_QUEUE_QUERY_LENGTH,
  queueSelectionFromSearchParams,
  syncQueueSelectionToSearchParams,
} = await import(moduleUrl);

const storedValues = new Map();
const storage = {
  getItem: (key) => storedValues.get(key) ?? null,
  setItem: (key, value) => storedValues.set(key, value),
  removeItem: (key) => storedValues.delete(key),
};

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

const reordered = [...ids.slice(1), ids[0]];
const reorderedParams = new URLSearchParams();
syncQueueSelectionToSearchParams(
  reorderedParams,
  reordered,
  ids,
  storage,
  () => 'large-reordered',
);
assert.equal(reorderedParams.toString(), 'queue_ref=large-reordered');
assert.ok(reorderedParams.toString().length < MAX_QUEUE_QUERY_LENGTH);
assert.deepEqual(queueSelectionFromSearchParams(reorderedParams, ids, storage), reordered);

const middleSubset = ids.filter((_, index) => index % 2 === 0);
const middleSubsetParams = new URLSearchParams();
syncQueueSelectionToSearchParams(
  middleSubsetParams,
  middleSubset,
  ids,
  storage,
  () => 'large-middle-subset',
);
assert.equal(middleSubsetParams.toString(), 'queue_ref=large-middle-subset');
assert.ok(middleSubsetParams.toString().length < MAX_QUEUE_QUERY_LENGTH);
assert.deepEqual(queueSelectionFromSearchParams(middleSubsetParams, ids, storage), middleSubset);

const unavailableStorage = {
  getItem: () => null,
  setItem: () => { throw new Error('quota exceeded'); },
  removeItem: () => {},
};
const storageFailureParams = new URLSearchParams();
syncQueueSelectionToSearchParams(storageFailureParams, reordered, ids, unavailableStorage);
assert.equal(storageFailureParams.toString(), 'ids=');
assert.deepEqual(queueSelectionFromSearchParams(storageFailureParams, ids, unavailableStorage), []);

const missingRefParams = new URLSearchParams('queue_ref=missing');
assert.deepEqual(queueSelectionFromSearchParams(missingRefParams, ids, storage), []);
const invalidRefParams = new URLSearchParams('queue_ref=../../invalid');
assert.deepEqual(queueSelectionFromSearchParams(invalidRefParams, ids, storage), []);

const emptyParams = new URLSearchParams();
syncQueueSelectionToSearchParams(emptyParams, [], ids);
assert.equal(emptyParams.toString(), 'ids=');
assert.deepEqual(queueSelectionFromSearchParams(emptyParams, ids), []);

assert.equal(queueSelectionFromSearchParams(new URLSearchParams(), ids), null);

const queueStep = await readFile(new URL('../src/views/Share/QueueStep.tsx', import.meta.url), 'utf8');
assert.doesNotMatch(queueStep, /queueOrder\.includes\(/);
assert.match(queueStep, /queuedIds\.has\(s\.session_id\)/);
assert.match(queueStep, /p\.queuedSessions\.slice\(0, visibleQueueCount\)/);
assert.match(queueStep, /available\.slice\(0, visibleAvailableCount\)/);

const redactStep = await readFile(new URL('../src/views/Share/RedactStep.tsx', import.meta.url), 'utf8');
assert.match(redactStep, /p\.queuedSessions\.slice\(0, visibleCount\)/);
assert.doesNotMatch(redactStep, /p\.queuedSessions\.map\(/);

const reviewStep = await readFile(new URL('../src/views/Share/ReviewStep.tsx', import.meta.url), 'utf8');
assert.match(reviewStep, /sorted\.slice\(0, visibleCount\)/);
assert.doesNotMatch(reviewStep, /sorted\.map\(/);

const shareIndex = await readFile(new URL('../src/views/Share/index.tsx', import.meta.url), 'utf8');
assert.match(shareIndex, /\.slice\(0, PACKAGE_LOG_TRACE_LIMIT\)/);
assert.match(shareIndex, /Math\.min\(PACKAGE_ANIMATION_MAX_MS, 2200 \+ approvedList\.length \* 220\)/);
assert.match(shareIndex, /cancelRedactionRun\(redactionRunRef\)/);
assert.match(shareIndex, /return !data \|\| data\.loading/);
assert.match(shareIndex, /signal: run\.controller\.signal/);
assert.match(shareIndex, /if \(!isActive\(\)\) break/);
assert.match(shareIndex, /const approvedSessions = useMemo\(/);
assert.match(shareIndex, /approvedList=\{approvedSessions\}/);

const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
assert.match(apiSource, /redactionReport\(id: string, opts\?: \{ aiPii\?: boolean; signal\?: AbortSignal \}\)/);
assert.match(apiSource, /signal: opts\?\.signal/);

const packageStep = await readFile(new URL('../src/views/Share/PackageStep.tsx', import.meta.url), 'utf8');
assert.match(packageStep, /p\.approvedList\.slice\(0, PACKAGE_ANIMATION_TRACE_LIMIT\)\.forEach/);
assert.doesNotMatch(packageStep, /p\.approvedList\.forEach\(/);

const redactionRunSource = await readFile(new URL('../src/views/Share/redactionRun.ts', import.meta.url), 'utf8');
const redactionRunOutput = ts.transpileModule(redactionRunSource, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const redactionRunUrl = `data:text/javascript;base64,${Buffer.from(redactionRunOutput).toString('base64')}`;
const {
  beginRedactionRun,
  cancelRedactionRun,
  finishRedactionRun,
  isRedactionRunActive,
} = await import(redactionRunUrl);

const runSlot = { current: null };
const firstRun = beginRedactionRun(runSlot);
assert.ok(firstRun);
assert.equal(beginRedactionRun(runSlot), null);
assert.equal(isRedactionRunActive(runSlot, firstRun), true);
cancelRedactionRun(runSlot);
assert.equal(firstRun.controller.signal.aborted, true);
assert.equal(isRedactionRunActive(runSlot, firstRun), false);

const replacementRun = beginRedactionRun(runSlot);
assert.ok(replacementRun);
finishRedactionRun(runSlot, firstRun);
assert.equal(isRedactionRunActive(runSlot, replacementRun), true);
finishRedactionRun(runSlot, replacementRun);
assert.equal(runSlot.current, null);
