import {
  LIMITS, NERFWATCH_LIMITS, NerfwatchRunSchema, TIER_CLASSES,
  type LatencyInput, type NerfwatchRun, type SharePayload, type TierCounts, type WindowSplitInput,
} from './schema.js';
import type { Counts } from './types.js';

// Shape is checked by the schema; this checks that the numbers could have come from the
// plugin. It cannot prove a report is honest -- every figure is self-reported -- but it
// rejects the arithmetic no real log can produce, and the obviously invented.

const DAY_MS = 86_400_000;

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** Problems with the payload, or an empty list. `now` bounds how recent a day may be. */
export function plausibilityIssues(p: SharePayload, now: Date): string[] {
  const issues: string[] = [];
  // A day in UTC+14 is already tomorrow in UTC.
  const latestDay = isoDay(new Date(now.getTime() + DAY_MS));
  const latestInstant = now.getTime() + 10 * 60_000;

  const seenDays = new Set<string>();
  let dayInput = 0;
  for (const d of p.days) {
    const at = `days[${d.date}]`;
    if (seenDays.has(d.date)) issues.push(`${at}: duplicate date`);
    seenDays.add(d.date);
    if (d.date < LIMITS.earliestDay) issues.push(`${at}: before ${LIMITS.earliestDay}`);
    if (d.date > latestDay) issues.push(`${at}: in the future`);
    if (d.responses < 1) issues.push(`${at}: a day with usage needs at least one response`);
    if (d.cached > d.input) issues.push(`${at}: cached exceeds input`);
    if (d.reasoning > d.output) issues.push(`${at}: reasoning exceeds output`);
    if (d.input > LIMITS.maxDayInput) issues.push(`${at}: input above ${LIMITS.maxDayInput}`);
    if (d.sessions > d.responses) issues.push(`${at}: more sessions than responses`);
    issues.push(...tierIssues(at, d.tiers, d));
    dayInput += d.input;
  }

  const seenSessions = new Set<string>();
  let sessionInput = 0;
  for (const s of p.sessions) {
    const at = `sessions[${s.id.slice(0, 12)}]`;
    if (seenSessions.has(s.id)) issues.push(`${at}: duplicate id`);
    seenSessions.add(s.id);
    const start = Date.parse(s.start);
    const end = Date.parse(s.end);
    const span = (end - start) / 1000;
    if (end < start) issues.push(`${at}: ends before it starts`);
    if (span > LIMITS.maxSessionSpanS) issues.push(`${at}: span above ${LIMITS.maxSessionSpanS}s`);
    if (end > latestInstant) issues.push(`${at}: ends in the future`);
    if (s.active_s > span + 1) issues.push(`${at}: active time exceeds its span`);
    if (s.active_s > LIMITS.maxSessionActiveS) {
      issues.push(`${at}: active time above ${LIMITS.maxSessionActiveS}s`);
    }
    if (s.day < LIMITS.earliestDay || s.day > latestDay) issues.push(`${at}: day out of range`);
    if (!seenDays.has(s.day)) issues.push(`${at}: its day is not among the reported days`);
    if (s.responses < 1) issues.push(`${at}: a session needs at least one response`);
    if (s.cached > s.input) issues.push(`${at}: cached exceeds input`);
    if (s.reasoning > s.output) issues.push(`${at}: reasoning exceeds output`);
    sessionInput += s.input;
  }
  // The sessions sent are a subset of the days' usage, so they cannot add up to more.
  if (sessionInput > dayInput) issues.push('sessions: more input than all days combined');

  const earliest = Date.parse(`${LIMITS.earliestDay}T00:00:00Z`);
  const seenWindows = new Set<number>();
  let windowInput = 0;
  for (const w of p.windows ?? []) {
    const at = `windows[${w.start}]`;
    const start = Date.parse(w.start);
    if (seenWindows.has(start)) issues.push(`${at}: duplicate start`);
    seenWindows.add(start);
    if (start < earliest) issues.push(`${at}: before ${LIMITS.earliestDay}`);
    if (start > latestInstant) issues.push(`${at}: starts in the future`);
    if (w.first_pct > w.peak_pct) issues.push(`${at}: first reading above its peak`);
    if (w.cached > w.input) issues.push(`${at}: cached exceeds input`);
    if (w.input + w.output > 0 && w.responses < 1) issues.push(`${at}: tokens without responses`);
    issues.push(...splitIssues(at, w.split, w));
    windowInput += w.input;
  }
  // Windows are cut from the same responses as the days, so they cannot hold more either.
  if (windowInput > dayInput) issues.push('windows: more input than all days combined');

  if (p.latency) issues.push(...latencyIssues(p.latency, p.days, latestDay));

  return issues.slice(0, 50);
}

function tierIssues(at: string, tiers: TierCounts | undefined, totals: Counts): string[] {
  const issues: string[] = [];
  const keys = ['responses', 'input', 'cached', 'output', 'reasoning'] as const;
  const sums: Counts = { responses: 0, input: 0, cached: 0, output: 0, reasoning: 0 };
  for (const tier of TIER_CLASSES) {
    const row = tiers?.[tier];
    if (!row) continue;
    const where = `${at}.tiers[${tier}]`;
    if (row.cached > row.input) issues.push(`${where}: cached exceeds input`);
    if (row.reasoning > row.output) issues.push(`${where}: reasoning exceeds output`);
    if (row.input + row.output > 0 && row.responses < 1) issues.push(`${where}: tokens without responses`);
    for (const key of keys) sums[key] += row[key];
  }
  for (const key of keys) {
    if (sums[key] > totals[key]) issues.push(`${at}.tiers: more ${key} than day total`);
  }
  return issues;
}

function splitIssues(
  at: string,
  split: readonly WindowSplitInput[] | undefined,
  totals: Pick<Counts, 'responses' | 'input' | 'cached' | 'output'>,
): string[] {
  const issues: string[] = [];
  const seen = new Set<string>();
  const keys = ['responses', 'input', 'cached', 'output'] as const;
  const sums = { responses: 0, input: 0, cached: 0, output: 0 };
  for (const row of split ?? []) {
    const where = `${at}.split[${row.model}/${row.tier}]`;
    const key = JSON.stringify([row.model, row.tier]);
    if (seen.has(key)) issues.push(`${where}: duplicate model and tier`);
    seen.add(key);
    if (row.cached > row.input) issues.push(`${where}: cached exceeds input`);
    if ((row.cache_write_5m ?? 0) + (row.cache_write_1h ?? 0) > Math.max(0, row.input - row.cached)) {
      issues.push(`${where}: cache creation exceeds uncached input`);
    }
    if (row.input + row.output > 0 && row.responses < 1) issues.push(`${where}: tokens without responses`);
    for (const field of keys) sums[field] += row[field];
  }
  for (const key of keys) {
    if (sums[key] > totals[key]) issues.push(`${at}.split: more ${key} than window total`);
  }
  return issues;
}

function tierGroupIssues(l: LatencyInput): string[] {
  const issues: string[] = [];
  const seen = new Set<string>();
  let grouped = 0;
  for (const g of l.tier_groups ?? []) {
    const at = `latency.tier_groups[${g.model}/${g.effort}/${g.tier}]`;
    const key = JSON.stringify([g.model, g.effort, g.tier]);
    if (seen.has(key)) issues.push(`${at}: duplicate model, effort and tier`);
    seen.add(key);
    issues.push(...timingIssues(at, g, LIMITS.maxResponseS));
    grouped += g.n;
  }
  if (grouped > l.responses.n) issues.push('latency.tier_groups: more responses than latency.responses');
  return issues;
}

function timingIssues(at: string, t: { median_s: number; p90_s: number }, cap: number): string[] {
  const out: string[] = [];
  if (t.median_s > t.p90_s) out.push(`${at}: median above p90`);
  if (t.p90_s > cap) out.push(`${at}: p90 above ${cap}s, which the plugin leaves out`);
  return out;
}

// Only what follows from how the plugin times responses (section 5.8 of token-counter's
// ARCHITECTURE.md): the fitted figures are left alone, since a nearly flat line gives a real,
// enormous output rate.
function latencyIssues(l: LatencyInput, days: SharePayload['days'], latestDay: string): string[] {
  const issues: string[] = [];
  if (l.from > l.to) issues.push('latency: from is after to');
  if (l.from < LIMITS.earliestDay) issues.push(`latency: from is before ${LIMITS.earliestDay}`);
  if (l.to > latestDay) issues.push('latency: to is in the future');
  const span = (Date.parse(l.to) - Date.parse(l.from)) / DAY_MS + 1;
  if (span > LIMITS.maxLatencySpanDays) issues.push(`latency: spans over ${LIMITS.maxLatencySpanDays} days`);
  issues.push(...timingIssues('latency.responses', l.responses, LIMITS.maxResponseS));
  // Every timed response is a response the days counted.
  const dayResponses = days.reduce((n, d) => n + d.responses, 0);
  if (l.responses.n > dayResponses) issues.push('latency.responses: more than all days combined');
  if (l.turns) {
    issues.push(...timingIssues('latency.turns', l.turns, LIMITS.maxTurnS));
    // A turn is timed to its last timed response, so each holds at least one.
    if (l.turns.n > l.responses.n) issues.push('latency.turns: more turns than responses');
  }
  const seen = new Set<string>();
  let grouped = 0;
  for (const g of l.groups) {
    const at = `latency.groups[${g.model}/${g.effort}]`;
    const key = JSON.stringify([g.model, g.effort]);
    if (seen.has(key)) issues.push(`${at}: duplicate model and effort`);
    seen.add(key);
    issues.push(...timingIssues(at, g, LIMITS.maxResponseS));
    grouped += g.n;
  }
  // The groups split the timed responses, so they cannot add up to more.
  if (grouped > l.responses.n) issues.push('latency.groups: more responses than latency.responses');
  issues.push(...tierGroupIssues(l));
  issues.push(...clockIssues('latency.utc_hours', (l.utc_hours ?? []).map((h) => ({ ...h, key: h.hour })), l.responses.n));
  issues.push(...clockIssues('latency.utc_weekdays', (l.utc_weekdays ?? []).map((d) => ({ ...d, key: d.day })), l.responses.n));
  return issues;
}

/** Hours of the day, or days of the week: each once, and together no more than the responses. */
function clockIssues(at: string, buckets: { key: number; n: number; median_s: number }[], responses: number): string[] {
  const issues: string[] = [];
  const seen = new Set<number>();
  let n = 0;
  for (const b of buckets) {
    if (seen.has(b.key)) issues.push(`${at}[${b.key}]: duplicate`);
    seen.add(b.key);
    if (b.median_s > LIMITS.maxResponseS) issues.push(`${at}[${b.key}]: median above ${LIMITS.maxResponseS}s`);
    n += b.n;
  }
  if (n > responses) issues.push(`${at}: more responses than latency.responses`);
  return issues;
}

/**
 * Problems with a Nerf Watch run, or an empty list. The runner is the owner's own, so this is
 * not about honesty but about a bug in it storing a day it never ran, or figures that
 * contradict each other. `now` bounds how recent the run may be.
 */
export function runIssues(r: NerfwatchRun, now: Date): string[] {
  const issues: string[] = [];
  // Runs are filed by UTC day, so unlike a sharer's local day, today is the latest.
  if (r.day > isoDay(now)) issues.push('day: in the future');
  if (r.day < NERFWATCH_LIMITS.earliestDay) issues.push(`day: before ${NERFWATCH_LIMITS.earliestDay}`);
  const ranAt = Date.parse(r.ran_at);
  if (ranAt > now.getTime() + 10 * 60_000) issues.push('ran_at: in the future');
  const skew = Math.abs(Date.parse(isoDay(new Date(ranAt))) - Date.parse(r.day)) / DAY_MS;
  if (skew > NERFWATCH_LIMITS.maxRunSkewDays) {
    issues.push(`ran_at: more than ${NERFWATCH_LIMITS.maxRunSkewDays} day from day`);
  }

  const categories = new Set<string>();
  for (const c of r.categories ?? []) {
    const at = `categories[${c.id}]`;
    if (categories.has(c.id)) issues.push(`${at}: duplicate id`);
    categories.add(c.id);
    if ((c.passed === undefined) !== (c.total === undefined)) issues.push(`${at}: passed and total go together`);
    if (c.passed !== undefined && c.total !== undefined && c.passed > c.total) issues.push(`${at}: passed exceeds total`);
  }
  const tests = new Set<string>();
  for (const t of r.tests ?? []) {
    const at = `tests[${t.id.slice(0, 40)}]`;
    if (tests.has(t.id)) issues.push(`${at}: duplicate id`);
    tests.add(t.id);
    if (t.category && !categories.has(t.category)) issues.push(`${at}: its category is not among the categories`);
  }
  return issues.slice(0, 50);
}

/** A run checked for storing: the run, or why not, as the API answers it. */
export type RunCheck =
  | { ok: true; run: NerfwatchRun }
  | { ok: false; status: 400 | 422; code: 'invalid_payload' | 'implausible'; message: string; issues: string[] };

/**
 * Everything a Nerf Watch run must pass before `ingestRun` stores it: its shape (a finite
 * score from 0 to 100, a suite version, a model id), then `runIssues`. The ingest endpoint
 * and an in-process scheduled runner both go through this.
 */
export function checkRun(raw: unknown, now: Date): RunCheck {
  const parsed = NerfwatchRunSchema.safeParse(raw);
  if (!parsed.success) {
    return {
      ok: false, status: 400, code: 'invalid_payload', message: 'the run does not match Nerf Watch schema 1',
      issues: parsed.error.issues.slice(0, 20).map((i) => `${i.path.join('.') || '(root)'}: ${i.message}`),
    };
  }
  const issues = runIssues(parsed.data, now);
  if (issues.length) {
    return { ok: false, status: 422, code: 'implausible', message: 'some figures in the run contradict each other', issues };
  }
  return { ok: true, run: parsed.data };
}
