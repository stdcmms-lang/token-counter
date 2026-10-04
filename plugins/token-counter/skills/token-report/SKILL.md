---
name: token-report
description: Build a local HTML report of Codex token usage from rollout logs. Use when asked about token usage, the context window, cached vs uncached input, prompt caching, which sessions or tool outputs burn the most tokens, how long responses, turns or tools take (latency, response time, waiting or queueing), what the usage would cost or is worth at API prices (dollar value, API-equivalent cost), when the weekly rate limit last reset or how much of it is used, how often the rate limit was hit, which account the usage belongs to, or for a token/usage report or dashboard.
---

# Token Report

Re-tokenizes `~/.codex/sessions/**/rollout-*.jsonl` locally and renders a self-contained HTML
dashboard. Every figure comes from rollout logs; `~/.codex/auth.json` is read only to name
the account (id_token identity claims — never the access or refresh tokens, and
`--no-account` skips it). Nothing is intercepted and nothing of the user's is sent anywhere.
The API value is priced from a table of OpenAI's list prices that ships with the plugin; it
is never fetched. The one network call is installing `tiktoken` from PyPI, once, when it is
missing (see below). Publishing numbers to the tokenusage.dev leaderboard is
a different skill, token-share, used only when the user asks to share.

Run from this skill's directory. Use whichever interpreter name exists on the machine:
`python3` on macOS and Linux, `python` on Windows, where `python3` is usually a Microsoft
Store stub that opens a web page instead of running anything. Python 3.8 or newer.

```bash
python3 scripts/report.py            # macOS, Linux
python  scripts/report.py            # Windows
```

That covers every session, writes `~/.codex/token-counter/report-all.html`, and opens it.

## First run: tiktoken installs itself

Installing the plugin does not install Python packages. When `tiktoken` is not importable,
the first run installs it with `pip install --target` into
`~/.codex/token-counter/lib/<interpreter>-<platform>/` and prints
`tiktoken is not installed; installing it for token-counter (once, from PyPI)`. The user's own
Python environment is not changed, and later runs reuse that directory without the network.

That install needs network access, which the Codex sandbox usually blocks. If the output says
`tiktoken could not be installed: PyPI is unreachable`, the report was still written, with
every usage figure exact and only the content composition empty. Tell the user, and offer to
re-run the same command once with network access they approve. Do not run `pip install`
yourself instead. If the user does not want anything downloaded, pass `--no-install`.

## Options

```bash
python3 scripts/report.py --since 2026-09-01          # windowed
python3 scripts/report.py --since 2026-09-01 --until 2026-09-14
python3 scripts/report.py --session 019fa0e6          # one session (id or prefix)
python3 scripts/report.py --fast                      # use every core instead of half
python3 scripts/report.py --metrics-only              # usage ledger only, no tokenization
python3 scripts/report.py --json model.json           # machine-readable model
python3 scripts/report.py --no-open                   # write the file, do not launch a browser
python3 scripts/report.py --rebuild                   # discard the index and re-parse
python3 scripts/report.py --no-account                # do not read auth.json
python3 scripts/report.py --no-install                # never install tiktoken from PyPI
python3 scripts/report.py --sessions-root PATH       # a corpus somewhere else
python3 scripts/report.py --prices table.json        # price the API value with another table
python3 scripts/report.py --doctor                   # what this machine provides, then exit
```

Run `--doctor` first when anything looks wrong or empty. It prints the interpreter, the
resolved `CODEX_HOME`, the sessions root and what is in it, the auth mode, the vocabulary and
the index, marking every failed check with `!`, and exits non-zero if any check failed.

Use `--no-open` when the user only wants numbers, and read the summary the script prints on
stdout: one line of totals (responses, input counted with tiktoken, then Codex's recorded
input, uncached, cache hit and output; without the tokenizer, recorded input only), the API
value with the lines under it saying what it priced and what it could not, a line
of response time (median, p90, and the estimated share above the fastest pace), a line
of rate-limit events (snapshots in which Codex logged a limit as reached, and how many days
they fell on) when there were any, and, when an account and a limit window are available, a
line with the account, plan, weekly limit used, when the current window opened, and when it
next resets. Use `--json` when you need to answer a specific question rather than hand over
a page.

The first run parses the whole corpus; later runs reuse a SQLite index at
`~/.codex/token-counter/index.db` and only re-read files that changed. On the development
machine (1,953 files / 7.7 GB, 16 cores) a cold run is ~31s (~22s with `--fast`) and a warm
run ~4s.

## Reporting results

These things must survive into whatever you tell the user, because the report is easy to
over-read:

- **Input is counted with tiktoken; output and caching are Codex's.** The input tile, the
  longest session, the daily chart and the cumulative curve count each request's prompt as
  rebuilt from the log. That reads below what Codex recorded (86% on the development corpus),
  because tool definitions, message framing and encrypted reasoning are not in the log in a
  countable form. Output, cached input and the cache hit are Codex's recorded figures:
  reasoning tokens are encrypted and caching is decided on the server. Say which is which
  when you quote a number. If the tile says "Recorded input", the run had no tokenizer and
  every figure is Codex's.
- **Recorded, not billed.** Codex's figures are the counts it wrote to its logs. Legacy
  compaction intervals carry no usage evidence, so this is recorded coverage, not a billing
  total.
- **Cached input is a subset of Codex's recorded input**, not an extra charge on top of it.
  It can exceed the tiktoken input, which is why the cache hit is never measured against
  that.
- **Cache-divergence rows are leads, not findings.** Rollouts carry no request body, no send
  timestamp, no cache key and no TTL, so cache *causation* is not recoverable. Cache also
  survives across sessions, and the log is written in completion order, not request order.
- **The reconciliation residual does not validate the tokenizer.** `o200k_base` is an
  assumption; the encoding current Codex models use is not published.
- **Weekly-limit percentages are the server's own figures**, recorded verbatim from the
  logs — not something this tool measured, and not derived from any token count on the page.
  Window boundaries, by contrast, *are* inferred: they are taken from where the reported
  percentage drops, because resets are routinely earlier than seven days.

- **Response time is measured; the queue is not.** A response runs from the moment its
  prompt was complete (the user's message or the last tool output) to the moment Codex
  recorded it, so it covers network, queueing, reading the prompt and writing the answer
  together. "Time above the fastest pace" is an estimate fitted to those times, worded as
  one on the terminal line; so are the per-model overhead and output rate in `--json`
  (`latency.groups[].fit`). The page shows none of it. Never call the time above the pace a queue
  time: retries, slow generation and ordinary variation land there too. Tool time includes
  any wait for the user's approval.

- **The API value is a counterfactual, not a bill.** It is what the recorded usage would
  cost at OpenAI's API list prices as of the table's date, priced per response by model,
  the processing tier Codex requested (Fast mode costs 2× or more), prompt size and the
  cached/uncached split. Say "would cost at API list prices", never "you spent" or "you were
  billed": a ChatGPT plan is not billed per token. Always pass on the lines under it:
  responses it could not price (the model or tier has no published rate) and aborted turns
  (a response cut off by an interruption writes no usage record) are not in the figure.
  Codex never records the tier for a thread's first turn, so that turn takes the tier
  recorded next (an inference, counted). A thread with no tier recorded at all, such as a
  one-turn `codex exec` run, is priced at standard, and the line gives what it would cost in
  Fast mode. If the user uses Fast mode, quote that higher figure as the upper bound.

Quote the API value the report computed; do not price tokens yourself, with other rates, or
from the totals, which cannot tell which responses ran in Fast mode or over 272K tokens. If
the user wants other prices, `--prices table.json` takes a table in the vendored format. To
refresh the vendored prices, `python3 scripts/fetch_prices.py` from the repository root is a
packaging step, like the vocabulary; the report never downloads them.

Do not invent a tokens-per-percent rate — the corpus shows 95% of a weekly window costing
2.81B recorded input one week and 790M another. Quoting the reported percentage and the
reset times is fine; those are recorded facts.

## If it fails

- `tokenizer unavailable` — the run continues without it. Input then shows as Codex
  recorded it (the tile reads "Recorded input"), every other headline number, the weekly
  limit chart and the daily chart are unaffected, and the composition panel says why it is
  empty.
  If the reason is that `tiktoken` could not be installed, re-run with network access (see
  *First run*). A missing or corrupt vocabulary is fixed with `python3 scripts/fetch_vocab.py`
  once from the repository root; that is a packaging step, and the report never downloads
  the vocabulary.
- `API value not available` — either no response's model is in the price table (the line
  names the models), or the table could not be read (a broken `--prices` file, or a damaged
  install: `--doctor` shows the table it found). Every other figure is unaffected.
- `No rollout files found` — Codex has not written any sessions yet, or `CODEX_HOME` points
  elsewhere. The message prints the directory it searched. Pass `--sessions-root` to override.
- `index unusable` / `process pool unavailable` — both are optimisations and both degrade on
  their own: the run continues without the cache, or on a single process. Slower, identical
  output.
- `could not write ...` — the report falls back to the system temp directory and prints the
  path it actually used. The last line of stdout is always where the file is.
- Anything else — `--doctor` first, then `--rebuild`, which discards the index and re-parses.

## What the report contains

Headline numbers — input (counted with tiktoken), output, cache hit, the API value (when
anything could be priced), sessions, and the weekly limit as the server's own reported
percentage — plus the longest session, and three
charts: a cumulative token curve per weekly limit window with the reported percentage
overlaid; daily input stacked by the model that was charged for it; response time by day,
as two lines, the median and the p90, with the rate-limit events Codex logged each day as
bars behind them on an axis of their own; and content composition by category. The median response time is also a tile. The per-model
response times and pace estimates, the hour of day, turn time and time per tool are in
`--json` (`latency`), not on the page.

The three time charts share one axis, so a day in one is the same x in the others.
Scrolling, dragging or pinching any of them zooms and pans all three — horizontally only, the value axes do
not move — and the composition chart recomposes over whatever range is on screen. It works
the same on a phone. There are no zoom or reset buttons: a double-click on any of the charts
returns all three to the full range.

Everything else the run produces — sessions, reconciliation, images, the per-window table and
the data-quality counters — is in `--json` and the stdout summary, not on the page.

Figures derived from inference rather than measurement are marked in the page itself.
