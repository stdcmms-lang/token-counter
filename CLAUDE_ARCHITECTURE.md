# Token Counter for Claude Code — Architecture

This describes token-counter-claude 0.2.0. The 0.1.0 usage, capture and sharing
contracts remain in force; 0.2.0 adds the deterministic token-film skill.
Evidence is a snapshot of development corpora, not a guarantee about future
Claude Code records.

## 1. Scope and relationship to the Codex product

Claude's headline is recorded usage, not independently tokenized content. Its
visible composition is a UTF-8 byte inventory. The product has its own marketplace
entry, manifest, three skills, package version, state and endpoint-specific share
token. Codex remains `token-counter` 1.10.0 with its existing counting contract.

Four checked-in files are byte-identical across the products: `render.py`,
`latency.py`, `index.py` and `images.py`. `scripts/sync_shared.py` copies bytes from
Codex to Claude or checks them with `--check`, without following symlinks. Claude
owns extraction, its strict count validation, ledger, history, calendar, price
validation, window logic and share validation. Its adapters satisfy the shared
utilities' interfaces. No runtime import reaches the sibling Codex plugin.

The renderer's legacy defaults are protected by a frozen `render.py` from commit
`2aebba1`, isolated baseline imports and complete byte comparisons. The sensitivity
self-test appends exactly one byte only to the current renderer and must detect
that byte. No Codex runtime or test file changes in round 9.

There is no daemon, hook, MCP server, agent or continuous capture process. Normal
report/share collection runs capture evidence on demand. Python 3.8+ and its
standard library are the collection runtime requirements. Film playback needs
Canvas 2D, binary FontFace and Web Audio; MP4 export also needs supported
WebCodecs codecs. No tokenizer, vocabulary,
dependency installation or runtime price fetching exists in the Claude product.

## 2. Evidence and shape dispatch

The worker parses every complete JSONL object on both full and metrics-only paths.
Envelope and nested shapes select interpretation; recorded `version` never gates
parsing. A final fragment without LF is withheld. Malformed JSON, nonobjects,
unknown records and unsupported shapes have named counters. A file is restated
after extraction; a changed file is retried once, then its unstable snapshot cannot
replace prior capture.

The recorded response identity is `(message.id, requestId)`, requiring two nonempty
strings. Assistant usage, block indices, parent/UUID chains, continuation/fork
links, genuine prompt-side inputs, compaction facts, structured `/usage` limits,
explicit API-error status, tool intervals and logged turn durations are distinct
facts. They are not inferred from assistant text or the version string.

Synthetic model records and API-error records never charge usage. A real interrupted
or `truncatedAfterOutput` response retains valid partial counts but is excluded
from completed timing and new comparison windows. `max_tokens`, refusal and missing
stop reason do not by themselves imply interruption. A real zero-usage response is
still a response. Compaction boundaries and summaries are not usage; top-level usage
is authoritative over `iterations`. Advisor metadata does not manufacture a
separate response. Cost-state and `/usage` costs are diagnostics, never gap filling.

All committed test fixtures are invented. No real transcript excerpt, identity or
content is part of the fixtures or vendored server contract.

## 3. Transcript discovery and account allow-list

Discovery accepts only `<project>/<session>.jsonl` and
`<project>/<session>/subagents/<agent>.jsonl`. Unexpected JSONL layouts are counted
without opening them. `tool-results` directories and directory symlinks are skipped;
outside-root links are counted and not followed. External tool-result files, image
URLs and file references are never opened or fetched.

Let C be nonempty `CLAUDE_CONFIG_DIR`, otherwise `~/.claude`. Default transcripts
are `C/projects`. Account lookup is exactly `~/.claude.json` with the environment
variable unset, or `C/.claude.json` when set. An explicit `--sessions-root R` uses
`R.parent/.claude.json`, without falling back to live account metadata.

Only these `oauthAccount` string/null values are extracted: `organizationType`,
`organizationRateLimitTier`, `emailAddress`, `organizationName` and
`subscriptionCreatedAt`. Wrong types become unavailable with counters. No account
UUID, credentials, billing details, other settings or account cache is a usage
source. **Never open `~/.claude/.credentials.json` or other credentials files.**

Email, falling back to organization name, identifies a local report through the
existing escaped account slot. Public output, payloads, receipts and history omit
that identity. The ephemeral `AccountInfo` is separate from the five-field
content-free `AccountSnapshot`: observation time, organization type, rate-limit
tier, mapped current plan and parsed subscription creation epoch.

`--no-account` opens no account file and captures no new snapshot; old observations
remain usable. `--doctor` can inspect allow-listed account availability but does not
capture it. `--version` exits before collection. Deletion does not read account data.

## 4. Response identity, block collapse and ownership

Each source copy preserves physical block order. The last valid required usage
record supplies the entire terminal tuple; independent component maxima are never
combined. A changed prompt tuple conflicts, decreasing output is counted, and an
invalid terminal can leave a preceding valid lower bound marked partial. Stable
block identities deduplicate copies; missing block indices do not remove valid
response identity but can leave composition unavailable.

The continuation graph needs one root and no cycle. Fork-context links attach
subagent streams to their parent family. Ownership prefers the original ancestor
main stream, then related original work. The parent's inherited response stays
with the parent; the subagent's own work stays with its stream. An orphan subagent
retains its own valid usage and labels missing main evidence. Cyclic or incompatible
lineage can retain dated usage while withholding invented session attribution.

Matching nonzero terminal tuples charge once; an all-zero placeholder does not
replace a nonzero tuple. Conflicting nonzero counts or incompatible served models
quarantine the identity. Identical unrelated copies charge once, owned by earliest
source first-record timestamp, then source ID; absent timestamps sort last.
Historical source metadata preserves that rank when a file is shortened.

Copied/restamped evidence does not establish trustworthy response time. Ambiguous
original timing is marked and excluded from timing; an unassignable completion
date remains local undated usage and is omitted from shares. Live and retained
evidence are reconciled before date, session or live-only report filtering.

## 5. Canonical row schema and quality counters

`ResponseRow` is ordinary content-free data: response/source/family/stream keys,
recorded and frozen prompt/output timestamps, captured local day and midnight
boundaries, calendar signature, turn, served/canonical/requested model metadata,
effort, speed/tier, geography, optional usage measurements, partial/replay flags
and quality flags. Its `usage` holds integer whole input, cached reads, output,
nullable thinking and arithmetic total. Separate fields hold base input, creation,
5m/1h writes and recorded server-tool counts. Per-row plan fields remain null;
window/latency attribution is a span decision.

Required counts must be actual nonnegative JSON integers: booleans, strings,
fractional values and negatives are rejected. Invalid optional reasoning or TTL
details do not erase valid required usage. Thinking cannot exceed output; exact
TTL writes must sum to creation. Zero creation establishes an exact zero split.

Model normalization is closed: safe lowercase/hyphenated Claude IDs, the known
Haiku 4.5 dated alias and only the exact terminal `[1m]` modifier. Served model is
authoritative; requested model never substitutes. Unknown safe IDs remain named
and unpriced. Invalid metadata becomes `unknown`. Effort preserves `xhigh` and
`max`; disagreement with `perTurnEffort` makes effort unknown. Known recorded speed
classes are `standard` and `fast`; missing/unknown speed never creates Standard
wire attribution.

Counter names are catalogued in `models.COUNTER_NAMES`. Record, deduplicated response,
lineage, measurement and state counters have different scopes; subset reasons are
not additive independent losses. Excluded identities count once. Quality remains
in JSON and the sorted terminal summary; it is not a page panel. Archived canonical
responses are disclosed by the terminal's captured-history line.

## 6. Recorded prompt counts and visible-byte inventory

Whole input is base input + cache creation + cache reads. Reads are a subset of
whole input; creation is counted once. Cache hit divides reads by whole input.
Recorded output already contains thinking where known. Thinking is a nullable
subset, not another total; missing thinking makes reasoning a lower bound.

The fixed inventory order is saved system prompt, saved tool definitions,
instructions, skill listings, user text, tool arguments, tool results, assistant
text, readable thinking, compaction summaries and other known attachment text.
Arguments use compact sorted-key UTF-8 JSON while measured. Instructions/file
attachments use their specified content fields, edited files use snippets, and
unknown attachments are omitted with counters. There is no recursive extraction
of every string. Invalid Unicode makes that item's byte count unavailable without
discarding usage.

Message/block facts deduplicate by stable identities. Saved system/tool snapshots
deduplicate by family, category and body digest; tool definitions include only
name, description and schema. Deferred tool-input copies are saved state, not more
arguments. Facts retain only lengths, digests and observed times, bucketed hourly
for the viewport. History/index never retain text, arguments or image data.

Byte shares describe captured text and saved snapshots. They are not Claude token
shares or a reconstruction of the full API prompt. Prompt growth is a separate
diagnostic between consecutive prompts in a stream/context segment; compaction,
model changes and negative growth are counted/skipped as appropriate. It is never
distributed over content categories or converted from bytes to tokens.

## 7. Images and unavailable attribution metrics

Shared image header readers recover embedded dimensions without decoding whole
image payloads. Claude's estimator uses 28×28 patches, preserving aspect ratio,
with standard limits 1568 pixels/1568 patches and high-resolution limits
2576 pixels/4784 patches for known supported model generations. A binary search
finds the largest fitting edge after rounded short-edge and padded-patch checks.
Examples are 1456×819 → 1560 patches and 2576×1449 → 4784.

Prompt-side image facts use the first consuming response's known served model on
the parent chain; an unrelated previous model is not borrowed. Unknown model or
transformation leaves the estimate unavailable; unsupported sources are counted.
Images remain outside the text-byte pie and recorded input. Inferred visual counts
are local JSON only, excluded from public models.

`reconciliation`, `resend_cost`, `amplification` and `cache_leads` each carry
`available: false` and the reason "Claude transcripts do not establish per-content
token attribution." Report analytics make no residual, byte-to-token estimate,
resend-price, cache-prefix-lead or TTL-causation claim. The film's Cache expired!
sticker is an illustration selected by the documented cache-rewrite threshold,
not a measured cause.

## 8. Durable capture history and replacement coverage

`index-cache.db` is disposable; `history.db` is permanent captured evidence.
SQLite schema 1 has `meta`, `sources`, `response_copies`, `facts` and `submissions`.
Source/copy keys, frozen calendars, limits/events, tools/turns, compactions, byte
measurements/digests, diagnostic facts and account snapshots survive transcript
removal, shortening and incompatible revisions. No content, identity display,
image bytes or titles are retained.

Blobs contain zlib-compressed canonical UTF-8 JSON with `allow_nan=False` and checked
SHA-256. Round 4b compact positional response blobs and immutable per-source/kind
fact-list batches preserve the same safe projection and SQL schema. Legacy
dictionary/single-fact schema-1 blobs remain readable without rewriting unchanged
evidence. Tool references depend on independently retained immutable tool identities.
New databases use 16 KiB pages; old databases are not reset or migrated silently.

Every store opens with a full SQLite/blob/dependency integrity check. Verified
compressed bytes plus digest are cached only in that instance; changed bytes/checksum
cannot reuse a decision. `load()` returns freshly decoded plain data. An explicit
`check_integrity()` always performs a fresh full pass, including prepared payloads
and contributors. Unsupported schema, checksum failure, orphan evidence, broken
dependencies or uncommittable state disables sharing without clearing evidence.

Capture unions stable evidence, retaining absent responses/blocks and earlier
first-record evidence. A shortened source cannot roll its terminal back. A genuinely
later block can advance the whole tuple; incompatible evidence remains conflicting.
Derived content measurements can refresh without becoming usage revision conflicts.
Capture loads/builds once and returns the canonical pre-commit ledger; collection
commits it and then evaluates endpoint-specific coverage.

Default reports include archived capture; live-only is a view. `--rebuild` clears
only the index and re-extracts every live file before a nondeleting history merge;
`--no-cache` bypasses only the index. Failure allows a live lower-bound local report
with an explicit warning and refuses sharing.

Before replacing a month, all prior prepared/confirmed/unknown-outcome contributors,
nondecreasing recorded counts, frozen calendars and selected-session evidence must
be reproducible under the endpoint/token history binding. Unsafe months are omitted
whole. Selected sessions touching withheld months are excluded. The complete
previous window list/evidence must be reproduced or advanced; unsafe replacement
omits `windows` entirely, never an empty/pruned list. There is no automatic window
expiry. Reattribution may change a retained plan to null while preserving counts.

Receipts store the exact payload bytes and contributor snapshots before POST,
then confirmed/unknown status. An uncertain first POST without a returned token is
not retried. Deletion can use a retained token without reconstructing history;
successful authorized deletion retires its receipts and preserves capture.
Calendar completeness remains `unknown`; pre-capture losses cannot be recovered.

## 9. Sparse quota readings, observation spans and account observations

`weekly_all`, identity-specific `weekly_scoped` and five-hour `session` limits are
separate. Only weekly-all enters comparison shares. A qualifying rejected
`seven_day` 429 creates an event plus a sourced 100% `quota_429` weekly-all reading,
counted once under `limit_429_assumed_all_models`. A 429 without the requisite
rejection/reset can remain an event without a percentage; a 500 supplies neither.
The literal's all-model scope is an explicit unverified assumption.

Resets cluster by kind/scope with full min/max spread ≤2 seconds, not chained
neighbor distance. Initial median resets round to the nearest second, ties upward.
Established anchors persist; a reading fitting two established clusters is
ambiguous. An account's first weekly anchor supplies observed grid phase; departures
over two seconds are diagnostics, not snapping/rejection. Thursday 16:00Z was
observed in the development evidence and is not a universal grid.

The nominal start is reset minus seven days (five hours for session limits), an
inferred anchor. Chart counts cover `[nominal start, reset)`. Comparison counts
cover `(first reading, earliest reading attaining peak]`: at-first, before-first
and after-first-peak responses are excluded. Plateau readings remain sourced
points but do not extend the numerator. No 0% opening or percentage interpolation
is invented. JSON carries nominal and observation counts/endpoints.

New comparisons with insufficient observations, no positive delta, decreases,
same-time conflicts, ambiguous required timing, partial rows, unavailable nonzero
TTL splits or overlapping all-model spans are withheld. Missing speed remains in
totals outside recognized splits and can make the server estimator ineligible.
A positive delta below five points can be shared but is estimator-ineligible.
Unknown plan and pricing-difference diagnostics do not themselves withhold counts.

Personal plan mapping is Pro or known Max 5x/20x; a Pro type with a Max tier is
contradictory, unknown Max is null, and Team/Enterprise cannot establish a personal
Max plan. The normal Pro tier literal remains unverified; an unfamiliar Pro tier
is diagnostic while a consistent Pro organization type establishes Pro.

Plan P requires: a non-null captured observation for P; every observation at or
after the span start mapping to P, including observations after the span ended;
the latest predecessor if present also mapping to P; and the latest observation's
non-null creation date no later than the start. Older observations before that
latest predecessor do not independently veto. Otherwise plan/source are null.
Known-plan disagreement is counted once per attribution, not once per response.
Latency applies the same rule starting at the earliest accepted sample end.
`--no-account` adds no observation but leaves retained observations available.

A plan change that leaves subscriptionCreatedAt unchanged is not detectable until a later run observes the new tier. A window that ended before that run can therefore carry the old plan.

Whether the date moves on upgrade/downgrade is **UNVERIFIED**. These observations
are not recovered subscription history or independently verified plan records.

## 10. Response, stream-turn and tool timing

The parent chain supplies the latest genuine user/tool-result/prompt-text attachment
timestamp when the first output block arrives. Saved snapshots and metadata do not
move it. The frozen anchor is floored at previous charged end in the same stream
and completed compaction; a floor cannot create an absent anchor. End is the last
accepted usage block. A streaming response's later input cannot move its start.

A subagent starts from its own genuine task-prompt record, never its parent's
spawning call or inherited fork context. Parallel streams are independent. Partial
and copied/unverified timing is excluded; missing stop reason alone is accepted.
The measurement includes network, queue, processing, generation and retry together,
not isolated send/server latency.

Computed turns run from genuine user opener through last accepted stream response,
including accepted responses without an individual duration. Timed model intervals
sum without overlap and cap at turn duration. Tool call/result IDs match within
stream/parent chain; copied intervals deduplicate, parallel intervals remain
separate, and approval waits may be included. Logged `turn_duration` remains a
separate local measurement, never shared as computed turns or summed family wall time.

Shared `latency.py` supplies summaries and a nonnegative quantile fit at q=.1,
with ≥40 sufficiently varied samples and at most 4000 evenly selected samples.
Responses cap at 3600 seconds, tools/turns at 7200; nonpositive/over-cap intervals
are omitted and counted. Mixed groups use actual model/effort; tier groups add
known speed. Unknown speed enters mixed summaries only. Mixed/tier arrays are
independently ranked by total time and capped at 50. Daily timing uses retained
calendar assignments. UTC-hour and weekday arrays are absent from outgoing data.

Local timing covers the selected local view. Public timing and shares use the last
30 days. A local HTML/terminal report computes the private recent adapter only
when JSON or public output needs it; a share preview reuses its payload's identical
recent selection. Complete JSON and emitted pages/counts keep their existing data.

## 11. Anthropic list-price parsing and per-response value

The offline table is `assets/vendor/anthropic_prices.json`, schema 1, Anthropic,
USD per million tokens, as of **2026-10-10**, parsed from the supplied pricing
Markdown. It has 20 models, 21 price rows, three supported Fast records and a closed
dated alias. `fetch_anthropic_prices.py --from ... --check` verifies the offline
fixture; fetching is a separate developer action, never a runtime operation.

The loader rejects an invalid table whole, including nonfinite/negative rates,
wrong types, invalid thresholds/alternate records/aliases and duplicate keys.
Every canonical response is priced once from base input, exact/assumed 5m/1h
creation, cache reads and output. Thinking is already output. Accumulation is
unrounded, with displayed JSON dollars rounded to six decimals. Price-row results
are cached within one analysis, including session/day/model/window cross-checks.

Whole prompt including reads/writes selects Haiku 5.5's >100,000-token band; `[1m]`
has no surcharge. Supported Fast is 2×, and supported explicit-US multiplies token
components by 1.1. Opus 4.6's documented Fast fallback uses Standard with a flag.
Unknown nonempty speed, unsupported Fast or unsupported geography/model settings
are unpriced; safe unknown model IDs remain named.

Missing speed prices at Standard only locally, missing write TTL at all-5m,
missing/`not_available` geography at global and missing recorded searches at zero,
with assumption counters. `usd_high` combines supported Fast and all-1h scenarios
for missing speed/TTL; it does not bound unknown calls, models, search fees or future
settings, and missing geography creates no US upper scenario. Recorded billable
search counts add $0.01 each; fetch adds zero. Tool names cannot invent a fee.
Credits are not subtracted from gross list value; search-error ambiguity is counted.

`api_value` has availability/reason, default/high/token/search dollars, calls,
table provenance, priced/unpriced responses, model/reason diagnostics and assumption
counts. Cost-state and `/usage` list prices are separate cross-checks, never
substitutions for missing recorded usage. Background/compaction/advisor calls
without separately identifiable usage remain outside this value. It is not a bill.

Server Fast 2.5, Sonnet 5.5 cache-read $0.20 versus published $0.10 per million,
missing US modifier and missing long-prompt Haiku pricing are owner defects.
The collector sends true counts/models/recorded speeds and prints
`window_rows_fast`, `window_rows_us` and `window_rows_haiku_long` diagnostics;
it does not compensate or withhold otherwise shareable windows for those differences.

## 12. Report profiles and public sanitization

Claude's profile supplies title/kickers, one-line tile notes (response count,
thinking-token and cache-read totals, list-price wording, stream count and the
expired-reading text), the largest-session label and the UTF-8 inventory labels.
The page has no standing-notes block: the coverage, price and plan assumptions
live in this document, the JSON counters and the terminal summary. Legacy
`profile=None` remains byte-identical to the frozen Codex renderer. Clinical,
Matisse and Nocturne share the same data, viewport and interactions. Sparse quota
marks stay points; the inferred nominal start is a dashed boundary, and the share's
observation span (§9, §13) is a JSON and share fact that the chart does not draw.
The limit metric
selector opens on recorded tokens and can show API list value, both across the
nominal interval; it is not a new panel. Curves are bounded/downsampled to 120
points per window while sourced reading points are retained. No quality/history
panel exists. Details and sorted counters stay in JSON/terminal output.

`public_model()` builds an allow-list: aggregate counts/days/models, sanitized
sessions, byte categories/buckets, weekly chart/window fields, limit events,
recent timing, value/quality/coverage and logged-duration aggregates. Raw identities,
paths, account email/name, tools' names, inferred image facts and detailed cost-state
diagnostics are removed. Public model metadata uses the safe Claude model grammar;
effort/tier labels are closed. Local account display is escaped; public terminal
output receives the sanitized model too.

The exact navigational anchor `<a href="https://tokenusage.dev">tokenusage.dev</a>`
appears once. Other external references and runtime src/import/fetch/XHR/WebSocket/
beacon/form primitives are prohibited. Price provenance is plain text. Rendering
and opening a report makes no network request; following the brand link is a user
navigation. No browser is needed for the page tests.

Film pages use a separate public projection. They retain no raw identities,
paths, content or account data. Their tool labels use the closed film
allow-list; MCP and custom labels become MCP and tool. Report public_model()
and share sanitization remain unchanged.

## 13. Share schema1 mapping and server limitations

Schema 1 with client `claude-usage` 0.2.0 carries whole safe captured months of
recorded daily counts and known-speed partial tier partitions, selected session
hashes, a retained-safe union of weekly-all observation windows and 30-day timing.
Session IDs are the first 16 hex characters of domain-separated SHA-256 over family
identity. Session start/end are captured charged-response ends; active seconds sum
unique consecutive end gaps of at most 1800 seconds, rounded to an integer. Model
is dominant by input, with lexical tie-break. Per-month selection unions top ten
active and top ten token sessions. Global selection prefers newer first-response
months and is bounded at 3000 sessions. Invalid spans/active time are omitted,
never clamped.

The budget is 4000 days, 1000 windows, 50 split rows/window, 1000 split rows overall,
independent 50 timing groups and a 2,000,000-byte payload. Oversized months are
withheld whole and an oversized retained window union omits the entire key.
Reasoning sums only known thinking. Unknown speed remains outside `tiers`/`split`
and is disclosed by deduplicated shared-response count. Actual `xhigh`/`max` labels
remain intact. No `api_usd`/`api_value` extension, UTC-hour or weekday data is sent.

Dry runs capture/commit history, print coverage decisions and diagnostics, optionally
write payload/preview, make zero requests and create no token. A send validates
locally, checks integrity, durably prepares exact payload/contributors, checks
integrity again, makes one POST, saves token atomically, confirms the receipt and
then PUTs the exact public preview bytes. The deterministic gzip uses mtime 0;
report limits are 8,000,000 uncompressed and 900,000 gzip bytes, with doctype checks.
Preview failure is distinguished from already successful counts. A POST outcome
without a usable returned token is not automatically retried.

After successful counts, an unsaved token is reported as preventing update/delete
from this machine; a saved token with failed confirmation is reported as a receipt
failure. The receipt stays prepared. Only a failure before POST claims no further
request was made. Successful public deletion removes the endpoint's local token
entry even if receipt retirement fails, then reports that failure with exit 5.
Network mutations honour environment proxies, refuse redirects and keep tokens
endpoint-specific. Lost-history deletion requires no replacement collection.

The server fixture is immutable commit `9afabc0`, with raw-byte SHA-256 provenance,
Zod **4.6.5** and TypeScript **5.9.3** exact lockfile pins. Its harness executes
`SharePayloadSchema.safeParse`, `plausibilityIssues` and
`ReportPayloadSchema.safeParse` after in-memory TypeScript transpilation. **It is
not the HTTP route.** Client identity, handle syntax, 16-hex session identity,
weekly duration, payload body size and report gzip/doctype clauses are stricter
local-only checks relative to this harness. The parity suite records that limitation
explicitly; passing it does not verify route authentication, storage, deployment,
live estimator behavior or actual transport.

The schema-1 Claude observation-span meaning below is an **owner decision,
accepted by the owner on 2026-10-11**. Codex's meanings remain established.
`docs/share-protocol.md` belongs to the tokenusage.dev repository and is not
created here; these exact amendment paragraphs from owner questions 1–5 are
the accepted text for that protocol document under schema 1:

> For clients `claude-usage` and `claude-usage-example`, a weekly-all entry describes a captured observation interval within one reset cluster. `start` is the nominal seven-day quota anchor inferred as the canonical reset minus seven days; it is not proof that the quota opened at that instant. `window_minutes` is 10080. `first_pct` is the earliest sourced percentage and `peak_pct` is the highest sourced percentage. Responses and token counts include only captured canonical responses ending strictly after the first observation and at or before the earliest observation attaining the peak. Repeated plateau readings do not extend this interval. Missing endpoints, declining or conflicting readings, partial response usage and unavailable nonzero cache-write TTL splits are not submitted as new comparison windows. Missing recorded speed remains unknown; those responses stay in totals but outside recognized splits.

> A rejected Claude weekly 429 with `rateLimitType: "seven_day"` supplies a 100% weekly-all reading and a refusal event. The collector currently assumes that this internal literal denotes the all-model limit, counts that assumption, and retains its source as `quota_429`.

> Schema1 retains established Codex meanings. This amendment defines the Claude collector’s vendor-specific window semantics before a production Claude collector uses them. Subsequent changes to either vendor’s established meanings require a schema change.

> The `token-counter-claude` plugin collects captured Claude Code transcript usage and shares with client name `claude-usage`. Claude plan IDs are `claude:pro`, `claude:max-5x` and `claude:max-20x`. Each account-reading report or share captures a content-free observation containing observation time, organization type, rate-limit tier, mapped plan and parsed subscription creation date.

> A Claude window receives plan P only when at least one captured account observation maps to non-null P; every observation at or after the window’s first reading maps to P; the latest observation before that reading, if present, maps to P; and the latest observation’s subscription creation date is present and no later than the first reading. Otherwise its plan is null. Later observations are considered even when they occur after the window ended.

> Claude latency uses the same rule with the earliest accepted sample end as the attribution start. Missing creation date, conflicting or null applicable plan observations produce null. Effort labels remain unchanged; `xhigh` and `max` are not `high`.

> A plan change that leaves `subscriptionCreatedAt` unchanged is not detectable until a later collector run observes the new tier. A window ending before that run can carry the old plan. Whether the date changes on upgrade or downgrade is unverified.

> Claude shares contain captured usage, which may omit transcripts removed before first capture and calls without transcript usage. Calendar completeness is unknown. The collector retains content-free history and every prepared or confirmed submission’s contributors. It replaces a month only when all previously submitted captured usage can be reproduced. Unsafe months are omitted. Because `windows` replaces the whole stored list, an unsafe replacement omits the key rather than submitting an empty or pruned list.

> Claude input is base input plus cache creation plus cache reads. Reads are a subset of that total. `cache_write_5m` and `cache_write_1h` are disjoint subsets of uncached input. Thinking is a recorded output subset and is never added again. Unavailable thinking makes shared reasoning a lower bound. Claude tiers contain only known recorded speed classes; absent speed is not asserted Standard, so day and window partitions can be partial.

> Pricing unrecorded speed at Standard for a local API-value comparison does not establish a recorded Standard class and does not populate share partitions.

> Claude response time is a transcript-record interval: latest known prompt-side timestamp frozen at first output, floored at previous response end in the same stream and completed compaction, through the last accepted block. A subagent starts from its own task-prompt record, not its parent’s spawning tool call. Parallel streams are independent. Explicit partial responses are excluded. Shared turns are computed stream turns; logged `turn_duration` values remain separate local measurements.

The current estimator's eligibility also depends on known catalog models,
sufficient delta, complete recognized splits and plan compatibility. Quota deltas
can include other clients/devices: a captured-token/quota-delta estimate is the
observed client's contribution under coverage/pricing assumptions, not a total
account token quota or guaranteed subscription allowance.

## 14. Paths, cache fingerprints and installed copies

Default state is `C/token-counter/`. Explicit roots use
`R.parent/token-counter/<root-hash>/`, first 12 hex SHA-256 characters of the
resolved platform-normalized root path. State files are `index-cache.db`,
`history.db`, `report.html`, `report-shared.html`, `claude-share.json` and
`films/film-<public-data-hash>-v0.2.0.html`. No mutable
state is in the installed directory or `${CLAUDE_PLUGIN_DATA}`. Uninstall cleanup
therefore does not remove capture history. Claude token format has exactly schema,
active endpoint and endpoint entries containing token, handle, history UUID and
opaque receipt binding; malformed state refuses sharing rather than silently
becoming a first share.

Index calls always pass an explicit Claude cache path. The shared compatibility
`codex_home()` returns only Claude's resolved config directory. Source identity
is stable across moves, while path/stat attributes are mutable. Freshness uses
size, mtime and leading 64 KiB hash. Fingerprint is six-byte BLAKE2b over
`models.py`, `rollout.py`, `worker.py`, `ledger.py`, `composition.py`, `windows.py`,
`images.py`, `analyze.py`, full/metrics mode, fact codec and strict integer,
canonical-JSON Unicode and offset/microsecond timestamp probes. Failure gives 0
and disables reuse without discarding history. Prices/rendering are not persisted
in file facts; raw model metadata is retained for later aliases/pricing.

Skills use quoted `${CLAUDE_SKILL_DIR}/scripts/...` substitutions with `python`
on Windows and `python3` on macOS/Linux. Default skill discovery is `skills/`
outside `.claude-plugin/`; there is no redundant manifest skills path. A copied
Claude plugin contains everything it needs. The isolated packaging smoke copies
only that plugin, uses paths with spaces, temporary corpus/account/state, `-I -S -B`,
both substituted interpreter variants and a network guard. It rejects checkout
entries on `sys.path`, imports outside the installed plugin, missing fixture counts,
missing preview/film or any dry-run token. The report/share/film smoke checks film
counts/value and guards account/history/index access for film. Film creates none
of those state files. No sibling repository is present.

Share collection retains its verified history handle through payload/preview and
preparation, then closes it in `finally`; ordinary reports close after collection.
This avoids the second open-time full pass. The explicit full pass before preparing
and every subsequent network mutation remains intact.

## 15. Tests, mutation obligations and CI

The unchanged Claude suites cover identity/collapse/ownership, strict count types,
retained prune/shorten/rebuild/conflict/calendar behavior, prepared receipts and
coverage, reset clustering/grid/observation boundaries, all four plan conditions,
pricing/composition/images, stream timing and three-style page behavior. Synthetic
B has 2 responses/input 255/cached 90/output 18/thinking 7 and API value .011578;
quota W charts 1650 nominal tokens but shares 770 observation-span tokens.

Mutation sections are ledger, retention, pricing, windows and share. Each named
mutation must execute and fail its designated semantic assertion; crashes/import
errors/unrelated failures are not evidence. Round 4b's existing performance tests
continue to require full verification once per open, unchanged-copy fast paths,
immutable batches and readable legacy storage. Round 8's count-success/local-state
failure test and its accepted stricter-than-harness parity stance are retained.

Full `check_manifests.check(repo) -> List[str]` checks marketplaces, contained
sources/components, reserved names, independent Claude/Codex versions, four shared
byte pairs, offline prices, frozen renderer/server hashes, provenance/lockfile
pins, sensitivity self-test, skill frontmatter/substitutions/interpreter variants
and isolated installed copy. `--existing-only` still checks every present surface
while allowing missing unfinished Claude packaging. It needs no Claude installation,
authentication or private corpus.

The four existing Ubuntu 24.04/Windows Python 3.8/3.14 cells and Codex steps stay
unchanged. Claude stdlib suites use `python -I -S -B`; share/mutations use `python -B`;
Node 22 runs the embedded-page suites. Each section/check has a failure-independent
`!cancelled()` condition; contract-dependent steps additionally require successful
lockfile installation. Full manifests, synchronization and frozen compatibility
checks are included. The added macOS-14/Python 3.14/Node 22 cell runs only Claude
suites and packaging/shared checks, with the manifest sensitivity self-test; it
does not run Codex suites or install tiktoken. Existing vocabulary CI stays unchanged.
CI installs the development harness with `npm ci --prefix scripts/server_contract`;
there is no new runtime dependency. The unchanged Codex pipeline's synthetic local
wheel installer test remains an offline test obligation.

Film suites are `python -I -S -B scripts/test_claude_film.py` and
`node scripts/test_claude_film_page.js`, in both the matrix and macOS jobs.
They cover invented extraction/privacy/installed artifacts, hand-calculated
model/score arithmetic, every fixture frame, paused player controls, fake codecs,
and independently parsed synthetic MP4 structure. Python sections are data and
integration; Node sections are model, draw and runtime. These are test-only
selectors. Font/license bytes have pinned SHA-256 packaging checks.

## 16. Known limitations and unverified behaviors

Film checks were verified by the owner on 2026-10-13 in Chrome for Testing 149
headless shell on Windows 11 from a file:// page: font loading, paused poster,
playback clock, frames matching the reference stills, and a Save MP4 export with
H.264 Constrained Baseline + AAC LC that ffmpeg decodes without errors (moov before
mdat). Still UNVERIFIED: other engines (Firefox, Safari), the Opus path and its MP4
playback, hardware encoders, and audible playback in a headed browser.
AAC retains encoder priming and padding. Cross-engine pixels and last sample bits
may differ; cross-engine pixel/PCM hash equality is not a release gate.

Calendar/account activity completeness is unknown. Transcripts removed before the
first capture, unlogged background/compaction/advisor calls and other devices or
surfaces cannot be recovered. Account observations are local statements, not
authenticated subscription history. Whether `subscriptionCreatedAt` changes on
upgrade/downgrade, the internal weekly-429 all-model scope and normal Pro tier literal
are **UNVERIFIED**. The observed per-account Thursday 16:00Z grid is a corpus
observation, not a universal invariant; future departures remain diagnostics.
Sparse readings cannot establish an opening percentage or intervening quota path.

Live plugin installation, actual cleanup deletion, `/clear`/rewind/fork rewrite
behavior, advisor-call completeness, future transcript fields, live HTTP/storage/
estimator behavior and Nocturne's actual WebGL/browser rendering remain unverified
by these offline checks. Parent chains and timestamps cannot isolate send/server
time, and image estimates do not prove request transformations. Captured facts can
be stale for one run during concurrent appends. Price defaults/high scenarios are
conditional, not bounds on missing/unpriced activity. Partial speed partitions can
exclude a valid shared window from the current estimator. Known server pricing
defects remain owner corrections (§11/§13).

The remote CI matrix first ran on the merge of PR #3 (2026-10-11). The four Ubuntu
cells passed; the macOS cell and both Windows cells failed four assertions in
`test_claude_ledger.py` whose expected paths were spelled unresolved while the code
resolves (`/private/var` behind `/var` on macOS, the 8.3 name `RUNNER~1` in the Windows
runner's temp directory). Those tests now resolve the temp directory first. The local
acceptance run still does not verify every configured runner/interpreter combination.

Implementation differences from the plan, kept explicit:

- Calendar helper bodies live in Claude `ledger.py` and are re-exported by
  `analyze.py`, rather than duplicated there; fixed-vector/DST parity verifies them.
- `History.capture()` returns the canonical pre-commit `LedgerResult` instead of
  the proposed `None`, avoiding a duplicate load/build; collection marks coverage
  committed only after commit. Compact lossless response arrays, deltas, bitmasks,
  reversible base64 hashes and immutable auxiliary lists implement the planned
  content-free facts under unchanged schema/codec, with legacy blobs retained.
- The vendored harness checks schemas/plausibility, not the HTTP route. The listed
  route/body/report restrictions are local-only rather than parity claims.
- Full blob verification stays at open because the unchanged round-4b suite
  explicitly requires it. The proposed load-only verification optimization is
  rejected; sharing instead reuses the verified collection instance and retains
  explicit pre-mutation checks. No stored shape or integrity rule changes.
- `analyze()` has an optional `include_share_latency` demand switch, defaulting to
  the complete established model. A report without JSON/public output omits only
  its unused private in-memory adapter; JSON remains complete and ordered exactly
  as before. Shares retain an ephemeral selection/time/model adapter for their
  preview; it never enters history, token state or the wire schema.
- The planned `scripts/verify_claude_schema.py` shape survey is not in
  the round-8 tree. This round does not add a new corpus survey; tests and existing
  captured evidence are the basis for documented shape claims.
- At the owner's request after the first installed run (2026-10-11), the page
  omits the plan's fixed standing notes and historical-plan notes, keeps every
  tile note to one line, and no longer draws the observation span along the limit
  chart's baseline, where it read as a 0% line. The span timestamps left the page
  payload with it. The span, the cache-write split, the responses without recorded
  thinking and the pricing assumptions remain in the JSON model, the terminal
  summary and this document.
- Report `--quiet` suppresses its own progress/timing text, so the prescribed quiet
  report commands have no CLI timing line. Wall-clock timing is reported separately;
  share's collection/total line remains present. Output bytes are not changed to
  add timing text. MacOS runs the manifest's compatibility sensitivity check,
  while the complete tokenizer-dependent Codex compatibility suite stays in the
  four existing Codex cells.

The supplied round-8 measurements on 282 files/~52k responses were warm report
36–39 s (35 s target), preview dry run 46.6 s (19.8 s collection) and counts-only
dry run 36.7 s. Round-9 measurements and the retained/rejected optimization timings
are recorded below; corpus snapshots and clock-dependent 30-day sample counts can
differ between runs.

| Measured wall clock | Round-9 baseline | Lazy/reused recent timing | Reused history handle | Target |
| --- | ---: | ---: | ---: | ---: |
| Warm local HTML report | 32.765 s | 28.312 s | 28.694 s | ≤35 s |
| Share dry run, `--no-report` | 34.936 s | 34.261 s | 30.503 s | ≤35 s |
| Share dry run, with preview | 46.904 s | 42.957 s | 39.098 s | ≤42 s |

The first change removes 4.453 s from the measured local report and 3.947 s from
preview; counts-only timing is unaffected by that calculation and its 0.675 s
variation is run noise. Reusing history then saves 3.758 s on counts-only and
3.859 s on preview. The local report does not retain an open share handle; its
0.382 s variation is run noise. Share CLI lines after both changes were
`Timing collection: 19.5s; total: 29.7s.` and
`Timing collection: 19.3s; total: 38.3s.` respectively. The report's quiet command
prints no internal timing line. A fingerprint-changing edit requires one initial
re-extraction; that warm-up is excluded from the warm measurements. These are
sequential guarded measurements of a live corpus, not universal timings.

## 17. Deterministic session films

token-film selects one currently present main transcript. It freezes a bounded
byte prefix in a temporary snapshot, runs unchanged metrics-only worker
extraction and a main-only ledger with history=None, then joins a content-free
second pass by worker response identity and collapsed physical block identity.
Pass 2 stops at the worker's last_complete_line. Live append-only growth is
accepted; shrink or captured-prefix change retries once, then fails without
artifacts. Temporary snapshots are removed. Parser fingerprints, fact shapes,
capture history, report models and share payloads remain unchanged.

All-zero canonical rows remain ledger responses but are omitted from animation
steps. Subagents are separately deduplicated and disclosed in terminal/JSON
output; they do not affect HTML bytes. Report comparisons require matching
evidence scope.

The script is Python 3.8+ standard library only. It writes one self-contained
HTML page with embedded public data, fonts, Canvas renderer, synthesized score,
player and WebCodecs MP4 exporter. No runtime network resource or dependency
installation is used.

The public projection contains numeric counts, relative time, closed block and
tool labels, validated compaction sizes and aggregate pricing. It contains no
conversation text, private path/title/account data, absolute session dates or
raw session ID. No account file is opened.

HTML has canonical serialization and no generated timestamp. The same
transcript bytes, plugin version and price table produce byte-identical HTML.
Within one browser engine and OS, a frame index gives the same drawing call
sequence and a sample index gives the same sample. Rendering is indexed at
30 fps and sound at 48 kHz, with fixed jitter/noise seeds. Player clocks choose
indices without changing those functions. Cross-engine pixels and samples may
differ and are not equality gates. Encoder/container timestamps are zero;
encoded MP4 bytes need not match.

The ordinary act-2 budget is capped at 500 frames. Dense compactions may extend
it under the exact three-frame-minimum, run-minimum and 40-percent-pause rule.
Missing boundary metadata uses disclosed neighboring-context display fallbacks.
Window labels are closed display inferences, not independently verified model
configuration.

Thinking remains present when null-thinking positive-output steps account for
at most one percent of total output. Those steps contribute zero displayed
Thinking and all their output to Writing. Above that threshold, Thinking is
omitted and all output becomes Writing. JSON and terminal output disclose
missing response/output counts. Unpriced token counts remain visible; API list
value covers priced tokens and recorded search fees. Receipt cents use stable
largest-remainder allocation. The value is not spend or a bill.

Verified by the owner on 2026-10-13 in Chrome for Testing 149 headless shell on
Windows 11 from a file:// page: font loading, paused poster, playback clock,
frames matching the reference stills, and a Save MP4 export with H.264 Constrained
Baseline + AAC LC that ffmpeg decodes without errors (moov before mdat). Still
UNVERIFIED: other engines (Firefox, Safari), the Opus path and its MP4 playback,
hardware encoders, and audible playback in a headed browser. AAC encoder priming
is retained. Opus dOps copies encoder PreSkip, or zero without a description.
No track has edts. Node tests establish structure and repeatability, not real
codec interoperability.

Film schema 1 is independent of share/history schemas. Counts and cumulative
totals must fit Number.MAX_SAFE_INTEGER. The content address is the first 12
hex SHA-256 characters of sorted compact ASCII-escaped public JSON before its
artifact_tag is added. Embedded JSON also escapes `<`, `>` and `&`. The optional
JSON wrapper contains closed ledger/pricing/film counters and omitted subagent
measurements; it is not embedded. Subagents, mtimes and local calendar displays
therefore cannot change the HTML. Closed display_evidence fields carry aggregate
context_1m, recorded cache_ttl (1h/5m/mixed) and read_discounted so the page can
apply its scale/TTL/price explanation rules without model names or identities.
Omitted-subagent quality uses the additional nonnegative family-ledger counters
introduced by child evidence and pricing counters for child-owned rows; the
main transcript's existing warnings are not attributed to omitted children.
If the unchanged float pricing engine cannot represent child-owned usage value,
that diagnostic value is unavailable with film_subagent_pricing_unavailable;
the main film remains independent of child evidence.

The first 20 steps use the reference durations, truncated for shorter sessions.
Act-1 boundaries add six-frame pauses, with 46 frames for the first manual and
42 for the first automatic boundary anywhere. Act-2 positive runs have minima
38, then seven frames. B0=min(500,max(sum(minima),ceil(30*log2(1+N2))+8*C2)).
Try ordinary pauses 6,5,4,3 in order, requiring pauses <=40% of B0 and enough
remaining run frames. Otherwise use three and extend B to
max(B0,ceil(pauses/.4),pauses+sum(minima)). Proportional constrained allocation
uses half-up duration rounding and stable signed-error residue correction.
Consecutive active presses override retracts; the last retract remains four
frames. frameOfStep preserves the reference arrival convention at integer cuts.

Context is reads+writes+base. Missing/inconsistent boundary sizes use clamped
neighboring-context displays and named film counters. A known display limit is
1M when any context/pre exceeds 200K or context_1m is recorded; otherwise 200K
when an automatic recorded pre is positive and below 200K; otherwise unknown.
niceAbove uses the smallest strictly higher 1/2/5 power-of-ten value. The tube
retains the reference 26-frame logarithmic zoom and .88/.85 alarm/sweat ratios.
The smallest power-of-ten ball unit limits whole balls to 1800; positive smaller
categories get reserved specks/lenses, and categories with 1–20 balls get rings.
Cache-rewrite illustration uses write>60000 and write>.5*context; its sticker
threshold is .2 of the display limit/maximum, not evidence of TTL causation.

Typed-prompt bubbles use strict physical intervals between retained responses'
first collapsed blocks; zero/excluded/quarantined responses do not consume
prompts. Safe block kinds are k/t and the fixed film tool list. Unknown tools
become tool; mcp__ names become MCP. Sticker interruption is per teach/compact/
recache/ball/speck slot; other slots and normal seven-frame fading tails coexist.
Relative time uses days at >=2 days, hours at >=2 hours, otherwise minutes;
the count is floor(elapsed/unit)+1. Missing times remain unavailable.

Playback initializes paused at min(60,DURATION-1). Play starts the untouched
poster at zero; explicit #f and seeks resume their selected frame. AudioContext
is created/resumed from a Play gesture. Export pauses playback and draws every
integer frame on a separate canvas. Quality H.264 probes prefer avc1.42E028 then
avc1.420028; audio probes prefer mp4a.40.2 then opus, 48kHz stereo. Encoder queues
are bounded at eight, cancellation/errors clean up objects/waiters, and encoded
payload accumulation is limited to 512 MiB. Video durations are 3000/90000 ticks;
audio tables use chunk durations or validated AAC/Opus packet durations.
AAC padding/priming is untrimmed; movie duration is the maximum raw track duration
converted to 90000 ticks. moov precedes mdat, dates are zero, and no edit boxes,
private metadata, calibration or runtime downloads of dependencies exist.
