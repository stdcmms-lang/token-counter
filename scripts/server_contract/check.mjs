#!/usr/bin/env node
/**
 * Offline executable snapshot of tokenusage.dev 9afabc0.
 *
 * Each stdin line is a raw JSON payload. Each stdout line has `share` and
 * `report` safeParse results (success/data or success/issues), the 422 check's
 * `plausibilityIssues` (null when shape fails), `shareAccepted`, and
 * `shareStatus` (200, 400 or 422). Invalid JSON gets one invalid_json answer;
 * processing continues with the next line. --now ISO pins the validator clock.
 * No compiled files, fetches or server services are needed.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
// Absolute package paths prevent an absent local install from finding an ambient
// dependency in an ancestor directory or through NODE_PATH.
const ts = require(join(here, 'node_modules/typescript'));
const zod = require(join(here, 'node_modules/zod'));
const provenance = JSON.parse(readFileSync(join(here, 'provenance.json'), 'utf8'));
const hashes = {
  'schema.ts': '61152fa71832a2565174e390041e0db8c2932b6c324d57fc799bd55288c69882',
  'validate.ts': '8cf76f0d71c5371633f9d47419ff0073daa5fc7b1dd09da114a35c553f5cded5',
};
assert.equal(provenance.server_commit, '9afabc0', 'server snapshot commit');
assert.equal(provenance.typescript, '5.9.3');
assert.equal(provenance.zod, '4.6.5');
assert.equal(ts.version, '5.9.3', 'installed TypeScript must match the pin');
assert.equal(JSON.parse(readFileSync(join(here, 'node_modules/zod/package.json'), 'utf8')).version,
  '4.6.5', 'installed Zod must match the pin');
const pkg = JSON.parse(readFileSync(join(here, 'package.json'), 'utf8'));
assert.equal(pkg.private, true);
assert.deepEqual(pkg.dependencies, { typescript: '5.9.3', zod: '4.6.5' });

const modules = new Map();
function loadSnapshot(name) {
  if (modules.has(name)) return modules.get(name);
  const bytes = readFileSync(join(here, name));
  const digest = createHash('sha256').update(bytes).digest('hex');
  assert.equal(digest, hashes[name], `${name}: frozen bytes changed`);
  assert.equal(provenance.files[name].sha256, digest, `${name}: provenance hash`);
  const result = ts.transpileModule(bytes.toString('utf8'), {
    fileName: name,
    reportDiagnostics: true,
    compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
      isolatedModules: true, esModuleInterop: true,
    },
  });
  const errors = (result.diagnostics ?? []).filter(d => d.category === ts.DiagnosticCategory.Error);
  assert.equal(errors.length, 0, `${name}: TypeScript transpilation failed`);
  const module = { exports: {} };
  const resolve = specifier => {
    if (specifier === 'zod') return zod;
    if (specifier === './schema.js') return loadSnapshot('schema.ts');
    // ./types.js is a type-only import and must have disappeared in memory.
    throw new Error(`Unexpected snapshot runtime import: ${specifier}`);
  };
  new Function('require', 'module', 'exports', result.outputText)(resolve, module, module.exports);
  modules.set(name, module.exports);
  return module.exports;
}

const { SharePayloadSchema, ReportPayloadSchema } = loadSnapshot('schema.ts');
const { plausibilityIssues } = loadSnapshot('validate.ts');
assert.equal(typeof plausibilityIssues, 'function');

function wireResult(parsed) {
  return parsed.success ? { success: true, data: parsed.data }
    : { success: false, issues: parsed.error.issues };
}

function check(payload, now) {
  const share = SharePayloadSchema.safeParse(payload);
  const issues = share.success ? plausibilityIssues(share.data, now) : null;
  return {
    share: wireResult(share), plausibilityIssues: issues,
    report: wireResult(ReportPayloadSchema.safeParse(payload)),
    shareAccepted: share.success && issues.length === 0,
    shareStatus: !share.success ? 400 : issues.length ? 422 : 200,
  };
}

// Exact JSON example from evidence/share-protocol.md, section Payload,
// supplied with tokenusage.dev commit 9afabc0. Kept here so CI needs no .local files.
const protocolExample = {
  "schema": 1,
  "client": { "name": "token-counter", "version": "1.10.0" },
  "handle": "ada",
  "generated_at": "2026-09-28T12:00:00Z",
  "days": [{
    "date": "2026-09-10",
    "responses": 8, "input": 2000, "cached": 490,
    "output": 200, "reasoning": 61, "sessions": 4,
    "tiers": {
      "standard": {
        "responses": 5, "input": 1300, "cached": 140,
        "output": 130, "reasoning": 29
      },
      "fast": {
        "responses": 2, "input": 400, "cached": 250,
        "output": 40, "reasoning": 25
      },
      "ultrafast": {
        "responses": 1, "input": 300, "cached": 100,
        "output": 30, "reasoning": 7
      }
    }
  }],
  "sessions": [
    {
      "id": "1111111111111111", "day": "2026-09-10",
      "start": "2026-09-10T10:00:06Z", "end": "2026-09-10T10:01:10Z",
      "active_s": 64, "responses": 2, "input": 200, "cached": 40,
      "output": 20, "reasoning": 4, "model": "m"
    },
    {
      "id": "2222222222222222", "day": "2026-09-10",
      "start": "2026-09-10T10:02:02Z", "end": "2026-09-10T10:03:04Z",
      "active_s": 62, "responses": 2, "input": 400, "cached": 250,
      "output": 40, "reasoning": 25, "model": "m"
    },
    {
      "id": "3333333333333333", "day": "2026-09-10",
      "start": "2026-09-10T10:04:08Z", "end": "2026-09-10T10:05:12Z",
      "active_s": 64, "responses": 2, "input": 600, "cached": 200,
      "output": 60, "reasoning": 14, "model": "n"
    },
    {
      "id": "4444444444444444", "day": "2026-09-10",
      "start": "2026-09-10T10:06:14Z", "end": "2026-09-10T10:07:16Z",
      "active_s": 62, "responses": 2, "input": 800, "cached": 0,
      "output": 80, "reasoning": 18, "model": "m"
    }
  ],
  "windows": [{
    "start": "2026-09-10T10:00:00Z",
    "window_minutes": 10080, "plan": "prolite",
    "first_pct": 0, "peak_pct": 20,
    "responses": 8, "input": 2000, "cached": 490, "output": 200,
    "split": [
      {
        "model": "m", "tier": "standard",
        "responses": 4, "input": 1000, "cached": 40, "output": 100
      },
      {
        "model": "m", "tier": "fast",
        "responses": 2, "input": 400, "cached": 250, "output": 40
      },
      {
        "model": "n", "tier": "standard",
        "responses": 1, "input": 300, "cached": 100, "output": 30
      },
      {
        "model": "n", "tier": "ultrafast",
        "responses": 1, "input": 300, "cached": 100, "output": 30
      }
    ]
  }],
  "latency": {
    "from": "2026-09-10", "to": "2026-09-10", "plan": "prolite",
    "responses": { "n": 8, "median_s": 9, "p90_s": 14.6 },
    "turns": null,
    "groups": [
      { "model": "m", "effort": "high", "n": 6, "median_s": 8, "p90_s": 15 },
      { "model": "n", "effort": "high", "n": 2, "median_s": 10, "p90_s": 11.6 }
    ],
    "tier_groups": [
      {
        "model": "m", "effort": "high", "tier": "standard",
        "n": 4, "median_s": 12, "p90_s": 15.4
      },
      {
        "model": "n", "effort": "high", "tier": "ultrafast",
        "n": 1, "median_s": 12, "p90_s": 12
      },
      {
        "model": "n", "effort": "high", "tier": "standard",
        "n": 1, "median_s": 8, "p90_s": 8
      },
      {
        "model": "m", "effort": "high", "tier": "fast",
        "n": 2, "median_s": 3, "p90_s": 3.8
      }
    ]
  }
};

function selfTest() {
  const now = new Date(protocolExample.generated_at);
  const good = check(protocolExample, now);
  assert.equal(good.share.success, true, 'protocol Payload example must match schema');
  assert.deepEqual(good.plausibilityIssues, []);
  assert.equal(good.shareAccepted, true);
  assert.equal(good.shareStatus, 200);
  const bad = JSON.parse(JSON.stringify(protocolExample));
  bad.days[0].cached = bad.days[0].input + 1;
  const refused = check(bad, now);
  assert.equal(refused.share.success, true, 'implausible example must still pass shape');
  assert.equal(refused.shareAccepted, false);
  assert.equal(refused.shareStatus, 422);
  assert.deepEqual(refused.plausibilityIssues, ['days[2026-09-10]: cached exceeds input']);
  const report = { schema: 1, client: protocolExample.client,
    html_gz: gzipSync(Buffer.from('<!doctype html><title>synthetic</title>'), { mtime: 0 }).toString('base64') };
  assert.equal(check(report, now).report.success, true, 'report schema example');
  assert.equal(check({ ...report, html_gz: '!' }, now).report.success, false, 'bad report base64');
  assert.equal(check({}, now).shareStatus, 400, 'invalid shape must be refused');
  process.stdout.write('[PASS] protocol Payload example: schema and plausibility checks\n');
  process.stdout.write('[PASS] cached > input: 422 days[2026-09-10]: cached exceeds input\n');
  process.stdout.write('[PASS] report schema: valid gzip/base64 accepted, invalid base64 refused\n');
}

const args = process.argv.slice(2);
if (args.length === 1 && args[0] === '--self-test') {
  selfTest();
} else {
  let now = new Date();
  if (args.length) {
    if (args.length !== 2 || args[0] !== '--now') throw new Error('Usage: check.mjs [--now ISO | --self-test]');
    now = new Date(args[1]);
    if (!Number.isFinite(now.getTime())) throw new Error('--now must be a valid ISO instant');
  }
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of lines) {
    let payload;
    try { payload = JSON.parse(line); }
    catch {
      process.stdout.write(JSON.stringify({ error: 'invalid_json' }) + '\n');
      continue;
    }
    process.stdout.write(JSON.stringify(check(payload, now)) + '\n');
  }
}
