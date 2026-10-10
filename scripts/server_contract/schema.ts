import { z } from 'zod';

// The share payload, version 1. docs/share-protocol.md is the prose version of this file;
// change both together.

export const LIMITS = {
  maxBodyBytes: 2_000_000,
  maxDays: 4000,
  maxSessions: 3000,
  /** Codex CLI shipped in April 2025; nothing can predate it. */
  earliestDay: '2025-04-01',
  /** Far above anything observed (the plugin's own corpus peaks near 0.5B a day). */
  maxDayInput: 20_000_000_000,
  maxSessionActiveS: 30 * 86_400,
  maxSessionSpanS: 400 * 86_400,
  maxWindows: 1000,
  maxWindowSplitRows: 50,
  maxWindowSplitRowsTotal: 1000,
  maxLatencyTierGroups: 50,
  /** Model and effort pairs in a latency summary. The plugin's own table shows twelve. */
  maxLatencyGroups: 50,
  /** The plugin times the last 30 days; this leaves room for it to send a quarter. */
  maxLatencySpanDays: 92,
  /** The plugin leaves out a response over an hour, and a turn over two: its start is not its own. */
  maxResponseS: 3600,
  maxTurnS: 7200,
  /** A report page, gzipped: under Firestore's 1 MiB document limit with room to spare. */
  maxReportGzBytes: 900_000,
  /** The same page unzipped. The plugin's pages run to a few hundred KB. */
  maxReportBytes: 8_000_000,
} as const;

const count = z.number().int().nonnegative();
const day = z.iso.date();
const instant = z.iso.datetime({ offset: true });
const percent = z.number().min(0).max(100);

export const TIER_CLASSES = ['standard', 'fast', 'ultrafast'] as const;

export const TierDayCountsSchema = z.object({
  responses: count,
  input: count,
  cached: count,
  output: count,
  reasoning: count,
});

export const DayTiersSchema = z.object({
  standard: TierDayCountsSchema.optional(),
  fast: TierDayCountsSchema.optional(),
  ultrafast: TierDayCountsSchema.optional(),
});

export const DaySchema = z.object({
  date: day,
  responses: count,
  input: count,
  cached: count,
  output: count,
  reasoning: count,
  sessions: count,
  tiers: DayTiersSchema.optional(),
});

export const SessionSchema = z.object({
  id: z.string().regex(/^[0-9a-f]{12,64}$/, 'a lowercase hex hash'),
  day,
  start: instant,
  end: instant,
  active_s: count,
  responses: count,
  input: count,
  cached: count,
  output: count,
  reasoning: count,
  model: z.string().min(1).max(80).nullish(),
});

export const WindowSplitRowSchema = z.object({
  model: z.string().min(1).max(80),
  tier: z.enum(TIER_CLASSES),
  responses: count,
  input: count,
  cached: count,
  output: count,
  /** Subsets of uncached input, priced at Claude's cache creation rates. */
  cache_write_5m: count.optional(),
  cache_write_1h: count.optional(),
});

/**
 * One rate-limit window as Codex logged it: the plan and the share of the limit the server
 * reported (first and highest reading), beside the tokens the plugin counted in the window.
 */
export const WindowSchema = z.object({
  start: instant,
  window_minutes: z.number().int().positive(),
  plan: z.string().min(1).max(40).nullish(),
  first_pct: percent,
  peak_pct: percent,
  responses: count,
  input: count,
  cached: count,
  output: count,
  split: z.array(WindowSplitRowSchema)
    .max(LIMITS.maxWindowSplitRows, 'at most 50 model/tier rows per window')
    .optional(),
});

// Non-negative rather than positive: the plugin rounds, and a real but tiny figure arrives as 0.
const seconds = z.number().nonnegative();
const share = z.number().min(0).max(1);

/** How long responses (or turns) took: how many were timed, and their median and 90th percentile. */
const TimingSchema = z.object({
  n: z.number().int().positive(),
  median_s: seconds,
  p90_s: seconds,
});

/**
 * One model and reasoning effort's response times. The last three are the plugin's estimate,
 * from a line fitted under its fastest tenth of responses, and null when it had too few
 * responses to fit one.
 */
export const LatencyGroupSchema = TimingSchema.extend({
  model: z.string().min(1).max(80),
  effort: z.string().min(1).max(40),
  /** Seconds a response takes on top of its tokens, on the fitted line. */
  overhead_s: z.number().nonnegative().nullish(),
  /** Output tokens a second, on the fitted line. */
  output_tps: z.number().nonnegative().nullish(),
  /** The share of these responses' time above the fitted line. */
  above_share: share.nullish(),
});

export const LatencyTierGroupSchema = LatencyGroupSchema.extend({
  tier: z.enum(TIER_CLASSES),
});

/**
 * Responses that started in one UTC hour of the day or day of the week: how many, their median,
 * and their median time above the plugin's fitted line (over those it had one for).
 */
const ClockSchema = z.object({
  n: z.number().int().positive(),
  median_s: seconds,
  median_above_s: z.number().nonnegative().nullish(),
});

/**
 * Response and turn times from the plugin's timing of the sharer's recent responses. No tool
 * names. The hours and days of the week are UTC's; the server checks them but keeps neither: they
 * say when a sharer works. They are named for it, because the report's own --json carries an
 * `hours` of the same shape in the machine's local time, which a straight copy would send.
 */
export const LatencySchema = z.object({
  /** The sharer's local days the timed responses span. */
  from: day,
  to: day,
  responses: TimingSchema,
  // The plugin's summary of no turns is { n: 0, median_s: null, ... }: read as none.
  turns: z.preprocess((t) => (typeof t === 'object' && t !== null && (t as { n?: unknown }).n === 0 ? null : t),
    TimingSchema.extend({
      /** The share of turn time the model spent responding; the rest is tools and approvals. */
      model_share: share.nullish(),
    }).nullish()),
  groups: z.array(LatencyGroupSchema).max(LIMITS.maxLatencyGroups),
  tier_groups: z.array(LatencyTierGroupSchema)
    .max(LIMITS.maxLatencyTierGroups, 'at most 50 tier groups')
    .optional(),
  plan: z.string().min(1).max(40).nullish(),
  /** Each UTC hour of the day, 0 to 23, with a timed response. */
  utc_hours: z.array(ClockSchema.extend({ hour: z.number().int().min(0).max(23) })).max(24).optional(),
  /** Each UTC day of the week, 1 (Monday) to 7 (Sunday), with a timed response. */
  utc_weekdays: z.array(ClockSchema.extend({ day: z.number().int().min(1).max(7) })).max(7).optional(),
});

// Unknown keys are stripped rather than rejected, so a newer plugin can send more than this
// server reads. A change that alters the meaning of an existing field bumps `schema`.
export const SharePayloadSchema = z.object({
  schema: z.literal(1),
  client: z.object({
    name: z.string().min(1).max(40),
    version: z.string().min(1).max(40),
  }),
  handle: z.string().max(64).nullish(),
  generated_at: instant,
  days: z.array(DaySchema).max(LIMITS.maxDays),
  sessions: z.array(SessionSchema).max(LIMITS.maxSessions),
  /** Sent by token-counter 1.2.0 and later. Absent: the stored windows are left as they are. */
  windows: z.array(WindowSchema)
    .max(LIMITS.maxWindows)
    .refine(
      (windows) => windows.reduce((n, w) => n + (w.split?.length ?? 0), 0)
        <= LIMITS.maxWindowSplitRowsTotal,
      { message: 'at most 1000 window split rows in total' },
    )
    .optional(),
  /** Sent by token-counter 1.7.0 and later. Absent: the stored response times are left as they are. */
  latency: LatencySchema.optional(),
});

/**
 * A report page: the plugin's own HTML report, rendered for sharing, gzipped and base64'd so
 * it travels as JSON like everything else. The page is served as is, in a sandbox.
 */
export const ReportPayloadSchema = z.object({
  schema: z.literal(1),
  client: z.object({
    name: z.string().min(1).max(40),
    version: z.string().min(1).max(40),
  }),
  html_gz: z.string().min(1).max(Math.ceil(LIMITS.maxReportGzBytes / 3) * 4)
    .regex(/^[A-Za-z0-9+/]+={0,2}$/, 'base64'),
});

// Nerf Watch: one model's result on the benchmark suite for one UTC day, as the scheduled
// runner sends it to POST /api/nerfwatch/ingest. docs/nerfwatch.md is the prose version.

export const NERFWATCH_LIMITS = {
  /** A run is a few KB; even the per-test list stays far below this. */
  maxBodyBytes: 256_000,
  maxCategories: 50,
  maxTests: 1000,
  /** Nothing older is worth backfilling: the site predates no benchmark run. */
  earliestDay: '2025-01-01',
  /** How far `ran_at`'s UTC day may sit from `day`: a run that starts late finishes tomorrow. */
  maxRunSkewDays: 1,
} as const;

/** A score is a percentage of the suite (or of a category or test) passed: 0 to 100. */
const score = z.number().min(0).max(100);
/** Model and category ids. A model's is its document id, so never `.` or `..`, and URL-safe. */
export const NERFWATCH_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const slug = z.string().regex(NERFWATCH_ID, 'lowercase letters, digits, ".", "_" or "-"');

export const NerfwatchCategorySchema = z.object({
  id: slug,
  score,
  passed: count.optional(),
  total: count.optional(),
});

export const NerfwatchTestSchema = z.object({
  id: z.string().min(1).max(120),
  /** The id of one of the run's categories. */
  category: slug.nullish(),
  passed: z.boolean(),
  /** For a graded test; a pass/fail one leaves it out. */
  score: score.optional(),
});

export const NerfwatchRunSchema = z.object({
  schema: z.literal(1),
  /** Scores are compared only within one suite version: a new suite is a new baseline. */
  suite_version: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$/, 'a version like 2026.10.1'),
  model: z.object({
    /** `gpt-6-sol`, `claude-opus-5.5`: the same id every day, or it starts a new history. */
    id: slug,
    name: z.string().min(1).max(80),
    provider: z.string().min(1).max(40),
  }),
  /** The UTC day the run counts for. One run per model and day: a second one replaces it. */
  day,
  ran_at: instant,
  score,
  categories: z.array(NerfwatchCategorySchema).max(NERFWATCH_LIMITS.maxCategories).optional(),
  tests: z.array(NerfwatchTestSchema).max(NERFWATCH_LIMITS.maxTests).optional(),
});

export type DayInput = z.infer<typeof DaySchema>;
export type TierClass = (typeof TIER_CLASSES)[number];
export type TierCounts = z.infer<typeof DayTiersSchema>;
export type WindowSplitInput = z.infer<typeof WindowSplitRowSchema>;
export type LatencyTierGroupInput = z.infer<typeof LatencyTierGroupSchema>;
export type SessionInput = z.infer<typeof SessionSchema>;
export type WindowInput = z.infer<typeof WindowSchema>;
export type LatencyInput = z.infer<typeof LatencySchema>;
export type SharePayload = z.infer<typeof SharePayloadSchema>;
export type ReportPayload = z.infer<typeof ReportPayloadSchema>;
export type NerfwatchRun = z.infer<typeof NerfwatchRunSchema>;
