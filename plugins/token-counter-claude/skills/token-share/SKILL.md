---
name: token-share
description: Explicitly share, update, or delete Claude Code usage and its sanitized report page on tokenusage.dev.
user-invocable: true
argument-hint: "[share with handle, update, delete report, or delete all]"
---

Use this skill only for an explicit request to share, publish, update or delete
Claude Code usage on tokenusage.dev.

Run a dry run first. On Windows:

```powershell
python "${CLAUDE_SKILL_DIR}/scripts/share.py" --handle HANDLE
```

On macOS or Linux:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/share.py" --handle HANDLE
```

Replace HANDLE only with the user's requested public handle. For an existing sharer,
omit `--handle` to use the saved handle. Do not invent a handle.

The dry run sends nothing. It captures local history and account plan observations
when the account file is read, writes the sanitized preview unless `--no-report` is
used, and states which complete captured months and quota windows can safely replace
stored data. Read its quality warnings and withholding reasons. Link the public
preview when available.

Read the count of shared responses with no recorded speed. Those responses remain
in totals but sit outside tiers and window splits; do not describe them as measured
Standard responses. Read Fast, US-residency and long-prompt pricing diagnostics too.

When the user's request authorizes the concrete share and the dry run is valid,
run the same command with `--yes`. An explicit request to share or update authorizes
that send; do not ask for the same permission again. A request only to preview or
inspect does not authorize `--yes`.

By default the send stores counts and uploads the exact previewed report page.
Use `--no-report` only if the user asks to share counts without a page.
Use `--style clinical`, `--style matisse`, or `--style nocturne` when requested.

For deletion, first run the requested action without `--yes`:

```powershell
python "${CLAUDE_SKILL_DIR}/scripts/share.py" --delete-report
python "${CLAUDE_SKILL_DIR}/scripts/share.py" --delete
```

On macOS/Linux use `python3` instead of `python`. Add `--yes` only for the deletion
the user explicitly requested. `--delete-report` keeps stored counts; `--delete`
removes the sharer's stored data and report.

Sharing sends schema 1 with client name `claude-usage`. It sends daily recorded
counts, selected hashed-session summaries, safely retained quota windows and recent
timing summaries. It never sends prompts, outputs, thinking, tool data, paths,
working directories, titles, raw session IDs, account identity, UTC-hour buckets
or weekday buckets.

The Claude share token is separate from the Codex plugin's token. Never copy,
import, print or reuse a Codex token. Never open `.credentials.json` or another
credentials file. `--api` uses TOKENUSAGE_API when set; tokens remain specific to
their saved endpoint.

Do not use date, session or live-only filters to share a partial replacement month.
Do not delete local capture history or bypass a coverage refusal. If history cannot
reproduce previously submitted evidence, report the withheld section.

If the saved token has no matching capture history, replacement is disabled.
Explain the recovery path: the retained token can still delete the old sharer,
then a fresh first share can be made with the user's handle.

On Windows, the recovery commands are:

```powershell
python "${CLAUDE_SKILL_DIR}/scripts/share.py" --delete --yes
python "${CLAUDE_SKILL_DIR}/scripts/share.py" --handle HANDLE --yes
```

On macOS/Linux use `python3`. Run deletion only when the user has authorized deleting
the old public data. Then dry-run the fresh first share before its `--yes` command.
Recovery preserves local capture history; it does not bypass the old binding.

Historical plan labels use the script's captured account-observation rule and
subscription creation guard. Unknown or conflicting evidence produces null.
Do not supply dated plan declarations or invent historical plans.

A plan change that leaves subscriptionCreatedAt unchanged is not detectable until a
later run observes the new tier. A window that ended before that run can therefore
carry the old plan. Whether that date changes on upgrade or downgrade is unverified.

After a successful send, return the public profile/report links and any material
withheld-data note. If counts succeeded but the page failed, say exactly that.
If the network outcome is unknown, report it without claiming success or retrying
automatically.
