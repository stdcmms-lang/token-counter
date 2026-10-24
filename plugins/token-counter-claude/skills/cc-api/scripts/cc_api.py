#!/usr/bin/env python3
"""Run one Claude Code print-mode task with a privately configured API key."""
import argparse
import decimal
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence


HELP = """Usage: cc-api [--model ID] [--allow-edits] [--] TASK

Run one task with your Claude Code CLI and Anthropic API key.

Options:
  --model ID       Pass ID to claude.
  --allow-edits    Pass --permission-mode acceptEdits.
  -h, --help       Show this help.

Reads CC_API_KEY, otherwise CC_API_KEY_FILE.
Never put the key in TASK or paste it into chat.
"""
INVALID_ARGUMENTS = "cc-api: invalid arguments. Use --model ID and/or --allow-edits before one quoted TASK; use --help for usage."
MISSING_KEY = "cc-api: no API key is configured. Set CC_API_KEY privately in your shell profile on macOS/Linux, or with setx or your PowerShell profile on Windows, then restart the calling application. Alternatively set CC_API_KEY_FILE to a UTF-8 key file. Never paste the key into chat."
KEY_FILE_ERROR = "cc-api: cannot read CC_API_KEY_FILE. Check the file path, permissions and UTF-8 encoding."
KEY_FORMAT_ERROR = "cc-api: the API key must be a single nonempty line."
SECRET_ARGUMENT = "cc-api: refusing to put the API key in child arguments."
MISSING_LAUNCHER = "cc-api: claude was not found on PATH. Install Claude Code and make claude, claude.exe or claude.cmd available on PATH."
START_ERROR = "cc-api: could not start claude. Check that the PATH launcher is executable and retry."
PROTOCOL_ERROR = "cc-api: claude did not return a JSON result object with a text result (--output-format json)."
JSON_ERROR = "cc-api: claude reported an error (child exit 0)."
NO_ERROR_TEXT = "cc-api: claude supplied no error text."
CREDIT_EXPLANATION = 'cc-api: "Credit balance is too low" means Anthropic rejected the request for lack of usable credit for this API run. Check the key\'s Console organization and Billing page; monthly credits may be absent, expired or inapplicable.'
INTERRUPTED = "cc-api: interrupted. The child may have incurred API usage; check Console Billing before retrying."
UNEXPECTED_ERROR = "cc-api: unexpected runner failure; no diagnostic values were printed."
BILLING_REMINDER = "cc-api: Anthropic decides whether API credits apply; check Console Billing: https://platform.claude.com/settings/billing"


class CcApiError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CcApiError(2, INVALID_ARGUMENTS)

    def print_help(self, file=None) -> None:
        (file or sys.stdout).write(HELP)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = _Parser(allow_abbrev=False)
    parser.add_argument("--model")
    parser.add_argument("--allow-edits", action="store_true")
    parser.add_argument("task")
    args = parser.parse_args(argv)
    if not args.task.strip() or "\0" in args.task:
        parser.error("")
    if args.model is not None and re.fullmatch(r"[A-Za-z0-9._:\[\]-]{1,100}", args.model) is None:
        parser.error("")
    return args


def load_key(environ: Mapping[str, str]) -> str:
    key = environ.get("CC_API_KEY", "").strip()
    if not key:
        path = environ.get("CC_API_KEY_FILE", "").strip()
        if path:
            try:
                with open(path, encoding="utf-8-sig") as source:
                    key = source.read().strip()
            except (OSError, UnicodeError, ValueError):
                raise CcApiError(2, KEY_FILE_ERROR) from None
    if not key:
        raise CcApiError(2, MISSING_KEY)
    if any(char in key for char in "\0\r\n"):
        raise CcApiError(2, KEY_FORMAT_ERROR)
    return key


def child_env(environ: Mapping[str, str], key: str) -> Dict[str, str]:
    removed = {
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_PROFILE", "CC_API_KEY", "CC_API_KEY_FILE",
        # Source: Anthropic SDK documentation, Workload Identity Federation auto-detection
        # (WIF) credential and organization variables; not the whole namespace.
        "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID", "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_WORKSPACE_ID",
    }
    child = {name: value for name, value in environ.items()
             if name.upper() not in removed and not name.upper().startswith("CLAUDE_CODE_USE_")}
    child["ANTHROPIC_API_KEY"] = key
    return child


def find_claude(environ: Mapping[str, str], *, windows: bool) -> str:
    names = ("claude.exe", "claude.cmd", "claude") if windows else ("claude",)
    for directory in os.get_exec_path(environ):
        if not directory:
            continue
        for name in names:
            candidate = os.path.abspath(os.path.join(directory, name))
            if os.path.isfile(candidate) and (windows or os.access(candidate, os.X_OK)):
                return candidate
    raise CcApiError(3, MISSING_LAUNCHER)


def build_argv(launcher: str, args: argparse.Namespace) -> List[str]:
    argv = [launcher, "-p", "--output-format", "json"]
    if args.model is not None:
        argv += ["--model", args.model]
    if args.allow_edits:
        argv += ["--permission-mode", "acceptEdits"]
    return argv


def run_child(
    argv: Sequence[str], environ: Mapping[str, str], task_bytes: bytes
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(argv), input=task_bytes, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=environ, cwd=None,
                              shell=False, check=False, timeout=None)
    except OSError:
        raise CcApiError(3, START_ERROR) from None


def parse_result(stdout: bytes) -> Optional[Dict[str, Any]]:
    def invalid_constant(value):
        raise ValueError("non-JSON constant")
    try:
        value = json.loads(stdout, parse_float=decimal.Decimal, parse_constant=invalid_constant)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) and value.get("type") == "result" else None


def model_label(result: Mapping[str, Any], requested_model: Optional[str]) -> str:
    usage = result.get("modelUsage")
    models = sorted(k for k in usage if isinstance(k, str) and k) if isinstance(usage, dict) else []
    model = result.get("model")
    label = (",".join(models) if models else model if isinstance(model, str) and model
             else requested_model or "unknown")
    return label.replace("\r", "\\r").replace("\n", "\\n")


def cost_label(result: Mapping[str, Any]) -> str:
    cost = result.get("total_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, decimal.Decimal)):
        return "unknown"
    if isinstance(cost, decimal.Decimal) and not cost.is_finite():
        return "unknown"
    if cost < 0:
        return "unknown"
    if isinstance(cost, int):
        return str(cost)
    if cost == 0:
        return "0"
    label = format(cost, "f")
    return label.rstrip("0").rstrip(".") if "." in label else label


def turns_label(result: Mapping[str, Any]) -> str:
    turns = result.get("num_turns")
    return str(turns) if isinstance(turns, int) and not isinstance(turns, bool) and turns >= 0 else "unknown"


def redact(text: str, key: str) -> str:
    spellings = {key, json.dumps(key, ensure_ascii=True)[1:-1], json.dumps(key, ensure_ascii=False)[1:-1]}
    for spelling in sorted(spellings, key=len, reverse=True):
        if spelling:
            text = text.replace(spelling, "[REDACTED]")
    return text


def _write_line(text: str, stream) -> None:
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")


def emit_outcome(
    completed: subprocess.CompletedProcess,
    requested_model: Optional[str],
    key: str,
) -> int:
    result = parse_result(completed.stdout)
    stderr = completed.stderr.decode("utf-8", errors="replace")
    reported_error = result is not None and (result.get("is_error") is True or
                                             ("subtype" in result and result["subtype"] != "success"))
    if completed.returncode != 0:
        code, heading = 4, "cc-api: claude failed (child exit %d)." % completed.returncode
    elif reported_error:
        code, heading = 4, JSON_ERROR
    elif result is None or not isinstance(result.get("result"), str):
        code, heading = 5, PROTOCOL_ERROR
    else:
        _write_line(redact(result["result"], key), sys.stdout)
        summary = "cc-api: total_cost_usd=%s; turns=%s; model=%s" % (
            cost_label(result), turns_label(result), model_label(result, requested_model))
        _write_line(redact(summary, key), sys.stdout)
        _write_line(BILLING_REMINDER, sys.stdout)
        if stderr:
            _write_line(redact(stderr, key), sys.stderr)
        return 0

    _write_line(heading, sys.stderr)
    texts = []
    if result is not None:
        message = result.get("result")
        if isinstance(message, str) and message:
            texts.append(message)
        errors = result.get("errors")
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, str) and error and error not in texts:
                    texts.append(error)
        for message in texts:
            _write_line(redact(message, key), sys.stderr)
    elif completed.stdout:
        _write_line("cc-api: child stdout:", sys.stderr)
        texts.append(completed.stdout.decode("utf-8", errors="replace"))
        _write_line(redact(texts[-1], key), sys.stderr)
    if stderr:
        _write_line("cc-api: child stderr:", sys.stderr)
        _write_line(redact(stderr, key), sys.stderr)
        texts.append(stderr)
    if not texts:
        _write_line(NO_ERROR_TEXT, sys.stderr)
    if any("credit balance is too low" in message.lower() for message in texts):
        _write_line(CREDIT_EXPLANATION, sys.stderr)
    _write_line(BILLING_REMINDER, sys.stderr)
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        args = parse_args(argv)
        key = load_key(os.environ)
        environ = child_env(os.environ, key)
        launcher = find_claude(environ, windows=os.name == "nt")
        command = build_argv(launcher, args)
        if any(key in argument for argument in command):
            raise CcApiError(2, SECRET_ARGUMENT)
        return emit_outcome(run_child(command, environ, args.task.encode("utf-8")), args.model, key)
    except SystemExit as exc:
        return exc.code
    except CcApiError as exc:
        _write_line(exc.message, sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        _write_line(INTERRUPTED, sys.stderr)
        _write_line(BILLING_REMINDER, sys.stderr)
        return 130
    except Exception:
        _write_line(UNEXPECTED_ERROR, sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
