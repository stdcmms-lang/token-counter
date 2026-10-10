#!/usr/bin/env python3
"""Raw-byte Codex compatibility against the render.py blob at 2aebba1.

Only the renderer is frozen. Dependencies are imported a second time under
tokencounter_baseline, whose package path searches the frozen directory first.
Entrypoints execute their unchanged source with a namespace-bound __import__.
Current tokencounter entries are never swapped or removed from sys.modules.

All data is invented. Data-file opens are confined to this repository and the
temporary fixture root. Network, installers and private tokenizer activation
are blocked. A supported tiktoken must already be installed for the full lane.
"""
import builtins
import collections
import concurrent.futures
import contextlib
import datetime
import gzip
import hashlib
import importlib
import importlib.machinery
import io
import json
import locale
import os
from pathlib import Path
import random
import shutil
import socket
import sys
import tempfile
import time
import types
import urllib.request
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "plugins/token-counter/skills/token-report/scripts"
PACKAGE = LIB / "tokencounter"
FROZEN = REPO / "scripts/fixtures/codex_2aebba1"
RENDER_SHA256 = "c4dc9e6592e4cc0948218b0313fde45d60ebe738a38c8f0af20df412b2b8f303"
SHARE = REPO / "plugins/token-counter/skills/token-share/scripts/share.py"
LOCAL_ZONE = datetime.timezone(datetime.timedelta(hours=8), "Fixture/+08:00")
FIXED_NOW = datetime.datetime(2026, 9, 28, 12, tzinfo=datetime.timezone.utc)
FIXED_EPOCH = FIXED_NOW.timestamp()
VOCAB = REPO / "plugins/token-counter/assets/vendor/o200k_base.tiktoken"
PRICES = REPO / "plugins/token-counter/assets/vendor/openai_prices.json"
_TREES = None
_TOKENIZER_LANES = {}


class ByteDifference(AssertionError):
    """The designated comparison assertion; no payload contents in its message."""
    def __init__(self, label, offset, before, after):
        self.label, self.offset = label, offset
        self.before_length, self.after_length = len(before), len(after)
        left = "EOF" if offset == len(before) else "0x%02x" % before[offset]
        right = "EOF" if offset == len(after) else "0x%02x" % after[offset]
        super().__init__("%s: first byte difference at offset %d (%s -> %s; lengths %d/%d)" %
                         (label, offset, left, right, len(before), len(after)))


def compare_bytes(label, before, after):
    if not isinstance(before, bytes) or not isinstance(after, bytes):
        raise TypeError("compatibility comparisons require bytes")
    if before != after:
        offset = next((i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]),
                      min(len(before), len(after)))
        raise ByteDifference(label, offset, before, after)


class FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls.fromtimestamp(FIXED_EPOCH, tz or LOCAL_ZONE)
        return value.replace(tzinfo=None) if tz is None else value

    @classmethod
    def today(cls):
        return cls.now()

    @classmethod
    def utcnow(cls):
        return cls.fromtimestamp(FIXED_EPOCH, datetime.timezone.utc).replace(tzinfo=None)

    @classmethod
    def fromtimestamp(cls, value, tz=None):
        dt = datetime.datetime.fromtimestamp(value, tz or LOCAL_ZONE)
        if tz is None:
            dt = dt.replace(tzinfo=None)
        return cls(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
                   dt.microsecond, tzinfo=dt.tzinfo, fold=dt.fold)

    @classmethod
    def utcfromtimestamp(cls, value):
        return cls.fromtimestamp(value, datetime.timezone.utc).replace(tzinfo=None)

    def timestamp(self):
        value = self.replace(tzinfo=LOCAL_ZONE) if self.tzinfo is None else self
        return datetime.datetime.timestamp(value)

    def astimezone(self, tz=None):
        value = self.replace(tzinfo=LOCAL_ZONE) if self.tzinfo is None else self
        return datetime.datetime.astimezone(value, tz or LOCAL_ZONE)


class FixedDate(datetime.date):
    @classmethod
    def today(cls):
        dt = FixedDateTime.now()
        return cls(dt.year, dt.month, dt.day)


class Facade:
    def __init__(self, module, **overrides):
        self.module = module
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self.module, name)


DATETIME = Facade(datetime, datetime=FixedDateTime, date=FixedDate)


class TestClock(Facade):
    def __init__(self, progress=False):
        super().__init__(time)
        # Restart this script for each entrypoint invocation, on each side.
        self.wall = iter([FIXED_EPOCH + i / 8.0 for i in range(256)]) if progress else None
        self.cpu = iter([i / 8.0 for i in range(256)])

    def time(self):
        return next(self.wall) if self.wall is not None else FIXED_EPOCH

    def process_time(self):
        return next(self.cpu)

    def monotonic(self):
        return 123.0

    def perf_counter(self):
        return 123.0

    def localtime(self, value=None):
        # gmtime never consults the operating system's timezone, including Windows.
        value = FIXED_EPOCH if value is None else value
        return time.gmtime(value + LOCAL_ZONE.utcoffset(None).total_seconds())

    def strftime(self, fmt, value=None):
        return time.strftime(fmt, self.localtime() if value is None else value)


def deny(*args, **kwargs):
    raise AssertionError("compatibility harness attempted network or installation")


def _entrypoint(path, name, namespace, report=None):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    original_import = builtins.__import__

    def selected_import(import_name, globals=None, locals=None, fromlist=(), level=0):
        if not level and import_name == "report" and report is not None:
            return report
        if not level and (import_name == "tokencounter" or import_name.startswith("tokencounter.")):
            import_name = namespace + import_name[len("tokencounter"):]
        return original_import(import_name, globals, locals, fromlist, level)

    module.__dict__["__builtins__"] = dict(vars(builtins), __import__=selected_import)
    saved_path = list(sys.path)
    try:
        # Compile original bytes: no AST rewriting or function-body substitutions.
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    finally:
        sys.path[:] = saved_path
    return module


def trees():
    global _TREES
    if _TREES is not None:
        return _TREES
    test_frozen_renderer_hash()
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    if "tokencounter_baseline" not in sys.modules:
        package = types.ModuleType("tokencounter_baseline")
        package.__package__ = package.__name__
        package.__path__ = [str(FROZEN), str(PACKAGE)]
        package.__spec__ = importlib.machinery.ModuleSpec(package.__name__, None, is_package=True)
        package.__spec__.submodule_search_locations = package.__path__
        sys.modules[package.__name__] = package
    result = []
    for namespace in ("tokencounter_baseline", "tokencounter"):
        tree = {"namespace": namespace}
        for name in ("account", "analyze", "classify", "deps", "encoding", "images",
                     "index", "latency", "ledger", "pricing", "render", "rollout", "worker"):
            tree[name] = importlib.import_module(namespace + "." + name)
        tree["report"] = _entrypoint(LIB / "report.py", namespace + "_report", namespace)
        tree["share"] = _entrypoint(SHARE, namespace + "_share", namespace, tree["report"])
        result.append(tree)
    _TREES = result
    return result


def _within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@contextlib.contextmanager
def controls(tree, root, lane, order, trace):
    """Bind clocks/OS/executor facades to one tree, restoring them on exit."""
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    real_open, real_io_open = builtins.open, io.open
    original_import = builtins.__import__

    def guarded_open(file, *args, **kwargs):
        if isinstance(file, int):
            raise AssertionError("untracked file descriptor in compatibility harness")
        target = Path(os.fsdecode(file)).resolve()
        if not (_within(target, REPO) or _within(target, root)):
            raise AssertionError("compatibility harness attempted an outside data-file open")
        return real_open(file, *args, **kwargs)

    def guarded_io_open(file, *args, **kwargs):
        if isinstance(file, int):
            raise AssertionError("untracked file descriptor in compatibility harness")
        target = Path(os.fsdecode(file)).resolve()
        if not (_within(target, REPO) or _within(target, root)):
            raise AssertionError("compatibility harness attempted an outside data-file open")
        return real_io_open(file, *args, **kwargs)

    blocked = lane.startswith("blocked")
    if _TOKENIZER_LANES.get(tree["namespace"]) != blocked:
        tree["encoding"].load.cache_clear()
        _TOKENIZER_LANES[tree["namespace"]] = blocked

    def selected_import(name, globals=None, locals=None, fromlist=(), level=0):
        if not level and blocked and (name == "tiktoken" or name.startswith("tiktoken.")):
            raise ImportError("tiktoken blocked by the compatibility lane")
        return original_import(name, globals, locals, fromlist, level)

    class Executor:
        def __init__(self, max_workers=None, **kwargs):
            trace["workers"].append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def map(self, fn, paths, **kwargs):
            chosen = list(paths)
            if order == "reversed":
                chosen.reverse()
            trace["completion"].extend(chosen)
            for path in chosen:
                yield fn(path)

    def counted(fn):
        def extract(*args, **kwargs):
            trace["extracted"].append(args[0])
            return fn(*args, **kwargs)
        return extract

    env = {"CODEX_HOME": str(root), "TOKEN_COUNTER_NO_INSTALL": "1",
           "TOKEN_COUNTER_VOCAB": str(VOCAB), "TOKEN_COUNTER_PRICES": str(PRICES),
           "TOKENUSAGE_API": "https://example.invalid/api", "LANG": "C", "LC_ALL": "C"}
    saved_locale = locale.setlocale(locale.LC_TIME)
    saved_out = list(tree["report"]._OUT_DIR)
    tree["report"]._OUT_DIR[:] = [str(state)]
    try:
        locale.setlocale(locale.LC_TIME, "C")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch.object(builtins, "open", guarded_open))
            stack.enter_context(mock.patch.object(io, "open", guarded_io_open))
            stack.enter_context(mock.patch.object(builtins, "__import__", selected_import))
            for module in (socket,):
                for name in ("socket", "create_connection", "getaddrinfo"):
                    stack.enter_context(mock.patch.object(module, name, deny))
            stack.enter_context(mock.patch.object(urllib.request, "urlopen", deny))
            for name, module in tree.items():
                if not isinstance(module, types.ModuleType):
                    continue
                stack.enter_context(mock.patch.object(module, "open", guarded_open, create=True))
                if getattr(module, "datetime", None) is datetime:
                    stack.enter_context(mock.patch.object(module, "datetime", DATETIME))
                if getattr(module, "time", None) is time:
                    stack.enter_context(mock.patch.object(module, "time", TestClock(progress=name == "report")))
                if getattr(module, "os", None) is os:
                    stack.enter_context(mock.patch.object(module, "os", Facade(os, cpu_count=lambda: 4)))
            stack.enter_context(mock.patch.object(tree["report"], "cf", Facade(
                concurrent.futures, ProcessPoolExecutor=Executor)))
            stack.enter_context(mock.patch.object(tree["deps"], "importable", lambda: not blocked))
            stack.enter_context(mock.patch.object(tree["deps"], "activate", lambda *a, **k: None))
            stack.enter_context(mock.patch.object(tree["deps"], "roots", lambda: [str(state), str(state / "fallback")]))
            stack.enter_context(mock.patch.object(tree["deps"], "ensure", deny))
            stack.enter_context(mock.patch.object(tree["share"], "request", deny))
            stack.enter_context(mock.patch.object(tree["share"], "gzip", Facade(
                gzip, compress=lambda data, compresslevel=9, mtime=None:
                gzip.compress(data, compresslevel, mtime=int(FIXED_EPOCH) if mtime is None else mtime))))
            for name in ("process", "metrics_only"):
                stack.enter_context(mock.patch.object(tree["worker"], name, counted(getattr(tree["worker"], name))))
            yield
    finally:
        tree["report"]._OUT_DIR[:] = saved_out
        locale.setlocale(locale.LC_TIME, saved_locale)


def _record(epoch, kind, payload):
    return {"timestamp": datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat(),
            "type": kind, "payload": payload}


def make_corpus(root):
    rng = random.Random(1947)
    paths = []
    base = datetime.datetime(2026, 9, 10, 18, tzinfo=datetime.timezone.utc).timestamp()
    for j, day_delta in enumerate((0, 1, 10, 11)):
        start = base + day_delta * 86400
        reset = base + (7 if j < 2 else 14) * 86400
        model = ("gpt-5.5", "gpt-5.6", "codex-auto-review", "gpt-5.5")[j]
        sid = "compat-session-%d" % j
        records = [_record(start - 20, "session_meta", {
            "id": "compat-thread-%d" % j, "session_id": sid,
            "cwd": str(root / ("invented-workspace-%d" % j)),
            "base_instructions": {"text": "Synthetic system instructions. " * (j + 1)},
            "dynamic_tools": [{"name": "synthetic", "description": "Fixture tool",
                               "inputSchema": {"type": "object"}}],
        })]
        cumulative = collections.Counter()
        for i in range(7):
            at = start + i * 3600
            records.append(_record(at, "turn_context", {"model": model, "effort": "high"}))
            records.append(_record(at + 1, "event_msg", {"type": "thread_settings_applied",
                "service_tier": ("default", "priority", "ultrafast")[i % 3]}))
            records.append(_record(at + 2, "response_item", {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Invented question 中文. " * rng.randint(1, 4)}]}))
            records.append(_record(at + 5, "response_item", {"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Synthetic answer."}]}))
            if i == 2:
                records.append(_record(at + 6, "response_item", {"type": "web_search_call"}))
            usage = {"input_tokens": 1000 + i * 101 + j * 20, "cached_input_tokens": 128 * (i + 1),
                     "output_tokens": 20 + i * 7, "reasoning_output_tokens": 5 + i,
                     "cache_write_input_tokens": 0}
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
            cumulative.update(usage)
            records.append(_record(at + 10 + i, "token_usage_record", {
                "response_id": "compat-response-%d-%d" % (j, i), "usage": usage}))
            records.append(_record(at + 10.01 + i, "event_msg", {"type": "token_count",
                "info": {"last_token_usage": usage, "total_token_usage": dict(cumulative)},
                "rate_limits": {"primary": {"window_minutes": 10080, "used_percent": 10 + i * 10,
                                            "resets_at": reset}, "secondary": None,
                                "plan_type": "pro", "rate_limit_reached_type": "primary" if i == 6 else None}}))
        day = datetime.datetime.fromtimestamp(start, LOCAL_ZONE)
        parent = root / "sessions" / day.strftime("%Y/%m/%d")
        parent.mkdir(parents=True, exist_ok=True)
        path = parent / ("rollout-%sT18-00-00-%d.jsonl" % (day.date().isoformat(), j))
        path.write_bytes(b"".join((json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
                                 .encode("utf-8") for r in records))
        os.utime(str(path), (FIXED_EPOCH - 100, FIXED_EPOCH - 100))
        paths.append(str(path))
    # Invented identity beside the explicit sessions root; no real auth lookup.
    claims = {"email": "fixture@example.invalid", "https://api.openai.com/auth": {
        "chatgpt_plan_type": "pro", "chatgpt_account_id": "fixture-account"}}
    import base64
    token = "fixture." + base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=") + ".fixture"
    (root / "auth.json").write_bytes(json.dumps({"tokens": {"id_token": token}}).encode("utf-8"))
    os.utime(str(root / "auth.json"), (FIXED_EPOCH - 100, FIXED_EPOCH - 100))
    return paths


def _reset_state(root):
    state = root / "state"
    assert _within(state.resolve(), root.resolve()), "state reset escapes fixture root"
    if state.exists():
        shutil.rmtree(str(state))
    state.mkdir()


def _capture(fn, args):
    stdout, stderr = io.BytesIO(), io.BytesIO()
    # Byte streams with fixed encoding; no read_text/splitlines/line-ending normalization.
    out = io.TextIOWrapper(stdout, encoding="utf-8", newline=None)
    err = io.TextIOWrapper(stderr, encoding="utf-8", newline=None)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(args)
    out.flush()
    err.flush()
    result = (code, stdout.getvalue(), stderr.getvalue())
    out.detach()
    err.detach()
    if code != 0:
        raise AssertionError("fixture entrypoint exited %s (not a comparison failure)" % code)
    return result[1:]


def _report(tree, root, lane, order, worker_args, public, style, json_only=False):
    state = root / "state"
    trace = {"workers": [], "completion": [], "extracted": []}
    args = ["--sessions-root", str(root / "sessions"), "--no-open", "--no-install",
            "--prices", str(PRICES), "--vocab", str(VOCAB), "--style", style,
            "--json", str(state / "model.json")] + list(worker_args)
    if not json_only:
        args += ["--out", str(state / "report.html")]
    if lane.endswith("metrics"):
        args.append("--metrics-only")
    if public:
        args.append("--public")
    with controls(tree, root, lane, order, trace):
        stdout, stderr = _capture(tree["report"].main, args)
        artifacts = {"--json": (state / "model.json").read_bytes(), "stdout": stdout, "stderr": stderr}
        if not json_only:
            artifacts["HTML"] = (state / "report.html").read_bytes()
            body = tree["share"].report_body(str(state / "report.html"))
            artifacts["report upload body"] = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return artifacts, trace


def _assert_lane(artifacts, lane, warm, trace, paths, workers):
    model = json.loads(artifacts["--json"])
    assert model["generated_at"] == "2026-09-28T20:00:00+08:00"
    assert model["rate_limits"]["now"] == FIXED_EPOCH
    assert model["totals"]["responses"] == 28
    assert model["totals"]["input_source"] == ("tiktoken" if lane == "installed-full" else "recorded")
    cached = lane == "installed-full" and warm
    assert len(trace["extracted"]) == (0 if cached else len(paths)), "cold/warm extraction not exercised"
    assert trace["workers"] == ([] if cached or workers == 1 else [workers]), "worker option not exercised"
    if not cached:
        assert set(trace["extracted"]) == set(paths)
    if lane == "installed-full" and warm:
        assert b"4 cached | 0 to parse" in artifacts["stderr"], "warm run did not reuse the index"


def test_frozen_renderer_hash() -> None:
    blob = (FROZEN / "render.py").read_bytes()
    assert hashlib.sha256(blob).hexdigest() == RENDER_SHA256, "frozen render.py SHA-256 mismatch"
    assert b"\r\n" not in blob, "frozen Git blob must retain LF bytes"


def test_namespace_isolation() -> None:
    baseline, current = trees()
    assert Path(baseline["render"].__file__).resolve() == FROZEN / "render.py"
    assert Path(current["render"].__file__).resolve() == PACKAGE / "render.py"
    for name in baseline:
        if name == "namespace":
            continue
        assert baseline[name] is not current[name]
        if name not in ("report", "share"):
            assert baseline[name].__package__ == "tokencounter_baseline"
            assert current[name].__package__ == "tokencounter"
    for tree in (baseline, current):
        assert tree["report"].analyze is tree["analyze"]
        assert tree["share"].reportcli is tree["report"]
        assert tree["share"].render is tree["render"]
        assert tree["analyze"].worker is tree["worker"]


class SpringZone(datetime.tzinfo):
    """Synthetic DST zone for explicit _day_span(tz=...) vectors on Python 3.8."""
    def utcoffset(self, dt):
        boundary = (2026, 3, 29, 2)
        later = dt is not None and (dt.year, dt.month, dt.day, dt.hour) >= boundary
        return datetime.timedelta(hours=2 if later else 1)

    def dst(self, dt):
        return self.utcoffset(dt) - datetime.timedelta(hours=1)

    def tzname(self, dt):
        return "Synthetic/DST"


def test_datetime_controls() -> None:
    assert FixedDateTime.now().astimezone().timestamp() == FIXED_EPOCH
    assert FixedDateTime.fromtimestamp(FIXED_EPOCH).timestamp() == FIXED_EPOCH
    assert FixedDateTime(2026, 9, 28, 20).timestamp() == FIXED_EPOCH
    assert FixedDateTime.now(datetime.timezone.utc) == FIXED_NOW
    assert TestClock().localtime(FIXED_EPOCH).tm_hour == 20
    with tempfile.TemporaryDirectory(prefix="codex-compat-clock-") as d:
        for tree in trees():
            trace = {"workers": [], "completion": [], "extracted": []}
            with controls(tree, Path(d), "installed-metrics", "forward", trace):
                start, stop = tree["analyze"]._day_span("2026-03-29", tz=SpringZone())
                assert stop - start == 23 * 3600
                assert tree["analyze"]._day("2026-09-10T23:30:00Z") == "2026-09-11"
                tree["analyze"]._local_day.cache_clear()


def test_report_bytes() -> None:
    pairs = 0
    choices = (([], 2, "default"), (["--fast"], 4, "fast"), (["--procs", "3"], 3, "procs=3"))
    with tempfile.TemporaryDirectory(prefix="codex-compat-") as d:
        root = Path(d).resolve()
        paths = make_corpus(root)
        for lane in ("installed-full", "installed-metrics", "blocked-full", "blocked-metrics"):
            for order in ("forward", "reversed"):
                for worker_args, workers, worker_name in choices:
                    for public in (False, True):
                        for style in ("clinical", "matisse", "nocturne"):
                            before = []
                            for side, tree in enumerate(trees()):
                                _reset_state(root)
                                for warm in (False, True):
                                    artifacts, trace = _report(tree, root, lane, order, worker_args, public, style)
                                    _assert_lane(artifacts, lane, warm, trace, paths, workers)
                                    if trace["completion"]:
                                        assert trace["completion"] == (paths if order == "forward" else list(reversed(paths)))
                                    label = "%s/%s/%s/%s/%s/%s" % (lane, order, worker_name,
                                        "public" if public else "local", style, "warm" if warm else "cold")
                                    if side == 0:
                                        before.append(artifacts)
                                    else:
                                        for name in artifacts:
                                            compare_bytes(label + " " + name, before[int(warm)][name], artifacts[name])
                                        pairs += 1
            print("[PASS] %s: JSON, stdout, stderr, three local/public HTML styles, cold/warm, both executor orders" % lane)
        # --json alone must also preserve its complete stdout and early-return behavior.
        for lane in ("installed-full", "installed-metrics", "blocked-full", "blocked-metrics"):
            for public in (False, True):
                before = []
                for side, tree in enumerate(trees()):
                    _reset_state(root)
                    for warm in (False, True):
                        artifacts, trace = _report(tree, root, lane, "forward", ["--procs", "1"], public, "clinical", True)
                        _assert_lane(artifacts, lane, warm, trace, paths, 1)
                        assert not (root / "state/report.html").exists()
                        if side == 0:
                            before.append(artifacts)
                        else:
                            for name in artifacts:
                                compare_bytes("JSON-only " + lane + " " + name, before[int(warm)][name], artifacts[name])
                            pairs += 1
    print("[PASS] %d paired report runs; default/--fast/--procs=3 and serial JSON-only" % pairs)


def test_share_bytes() -> None:
    pairs = 0
    with tempfile.TemporaryDirectory(prefix="codex-compat-share-") as d:
        root = Path(d).resolve()
        make_corpus(root)
        for lane in ("installed-full", "installed-metrics", "blocked-full", "blocked-metrics"):
            for order in ("forward", "reversed"):
                for worker_args, workers in (([], 2), (["--fast"], 4), (["--procs", "3"], 3)):
                    before = []
                    for side, tree in enumerate(trees()):
                        _reset_state(root)
                        for warm in (False, True):
                            # Prime the warm state exactly as the corresponding report lane does.
                            if warm:
                                _report(tree, root, lane, order, worker_args, False, "clinical")
                            trace = {"workers": [], "completion": [], "extracted": []}
                            with controls(tree, root, lane, order, trace):
                                args = ["--sessions-root", str(root / "sessions"), "--handle", "synthetic-compat",
                                        "--no-report", "--out", str(root / "state/share.json")] + worker_args
                                stdout, stderr = _capture(tree["share"].main, args)
                                payload = (root / "state/share.json").read_bytes()
                                # Use the transport's serialization verbatim, without making a request.
                                transport = json.dumps(json.loads(payload), separators=(",", ":")).encode("utf-8")
                                artifacts = {"share payload file": payload, "share transport": transport,
                                             "share stdout": stdout, "share stderr": stderr}
                                # Also exercise build_payload's explicit aware `now` argument.
                                results, _ = tree["report"].collect(
                                    tree["rollout"].discover(str(root / "sessions")), workers, 1,
                                    None, None, True, True, 0)
                                charged, _ = tree["ledger"].build(results)
                                explicit, _ = tree["share"].build_payload(results, charged, now=FIXED_NOW)
                                artifacts["explicit-now payload"] = json.dumps(explicit, separators=(",", ":")).encode("utf-8")
                            if side == 0:
                                before.append(artifacts)
                            else:
                                for name in artifacts:
                                    compare_bytes(lane + "/" + order + " " + name, before[int(warm)][name], artifacts[name])
                                pairs += 1
    print("[PASS] %d paired share runs: payload file/transport/stdout, cold/warm report state, explicit now" % pairs)


def test_matisse_seed() -> None:
    with tempfile.TemporaryDirectory(prefix="codex-compat-matisse-") as d:
        root = Path(d).resolve()
        make_corpus(root)
        for tree in trees():
            _reset_state(root)
            artifacts, _ = _report(tree, root, "installed-metrics", "forward", [], False, "matisse")
            model = json.loads(artifacts["--json"])
            trace = {"workers": [], "completion": [], "extracted": []}
            with controls(tree, root, "installed-metrics", "forward", trace):
                first = tree["render"].render(model, style="matisse").encode("utf-8")
                second = tree["render"].render(model, style="matisse").encode("utf-8")
                compare_bytes("repeat Matisse render", first, second)
                compare_bytes("repeat Matisse seeded decoration", tree["render"]._matisse().encode("utf-8"),
                              tree["render"]._matisse().encode("utf-8"))


def test_comparison_detects_render_byte_change() -> None:
    baseline, current = trees()
    with tempfile.TemporaryDirectory(prefix="codex-compat-sensitivity-") as d:
        root = Path(d).resolve()
        make_corpus(root)
        _reset_state(root)
        before, _ = _report(baseline, root, "installed-metrics", "forward", [], False, "clinical")
        _reset_state(root)
        original = current["render"].render

        def changed(*args, **kwargs):
            return original(*args, **kwargs) + "!"  # exactly one UTF-8 byte, even on Windows

        with mock.patch.object(current["render"], "render", changed):
            after, _ = _report(current, root, "installed-metrics", "forward", [], False, "clinical")
        for name in ("--json", "stdout", "stderr"):
            compare_bytes("sensitivity control " + name, before[name], after[name])
        try:
            compare_bytes("sensitivity HTML", before["HTML"], after["HTML"])
        except ByteDifference as exc:
            assert exc.label == "sensitivity HTML"
            assert exc.offset == len(before["HTML"])
            assert exc.after_length == exc.before_length + 1
            print("[PASS] test_comparison_detects_render_byte_change: " + str(exc))
        else:
            raise AssertionError("one-byte current-renderer mutation reported equality")


def main() -> int:
    # Dependency absence is a test failure, never a silently skipped installed lane.
    try:
        import tiktoken
    except ImportError:
        print("[FAIL] installed-tokenizer lane requires an already installed supported tiktoken", file=sys.stderr)
        return 1
    assert hasattr(tiktoken, "Encoding"), "installed tiktoken lacks its supported Encoding API"
    test_frozen_renderer_hash()
    test_namespace_isolation()
    saved_current = {name: module for name, module in sys.modules.items()
                     if name == "tokencounter" or name.startswith("tokencounter.")}
    test_datetime_controls()
    test_report_bytes()
    test_share_bytes()
    test_matisse_seed()
    test_comparison_detects_render_byte_change()
    for name, module in saved_current.items():
        assert sys.modules.get(name) is module, "current package sys.modules identity changed"
    print("[PASS] frozen hash, isolated namespaces, fixed +08:00 clock/DST vectors, Matisse repeatability")
    return 0


if __name__ == "__main__":
    sys.exit(main())
