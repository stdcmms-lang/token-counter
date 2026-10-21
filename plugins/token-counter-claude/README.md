# Token Counter for Claude Code

Token Counter for Claude Code shows what your Claude Code usage would cost at Anthropic's API
list prices, and how it filled your weekly limit: it charts your usage within each weekly
all-model limit window it observed, alongside the quota readings Claude Code recorded, and its
JSON output attributes each window to Pro, Max 5x or Max 20x when the account observations it
captured support that. The report is a local HTML page built from the transcripts Claude Code
keeps on your machine and the history the plugin retains, with input, output, cache reads and
response times. By default, prices come from a table bundled with the plugin and nothing is
fetched, and making the report sends nothing. A Codex edition, `token-counter`, is in the same
repository.

Sharing is separate and opt-in. The token-share skill can publish your totals and a sanitized
copy of the report page to the public leaderboard at
[tokenusage.dev](https://tokenusage.dev/share). Its default is a dry run that sends nothing;
publishing needs `--yes`.

## Install

```text
claude plugin marketplace add stdcmms-lang/token-counter
claude plugin install token-counter-claude@stdcmms-lang
```

Then ask Claude Code for a usage report, or run `/token-counter-claude:token-report`.
`/token-counter-claude:token-share` previews a share without sending anything.

## What it reads, runs and sends

Paths below are the defaults. A nonempty `CLAUDE_CONFIG_DIR` moves `~/.claude` to that
directory and `~/.claude.json` into it.

- **Reads:** Claude Code transcripts under `~/.claude/projects`, the plugin's own state under
  `~/.claude/token-counter/`, the bundled price table, and five allow-listed `oauthAccount`
  fields from `~/.claude.json`: the plan fields (`organizationType`,
  `organizationRateLimitTier`, `subscriptionCreatedAt`) and the email or organization name that
  labels your local report. `--no-account` skips that file. It does not open Claude's
  credential files, such as `~/.claude/.credentials.json`.
- **Writes:** in `~/.claude/token-counter/`: a history database, an index cache, the report
  pages and, after a first share, `claude-share.json`. Deleting a share keeps this local
  history.
- **Runs:** two Python scripts, `report.py` and `share.py`, on Python 3.8 or later with the
  standard library only; collecting can use local worker processes. The report script can open
  the finished page with your system's default handler (`--no-open` turns that off). It
  installs no packages and fetches nothing.
- **Sends:** nothing, unless token-share runs with `--yes`. Then it posts JSON to
  `https://tokenusage.dev/api` (or the API base set with `--api` or `TOKENUSAGE_API`, which
  must be HTTPS, or HTTP to a loopback address for testing) and uploads the sanitized report
  page, which `--no-report` leaves out. The JSON holds your public handle, daily recorded token
  counts, selected session summaries keyed by a one-way hash (dates, start and end times,
  active time, counts and model), weekly limit windows (plan, percentages and counts) and
  recent response-time summaries. Prompts, outputs, thinking, tool arguments and results, tool
  names, file names and paths, working directories, session titles, raw session IDs and
  account identity are not sent, and neither are hour-of-day or weekday breakdowns, though
  session start and end times are. The first share returns a token, kept in
  `claude-share.json`; updates and deletions send it to prove the entry is yours, so keep it
  private. `--delete-report --yes` takes the page down, and `--delete --yes` removes everything
  you shared.

Full documentation:
[github.com/stdcmms-lang/token-counter](https://github.com/stdcmms-lang/token-counter#claude-code).
MIT licence.
