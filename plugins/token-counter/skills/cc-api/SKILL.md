---
name: cc-api
description: Run a user-requested task through Claude Code with the user's Anthropic API key, including delegation from Codex. Use when the user asks for cc-api or asks to run a task through Claude Code using their API key or Console API credits.
---

Run the requested task with the user's own Claude Code CLI, using this skill's
stdlib Python helper. The child runs in the current working directory. The
calling Codex session keeps its own authentication and plan.

Use Python 3.8 or newer and an installed `claude` on PATH.

Resolve `<skill-dir>` to the absolute directory containing this SKILL.md, using
the skill path supplied to you. This is a documentation placeholder, not an
environment variable or Codex substitution. Keep the script path quoted and
keep the current working directory.

On Windows:

```powershell
python "<skill-dir>/scripts/cc_api.py" -- "TASK"
```

On macOS or Linux:

```bash
python3 "<skill-dir>/scripts/cc_api.py" -- "TASK"
```

Replace TASK with the user's task as one argument, using the active shell's
literal-argument quoting. Do not interpolate the task as shell code. Never put
an API key in the command, task or model argument.

Add `--model ID` before `--` only when the user specifies a model. Add
`--allow-edits` before `--` when the requested task authorizes file editing.
It maps only to Claude Code's `--permission-mode acceptEdits`. Other tool
permissions remain Claude Code's responsibility. Do not add approval-bypass
flags or retry a denied operation with broader permissions.

The helper reads a nonempty CC_API_KEY, otherwise the UTF-8 file named by
CC_API_KEY_FILE, and strips surrounding whitespace. It passes the selected
key only through the child's ANTHROPIC_API_KEY environment variable. It
removes inherited credential and provider-selection variables from that
child and leaves the calling session untouched.

Never ask the user to paste a key into chat, read the key yourself, print it,
or supply it in a shell command. If configuration is missing, relay the
helper's private-setup instructions.

Start the command with Codex's continuing-process shell mechanism so a long
task survives the initial tool wait. With exec_command, use a short
yield_time_ms, such as 1000 on POSIX or 10000 on Windows. If it returns a
session_id, continue polling that same session with write_stdin and empty
chars until it exits. A yielded process is still running. Do not start a
second copy or send Ctrl-C merely because the initial wait ended.

If the shell tool cannot keep a long command running, tell the user the task may be cut off and offer to run it from their own terminal with the same command.

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

After completion, report the task result, child-reported total_cost_usd,
turns and model, and preserve the reminder to check Console Billing.
Do not describe total_cost_usd as the amount deducted from promotional credit.

On failure, relay the redacted child error and the helper's explanation.
"Credit balance is too low" means usable API credit did not cover that run;
check the key's organization and Billing page. Do not switch authentication,
change login, launch interactive Claude Code or retry automatically.
