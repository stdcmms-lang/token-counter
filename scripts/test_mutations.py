"""Mutation tests: revert a fix, and require the test named for it to fail.

This exists because of a specific incident. A fix for out-of-window damage counters was
placed *after* the window filter, where its own guard could never be true; the fix did
nothing for a full revision, and the regression test named for it passed throughout because
it exercised the layer beneath the bug rather than the path containing it.

A passing test proves nothing unless it would fail without its fix. Each case below reverts
one historical defect and asserts that at least one named assertion goes red.

    python scripts/test_mutations.py
"""
import contextlib
import collections
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), 'plugins', 'token-counter', 'skills',
                   'token-report', 'scripts')
sys.path.insert(0, HERE)
sys.path.insert(0, LIB)

import test_pipeline as tp                                       # noqa: E402
import report as rp                                              # noqa: E402
from tokencounter import analyze, classify, latency, ledger, pricing, render, rollout, worker  # noqa: E402


def ledger_case(name_fragment):
    """Run the one `test_ledger.py` case whose name contains `name_fragment`."""
    import test_ledger as tl
    for name, files, expected in tl.CASES:
        if name_fragment.lower() in name.lower():
            got = tl.run(files)
            for k, v in expected.items():
                tp.RESULTS.append((f'{name} [{k}]', got.get(k, 0) == v,
                                   f'expected {v}, got {got.get(k, 0)}'))
            return
    raise LookupError(f'no ledger case matching {name_fragment!r}')


def run(fn):
    """Run one test function, capturing its assertions instead of printing them."""
    tp.RESULTS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            fn()
        except Exception as exc:                      # a crash is also a failure
            return [('raised ' + exc.__class__.__name__, False, str(exc)[:100])]
    return list(tp.RESULTS)


CASES = []


def case(name, target):
    def deco(fn):
        CASES.append((name, target, fn))
        return fn
    return deco


@case('out-of-window damage aggregation is inert',
      lambda: tp.test_damage_outside_window_reaches_the_report())
def _damage():
    orig = rp.damage_outside
    rp.damage_outside = lambda results, window: collections.Counter()
    return lambda: setattr(rp, 'damage_outside', orig)


@case('cache key ignores the tokenizer implementation',
      lambda: tp.test_extractor_version_tracks_source())
def _weak_key():
    import hashlib
    orig = rp._extractor_version

    def weak(vocab=None):
        h = hashlib.blake2b(digest_size=6)
        import tiktoken
        h.update(getattr(tiktoken, '__version__', '?').encode())
        return int.from_bytes(h.digest(), 'big')

    rp._extractor_version = weak
    return lambda: setattr(rp, '_extractor_version', orig)


@case('byte prefilter reinstated on the ledger path',
      lambda: tp.test_object_shaped_corruption_is_visible())
def _prefilter():
    orig = rollout.iter_records

    def filtered(path, hints=None, damage=None):
        return orig(path, rollout.USAGE_HINTS + (rollout.META_HINT,), damage)

    worker.rollout.iter_records = filtered
    return lambda: setattr(worker.rollout, 'iter_records', orig)


@case('charged replay is counted as "dropped"',
      lambda: tp.test_replay_mode_is_labelled())
def _replay_label():
    orig = ledger.build

    def relabel(*a, **k):
        charged, counters = orig(*a, **k)
        if k.get('exclude_replay', True) is False and counters.get('inherited_charged'):
            counters['inherited'] = counters.pop('inherited_charged')
            counters['inherited_input'] = counters.pop('inherited_charged_input', 0)
        return charged, counters

    tp.ledger.build = relabel
    return lambda: setattr(tp.ledger, 'build', orig)


@case('the cumulative curve is accumulated in file order, not time order',
      lambda: tp.test_cumulative_curve_is_monotonic())
def _arrival_order():
    orig = analyze._in_time_order
    analyze._in_time_order = lambda rs: [r for r in rs if r[0] is not None]
    return lambda: setattr(analyze, '_in_time_order', orig)


@case('replay matching searches the whole session, not the ancestor chain',
      lambda: ledger_case('unrelated siblings'))
def _session_wide():
    orig = ledger._ancestor_paths

    def every_earlier(files, entries):
        out, seen = {}, []
        for _, path in entries:
            out[path] = list(seen)
            seen.append(path)
        return out

    ledger._ancestor_paths = every_earlier
    return lambda: setattr(ledger, '_ancestor_paths', orig)


@case('resend costing counts both usage streams',
      lambda: tp.test_resend_identity())
def _double_resend():
    orig = worker._charged_stream
    # Revert to counting every usage mark: in a both-stream file that is every response
    # twice, which inflated resend cost ~2x across 602 files.
    worker._charged_stream = lambda res: None
    orig_call = worker._is_model_call
    worker._is_model_call = lambda rec, usage, prev, stream: True

    def undo():
        worker._charged_stream = orig
        worker._is_model_call = orig_call
    return undo



@case('rate-limit windows keyed on the raw resets_at, without clustering',
      lambda: tp.test_rate_limit_windows())
def _no_cluster():
    # An idle window re-quotes its reset as now+7d on every call, so keying a window on the
    # integer turns one untouched week into hundreds of "windows" -- 1,355 of them across
    # the development corpus for roughly thirty real ones.
    orig = analyze.CLUSTER_TOL_S
    analyze.CLUSTER_TOL_S = -1
    return lambda: setattr(analyze, 'CLUSTER_TOL_S', orig)


@case('the daily chart stacks fewer models than the corpus has',
      lambda: tp.test_daily_model_split())
def _daily_cap():
    # A model folded into `other` disappears from the legend without the bar changing
    # height, so the chart looks correct while naming the wrong thing.
    orig = render.DAILY_MODELS
    render.DAILY_MODELS = 1
    return lambda: setattr(render, 'DAILY_MODELS', orig)


@case('a replaced window keeps being drawn after its successor opens',
      lambda: tp.test_rate_limit_windows())
def _no_clip():
    # The server keeps serving a replaced window to some sessions -- 1.6 days into the next
    # week on the development corpus -- so two limit curves are drawn live at once.
    orig = analyze.LATE_TOL_S
    analyze.LATE_TOL_S = 10 ** 9
    return lambda: setattr(analyze, 'LATE_TOL_S', orig)


@case('weekly window identified by slot name instead of window length',
      lambda: tp.test_rate_limit_windows())
def _slot_name():
    # The weekly limit is `secondary` behind a 5-hour `primary` in older CLI builds and
    # `primary` alone in newer ones.  Trusting the name charts a 5-hour window as a week.
    orig = worker.note_rate_limits

    def primary_only(rl, ts, acc, ctr):
        if isinstance(rl, dict):
            rl = dict(rl, secondary=None)
        return orig(rl, ts, acc, ctr)

    worker.note_rate_limits = primary_only
    return lambda: setattr(worker, 'note_rate_limits', orig)


@case('account claims read in only one namespace shape',
      lambda: tp.test_account_claims())
def _flat_claims_only():
    # The live tokens carry OpenAI's claims as a nested object under the namespace; the
    # flattened spelling is the usual JWT convention.  Reading one shape loses the plan and
    # the account id *silently*, because email and name populate from the plain claims and
    # the record still reports itself as available.
    from tokencounter import account
    orig = account.NS
    account.NS = orig + '/'
    return lambda: setattr(account, 'NS', orig)


def _worker_with(*edits):
    """`tokencounter.worker` re-executed from source with ``(old, new)`` edits applied.

    The two baseline defects are one line each inside `_assemble`, which no attribute patch
    can reach; the source is the only handle.  Each `old` must occur exactly once.
    """
    import types
    path = worker.__file__
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    for old, new in edits:
        assert src.count(old) == 1, f'edit anchor not unique: {old!r}'
        src = src.replace(old, new)
    mod = types.ModuleType('tokencounter.worker')
    mod.__file__, mod.__package__ = path, 'tokencounter'
    exec(compile(src, path, 'exec'), mod.__dict__)
    return mod


_BASELINE = ("        if stream == preferred:\n"
             "            q_start, q_end = p_start, p_end\n")
_FOLD_END = "                p_end = it['_pos'] + 1\n\n    # Per-item resend cost"


@case('stable-prefix baseline captured after the output fold (round 6)',
      lambda: tp.test_stable_prefix_baseline())
def _late_baseline():
    # Baseline = previous prompt *plus its own reply*: a 20-token request, a 100-token answer
    # and a 10-token input reported a 120-token stable prefix.  The original test wrote the
    # reply after the usage record, so no fold happened and it passed with this reverted.
    orig = tp.worker
    tp.worker = _worker_with(
        (_BASELINE, ''),
        (_FOLD_END, "                p_end = it['_pos'] + 1\n"
                    "        q_start, q_end = p_start, p_end\n\n    # Per-item resend cost"))
    return lambda: setattr(tp, 'worker', orig)


@case('stable-prefix baseline moved by every stream, not only the charged one',
      lambda: tp.test_stable_prefix_baseline())
def _mirror_baseline():
    # The legacy mirror after each explicit record re-captured the baseline once that
    # record's output had been folded in -- round 6 again, for every both-stream file.
    orig = tp.worker
    tp.worker = _worker_with((_BASELINE, "        q_start, q_end = p_start, p_end\n"))
    return lambda: setattr(tp, 'worker', orig)


@case('an archived session joins the results but not the window',
      lambda: tp.test_include_archived_counts_archived_sessions())
def _archived_out_of_window():
    orig = rp._include_archived

    def results_only(cache, results, window, a):
        n = 0
        for r in cache.archived():
            p = r.get('path')
            if p and p not in results and rp._in_scope(r, a):
                results[p] = r
                n += 1
        return n

    rp._include_archived = results_only
    return lambda: setattr(rp, '_include_archived', orig)


@case('--rebuild gives up when the index file cannot be deleted',
      lambda: tp.test_rebuild_discards_a_held_index())
def _rebuild_gives_up():
    # What the delete-only version did on Windows with the file held open: printed that it
    # could not delete the index, promised the schema check would re-parse, and reused it.
    orig = rp._discard_index
    rp._discard_index = lambda db_path: None
    return lambda: setattr(rp, '_discard_index', orig)


@case('daily span is the localised start plus 86,400 seconds',
      lambda: tp.test_day_span_across_clock_changes())
def _fixed_offset_day():
    import datetime
    orig = analyze._day_span

    def old(date_str, tz=None):
        d = datetime.date.fromisoformat(str(date_str))
        start = datetime.datetime(d.year, d.month, d.day)
        start = start.replace(tzinfo=tz) if tz is not None else start.astimezone()
        # `astimezone()` attaches a fixed offset; on one, + a day is + 86,400 s.
        start = start.astimezone(datetime.timezone(start.utcoffset()))
        return start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()

    analyze._day_span = old
    return lambda: setattr(analyze, '_day_span', orig)


@case('a missing tiktoken is left missing',
      lambda: tp.test_report_installs_missing_tiktoken())
def _never_installs():
    # Before 1.4.0: load the tokenizer or give up.  `codex plugin add` installs no Python
    # packages, so on a fresh install this gave up every time.
    orig = rp.tokenizer_status

    def old(a):
        from tokencounter import encoding as tcenc
        try:
            tcenc.load(a.vocab)
            return None
        except (ImportError, FileNotFoundError, ValueError) as exc:
            return str(exc).strip().splitlines()[0]

    rp.tokenizer_status = old
    return lambda: setattr(rp, 'tokenizer_status', orig)


@case('the vocabulary is read through tiktoken.load',
      lambda: tp.test_vocabulary_read_from_the_file())
def _tiktoken_loader():
    # Before 1.5.1: `load_tiktoken_bpe`, which needs `blobfile` under tiktoken 0.7.0 (the
    # last release for Python 3.8) and otherwise serves a copy cached by path.
    from tokencounter import encoding as tcenc
    orig = tcenc.read_ranks

    def old(path):
        from tiktoken.load import load_tiktoken_bpe
        return load_tiktoken_bpe(path)

    tcenc.read_ranks = old
    tcenc.load.cache_clear()

    def undo():
        tcenc.read_ranks = orig
        tcenc.load.cache_clear()
    return undo


@case('a vocabulary with a repeated rank is handed to tiktoken',
      lambda: tp.test_vocabulary_read_from_the_file())
def _repeated_rank():
    # read_ranks without its duplicate check: tiktoken then panics on a truncated rank.
    import base64
    from tokencounter import encoding as tcenc
    orig = tcenc.read_ranks

    def unchecked(path):
        with open(path, 'rb') as fh:
            contents = fh.read()
        ranks = {}
        for n, line in enumerate(contents.splitlines(), 1):
            if not line:
                continue
            try:
                token, rank = line.split()
                ranks[base64.b64decode(token)] = int(rank)
            except Exception as exc:
                raise ValueError(f'line {n} is not "<base64 token> <rank>": '
                                 f'{line[:60]!r}') from exc
        return ranks

    tcenc.read_ranks = unchecked
    tcenc.load.cache_clear()

    def undo():
        tcenc.read_ranks = orig
        tcenc.load.cache_clear()
    return undo


@case('input shown as Codex recorded it, though the run counted it with tiktoken',
      lambda: tp.test_input_counted_with_tiktoken())
def _recorded_input():
    # Before 1.4.0 every input figure was Codex's, and tiktoken fed only the content pie.
    orig = analyze.tiktoken_inputs
    analyze.tiktoken_inputs = lambda fr: {}
    return lambda: setattr(analyze, 'tiktoken_inputs', orig)


@case('a request starts at the input before its usage record, not before its first output',
      lambda: tp.test_response_time_from_records())
def _late_request_start():
    # A tool that finishes while its response is still streaming writes its output before
    # that response's usage record; anchoring there shortens the response by the tool's run.
    orig = worker._request_start
    worker._request_start = lambda frozen_ts, frozen, anchor_ts: anchor_ts
    return lambda: setattr(worker, '_request_start', orig)


@case('a response is timed from its last input even when the previous response ended later',
      lambda: tp.test_response_time_from_records())
def _input_only_start():
    # After a compaction the last input predates the compaction call, so the next response
    # is charged the compaction's time as well as its own.
    orig = latency._response_start
    latency._response_start = lambda req, prev_end: req
    return lambda: setattr(latency, '_response_start', orig)


@case('a response with no request stamp is timed from the previous response',
      lambda: tp.test_latency_caps_and_counters())
def _gap_as_response():
    orig = latency._timed_start
    latency._timed_start = lambda req, prev_end: latency._response_start(req, prev_end)
    return lambda: setattr(latency, '_timed_start', orig)


@case("a fork child's replayed tool calls are timed as its own",
      lambda: tp.test_replayed_history_is_not_timed())
def _replayed_tools():
    orig = latency._own_work
    latency._own_work = lambda call_t, live_start: True
    return lambda: setattr(latency, '_own_work', orig)


@case("an interrupted response's start survives into the next turn",
      lambda: tp.test_request_start_edge_cases())
def _stale_freeze():
    # An interrupted response writes output and no usage record, so nothing cleared the
    # start frozen at its first output: the next turn's first response was timed from the
    # interrupted request, the user's idle time included -- 305 s for a 5 s response.
    orig = worker._ends_response
    worker._ends_response = lambda outer, payload: False
    return lambda: setattr(worker, '_ends_response', orig)


@case('a legacy compaction is charged to the response after it',
      lambda: tp.test_request_start_edge_cases())
def _compaction_in_next():
    # The legacy stream charges no usage for a compaction, so the previous-response floor
    # never sees it, and the next response was timed from the last input before it.
    orig = worker._request_anchor
    worker._request_anchor = lambda anchor_ts, compact_ts: anchor_ts
    return lambda: setattr(worker, '_request_anchor', orig)


@case('an uncharged legacy repeat ends the response in flight',
      lambda: tp.test_request_start_edge_cases())
def _repeat_ends_response():
    orig = tp.worker
    tp.worker = _worker_with((
        "            if not (full_state == prev_full\n"
        "                    or (li == 0 and lo_ == 0 and lt > 0)):\n",
        "            if True:\n"))
    return lambda: setattr(tp, 'worker', orig)


@case("any item the classifier does not call output moves a request's start",
      lambda: tp.test_request_start_edge_cases())
def _any_input_anchors():
    # `classify.item_role` calls everything it does not recognise input, which is right for
    # prompt content and wrong for timing: a web search the model ran shortened its response.
    orig = worker._timing_side
    worker._timing_side = classify.item_role
    return lambda: setattr(worker, '_timing_side', orig)


@case('a replay the ledger charges anyway is timed',
      lambda: tp.test_replayed_history_is_not_timed())
def _charged_replay_timed():
    # An ambiguous one-record match, or every match under --no-replay-exclusion, is charged;
    # its rows carry the child's creation time and read as millisecond responses.
    orig = latency._is_replay
    latency._is_replay = lambda row: False
    return lambda: setattr(latency, '_is_replay', orig)


@case('a token column with a single distinct value sets its rate',
      lambda: tp.test_pace_split())
def _sparse_column():
    orig = latency.FIT_DISTINCT
    latency.FIT_DISTINCT = 2
    return lambda: setattr(latency, 'FIT_DISTINCT', orig)


@case("a fork child's replayed rate-limit events are counted as its own",
      lambda: tp.test_limit_events())
def _replayed_limit_events():
    # A fork child replays its parent's snapshots stamped with its own creation time: every
    # refusal the parent logged would be counted again, on the day the child was made.
    orig = worker.OPENING_BURST_GAP_S
    worker.OPENING_BURST_GAP_S = -1
    return lambda: setattr(worker, 'OPENING_BURST_GAP_S', orig)


@case("the opening burst runs on into the child's own work",
      lambda: tp.test_limit_events())
def _burst_too_long():
    # A refusal needs a round trip, so it lands after the burst; a burst that swallows the
    # child's first seconds takes its own refusal for the parent's.
    orig = worker.OPENING_BURST_GAP_S
    worker.OPENING_BURST_GAP_S = 10
    return lambda: setattr(worker, 'OPENING_BURST_GAP_S', orig)


def _pricing_with(*edits):
    """`tokencounter.pricing` re-executed from source with ``(old, new)`` edits applied, as
    `_worker_with` does for the worker: the price arithmetic is inline in `price`."""
    import types
    path = pricing.__file__
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    for old, new in edits:
        assert src.count(old) == 1, f'edit anchor not unique: {old!r}'
        src = src.replace(old, new)
    mod = types.ModuleType('tokencounter.pricing')
    mod.__file__, mod.__package__ = path, 'tokencounter'
    exec(compile(src, path, 'exec'), mod.__dict__)
    return mod


@case('cache writes are billed on top of ordinary input',
      lambda: tp.test_pricing_math())
def _writes_double_billed():
    # OpenAI's formula takes writes out of input: ordinary = input - cached - writes.  Left
    # in, every written token is billed twice, once as input and once at the write rate.
    orig = tp.pricing
    tp.pricing = _pricing_with(("usd = ((inp - cached - writes) * r_in",
                                "usd = ((inp - cached) * r_in"))
    return lambda: setattr(tp, 'pricing', orig)


@case('a prompt of exactly the threshold is priced as long context',
      lambda: tp.test_pricing_math())
def _threshold_inclusive():
    # The page: "Short context: <=272K input tokens. Long context: >272K."
    orig = tp.pricing
    tp.pricing = _pricing_with(("if inp > table['long_context_threshold'] and",
                                "if inp >= table['long_context_threshold'] and"))
    return lambda: setattr(tp, 'pricing', orig)


@case('a search lands on whichever stream writes first',
      lambda: tp.test_tier_and_searches_extracted())
def _searches_shared():
    # A both-stream file charges the explicit stream; one pending count consumed by the
    # legacy record written before it drops the search from the charged row.
    orig = tp.worker
    tp.worker = _worker_with((
        "                rec['web_search'], ws_pending['legacy'] = ws_pending['legacy'], 0\n",
        "                rec['web_search'], ws_pending['legacy'] = ws_pending['legacy'], 0\n"
        "                ws_pending['explicit'] = 0\n"))
    return lambda: setattr(tp, 'worker', orig)


@case('a context snapshot takes the searches before it',
      lambda: tp.test_tier_and_searches_extracted())
def _snapshot_takes_searches():
    # The ledger never charges a zero/zero snapshot, so searches attached to one vanish.
    orig = tp.worker
    tp.worker = _worker_with((
        "            if _is_model_call(None, u, None, 'explicit'):\n",
        "            if True:\n"))
    return lambda: setattr(tp, 'worker', orig)


@case('a settings snapshot with no tier reads as no snapshot',
      lambda: tp.test_tier_and_searches_extracted())
def _absent_tier_unrecorded():
    # Codex leaves `service_tier` out when none is set: that is a recorded standard, and
    # reading it as unrecorded counts every such response as a guess.
    orig = worker._service_tier

    def absent_is_none(payload):
        ts = payload.get('thread_settings')
        v = ts.get('service_tier') if isinstance(ts, dict) else None
        return v if isinstance(v, str) and v else None
    worker._service_tier = absent_is_none
    return lambda: setattr(worker, '_service_tier', orig)


@case("a fork child's replayed interruptions are counted as its own",
      lambda: tp.test_interrupted_turns_counted())
def _replayed_aborts():
    orig = worker.OPENING_BURST_GAP_S
    worker.OPENING_BURST_GAP_S = -1
    return lambda: setattr(worker, 'OPENING_BURST_GAP_S', orig)



@case("a thread's first turn keeps no tier, though its file records one",
      lambda: tp.test_tier_and_searches_extracted())
def _no_backfill():
    # Codex never persists the first turn's settings snapshot: without the back-fill every
    # thread's first turn is priced at standard, Fast mode or not.
    orig = tp.worker
    tp.worker = _worker_with(("    _backfill_tier(res, first_tier)\n", ""))
    return lambda: setattr(tp, 'worker', orig)


@case("a fork child's replayed, unanswered search is charged to its first response",
      lambda: tp.test_replayed_search_not_charged_twice())
def _replayed_search_pending():
    orig = tp.worker
    tp.worker = _worker_with((
        "                    if res['parent_thread_id']:\n"
        "                        ws_pending['explicit'] = ws_pending['legacy'] = 0\n", ""))
    return lambda: setattr(tp, 'worker', orig)


@case('an infinite or NaN rate passes validation',
      lambda: tp.test_price_table())
def _infinite_rate():
    # JSON parses Infinity and NaN; a table carrying one prices a response at inf or nan,
    # which reaches the page as "$nan" and the JSON as an invalid bare NaN.
    orig = pricing._rate_ok
    pricing._rate_ok = lambda v, required: (not required if v is None else
                                            isinstance(v, (int, float))
                                            and not isinstance(v, bool) and not v < 0)
    return lambda: setattr(pricing, '_rate_ok', orig)


@case('the Cyber models table is skipped',
      lambda: tp.test_fetch_prices_parser())
def _cyber_skipped():
    import types
    import fetch_prices
    path = fetch_prices.__file__
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    old = "                    cyber[got[0]] = {'standard': got[1]}\n"
    assert src.count(old) == 1
    mod = types.ModuleType('fetch_prices')
    mod.__file__ = path
    exec(compile(src.replace(old, "                    pass\n"), path, 'exec'), mod.__dict__)
    sys.modules['fetch_prices'] = mod
    return lambda: sys.modules.__setitem__('fetch_prices', fetch_prices)

def main():
    print(f'{len(CASES)} mutations\n')
    bad = 0
    for name, target, build in CASES:
        undo = build()                 # applies the mutation, returns its undo
        results = run(target)
        undo()
        failed = [n for n, ok, _ in results if not ok]
        if failed:
            print(f'[PASS] {name}\n        caught by: {failed[0]}')
        else:
            bad += 1
            print(f'[FAIL] {name}\n        NOTHING FAILED -- {len(results)} assertions all '
                  f'passed with the fix reverted')
    print(f'\n{len(CASES) - bad}/{len(CASES)} mutations caught')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
