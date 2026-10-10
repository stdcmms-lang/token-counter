# token-counter

For Claude Code, see the [Claude Code edition](#claude-code). The existing sections below describe the Codex plugin.

A Codex CLI plugin that independently tokenizes your session history, measures cached against
uncached input, names the account it covers, and renders a local HTML dashboard on request.

Every figure comes from `~/.codex/sessions/**/rollout-*.jsonl`. One other file is read, and
only to put a name on the report: `~/.codex/auth.json`, for the non-secret identity claims in
its id_token — access and refresh tokens are never parsed, and `--no-account` skips the file
entirely. No daemon, no interception, and no conversion of tokens into rate-limit
consumption. It does put a dollar figure on the usage: what it would cost at OpenAI's API
list prices, from a price table that ships with the plugin (see [API value](#api-value)).
The report makes one network call, once: if `tiktoken` is missing, the first run installs it
from PyPI (see [Install](#install)). Nothing of yours is sent.

The one exception is opt-in and separate: the `token-share` skill posts daily token counts to
the public leaderboard at [tokenusage.dev](https://tokenusage.dev), with your report page at a
link you can send, and only when you ask it to and confirm with `--yes`. See [Sharing](#sharing).

## Install

```
codex plugin marketplace add stdcmms-lang/token-counter
codex plugin add token-counter@stdcmms-lang
```

The `o200k_base` vocabulary ships in this repository, so there is nothing to download for it.
(`scripts/fetch_vocab.py` re-vendors it, and `--verify` checks it against stock `o200k_base`;
neither is needed to install.)

`tiktoken` is the only third-party package, and you do not need to install it yourself.
`codex plugin add` only copies files, so the first report that needs `tiktoken` installs it,
once, with `pip install --target` into a directory of the plugin's own:

```
~/.codex/token-counter/lib/<interpreter>-<platform>/     # delete it to undo
```

Your Python environment is not touched: nothing goes into site-packages or `--user`. A
`tiktoken` you already have is used as is. The install needs network access. Inside the Codex
sandbox the network is usually off, so approve network access for that first run. If the
install fails, the report still runs with an empty content breakdown (every usage figure is
exact without it), and the next run tries again. `--no-install` or
`TOKEN_COUNTER_NO_INSTALL=1` turns the install off, and `python -m pip install tiktoken` works
as it always has.

Then ask Codex for a token report, or run it directly:

```
cd plugins/token-counter/skills/token-report
python scripts/report.py
```

To install from a local checkout instead — developing, or reading the code before trusting
it — point the marketplace at the clone:

```
git clone https://github.com/stdcmms-lang/token-counter
codex plugin marketplace add ./token-counter
codex plugin add token-counter@stdcmms-lang
```

### Desktop app

Codex in the ChatGPT desktop app installs from the same marketplace, but has no documented way
to add one, so the marketplace has to be made known to it first. Either register it with the
CLI:

```
codex plugin marketplace add stdcmms-lang/token-counter
```

or clone this repository and open it as a project: the app picks up
`.agents/plugins/marketplace.json` from a project's root without being told. Then restart the
app, open **Plugins**, choose the **stdcmms-lang** source and install **Token Counter**.
Once the marketplace is known, this link opens the same install flow:

```
codex://plugins/install/token-counter?marketplace=stdcmms-lang
```

Start a new chat afterwards; skills load only in chats begun after the install. The skills run
`python3` (`python` on Windows), which must be 3.8 or newer and on the app's `PATH`.

## Sharing

Ask Codex to "share my token usage to tokenusage.dev", or run it directly:

```
cd plugins/token-counter/skills/token-share
python scripts/share.py                        # dry run: prints what would be sent, sends nothing
python scripts/share.py --handle NAME --yes    # first share: claims NAME on the leaderboard
python scripts/share.py --yes                  # every later share
python scripts/share.py --yes --style matisse  # the shared page opens in Matisse
python scripts/share.py --delete-report --yes  # take the report page down, keep the numbers
python scripts/share.py --delete --yes         # remove everything you shared
```

The last line a share prints is your report, `https://tokenusage.dev/r/<handle>`: the same
page as your local report, in the same three styles, for anyone you send the link to. It is
rendered with token-report's `--public`, which leaves out the top session's id and directory
name; the dry run writes it to `~/.codex/token-counter/report-shared.html` so you can open
exactly what will be published. Its response-time chart goes too: the median and p90
response time per day, and the rate-limit events per day behind them. `--no-report` shares
the numbers without it.

What is sent is Codex's own recorded counts, from the same ledger the report reads, so the
leaderboard agrees day for day with the report's recorded figures (in `--json` and on its
terminal line). The report page counts input with tiktoken instead (see
[Where the numbers come from](#where-the-numbers-come-from)), so its input reads lower than
the leaderboard's. Sent: per-day responses, recorded input, cached input, output
and reasoning tokens, plus each month's top sessions by active time and by tokens (a one-way
hash of the session id, start and end times, active time, counts and model name), plus each
weekly rate-limit window (its start, the plan and percentages Codex logged for it, and the
tokens counted in it; tokenusage.dev analyses these across sharers and documents its
own methods), plus the [API value](#api-value) per day, per session and in total. Never
sent: prompts, outputs, tool results, file contents or paths, session titles, or anything
from `auth.json`. `--out payload.json` writes the exact payload for you to read without
sending it.

Since 1.10.0 a share also sends each day's counts by service tier (Standard, Fast,
Ultrafast), each weekly window's counts by model and tier when available, response times
by model, effort and tier, and the plan the timed responses fell under, when all can be
attributed to one recorded plan. Fast and Ultrafast mean Codex recorded that setting;
anything else, including no recorded tier, counts as Standard. No per-request records,
tool names, UTC hours or weekdays are sent.

The first share returns a token, kept in `~/.codex/token-counter/share.json` (mode 0600); it
is what lets you update or delete your numbers later. The leaderboard is public and every
figure on it is self-reported.

## What it does that reported usage does not

Reported usage gives totals. It never says what filled the window. This re-tokenizes the
content locally and attributes every token to a category, so you can see that — on the corpus
this was built against — **75.6% of all unique content is tool output**, and that the single
most expensive item is a 66,456-token review instruction carried by 83 consecutive prompts,
costing 5.52M input tokens on its own.

It also charts **how each weekly rate-limit window was spent**: the server's own reported
percentage laid over a locally measured cumulative token curve that restarts at zero every
time the limit resets. Those resets are not on a seven-day grid — a window reading 99% is
replaced, inside a single rollout file a minute later, by a fresh one resetting seven days
from *that* instant — so boundaries are taken from where the reported percentage drops.

To follow public Codex reset announcements, use the [Codex reset tracker](https://tokenusage.dev/resets).
It tracks @thsottiaux's posts and offers text or email alerts. Check your Codex account
for your personal reset times.

The limit chart, the daily chart and the response-time chart are drawn on **one time
axis**, and scrolling, dragging or pinching any one zooms and pans all three — horizontally
only, so heights stay comparable — while the composition chart recomposes over whatever
range is on screen. On a phone too.

It times the work, too: **how long each response took**, from the moment its prompt was
complete — your message or the last tool output — to the moment Codex recorded it, per model
and reasoning effort, with turn time and time per tool beside it. How much of that was
waiting in a queue cannot be read from the logs, which hold no server timing, so the report
estimates it and says so: per model it fits the pace of the fastest tenth of responses for
their size, and reports how much time sat above that pace in `--json` and on its terminal
line. The page charts only measured times: the median and p90 response time by day. The
per-model, hourly, turn and tool breakdowns are in `--json`.

Behind those two lines, on an axis of its own, a bar a day counts the **rate-limit events**
Codex logged: snapshots in which the server reported a limit as reached. A day the limit
blocked outright has bars and no line. A forked session replays its parent's log, so the
parent's events copied into it are left out rather than counted twice.

It also refuses to overclaim. Cache *causation* is not recoverable from rollout logs, so
divergence between prompt-prefix stability and reported caching is presented as a ranked list
of leads, visibly marked as inference, not as findings. The same restraint applies to the
limit: the two curves share an axis, but no tokens-per-percent rate is published, because the
corpus shows 95% of a window costing 2.81B recorded input one week and 790M another.

## API value

The report's API value tile says what the usage would cost **if it were billed at OpenAI's
API list prices**. A ChatGPT plan is not billed per token, so this is a counterfactual, and
the page words it as one. It is priced **per response**, from what Codex recorded for it:

- **the model** of the turn that sent it;
- **the processing tier** Codex requested. Fast mode sends `priority`, which costs 2× or
  more. Codex records the thread's tier in the rollout, but never for a thread's first turn,
  which takes the tier recorded next and is counted as inferred. A thread with no record
  at all, such as a one-turn `codex exec` run, is priced at standard. The terminal line
  gives what those responses would cost in Fast mode;
- **the prompt size.** Above 272K input tokens, models with long-context rates charge them
  for the whole request;
- **OpenAI's input split.** Ordinary input, cached reads and cache writes each have their
  own rate. Reasoning tokens are part of output and are not added again;
- **web searches**, at $10 per 1,000 calls.

The terminal line says what the figure leaves out:

- responses whose model, tier or prompt size has no published rate. Codex's guardian
  reviewer, `codex-auto-review`, is one. These are left out and named, never guessed;
- aborted turns (interrupted, or replaced by a new turn). A response cut off by one writes no
  usage record, so it is counted but cannot be priced;
- the 10% regional-processing uplift.

The prices ship with the plugin in `assets/vendor/openai_prices.json`, so the report works
without internet. Two runs over the same logs agree until the table is refreshed on
purpose:

```
python scripts/fetch_prices.py           # re-vendor from developers.openai.com/api/docs/pricing
python scripts/fetch_prices.py --check   # how the vendored table differs from the live pages
```

The table's date is on the terminal line. Codex models that the pricing page no longer lists, such as
`gpt-5-codex`, `gpt-5.1-codex*` and `gpt-5.2-codex`, are priced from their own model pages.
Those pages publish standard rates only. `--prices table.json` prices the usage with any
table in the same format.

## Where the numbers come from

| On the page | Source |
| --- | --- |
| Input tile, longest session, daily chart, input-by-model pie, cumulative token curve | **tiktoken**: each request's prompt, rebuilt from the log and counted with `o200k_base` |
| Content composition | **tiktoken** |
| Output, cache hit, cached and uncached input | Codex's own usage records |
| Weekly limit used, reset times, the limit's % line | Codex's rate-limit snapshots, as the server reported them |
| Rate-limit events per day | the same snapshots: those in which the server reported a limit as reached |
| Response time, turn time, tool time | the records' own timestamps |
| Time above the fastest pace (`--json`, terminal line) | **an estimate**, fitted to those times; not a measured queue time |
| API value | Codex's own usage records, each response priced at the vendored OpenAI API list prices for its model, tier and prompt size |

Input is counted because its content is in the log. Output and caching are not: reasoning
tokens are encrypted (only summaries are readable), and what is cached is decided on the
server, which the log does not record. The cache hit is Codex's cached input over Codex's
recorded input, never over the tiktoken count.

The tiktoken input reads below what Codex recorded, 86% on the corpus this was built
against, because tool definitions, message framing and resent encrypted reasoning are not
in the log in a form that can be counted. `o200k_base` is itself an assumption: the
tokenizer current Codex models use is not published. The report writes both figures to
`--json` (`input` is Codex's, `tiktoken_input` is counted) and prints both on its terminal
line. Without tiktoken (`--metrics-only`, or before it is installed) every input figure is
Codex's and is labelled "Recorded input".

Tokenizing is already part of every full run, so this costs no extra time: a cold run over
7.7 GB takes ~31s against ~8s without tokenizing, and a warm run ~4s either way.

## Options

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
report.py --no-account             # do not read auth.json; name no account
report.py --no-install             # never install tiktoken; count no content without it
report.py --prices table.json      # price the API value with another table
report.py --doctor                 # what this machine provides, then exit
```

Cold run ~28s over 7.75 GB (~22s with `--fast`); warm run ~4s, from a SQLite index at
`~/.codex/token-counter/index.db`. A window narrows the report, not the ledger: `--since`
still charges over the whole corpus, because a fork child's ancestors may sit outside it.

## What it needs

| Dependency | Required? | Without it |
| --- | --- | --- |
| Python **3.8+** | yes | refuses to start, with the version it found |
| `~/.codex/sessions/**/rollout-*.jsonl` | yes | nothing to report; the error names the directory searched and `CODEX_HOME` |
| `tiktoken` + the vendored `o200k_base` vocabulary | **no**; `tiktoken` installs itself on first use | content composition is empty and says why; every usage figure is unaffected, and `--metrics-only` is the same path chosen deliberately |
| `~/.codex/auth.json` | no | the report names no account (`--no-account` does this on purpose) |
| SQLite index at `~/.codex/token-counter/index.db` | no | every run re-parses; a corrupt, locked or unwritable index is reported and skipped |
| Multiple processes | no | falls back to one, slower and identical |
| A writable `CODEX_HOME` | no | output goes to a directory of the user's own under the system temp directory (`token-counter-<uid>`, 0700), and the path is printed |
| Network | once, to install `tiktoken` from PyPI if it is missing | the install fails, the run continues without content composition, and the next run tries again |
| The vendored price table, `assets/vendor/openai_prices.json` | no; it ships with the plugin and is never downloaded | the API value is left out and the terminal line says why; every other figure is unaffected |

`CODEX_HOME` is honoured everywhere Codex honours it. `--sessions-root` overrides the corpus
alone, and moves the `auth.json` lookup with it so a copied corpus is never stamped with the
live account. Run `python scripts/report.py --doctor` to see all of this resolved for the
machine you are on.

## Verify

```
python scripts/fetch_vocab.py --verify   # vendored tokenizer parity with stock o200k_base
python scripts/test_ledger.py            # 13 response-identity regressions
python scripts/test_pipeline.py          # 317 pipeline assertions
python scripts/test_mutations.py         # every fix must fail when reverted
python scripts/test_share.py             # the share payload, its privacy and its transport
python scripts/bench.py                  # the parallelism grid
python scripts/verify_schema.py          # schema claims against the live corpus,
                                         #   including every rate-limit structural claim
python scripts/verify_install.py         # the installed plugin is this code
python scripts/diag_fork.py              # independent witness for fork-replay exclusion
python scripts/ref_bpe.py                # pure-Python BPE oracle
python scripts/fetch_prices.py --check   # vendored prices against the live pricing pages
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the four test scripts and
`node scripts/test_page.js` on every pull request and every push to `main`, on Ubuntu and
Windows with Python 3.8 and 3.14, plus `fetch_vocab.py --verify`. The rest need a real
`~/.codex` corpus or an installed plugin, so they stay manual. So does
`fetch_prices.py --check`: it reads the live pricing pages, which change whenever OpenAI's
prices do.

## Design

[`ARCHITECTURE.md`](ARCHITECTURE.md) — in particular §2.2, the canonical usage ledger, which
is the one part that has to be right. Two overlapping usage streams exist in the logs and
summing them inflates the total by 30%.

§10 records every correction and the finding that forced it, across six rounds of adversarial
review. Four rounds reviewed the document; two reviewed the code, and found more. The most
recent verdict before the current revision was *"not sound enough to rely on"* — `--since`
overcharged a session by 30.5M tokens, the cache key did not move when extraction changed,
and the installed plugin was not the reviewed code.

## Claude Code

The Claude Code edition is the separate **token-counter-claude 0.1.0** plugin. Its
version, local state and share token are separate from the Codex plugin's. It reads
Claude Code's recorded usage and retained capture history; it ships no tokenizer,
downloads nothing and installs no packages. See [CLAUDE_ARCHITECTURE.md](CLAUDE_ARCHITECTURE.md)
for its counting, retention and sharing contracts.

### Install in Claude Code

```text
claude plugin marketplace add stdcmms-lang/token-counter
claude plugin install token-counter-claude@stdcmms-lang
```

For this local checkout:

```text
claude plugin marketplace add <path to this checkout>
claude plugin install token-counter-claude@stdcmms-lang
```

The two skills are:

```text
/token-counter-claude:token-report
/token-counter-claude:token-share
```

Claude Code substitutes `${CLAUDE_SKILL_DIR}` in each skill's text with the installed
skill directory. It is not a Bash environment variable. The installed plugin needs
only its own copied directory; it does not import the Codex plugin or this checkout.

### Build a local report

Ask Claude Code for a usage report, or run this from the repository root on Windows:

```powershell
python plugins/token-counter-claude/skills/token-report/scripts/report.py --no-open
```

Use `python3` for every command on macOS/Linux. Without `--no-open`, the report opens
the completed HTML with the platform handler. The default page is `report.html` in
the resolved Claude state directory. `--json PATH` writes the complete model;
`--json -` writes only JSON to stdout. Read the terminal's quality counters when
quoting figures. A local report sends nothing and does not authorize a share.

### Share Claude Code usage

Sharing is an explicit, separate action. From the repository root on Windows:

```powershell
python plugins/token-counter-claude/skills/token-share/scripts/share.py --handle HANDLE
python plugins/token-counter-claude/skills/token-share/scripts/share.py --handle HANDLE --yes
python plugins/token-counter-claude/skills/token-share/scripts/share.py --yes
python plugins/token-counter-claude/skills/token-share/scripts/share.py --out payload.json
python plugins/token-counter-claude/skills/token-share/scripts/share.py --delete-report
python plugins/token-counter-claude/skills/token-share/scripts/share.py --delete-report --yes
python plugins/token-counter-claude/skills/token-share/scripts/share.py --delete
python plugins/token-counter-claude/skills/token-share/scripts/share.py --delete --yes
```

Replace HANDLE with your chosen public handle. The default is a dry run: it makes
no network call and creates no token. It captures history, prints whole-month and
window decisions, and writes the sanitized `report-shared.html` preview. `--yes`
submits counts and then uploads the exact bytes of the preview built by that run.
`--no-report` sends counts only. `--out` writes the payload and cannot be combined
with `--yes`. `--delete-report --yes` removes the public page while keeping counts;
`--delete --yes` removes the public usage and page, retires receipts and preserves
local captured history. Preview a deletion without `--yes` first.

The schema-1 client is `claude-usage`. It sends daily recorded counts, selected
hashed-session summaries, safely retained weekly comparison windows and recent
timing summaries. The public page and payload omit prompts, outputs, thinking,
tool data and names, paths, working directories, titles, raw session IDs, account
identity, UTC-hour buckets and weekday buckets. API list value appears in the
sanitized page; it is not an extra numeric field in the stored schema-1 payload.

The first successful POST creates `claude-share.json`, with tokens segregated by
normalized endpoint and bound to the capture history UUID. Never copy, import,
print or reuse a Codex token. `--api` defaults to `TOKENUSAGE_API` when nonempty,
otherwise `https://tokenusage.dev/api`; only HTTPS and test HTTP loopback are accepted.
The transport honours environment proxies and refuses redirects. A failed page
upload is reported separately from a successful count submission. An unknown POST
outcome retains prepared evidence and is not automatically retried.

If a retained token has no matching history, replacement is disabled. Recovery
uses that endpoint's retained token to delete the old sharer, then starts a fresh
first share. **Run deletion only when you have authorized deleting the old public
data**, and dry-run the fresh share before sending it:

```powershell
python plugins/token-counter-claude/skills/token-share/scripts/share.py --delete --yes
python plugins/token-counter-claude/skills/token-share/scripts/share.py --handle HANDLE
python plugins/token-counter-claude/skills/token-share/scripts/share.py --handle HANDLE --yes
```

Recovery preserves capture history. There is no token-import or `--forget` option.

### What the Claude plugin needs

Python **3.8+**, including its standard-library SQLite, is the only runtime
requirement. No tiktoken, vocabulary, price download, dependency installer or
background collector is used. A writable state directory is needed to retain
history; sharing refuses if history cannot be committed. A failed index or process
pool falls back to extraction or one process. Unavailable history permits a live
local lower-bound report and disables sharing.

Let C be nonempty `CLAUDE_CONFIG_DIR`, otherwise `~/.claude`. The defaults are
`C/projects` for transcripts and `C/token-counter/` for state. The only external
account source is `~/.claude.json` when `CLAUDE_CONFIG_DIR` is unset, or
`C/.claude.json` when it is set. With `--sessions-root R`, account lookup is exactly
`R.parent/.claude.json` and state is `R.parent/token-counter/<root-hash>/`;
the root hash is the first 12 hex characters of SHA-256 over the normalized resolved
root path. There is no fallback to the live account for a copied corpus.

The account allow-list is `oauthAccount.organizationType`,
`organizationRateLimitTier`, `emailAddress`, `organizationName` and
`subscriptionCreatedAt`, accepted only as strings or null. Local display uses
email, falling back to organization name. `--public`, shares and history exclude
that identity. History keeps only plan fields and the parsed subscription creation
date. `--no-account` skips the account file and creates no new snapshot; previously
captured snapshots remain usable. Credentials, settings and account cache files
are not usage sources. **Never open `~/.claude/.credentials.json` or another
credentials file**, external tool-result files or image URLs.

Mutable files are `index-cache.db`, `history.db`, `report.html`,
`report-shared.html` and `claude-share.json`. None is stored in the installed plugin
or `${CLAUDE_PLUGIN_DATA}`, so uninstall cleanup does not remove capture history.

### Claude options

All paths below are resolved as described above. Boolean switches default off
unless the table states otherwise; no date or session filter is applied by default.

| Report option | Default and behavior |
| --- | --- |
| `--sessions-root PATH` | Resolved Claude projects root; relocates account and state |
| `--since YYYY-MM-DD`, `--until YYYY-MM-DD` | All captured days; bounds are inclusive |
| `--session PREFIX` | All families; a prefix must select one family in the date/view range |
| `--out PATH` | State `report.html` |
| `--json PATH` | No JSON file; `-` suppresses terminal stdout |
| `--no-open` | Off; otherwise opens completed HTML |
| `--style clinical\|matisse\|nocturne` | Clinical opening style |
| `--public` | Off; allow-listed model without local identity |
| `--metrics-only` | Off; skips text-byte inventory, preserves usage/timing/value |
| `--no-account` | Off; skips account read and new observation |
| `--no-cache` | Off; bypasses only the disposable index |
| `--rebuild` | Off; clears index and re-extracts every live file |
| `--include-archived` | Default alias; conflicts with explicit `--live-only` |
| `--live-only` | Off; changes the view without deleting captured evidence |
| `--prices PATH` | Vendored offline Anthropic table; optional same-format override |
| `--procs N` | Half logical cores, minimum one; Windows maximum 61 |
| `--fast` | Off; all cores, Windows maximum 61; conflicts with `--procs` |
| `--quiet` | Off; suppresses progress, preserves warnings and summary |
| `--doctor` | Off; read-only path/parser/state diagnostics, no capture or network |
| `--version` | Prints `token-counter-claude 0.1.0`, then exits |

| Share option | Default and behavior |
| --- | --- |
| `--handle HANDLE` | Saved handle; first actual share requires one; normalizes lowercase |
| `--yes` | Off; explicit network mutation |
| `--out PATH` | No payload file; writes payload without sending; conflicts with `--yes` |
| `--no-report` | Off; otherwise builds preview and uploads it after counts on `--yes` |
| `--style clinical\|matisse\|nocturne` | Clinical |
| `--delete`, `--delete-report` | Off; exclusive actions, dry-run without `--yes` |
| `--api URL` | `TOKENUSAGE_API` or `https://tokenusage.dev/api` |
| `--sessions-root PATH` | Same resolution as report |
| `--no-account` | Off; skips account read/new snapshot |
| `--prices PATH` | Vendored offline table |
| `--no-cache`, `--rebuild` | Off; index only, retained history survives |
| `--procs N`, `--fast` | Same worker defaults/conflict/Windows cap as report |
| `--quiet` | Off; suppresses collection progress, preserves decisions and warnings |
| `--version` | Prints product/version, then exits |

Share has no date, session or live-only filter. Neither CLI accepts plan
declarations, tokenizer/vocabulary options or a dependency-install option.

### Where Claude's numbers come from

Recorded input is **base input + cache creation + cache reads**, with the three
disjoint components counted once. Cache hit is reads divided by whole recorded
input. Output already includes thinking; known thinking is a subset and is never
added again. Missing thinking makes reported reasoning a lower bound. Responses
are globally deduplicated by `(message.id, requestId)`, with the whole last valid
block's usage; conflicting copies are quarantined. Date filters apply afterwards.

The composition view is **Visible text inventory**, in UTF-8 bytes, including
captured text and saved system/tool snapshots. Byte shares are not Claude token
shares or a reconstructed API prompt. Inferred image counts remain local JSON,
outside the byte pie and recorded input. Per-content token attribution,
reconciliation, resend cost, amplification and cache causation are unavailable.
Background/advisor and compaction calls without separately recorded usage cannot
be recovered; cost-state and `/usage` cost figures are cross-checks only.

**API list value** prices each captured response at the offline Anthropic table of
**2026-10-10**, including the whole-prompt Haiku 5.5 band above 100,000 tokens,
recorded speed, exact known write TTLs and supported explicit-US residency. It is
a comparison, not spend or a bill. `[1m]` adds no surcharge. Missing speed defaults
to Standard for pricing only; missing nonzero write TTL defaults to all-5m;
missing/`not_available` geography defaults to global; missing search count defaults
to zero. Recorded searches add $0.01 each and fetches add no fee. These choices
have counters. `usd_high` applies supported Fast and all-1h scenarios together
for missing speed/TTL; it does not bound missing calls, unrecorded searches,
unpriced models, unsupported speed/geography or future settings. Missing geography
does not create a US upper scenario. Unknown models/settings are named and unpriced;
partial captured usage and omitted background/compaction usage are disclosed.

Response time is a transcript interval from the latest known prompt-side record,
frozen at first output and floored within the stream, to its last accepted block.
It includes network/retry/queue time together. Fitted time above the fastest pace
is an estimate, not measured queue time. Computed stream turns, parallel tool
intervals (including possible approval waits) and logged `turn_duration` values
are separate measurements.

### Transcript retention and captured history

`history.db` permanently retains content-free evidence, original calendar
assignment and prepared/confirmed share contributors. Reports include captured
history by default, including transcripts that disappeared. Calendar completeness
is **unknown**: gaps before first capture cannot be recovered, and activity on
other devices or surfaces may be absent. The terminal reports archived responses.

`--rebuild` discards only `index-cache.db`, re-extracts every live file on that run,
then merges without deleting retained history. `--no-cache` also bypasses only
the index. Shortening a transcript does not replace its retained terminal with
an earlier block; new later blocks can advance it, while incompatible revisions
remain conflicts. `--live-only` narrows the local view and does not erase evidence.

A share replaces a whole captured month only when previously submitted counts,
contributors and captured dates can be reproduced safely. An unsafe month is
withheld whole. Because `windows` replaces the server's entire list, an unsafe
replacement omits the key, preserving stored windows; it never sends an empty or
pruned list to represent unavailable evidence. Prepared unknown-outcome submissions
are protected too. No local history deletion or coverage bypass repairs a refusal.

### Historical plans and sparse quota readings

Every normal account-reading report/share captures observation time, organization
type, rate-limit tier, mapped current plan and parsed subscription creation date.
No identity enters those observations. Personal Pro and known Max 5x/20x map to
`claude:pro`, `claude:max-5x` and `claude:max-20x`; Team, Enterprise, unknown or
contradictory metadata maps to null.

A window gets plan P only when a captured observation maps to non-null P, **every**
observation at or after its first reading maps to P (including later runs), the
latest predecessor observation if present maps to P, and the latest observation's
subscription creation date is present and no later than that first reading.
Otherwise its plan is null. Latency uses the same rule from the earliest accepted
sample end. These are captured account observations, not independently verified
subscription history or transcript plan records.

A plan change that leaves subscriptionCreatedAt unchanged is not detectable until a later run observes the new tier. A window that ended before that run can therefore carry the old plan.

Whether `subscriptionCreatedAt` moves on upgrade/downgrade is **UNVERIFIED**.

Quota points come from sparse structured `/usage` records and qualifying rejected
weekly 429s. A `rateLimitType: "seven_day"` rejection supplies a sourced 100%
weekly-all point under the explicit all-model scope assumption;
`limit_429_assumed_all_models` counts it. That literal's exact scope is
**UNVERIFIED**. Session (five-hour) and scoped weekly readings stay separate.
Reset clustering uses a full min/max span of at most two seconds. The observed
weekly grid is diagnostic only: it is learned per account, not a universal
Thursday grid, and never snaps or invents a window.

The chart accumulates recorded input plus output over the **whole nominal window**
`[reset − 7 days, reset)`, with API list value as an alternate metric and sparse
percentage points. The start is inferred; no 0% opening is invented. Sharing uses
only `(first reading, earliest peak reading]`; plateau readings do not extend its
numerator. JSON carries both sets of counts. Declines, conflicts, missing endpoints,
partial responses and unavailable nonzero write TTLs withhold new comparisons.

Missing recorded speed stays unknown. Its counts are accepted in days/windows but
remain outside known `tiers` and `split`; the dry run prints the deduplicated shared
response count without speed. Pricing it locally at Standard does not establish
a recorded Standard class. Fast, US-residency and long-prompt Haiku window counters
are diagnostics and do not withhold otherwise shareable windows. The server's
Fast multiplier (2.5 versus supported Claude 2), Sonnet 5.5 cache-read catalog price
($0.20 versus published $0.10 per million), missing US modifier and missing Haiku
long-prompt band are owner corrections, not reasons to scale counts or relabel rows.

### Claude verification

```text
python -B scripts/check_manifests.py
python -B scripts/check_manifests.py --existing-only
python -B scripts/sync_shared.py --check
python -B scripts/test_codex_compat.py
python -I -S -B scripts/test_claude_ledger.py
python -I -S -B scripts/test_claude_pipeline.py
python -I -S -B scripts/test_claude_retention.py
python -I -S -B scripts/test_claude_windows.py
python -I -S -B scripts/test_claude_pricing.py
python -B scripts/test_claude_share.py
python -B scripts/test_claude_mutations.py
node scripts/test_claude_page.js
python -B scripts/fetch_anthropic_prices.py --from scripts/fixtures/anthropic_pricing.md --as-of 2026-10-10 --check
```

The manifest checker requires both marketplaces, versions, contained paths,
byte-identical shared files, offline price parity, frozen fixture hashes and
server provenance/lockfile pins. It runs the compatibility sensitivity self-test
and a synthetic installed-copy report/share smoke with no sibling checkout,
network or token, including both interpreter variants and skill-dir substitution.
`--existing-only` permits unfinished packaging while checking present surfaces.
Claude's stdlib suites run with site packages disabled; the development-only
server harness uses pinned Zod 4.6.5 and TypeScript 5.9.3. It checks Zod parsing
and plausibility, not the HTTP route; stricter local-only checks are documented
in [CLAUDE_ARCHITECTURE.md §13](CLAUDE_ARCHITECTURE.md#13-share-schema1-mapping-and-server-limitations).

CI preserves the four Ubuntu/Windows Python 3.8/3.14 Codex cells and suites, adds
Claude suites/checks to them, and adds a Claude-only macOS-14/Python 3.14/Node 22
cell. Each independent check still runs after another check fails. The contract
lockfile is installed in CI with `npm ci --prefix scripts/server_contract`.

Both report pages have no external runtime dependencies or networking. The fixed
`<a href="https://tokenusage.dev">tokenusage.dev</a>` is a deliberate navigational
brand link; rendering/opening a report itself sends nothing. All other external
references and network primitives are rejected by self-containment tests.
