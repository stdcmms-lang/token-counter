"""Offline cc-api contracts, with invented keys and a fake PATH-only launcher."""
import builtins
import contextlib
from decimal import Decimal
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts/fixtures"))
import cc_api_cases as cases

RESULTS = []
PLUGINS = ("token-counter", "token-counter-claude")
BILLING = "cc-api: Anthropic decides whether API credits apply; check Console Billing: https://platform.claude.com/settings/billing\n"
INVALID = "cc-api: invalid arguments. Use --model ID and/or --allow-edits before one quoted TASK; use --help for usage.\n"
MISSING = "cc-api: no API key is configured. Set CC_API_KEY privately in your shell profile on macOS/Linux, or with setx or your PowerShell profile on Windows, then restart the calling application. Alternatively set CC_API_KEY_FILE to a UTF-8 key file. Never paste the key into chat.\n"
FILE_ERROR = "cc-api: cannot read CC_API_KEY_FILE. Check the file path, permissions and UTF-8 encoding.\n"
PROTOCOL = "cc-api: claude did not return a JSON result object with a text result (--output-format json).\n"
CREDIT = 'cc-api: "Credit balance is too low" means Anthropic rejected the request for lack of usable credit for this API run. Check the key\'s Console organization and Billing page; monthly credits may be absent, expired or inapplicable.\n'
SUCCESS = cases.RESULT + "\ncc-api: total_cost_usd=0.2705; turns=3; model=claude-opus-5-5\n" + BILLING
HELP = """Usage: cc-api [--model ID] [--allow-edits] [--] TASK

Run one task with your Claude Code CLI and Anthropic API key.

Options:
  --model ID       Pass ID to claude.
  --allow-edits    Pass --permission-mode acceptEdits.
  -h, --help       Show this help.

Reads CC_API_KEY, otherwise CC_API_KEY_FILE.
Never put the key in TASK or paste it into chat.
"""


def expect(name, got, expected=True):
    # Assertion logs intentionally contain no dynamic output or credential values.
    RESULTS.append((name, got == expected, ""))


def load_module(plugin):
    path = REPO / "plugins" / plugin / "skills/cc-api/scripts/cc_api.py"
    spec = importlib.util.spec_from_file_location("cc_api_" + plugin.replace("-", "_"), str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def sandbox():
    with tempfile.TemporaryDirectory(prefix="cc api contract ") as temporary:
        root = Path(temporary).resolve()
        fake_bin = cases.install_fake_claude(root)
        task_dir = root / "task directory"
        task_dir.mkdir()
        for path in (root / "config/login-marker", root / "codex home/config-marker", task_dir / "README.md"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic unchanged marker\n", encoding="utf-8")
        yield root, task_dir, cases.base_env(root, fake_bin)


def invoke(module, env, task_dir, argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    try:
        os.chdir(str(task_dir))
        with mock.patch.object(module.os, "environ", env), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
    finally:
        os.chdir(previous)
    return code, stdout.getvalue(), stderr.getvalue()


def fake(module, mode="success", options=(), task=cases.TASK, updates=None):
    with sandbox() as (root, task_dir, env):
        env["CC_API_FAKE_MODE"] = mode
        env.update(updates or {})
        result = invoke(module, env, task_dir, list(options) + ["--", task])
        path = Path(env["CC_API_RECORD"])
        record = cases.read_record(path) if path.exists() else None
        return result, record, str(task_dir)


def completed(value=None, code=0, stderr=b""):
    return subprocess.CompletedProcess([], code, json.dumps(cases.success_result() if value is None else value).encode("utf-8"), stderr)


def outcome(module, value=None, code=0, stderr=b"", raw=None, model=None, key=cases.ENV_KEY):
    stdout, errors = io.StringIO(), io.StringIO()
    child = completed(value, code, stderr)
    if raw is not None:
        child.stdout = raw
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(errors):
        rc = module.emit_outcome(child, model, key)
    return rc, stdout.getvalue(), errors.getvalue()


def test_default_run(module):
    result, record, _cwd = fake(module)
    expect("default argv exact", record["argv"], ["-p", "--output-format", "json"])
    expect("task_in_argv absent by default", cases.TASK not in record["argv"])
    expect("default child task stdin exact", record["stdin_hex"], cases.TASK.encode("utf-8").hex())
    expect("default run succeeds", result[0], 0)


def test_options(module):
    result, record, _cwd = fake(module, options=("--model", cases.MODEL, "--allow-edits"))
    expected = ["-p", "--output-format", "json", "--model", cases.MODEL, "--permission-mode", "acceptEdits"]
    expect("model argv exact", record["argv"], expected)
    expect("acceptEdits argv exact", record["argv"], expected)
    expect("task_in_argv absent with options", cases.TASK not in record["argv"])
    expect("options succeed", result[0], 0)
    args = module.parse_args(["--model", "first", "--model", cases.MODEL + "[1m]", "--allow-edits", "--allow-edits", "--", "-task"])
    expect("ordinary repeated option semantics", (args.model, args.allow_edits, args.task), (cases.MODEL + "[1m]", True, "-task"))


def test_default_permissions(module):
    args = module.build_argv("launcher", module.parse_args([cases.TASK]))
    expect("default grants no extra permissions", args, ["launcher", "-p", "--output-format", "json"])


def test_cwd(module):
    _result, record, cwd = fake(module)
    expect("caller cwd preserved", record["cwd"], cwd)


def test_key_delivery(module):
    _result, record, _cwd = fake(module, updates={"CC_API_KEY": " \t" + cases.ENV_KEY + "\r\n"})
    expect("selected key reaches ANTHROPIC_API_KEY", record["env"].get("ANTHROPIC_API_KEY"), cases.ENV_KEY)


def test_key_locations(module):
    _result, record, _cwd = fake(module)
    others = record["argv"] + [v for k, v in record["env"].items() if k != "ANTHROPIC_API_KEY"]
    expect("key occurs only in child API-key slot", not any(cases.ENV_KEY in v for v in others))


def test_parent_unchanged(module):
    with sandbox() as (_root, task_dir, env):
        before = dict(env)
        invoke(module, env, task_dir, [cases.TASK])
        expect("parent environment unchanged", env, before)


def test_environment_removal(module):
    for mixed in (False, True):
        with sandbox() as (_root, task_dir, env):
            if mixed:
                for name in list(env):
                    if name in cases.REMOVALS or name == "ANTHROPIC_API_KEY":
                        # Key loading is uppercase by contract; child filtering is not.
                        if name not in ("CC_API_KEY", "CC_API_KEY_FILE"):
                            env[name.swapcase()] = env.pop(name)
                env["cc_api_key"] = "fixture-mixed-source"
                env["cC_aPi_KeY_fIlE"] = "fixture-mixed-file"
            invoke(module, env, task_dir, [cases.TASK])
            received = cases.read_record(Path(env["CC_API_RECORD"]))["env"]
            upper = {name.upper(): value for name, value in received.items()}
            expect("incoming API key replaced", upper.get("ANTHROPIC_API_KEY"), cases.ENV_KEY)
            expect("exactly one child API key slot", sum(k.upper() == "ANTHROPIC_API_KEY" for k in received), 1)
            for name, (_value, label) in cases.REMOVALS.items():
                expect(label, name not in upper)


def test_kept_environment(module):
    _result, record, _cwd = fake(module)
    expect("unrelated environment preserved", {k: record["env"].get(k) for k in cases.KEPT}, cases.KEPT)
    expect("Git Bash path reaches child unchanged", record["env"].get("CLAUDE_CODE_GIT_BASH_PATH"), cases.KEPT["CLAUDE_CODE_GIT_BASH_PATH"])
    expect("Anthropic model reaches child unchanged", record["env"].get("ANTHROPIC_MODEL"), cases.KEPT["ANTHROPIC_MODEL"])
    expect("parent session settings retained", record["env"].get("CLAUDECODE"), "1")
    root = Path(_cwd).parent
    expect("Claude config directory preserved", record["env"].get("CLAUDE_CONFIG_DIR"), str(root / "config"))
    expect("Codex home preserved", record["env"].get("CODEX_HOME"), str(root / "codex home"))


def test_file_key(module):
    with sandbox() as (root, task_dir, env):
        key_file = root / "private synthetic key"
        key_file.write_text(" \r\n" + cases.FILE_KEY + "\t\n", encoding="utf-8-sig")
        env.pop("CC_API_KEY")
        env["CC_API_KEY_FILE"] = " \t" + str(key_file) + "\n"
        result = invoke(module, env, task_dir, [cases.TASK])
        path = Path(env["CC_API_RECORD"])
        record = cases.read_record(path) if path.exists() else {"env": {}}
        expect("file key selected and stripped", (result[0], record["env"].get("ANTHROPIC_API_KEY")), (0, cases.FILE_KEY))


def test_env_precedence(module):
    with sandbox() as (root, task_dir, env):
        env["CC_API_KEY"] = " \t" + cases.ENV_KEY + "\n"
        env["CC_API_KEY_FILE"] = str(root / "does not exist")
        # A fake read makes an erroneous attempted read observable without turning
        # the mutant into an OSError; the named assertion still requires zero reads.
        with mock.patch.object(builtins, "open", mock.mock_open(read_data=cases.FILE_KEY)) as reads:
            result = invoke(module, env, task_dir, [cases.TASK])
        record = cases.read_record(Path(env["CC_API_RECORD"]))
        expect("environment key wins without file read", (result[0], record["env"].get("ANTHROPIC_API_KEY"), reads.call_count), (0, cases.ENV_KEY, 0))


def test_empty_env_falls_back(module):
    with mock.patch.object(builtins, "open", mock.mock_open(read_data=cases.FILE_KEY)):
        expect("blank environment key falls back", module.load_key({"CC_API_KEY": " \t", "CC_API_KEY_FILE": "fixture"}), cases.FILE_KEY)


def test_missing_key(module):
    for updates in ({"CC_API_KEY": "", "CC_API_KEY_FILE": ""}, {"CC_API_KEY": " \n", "CC_API_KEY_FILE": " \t"}):
        result, record, _cwd = fake(module, updates=updates)
        expect("missing key exit and guidance", result, (2, "", MISSING))
        expect("missing key starts no child", record, None)


def test_bad_key_file(module):
    with sandbox() as (root, task_dir, env):
        for path in (root / "nonexistent", root / "invalid utf8"):
            if path.name == "invalid utf8":
                path.write_bytes(b"\xff\xfe\x80")
            env.update(CC_API_KEY="", CC_API_KEY_FILE=str(path))
            expect("unreadable key file exit and message", invoke(module, env, task_dir, [cases.TASK]), (2, "", FILE_ERROR))


def test_empty_key_file(module):
    with sandbox() as (root, task_dir, env):
        path = root / "blank key"
        path.write_text(" \r\n\t", encoding="utf-8-sig")
        env.update(CC_API_KEY="", CC_API_KEY_FILE=str(path))
        expect("empty key file uses missing guidance", invoke(module, env, task_dir, [cases.TASK]), (2, "", MISSING))


def test_key_format(module):
    for key in ("fixture\nprivate", "fixture\rprivate", "fixture\0private"):
        result, record, _cwd = fake(module, updates={"CC_API_KEY": key})
        expect("multiline key rejected privately", result, (2, "", "cc-api: the API key must be a single nonempty line.\n"))
        expect("invalid key starts no child", record, None)


def test_missing_claude(module):
    result, record, _cwd = fake(module, updates={"PATH": ""})
    expect("missing launcher exit and message", result, (3, "", "cc-api: claude was not found on PATH. Install Claude Code and make claude, claude.exe or claude.cmd available on PATH.\n"))
    expect("missing launcher creates no record", record, None)


def test_start_failure(module):
    with mock.patch.object(module.subprocess, "run", side_effect=OSError("private exception")):
        result, record, _cwd = fake(module)
    expect("start failure exit and message", result, (3, "", "cc-api: could not start claude. Check that the PATH launcher is executable and retry.\n"))
    expect("start failure private", record, None)


def test_task_stdin(module):
    with sandbox() as (_root, task_dir, env):
        task = ' \nRead "two words" & preserve ^ | < > %CC_API_TEST_EXPAND% !literal!\r\nUnicode: 你好 🐍\ntrailing\\ '
        env["CC_API_TEST_EXPAND"] = "SHOULD-NOT-EXPAND"
        child = subprocess.run([sys.executable, "-I", "-S", "-B", module.__file__, "--model", cases.MODEL + "[1m]", "--", task],
                               input=cases.PARENT_STDIN, env=env, cwd=str(task_dir),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        record = cases.read_record(Path(env["CC_API_RECORD"]))
        expect("child receives task then EOF", record["stdin_hex"], task.encode("utf-8").hex())
        expect("parent stdin never reaches child", cases.PARENT_STDIN.hex() not in record["stdin_hex"])
        expect("task_in_argv absent for multiline metacharacters", record["argv"], ["-p", "--output-format", "json", "--model", cases.MODEL + "[1m]"])
        expect("multiline UTF-8 task succeeds", child.returncode, 0)


def test_subprocess_contract(module):
    with mock.patch.object(module.subprocess, "run", return_value=completed()) as run:
        result, _record, _cwd = fake(module)
    argv, kwargs = run.call_args[0][0], run.call_args[1]
    expect("child task bytes on stdin", kwargs.get("input"), cases.TASK.encode("utf-8"))
    expect("task_in_argv absent in subprocess", cases.TASK not in argv)
    expect("stdin is a pipe via input", "stdin" not in kwargs)
    expect("child has no timeout", kwargs.get("timeout"), None)
    expect("child uses inherited cwd", kwargs.get("cwd"), None)
    expect("child uses shell false", kwargs.get("shell"), False)
    expect("child captures bytes", (kwargs.get("stdout"), kwargs.get("stderr"), kwargs.get("text", False)), (subprocess.PIPE, subprocess.PIPE, False))
    expect("child uses list invocation", isinstance(argv, list))
    expect("child does not raise for exit", kwargs.get("check"), False)
    expect("spy run succeeds", result[0], 0)


def test_success_output(module):
    result, _record, _cwd = fake(module)
    expect("success output exact", result, (0, SUCCESS, ""))


def test_utf8_output(module):
    for encoding in (None, "cp1252"):
        with sandbox() as (_root, task_dir, env):
            env.pop("PYTHONIOENCODING", None)
            env["PYTHONUTF8"] = "0"
            label = "default pipe" if encoding is None else encoding + " pipe"
            if encoding is not None:
                # Force the Windows pre-3.15 pipe encoding on every CI platform.
                env["PYTHONIOENCODING"] = encoding
            env["CC_API_FAKE_MODE"] = "unicode-result"
            # -I ignores PYTHONIOENCODING/PYTHONUTF8, so use only -S and -B here.
            child = subprocess.run([sys.executable, "-S", "-B", module.__file__, "--", cases.TASK],
                                   input=cases.PARENT_STDIN, env=env, cwd=str(task_dir),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            expect(label + " Unicode result succeeds", child.returncode, 0)
            expect(label + " UTF-8 first line", child.stdout.decode("utf-8").splitlines()[:1], [cases.UNICODE_RESULT])
            expect(label + " UTF-8 stderr", child.stderr.decode("utf-8").splitlines(), [cases.UNICODE_RESULT])


def test_nonzero_child(module):
    result, _record, _cwd = fake(module, mode="nonzero-success-json")
    expect("nonzero child stays failure", result[0] == 4 and result[1] == "" and result[2].startswith("cc-api: claude failed (child exit 7).\n"))
    expect("failure has no success metrics", "total_cost_usd=" not in result[2])


def test_json_error_zero(module):
    result, _record, _cwd = fake(module, mode="error-json-zero")
    expect("JSON error stays failure", result, (4, "", "cc-api: claude reported an error (child exit 0).\nsynthetic API failure\n" + BILLING))


def test_plain_error(module):
    result, _record, _cwd = fake(module, mode="error-text")
    expect("plain child errors retained", result, (4, "", "cc-api: claude failed (child exit 9).\ncc-api: child stdout:\nsynthetic plain failure\ncc-api: child stderr:\nsynthetic stderr failure\n" + BILLING))
    result, _record, _cwd = fake(module, mode="error-json")
    expect("nonzero JSON error retained", result, (4, "", "cc-api: claude failed (child exit 7).\nsynthetic API failure\n" + BILLING))


def test_silent_error(module):
    result, _record, _cwd = fake(module, mode="silent-error")
    expect("silent child error explained", result, (4, "", "cc-api: claude failed (child exit 6).\ncc-api: claude supplied no error text.\n" + BILLING))


def test_credit_errors(module):
    for mode in ("credit-json", "credit-text"):
        result, _record, _cwd = fake(module, mode=mode)
        expect("credit balance error explained", result[0] == 4 and result[1] == "" and "Credit balance is too low" in result[2] and CREDIT in result[2] and result[2].endswith(BILLING))
    value = cases.success_result()
    value.update(is_error=True, result="CREDIT BALANCE IS TOO LOW")
    expect("credit phrase case insensitive", CREDIT in outcome(module, value)[2])


def test_bad_json(module):
    result, _record, _cwd = fake(module, mode="bad-json")
    expect("invalid result protocol fails", result, (5, "", PROTOCOL + "cc-api: child stdout:\nnot-json\n" + BILLING))


def test_bad_shape(module):
    result, _record, _cwd = fake(module, mode="bad-shape")
    expect("nonobject result protocol fails", result, (5, "", PROTOCOL + "cc-api: child stdout:\n[]\n" + BILLING))
    for value in ({"type": "other", "result": "text"}, {"type": "result"}, {"type": "result", "result": 1}):
        expect("wrong type or missing text fails", outcome(module, value)[0], 5)
    for raw in (b'{"type":"result","result":"ok","total_cost_usd":NaN}', b'{} {}', b'\xff'):
        expect("non-JSON protocol rejected", outcome(module, raw=raw)[0], 5)


def test_empty_result(module):
    result, _record, _cwd = fake(module, mode="empty-result")
    expect("empty final result succeeds", result, (0, SUCCESS.replace(cases.RESULT, "", 1), ""))


def test_metadata_fallbacks(module):
    for mode, options, label in (("missing-metadata", (), "total_cost_usd=unknown; turns=unknown; model=unknown"),
                                 ("missing-metadata", ("--model", "requested"), "model=requested"),
                                 ("multi-model", (), "model=claude-haiku-5-5,claude-opus-5-5"),
                                 ("top-model", (), "model=fixture-top-model")):
        result, _record, _cwd = fake(module, mode=mode, options=options)
        assertion = {"multi-model": "multiple models sorted", "top-model": "top model fallback used"}.get(mode, "model fallback uses requested ID" if options else "missing metadata is unknown")
        expect(assertion, result[0] == 0 and label in result[1])
    for value, label in ((0, "0"), (12, "12"), (9007199254740993, "9007199254740993"), (10 ** 200, "1" + "0" * 200), (Decimal("0.270500"), "0.2705"), (Decimal("1E+3"), "1000"), (Decimal("-0.00"), "0"), (True, "unknown"), (-1, "unknown"), (1.2, "unknown"), ("1", "unknown"), (Decimal("NaN"), "unknown"), (Decimal("Infinity"), "unknown")):
        expect("finite nonnegative decimal cost", module.cost_label({"total_cost_usd": value}), label)
    for value, label in ((0, "0"), (3, "3"), (True, "unknown"), (-1, "unknown"), (Decimal(1), "unknown")):
        expect("strict nonnegative integer turns", module.turns_label({"num_turns": value}), label)
    expect("model summary stays one line", module.model_label({"modelUsage": {"": {}, "a\r\nb": {}}}, None), "a\\r\\nb")
    expect("empty usage falls back to top model", module.model_label({"modelUsage": {"": {}}, "model": "top"}, "requested"), "top")
    parsed = module.parse_result(b'{"type":"result","result":"ok","total_cost_usd":0.270500}')
    expect("JSON fractional numbers are Decimal", type(parsed["total_cost_usd"]), Decimal)


def test_redaction(module):
    for mode, labels in (("echo-key-success", ("stdout key redacted", "stderr key redacted")),
                         ("echo-key-error", ("failure key redacted",)),
                         ("echo-key-invalid-json", ("invalid JSON key redacted",))):
        result, _record, _cwd = fake(module, mode=mode)
        for label in labels:
            text = result[2] if label != "stdout key redacted" else result[1]
            expect(label, cases.ENV_KEY not in text and "[REDACTED]" in text)
    key = 'fixture-秘密-"\\key'
    for ascii_only in (True, False):
        escaped = json.dumps(key, ensure_ascii=ascii_only)[1:-1]
        expect("JSON escaped key redacted", module.redact(escaped + key, key), "[REDACTED][REDACTED]")
    value = cases.success_result()
    value["modelUsage"] = {cases.ENV_KEY: {}}
    expect("model key redacted", "model=[REDACTED]" in outcome(module, value)[1])


def test_secret_arguments(module):
    result, record, _cwd = fake(module, options=("--model", "prefix-" + cases.ENV_KEY))
    expect("secret-bearing arguments refused", (result, record), ((2, "", "cc-api: refusing to put the API key in child arguments.\n"), None))


def test_cli_errors(module):
    invalid = ([], [""], [" \n\t"], ["one", "two"], ["--unknown", cases.TASK], ["--mod", "x", cases.TASK], ["-task"], ["--model"], ["--model", "a b", cases.TASK], ["--model", 'quoted"id', cases.TASK], ["--model", "a&b", cases.TASK], ["--model", "a|b", cases.TASK], ["--model", "a%b", cases.TASK], ["--model", "a\nb", cases.TASK], ["--model", "a\rb", cases.TASK], ["--model", "x" * 101, cases.TASK], ["--model", "", cases.TASK], ["--model", "a\0b", cases.TASK], ["a\0b"])
    with sandbox() as (_root, task_dir, env):
        for args in invalid:
            expect("unsupported CLI is private", invoke(module, env, task_dir, args), (2, "", INVALID))
    for model in ("a", "x" * 100, "claude-opus-5-5[1m]", "vendor:model._-[]"):
        expect("safe model ID unchanged", module.parse_args(["--model", model, cases.TASK]).model, model)


def test_help(module):
    with sandbox() as (_root, task_dir, env):
        with mock.patch.object(module, "load_key", side_effect=AssertionError), mock.patch.object(module, "find_claude", side_effect=AssertionError), mock.patch.object(builtins, "open", side_effect=AssertionError):
            for flag in ("-h", "--help"):
                expect("help exact", invoke(module, env, task_dir, [flag]), (0, HELP, ""))


def test_windows_resolution(module):
    with tempfile.TemporaryDirectory(prefix="cc api resolver ") as directory:
        root = Path(directory).resolve()
        first, second = root / "first", root / "second"
        first.mkdir(); second.mkdir()
        cmd, exe = first / "claude.cmd", first / "claude.exe"
        cmd.touch(); exe.touch(); (second / "claude.exe").touch()
        env = {"PATH": str(first) + os.pathsep + str(second)}
        expect("Windows exe preferred within directory", module.find_claude(env, windows=True), str(exe))
        exe.unlink()
        expect("Windows cmd resolved", module.find_claude(env, windows=True), str(cmd))
        with mock.patch.object(module.subprocess, "run", return_value=completed()) as run:
            module.run_child([str(exe), "-p", "--output-format", "json"], {}, cases.TASK.encode("utf-8"))
        expect("native exe uses argument list", run.call_args[0][0], [str(exe), "-p", "--output-format", "json"])
        previous = os.getcwd()
        try:
            os.chdir(str(root))
            expect("relative PATH entry honored", module.find_claude({"PATH": "first"}, windows=True), str(cmd))
            (root / "claude.exe").touch()
            try:
                module.find_claude({"PATH": ""}, windows=True)
                rejected = False
            except module.CcApiError as exc:
                rejected = exc.exit_code == 3
            expect("empty PATH does not search cwd", rejected)
        finally:
            os.chdir(previous)
        if os.name != "nt":
            plain = first / "claude"
            plain.touch()
            try:
                module.find_claude(env, windows=False)
                rejected = False
            except module.CcApiError:
                rejected = True
            expect("POSIX launcher must be executable", rejected)
            plain.chmod(0o755)
            expect("POSIX executable resolved", module.find_claude(env, windows=False), str(plain))


def test_cmd_discovery(module):
    with tempfile.TemporaryDirectory(prefix="cc api cmd discovery ") as directory:
        root = Path(directory).resolve()
        first, second = root / "first", root / "second"
        first.mkdir(); second.mkdir()
        cmd = first / "claude.cmd"
        cmd.touch(); (second / "claude.exe").touch()
        env = {"PATH": str(first) + os.pathsep + str(second)}
        expect("Windows cmd resolved", module.find_claude(env, windows=True), str(cmd))


def test_windows_cmd_integration(module):
    if os.name != "nt":
        return
    task = 'Read "two words" & preserve ^ | < > %CC_API_TEST_EXPAND% !literal!\nsecond line\\'
    with sandbox() as (root, task_dir, env):
        env["CC_API_TEST_EXPAND"] = "SHOULD-NOT-EXPAND"
        before = {str(p.relative_to(root)) for p in root.rglob("*")}
        result = invoke(module, env, task_dir, ["--model", cases.MODEL + "[1m]", "--", task])
        record = cases.read_record(Path(env["CC_API_RECORD"]))
        after = {str(p.relative_to(root)) for p in root.rglob("*")}
        expect("Windows cmd creates no unexpected files", after - before, {"child-record.json"})
    expect("Windows cmd task stdin literal", (result[0], record["stdin_hex"]), (0, task.encode("utf-8").hex()))
    expect("Windows cmd argv literal", record["argv"], ["-p", "--output-format", "json", "--model", cases.MODEL + "[1m]"])
    expect("task_in_argv absent for Windows cmd", task not in record["argv"])


def test_no_wrapper_files(module):
    with sandbox() as (root, task_dir, env):
        def snapshot():
            return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and p != Path(env["CC_API_RECORD"])}
        before = snapshot()
        open_real, io_real, os_real = builtins.open, io.open, os.open
        def guard(original):
            def checked(path, mode="r", *args, **kwargs):
                if not isinstance(path, int) and isinstance(mode, str) and any(c in mode for c in "wax+"):
                    raise AssertionError("wrapper attempted a write")
                return original(path, mode, *args, **kwargs)
            return checked
        def os_guard(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise AssertionError("wrapper attempted os.open write")
            return os_real(path, flags, *args, **kwargs)
        with mock.patch.object(builtins, "open", guard(open_real)), mock.patch.object(io, "open", guard(io_real)), mock.patch.object(os, "open", os_guard):
            result = invoke(module, env, task_dir, [cases.TASK])
        expect("runner creates no files", (result[0], snapshot()), (0, before))


def test_packaging(module):
    paths = [REPO / "plugins" / plugin / "skills/cc-api/scripts/cc_api.py" for plugin in PLUGINS]
    expect("both helpers byte identical", paths[0].read_bytes(), paths[1].read_bytes())
    spec = importlib.util.spec_from_file_location("cc_api_manifest_checks", str(REPO / "scripts/check_manifests.py"))
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    checker._installed_cc_api_smoke(REPO)
    expect("installed copies independent", True)
    with tempfile.TemporaryDirectory(prefix="cc api unfinished packaging ") as directory:
        checkout = Path(directory) / "checkout"
        shutil.copytree(str(REPO), str(checkout), ignore=shutil.ignore_patterns(
            ".git", "node_modules", "__pycache__", "*.pyc"))
        # Present skill texts must still be checked when their helpers are not
        # implemented yet. Full mode must require those same missing helpers.
        for plugin in PLUGINS:
            (checkout / "plugins" / plugin / "skills/cc-api/scripts/cc_api.py").unlink()
        with mock.patch.object(checker, "_installed_smoke"), mock.patch.object(checker, "_installed_cc_api_smoke") as smoke, contextlib.redirect_stdout(io.StringIO()):
            partial, _notes = checker._run(checkout, existing_only=True)
            required, _notes = checker._run(checkout)
        expect("existing-only tolerates unfinished helpers", partial, [])
        expect("full packaging requires both helpers", len([error for error in required if error.startswith("missing:") and error.endswith("cc_api.py")]), 2)
        expect("unfinished helper smoke deferred", smoke.call_count, 0)
        skill = checkout / "plugins/token-counter/skills/cc-api/SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8").replace("name: cc-api", "name: invalid"), encoding="utf-8")
        with mock.patch.object(checker, "_installed_smoke"), contextlib.redirect_stdout(io.StringIO()):
            errors, _notes = checker._run(checkout, existing_only=True)
        expect("existing-only checks present skill text", any(error.startswith("Codex cc-api frontmatter/commands:") for error in errors))
    # Synchronizer refusal must precede opening a symlink destination for writing.
    sync = checker._import_script(REPO, "scripts/sync_shared.py", "cc_api_sync")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, dest = root / "source", root / "dest"
        source.write_bytes(b"canonical")
        dest.write_bytes(b"unchanged")
        real_islink = os.path.islink
        with mock.patch.object(sync, "shared_pairs", return_value=[(str(source), str(dest))]), mock.patch.object(sync.os.path, "islink", side_effect=lambda path: path == str(dest) or real_islink(path)), mock.patch.object(builtins, "open", wraps=builtins.open) as opened:
            refused = False
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    sync.synchronize(str(root))
            except OSError:
                refused = True
        expect("sync refuses symlink before write", refused and not any(c.args[0] == str(dest) and len(c.args) > 1 and "w" in c.args[1] for c in opened.call_args_list))
        expect("symlink target untouched", dest.read_bytes(), b"unchanged")


def test_interrupt(module):
    with mock.patch.object(module.subprocess, "run", side_effect=KeyboardInterrupt):
        result, _record, _cwd = fake(module)
    expect("interrupt exit and guidance", result, (130, "", "cc-api: interrupted. The child may have incurred API usage; check Console Billing before retrying.\n" + BILLING))
    with mock.patch.object(module.subprocess, "run", side_effect=RuntimeError("private values")):
        result, _record, _cwd = fake(module)
    expect("unexpected exception private", result, (5, "", "cc-api: unexpected runner failure; no diagnostic values were printed.\n"))


def test_error_details(module):
    value = cases.success_result()
    value.update(is_error=True, result="first", errors=["first", "second", "", 7, "second", "third"])
    expect("error details ordered and distinct", outcome(module, value), (4, "", "cc-api: claude reported an error (child exit 0).\nfirst\nsecond\nthird\n" + BILLING))
    for subtype in (None, "error", False, ""):
        value = cases.success_result(); value["subtype"] = subtype
        expect("present non-success subtype fails", outcome(module, value)[0], 4)
    value = cases.success_result(); value.pop("subtype"); value["is_error"] = 1
    expect("only literal true is JSON error", outcome(module, value)[0], 0)
    value["result"] = "  result\n\n"
    expect("result whitespace preserved", outcome(module, value)[1].startswith("  result\n\ncc-api:"))
    expect("invalid UTF8 display replaced", "\ufffd" in outcome(module, raw=b"invalid \xff")[2])


TESTS = tuple(name for name in globals() if name.startswith("test_"))


def main():
    def deny(*args, **kwargs):
        raise AssertionError("cc-api suite attempted network")
    with mock.patch.object(socket, "socket", deny), mock.patch.object(socket, "create_connection", deny), mock.patch.object(socket, "getaddrinfo", deny):
        for plugin in PLUGINS:
            module = load_module(plugin)
            for name in TESTS:
                start = len(RESULTS)
                try:
                    globals()[name](module)
                except BaseException as exc:
                    RESULTS.append((name + " runtime " + type(exc).__name__, False, ""))
                for label, ok, _detail in RESULTS[start:]:
                    print("[%s] %s: %s" % ("PASS" if ok else "FAIL", plugin, label))
    good = sum(ok for _name, ok, _detail in RESULTS)
    print("\n%d/%d cc-api assertions passed (both plugins; synthetic keys; no network)" % (good, len(RESULTS)))
    return 0 if good == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
