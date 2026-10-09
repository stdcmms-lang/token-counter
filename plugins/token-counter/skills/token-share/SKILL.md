---
name: token-share
description: Share Codex token usage with the public tokenusage.dev leaderboard and get a link to the report page, update a previous share, or delete it. Use only when the user explicitly asks to share, publish, post or upload their token usage or report, to join or update the tokenusage.dev leaderboard, or to remove their data from it. For a private local report, use token-report instead.
---

# Token Share

Sends a summary of the user's Codex token usage to **tokenusage.dev**, where it appears on
public leaderboards (most tokens in a month, longest session, biggest session, most active
days) and on a public profile page at `https://tokenusage.dev/u/<handle>`. It also publishes
the token-report page itself at `https://tokenusage.dev/r/<handle>`: the same page, charts and
styles the user gets locally, for anyone they send the link to.

This is the only part of the plugin that sends anything over the network, and it sends
nothing unless run with `--yes`. (token-report's one other network call downloads `tiktoken`
from PyPI when it is missing. The share never does that, so the page published on `--yes` is
the page the dry run wrote.) The numbers come from the same canonical usage ledger as
token-report, so the leaderboard agrees day for day with the report's recorded figures. The
report page counts input with tiktoken, so its input reads lower than the leaderboard's. If
the user compares the two, that is why.

Run from this skill's directory, with `python3` on macOS and Linux or `python` on Windows.

## Always dry-run first, and get consent

1. Run the dry run. It reads the logs, prints exactly what would be shared and sends nothing:

   ```bash
   python3 scripts/share.py
   ```

2. Show the user the summary it printed: the handle, the date range, the totals, the API
   value line, and the per-month lines. Tell them the leaderboard is **public**, and that the report page the dry
   run wrote (the `report page` line gives its path) is published as is: anyone with the link
   sees it. They can open that file to check it first.
3. On a first share, ask which **handle** they want shown (3-24 characters: lowercase
   letters, digits, hyphens). Do not invent one, and do not derive it from their account,
   email or machine name.
4. Only after they agree, send:

   ```bash
   python3 scripts/share.py --handle THEIR-HANDLE --yes   # first share
   python3 scripts/share.py --yes                         # every later share
   ```

   The last line of output is their report URL, `report: https://tokenusage.dev/r/<handle>`.
   Give it to them; the line before it is their profile on the leaderboard.

   The shared page opens in Clinical unless `--style` says otherwise. If the user says which
   style they use locally (Clinical, Matisse or Nocturne), or asks for one, pass it:
   `--style matisse`. Readers can still switch styles on the page.

Sending needs network access. Inside the Codex sandbox the network is usually off, so the
`--yes` command must run with network access the user approves. The dry run needs none.

## What is sent, and what never is

Sent: per-day counts (responses, recorded input, cached input, output, reasoning, sessions
started); for each month the top sessions by active time and by tokens, each as a one-way
hash of its id, start and end times, active time, token counts and the model name; and for
each weekly rate-limit window, its start, the plan type and the percentages used that Codex
logged in its rate-limit snapshots, and the tokens counted in it. tokenusage.dev analyses the
windows across sharers and documents its own methods. Also, over the last 30 days of responses: how many were timed, and their median and p90
response time and turn time in seconds, in total and for each model and reasoning effort
(no per-request times, no tool names, and no hour-of-day or weekday breakdown). And the API
value token-report computes: what the usage would cost at OpenAI's API list prices, per day
(`api_usd`), per summarised session, and in total with the price table's date and how many
responses could be priced (`api_value`).

Since 1.10.0 a share also sends each day's counts by service tier (Standard, Fast,
Ultrafast), each weekly window's counts by model and tier when available, response times
by model, effort and tier, and the plan the timed responses fell under, when all can be
attributed to one recorded plan. Fast and Ultrafast mean Codex recorded that setting;
anything else, including no recorded tier, counts as Standard. No per-request records,
tool names, UTC hours or weekdays are sent.

Also sent, unless `--no-report`: the report page, rendered by token-report with `--public`.
It carries the charts' data (daily input by model, the cumulative token curve and
reported percentage of each weekly limit window over time, and content composition by
category in hourly buckets, and the median and p90 response time and the number of
rate-limit events for each day) and the headline numbers. It leaves out the two machine
strings the local page shows, the top session's id and its directory name, and never reads
`auth.json`. The server serves it in a sandbox: its scripts run, and it can reach nothing.

Never sent: prompts, outputs, tool results, file contents or paths, working directories,
session titles, anything from `auth.json`, or the account email. The plan type comes from
the rollout logs, not from the account. The handle the user picks is the only identity.

Use `--out payload.json` to write the exact payload to a file for the user to inspect,
without sending it.

## Options

```bash
python3 scripts/share.py                        # dry run
python3 scripts/share.py --out payload.json     # write the payload, send nothing
python3 scripts/share.py --handle NEW --yes     # rename (the old handle is released)
python3 scripts/share.py --yes --style nocturne # the shared page opens in Nocturne
python3 scripts/share.py --yes --no-report      # the numbers only, no report page
python3 scripts/share.py --delete-report --yes  # take the report page down, keep the numbers
python3 scripts/share.py --delete               # say what would be deleted
python3 scripts/share.py --delete --yes         # delete everything shared, forget the token
python3 scripts/share.py --forget               # drop the local token only
python3 scripts/share.py --sessions-root PATH   # a corpus somewhere else
python3 scripts/share.py --fast                 # every core instead of half
```

The first share returns a token, stored in `~/.codex/token-counter/share.json` (mode 0600).
It is the only proof of who owns the handle: later shares and deletion need it. Never print
it or paste it anywhere. Lose it, and the numbers already shared cannot be updated or deleted
from this machine; `--forget` then lets the user start over under a new handle.

## How the figures are defined

- **Tokens** on the leaderboard are recorded input plus output. Cached input is part of
  recorded input, not added to it. These are counts Codex wrote to its logs, not a bill.
- **API value** is those recorded responses priced at OpenAI's API list prices, the same
  figure as the report's tile. It is what the usage would cost on the API, not what the
  user paid; a day with nothing priced (and no web search fee) is sent as null.
- **Days** are the user's local calendar days, as in token-report; a session belongs to the
  day and month its first response landed in.
- **Active time** is the time between consecutive responses in a session, leaving out any
  gap longer than 30 minutes. Wall-clock span is shown beside it.
- Each share replaces the months it contains, and the report page as a whole. Months no longer in the logs (pruned rollout
  files) stay as they were last shared.

Every figure is self-reported. The server rejects arithmetic no log can produce, but it
cannot verify a report, and the site says so.

## If it fails

- `the first share needs a leaderboard name` -- ask the user for a handle, add `--handle`.
- `handle_taken` (409) -- that handle belongs to someone else; ask for another.
- `bad_token` (401) -- the stored token was rejected. `--forget`, then share under a handle.
- `rate_limited` (429) -- too many shares this hour; later.
- `could not reach` -- no network: approve network access for the command, or try later.
- `report page not published` -- the numbers were shared (the profile line printed) but the
  page was not; the message says why. Sharing again retries it.
- `report page could not be built` -- token-report failed; the numbers can still be shared.
  Run token-report's `--doctor`.
- `No rollout files found` -- same as token-report; `--sessions-root` or `CODEX_HOME`.
