# Security

## Reporting a vulnerability

Report security problems privately to **support@tokenusage.dev**, not in a public issue or pull
request. Say which plugin and version you tested, what you found and how to reproduce it, and
please allow time for a fix before you disclose it publicly.

Fixes go into the latest version on `main`. Older versions are not patched.

## Scope

- The Codex plugin, `plugins/token-counter`, and the Claude Code plugin,
  `plugins/token-counter-claude`: their scripts, the reports they write and the files they keep.
- The opt-in token-share skill in each plugin, and the requests it sends to tokenusage.dev.
- The opt-in cc-api skill in both plugins and its launch of the user's own
  Claude Code CLI.
- Problems with tokenusage.dev itself, including its API and the shared report pages, can go to
  the same address.

## What each plugin reads and sends

Paths are the defaults; `CODEX_HOME` and `CLAUDE_CONFIG_DIR` move them.

- **token-report (Codex)** reads `~/.codex/sessions/**/rollout-*.jsonl` and JSON-decodes
  `~/.codex/auth.json` to take a few allow-listed account fields. Access and refresh token
  values are never extracted, used or sent, and `--no-account` skips the file. It uploads
  nothing. Its only network access installs `tiktoken` and its dependencies (with pip, or uv
  where pip is missing) into `~/.codex/token-counter/lib/` when tiktoken cannot be imported; a
  failed install is tried again on a later run. `--no-install` or `TOKEN_COUNTER_NO_INSTALL=1`
  turns it off. The vocabulary and the price table ship with the plugin.
- **token-report (Claude Code)** reads Claude Code's transcripts and five allow-listed fields
  from `~/.claude.json`, and does not open Claude's credential files. It makes no network
  requests and installs nothing.
- **token-share (both plugins)** sends nothing without `--yes`; the default is a dry run. With
  `--yes` it posts JSON to `https://tokenusage.dev/api`, or to the API base given by `--api` or
  `TOKENUSAGE_API`, and uploads the sanitized report page unless `--no-report` is given. The
  JSON holds a public handle, daily token counts, session summaries keyed by a one-way hash
  (with start and end times), weekly limit windows and response-time summaries; the Codex
  plugin also sends API list value. Prompts, outputs, tool arguments and results, file contents
  and paths, session titles, account identity and Codex or Claude login credentials are never
  sent. The README's [Sharing](README.md#sharing) and
  [Share Claude Code usage](README.md#share-claude-code-usage) sections describe sharing in
  full.
- **cc-api (both plugins)** reads CC_API_KEY, otherwise the UTF-8 file named
  by CC_API_KEY_FILE, and strips surrounding whitespace. It runs the user's
  own `claude -p` in the caller's working directory. That CLI sends the task
  and the workspace/tool context it selects to Anthropic for API processing.
  Its existing configuration and proxy behavior still apply. The helper
  passes the selected key only through the child's ANTHROPIC_API_KEY
  environment variable. It removes inherited credential and provider-selection
  variables and CC_API_KEY/CC_API_KEY_FILE from the copied child environment
  (see the exact [environment policy](ARCHITECTURE.md#62-api-key-claude-code-tasks)).
  Other configuration, including CLAUDECODE, CLAUDE_CODE_GIT_BASH_PATH and
  ANTHROPIC_MODEL, is inherited unchanged. It never prints the selected key, places
  it in child arguments or writes a key/log/state file. Captured child output
  is redacted before display. Claude Code and its tools receive the API
  credential and can write their normal session/workspace files.
  The helper changes no login or configuration files and sends nothing to
  tokenusage.dev. The calling session keeps its own login and plan.
  Anthropic determines whether monthly credits cover the API run; otherwise
  normal API rates apply.
- The first share returns a token, kept in `~/.codex/token-counter/share.json` (Codex) or
  `~/.claude/token-counter/claude-share.json` (Claude Code). Updates and deletions send it to
  prove the entry is yours, and anyone who holds it can change or delete that leaderboard
  entry, so keep it private.
