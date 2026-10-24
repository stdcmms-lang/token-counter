"""Require each cc-api mutation to break its designated executed assertion."""
import importlib.util
from pathlib import Path
import socket
import sys
import tempfile
from unittest import mock

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("cc_api_contract_tests", str(Path(__file__).with_name("test_cc_api.py")))
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
MANIFEST = {}
REQUIRED = {
    "env_key_ignored", "key_whitespace_kept", "file_fallback_ignored", "file_read_despite_env_key",
    "api_key_not_set", "source_key_leaked", "source_file_leaked", "auth_token_kept", "oauth_token_kept",
    "profile_kept", "endpoint_kept", "wif_variable_kept", "bedrock_kept", "vertex_kept", "foundry_kept",
    "future_provider_kept", "parent_environment_mutated", "model_dropped", "accept_edits_dropped",
    "default_permission_added", "output_format_changed", "cwd_changed", "task_not_on_stdin",
    "hard_timeout_added", "nonzero_exit_ignored", "json_error_ignored", "invalid_json_accepted",
    "credit_explanation_dropped", "stdout_redaction_disabled", "stderr_redaction_disabled",
    "invalid_json_redaction_disabled", "cmd_resolution_removed", "stdio_utf8_reconfiguration_removed",
}


MANIFEST["stdio_utf8_reconfiguration_removed"] = dict(
    test="test_utf8_output", assertion="cp1252 pipe UTF-8 first line",
    source_replace=('    for stream in (sys.stdout, sys.stderr):\n'
                    '        if hasattr(stream, "reconfigure"):\n'
                    '            stream.reconfigure(encoding="utf-8", errors="replace")\n', ""))


def paired(name, test, function, assertion):
    def decorate(factory):
        if name in MANIFEST:
            raise ValueError("duplicate mutation")
        MANIFEST[name] = dict(test=test, function=function, assertion=assertion, factory=factory)
        return factory
    return decorate


@paired("env_key_ignored", "test_env_precedence", "load_key", "environment key wins without file read")
def env_key_ignored(module, original):
    return lambda environ: original(dict(environ, CC_API_KEY=""))


@paired("key_whitespace_kept", "test_key_delivery", "load_key", "selected key reaches ANTHROPIC_API_KEY")
def key_whitespace_kept(module, original):
    return lambda environ: environ.get("CC_API_KEY") or original(environ)


@paired("file_fallback_ignored", "test_file_key", "main", "file key selected and stripped")
def file_fallback_ignored(module, original):
    def changed(argv=None):
        if not module.os.environ.get("CC_API_KEY", "").strip():
            print(module.MISSING_KEY, file=sys.stderr)
            return 2
        return original(argv)
    return changed


@paired("file_read_despite_env_key", "test_env_precedence", "load_key", "environment key wins without file read")
def file_read_despite_env_key(module, original):
    def changed(environ):
        with open(environ["CC_API_KEY_FILE"], encoding="utf-8-sig") as source:
            source.read()
        return original(environ)
    return changed


@paired("api_key_not_set", "test_key_delivery", "child_env", "selected key reaches ANTHROPIC_API_KEY")
def api_key_not_set(module, original):
    def changed(environ, key):
        result = original(environ, key)
        result.pop("ANTHROPIC_API_KEY", None)
        return result
    return changed


def restore_variable(variable):
    def factory(module, original):
        def changed(environ, key):
            result = original(environ, key)
            for name, value in environ.items():
                if name.upper() == variable:
                    result[name] = value
            return result
        return changed
    return factory


for name, variable in (
    ("source_key_leaked", "CC_API_KEY"), ("source_file_leaked", "CC_API_KEY_FILE"),
    ("auth_token_kept", "ANTHROPIC_AUTH_TOKEN"), ("oauth_token_kept", "CLAUDE_CODE_OAUTH_TOKEN"),
    ("profile_kept", "ANTHROPIC_PROFILE"), ("endpoint_kept", "ANTHROPIC_BASE_URL"),
    ("wif_variable_kept", "ANTHROPIC_IDENTITY_TOKEN"), ("bedrock_kept", "CLAUDE_CODE_USE_BEDROCK"),
    ("vertex_kept", "CLAUDE_CODE_USE_VERTEX"), ("foundry_kept", "CLAUDE_CODE_USE_FOUNDRY"),
    ("future_provider_kept", "CLAUDE_CODE_USE_FUTURE_PROVIDER"),
):
    paired(name, "test_environment_removal", "child_env", tests.cases.REMOVALS[variable][1])(restore_variable(variable))


@paired("parent_environment_mutated", "test_parent_unchanged", "child_env", "parent environment unchanged")
def parent_environment_mutated(module, original):
    def changed(environ, key):
        result = original(environ, key)
        environ["MUTATED_PARENT_SENTINEL"] = "changed"
        return result
    return changed


def remove_option(option):
    def factory(module, original):
        def changed(launcher, args):
            result = original(launcher, args)
            if option in result:
                index = result.index(option)
                del result[index:index + 2]
            return result
        return changed
    return factory


paired("model_dropped", "test_options", "build_argv", "model argv exact")(remove_option("--model"))
paired("accept_edits_dropped", "test_options", "build_argv", "acceptEdits argv exact")(remove_option("--permission-mode"))


@paired("default_permission_added", "test_default_permissions", "build_argv", "default grants no extra permissions")
def default_permission_added(module, original):
    return lambda launcher, args: original(launcher, args) + ["--permission-mode", "acceptEdits"]


@paired("output_format_changed", "test_default_run", "build_argv", "default argv exact")
def output_format_changed(module, original):
    def changed(launcher, args):
        result = original(launcher, args)
        result[result.index("--output-format") + 1] = "text"
        return result
    return changed


def subprocess_override(**overrides):
    def factory(module, original):
        def changed(*args, **kwargs):
            run = module.subprocess.run
            def substituted(*call_args, **call_kwargs):
                call_kwargs.update(overrides)
                return run(*call_args, **call_kwargs)
            with mock.patch.object(module.subprocess, "run", substituted):
                return original(*args, **kwargs)
        return changed
    return factory


paired("cwd_changed", "test_cwd", "run_child", "caller cwd preserved")(
    subprocess_override(cwd=str(Path(__file__).resolve().parent)))
paired("hard_timeout_added", "test_subprocess_contract", "run_child", "child has no timeout")(
    subprocess_override(timeout=1))


@paired("task_not_on_stdin", "test_subprocess_contract", "run_child", "child task bytes on stdin")
def task_not_on_stdin(module, original):
    return lambda argv, environ, task_bytes: original(list(argv) + [task_bytes.decode("utf-8")], environ, b"")


@paired("nonzero_exit_ignored", "test_nonzero_child", "emit_outcome", "nonzero child stays failure")
def nonzero_exit_ignored(module, original):
    def changed(completed, model, key):
        completed.returncode = 0
        return original(completed, model, key)
    return changed


@paired("json_error_ignored", "test_json_error_zero", "parse_result", "JSON error stays failure")
def json_error_ignored(module, original):
    def changed(stdout):
        result = original(stdout)
        if result is not None:
            result.update(subtype="success", is_error=False)
        return result
    return changed


@paired("invalid_json_accepted", "test_bad_json", "parse_result", "invalid result protocol fails")
def invalid_json_accepted(module, original):
    return lambda stdout: original(stdout) or tests.cases.success_result()


@paired("credit_explanation_dropped", "test_credit_errors", "emit_outcome", "credit balance error explained")
def credit_explanation_dropped(module, original):
    def changed(*args, **kwargs):
        with mock.patch.object(module, "CREDIT_EXPLANATION", ""):
            return original(*args, **kwargs)
    return changed


def skip_redaction(prefix):
    return lambda module, original: lambda text, key: text if text.startswith(prefix) else original(text, key)


paired("stdout_redaction_disabled", "test_redaction", "redact", "stdout key redacted")(skip_redaction("exposed:"))
paired("stderr_redaction_disabled", "test_redaction", "redact", "stderr key redacted")(skip_redaction("stderr exposed:"))
paired("invalid_json_redaction_disabled", "test_redaction", "redact", "invalid JSON key redacted")(skip_redaction("not-json exposed:"))


@paired("cmd_resolution_removed", "test_cmd_discovery", "find_claude", "Windows cmd resolved")
def cmd_resolution_removed(module, original):
    def changed(*args, **kwargs):
        isfile = module.os.path.isfile
        with mock.patch.object(module.os.path, "isfile", lambda path: not path.lower().endswith(".cmd") and isfile(path)):
            return original(*args, **kwargs)
    return changed


def evaluate(module, entry, factory=None):
    test = getattr(tests, entry["test"])
    def run():
        tests.RESULTS.clear()
        unexpected = []
        write = module._write_line
        def observe(text, stream):
            if text == module.UNEXPECTED_ERROR:
                unexpected.append("unexpected runner failure")
            return write(text, stream)
        try:
            with mock.patch.object(module, "_write_line", observe):
                test(module)
        except BaseException as exc:
            return list(tests.RESULTS), type(exc).__name__
        return list(tests.RESULTS), unexpected[0] if unexpected else None
    baseline, error = run()
    named = entry["assertion"]
    if error or not baseline or any(not ok for _name, ok, _detail in baseline) or not any(name == named for name, _ok, _detail in baseline):
        return False, "invalid mutation: baseline did not pass its designated assertion"
    if "source_replace" in entry:
        before, after = entry["source_replace"]
        source = Path(module.__file__).read_text(encoding="utf-8")
        if before == after or source.count(before) != 1:
            return False, "invalid mutation: source replacement must change exactly one target"
        # In-process monkeypatches cannot reach the real helper subprocess.
        with tempfile.TemporaryDirectory(prefix="cc api source mutation ") as directory:
            path = Path(directory) / "cc_api.py"
            path.write_text(source.replace(before, after), encoding="utf-8")
            with mock.patch.object(module, "__file__", str(path)):
                results, error = run()
        if error:
            return False, "invalid mutation: exception " + error
        if not any(name == named and not ok for name, ok, _detail in results):
            return False, "invalid mutation: designated assertion did not fail"
        return True, "caught by: " + named
    original = getattr(module, entry["function"])
    state = {"calls": 0, "error": None}
    try:
        mutation = (factory or entry["factory"])(module, original)
    except BaseException as exc:
        return False, "invalid mutation: setup " + type(exc).__name__
    def instrumented(*args, **kwargs):
        state["calls"] += 1
        try:
            return mutation(*args, **kwargs)
        except BaseException as exc:
            state["error"] = type(exc).__name__
            raise
    setattr(module, entry["function"], instrumented)
    try:
        results, error = run()
    finally:
        setattr(module, entry["function"], original)
    if state["error"] or error:
        return False, "invalid mutation: exception " + (state["error"] or error)
    if not state["calls"]:
        return False, "invalid mutation: decision did not execute"
    if not any(name == named and not ok for name, ok, _detail in results):
        return False, "invalid mutation: designated assertion did not fail"
    return True, "caught by: " + named


def self_tests(module):
    witness = MANIFEST["default_permission_added"]
    expect_rejected = []
    expect_rejected.append(not evaluate(module, witness, lambda _m, original: original)[0])
    unexecuted = dict(witness, function="turns_label")
    expect_rejected.append(evaluate(module, unexecuted, lambda _m, original: original)[1] == "invalid mutation: decision did not execute")
    for exception in (RuntimeError, ImportError):
        def crash_factory(_module, _original):
            def crash(*args, **kwargs):
                raise exception("invented failure")
            return crash
        # load_key executes inside main; main swallows the injected exception.
        entry = dict(MANIFEST["key_whitespace_kept"])
        ok, reason = evaluate(module, entry, crash_factory)
        expect_rejected.append(not ok and reason == "invalid mutation: exception " + exception.__name__)
    def setup_crash(_module, _original):
        raise RuntimeError("invented setup failure")
    expect_rejected.append(evaluate(module, witness, setup_crash)[1] == "invalid mutation: setup RuntimeError")
    unrelated = dict(MANIFEST["hard_timeout_added"], assertion="child task bytes on stdin")
    expect_rejected.append(evaluate(module, unrelated)[1] == "invalid mutation: designated assertion did not fail")
    broken = dict(witness, assertion="absent baseline assertion")
    expect_rejected.append(evaluate(module, broken)[1] == "invalid mutation: baseline did not pass its designated assertion")
    source = MANIFEST["stdio_utf8_reconfiguration_removed"]
    before, _after = source["source_replace"]
    for replacement in ((before, before), ("absent source target", "")):
        invalid = dict(source, source_replace=replacement)
        expect_rejected.append(evaluate(module, invalid)[1] == "invalid mutation: source replacement must change exactly one target")
    return all(expect_rejected)


def main():
    if set(MANIFEST) != REQUIRED:
        print("[FAIL] cc-api mutation manifest incomplete")
        return 1
    def deny(*args, **kwargs):
        raise AssertionError("cc-api mutation suite attempted network")
    good = 0
    with mock.patch.object(socket, "socket", deny), mock.patch.object(socket, "create_connection", deny), mock.patch.object(socket, "getaddrinfo", deny):
        for plugin in tests.PLUGINS:
            module = tests.load_module(plugin)
            if not self_tests(module):
                print("[FAIL] " + plugin + ": mutation harness self-tests")
                return 1
            print("[PASS] " + plugin + ": rejects no-op, unexecuted decision, swallowed crashes/import errors, setup errors, unrelated failure and invalid baseline")
            for name, entry in sorted(MANIFEST.items()):
                ok, reason = evaluate(module, entry)
                good += int(ok)
                print("[%s] %s: %s -> %s" % ("PASS" if ok else "FAIL", plugin, name, reason))
    total = len(MANIFEST) * len(tests.PLUGINS)
    print("\n%d/%d cc-api mutations caught (both plugins; manifest complete)" % (good, total))
    return 0 if good == total else 1


if __name__ == "__main__":
    sys.exit(main())
