# Token Counter — Architecture

A Codex plugin that independently tokenizes Codex session history, measures cached vs
uncached input, and renders a local HTML dashboard.

**Revision 10.** The plugin described here is built, installed and verified against the
installed copy. Revisions 2–5 were driven by four rounds of adversarial review of the
*document*; revisions 6–10 review the *code*, which had far less scrutiny.

Revision 6 was self-review during implementation: a counting blocker (resend cost doubled on
602 files), three wrong figures, a byte filter that only worked on minified JSON, a timezone
error, an index that archived everything a windowed run did not scan, and a packaging layout
that would have shadowed the standard library.

Revision 7 is round 6 of review. Verdict: **not sound enough to rely on**, and it was
right — `--since` overcharged a live session by 30.5M tokens, the cache key did not move when
extraction changed, replay matching could delete unrelated sibling threads, corrupt records
vanished without a counter, and the *installed* plugin was not the reviewed code.

Revision 8 is round 7, which confirmed those fixes and returned **No** on three narrower
grounds. Revision 9 is round 8, which found that **one of those three fixes did not work**:
the out-of-window damage aggregation ran *after* the window filter, so its own guard was
unreachable and the fix was a silent no-op — and the regression test named for it exercised
the worker beneath the bug rather than the reporting path containing it. That is the failure
mode this project keeps hitting: a fix that passes its own test and does nothing.

Round 8 also found that the cache key still ignored the installed `tiktoken`, that an
object-shaped corrupt record remained invisible to the ledger-only path, and that the report
labelled a charged replay as "dropped".

Revision 10 answers round 9, which confirmed three of those four fixes were live, found the
fourth still incomplete, and — more usefully — demonstrated that the *rewritten* damage test
still would not fail if the fix were reverted. The response is `scripts/test_mutations.py`:
every historical defect is reverted in-process and the test named for it must go red. It
caught two of its own six cases immediately, meaning two tests did not protect their fix.
§10 records all of it.

Every quantitative claim below is measured against a live corpus of **~1,957 rollout files /
7.75 GB** on the development machine (Windows 11, 16 logical cores, NVMe, Python 3.14). The
corpus is **live and grows while it is being measured** — it gained 23 files during this
session — so structural counts move by fractions of a percent between runs and are a
snapshot, not a constant. A figure that differs in the third significant digit is drift; one
that differs in the first is a bug.

Reproduce with:

```
python scripts/fetch_vocab.py --verify     # §4  vendored tokenizer parity
python scripts/test_ledger.py              # §2  13 response-identity regressions
python scripts/test_pipeline.py            # §3–§7  317 pipeline assertions
python scripts/test_mutations.py           # §11 every fix fails when reverted
python scripts/bench.py                    # §3.4  the parallelism grid
python scripts/verify_schema.py            # §2.2, §2.3 schema claims
python scripts/verify_install.py           # §6  installed copy == this code
python scripts/fetch_prices.py --check     # §5.9  vendored prices against the live pages
```

---

## 1. Goals

1. **Independent tokenization.** Re-tokenize session content locally rather than trusting
   the aggregates Codex reports, and attribute tokens to *categories* (system prompt, tool
   output, user message, ...). Reported usage gives totals; it never says what filled the window.
2. **Cached vs uncached measurement.** Report what actually hit cache and what did not, and
   — separately and explicitly labelled as inference — where cache *appears* to have broken.
3. **Local HTML report.** Generated on request from inside Codex, opened in the browser.
4. **Attribution and limit context.** Name the account the report covers (§2.6), and show
   how each weekly rate-limit window was spent — the server's reported percentage beside a
   locally measured cumulative token curve that restarts at every reset (§5.6).
5. **Response time.** How long each model call, turn and tool call took, from the records'
   own timestamps — and, separately and labelled as an estimate, how much of a response's
   time sat above the pace its fastest responses show (§5.8).
6. **API value.** What the recorded usage would cost at OpenAI's published API list prices,
   priced per response from a vendored table (§5.9). A counterfactual, worded as one: a
   ChatGPT plan is not billed per token.

Non-goals: a reconstructed bill (the API value prices *recorded* usage at *list* prices, and
says what it could not price), fetching prices at run time, live interception of in-flight
requests, modifying Codex behavior, and converting tokens into rate-limit consumption — the
limit percentages are the server's own figures, recorded verbatim and never derived.

---

## 2. Data source

Codex writes append-only JSONL rollouts to `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.
This is the only input **to every figure in the report**. Nothing is intercepted at runtime.

One file outside that tree is read, and only to *name* the account: `~/.codex/auth.json`,
for the non-secret identity claims in its id_token (§2.6). It feeds no number, and
`--no-account` skips it.

### 2.1 Records consumed

| Record | Use |
| --- | --- |
| `session_meta` | `base_instructions.text`, `cwd`, `cli_version`, `git`, `dynamic_tools` (§5.3) |
| `turn_context` | `model`, `effort`, `cwd`, `personality` per turn |
| `token_usage_record` | Per-response usage, **explicit** form, carries `response_id` |
| `event_msg`/`token_count` | Per-response usage, **legacy** form, no response identity |
| `response_item` | `message`, `custom_tool_call`, `custom_tool_call_output`, `function_call`, `function_call_output`, `reasoning`, `agent_message` |
| `compacted` | Compaction boundaries; `replacement_history`; also carries `latest_token_usage_record` (§2.2) |
| `world_state` | Environment block, AGENTS.md presence |

`event_msg`/`item_completed` mirrors `response_item` content and is **skipped** to avoid
double counting content.

Every record's outer `timestamp` is read too, on both passes: for usage records, `turn_context`,
`world_state`, and each `response_item`'s side (input or output) and `call_id`. That is all
the timing in §5.8 uses; no other record type is consumed for it.

Shapes the classifier must tolerate, all observed in the corpus:

- **`function_call_output.output` is a bare string in 603 of 644 sampled cases** and a list of
  `input_text` elements in the rest. `custom_tool_call_output.output` varies the same way.
  A list-only reader silently drops most tool output.
- **`agent_message`** carries multi-agent traffic as `input_text` plus an `encrypted_content`
  element; the encrypted part is opaque, like reasoning.
- **`compacted.replacement_history`** contains `message` items *and* `compaction` items with
  their own `encrypted_content`.
- **`internal_chat_message_metadata_passthrough`** is present on nearly every item but is
  local bookkeeping (~51 characters, a turn id). It is **not** counted as content.
- `inter_agent_communication_metadata` is a distinct outer record carrying only
  `{"trigger_turn": bool}`; it has no content.

### 2.2 The canonical usage ledger

**This is the single most important correctness constraint in the system.**

Two usage streams coexist and **overlap**. Measured across the corpus:

| Files | Streams present |
| --- | --- |
| 1,341 | legacy `token_count` only |
| 602 | **both** legacy *and* explicit `token_usage_record` |
| 0 | explicit only |
| 7 | neither — no usage data at all |

In those files the streams **overlap but are not a pure mirror**. Summing both double-counts:
naive sum is **21.622B** input tokens against a canonical **16.669B**.

Of 602 both-stream files, only 393 mirror one-to-one. The remaining 209 break down as:

| Difference | Files |
| --- | --- |
| Compaction differences only | 75 |
| Compaction differences plus repeats | 75 |
| Repeats only | 54 |
| Unmatched terminal explicit response (± the above) | 4 |

Across these, the explicit stream contributes **254 compaction responses and 4 terminal
responses** that ordinary legacy usage omits, while legacy contributes **255 context
snapshots and 189 repeats** that are not responses. Every ordinary legacy value had a
preceding explicit match, and explicit running sums reconcile — so preferring the explicit
stream loses no response and retains no duplicate on the observed corpus.

Rules:

1. **Exactly one ledger per file.** Prefer `token_usage_record` when present (it carries
   `response_id`, enabling identity-based dedup); otherwise use legacy `last_token_usage`.
   Never add the two.
2. **Deduplicate by `response_id`** within the explicit stream.
3. `compacted.payload.latest_token_usage_record` (**255 occurrences**) exactly duplicates
   standalone records. It is *saved state*, not a new response — never counted.
4. **8 `token_count` events carry `info: null`** and must be skipped without erroring.
5. **7 files have no usage data at all.** Represent as unavailable, never as zero.

### 2.3 Schema drift — dispatch by shape, not by version

`token_usage_record` appears from **0.153.4** onward, not 0.155.1 as first assumed. Version
gating is unsafe; dispatch on record shape.

| CLI versions | Usage shapes present |
| --- | --- |
| 0.142.5, 0.143.0, 0.144.1, 0.144.3, 0.144.5 | legacy, 5-field |
| 0.144.6 | legacy, both 5- and 6-field |
| 0.145.0 – 0.152.0 | legacy, 6-field |
| 0.153.4, 0.154.0, 0.155.1 | legacy 6-field **and** explicit records |

The 6-field shape adds `cache_write_input_tokens`, present in **123,687 of 140,107 legacy
snapshots** and in **all 36,361 explicit records**. Preserve it where present; emit `null`
only where genuinely absent, so "unavailable" stays distinguishable from "measured zero".

**But the field carries no information.** Across all **160,048 occurrences** its value is
**0, without exception**. Earlier drafts implied it was a usable measurement of cache writes;
it is not. The plugin records its presence and its uniform zero as a data-quality counter and
derives nothing from it. If a future CLI version starts populating it, that counter changes
and the drift is visible.

### 2.4 Reported usage semantics — scoped by record class

Verified on the **explicit** stream (all checks pass):

- `input_tokens` is the whole prompt; `cached_input_tokens` is a **subset**.
- `total_tokens == input_tokens + output_tokens`.
- `reasoning_output_tokens` is a **subset** of `output_tokens`.
- Thread running sums reconcile against per-response records.

The **legacy** stream does *not* uniformly satisfy these:

- **855 snapshots violate** `total == input + output`. All have zero input and output with a
  positive total: context state, not responses. They are **not** all aborted turns —
  the breakdown is **818 compaction snapshots, 16 aborted-turn snapshots, 20 repeated
  invalid snapshots**. Of these, **599 fall in files that select the legacy stream** and are
  excluded there; the other 255 fall in both-stream files and are superseded by §2.2.
- **Compaction can carry a real response.** One explicit record reports 247,153 input /
  4,084 output immediately before a compaction and its zero/zero snapshot. The 254 explicit
  compaction responses are **preserved**; only the zero/zero snapshots are dropped. Legacy
  compaction intervals carry no equivalent usage evidence, so recorded-usage coverage is
  reported separately from any "true billed" claim.
- **737 repeated `info` snapshots** — the same state re-emitted. Counting each event as a
  response over-counts.
- **7 decreases in cumulative counters** — so unconditional cumulative differencing is
  also invalid.

Therefore neither "one event = one response" nor "difference the cumulative counter" works.
Classify each record as *response usage*, *context snapshot*, or *repeat* before counting.

**On the 128-token quantization:** every observed `cached_input_tokens` is divisible by 128.
This is a real regularity worth surfacing, but divisibility is *consistent with* a 128-token
cache granularity, not proof of one — it may equally be reporting-side rounding. Stated as an
observation, not a mechanism.

### 2.5 Thread identity, forks, and multi-agent rollup

- All **1,934 files carry `session_id`**, forming **1,689 sessions**. All **245 parent links
  resolve** within their session; no orphans or cross-session collisions were found.
- **6,987 of 9,254 `turn_context` records lack `root_turn_id`.** Root-turn attribution is
  **nullable**; missing values must not collapse into a single synthetic turn.
- **44 files contain a second `session_meta` whose thread id differs from the file's own
  header** — a fork carrying copied parent metadata. The *first* header owns the file; a
  fork is a **distinct thread identity**, not a continuation of the parent's.
- **Forks replay parent history into the child.** One child's first snapshot already reports
  **12,577,463** cumulative input tokens, and the file contains 37 inherited snapshots
  matching its parent — with **timestamps rewritten to the child's creation time**, and
  no response IDs.
- **A copied `session_meta` is not a sufficient signal.** Three children carry inherited
  usage with *no* copied parent header. Detection must not rely on it.
- **Usage-tuple equality is not response identity.** Two sibling threads can legitimately
  produce identical usage, and one thread can legitimately repeat a usage value with the
  cumulative counter advancing. A session-wide tuple dedup gets the right answer on this
  corpus but fails both cases in principle, over-dropping ~618 records.
- **A declared parent link is not the same thing as a copied header.** 250 files declare a
  `parent_thread_id` in their *own* first `session_meta`; separately, 44 files contain a
  *second* `session_meta` copied from the parent. The second signal is unreliable; the first
  is not. All **39** files with detectable replayed history declare a `parent_thread_id`, and
  in all 39 the file the run matches **is** that declared ancestor.
- **Rule — three discriminators, strongest first:**
  1. `response_id`, on the explicit stream: exact identity, applied at **session** scope.
     Per-file dedup is not enough in principle: a fork child replaying its parent's history
     on the explicit stream would repeat those ids across files, and discriminator 3 runs
     only on the legacy branch. Zero such duplicates exist on this corpus across the 45
     sessions with more than one explicit-stream file — this closes a structural gap, not an
     observed one, and since `response_id` *is* identity it can never over-drop.
  2. **Immediate-predecessor full-state equality** on the legacy stream: a record is a
     repeat only if its `last_token_usage` *and* `total_token_usage` both match the
     preceding record. An advancing cumulative counter means a new response.
  3. **Ancestor replay:** a fork replays ancestor history as a **leading run** of the child
     file matching a contiguous run in a file on its **declared ancestor chain**
     (`parent_thread_id`, followed transitively so a grandchild can replay an inherited
     run). A run of length 1 is indistinguishable from coincidence and is marked
     **ambiguous** (charged, and counted in §8) rather than silently dropped.

     Searching every earlier file in the session — the revision-6 behaviour — deletes real
     responses whenever two unrelated sibling threads share a run of states, which sub-agents
     launched with identical prompts can easily do. Restricting to the declared chain costs
     **nothing measurable**: all exclusions (580 / 547 / 3,040 / 5 across 39 files) are
     unchanged, because every real match was already a declared-parent match.

     **This remains an inference, and is labelled as one.** The legacy stream carries no
     response id, so a replayed run is *matched*, not identified. A false positive deletes
     real responses; a false negative charges inherited history twice. `--no-replay-exclusion`
     exposes the other bound instead of arguing about which is right: **136,284 responses /
     16.708B** with the exclusion, **139,332 / 17.015B** without it.

     The report names its mode in a standing note, renders the mode counter even at zero, and
     counts matched records under `inherited_charged` rather than `inherited` when the
     exclusion is off. It previously reported "Ancestor-replay records dropped: 3" about
     three records it had just charged, which is false labelling rather than disclosed
     approximation.

**Both sides of a replay match must be normalized identically.** Each sequence is reduced
the same way — immediate full-state repeats collapsed, context snapshots removed — and
compared on **complete state (usage + cumulative counters)**, not usage alone. Asymmetric
normalization is a silent correctness bug: if the ancestor sequence drops context snapshots
and the child's does not, matching halts at the first compaction and inherited history is
charged twice. That defect cost 632 records across 11 files before it was caught.

Ancestor history is stored as the file's **own normalized sequence, independent of which
records were charged** — a grandchild may replay a run that includes the parent's own
inherited portion.
- Validated two ways. Against the known fork pair, the child's leading run matches the
  parent at offset 96 for exactly **37 records** — the count independent parent-history
  matching produces. Across the corpus the rule excludes **3,587 records / 369M input
  tokens**, reconciling with an independent ancestry-based diagnostic. And it passes both
  cases that defeat naive tuple dedup. Regression tests: `scripts/test_ledger.py`.

### 2.6 Account identity — the one file outside the corpus

A rollout records **no account**. Not an email, not an account id, not a user id: the
identity of the signed-in account appears nowhere in `~/.codex/sessions`. The only thing the
logs carry is `plan_type` inside a rate-limit snapshot, which distinguishes `pro` from
`prolite` and nothing else.

That is a real gap once a report is shared, archived, or built over a corpus copied from
another machine, and it is invisible: an unattributed report looks exactly like a correct
one. So `tokencounter/account.py` reads `~/.codex/auth.json`, and it is the **only** departure
from "rollouts and nothing else" in the system. The exception is narrow by construction:

- The file is opened read-only and parsed as JSON. Nothing is written back.
- Of the OAuth material in it, only `tokens.id_token` is touched, and only its **middle
  segment** — the claims, an unsigned base64url JSON object. The signature segment is never
  decoded. `access_token` and `refresh_token` are never read out of the parsed object.
- Extraction is an **allow-list**, not a filter: `email`, `name`, `chatgpt_account_id`,
  `chatgpt_plan_type` and the subscription window, each accepted only if it is a scalar.
  A claim OpenAI adds later cannot leak into the report by default, and a claim arriving as
  a nested object is rejected rather than stringified.
- `report.py --no-account` skips the file entirely; every field then reports as absent and
  the page says *account not identified* rather than implying one.

Two properties are asserted by test, not by inspection: that no access or refresh token
value reaches the account record or the rendered HTML, and that `--sessions-root` makes the
lookup happen **beside that root** rather than in the real `~/.codex` — otherwise a report
over a copied corpus, or a test run, would be stamped with the live account's email address.

**The claims are not verified.** Checking a JWT signature needs the issuer's keys and
therefore a network call, and this tool makes none. The file is read as a local statement of
which account the CLI is signed into — which is the question being asked — never as an
authentication decision. A stale or hand-edited file yields a wrong name, not a wrong number:
nothing here feeds any figure in the report.

**Two namespace shapes, both read.** OpenAI namespaces its claims with a URL, and the live
tokens carry them as a **nested object** under `https://api.openai.com/auth` while the
flattened `<namespace>/<claim>` spelling is the more usual JWT convention. Reading only the
flattened form was the first implementation and it failed *silently*: `email` and `name` are
plain claims, so they still populated, the record still reported itself available, and only
`plan` and `account_id` went quietly missing. The claim set also varies between issuances of
the same account's token — a refresh during development replaced a token carrying these with
one that did not — so every field is optional and the report falls back to the `plan_type`
the rollouts report.

---

## 3. Pipeline

A single pass, not a tiered one: full independent tokenization of the whole corpus costs
~16–22s, so there is no reason to defer it behind an opt-in tier.

```
discover -> [index cache] -> parse -> classify -> tokenize -> analyze -> render -> open
```

### 3.1 discover

Glob `~/.codex/sessions/**/rollout-*.jsonl`. Stat each file.

Archiving **moves** rollout files out of this tree, so path is not a stable identity. Index
entries whose files vanish are retained as archived, not silently dropped.

**A window narrows the report, never the ledger.** `--since`/`--until`/`--session` filter
which files are *reported on*; the ledger is always charged over the whole corpus, because a
fork child's ancestors may sit outside the window and discriminator 3 then cannot see the
history the child replays. Filtering first charged that inherited history as new — measured
at **246 responses and 30,536,496 input tokens** overcharged on one session with
`--since 2026-07-26`. Out-of-window files are needed only for their usage records, so they
take the metrics-only pass, which is ~7x cheaper than full extraction.

### 3.2 index cache

SQLite at `~/.codex/token-counter/index.db`, keyed on `(session_id, thread_id)` with path as
a mutable attribute rather than the key, so a file that moves updates its own row instead of
duplicating it. Payloads are zlib-compressed JSON; the whole corpus indexes to **20 MB**.

**Granularity is the whole file, not a byte offset.** Earlier drafts specified incremental
tailing from a committed offset. That is not implemented, and on measurement it is not worth
implementing: re-parsing a *changed* file costs 0.5s even at 249 MB, and resuming mid-file
would require persisting parser state — the prompt chain, pending items, call-id map — which
is a large correctness surface bought for very little. A changed file is re-parsed whole.

Reuse is guarded, never assumed:

- Append-only was *observed*, not guaranteed: across two full reads, 1,932 files were
  byte-identical and 2 had grown with their previous prefixes intact. No rewrite was seen.
- A cached payload is used only when size, mtime **and** the leading-64 KiB hash all match,
  and only when it was produced by the current extractor version. Any mismatch re-parses.
- The reader yields only **complete JSONL records**; a trailing fragment without a newline is
  withheld, because a rollout may be read while Codex is mid-write. Regression-tested.

**Archiving is decided by file existence, not by scan coverage.** This was a real bug: the
first implementation archived every entry it had not seen in the current run, so
`--since 2026-09-18` archived 1,801 of 1,947 entries. Archived rows are retained with their
payloads and can be counted back in with `--include-archived`; the report always shows how
many exist.

Measured: cold run **31.2s** (21.9s with `--fast`), warm run **4.0s** for the full 7.78 GB
corpus. Steady state re-parses only the day's changed files, typically one or two.

**The cache key is derived from everything that shapes a payload, not hand-maintained.** A
constant that somebody has to remember to bump is a footgun, and it fired: extraction changed
four times in one sitting while the constant stayed put, and the index kept serving payloads
from before the changes — 18.43B resend tokens against a true 14.41B, corrected only by an
explicit `--rebuild`.

The key now hashes `worker.py`, `classify.py`, `images.py`, `encoding.py`, `rollout.py`, the
**vocabulary's contents**, and the installed **`tiktoken` version and module path**. Each was
added because leaving it out was demonstrably wrong: `--vocab` selects the tokenizer that
produced every stored count, and a `tiktoken` upgrade changes counts without touching a
single local file — reproduced by patching `encode_ordinary`, where direct extraction gave
999 tokens while the cache happily served 2. The vocabulary is keyed on contents rather than
path, so the same blob at a new location does not force a pointless rebuild. An edit to the
renderer invalidates nothing.

### 3.3 parse and classify

Two modes over one reader:

- **metrics-only** — usage records only, no tokenization and no attribution. **8.1s** end
  to end over 7.8 GB, used for out-of-window ancestors that the ledger needs but the report
  does not cover.

  **There is no longer a byte prefilter.** Two successive versions of it were wrong: a
  quoted fragment like `b'"type":"token_usage_record"'` matches nothing the moment the
  writer emits a space after a colon, and a broad `b'token_usage'` matches
  `last_token_usage` too (the round-1 double count). Both were fixable. What was not is that
  **no** byte filter can see a record whose own `type` string is damaged — a complete,
  object-shaped, corrupt line simply never reaches the parser, and its usage leaves the
  ledger with no counter.

  Measured, the filter bought **1.0s across the whole corpus** (3.6s against 4.6s), and it
  was *slower* than parsing outright once validation of the rejected lines was added
  (6.3s). Every complete line is now parsed on both paths, and a line that fails to parse is
  counted — with a flag for whether it looked like it carried usage. Classification remains
  by parsed JSON, which was always the authoritative step.
- **full** — parse every line, extract content segments tagged by category and turn.

Threading is not used on the metrics path: it is GIL-bound on `json.loads`, and 8 threads
buy 4% wall time for 28% more CPU.

### 3.4 tokenize

`tiktoken` with `o200k_base`, vendored offline (§4). One rollout file is one unit of work, so
the process pool parallelises the **whole** pipeline, not just the BPE step.

Two independent parallelism axes exist — process count, and
`encode_ordinary_batch(num_threads=...)`, which is a Python `ThreadPoolExecutor` **defaulting
to 8**, not rayon. Conflating them produced a misleading round-1 table. Measured separately,
now over the complete extraction pipeline (parse, classify, tokenize, image dimensions,
prompt reconstruction, resend costing) — `scripts/bench.py`:

| Processes | tiktoken threads | Wall | Worker CPU | Utilization | Throughput |
| --- | --- | --- | --- | --- | --- |
| 1 | 1 | 179.0s | 176s | 1.0x | 43 MB/s |
| 1 | 8 | 84.9s | 204s | 2.4x | 91 MB/s |
| 4 | 1 | 47.3s | 184s | 3.9x | 164 MB/s |
| **8** | **1** | **24.9s** | **190s** | **7.7x** | **312 MB/s** |
| **16** | **1** | **18.2s** | **236s** | **12.9x** | **425 MB/s** |
| 8 | 4 | 20.9s | 263s | 12.6x | 371 MB/s |
| 8 | 8 | 20.6s | 269s | 13.1x | 376 MB/s |

Two independent reruns agree within run-to-run noise: 176.3s and 173.5s at 1×1, 24.4s and
25.2s at 8×1, 17.9s and 18.1s at 16×1, 23.5s and 20.8s at 8×4. The **4×1 row is the least
stable** — 47.3s here against 40.7s measured independently — so treat single rows as ±15%
rather than exact. The revision-6 table did **not** reproduce at all: its low-parallelism
rows (107.5s at 1×1) were roughly 40% too fast, most likely measured with a warm page cache
and before per-item content digests. The conclusion was unaffected, but the table was wrong
and is replaced.

**This reverses the revision-5 recommendation.** That table, measured over extraction plus
BPE only, put 8×4 at the front. Over the real pipeline **16×1 dominates 8×4 on both axes** —
18.2s against 20.9s, and 236 CPU-seconds against 263. The thread axis is not a trade-off to
tune; it is simply worse. Threads only help when there are fewer files than cores, and there
are 1,957.

**Default: half the cores (capped at 8) × 1 thread.** `--fast` uses every core × 1 thread.
`num_threads` is never left to tiktoken's default of 8; `--threads` remains as an escape
hatch, documented as a losing move.

Then, once per run and single-threaded: ledger **0.57s**, analysis **0.85s**, render
**0.01s** (376 KB of HTML). End to end, `report.py` takes **28.1s** cold, **21.9s** with
`--fast`, and **4.0s** warm.

Memory, measured over a full cold run: **541 MB** in the parent (it holds every file's
extraction result until the ledger runs) and **1.31 GB** across the whole process tree at
8x1. `--fast` at 16 processes roughly doubles the worker half. Tokenization runs in bounded
chunks (4 MiB of text or 512 texts) and keeps only the lengths, so a 249 MB image-heavy
rollout never materialises its token ids. Base64 image payloads are never handed to the
tokenizer at all, which is why the largest file in the corpus extracts in 0.61s.

### 3.5 render / open

Self-contained HTML (no CDN, no network), written to
`~/.codex/token-counter/report-<scope>.html` and opened via the platform handler.

---

## 4. Tokenizer

**Decision: vendored `tiktoken` + vendored vocab.**

The `o200k_base` vocab is a single 3.6 MB blob committed to `assets/vendor/`.

**`TIKTOKEN_CACHE_DIR` alone does not work**, and the earlier draft's layout would have
shipped broken. `tiktoken.load.read_file_cached` derives the cache filename from
`sha1(vocab_url)` — for `o200k_base` that is
`fb374d419588a4632f3f557e76b4b70aebbca790`. A file named `o200k_base.tiktoken` in that
directory is simply not found, and tiktoken falls through to a network fetch that fails
under a restricted sandbox.

The plugin therefore **constructs the encoding explicitly**, with no dependence on cache-key
naming:

```python
from tiktoken import Encoding

ranks = encoding.read_ranks(VENDORED_PATH)      # any filename, parsed from the file itself
enc = Encoding(name="o200k_base_vendored", pat_str=O200K_PAT,
               mergeable_ranks=ranks,
               special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018})
```

`read_ranks` parses the `.tiktoken` format (`base64(token) rank`, one pair per line) as
`tiktoken.load.load_tiktoken_bpe` does, and `scripts/test_pipeline.py` checks the two agree. It
also refuses a repeated token or rank, which tiktoken would panic on with an exception that is
not an `Exception`. That loader is not called, for two reasons (Rev 19):

- **It does not read the file.** It reads through `read_file_cached`, which copies any path
  it is given into `$TMPDIR/data-gym-cache/<sha1(path)>` and serves that copy from then on,
  without looking at the file again. A vocabulary damaged or replaced in place kept loading
  as it was, while the cache key (§3.2) hashed the file as it is.
- **On Python 3.8 it cannot read a local path at all.** tiktoken 0.7.0 is the last release
  for 3.8, and its `read_file` raises for any local path unless the `blobfile` package is
  installed. A 3.8 run had no tokenizer and an empty content breakdown, with an error
  telling the user to re-vendor a vocabulary that was fine. It looked fine only on a machine
  where a newer tiktoken had already left the cached copy in `/tmp`.

Verified in a fresh process with `socket.socket` hard-blocked: the encoding builds and
produces **token-for-token identical output to stock `o200k_base`**. Startup is ~0.3s.

A pure-Python BPE was implemented and benchmarked as the alternative (`scripts/ref_bpe.py`).
It produces **byte-identical output** to tiktoken, but runs at 5.29 MB/s vs 16.9 MB/s
single-threaded, and still requires the third-party `regex` module because the `o200k_base`
split pattern uses `\p{L}`-style Unicode property classes that stdlib `re` cannot compile.
Since neither option is dependency-free, the faster one wins. The pure-Python encoder is
retained as a correctness oracle, not a runtime path.

### 4.1 Encoding fidelity is unverified — and the residual cannot settle it

`o200k_base` is an assumption; the encoding used by current Codex models is not published.

The reconciliation residual (§5.3) is reported, but it **cannot validate the encoding**: the
residual aggregates missing tool schemas, wire framing, compaction effects, reconstruction
error, and any encoding mismatch. A small residual does not confirm the tokenizer, and a
large one does not localize to it. The residual is published as an unexplained quantity;
it is never used as evidence that the encoding is right.

### 4.2 Installing tiktoken

`codex plugin add` copies the plugin directory and nothing else. The manifest has no field
for Python dependencies and no install step, so on a fresh install `tiktoken` was missing and
the content panel was empty until the user ran pip by hand. Most users never did.

The report now installs it itself (`tokencounter/deps.py`, called from
`report.tokenizer_status`), the first time a run needs it:

```
pip install --target <CODEX_HOME>/token-counter/lib/<cache_tag><abiflags>-<platform>/ \
            --only-binary :all: --retries 1 --timeout 10 tiktoken
```

- **Only when needed.** A `tiktoken` that already imports is used as is, and `sys.path` is
  not touched. `--metrics-only` never installs, and neither does a run whose vocabulary is
  missing, since no install would fix that.
- **Nothing in the user's environment.** `--target` writes only into the plugin's own
  directory: no site-packages, no `--user`, and no PEP 668 override (pip skips the
  externally-managed check for `--target`). `PIP_USER` and `PIP_REQUIRE_VIRTUALENV` are
  cleared for the pip process, because either one makes `--target` fail. When the
  interpreter has no pip (Debian without `python3-pip`), `uv pip install --target` is used
  if `uv` is on `PATH`.
- **Keyed by interpreter.** `tiktoken` and `regex` are native extensions that load only in
  the ABI they were built for, so each interpreter and platform gets its own directory. A
  free-threaded build gets a `t` in its key even on Windows, which has no `abiflags`.
- **Wheels only.** Without `--only-binary`, a platform with no wheel falls through to
  building the Rust extension from source, which needs a toolchain, takes minutes, and then
  fails.
- **Staged.** pip installs into `.install-*` beside the target, and the result is renamed
  into place only after pip succeeds. An interrupted run cannot leave a `tiktoken` without
  its extension where the next run would activate it. An existing install that does not
  import is moved aside, before the download, and then replaced. On Windows a directory
  holding an extension this process loaded cannot be moved; the run then names the
  directory to delete instead of downloading on every run. When two first runs race, the
  one that finishes second keeps the first one's copy. Four concurrent runs were verified
  to leave one working install and nothing else.
- **Prepended.** The directory goes at the front of `sys.path`. pip resolved `tiktoken`,
  `regex` and `requests` there as one set, and an older `regex` elsewhere must not be mixed
  in. Worker processes inherit `sys.path` under fork, spawn and forkserver alike, which was
  verified under all three.
- **Never fatal.** Any failure (no network, no pip, an unwritable directory, a 300 s timeout)
  becomes the page's note, `tiktoken could not be installed: <why>.`, the run continues on
  the usage ledger as in Rev 12, and the next run tries again. Any exception from
  `import tiktoken` counts as "not importable" (a foreign-ABI extension raises `OSError`),
  and installer output is decoded leniently, so neither can escape into the run.
- **Bounded.** One retry and a 10 s socket timeout: a network that drops packets costs about
  20 s per run until it works or `--no-install` is passed. A sandbox that refuses the
  network fails in about a second. A failure is not remembered: a back-off would also
  block the re-run with network access that token-report's SKILL.md asks for.

This is the report's one network call, and it breaks the old "no network, ever" line (§6).
Nothing of the user's is sent: it is a package download from PyPI. `--no-install` or
`TOKEN_COUNTER_NO_INSTALL=1` turns it off. `share.py` always passes `--no-install`. The
share's dry run usually has no network and its `--yes` run does, so installing there would
publish a page with a panel the dry run lacked (§6.1).

The cache key needs no change. `_extractor_version` fingerprints the tokenizer by what it
returns (Rev 10), so going from no tokenizer to an installed one moves the key, and so does
a later upgrade.

---

## 5. Analysis

### 5.1 Attribution

Every content segment is tokenized separately and tagged:
`system_prompt | developer_instructions | agents_md | user_message | assistant_message |
tool_call_input | tool_output | reasoning_summary | reasoning_blob | image`, plus
`turn_index` and `thread_id`.

**Reasoning is partially recoverable.** Alongside the encrypted blobs, the corpus contains
**1,525 plaintext `summary_text` elements across 8 files (74,689 characters)**. These are
independently tokenizable and are counted as their own category — they are reasoning
*summaries*, never presented as full reasoning content.

This is what reported usage cannot provide, and it drives the composition and waterfall views.

### 5.2 Cache measurement — two measurements and one inference

| Layer | Source | Status |
| --- | --- | --- |
| Reported | `cached_input_tokens` | **Measurement** |
| Derived | `input - cached` | **Measurement** |
| Prefix similarity | Longest common token prefix between reconstructed requests | **Inference — heuristic only** |

The first two are ground truth. The third is a *hypothesis generator*, and the document
previously overclaimed it. The constraints:

- **Cache survives across sessions, in almost every session.** **1,853 of 1,957 files (95%)
  report cached > 0 on their first charged response**, median first cached value **11,008
  tokens**. An independent check over raw records — first non-snapshot record of either
  stream — gives **1,823**; the two differ only because the ledger drops leading replayed
  records in fork children, so the first *charged* record is sometimes later.

  Revision 5 claimed **558**, and the cause is worth naming: `verify_schema.py` reports
  *"files whose FIRST new-stream response already has cached>0"*, which ranges only over the
  602 files that carry the explicit stream. That count is now 569 — a **94.5% rate within its
  own denominator**, essentially identical to the corpus-wide 94.6%. The rate was never
  wrong; a subset count was read as a corpus count. The correction strengthens the argument:
  there is essentially never a valid in-file baseline at session start, so "compare to
  request N−1" is not a usable method.
- **The rollout is written in completion order, not request order.** With parallel and
  sub-agent threads in flight, completion order cannot establish which request preceded
  which, nor which shared a cache. Shared `session_id` does not imply a shared cache.
- **No cache telemetry exists.** Rollouts carry no request body, no send timestamp, no cache
  key, no routing decision, no TTL. Cache *causation* is not recoverable from this data.
- **818 `compacted` records and 16 rollback markers** rewrite history and require replay,
  not a "cache break" label.

Consequently the tool reports **where reported caching diverges from prefix stability**, as a
ranked list of *candidate* explanations with explicit uncertainty. It does not assert that a
prefix "should have" hit cache, and it does not attribute breaks to TTL, tool reordering, or
prompt mutation without telemetry it does not have.

As implemented: prefix stability is measured at **item granularity** via a chained content
digest over each prompt item, so the longest common prefix between consecutive requests is
found without storing token arrays. The digest covers the item's actual bytes, including
opaque blobs. Hashing only `(kind, token count)` — the first attempt — makes two different
items of equal length collide, which *lengthens* the apparent common prefix and manufactures
leads; on this corpus it turned out to cost exactly one spurious lead out of 1,005, but the
cheap fix is worth 12% of runtime.

**The baseline is the previous request's prompt**, captured at the request boundary rather
than after that response's own output has been folded in. Extending it overstated the shared
prefix by the whole assistant turn — a 20-token request followed by a 100-token reply and a
10-token input reported a 120-token stable prefix, which the provider never saw. The effect
on this corpus is small (1,005 leads became 1,006) because most assistant output is encrypted
reasoning and therefore contributes zero tokens, but the definition was wrong.

Compaction starts a fresh hash segment, **seeded distinctly per segment**. Reseeding to a
constant meant identical content either side of a compaction hashed identically and reported
a stable prefix across a boundary that discarded the prompt.

A response becomes a *lead* when its stable prefix exceeds reported caching by
**≥ 20,000 tokens**. On the corpus **1,006 responses** qualify; the largest shows a
249,363-token stable prefix against 4,864 reported cached.

This is still worth building: divergence is the only available signal for *where* to look.
It is labelled as a lead, not a finding, in the report itself.

### 5.3 Reconciliation residual

For each response, the reconstructed prompt prefix is tokenized and compared against reported
`input_tokens`. The delta is published per session, never hidden. Contributors:

- **Tool schemas — partially available.** **8 files persist `session_meta.dynamic_tools`**
  with full `inputSchema` objects. Where present, they are consumed. Elsewhere they are
  absent. "Tool schemas are never recorded" was wrong; coverage is partial.
- **Wire envelope** — role/type framing around each item.
- **Compaction and reconstruction error.**
- **Encoding drift** (§4.1).

Tool configuration varies independently of model identity, so there is **no per-model schema
constant to subtract**. Earlier plans to measure and subtract one are dropped. The residual
is reported as unexplained.

**Measured.** Over the 136,026 charged responses that report `input_tokens`:

| | Tokens |
| --- | --- |
| Reported input | 16.685B |
| Reconstructed | 14.384B |
| **Residual** | **2.301B** |
| Coverage | **86.2%** |

The single largest identifiable contributor is re-sent encrypted reasoning: **539M characters
of opaque `encrypted_content`** sit in the reconstructed prompts and are counted as characters,
never as tokens, because they cannot be decoded. That is disclosed beside the residual rather
than converted into an estimate. A 13.8% gap is *reported*, not explained, and — per §4.1 —
it is not evidence for or against the encoding.

Prompt reconstruction itself is an approximation and is labelled as one: the rollout records
no request body, so a response's prompt is taken to be everything written before that
response's own trailing run of output items.

### 5.4 Image accounting

**23.6% of corpus bytes (1.82 GB across 1,489 `input_image` elements) is base64 image data.**
Images are not BPE-tokenizable; they are billed by a patch formula over pixel dimensions.
A text-only extractor returns 1.7 MB of text for a 249 MB image-heavy rollout — an
undercount of two orders of magnitude, silently.

Images are handled separately: decode only enough of the base64 header to read dimensions,
apply the model's patch formula, report as a distinct category. Where the formula is
uncertain, report an explicit **range**, and fold the uncertainty into the residual.

Dimension recovery is verified for this corpus: all 1,489 images are **1,100 PNG and
389 JPEG**, and every one yielded dimensions from **at most 4 KiB of decoded prefix**.
No other format appeared, and **zero images fell back to the ambiguous state**. GIF, WebP and
BMP sniffers are present so a new format degrades to "undetermined" rather than to a silent
zero; the counter for that is in §8.

The two formula families give, over the corpus: **1.131M tokens** (patch family, 32px patches
capped at 1,536) and **1.846M tokens** (tile family, 512px tiles with an 85-token base). Both
bounds are reported; only the **low** bound is folded into the category totals, and the
spread is part of the residual.

### 5.5 Derived metrics (corrected)

From the canonical ledger over 1,957 files:

| Metric | Value |
| --- | --- |
| Canonical responses | 136,026 |
| Recorded input | **16.708B** |
| Cached | 16.110B (**96.6%**) |
| Uncached | **574M** |
| Output | 90M (reasoning 47M) |
| Unique content tokens | **372.7M** |
| Resend amplification | **44.8x** |
| Total resend cost | **14.41B** (tokens × times carried in a prompt) |

Produced by `tokencounter/ledger.py`, which implements the §2.2–§2.5 exclusions rather than
merely documenting them. Excluded: 580 context snapshots, 547 immediate-repeat legacy
records (63M input), and 3,040 ancestor-replay records (307M input) — 3,587 records and
369M input tokens in total. 5 records remain **ambiguous** — singleton replay matches that
cannot be distinguished from coincidence, carrying 194,117 input tokens. They are charged,
with that fact disclosed rather than buried.

Unique content rose from the 355.7M reported in revision 5 to **372.5M**, and amplification
therefore fell from ~47x to **44.8x**. The earlier extractor omitted the environment block,
`agent_message` content and image tokens; the denominator was too small, so amplification was
overstated. It still excludes tool schemas and encrypted reasoning, so it remains approximate
and is stated as such.

Composition of that unique content — the thing reported usage cannot tell you:

| Category | Tokens | Share |
| --- | --- | --- |
| `tool_output` | 281.9M | **75.6%** |
| `tool_call_input` | 35.3M | 9.5% |
| `user_message` | 28.5M | 7.6% |
| `developer_instructions` | 7.3M | 2.0% |
| `system_prompt` | 7.3M | 2.0% |
| `environment` | 6.4M | 1.7% |
| `assistant_message` | 4.8M | 1.3% |
| `image` (low bound) | 1.1M | 0.3% |
| `agent_message`, `tool_schema`, `reasoning_summary`, `agents_md` | 88K | <0.1% |
| `reasoning_blob` | opaque | 539M characters, not tokenizable |

Also reported: per-item resend cost — tokens × times carried in a prompt — which is the
actionable ranking. The most expensive single item in the corpus is a 66,456-token review
instruction (category `user_message`) carried by 83 consecutive prompts, costing **5.52M
input tokens on its own**. The worst `tool_output` is 22,102 tokens carried 153 times,
costing **3.38M**; revision 6 named a 21,793×170 item here, which is `tool_call_input`, not
output.

**Resend cost has an exact invariant**, and checking it found a real bug. Summed over items,
tokens × prompts-containing-it is the same double sum as summing reconstructed prompt tokens
over responses, so the two must be equal. They were not: both-stream files record every
response on *both* streams, so every prompt was counted twice and resend costs were inflated
~2x for **602 of 1,952 files** — the corpus total read 18.43B instead of 14.39B. Resend
costing now counts only the stream the ledger charges, and applies the file-local half of the
ledger's rules (context snapshots and legacy immediate repeats are not model calls).

The identity now holds exactly on legacy-only and both-stream files alike. Corpus-wide a
**0.206%** gap remains (29,687,553 tokens), and it is precisely the part the per-file worker
cannot decide: ancestor replay in fork children is a cross-file judgement that only the
ledger can make. The report prints that gap next to the total rather than presenting the
larger number bare.

`scripts/test_pipeline.py` pins the invariant.

Figures drift by ~0.1% between runs because the corpus is live and grows during
measurement; they are a snapshot, not a constant.

**Coverage caveat:** legacy compaction intervals carry no usage evidence, so this is the
*recorded* total, not a reconstructed "true billed" total. The two are not the same number
and the report labels it accordingly.

Amplification is stated approximately: the denominator counts content the tool can
reconstruct, which excludes tool schemas and encrypted reasoning.

### 5.6 Weekly rate-limit windows — reported, never derived

Every `event_msg`/`token_count` record carries a `rate_limits` block beside the usage it
already contributes. Across the corpus that is **142,720 snapshots**, and it is the one
figure in the system that is *reported* rather than *measured*: nothing here derives it from
content. It is kept in its own structure and labelled as such on the page so it can never be
mistaken for something this tool counted.

**Structure, verified by `scripts/verify_schema.py`:**

| Claim | Observed |
| --- | --- |
| Carrier | `event_msg`/`token_count` only — 142,720 |
| Window key shape | `(resets_at, used_percent, window_minutes)`, uniform |
| Window lengths | `10080` (weekly) × 142,717; `300` (5-hour) × 5,684 |
| **Which slot holds the weekly window** | `primary` × 137,033 **and `secondary` × 5,684** |
| `plan_type` | `pro` × 125,392, `prolite` × 17,200, null × 128 |
| Relative `resets_in_seconds` | 0 — handled defensively, never seen |

The slot row is the trap. Older CLI builds put a 5-hour limit in `primary` and the weekly
limit in `secondary`; newer ones report the weekly limit in `primary` and leave `secondary`
null. **A window is therefore identified by `window_minutes`, never by the slot name** —
trusting the name charts a 5-hour window as a week for 5,684 observations.

#### The reset does not sit on a seven-day grid

Two behaviours make naive reconstruction wrong, and both are measured:

1. **An idle window slides.** While the limit reads 0%, the server re-quotes `resets_at` as
   *now plus seven days* on every single call. Across the corpus, **1,355 distinct
   `resets_at` values** resolve to **83 that were ever consumed** and **1,272 that never
   left 0%**. Keying a window on the integer shatters one untouched week into hundreds.
   Quotes within `CLUSTER_TOL_S` (1 hour) are therefore chained into one window — two orders
   of magnitude above the observed jitter, and two below the observed gap between genuine
   windows. A cluster that never exceeded 0% is an *idle slide*, counted but never drawn.

2. **Resets are frequently early.** A consumed window holds its `resets_at` still — median
   22.9h, up to the full 7.00d — and then is replaced *before* it would have expired. The
   transition is visible **inside a single rollout file**: one file reports 96% → 99% on one
   window and, one minute later, 0% on a new window whose reset is seven days out from
   *that* instant rather than from the previous window's.

3. **A replaced window keeps being reported.** Some sessions are still served a window
   after its successor has opened. On this corpus the week opening 09-07 10:29 was reported
   again on 09-10, 1.6 days into the week that opened 09-08 10:51, by one session out of the
   thirty-one reporting in that span &mdash; the same model, the same CLI version, and the
   same session file carrying both within minutes, so it is neither a version split nor a
   per-model bucket. Both curves drawn as-is put two live limit lines on the chart at once.
   The drawn percentage series is therefore **clipped where the next window opens**, the
   same rule the token attribution already follows, and the clipped readings are counted
   (`late_readings`, 5 across the corpus) and published in the window table. They still set
   the window's reported peak: the server did say 44%, and dropping that would be a quieter
   lie than showing it beside a curve that stops at 22%.

So **a window boundary is taken from where the reported percentage drops**, not from
arithmetic on `resets_at`. Seven-day arithmetic would put nearly every boundary in the wrong
place. The oldest window in range is the exception — its predecessor may sit outside the
corpus entirely — so its start falls back to the reported anchor and is marked
`start inferred` on the page.

#### What the chart shows, and what it refuses to say

Two independent series share one time axis and are never combined into one number:

- the **reported** percentage curve, quoted verbatim from the server, and
- the **measured** cumulative token curve, accumulated from the canonical ledger in time
  order and restarted at zero at each boundary.

No tokens-per-percent exchange rate is published, and the corpus shows why that restraint is
not merely stylistic: 95% of a window cost 2.81B recorded input in one week and 790M in
another. The limit's unit is unpublished and its weighting by model, effort and cache state
is not visible in a rollout, so the two curves are placed side by side and the reader draws
their own conclusion.

Two disclosures are published rather than smoothed away, because the logs cannot settle
either: **overlapping windows** (two reported live at the same moment, so which window a
response near the boundary belongs to is undecidable — tokens go to the earlier one), and
**boundaries with no percentage drop** (a split that may be one window whose quoted reset
moved far enough to read as two, restarting the curve mid-week for no underlying reason).

#### Rate-limit events — reported, counted per day

A snapshot also says whether a limit was reached: `rate_limit_reached_type`, null unless the
server reported one. A **rate-limit event** is a snapshot with it set, one per snapshot
whichever windows it names, so a refused request is one event even when both windows are
full. Nothing is inferred from a percentage reaching 100%. How often it is set on the
development corpus is not yet measured; `scripts/verify_schema.py` now tallies its values.

The worker keeps each event's time (`limit_events`, epoch seconds; one with no readable
stamp is counted as `limit_event_no_time`), and `analyze.limit_events` counts them per
local day, with the daily buckets' own spans. They are drawn as bars on the response-time
chart (§7), and they keep their own list of days, separate from the timed days: a day the
limit blocked outright has events and no timed response.

**A fork child's replayed copies are left out.** A child replays its parent's history
stamped with its own creation time (§2.5), the parent's snapshots among them, so counting
them would put every refusal the parent logged a second time, on the day the child was
made. The ledger's replay match cannot help: an event is usually a `token_count` with
`info: null`, which the ledger never sees. Timing can. A file opens with a burst of records
written back to back, its header and any replay a millisecond or so apart, and the worker
records where that burst ends: at the first gap over `OPENING_BURST_GAP_S` (50 ms),
`opening_burst_end`. In a file that declares a parent, an event inside the burst is the
parent's, and is counted as `limit_events_replayed` instead. A refusal of the child's own
needs a round trip to the server, so it lands after the burst. This is an inference, like
the ledger's replay match: a stall of over 50 ms in the middle of a replay would let the
later copies through, and a refusal back within 50 ms of the child's first record would be
dropped. Neither has been measured. A file with no declared parent never loses an event.

---

### 5.7 Input counted with tiktoken

Since 1.4.0 the page shows input as tiktoken counts it, not as Codex recorded it. Everything
on the page that shows input switches together, so the page never mixes the two: the input
tile, the longest-session tile and the order of `sessions`, the daily chart and the
input-by-model pie, and the cumulative curve in each limit window.

- **What is counted.** Each charged response's reconstructed prompt (§5.3): every item
  written before that response's own trailing output, tokenized with the vendored
  `o200k_base`, with images at the low bound of §5.4. It is the same `recon_input` the
  residual has always compared against Codex's figure, so the headline input and
  `residual.reconstructed` are one number. No extra tokenization is needed. The counts are
  already made for the content composition, and a prompt's total is a running sum over
  them.
- **Why it reads lower.** It came to 86.2% of Codex's recorded input on the development
  corpus (§5.3). The log does not keep tool definitions (except `dynamic_tools` in 8 files)
  or message framing, and the encrypted reasoning resent in prompts cannot be counted. The
  shortfall is the residual, and it says nothing about the encoding (§4.1).
- **What stays Codex's.**
  - Output: reasoning tokens are encrypted, 47M of the 90M output on the development
    corpus, and only their summaries are readable.
  - Cached input, uncached input and the cache hit: caching is decided on the server and
    the log has no cache telemetry (§5.2). The hit ratio is Codex's cached over Codex's
    input, never over the tiktoken count, which cached input can exceed.
  - The weekly-limit percentages (§5.6).
  - The response count: the reconstruction is anchored on Codex's usage records.

  The output and cache tiles say so, and the terminal line prints Codex's figures under
  "recorded by Codex".
- **The curve.** Each window's cumulative curve draws tiktoken input plus Codex's output.
  Output is under 1% of it (§7).
- **Fallbacks.** With `--metrics-only`, or with no tokenizer, nothing is counted, and every
  input figure is Codex's, labelled "Recorded input" as before. A charged response whose
  file has no reconstruction, or whose prompt reconstructs to zero tokens, keeps Codex's
  figure rather than contributing zero. Every request carries at least its instructions, so
  zero means the reconstruction found nothing. That
  happens with an attribution error part-way through a file, and such responses are counted
  in `tiktoken_input_fallback` and `tiktoken_input_fallback_tokens`.
- **JSON and share.** These keep Codex's figures under their old names: `totals.input`,
  `daily[].input`, `daily[].models`, each window's `tokens.input`, and the first four
  `cum_points` columns. The tiktoken counts are added beside them: `totals.tiktoken_input`,
  `totals.input_source`, `daily[].tiktoken_input`, `daily[].tiktoken_models`,
  `sessions[].tiktoken_input`, `models[].tiktoken_input`, `tokens.tiktoken_input` in each
  window, and a fifth `cum_points` column. `share.py` sends Codex's figures, so the
  leaderboard still agrees with the report's recorded figures day for day. The shared
  page's input reads lower than the leaderboard's, by the residual.

### 5.8 Response time — measured gaps and one estimate

A rollout stamps each record when it is written. It records **no send time** for a request,
so a response's time cannot be read off any one record. It can be read off two.

**What is measured.**

- **Response time.** A request goes out as soon as its prompt is complete: the user's
  message, or the last tool output. So for each usage record the worker keeps `req_ts`, the
  latest input-side record (`turn_context`, `world_state`, or an input item) **as it stood
  when that response's first output item arrived**. A response's time is its usage record's
  stamp minus the later of `req_ts` and the previous charged response's stamp in the same
  file (`latency._response_start`).
  - *Why freeze at the first output.* A tool can run while its response is still streaming.
    Its output is then written before that response's usage record, and taking the latest
    input at the usage record would date the request to the tool's finish and shorten the
    response by the tool's whole run (`worker._request_start`).
  - *Why the previous response's end.* On the explicit stream a compaction call has a
    usage record of its own and no input after the previous response; timing it, or two
    responses with no input between them, from the last input would charge each with the
    previous response's time as well.
  - *Why the `compacted` stamp.* On the legacy stream a compaction is charged nothing --
    a `compacted` record and a zero/zero snapshot -- so the previous-response floor never
    sees it. The start frozen at the next response's first output is therefore the later of
    the latest input and the latest `compacted` stamp (`worker._request_anchor`); without it
    a 60 s compaction was charged to a 2 s response. A compaction call with no output items
    keeps its own start.
  - *What ends a response besides its usage record.* An interrupted response writes output
    and no usage record, and its frozen start would otherwise date the next turn's first
    response, idle time included (305 s for a 5 s response). A `turn_context` or a
    `turn_aborted` event clears it (`worker._ends_response`). A legacy record the ledger
    cannot charge -- a repeat of the previous state, or a context snapshot -- does not: one
    can arrive mid-stream.
  - *Which items count.* Only known inputs move the start: user, developer and system
    messages, `*_output` items and `agent_message`. Anything the model emits (`*_call`, and
    the classifier's output items) freezes it, and an unknown item does neither
    (`worker._timing_side`). `classify.item_role` calls anything unknown an input, which is
    right for prompt content and wrong here: a web search the model ran would have dated
    its own response's start.
  - *Only charged rows are timed, and not every charged row.* A repeated or
    context-snapshot record never becomes a sample, because the ledger never charges it
    (§2.2–§2.5). A replayed record the ledger charges anyway -- an ambiguous one-record
    match, or every match under `--no-replay-exclusion` -- is flagged `replayed` by the
    ledger and left out (`latency._is_replay`, counted as `latency_replayed`); it carries
    the child's creation time and would read as a millisecond response.
  - *A row with no request stamp is not timed.* The previous response's end is only ever
    a lower bound on the start, never the start itself: on its own it would time the whole
    gap between two responses, and every row of a payload extracted before `req_ts`
    existed (an archived one, with `--include-archived`) would be one.

  It is **one number**: network, the server's queue, reading the prompt, writing the answer,
  and any retry. A wait for the user's approval is not in it: approval happens between a
  response's usage record and its tool's output, and that output is the next request's start.
- **Turn time.** From a turn's `turn_context` stamp to its last timed response. The model's
  share of a turn is its responses' summed time over the turn's; the rest is tools,
  approvals and Codex between calls.
- **Tool time.** From a `function_call` (or `custom_tool_call`, `local_shell_call`) to the
  output with the same `call_id`. It **includes any wait for approval**, and calls that ran
  side by side each count their full time. A fork child replays its parent's history
  stamped with the child's creation time (§2.5), so its copied tool calls sit a few
  milliseconds apart; every tool call before the file's first timed response is left out
  and counted as `tool_replayed` (`latency._own_work`). Replayed rows the ledger charged
  do not count as that first response. A file with no timed response contributes no tool
  time.
- **Caps.** A response over an hour, or a tool call or turn over two hours, is left out and
  counted, never clipped: its start is not its request's (a sleep, a late write), or it is a
  person's absence.

**What is estimated.** The rollout cannot say how much of a response was queueing. For each
model and effort with at least 40 timed responses, a line is fitted **under the fastest
tenth**: `seconds = overhead + output_tokens / output_rate + uncached_input / input_rate`, by
linear quantile regression at q = 0.1 (iteratively reweighted least squares, every term held
at zero or above). A token column enters only with at least ten distinct values, so one
response with uncached input among thousands cannot set the input rate alone, and a singular
design (two columns moving together) drops a column rather than the fit. At most 4,000
responses, evenly spaced in time, place the line. Each
response's time **above the pace** is its time minus the line, floored at zero; the rest is
called the work. None of the estimate is on the page, whose response-time chart (§7) shows
only measured times. The overhead, the output rate and the share above the pace per model,
and the median time above the pace by local hour of the day and by day, are in `--json`
(`latency.groups[].fit`, `latency.hours`, `latency.daily`), with turn and tool time; the
terminal line carries the overall share, worded as an estimate. A
model with too few responses has no line, and its time above the pace is unavailable
(`None`), never zero; the overall share covers the fitted models only, and the terminal line
says how much of all response time that is when it is not all of it
(`responses.fitted_share`).

It is **not a queue time**, and is never called one. Above the line lands queueing and
retries, but also a slow stretch of generation and ordinary variation; the line itself sits
on the fastest tenth, so it carries the network round trip and whatever queue even fast
calls saw. On synthetic responses with a known pace and planted waits the fit recovers the
overhead within 0.1 s and the output rate within 1.5 tokens/s, and the planted busy hour
stands out (`test_pace_split`).

**Not yet measured on the development corpus.** The rules above rest on the write order the
prompt reconstruction already depends on (§5.3): output items are written as they complete
and before their response's usage record, and inputs before the request that carries them.
This revision was built without access to that corpus, so §8 has no values for its
counters yet. `latency_no_start`, `latency_no_end`, `latency_nonpositive` and
`latency_over_cap` are the first place to look if a figure seems wrong.

**A measured split exists, outside the rollout.** Over its WebSocket transport the Responses
API can return server timing (`responsesapi.websocket_timing`: time in the inference
engine, time to first token, time between tokens, and the API's own time outside the
engine), requested when Codex's `runtime_metrics` feature is on. That feature is marked
under development and is off by default; Codex does not write the event to the rollout, only
to OpenTelemetry metrics and a trace-level log line. Reading it would make a log file a
second source for the report's figures (§2), so it is not read.

**Cost.** About 1.3–2.4 s, depending on the machine, for a synthetic ledger of 136,500
responses in eight model and effort groups, of which the fits are 0.05–0.1 s each. The JSON carries all of it under
`latency`: `responses`, `groups[]` (with `fit`), `hours[]`, `daily[]`, `turns`, `tools[]`
and the constants used, in `method`.

### 5.9 API value — recorded usage at list prices

What the charged responses (§2.2) would cost if each were sent to the OpenAI API at its
published list price. Not what anyone paid: a ChatGPT plan is not billed per token, and the
page says "if billed at API list prices" under the figure.

**Per response, never from totals** (`tokencounter.pricing.price`), because the rate depends
on the response:

- **Model** — the model of the turn that sent it (`turn_context`), looked up in the table;
  a dated snapshot (`gpt-5-2025-08-07`) falls back to its alias. A model the table does not
  list is **unpriced**, counted with its tokens, and named on stdout and in `--json`. Codex's
  guardian reviewer (`codex-auto-review`) is one such model.
- **Processing tier** — Codex writes an `event_msg`/`thread_settings_applied` snapshot into
  the rollout (codex-rs `rollout/src/policy.rs` persists it) carrying the tier it will
  request: `priority` for Fast mode, `flex`, or its `default` sentinel; the key is left out
  when none is set, which is a recorded standard. `worker._service_tier` reads it, and every
  usage record after it carries `tier`. **A thread's first turn never has one:** Codex
  persists a snapshot only once the rollout file exists
  (`send_event_raw_without_materializing_rollout`), and the file is created after the first
  user message, so the turn-start snapshot is dropped. Codex's own test
  `initial_plugin_ids_use_turn_context_without_extra_settings_checkpoints` asserts zero
  snapshots after a first turn. The TUI re-sends its settings on every later turn, so the
  second turn's snapshot carries the first turn's setting unless the user changed it in
  between: `worker._backfill_tier` gives the records before a file's first snapshot that
  snapshot's tier and marks them `tier_inferred`. A file with no snapshot at all (a one-turn
  `codex exec` run, a one-turn sub-agent, an older Codex) keeps no tier, is priced at
  standard and counted as `tier_unrecorded`, and `usd_high` prices those same responses at
  Fast where the model has a Fast rate. Fast is the most a tier costs, and some models
  default to it. Not logged at all: a per-turn tier override (`turn/start`'s
  `serviceTierForTurn` applies to the turn's copy only) and the tier the server actually
  *served*, which is in `response.completed`; Codex does not persist that. A tier with no
  published rate for the model is unpriced.
- **Prompt size** — above the long-context threshold (272K input tokens, read off the
  pricing page) a model with long-context rates is charged them for the whole request. At
  exactly the threshold it is short context ("Short context: ≤272K"). A tier with no
  long-context rate on such a model (Fast on gpt-5.5) leaves a long request unpriced.
- **Input split** — OpenAI's own formula (prompt-caching guide): ordinary input = input −
  cached − cache writes, each at its own rate. Cache writes are a part of input, disjoint
  from cached reads; GPT-5.6 and later bill them at 1.25× input. A model with no cached rate
  bills cached tokens as ordinary input; one with no cache-write rate, writes. Reasoning is a
  subset of output (§2.4) and is never added again.
- **Web searches** — $10 per 1,000 calls on top of their tokens, which already ride in
  input. The worker counts `web_search_call` items since each stream's last charged record
  and puts the count on the next one, per stream: a both-stream file charges only the
  explicit stream, and a search counted onto a legacy record written first would vanish
  with it. A context snapshot (§2.4) never takes them, since the ledger never charges it.
  Replayed records in a fork child drop with their searches. A replayed search that
  followed the parent's last usage record (one made before an interruption) is still
  pending when the replay ends, so in a file that declares a parent the pending counts are
  cleared where its opening burst ends (§5.6), or the child's first response would take
  it. Current Codex leaves searches out of forks anyway (codex-rs
  `agent/control/spawn.rs`).

**What is not in it**, and is said so: unpriced responses; **aborted turns** — a response cut
off when its turn is aborted (interrupted, replaced by a new turn, a review ended, a budget
limit) writes no usage record (Codex records usage only on `response.completed`), so
`turn_aborted` events of every reason are counted (`analyze.turn_aborts`, a fork child's
replayed copies left out as §5.6 does) and the count is printed beside the figure. A stream
that drops is retried, and one that finally fails is logged as an error; neither is
counted here;
Batch rates, which Codex never uses; the 10% regional-processing uplift; and whatever the
recorded total itself misses (§5.5).

**The table never comes from the network at run time.** `assets/vendor/openai_prices.json`
is written by `scripts/fetch_prices.py` from the pricing page's Markdown (the flagship
tables per tier, the Cyber models' table as standard rates, the grouped table listing the
Codex models, the web search fee and the threshold) and, for Codex models the page no
longer lists (`gpt-5-codex`, `gpt-5.1-codex*`, `gpt-5.2-codex`, `codex-mini-latest`), each
model's own page, which publishes standard rates only. Every figure is copied as printed; a
"-" stays null. `--check` diffs the vendored file against the live pages. The page the
table was built from is saved as `scripts/fixtures/openai_pricing.md` with it, and
`test_pipeline.py` runs the parser on that copy offline, asserting the vendored table is
what the parser makes of it. The loader requires a positive integer `unit_tokens` and
finite rates: JSON parses `Infinity`, which would price a response at inf or nan. The table is validated whole on load (`pricing.load`): one
malformed rate would price some responses and skip others, and a total built that way is
wrong without looking wrong. `--prices PATH` (or `TOKEN_COUNTER_PRICES`) substitutes
another table in the same format; an unreadable one costs the run its API value and nothing
else.

Prices are applied at analysis time, so the index (§3.2) holds no price: re-vendoring the
table changes the next run's figure without a rebuild. The tier and search counts are
extraction, in `worker.py`, which the extractor fingerprint already hashes.

`--json` carries it under `api_value`: `usd` (tokens plus search fees), `usd_high` (the
unrecorded tiers at Fast), `tokens_usd`, `web_search_usd` and `web_search_calls`, the table's
`as_of` and source, `priced` and `unpriced` with reasons and the models behind them, `tiers`,
`tier_inferred`, `tier_unrecorded`, `long_context`, `aborted_turns`, and `by_model`; each day and session carries
`api_usd`, null when nothing on it was valued. The page shows it as a tile (§7), only when
something was priced.

## 6. Plugin packaging

Codex plugins are directories with `.codex-plugin/plugin.json` plus `skills/<name>/SKILL.md`,
installed through a marketplace. The bundled `visualize` plugin is the reference precedent.

```
tokenCounter/
├── .agents/plugins/marketplace.json      # local dev marketplace
├── ARCHITECTURE.md
├── scripts/                              # reproducible measurement harness
│   ├── fetch_vocab.py                    # §4 one-time vendoring + parity check
│   ├── bench.py                          # §3.4 parallelism grid
│   ├── verify_schema.py                  # §2.2, §2.3 claims
│   ├── verify_install.py                 # installed copy == this code
│   ├── test_ledger.py                    # §2.5 response identity, 13 cases
│   ├── test_pipeline.py                  # §3–§7, 237 cases
│   ├── test_mutations.py                 # every fix must fail when reverted
│   ├── test_share.py                     # §6.1 payload, privacy, transport
│   └── ref_bpe.py                        # §4 correctness oracle
└── plugins/token-counter/
    ├── .codex-plugin/plugin.json
    ├── assets/
    │   ├── token-counter.svg, token-counter-dark.svg
    │   └── vendor/o200k_base.tiktoken    # 3.6 MB, sha256 446a9538...
    └── skills/
        ├── token-share/                  # §6.1 opt-in upload to tokenusage.dev
        │   ├── SKILL.md
        │   └── scripts/share.py          # imports token-report's package, no copy
        └── token-report/
            ├── SKILL.md
            └── scripts/
                ├── report.py                 # the report's only entry point
                └── tokencounter/
                    ├── rollout.py    # discovery, record iteration, prefilter
                    ├── classify.py   # payload -> (category, text) segments
                    ├── images.py     # dimensions from a 4 KiB prefix, token range
                    ├── encoding.py   # vendored o200k_base construction
                    ├── deps.py       # installs tiktoken on first use (§4.2)
                    ├── worker.py     # one file: parse, tokenize, reconstruct prompts
                    ├── ledger.py     # the canonical usage ledger
                    ├── index.py      # SQLite cache
                    ├── analyze.py    # aggregation into the report model
                    ├── latency.py    # response, turn and tool time; the pace estimate
                    └── render.py     # self-contained HTML
```

**The layout in revision 5 would not have worked.** It specified
`scripts/{collect,tokenize,analyze,report}.py` as flat modules. Python prepends a script's
own directory to `sys.path`, so `tokenize.py` would shadow the **standard library's**
`tokenize` module for the whole process — and `inspect`, `doctest` and traceback formatting
import it. The failure would have been intermittent and baffling. Everything therefore lives
in a `tokencounter/` package, with `report.py` as the only top-level script.

Install:

```
codex plugin marketplace add jack-beanstalk-2022/token-counter   # published
codex plugin marketplace add .                                  # or this checkout
codex plugin add token-counter@jack-beanstalk-2022
```

`codex plugin add` **copies** the plugin into `~/.codex/plugins/cache/`, so editing the
repository afterwards leaves the installed copy stale. That happened, while this document
claimed the installed plugin was verified; `scripts/verify_install.py` now compares the two
trees file by file and is part of the verification sweep (§11). `SKILL.md` maps natural requests ("token report",
"where is my context going", "which sessions waste the most tokens") onto the CLI:

```
report.py                          # all sessions
report.py --since 2026-09-01       # windowed
report.py --session <id-or-prefix> # single-session deep dive
report.py --fast                   # every core instead of half
report.py --metrics-only           # usage ledger only, no tokenization
report.py --json model.json        # machine-readable model
report.py --no-open                # write the file, do not launch a browser
report.py --include-archived       # count sessions whose rollout file is gone
report.py --rebuild                # discard the index and re-parse
report.py --no-install             # never install tiktoken (§4.2)
```

`SKILL.md` also carries the four caveats that must survive into anything the model says about
the output — recorded ≠ billed, cached is a subset, leads are not findings, the residual does
not validate the tokenizer — because the report is easy to over-read.

No MCP server, no daemon, no background process. The report sends nothing anywhere. Its one
network call is the first-use `tiktoken` install from PyPI (§4.2), which `--no-install` turns
off. §6.1 is the one opt-in exception, in a skill of its own, and the only code that sends
anything.

### 6.1 Sharing to tokenusage.dev

`token-share` is a separate skill so that its description, not token-report's, decides when
it runs: a request for a report never reaches code that can send anything. `share.py` imports
token-report's `tokencounter` package from the sibling skill directory rather than carrying a
copy, and runs the metrics-only pass — no tokenizer, and the index is neither read nor
written — followed by the same `ledger.build` the report uses. Days are bucketed with
`analyze._day`, so a day on the leaderboard is the same local day as in the report;
`scripts/test_share.py` asserts the two agree.

The payload (schema 1; the server's copy of the contract lives in the tokenusage.dev
repository) is per-day counts plus, per month, the top ten sessions by active time and the
top ten by tokens. A session is sent as a 16-hex-character SHA-256 prefix of its id (domain-separated, so
it matches no other tool's hash of the same id), its first and last
response time, **active time** — the sum of gaps between consecutive responses, leaving out
any gap over 30 minutes — and its counts and most-used model.

Since 1.2.0 the payload also carries every consumed weekly rate-limit window (up to 104,
newest kept), as `rate_limit_windows` reconstructs it (§5.6): its start, the `plan_type` and
the first and peak `used_percent` the server reported, and the tokens the ledger attributes
to it. The two series travel side by side, as on the report's chart; `share.py` still
asserts no tokens-per-percent rate. tokenusage.dev estimates one per plan from many sharers'
recent windows and publishes how. The windows are built from the same clamped ledger rows
as the days, so a window never holds more than the days do, which the server checks.

The payload also carries `latency`, in the shape the server's contract defines (`from`/`to`,
`responses`, `turns`, up to 50 `groups`): `latency.build` (§5.8) over the ledger rows of the
last 30 days, so its figures are the report's. It is omitted when nothing in that span could
be timed. The server's optional UTC hour and weekday buckets are not sent, since they say when
a sharer works; tool names never are.

Nothing from content extraction, no `cwd`, no titles and no account claims can reach the
payload: it is built from ledger rows, the rate-limit snapshots, and three fields of the file
result (`session_id`, `date`, and the row timestamps), and the test asserts the exact key set.
The `plan_type` in it is the rollout's, never `auth.json`'s.

Since 1.3.0 a share also publishes the report page (§7) at `/r/<handle>`, so the link the
user gets opens the same page they have locally. `share.py` runs token-report's own `main`
with `--public --no-open`, which renders the page from the full pipeline (the index is used
as usual) with two differences: the top-session tile drops its note, the session id prefix
and the `cwd` basename, the only strings on the page taken from the machine rather than
counted, and a tokenizer failure is described without the exception text, which names the
vocabulary's or the install directory's path; and `auth.json` is not read. The response-time chart goes with the rest: the
median and p90 response time per day, and the rate-limit events per day (§5.6). Tool names, which come from the machine
(MCP servers among them), are on neither page. The dry run and the token-share skill both
list the chart among what is published. `--style` bakes the style the page opens in, over any
style the reader has remembered from another report; without it, a remembered style wins over
the first. The page is written to disk on the
dry run, so what goes public can be opened first, and on `--yes` it is gzipped and `PUT`
after the numbers under the same token. tokenusage.dev serves it with
`Content-Security-Policy: sandbox allow-scripts` and nothing else allowed, so it runs with an
opaque origin and can fetch or post nothing; the page needs nothing more, being built to
render offline. `scripts/test_share.py` asserts the page sent is the page the dry run wrote,
and that no prompt, directory or session id is in it.

Since 1.4.0 that page counts input with tiktoken when the report could (§5.7), so its input
tile and daily chart read below the leaderboard's recorded input. The payload is unchanged.

Since 1.8.0 the payload also carries the API value (§5.9), priced by the same
`analyze.ApiValue` as the report: `api_usd` on each day and each session (null where nothing
was valued — no priced response and no search fee — never a 0 that reads as free), and a top-level `api_value` with the total, the
price table's date, priced and unpriced counts, tiers, the Fast upper bound and aborted
turns. tokenusage.dev
does not read these yet: its `SharePayloadSchema` (zod) strips keys it does not know rather
than rejecting them, which was checked by parsing a `--out` payload with the server's own
schema. They cost a share nothing until the server reads them, and the published page shows
the tile either way.

It is dry-run by default and sends only with `--yes`. A damaged record with cached > input
or reasoning > output is clamped and counted rather than failing the share, because the
server rejects that arithmetic outright. The first share returns a bearer token, stored per
API endpoint in `share.json` with mode 0600; the server keeps only its hash.

---

## 7. Report

Single self-contained HTML, dark/light, no external requests — **151 KB** over a 22-day,
2,081-file corpus, rendered in 1.9 ms, before the Nocturne scene's script, which adds about
75 KB to every report whatever its corpus. Charts are inline SVG; the interaction is ~300 lines
of vanilla JS over an embedded JSON blob. A test asserts the output contains no `http://` or
`https://` reference at all.

The headline numbers and four charts. Everything else the model carries — sessions,
reconciliation, images, the window table, the data-quality counters — is reported through
`--json` and the stdout summary, not here (rev 12).

1. Tiles — input (counted with tiktoken, §5.7), output, cache hit, the API value (§5.9;
   only when something was priced, noting how many responses were not), sessions, the
   longest session, the median response time (§5.8), and the weekly limit as the server's
   own reported percentage -- or, once that window has reset with no reading since, how long
   ago it reset. Logs that quote no weekly window get the longest one they do, named for its
   length ("5-hour limit"), here and on the chart
2. **Cumulative tokens per weekly limit window** — tiktoken input plus Codex's output,
   restarting at zero at every reset, with the reported percentage overlaid (§5.6)
3. **Daily input**, stacked by the model that was charged for it
4. **Response time by day** (§5.8) — two lines, the median response (solid, `--uncached`)
   and the p90 (dashed, `--dim`), a point at the middle of each day. A third chart on the
   shared time axis (§7.1), drawn by the page (`drawLat`) as the limit chart's curves are,
   over a value axis fixed over the corpus and rounded up to a clean ceiling. A day with
   fewer than five timed responses is not a point, and it and any day with none break the
   line rather than letting it bridge them; every day keeps a hover target that says what it
   has. Behind the lines, a bar a day counts the rate-limit events Codex logged (§5.6),
   in `--warn` at half opacity, on an axis of its own at the right whose ceiling is even, so
   its middle guide reads a whole number. The bars have their own days: a day with events
   and no timed response still gets a bar, a hover target, and the chart itself when nothing
   was timed at all (the legend then says why there are no lines). With no events, the
   legend says none were logged. Nocturne shows it as a sixth exhibit with a builder of its
   own (`N3.lines`): a lit tube for the median, a dashed one for the p90, a slab behind them
   for each day's events, a hover target a day.
5. **What filled the window** — independently tokenized content, by category

The per-model response times and pace estimates, the hour of day, turns and tools are in
`--json` only (rev 19 removed the panel that showed them).

Every figure derived from inference rather than measurement carries a visible `inference`
badge; measurements carry a neutral `measured` badge. The page opens with a standing note
that these are *recorded*, not billed, counts.

**Styles.** The page carries three styles over one markup — **Clinical** (the original look,
light or dark with the system, and the default), **Matisse**, and **Nocturne**, a 3D scene
(below) — cycled by the dot at the top right of the bar or `[` / `]`. The dot carries no words
or symbols: it is drawn in the style it switches *to* (a blue disc on a white ring for
Clinical, a cobalt gouache cut-out over a sage sheet for Matisse, a lit gold sphere on a
night-blue ring for Nocturne), with fixed colours rather than the page's variables, and
names that style only to assistive tech. Styles are remembered in `localStorage` and the URL hash (`#style=matisse`); a
remembered style the page no longer carries falls back to the first. A style is CSS keyed on
`html[data-style]`; every chart colour is a CSS variable, so the charts restyle without being
redrawn except to re-measure a panel whose width changed. **Matisse** is the late cut-outs,
*papiers découpés*: cream paper, a torn sage sheet and a dusty-rose one pinned behind the page,
white brush dashes, and an ink flower on a stem of cut leaves. Tiles are gouache cut-outs,
each panel sits a few millimetres out of register over a coloured sheet, and pie slices are
cut apart. The collage is inline SVG drawn in Python from a fixed seed — the same edges every
time, nothing fetched — and hidden under every other style. Its categorical palette is
checked for colour-vision separation against its own panel: neighbouring slots, and every
pair among the first four. (Clinical's palette predates that check and does not pass it: its
first two slots converge under deuteranopia.) `prefers-reduced-motion` stills the flower and
the switch's fade.

**The WebGL layer.** Under Clinical (`GL_STYLES` in `render.py`) the chart *marks* — the
limit chart's area and curves, the daily bars, the pie slices — are painted by WebGL2, so
the water (below) can bend them. Every chart, in every style, records what it drew as plain
shapes in its SVG's own units (`SCN`: areas, polylines with optional dashes, rects, pies,
colours as CSS variable names). The layer puts one canvas behind each panel's content, reads
the shapes, and draws them into a 4× multisampled buffer that is resolved to a texture and
put on screen through **one post pass**, which draws the panel through the water. The SVG is not replaced: it still
draws the grid, the axes, every label and every tooltip, and its marks stay in place with
zero opacity so the pointer still finds them (the time charts' hover titles, the pie slices'
legend highlight). The layer draws in the same task as
the SVG it replaces (a microtask, not the next frame), so axes and marks never part during a
drag; colours are read from the style's own variables, so light and dark follow the system.

The marks are always drawn plain: there is no effect switcher. (Earlier revisions carried
a catalogue of post-pass effects and an **FX** button to cycle them; both were removed.) No
WebGL2, a failed context, a lost context, a post pass that fails to compile, or any other
style: `data-gl` comes off the root and the SVG marks are simply visible again.

**Water.** The page is seen through water. The surface is
simulated: a height field on a grid over the viewport, six CSS pixels a cell, where every step
pulls each cell's velocity toward its neighbours' mean height, evens it out a little with
theirs, and damps it, so a disturbance travels outward as rings, reflects and interferes, and
dies away. A pointer moving over ~0.3 px/ms (not a touch, not a drag of the charts) drags a
soft brush through it along its path, harder the faster it went; slower than that, reading or
hovering a tooltip, it leaves the water alone. The post pass reads the surface's slope under
each pixel and samples the panel that far away -- a refraction -- with red and blue split
slightly either side, so an edge fringes as through a lens. There is no lighting by default
(`WAVE.light`); every tunable -- cell, brush, speed, damping, viscosity, refraction, dispersion
-- is one entry in `WAVE` in `GL_JS`. One field serves every canvas and is kept to the page
as it scrolls, so one ring crosses the tiles and the charts as a single surface. The headline
tiles join the layer for this: their text is drawn into a texture glyph by glyph, where the
browser laid each one out and in its computed font, colour, spacing and case, and the text
beneath stays in place, selectable and readable, just not painted. The simulation steps only
while there is motion in the water and stops, flat, once it settles; `prefers-reduced-motion`
makes none.

**Nocturne: the report as a 3D scene.** Under Nocturne (`SCENE_STYLES` in `render.py`) the
page is not painted at all. One canvas fills the window with a WebGL2 scene, and the page
stays in place beneath it, unpainted and taking no pointer, so every number is still text for
assistive tech. The scene is after Whistler's *Nocturnes* — the Thames at night in blue and
gold, gas lamps doubled in still water, the falling sparks of *The Falling Rocket*: a hazy
night sky over a still river, a far shore strung with lamps, and gold sparks drifting down.
Nothing in it is 2D except words, which are drawn into textures and set in the scene.

Each chart is an **exhibit**, a physical object on a plinth. The weekly windows are panes of
glass, one a window, each cut to its cumulative curve and standing on edge along the time
axis, with the curve as a lit edge; the reported percentage is a gold wire strung in front,
in dashes as on the page, against its own axis; a rod marks each reset. The daily input is a
skyline, one block a model a day, stacked, with a sliver of night between blocks. The two
pies are medallions standing over round plinths, cut into slices of one thickness, so the
angle stays the only measure. The headline numbers are words afloat over a slab, with the
product's name cut into its edge. Dates, legends and value labels are set into the stone
or float beside the solid, and every legend is complete, wrapping onto more lines rather
than dropping an entry.

**One exhibit stands in front; the rest hang in the sky.** The camera does not fly. It
stands on the bank with its eye at the plinths' height, so the horizon runs behind the stone
and the data is seen against the night. The exhibit in front hovers over the water as near
as it can come while it stays on screen and under the sky's row, and square to the camera,
so a height or an angle is read without foreshortening. Its whole reflection is in the
water below. The others hang in a row in the sky (two rows on a portrait screen), turned
toward the camera. **Clicking one brings it down**: it falls like a leaf, swinging side to
side, and where it meets its place the water rings; the one it replaces rises back to the
vacated slot like a released lantern, drifting wide of it. `←` / `→` and the digits do the
same from the keyboard, and a polite live region names the exhibit brought forward.

**The scene draws only what the page draws.** Every solid is built from the marks its chart
recorded in `SCN` — the same shapes the WebGL layer paints — placed by the plot rectangle
recorded with them, so a bar is exactly as tall, relative to the rest, as it is on the page.
The time charts in front take the page's own gestures and move the **one shared viewport**:
the wheel zooms, a drag pans, a pinch zooms on touch, a double-click returns to the full
range, and the pies recompose in the scene as they do on the page. A drag anywhere else turns
the camera a little around the exhibit in front, and a wheel moves it nearer or farther; a new
exhibit brought forward resets both. Hovering a day or a window answers in words above the
chart; hovering a slice slides it out and lights its legend line. On a portrait screen both
time charts are built half as wide, as the page's own charts keep their height and give up
width on a phone.

Nocturne's categorical palette was chosen with the validator (`validate_palette.js`, dark
mode, against the plinth `#10202a`): all thirteen slots inside the dark lightness band and
over the chroma floor, neighbours clear of the colour-vision floor (worst ΔE 9.7) and the
normal-vision floor (22.5), every pair among the first four as well (9.7 and 19.2), and each
at 3:1 or more. It leads with gold and blue, the two colours of the Nocturnes. The style
switch in the corner is drawn by the scene too: the next style's dot as a small coin that rocks
and turns over under the pointer, with the real button over it for the click and the keyboard.

No WebGL2, a context lost, or a shader that will not compile, and `data-s3d` comes off the
root: the page is painted again in Nocturne's own 2D sheet, dark blue and gold.
`prefers-reduced-motion` stills the scene: exhibits arrive at once, the water is flat, no
sparks fall, and nothing is drawn between changes.

### 7.1 One time axis, one viewport

Charts 2, 3 and 4 answer different questions about the same hours, and are only useful
together if a moment can be found in each. They are drawn over **one domain**, at one viewBox
width, with one pair of margins, and they carry the same ticks — generated once per redraw
and handed to all three — so a date sits at the same x in each and a spike in one (a slow
day) can be traced to the others (the usage that day, the limit it spent). The domain covers
the limit series, the daily buckets, the response-time days and the content buckets, so
nothing the page can draw falls outside it.

The viewport is shared. Scrolling, dragging or pinching **any** of them moves all three, and
a double-click on one returns them all to the full range. Wheel zoom and the double-click
**glide** rather than jump, in every style: the view eases toward its goal over about a
quarter of a second, as a true zoom -- the moment that sits at the same x in the view it
leaves and the view it reaches stays put the whole way, and the span changes geometrically --
and wheel turns that land mid-glide steer the goal, so a fast spin runs as one motion. A drag
or a pinch follows the hand directly and takes over from a glide at once;
`prefers-reduced-motion` jumps. **Only the plot answers a mouse**: the rectangle inside the
axes, from the left axis to the right and from the top guide to the baseline. The tick
labels, the date axis, the legend and the margins stay the page's, so a wheel turned over
them scrolls it, and the grab cursor shows only over the plot. (Touch is not limited: the
charts are `touch-action: pan-y`, which already leaves a vertical swipe to the page.)
**Zoomed all the way out, scrolling down scrolls the page.** A wheel gesture (events under
250 ms apart) belongs wholly to whichever it started on, tracked for the whole page: a swipe
that zooms out to the full range spends its momentum on the chart and the next one scrolls,
and a page scroll that carries a plot up under a still pointer goes on scrolling. **Zoom is horizontal
only.** Each chart's value axis is fixed over the corpus, never over the viewport, so a bar's
height and a curve's height mean the same thing at every zoom level; rescaling y to the
visible slice would make two views of the same chart quietly incomparable.

Chart 5 has no time axis of its own, so it follows the viewport by filtering. Tokenized
content is deduplicated **per rollout file** (§3.3), which makes the file the finest unit its
categories can honestly be placed on: `analyze` buckets each file's categories at the hour
the file opened (`CAT_BUCKET_S`), and a bucket counts when it overlaps the visible range.
Zooming inside one file's hour therefore does not subdivide it, and a view much narrower than
a day mostly shows the gaps between sessions — so **zoom stops at one day**, the unit the daily
chart is drawn in. A range with nothing in it (an idle day) keeps the pie's place as a hollow
ring and says why, rather than collapsing the panel. That series is
36% of the page's bytes, which is the price of a composition chart that answers *for the week
on screen* rather than for the corpus.

A pie slice has no tooltip: hovering one lights its line in the legend beside it, which
already carries the name and the share, and dims the others; the highlight holds while the
pie turns under the pointer. (A slice under 0.05% has no legend line, so lights nothing.)

The pies follow a beat behind. A drag or a zoom redraws the time charts on every frame, and
recomposing two pies at that rate reads as flicker, so they wait until the viewport has been
still for 180 ms and then turn from the slices they show to the new ones over about half a
second. Each pie draws the same keys in the same order every time, zero-valued ones included,
so a slice grows from nothing or shrinks away instead of jumping; a move during a turn starts
the next one from where it had got to. The legend reads the new shares at once. This is in
every style, and `prefers-reduced-motion` skips the turn.

What the renderer owns and what the page owns:

| | |
| --- | --- |
| Daily bars | Rendered in Python in **unit x** — one unit is one local day — carrying the transform for the full domain. The page rewrites only that transform. Nothing vertical is ever recomputed, and the chart is correct before the script runs |
| Day spans | `analyze` publishes `start`/`end` per daily bucket. Buckets are *local* calendar days (§3.3), and converting a local date to an instant is DST-sensitive; it is done once, where the bucketing is, not again in the renderer |
| Width | The page re-derives it from the panel: one viewBox unit is one CSS pixel. A fixed 980-unit viewBox scaled onto a 360 px phone renders 11 px axis text at four |
| Ticks | Chosen by label pitch, not by a target count. A count tuned for a 1,040 px desktop plot leaves a phone showing one label, because the step jumps 2 days → 7 days |
| Touch | `touch-action: pan-y`: a vertical swipe still scrolls the page on a phone, while a horizontal drag and a two-finger pinch reach the chart |

`scripts/test_page.js` runs the page's own embedded script against a stub DOM — Node only, no
npm, no browser — and asserts what Python cannot see: that both charts emit identical ticks,
that a day bar starts where the limit chart puts that instant, that zooming moves x and
leaves the value axis alone, that dragging moves the range by the distance dragged, that the
viewport cannot leave its domain, and that the pie recomposes with it. The Nocturne scene's
runtime needs WebGL2 and stays out of the stub, but its pure half (`N3`) is checked against
the same marks: one pane of glass a window, as tall as its curve; the gold wire reaching the
reported peak; one block a stacked segment, the tallest where its bar stands; slices that
close the circle and a pointer that names the slice under it; an exhibit in front that fits
the screen square to it, landscape and portrait; sky slots that never overlap; and a flight
that starts and ends exactly where it should.

---

## 8. Data-quality counters

Surfaced in the report, not swallowed. Current corpus values:

| Counter | Value |
| --- | --- |
| Files charged on the explicit stream | 606 |
| Files charged on the legacy stream | 1,341 |
| Files with no usage data | 7 |
| `token_count` events with `info: null` | 8 |
| Context snapshots excluded | 580 |
| Legacy records violating `total == in + out` | 855 (818 compaction / 16 aborted / 20 repeat) |
| Raw repeated legacy snapshots, before dedup | 739 |
| Legacy immediate repeats dropped | 547 (63M input tokens) |
| Ancestor-replay records dropped | 3,040 (307M input tokens) |
| Files carrying fork-inherited history | 39 |
| Ambiguous provenance (charged, disclosed) | 5 (194,117 input tokens) |
| `response_id` charged in two files of one session | 0 |
| Forks with inherited history but no copied header | 3 |
| Plaintext reasoning summaries recovered | 1,525 |
| Cumulative counter decreases | 7 |
| `turn_context` lacking `root_turn_id` | 6,987 / 9,282 |
| Files with a second `session_meta` header | 58 (44 with a differing thread id) |
| Duplicated `compacted` usage records (never charged) | 255 |
| `compacted` records | 819 |
| Files persisting `dynamic_tools` schemas | 8 |
| Responses with `cache_write_input_tokens` absent | 16,210 (present elsewhere, always 0) |
| Files whose first charged response reports cached > 0 | 1,853 |
| Images with undetermined dimensions | 0 of 1,489 |
| Indexed sessions whose rollout file is gone | 0 |
| Complete JSONL records that failed to parse (lost) | 0 |
| ... that looked like usage records | 0 |
| Lines that are not JSON objects (metrics path) | 0 |
| Damaged records in out-of-window ancestors | 0 |
| Files that errored during extraction | 0 |
| Fork-replay exclusion applied (1 = yes, heuristic) | 1 |
| Responses timed (`latency_samples`) | not yet measured (§5.8) |
| ... with no request start, no end stamp, a non-positive time, or over the cap | not yet measured |
| Charged replayed rows left untimed (`latency_replayed`) | not yet measured |
| Tool calls timed / replayed / non-positive / over the cap / unmatched / without output | not yet measured |
| Turns with no opening stamp, or over the cap | not yet measured |
| Rate-limit events counted (`limit_events`) | not yet measured (§5.6) |
| ... replayed into a fork child and left out (`limit_events_replayed`), or with no readable stamp (`limit_event_no_time`) | not yet measured |

Every one of these is rendered in the report, not swallowed. A change across runs indicates
schema drift and is itself reportable — that is the point of surfacing them.

**§8 is a snapshot of a live corpus.** It gained 23 files during the session that produced
revisions 6–8, and an independent reviewer measuring hours later saw 612 explicit-stream
files against the 606 recorded here, 9,293 turn contexts against 9,282, and 1,856
first-response-cache files against 1,853. That is drift, not disagreement. What must not
drift are the exclusions — 580 / 547 / 3,040 / 5 — which held across every rerun and across
the ancestor-chain restriction.

**Damage counters cover every file the ledger touched**, including out-of-window ancestors.
A corrupt record in an ancestor changes what a windowed report charges, so filtering its
counter out of that report hid the reason the numbers moved.

"Files charged on the legacy stream" previously read 1,348 because it counted the 7 files
with no usage at all. The corruption counters are new in revision 7: a complete JSONL line
that fails to parse is a **lost record**, and if it carried usage that usage silently left
the ledger. It was being swallowed without a counter. Zero occur on this corpus, which is
worth knowing rather than assuming.

---

## 9. Known limits

Structural, not deferred work.

- **Reasoning content is mostly encrypted.** `reasoning` items carry opaque
  `encrypted_content`. Plaintext `summary_text` is recovered where present (§5.1, 1,525
  elements), but full reasoning cannot be independently tokenized; output reasoning is taken
  from reported `reasoning_output_tokens`, and re-sent reasoning lands in the residual.
- **Recorded usage is not billed usage.** Legacy compaction intervals carry no usage
  evidence, so totals are reported as *recorded* coverage with the gap stated (§5.5).
- **Cache causation is unrecoverable** — no request-side telemetry exists (§5.2).
- **Prompt reconstruction is an approximation** (§5.3), and 13.8% of reported input is
  unexplained residual.
- **`cache_write_input_tokens` is always zero** on the development corpus and yields
  nothing there (§2.3). GPT-5.6 and later report cache writes, and the API value prices
  them (§5.9).
- **The API value is list price for recorded usage**, not a bill: unpriced models, an
  unlogged served tier, an unrecorded tier (bounded by `usd_high`), responses cut off by an
  aborted turn and the regional uplift are outside it, and each is counted or stated (§5.9).
- **Tool schema coverage is partial** (§5.3).
- **Model encoding is unpublished**, and the residual cannot settle it (§4.1).
- **Image token formula is model-specific** and may be approximate (§5.4).
- **Append-only is observed, not guaranteed** (§3.2), and incremental tailing is not
  implemented — a changed file is re-parsed whole.
- **Resend cost exceeds the canonical figure by 0.206%.** Cross-file ancestor replay is not a
  per-file judgement; the gap is printed beside the total (§5.5).
- **Clock skew is unchecked.** Files are ordered by `started_at`, so a child timestamped
  before its parent would invert the ancestry and misattribute, though the declared-parent
  restriction (§2.5) now bounds the damage to genuinely related threads.
- **A concurrent append can yield one stale cache hit** through the stat/hash/read race; the
  next run corrects it.
- **Rollouts are the only source of every figure.** Anything Codex does not persist is
  invisible. The sole non-rollout read is `auth.json`, for the account's name only (§2.6).
- **Account claims are unverified and may be stale.** No signature check is possible without
  a network call, and the claim set varies between token issuances (§2.6).
- **Rate-limit percentages are the server's, not a measurement**, and no tokens-per-percent
  rate is derivable: 95% of a window cost 2.81B recorded input once and 790M another time
  (§5.6).
- **Response time has no send stamp behind it.** A request's start is the input it was
  waiting for, which assumes Codex sends as soon as that input is written. The split into
  work and time above the pace is an estimate, not a queue time, and the timing rules have
  not yet been checked against the development corpus (§5.8).
- **Tool time includes approval waits**, and calls that ran side by side each count their
  full time (§5.8).
- **Rate-limit events are the snapshots Codex logged** with a limit reached. A refusal it
  did not log is not counted, and a fork child's replayed copies are told apart from its
  own refusals by timing alone (§5.6).
- **Window boundaries are inferred from where the reported percentage drops.** Overlapping
  windows and boundaries without a drop are counted and published, because the logs cannot
  settle which reading is right (§5.6).

---

## 10. Revision history

**Rev 23** — after a review of the whole repository. Every finding was checked against the
code, and the ones a test could reach were reproduced first. Plugin 1.8.1.

| Change | Cause |
| --- | --- |
| The temp-directory fallback is `token-counter-<uid>`, made 0700, and trusted only while it is a real directory this user owns that no one else can open (`deps.private`); `activate` skips it otherwise, and `out_dir` then takes a fresh `mkdtemp` | **Security:** the fallback was `/tmp/token-counter` on Linux, which any local user could create first. `activate` searched it on every run that had no tiktoken yet, so a planted `tiktoken` package ran as the victim; the report, the index and the share token were written there too |
| A report that cannot be written goes to a new `mkstemp` file; the share token through one too | Both opened a fixed name in a directory others could write, following any symlink planted at it |
| `scripts/fetch_vocab.py` stages the blob and checks its sha256 before it replaces the vendored file | A cache blob from the shared `$TMPDIR/data-gym-cache`, or a cut-off download, replaced the vocabulary first and failed the check after, leaving it in the tree |
| The worker catches an error per record, counts it as `malformed_records` (a damage counter, carried out of the window too), and goes on | One record with a list for `info`, a string for `usage`, a list `call_id` or `response_id`, or a dict model raised out of the pool; `_run` read the exception as a pool that could not start, re-ran the corpus serially, and failed the run anyway. The test for a list `call_id` ran only the ledger pass |
| A rate-limit reset of `Infinity` counts as no reset | `int(inf)` raised, and took the record's usage with it |
| A fork child's opening burst does not feed `note_rate_limits` (`rate_limit_replayed`); `analyze.replay_end` is the one rule `limit_events` and `turn_aborts` read | The replayed snapshots were dated at the child's creation: with the parent in range they read as late and overlapping readings, and with it out of range (`--since`) they opened a spurious window and moved the current one's reset |
| On the explicit stream only a charged record ends the response in flight | A zero/zero context snapshot cleared the frozen start, as the legacy stream had been fixed not to, and a tool output after it became the next response's start |
| A turn's effort is its own `turn_context`'s, or none | A turn on a model with no reasoning effort was labelled with the previous turn's |
| `classify.item_role` reads any `*_call` as output | A `web_search_call` between a response's reasoning and its message split the trailing output run, so the reasoning summary was counted into that response's own prompt |
| The tool-call replay boundary applies only to files that declare a parent | A non-fork file's tool calls before its first timed response (an interrupted first turn) were dropped as `tool_replayed` |
| `latency.build(since=)`: share times the last month with every row as a floor | Rows were filtered first, so the month's first response in each file lost the previous response's end as the floor of its start |
| The page's domain no longer includes the wall clock | A report with `--until` in the past, or after a long break, stretched every chart to today. The page test had zoomed by a factor that only cleared the one-day minimum because of it |
| The limit tile and stdout line say a window has reset rather than showing its last reading; a non-weekly window is named for its length | After a reset with no use since, the tile read "87%, reported by the server" and stdout gave a "next" reset in the past. Logs with only a 5-hour window were labelled weekly |
| The public page's tokenizer note carries no exception text | The first line named the vocabulary's path, so a damaged install published the home directory to `/r/<handle>` |
| `--session` matches within the date range, and says so when nothing in it matches | A session outside the range made a prefix ambiguous, and one wholly outside it produced an empty report with no message |
| `glob.escape` on the sessions root | A `[` in `CODEX_HOME` turned the root into a pattern and the corpus read as empty |
| `--style` marks the page (`data-style-set`) and the page opens in it over a remembered style; without it, the remembered style still wins | `report.py --style matisse` opened in whatever style the browser last remembered |
| The page's script has no escape Python does not know | `\(` and `\w` in a non-raw string are a SyntaxWarning on 3.12 and later |
| Worker processes are capped at 61 on Windows | `ProcessPoolExecutor` refuses more there, and the refusal sent `--fast` on a large machine down the single-process path |
| `encoding.load` caches by resolved path; the extractor fingerprint is computed only when the index is used | The vocabulary was built twice per run, and `--no-cache` and `--metrics-only` paid for a key they never used |
| `rollout.codex_home` is the one place `CODEX_HOME` is read | Five copies of the rule, in `report`, `rollout`, `deps`, `index` and `account` |
| `verify_install.py` picks the newest installed version by number | `'1.9.0'` sorted after `'1.10.0'` |
| The token-report skill no longer mentions a zoom toolbar; the manifest no longer says "no pricing" or that the report never leaves the machine | The toolbar was removed in Rev 13; the API value (Rev 22) and the published page (1.3.0) made the other two false |

**Rev 22** — the API value: recorded usage at OpenAI's API list prices (§5.9). Plugin 1.8.0.

| Change | Cause |
| --- | --- |
| A headline tile and a stdout line: what the charged responses would cost at API list prices, worded as a counterfactual | Requested. Reverses the §1 non-goal "no cost translation"; the replacement non-goal is a reconstructed bill |
| Priced per response, by model, requested tier, prompt size and OpenAI's input split | A total cannot say which responses crossed 272K or ran in Fast mode, and the rates differ by 2× and more |
| The worker reads `thread_settings_applied`'s `service_tier`; a record with no snapshot before it is priced at standard and counted | Codex persists the requested tier there and nowhere else; `turn_context` carries none. Older rollouts have no snapshot, and saying so beats guessing Fast or standard silently |
| Web searches are counted per stream and put on that stream's next charged record; a context snapshot never takes them | A both-stream file charges one stream, and a snapshot is never charged: either way the searches would have vanished. Two mutation cases |
| `turn_aborted` events are counted, fork replays left out, and printed beside the figure | A response cut off by an interruption writes no usage record; its cost cannot be priced, only disclosed |
| Prices vendored by `scripts/fetch_prices.py` from the pricing page and model pages; never fetched by the plugin | The report runs offline, and two runs over the same logs agree until the table is re-vendored on purpose |
| A model, tier or long prompt the table has no rate for is unpriced and named, never folded in | A guessed rate makes a wrong total that looks right |
| The share payload gains `api_usd` per day and session and a top-level `api_value` | Requested, ahead of the server reading them; its schema strips unknown keys, checked against the schema itself |
| Review before merge, each finding reproduced first: a thread's first turn takes the tier of the file's first settings snapshot (`tier_inferred`), and `usd_high` bounds the files with none | **Major:** Codex never persists the first turn's snapshot (the rollout file does not exist yet), so every thread's first turn, and every one-turn `exec` run, priced at standard under a doc that blamed "older Codex". A Fast-mode session read $0.73 against $1.31 |
| The loader requires a positive integer `unit_tokens` and finite rates; a table with no date reads "an unknown date" | A `--prices` table with `unit_tokens: 0` raised `ZeroDivisionError` and failed the run; `Infinity` priced at nan and wrote `$nan` and a bare `NaN` into the JSON |
| A fork child's pending searches are cleared where its opening burst ends | A replayed search made before the parent's interruption was charged again to the child's first response: three fees for two searches |
| `fetch_prices.py` reads the Cyber models' table; the page is kept as a fixture and the parser tested on it offline | `gpt-5.6-cyber` and `gpt-5.5-cyber` are published and were unpriced; the parser had no offline test |
| `interrupted_turns` became `aborted_turns`, and a dropped stream is no longer said to be one | Every abort reason is counted (interrupted, replaced, review ended, budget limited), and a stream that fails is logged as an error, not an abort. Four mutation cases added |

**Rev 21** — rate-limit events per day, as bars on the response-time chart (§5.6, §7).
Plugin 1.7.0.

| Change | Cause |
| --- | --- |
| A bar a day behind the response-time lines, on an axis of its own: the snapshots in which Codex logged a limit as reached | Requested: how often the limit was hit, beside how slow the days around it were. Reported, as the window percentages are; nothing is inferred from a percentage at 100% |
| One event a snapshot, whichever windows it names | A refused request is one refusal, even when the 5-hour and the weekly windows are both full |
| In a file that declares a parent, an event inside the file's opening burst of records is left out and counted (`limit_events_replayed`) | A fork child replays its parent's snapshots stamped with its own creation time, and the ledger never sees an `info: null` snapshot to match it. Two mutation cases: a burst that ends at once counts the copies, one that runs on into the child's work drops its own refusal |
| Event days are kept apart from the timed days, and are enough on their own for the chart to be drawn | A day the limit blocked outright has events and no timed response; dropping it would hide the days the limit mattered most |
| The bars move with the shared viewport and are recorded as marks for the WebGL style (the rect mark now takes an alpha) and for Nocturne (a slab a day) | One chart on one axis (§7.1). The page test checks every bar is centred on its day, as tall as its count, labelled on its own axis, wider and no taller when zoomed, and that each event bar becomes one slab |
| The terminal summary gains a line of rate-limit events and the days they fell on; `verify_schema.py` tallies `rate_limit_reached_type` | How often the field is set on the development corpus is not yet measured; the tally is how to measure it |
| The token-share skill and dry run name the events among what the published page carries | The page goes public as is (§6.1) |

**Rev 20** — response time by day, on the shared time axis (§7, §7.1). Plugin 1.6.0.

| Change | Cause |
| --- | --- |
| A response-time chart under the daily input: two lines, the median and the p90 response per day | Requested: response time was a tile and a table, with no way to see a slow day against the usage and the limit around it. It was first drawn as bars capped by the estimated time above the pace; on request it became the two measured lines, and the estimate left the page |
| Drawn by the page (`drawLat`) from per-day rows, as the limit chart's curves are, with the same ticks and `X` | A line's points move with the viewport. The page test asserts every point sits at the middle of its day at the page's own `X` -- over that day's bar in the daily chart -- at full extent, zoomed and on a phone, and that the 3D builder makes a hover target a day |
| Days come with `analyze._day_span` spans and a p90, and a row each in the page payload | The spans are the daily bars' own, DST handling included |
| A day under five timed responses is not a point, and a thin or missing day breaks the line | One slow response is not a slow day, and a line across a gap would draw days that have no data. The page test checks no two consecutive points of a line are more than a day and a half apart, and fails when the line bridges |
| The response-time panel below the pies is removed; the chart carries only its legend, and a chart with nothing timed says why | Requested. The tile and the chart carry response time on the page; the per-model table, the hour of day, turns and tools stay in `--json` |
| `test_page.js` builds each bar chart's stub from its own markup | It took every bar after `id="dailychart"`, so the new chart's bars would have been read as daily input |
| The page fixture gives the response-time days enough responses to draw, and keeps one thin | The shared fixture times one response a day, which draws no point and would leave the shared-axis checks nothing to move; the thin day and the corpus's own two-day gap give the line two breaks to keep |

**Rev 19** — the vocabulary is parsed from its own file, and CI runs every suite. Plugin 1.5.1.

| Change | Cause |
| --- | --- |
| `encoding.read_ranks` parses the vendored `.tiktoken` file; `encoding.load` no longer imports `tiktoken.load` | Found by running the suites on Python 3.8, which the README promises: tiktoken 0.7.0, the last release for 3.8, cannot read a local path without `blobfile`, so a 3.8 run had no tokenizer. Four `test_pipeline.py` cases fail there. The report itself only looked right where a newer tiktoken had already left the vendored file in `/tmp` |
| The same change stops the vocabulary being served from `$TMPDIR/data-gym-cache` | Newer tiktoken caches any path it reads under `sha1(path)` and never checks the copy against the file, so a vocabulary damaged in place loaded as it was before. Reproduced: with the old loader, a vendored file cut to 1,000 lines still loaded 200,019 ranks |
| `test_vocabulary_read_from_the_file`, with a mutation case restoring `load_tiktoken_bpe` | It fails with the fix reverted on every Python, not only 3.8: it makes `tiktoken.load.read_file` raise as 0.7.0's does, damages a loaded vocabulary in place, and checks that the parsed ranks equal tiktoken's own |
| `read_ranks` refuses a repeated token or rank, with a mutation case removing the check | Found by the first Windows CI run: the checkout turns the vendored file to CRLF, so `test_extractor_version_tracks_source`'s `blob[:-40]` ended inside a rank (`<token> 19`) instead of inside a token. tiktoken panicked on the repeated rank with a `pyo3_runtime.PanicException`, which is a `BaseException`, so `_extractor_version`'s "unusable tokenizer: never reuse" `except Exception` let it through and the run crashed. The new check in `test_vocabulary_read_from_the_file` cuts a rank on every platform |
| `test_share.py` compares the page sent with the file's bytes | Also Windows only: the report writes the page in text mode, so the file holds CRLF and `share.py` sends it as it is, while the test read it back in text mode as LF. The page sent was the page on disk all along |
| `scripts/ref_bpe.py` reads the ranks through `read_ranks` | It promised to work offline "like everything else", and on 3.8 it could not |
| `.github/workflows/ci.yml`: the four test scripts and `test_page.js` on Ubuntu and Windows, Python 3.8 and 3.14, on every pull request and push to `main`; `fetch_vocab.py --verify` and a manifest check beside them | The suites ran only when someone ran them, and never on the 3.8 the README promises, which is how the loader defect above shipped. The job stops unless `tiktoken` imports, because `test_mutations.py` counts a target's `ImportError` as a caught mutation: without `tiktoken` it reported 29/29, 19 of them caught by nothing but the missing import. `--verify` must print its parity line, since it exits 0 when it cannot download the reference. The manifest check holds `share.py`'s client version to `plugin.json`'s, which drifted once (Rev 17) |

**Rev 18** — response time, turn time and tool time (§5.8). Plugin 1.5.0.

| Change | Cause |
| --- | --- |
| The worker keeps, per usage record, the request's start (`req_ts`) and turn; per file, each turn's opening stamp and each tool call's time. Both passes read them | Requested: the report measured tokens and nothing about time. The ledger-only pass skipped `response_item` records entirely, and timing needs their side and stamps, not their content |
| A request's start is frozen at its response's first output item (`worker._request_start`) | A tool finishing mid-stream writes its output before the response's usage record; the latest input at the usage record shortened that response by the tool's run. Mutation case added |
| A response starts no earlier than the previous charged response's end (`latency._response_start`) | A compaction call has no input of its own; timing it from the last input charged it with the previous response too. Mutation case added |
| Review of rev 18 before merge, every finding reproduced first: an interrupted response's frozen start is cleared by `turn_context` and `turn_aborted` (`worker._ends_response`); a legacy `compacted` stamp floors the next request (`worker._request_anchor`); an uncharged legacy repeat or snapshot no longer ends the response in flight; only known inputs move a start (`worker._timing_side`); charged replay rows are flagged by the ledger and not timed (`latency._is_replay`); a token column needs ten distinct values, and a singular design drops a column; unfitted groups report no estimate rather than zero; the headline share says what it covers; `latency_no_end` is its own counter; a non-string tool name or call id is survived; `secs` rounds into the next unit; the token-share skill and dry run list the panel | Each was a reachable miscount or crash: 305 s for a 5 s response after an interruption, 62 s for a 2 s response after a legacy compaction, six 6 ms replay "responses" with the exclusion off, a fit set by one response, `TypeError` in `--metrics-only` on a list-typed tool name. The replay fixture stamped every replayed record at one instant, so its millisecond check was vacuous; it now stamps them a millisecond apart. Six mutation cases added |
| A row with no request stamp of its own is not timed (`latency._timed_start`) | Found in review: falling back to the previous response's end timed the whole gap between two responses, and every row of an archived payload extracted before `req_ts` existed would have been one. Mutation case added |
| Tool calls before a file's first timed response are left out (`latency._own_work`) | A fork child's replayed tool calls carry the child's creation time and would pass for millisecond calls. Mutation case added |
| The split into work and time above the pace is an estimate, starred on the page and worded so in the terminal line | The rollout records no server timing, so queue time cannot be measured from it. The excess also holds slow generation and ordinary variation |
| The fit works on whole columns | A row-at-a-time loop took 10.8 s over a 136,500-response synthetic ledger; columns take 1.3 s with the same estimates |
| The hour-of-day chart is CSS columns, not SVG | A fixed viewBox scaled its labels to about 4 px at phone width |
| The public page leaves out the tool table | Tool names, MCP servers among them, come from the machine, like the session id and `cwd` already left out |

**Rev 17** — input is counted with tiktoken (§5.7). Also plugin 1.4.0.

| Change | Cause |
| --- | --- |
| The page's input (tile, longest session, daily chart, input-by-model pie, cumulative curve) is tiktoken's count of each response's reconstructed prompt | Requested. Every number on the page came from Codex's usage records, and tiktoken fed only the content pie |
| Output, cached, uncached, the cache hit and the weekly limit stay Codex's; the output and cache tiles and the terminal line say so | Requested, and forced by the data. Reasoning tokens are encrypted and caching is decided on the server, so neither can be counted from the log. A cache hit over the tiktoken input would divide one measurement by another, and cached input can exceed it |
| The tiktoken counts are new JSON fields; the old ones keep Codex's figures | `share.py` and `test_share.py` read `daily[].input` and each window's `tokens`, and the leaderboard has to keep agreeing with the report's recorded figures |
| A response with no reconstruction keeps Codex's figure, and is counted | Counting it as zero would understate the input silently |
| `analyze.tiktoken_inputs`, with a mutation case that empties it | So that reverting the switch is testable in-process. `test_input_counted_with_tiktoken` catches it |
| `test_render` injects its hostile model name into both daily splits | The chart now draws `tiktoken_models` when the run tokenized, so a name only in `models` never reached the legend the test checks |
| Review of both revisions, before merge: `deps.importable` catches any exception; installer output is decoded as UTF-8 with replacement; `_forget` drops everything the install provides (`regex` too); the free-threaded key; a broken install is moved aside before downloading; `report.out_dir` picks from `deps.roots`; a zero-token prompt falls back; share's client version is 1.4.0 | Each was a reachable crash or miscount. A foreign-ABI tiktoken raising `OSError` crashed `--doctor` and the run; undecodable pip output raised out of a function promising not to; a stale system `regex` could be mixed into the private install; 3.13 and 3.13t on Windows replaced each other's install; a broken install held open on Windows re-downloaded every run; the two copies of the directory list could drift apart. Each fix has a test, and each test fails with its fix reverted |

**Rev 16** — `tiktoken` installs itself (§4.2). Plugin 1.4.0.

| Change | Cause |
| --- | --- |
| `tokencounter/deps.py`: the first run that needs `tiktoken` installs it with `pip install --target` into `<CODEX_HOME>/token-counter/lib/<interpreter>-<platform>/` | Reported: `tiktoken` was not installed with the plugin. `codex plugin add` copies files and has no dependency step, so every fresh install ran with an empty content panel. The README called `tiktoken` optional, and in practice that meant it was never installed |
| The tokenizer setup moved out of `main` into `report.tokenizer_status`, with a mutation case restoring the old "load or give up" behaviour | So that reverting the fix is testable in-process. `test_report_installs_missing_tiktoken` catches it |
| `--no-install` / `TOKEN_COUNTER_NO_INSTALL=1`; `share.py` always passes `--no-install` | The install is a network call, and the report had promised none. A share must publish the page its dry run showed |
| The page note for a missing `tiktoken` is a whole sentence | It used the exception's first line, which ended in "Install it with:" and stopped there |
| `--doctor` reports the `tiktoken` version and where it was imported from, or where the next run will install it | "Not installed", "installed privately" and "installed by the user" produce the same report and need different fixes |
| `test_tiktoken_installs_itself` runs the real pip, offline, against a hand-built wheel, in `-I -S` interpreters | It must pass on a machine that already has `tiktoken`, and must never reach PyPI. It checks install, reuse without pip, replacing a broken install, failing cleanly, and the opt-out |

**Rev 15** — after round 10 of review, the third against the code. Five defects confirmed by
repro before being fixed — one of them the Rev-7 baseline bug back in a third of the corpus,
and one a test that could not fail.

| Change | Cause |
| --- | --- |
| `--include-archived` adds archived entries to the window (`report._include_archived`) | **Blocker:** the payload joined `results` but never `window`, so the ledger charged it out of scope and the report filter dropped it. The run printed "1 archived sessions included" over a report that included none. Extracted into a named function so its revert can be tested |
| `--rebuild` empties the index through SQLite (`Index.clear`), deleting the file only when it cannot be opened | **Blocker:** deleting was the whole mechanism, and the fallback comment promised the schema check would re-parse — it clears only on a version change. On Windows with a second run holding `index.db` open, `--rebuild` reported the failed delete and then served every cached payload |
| Stable-prefix baseline moves only on the charged stream (`worker._assemble`) | **Blocker:** in a both-stream file the legacy mirror lands right after each explicit record, once that record's output has been folded in, and re-captured the baseline there. The Rev-7 request+output overstatement was back for 602 of 1,952 files while the explicit-only test stayed green: `stable=1002` against a 201-token previous request |
| `test_stable_prefix_baseline` writes the reply *before* the usage record, and runs a both-stream variant | **Blocker in the test, not the code:** with the reply after the usage record there is no trailing output run, so the fold it tests never happened and the test passed with the Rev-7 fix reverted. Verified by reverting at the source: the old fixture still passed, the new one fails |
| `analyze._day_span` localises both midnights from the calendar | `astimezone()` attaches a fixed offset, and a day added to that is +86,400 s: the bar ended at 01:00 on the spring-forward day and 23:00 on the fall-back day, while the docstring and this document (Rev 13) said the opposite |
| Five mutations added; `_worker_with` re-executes `worker.py` from source | The two baseline defects are single lines inside `_assemble` that no attribute patch reaches. 12 → 17 cases, all caught |
| `collect` runs without the index when the extractor fingerprint is 0 | `_extractor_version` returns 0 for "cannot fingerprint; never reuse", and `collect` used 0 as an ordinary key — a row written under it was served back under it |
| `rate_limit_windows` carries each boundary with its window index | A live window with no readable timestamp has no `reset_at`; omitting it from a bare list of boundaries shifted every later window's tokens one slot back |
| `--session` refuses a prefix matching several sessions | It focused on whichever came out of a set first and labelled the report with one session |
| `images._jpeg` steps over `FF` fill bytes | `FF FF` before a marker was read as marker `FF` with a garbage length, skipping past the frame header |
| Stale docstrings: `--fast` (every core × 1 thread, not 8×4), `metrics_only` (no prefilter since Rev 9) | Documentation drift |

**Rev 14** — the cumulative curve only climbs.

| Change | Cause |
| --- | --- |
| Charged responses are ordered by time before the running total is accumulated (`analyze._in_time_order`, §5.6) | Reported: the blue curve was not monotonic. Responses reach `rate_limit_windows` in *file* order — files are read one at a time and sessions overlap, so a file read later routinely carries older responses — and the total was accumulated in that order, then sorted by timestamp afterwards by `_bucket_last`. Sorting the points could not undo it: the damage was in the values, not their order. The curve stepped backwards 104 times over the live corpus, once by 441M tokens, and 4 of 16 windows ended below their own measured total. §5.6 had claimed "in time order" since Rev 11; the code never did it |
| `test_cumulative_curve_is_monotonic`, plus a mutation case reverting the ordering | The defect was invisible to every existing assertion: the per-window *totals* were always right, and only the shape of the curve between the endpoints was wrong. The test's corpus makes path order the reverse of time order, so any regression shows as a curve that falls |

**Rev 13** — the three charts became one instrument.

| Change | Cause |
| --- | --- |
| The limit chart and the daily chart share a domain, a geometry and their ticks (§7.1) | Requested. They were drawn over different ranges at different margins: the daily chart gave each day an equal slice of the width, so a day with no sessions took no space and the two charts disagreed about where a date was |
| Daily bars carry a real span (`start`/`end` from `analyze`) and are drawn in unit x | A time axis needs the span of a bucket, and re-deriving a *local* day's span in the renderer would repeat the DST conversion that `_local_day` already does |
| Zoom and drag, shared across the charts, horizontal only | Requested. A zoom that rescaled the value axis would make two views of one chart incomparable, which is the opposite of what a shared axis is for |
| `cat_series`: categories bucketed by the hour their rollout file opened | Requested — the composition pie follows the viewport. Content is deduplicated per file, so the file is the finest unit it can be placed on; anything finer would be invented. Costs 36% of the page's bytes |
| The page sizes its own viewBox to the panel, and picks ticks by label pitch | Both charts were legible at 1,180 px and unreadable on a phone: 11 px axis text rendered at four, and the tick step jumped from two days to fourteen, leaving one label on screen |
| `scripts/test_page.js` | The interaction is ~300 lines of JS that no Python test can reach. It runs the page's own script against a stub DOM and drives it the way a reader would |

**Rev 12** — the page became a dashboard, and the environment became something the tool
reports on rather than assumes.

| Change | Cause |
| --- | --- |
| Page cut to five headline numbers and three charts; `--json` keeps the whole model | Requested. The renderer lost ~24 KB of row builders, the payload lost the deep-dive data, and a generated page went from 492 KB to 140 KB |
| `analyze` always emits the disclosure counters, at zero | They used to be forced visible by the renderer's counter table. "No damage found" and "damage not looked for" must stay distinguishable once no table exists |
| Daily chart stacked by model; category chart drawn as a pie | Requested |
| A window's percentage curve clipped where its successor opens | The server keeps reporting a replaced window to some sessions — 1.6 days into the next week here — which drew two limit curves at once (§5.6) |
| `tiktoken` and the vocabulary made a **soft** dependency | They feed one panel of four. A plugin copy shipped without the vendored blob used to fail outright; it now degrades to the usage ledger and the page says which panel is missing and why |
| Process pool failure falls back to one process | A sandbox with no fork or spawn, or a memory cap that kills a child, took the whole run down |
| Index failure, unwritable `CODEX_HOME` and unwritable output all degrade | Each was an unhandled exception on a machine that differs from the author's |
| `--doctor` | Every one of those setups produces a different empty or partial report, and the difference is invisible from the report |
| A file that could not be `stat`'ed is parsed instead of skipped | It was dropped from the corpus silently |
| `python3` vs `python` documented | `python3` on a default Windows install is a Store stub that opens a web page |

**Rev 11** — account identity and weekly rate-limit windows. Two new inputs, one of which
breaks a stated invariant; both were measured against the live corpus before being designed
against.

| Change | Cause |
| --- | --- |
| `tokencounter/account.py` reads `~/.codex/auth.json` | A rollout records **no account**, so a shared or copied report was unattributable — and looked identical to a correct one. Narrowed to an allow-list over the id_token's claims segment; access and refresh tokens are never read, asserted by test |
| Namespaced claims read in **both** shapes | The live tokens nest them under `https://api.openai.com/auth`; the flattened spelling is the usual convention. Reading one shape lost `plan` and `account_id` **silently**, because the plain `email`/`name` claims still populated and the record reported itself available |
| `--sessions-root` moves the `auth.json` lookup with it | Otherwise a report over a copied corpus — or the test suite — is stamped with the live account's email address |
| Weekly window identified by `window_minutes`, never by slot name | The weekly limit is `secondary` behind a 5-hour `primary` in older builds and `primary` alone in newer ones: **5,684 observations** would have been charted as a 5-hour window |
| `resets_at` quotes clustered into windows | An idle window re-quotes its reset as now+7d on every call. **1,355 distinct values** resolve to **83 consumed windows and 1,272 idle slides**; keying on the integer shattered one untouched week into hundreds |
| Window boundary taken from **where the reported percentage drops** | Resets are routinely early: one file reports 99% and, a minute later, 0% on a new window resetting seven days from *that* instant. Seven-day arithmetic put nearly every boundary in the wrong place |
| Rate limits read **before** the usage checks in the `token_count` branch | A `token_count` with `info: null` carries no usage but still carries a window; reading limits on the usage path alone lost window boundaries |
| The drawn percentage curve clipped at the successor's boundary | A replaced window is still reported to some sessions &mdash; 1.6 days into the next week here &mdash; so the chart drew two live limit curves over the same days. Clipped readings are counted, not dropped, and still set the peak |
| Overlapping windows and drop-less boundaries published as counters | Neither is decidable from the logs; smoothing them away would invent certainty |
| Three mutations added to `scripts/test_mutations.py` | The project's rule: no fix counts until its revert turns a named test red |

**Rev 10** — after round 9. Three of the four round-8 fixes were confirmed live; one was
still incomplete; and the rewritten test for the fourth still would not have caught its own
regression.

| Change | Cause |
| --- | --- |
| Cache key is a **behavioural fingerprint** of the tokenizer | **Blocker:** hashing `tiktoken.__version__` and its module path does not move for a same-version wheel replacement, a swapped native `_tiktoken`, or an editable install — all of which change token counts. The key now encodes a fixed probe string and hashes the ids, which measures the only thing that matters: what this tokenizer returns |
| `test_damage_outside_window_reaches_the_report` drives `report.main()` | **Blocker in the test, not the code:** it called `report.damage_outside` directly, so moving the production call back below the window filter would not have failed it — the exact bug it was named for |
| `scripts/test_mutations.py` added | **The pattern, not an instance.** A passing test proves nothing unless it fails without its fix. Six historical defects are reverted in-process and the matching test must go red; two of the six were *not* caught on the first run, so two tests were decoration |
| `worker._charged_stream()` extracted | One of those two: the stream-preference rule was inline and could not be reverted, so nothing tested it |
| `damage_outside_window` no longer double-counts | `unparseable_usage_records` is a subset of `unparseable_records`, and adding both scored one damaged line as two |
| Counter renamed to "out-of-window files the ledger used" | It aggregates every out-of-window file, not only ancestors; the old label overstated its precision |
| `non_object_records` and `cross_file_response_id` render | §8 claims every counter is surfaced; these two were not |
| The data-quality note is mode-aware | In charged mode it still told the reader to re-run with `--no-replay-exclusion`, which was the flag already in use |

**Rev 9** — after round 8. One of the three round-7 fixes **did not work**, and the test
named for it did not test it.

| Change | Cause |
| --- | --- |
| Out-of-window damage aggregated **before** the window filter, in `report.damage_outside()` | **Blocker:** the round-7 fix ran after `results` was reduced to the window, so its `p not in window` guard could never be true. The fix was a silent no-op for a full revision. Extracted into a named function so it can be tested directly |
| The regression test now drives `report.damage_outside` + `analyze` + `render` | **Blocker:** the test named `test_damage_outside_window_is_reported` called `worker.metrics_only` and checked the parent's own counters. It passed throughout, while the reporting path it was named for did nothing |
| Cache key includes the installed `tiktoken` version and module path | **Blocker:** a tokenizer upgrade changes counts without touching a local file, and the index served the old ones. Reproduced by patching `encode_ordinary`: direct extraction gave 999 tokens, the cache served 2 |
| Candidate byte prefilter **removed**; every complete line is parsed | **Blocker:** a complete, object-shaped record with damaged `type` strings never matched the filter and never reached the parser, so a corrupt ancestor was invisible to the ledger. Measured cost of parsing everything: **1.0s** across 7.8 GB (3.6s → 4.6s), and *faster* than filtering plus validating. One second is not worth a class of silent loss |
| `inherited_charged` replaces `inherited` when the exclusion is off | **Blocker:** the report said "Ancestor-replay records dropped: 3" about three records it had just charged. False labelling, not disclosed approximation |
| Report states its fork-replay mode in a standing note | The mode was invisible in HTML: `replay_exclusion_applied = 0` was suppressed by the `if q.get(k)` filter that hides zero counters |
| Mode and damage counters render at zero | "No damage found" and "damage not looked for" are different statements, and a suppressed zero cannot tell them apart |
| `verify_install.py` fails on extra files | The round-7 edit did not apply and was not re-checked; it still printed extras and exited 0 |
| "Billed input" removed from §7, `bench.py` and `SKILL.md` | The relabel covered the report and stdout but not the document, the benchmark or the skill instructions |

**Rev 8** — after round 7. The round-6 fixes were all confirmed to reproduce; three narrower
blockers remained, and the verdict was **No** again.

| Change | Cause |
| --- | --- |
| Cache key covers the **vocabulary contents** | **Blocker:** `--vocab` selects the tokenizer that produced every cached token count, and the key ignored it, so a run with one vocabulary happily reused payloads counted with another. Keyed on contents rather than path, so the same blob at a new location does not force a pointless rebuild |
| Replay exclusion labelled as inference, with `--no-replay-exclusion` | **Blocker:** a matched run was excluded silently, presenting a heuristic as identity. The legacy stream has no response id, so a replayed run is *inferred*. Both bounds are now reachable: 136,179 responses / 16.698B excluded, **139,219 / 17.005B** charged |
| Damage counters aggregated over every file the ledger touched | **Blocker:** a corrupt record in an out-of-window ancestor changes what a windowed report charges, and its counter was filtered out of that report — the numbers moved with no visible reason |
| `suspect_records` on the metrics path | A line whose own `type` string is damaged never matches the candidate filter, so it was invisible. A cheap structural check catches it without parsing |
| `_day` resolves to the **minute** | Truncating to the hour before conversion put a half-hour offset such as `-04:30` a day early |
| Image digests hash the whole payload | A 512-character prefix still collides for two images differing only in the middle |
| `verify_install.py` fails on extra installed files | It reported them and then exited 0 |
| Every input figure relabelled **recorded input** | The page warned that recorded is not billed and then said "billed input" nine times, including on stdout |
| Actionable failures print their message, not a pool traceback | A corrupt vocabulary surfaced as a re-raised worker exception |

Still open, and stated rather than closed: resend cost exceeds the canonical figure by 0.206%
(cross-file replay is not a per-file judgement); clock skew can invert a parent/child
ordering; a concurrent append can yield one stale cache hit.

**Rev 7** — after round 6 of review, the first run against the code rather than the document.
Verdict: *"not sound enough to rely on."* It was right.

| Change | Cause |
| --- | --- |
| §3.1 windows narrow the report, not the ledger | **Blocker:** `--since` filtered files *before* charging, hiding the ancestors a fork child replays. One session with `--since 2026-07-26` was overcharged by **246 responses / 30,536,496 input tokens** |
| Cache key derived from a hash of the extraction sources | **Blocker:** `EXTRACTOR_VERSION` stayed at 4 through four extraction changes, so the index served pre-change payloads — 18.43B resend tokens against a true 14.41B. A constant somebody must remember to bump is not a cache key |
| §2.5 discriminator 3 restricted to the declared ancestor chain | **Blocker:** matching any earlier file in the session deletes real responses when two unrelated siblings share a run of states. All 39 real matches are declared-parent matches, so the restriction costs nothing |
| `legacy_ancestry` defaulted off | **Blocker:** an explicit parent's legacy sidecar could hold records its explicit stream does not, dropping genuine child responses. It changed nothing measurable, so the risk bought nothing |
| Unparseable complete records counted | **Blocker:** a corrupt usage line vanished with no error and no counter, taking its response with it |
| `scripts/verify_install.py` added | **Blocker:** the installed plugin was **not** the reviewed code — 6 stale files — while this document claimed it was verified. Now checkable, and checked |
| Resend total printed with its overcount | The report presented 14.41B bare when 14.38B is the canonical figure; the 0.206% gap is now beside it |
| §5.2 stable prefix measured against the previous *request* | The baseline included that response's own output, overstating the shared prefix by a whole assistant turn |
| Compaction segments seeded distinctly | Reseeding the chain to a constant let identical content either side of a compaction hash identically |
| `_seg_base` replaces a fallback to index 0 | After an empty compaction segment, responses were attributed to segment 0, charging pre-compaction items for post-compaction prompts |
| Image digests mix in a payload prefix | Dimensions plus length collide for different images of the same size |
| Any ISO offset honoured, not only `Z` | `-04:00` was treated as wall-clock and bucketed a day early |
| §3.4 benchmark re-measured | **Not reproducible:** the revision-6 low-parallelism rows were ~40% too fast. The conclusion held; the table did not |
| §5.5 "worst tool output" corrected | The item named was `tool_call_input`. The real worst `tool_output` is 22,102 × 153 |
| §8 legacy-stream file count corrected to 1,341 | It counted the 7 no-usage files |
| `scripts/test_ledger.py` 10 → 13, `test_pipeline.py` 46 → 55 | Every blocker above is now a regression test, including the reviewer's own counterexamples |

Not fixed, and stated rather than hidden: resend cost still exceeds the canonical figure by
0.206% (cross-file replay is not a per-file judgement); clock skew can invert a parent/child
ordering; a concurrent append can yield one stale cache hit through the stat/hash/read race.

**Rev 6** — self-review during implementation, which found four things the document had
wrong:

| Change | Cause |
| --- | --- |
| §6 layout rewritten to a `tokencounter/` package | **Blocker:** the documented `scripts/tokenize.py` would have shadowed the **standard library's** `tokenize` module for the whole process, since Python prepends the script's directory to `sys.path` |
| §3.4 recommendation reversed to 16×1; `--fast` buys cores, not threads | **Major:** measured over the real pipeline rather than extraction-plus-BPE, 16×1 beats 8×4 on *both* wall time and CPU (16.8s/229s vs 19.4s/254s). The thread axis is not a trade-off; it is worse |
| §5.2 first-response cache corrected 558 → 1,851 (95% of files) | **Major:** 558 was `verify_schema.py`'s count *within the 602 explicit-stream files* read as a corpus-wide count. The underlying rate (94.5%) was right all along; the denominator was not |
| §2.3 records that `cache_write_input_tokens` is uniformly zero | **Major:** documented as a field to preserve and use; it is present in 160,048 records and is 0 in every one, so it carries no information |
| §3.2 archiving keyed on file existence, not scan coverage | **Bug found in testing:** a `--since` run archived 1,801 of 1,947 index entries because it had not scanned them |
| §3.2 tailing dropped in favour of whole-file re-parse | Re-parsing a changed 249 MB file costs 0.5s; persisting parser state mid-file is a large correctness surface for no measurable gain |
| §5.5 unique content 355.7M → 372.5M, amplification ~47x → 44.8x | The earlier extractor omitted the environment block, `agent_message` and image tokens, so the denominator was too small |
| §2.1 extended with observed payload shapes | `function_call_output.output` is a bare **string** in 603 of 644 sampled cases; a list-only reader drops most tool output |
| §5.3 residual measured: 86.2% coverage, 2.297B unexplained | Previously undefined; now published with its largest known contributor named |
| §3.4, §9 full-pipeline timings measured | Previously listed as a known limit; now 31.5s cold, 21.9s with `--fast`, 4.0s warm |
| Resend costing restricted to the charged stream | **Blocker found by invariant check:** resend cost must equal summed reconstructed prompts; it did not. Both-stream files counted every prompt twice, inflating resend cost ~2x for 602 of 1,952 files (18.43B vs 14.39B) |
| §3.3 candidate byte filter widened | **Bug:** `b'"type":"token_usage_record"'` matches only minified JSON. One space after a colon and `--metrics-only` would report zero usage for every file. Classification is authoritative, so a broad hint cannot miscount |
| Daily buckets converted to local dates | **Bug:** record timestamps are UTC but file paths — and `--since`/`--until` — use local dates, so a 00:24 local session landed in the previous day east of Greenwich |
| Index archiving keyed on file existence | **Bug:** `--since 2026-09-18` archived 1,801 of 1,947 entries simply because it had not scanned them |
| Embedded JSON escaped for `<script>`; previews escaped in JS | Item previews are verbatim rollout content, so a tool output containing `</script>` would close the block and spill data into the page as markup |
| Extraction errors are contained per file | One malformed payload in a pool worker took the whole 30-second run with it |
| Truncated or unparseable vendored vocab is rejected | A short blob would otherwise build an `Encoding` that silently produced wrong counts |
| `scripts/test_pipeline.py` added (46 cases at rev 6) | Only the ledger had regression tests; classification, images, the tokenizer's offline path, failure modes and the renderer had none |

**Rev 5** — after round 4 of review:

| Change | Cause |
| --- | --- |
| §2.5 requires symmetric normalization of both match sides | **Blocker:** ancestor sequences dropped context snapshots while child sequences kept them, so matching halted at the first compaction — 632 inherited records across 11 files were charged twice |
| Match key widened to complete state (usage + cumulative) | Same defect class |
| Ancestor history stored independent of charging | A grandchild may replay a parent's own inherited run |
| §5.5 recomputed to 16.645B | Consequence: replay exclusions rise 2,408 → 3,040 |

**Rev 4** — after round 3 of review:

| Change | Cause |
| --- | --- |
| §2.5 dedup replaced with three ordered discriminators; `scripts/test_ledger.py` added | **Blocker:** session-wide usage-tuple equality is not response identity — it fails both a same-thread repeat with advancing cumulative and two sibling threads with identical usage |
| §5.5 recomputed to 16.693B | Consequence: the coarse dedup over-dropped ~618 records / 49M tokens |
| §8 adds an **ambiguous** counter | Unresolvable provenance now reaches the documented state instead of being silently charged |
| `scripts/encoding.py` added with `O200K_PAT` defined | §4's recipe referenced a pattern the repo never defined |
| §4 confirmed | Independently verified: fresh process, networking blocked, 0 network attempts, 1,009 samples token-identical |

**Rev 3** — after round 2 of review:

| Change | Cause |
| --- | --- |
| §5.5 recomputed to 16.644B; `scripts/ledger.py` rewritten | **Blocker:** documented exclusions were never implemented; 3,573 repeated/inherited records (369M tokens) were still counted |
| §4 packaging rewritten to explicit `Encoding` construction | **Major:** `TIKTOKEN_CACHE_DIR` requires a `sha1(url)` filename; the documented `o200k_base.tiktoken` layout would have failed offline |
| §2.5 fork rule replaced with parent-history matching | **Major:** 3 forks carry inherited usage with no copied `session_meta` |
| §2.4 corrected: 818 compaction / 16 aborted / 20 repeat | **Major:** the 854 violations were mischaracterized as aborted turns; 599 means "in legacy-selecting files" |
| §3.4 scoped to extraction + tokenization | **Major:** benchmark was presented as the full pipeline |
| §2.2 corrected: streams overlap, not mirror | 208 of 586 both-stream files differ; explicit adds 258 responses, legacy adds 444 non-responses |
| §3.3 exact candidate filter specified | **Minor:** bare `token_usage_record` still matches 254 compaction copies |
| §5.1 reasoning summaries added | **Minor / under-claimed:** 1,525 plaintext `summary_text` elements were recoverable |
| §5.4 image formats verified | 1,100 PNG + 389 JPEG, all dimensions from ≤4 KiB |
| §5.5, §9 coverage caveat added | Legacy compaction intervals have no usage evidence |

**Rev 2** — after adversarial review against the live corpus:

| Change | Cause |
| --- | --- |
| §2.2 canonical ledger added | **Blocker:** overlapping usage streams were summed; 21.582B vs true 17.007B |
| §5.2 demoted to heuristic | **Blocker:** cache crosses session boundaries (558 files); completion order ≠ request order; no cache telemetry |
| §2.3 dispatch by shape | Version cutoff was wrong (0.153.4, not 0.155.1); `cache_write_input_tokens` present in 123,502 legacy records |
| §2.4 claims scoped by record class | 854 arithmetic violations, 737 repeats, 7 counter decreases in legacy stream |
| §2.5 fork/root-turn handling | 6,987 records lack `root_turn_id`; forks inherit cumulative counters |
| §3.2 validation before tailing | Append-only was assumed, not verified; archive moves files |
| §3.4 table rebuilt | Process and thread parallelism were conflated; rayon attribution was wrong |
| §4.1, §5.3 residual demoted | Residual cannot validate encoding; no per-model schema constant exists |
| §5.5 metrics recomputed (17.007B) | Consequence of the ledger fix |
| §8 added | Data-quality counters were previously invisible |

---

## 11. Status

Built, installed and verified as `token-counter@jack-beanstalk-2022` on Codex CLI
0.155.1.

| Check | Result |
| --- | --- |
| `scripts/test_ledger.py` | **13/13** response-identity regressions, including both round-3 counterexamples, the round-4 compaction case, cross-file `response_id` replay and the round-6 sibling counterexample |
| `scripts/test_mutations.py` | **62/62** historical defects reverted, each caught by the test named for it |
| `scripts/test_pipeline.py` | **354/354** across tokenizer (the vocabulary parsed from its own file, on Python 3.8 too), installing `tiktoken` on first use, input counted with tiktoken, classification, images, attribution, prompt reconstruction, windowed ledger scope, cache-key derivation, the index end to end (archiving, `--rebuild` against a held file), damage counting, rate-limit windows, cumulative-curve monotonicity, day spans across clock changes, account identity, failure modes, output escaping, the renderer and its three styles (Nocturne's validated palette pinned), the shared time axis the three charts are drawn on, response, turn and tool time with the pace estimate (§5.8), rate-limit events per day with a fork child's replayed copies left out (§5.6), and the API value: the price table loaded offline and refused whole when malformed, the per-response arithmetic, tier and search extraction on both passes, the first turn's inferred tier, a fork child's replayed searches, aborted turns, the price-page parser on a saved copy, the tile and `--prices` (§5.9) |
| `node scripts/test_page.js` | **65/65** on the page's own embedded script: shared ticks across all three time charts, the rate-limit event bars on their own axis, shared viewport, x-only zoom, drag distance, clamping, the pie recomposing with the range, and the Nocturne scene's solids and stage built from the same marks |
| `scripts/fetch_vocab.py --verify` | sha256 `446a9538...`, 200,019 ranks, token-identical to stock `o200k_base` |
| Offline tokenizer | builds and encodes with `socket.socket` hard-blocked in a fresh process |
| `scripts/diag_fork.py` | the known fork pair matches for exactly **37 records at parent index 95** — an independent witness for the §2.5 rule |
| `scripts/ref_bpe.py` | pure-Python BPE reproduces the vendored tokenizer **byte for byte** |
| Self-containment | the rendered report contains no `http://` or `https://` reference at all |
| `scripts/verify_install.py` | **17 files identical** — the installed plugin is this code, not an earlier copy of it. It compares the copy cached under the marketplace the manifest names: an earlier version scanned every marketplace and took the last alphabetically, so the cache orphaned by the `@local-dev` rename became the copy verified |
| Installed run | executes from `~/.codex/plugins/cache/jack-beanstalk-2022/token-counter/1.0.0/` |

Not built, and deliberately: incremental byte-offset tailing (§3.2), a reconstructed bill
(the API value is list price for recorded usage, §5.9), fetching prices at run time, and any
conversion of tokens into rate-limit consumption (§1, non-goals).
