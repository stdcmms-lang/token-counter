# token-counter

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
codex plugin marketplace add jack-beanstalk-2022/token-counter
codex plugin add token-counter@jack-beanstalk-2022
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
git clone https://github.com/jack-beanstalk-2022/token-counter
codex plugin marketplace add ./token-counter
codex plugin add token-counter@jack-beanstalk-2022
```

### Desktop app

Codex in the ChatGPT desktop app installs from the same marketplace, but has no documented way
to add one, so the marketplace has to be made known to it first. Either register it with the
CLI:

```
codex plugin marketplace add jack-beanstalk-2022/token-counter
```

or clone this repository and open it as a project: the app picks up
`.agents/plugins/marketplace.json` from a project's root without being told. Then restart the
app, open **Plugins**, choose the **jack-beanstalk-2022** source and install **Token Counter**.
Once the marketplace is known, this link opens the same install flow:

```
codex://plugins/install/token-counter?marketplace=jack-beanstalk-2022
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
tokens counted in it; tokenusage.dev estimates each plan's weekly limit from these), plus
the [API value](#api-value) per day, per session and in total (tokenusage.dev ignores it
for now: its schema drops fields it does not know). Never
sent: prompts, outputs, tool results, file contents or paths, session titles, or anything
from `auth.json`. `--out payload.json` writes the exact payload for you to read without
sending it.

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
