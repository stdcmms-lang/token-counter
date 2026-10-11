---
name: token-report
description: Report Claude Code token usage, cache reads and writes, API list value, quota readings, and response times from local Claude Code transcripts.
user-invocable: true
disable-model-invocation: false
argument-hint: "[date range, session, style, or report question]"
---

Build a local Claude Code usage report with this plugin's script.

The report reads Claude Code session transcripts and retained local capture history.
Its headline uses recorded prompt counts: base input plus cache creation plus cache
reads. Its composition view is a labelled UTF-8 byte inventory, not Claude token
attribution. It sends nothing and installs no dependencies.

Use Python 3.8 or newer. On Windows run:

```powershell
python "${CLAUDE_SKILL_DIR}/scripts/report.py" --no-open
```

On macOS or Linux run:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/report.py" --no-open
```

Add only options supported by the user's request:

- `--since YYYY-MM-DD` and `--until YYYY-MM-DD` select inclusive captured local days.
- `--session PREFIX` selects one session family, including its captured subagents and continuations.
- `--style clinical`, `--style matisse`, or `--style nocturne` selects the opening style.
- `--json PATH` writes detailed figures and data-quality counters.
- `--metrics-only` skips the text-byte inventory.
- `--no-account` skips the account file entirely.
- `--sessions-root PATH` selects another transcript root and its separate local state.
- `--live-only` shows currently present transcripts; retained history remains captured.
- `--no-cache` bypasses the disposable index.
- `--rebuild` clears only the disposable index, re-extracts live files, and merges without deleting retained history.
- `--doctor` checks paths, parsing support and local state without writing or sending.
- `--out PATH` chooses the HTML destination.

Use `python` for the Windows variants of every command and `python3` for macOS/Linux.
Keep the quoted script path exactly as shown.

Read the terminal summary and material data-quality counters before describing the result.
Link the generated HTML and any requested JSON. Explain missing or partial measurements.
Call the dollar figure API list value, not spend or a bill. Distinguish recorded token
counts from byte composition, inferred image counts and fitted response-time figures.

Captured history is included by default because Claude Code can prune transcripts.
Do not claim complete account or calendar usage. Do not clear history to repair the index.

The local page may name the account from allow-listed email or organization metadata.
Public output and shares omit that identity. History retains only account plan fields
and the parsed subscription creation date.

Historical plan labels use captured account observations. The script supplies a plan
only when its observation rule and subscription creation guard pass; otherwise it uses
null. Do not invent plan labels or dated declarations.

A plan change that leaves subscriptionCreatedAt unchanged is not detectable until a
later run observes the new tier. A window that ended before that run can therefore
carry the old plan. Whether that date changes on upgrade or downgrade is unverified.

Never open `.credentials.json`, other credentials files, external tool-result files,
or image URLs. Never inspect or repeat prompt, output, thinking or tool content.
Do not fetch prices or install packages.

For public sharing, use the separate token-share skill only when the user explicitly
asks to share, publish, update or delete their public usage. Running this report does
not authorize sharing.
