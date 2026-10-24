---
name: cc-api
description: Run a user-requested task through a separate Claude Code print-mode process using the user's Anthropic API key and Console organization, so the run can draw on monthly API credits from a Max plan.
user-invocable: true
argument-hint: "[task, optional model, and whether edits are allowed]"
---

Run the requested task with the user's own Claude Code CLI, using this skill's
stdlib Python helper. The child runs in the current working directory. The
calling Claude Code session keeps its own authentication and plan.

Use Python 3.8 or newer and an installed `claude` on PATH.

On Windows:

```powershell
python "${CLAUDE_SKILL_DIR}/scripts/cc_api.py" -- "TASK"
```

On macOS or Linux:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/cc_api.py" -- "TASK"
```

Claude Code substitutes ${CLAUDE_SKILL_DIR} with this installed skill's
directory before execution. It is not a shell environment variable. Keep
the quoted script path exactly as shown and keep the current working directory.

Replace TASK with the user's task as one argument, using the active shell's
literal-argument quoting. Do not interpolate the task as shell code. Never
put an API key in the command, task or model argument.

Add `--model ID` before `--` only when the user specifies a model. Add
`--allow-edits` before `--` when the requested task authorizes file editing.
It maps only to `--permission-mode acceptEdits`. Other tool permissions
remain Claude Code's responsibility. Do not add approval-bypass flags or
retry a denied operation with broader permissions.

The helper reads a nonempty CC_API_KEY, otherwise the UTF-8 file named by
CC_API_KEY_FILE, and strips surrounding whitespace. It passes the selected
key only through the child's ANTHROPIC_API_KEY environment variable. It
removes inherited credential and provider-selection variables from that
child and leaves this calling session untouched.

Never ask the user to paste a key into chat, read the key yourself, print it,
or supply it in a shell command. If configuration is missing, relay the
helper's private-setup instructions.

Launch the command through the Bash tool with run_in_background: true,
including for tasks that appear short. Follow the returned task identifier
and output file through the available task-output mechanism until completion.
A tool wait ending does not establish that the task finished. Do not start a
second copy or interrupt the child merely because an initial wait ended.

The helper has no execution timeout. It sends the UTF-8 task through the
child's stdin, then EOF, keeping the task out of the child's arguments.
Native launchers and Windows .cmd shims support multiline tasks. Model IDs
must match `^[A-Za-z0-9._:\[\]-]{1,100}$`.

Billing uses the API key's Claude Console organization. Per Anthropic's monthly API credits article (read October 24, 2026), self-run claude -p usage can draw on monthly API credits claimed into that organization by a Max or Team plan.
The organization and Anthropic's eligibility rules determine whether they
apply. Normal API rates apply when those credits do not cover the run;
purchased credits, auto-reload or invoicing may pay for it. Interactive
Claude Code and extra plan usage are not covered by these monthly API
credits. Anthropic also excludes certain app, IDE and GitHub Action launches.
This helper cannot verify eligibility or the remaining balance. Check the
Console Billing page.

The maintainer's 2026-10-24 test demonstrated promotional-credit consumption
by an API-key-authenticated claude -p child started inside interactive Claude
Code. That observation does not guarantee eligibility for every launch.

After completion, report the task result, child-reported total_cost_usd,
turns and model, and preserve the reminder to check Console Billing.
Do not describe total_cost_usd as the amount deducted from promotional credit.

On failure, relay the redacted child error and the helper's explanation.
"Credit balance is too low" means usable API credit did not cover that run;
check the key's organization and Billing page. Do not switch authentication,
change login, launch interactive Claude Code or retry automatically.
