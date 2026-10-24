"""Invented cc-api credentials and an offline, disposable Claude CLI stand-in."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from typing import Any, Dict

ENV_KEY = "sk-ant-test-env-cc-api-20261024"
FILE_KEY = "sk-ant-test-file-cc-api-20261024"
STALE_KEY = "sk-ant-test-stale-cc-api-20261024"
TASK = "Read README.md and summarize it in five bullets."
MODEL = "claude-opus-5-5"
RESULT = "Done from fake Claude."
UNICODE_RESULT = "\u2713 \u4e2d\u6587 \u2014 done"
PARENT_STDIN = b"parent-stdin-must-not-reach-claude\n"

REMOVALS = {
    "ANTHROPIC_AUTH_TOKEN": ("fixture-auth", "competing auth token removed"),
    "ANTHROPIC_PROFILE": ("fixture-profile", "competing profile removed"),
    "ANTHROPIC_BASE_URL": ("https://invalid.example", "inherited API endpoint removed"),
    "CLAUDE_CODE_OAUTH_TOKEN": ("fixture-oauth", "competing OAuth token removed"),
    "ANTHROPIC_FEDERATION_RULE_ID": ("fixture-rule", "WIF federation rule removed"),
    "ANTHROPIC_ORGANIZATION_ID": ("fixture-org", "WIF organization removed"),
    "ANTHROPIC_SERVICE_ACCOUNT_ID": ("fixture-service", "WIF service account removed"),
    "ANTHROPIC_IDENTITY_TOKEN_FILE": ("fixture-token-file", "WIF token file removed"),
    "ANTHROPIC_IDENTITY_TOKEN": ("fixture-identity", "WIF identity token removed"),
    "ANTHROPIC_WORKSPACE_ID": ("fixture-workspace", "WIF workspace removed"),
    "CLAUDE_CODE_USE_BEDROCK": ("1", "Bedrock selector removed"),
    "CLAUDE_CODE_USE_VERTEX": ("1", "Vertex selector removed"),
    "CLAUDE_CODE_USE_FOUNDRY": ("1", "Foundry selector removed"),
    "CLAUDE_CODE_USE_FUTURE_PROVIDER": ("1", "future provider selector removed"),
    "CC_API_KEY": (ENV_KEY, "source key variable removed"),
    "CC_API_KEY_FILE": ("fixture-key-file", "source key-file variable removed"),
}
KEPT = {
    "CLAUDECODE": "1",
    "CLAUDE_CODE_GIT_BASH_PATH": "C:/fixture git/bin/bash.exe",
    "CLAUDE_CODE_SESSION_SENTINEL": "fixture-parent-session",
    "ANTHROPIC_MODEL": "fixture-inherited-model",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "fixture-opus",
    "ANTHROPIC_UNRELATED_SENTINEL": "fixture-anthropic-setting",
    "CLAUDE_CODE_UNRELATED_SENTINEL": "fixture-claude-setting",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "NO_PROXY": "fixture.invalid",
    "UNRELATED_SENTINEL": "keep-me",
    "AWS_ACCESS_KEY_ID": "invented-unrelated-provider",
}


def base_env(root: Path, fake_bin: Path) -> Dict[str, str]:
    # Never copy a real credential or arbitrary parent-session configuration.
    env = {name: os.environ[name] for name in ("SystemRoot", "SYSTEMROOT", "WINDIR")
           if name in os.environ}
    env.update({name: value for name, (value, _label) in REMOVALS.items()})
    env.update(KEPT)
    env.update(PATH=str(fake_bin), PATHEXT=".COM;.EXE;.BAT;.CMD",
               HOME=str(root / "home"), USERPROFILE=str(root / "home"),
               TMP=str(root), TEMP=str(root), CLAUDE_CONFIG_DIR=str(root / "config"),
               CODEX_HOME=str(root / "codex home"), ANTHROPIC_API_KEY=STALE_KEY,
               CC_API_RECORD=str(root / "child-record.json"), CC_API_FAKE_MODE="success")
    if os.name == "nt":
        env["COMSPEC"] = str(Path(env.get("SystemRoot", env.get("SYSTEMROOT", "C:/Windows"))) / "System32/cmd.exe")
    return env


def install_fake_claude(root: Path) -> Path:
    fake_bin = root / "fake bin with spaces"
    fake_bin.mkdir(parents=True, exist_ok=True)
    fixture = fake_bin / "fake_claude.py"
    shutil.copyfile(str(Path(__file__).resolve()), str(fixture))
    if os.name == "nt":
        launcher = fake_bin / "claude.cmd"
        launcher.write_bytes(('@echo off\r\n"%s" -I -S -B "%s" %%*\r\nexit /b %%errorlevel%%\r\n'
                              % (sys.executable, fixture)).encode("utf-8"))
    else:
        launcher = fake_bin / "claude"
        launcher.write_text("#!/bin/sh\nexec %s -I -S -B %s \"$@\"\n" %
                            (shlex.quote(sys.executable), shlex.quote(str(fixture))), encoding="utf-8")
        launcher.chmod(0o755)
    return fake_bin


def read_record(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def success_result() -> Dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False,
            "result": RESULT, "total_cost_usd": 0.2705, "num_turns": 3,
            "modelUsage": {MODEL: {"costUSD": 0.2705}}}


def fake_main() -> int:
    record = {"argv": sys.argv[1:], "env": dict(os.environ),
              "cwd": str(Path.cwd().resolve()), "stdin_hex": sys.stdin.buffer.read().hex()}
    Path(os.environ["CC_API_RECORD"]).write_text(json.dumps(record), encoding="utf-8")
    mode = os.environ.get("CC_API_FAKE_MODE", "success")
    result, stderr, code = success_result(), "", 0
    raw = None
    if mode == "unicode-result":
        result["result"] = UNICODE_RESULT
        stderr = UNICODE_RESULT + "\n"
    elif mode == "nonzero-success-json":
        stderr, code = "synthetic child failure", 7
    elif mode in ("error-json", "error-json-zero", "echo-key-error", "credit-json"):
        result.update(subtype="error_during_execution", is_error=True, result="synthetic API failure")
        code = 0 if mode == "error-json-zero" else 1 if mode == "credit-json" else 7
        if mode == "credit-json":
            result["result"] = "Credit balance is too low"
        if mode == "echo-key-error":
            result["result"] += " " + os.environ["ANTHROPIC_API_KEY"]
            stderr = "stderr exposed: " + os.environ["ANTHROPIC_API_KEY"] + "\n"
    elif mode == "error-text":
        raw, stderr, code = "synthetic plain failure\n", "synthetic stderr failure\n", 9
    elif mode == "credit-text":
        raw, stderr, code = "", "Credit balance is too low\n", 1
    elif mode == "silent-error":
        raw, code = "", 6
    elif mode == "bad-json":
        raw = "not-json\n"
    elif mode == "bad-shape":
        raw = "[]\n"
    elif mode == "empty-result":
        result["result"] = ""
    elif mode == "missing-metadata":
        for name in ("total_cost_usd", "num_turns", "modelUsage"):
            result.pop(name)
    elif mode == "multi-model":
        result["modelUsage"]["claude-haiku-5-5"] = {}
    elif mode == "top-model":
        result.pop("modelUsage")
        result["model"] = "fixture-top-model"
    elif mode == "echo-key-success":
        result["result"] = "exposed: " + os.environ["ANTHROPIC_API_KEY"]
        stderr = "stderr exposed: " + os.environ["ANTHROPIC_API_KEY"] + "\n"
    elif mode == "echo-key-invalid-json":
        raw = "not-json exposed: " + os.environ["ANTHROPIC_API_KEY"]
    sys.stdout.buffer.write((json.dumps(result) if raw is None else raw).encode("utf-8"))
    sys.stderr.buffer.write(stderr.encode("utf-8"))
    return code


if __name__ == "__main__":
    sys.exit(fake_main())
