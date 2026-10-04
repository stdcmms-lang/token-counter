"""Per-file extraction: parse, classify, tokenize, reconstruct prompts.

One rollout file is one unit of work, so a process pool parallelises the whole pipeline
rather than just the BPE step.  The result is a plain JSON-serialisable dict so it can be
cached verbatim (see :mod:`tokencounter.index`).

Prompt reconstruction is an *approximation* and is treated as one throughout: the rollout
records no request body, so the prompt for a response is taken to be everything written
before that response's own trailing run of output items.  The gap against reported
``input_tokens`` is published as the reconciliation residual and is never used to validate
the tokenizer (ARCHITECTURE.md sections 4.1 and 5.3).
"""
import bisect
import collections
import datetime
import hashlib
import os
import time

from . import classify, images as imagelib, rollout

# Bound peak memory: encode at most this many characters per batch, then keep only lengths.
BATCH_CHARS = 4 << 20
BATCH_ITEMS = 512
PREVIEW_CHARS = 120
HOT_ITEMS = 25

TOOL_CALLS = ('function_call', 'custom_tool_call', 'local_shell_call')
TOOL_OUTPUTS = ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output')

# Resolution of the per-file reported-usage curve.  A busy file emits a rate-limit snapshot
# on every response -- 6,000 of them in one file here -- and they are step-identical within
# a bucket, so keeping the last reading per quarter hour loses nothing visible on a chart
# spanning days and keeps the cached payload small.
RL_BUCKET_S = 900

# A file opens with a burst of records written back to back: its own header, and in a fork
# child the parent history it replays, all stamped with the child's creation time a
# millisecond or so apart (ARCHITECTURE.md section 2.5).  The burst ends at the first gap
# longer than this.  A request the server refuses needs a round trip, so a refusal of the
# file's own lands after the burst, and a replayed copy of the parent's lands inside it.
OPENING_BURST_GAP_S = 0.05


def _enc(vocab):
    from . import encoding
    return encoding.load(vocab)


def _token_lengths(texts, enc, num_threads):
    """Token counts for `texts`, in chunks, discarding the id lists as we go."""
    out = []
    i, n = 0, len(texts)
    while i < n:
        j, chars = i, 0
        while j < n and (j - i) < BATCH_ITEMS and chars < BATCH_CHARS:
            chars += len(texts[j])
            j += 1
        chunk = texts[i:j]
        if num_threads and num_threads > 1 and len(chunk) > 1:
            ids = enc.encode_ordinary_batch(chunk, num_threads=num_threads)
        else:
            ids = [enc.encode_ordinary(t) for t in chunk]
        out.extend(len(x) for x in ids)
        del ids
        i = j
    return out


def _hash(prev, payload_bytes):
    h = hashlib.blake2b(digest_size=8)
    h.update(prev)
    h.update(payload_bytes)
    return h.digest()


def _state_of(rec, usage):
    """Full comparison state, mirroring ledger.info_state for the legacy stream."""
    fields = ('input_tokens', 'cached_input_tokens', 'output_tokens',
              'reasoning_output_tokens', 'total_tokens')
    total = rec.get('total') or {}
    return (tuple(usage.get(f) for f in fields), tuple(total.get(f) for f in fields))


def _charged_stream(res):
    """Which usage stream the ledger will charge for this file.

    Mirrors ledger.build rule 1.  A function rather than an inline test so the mutation
    harness can revert it: counting both streams doubled every resend in a both-stream file
    and went unnoticed until an invariant check caught it.
    """
    return 'explicit' if res['explicit'] else 'legacy'


def _request_start(frozen_ts, frozen, anchor_ts):
    """When the request behind a usage record went out.

    The input-side stamp as it stood at the response's first output item, when there was
    one.  A function rather than an inline test so the mutation harness can revert it: a
    tool that finishes while its response is still streaming writes its output before the
    response's usage record, and taking the anchor at the usage record instead shortens that
    response by the tool's whole run.
    """
    return frozen_ts if frozen else anchor_ts


def _request_anchor(anchor_ts, compact_ts):
    """The later of the latest input-side stamp and the latest compaction.

    A compaction rebuilds the prompt, so the next request goes out only once it is done.
    On the explicit stream the compaction call has a usage record of its own, and the
    previous-response floor (`tokencounter.latency`) covers it; on the legacy stream it has
    none -- only a `compacted` record and an uncharged zero/zero snapshot -- and without
    this the next response is charged the compaction's whole run.  Applied when a response's
    first output arrives, so a compaction call with no output items keeps its own start.
    """
    if compact_ts is None or anchor_ts is None:
        return anchor_ts if compact_ts is None else compact_ts
    a, c = epoch(anchor_ts), epoch(compact_ts)
    return compact_ts if (a is not None and c is not None and c > a) else anchor_ts


def _service_tier(payload):
    """The processing tier a `thread_settings_applied` snapshot records for the thread.

    Codex writes the tier it will request -- ``priority`` for Fast mode, ``flex``, or its
    ``default`` sentinel -- and leaves the key out when none is set, which is a recorded
    standard, not an unknown.  ``None`` is kept for a record that is not such a snapshot, so
    a response with no snapshot before it stays distinguishable from one priced at standard.

    A thread's first turn has none: Codex persists a snapshot only once the rollout file
    exists, and the file is created after the first user message, so the turn-start
    snapshot is dropped (codex-rs `send_event_raw_without_materializing_rollout`; its test
    `initial_plugin_ids_use_turn_context_without_extra_settings_checkpoints` asserts zero).
    `_backfill_tier` covers that turn from the file's first snapshot.
    """
    ts = payload.get('thread_settings')
    if not isinstance(ts, dict):
        return None
    v = ts.get('service_tier')
    return v if isinstance(v, str) and v else 'default'


def _backfill_tier(res, first_tier):
    """Give the records before a file's first settings snapshot that snapshot's tier.

    They are the thread's first turn, whose own snapshot Codex never persists
    (`_service_tier`); the next snapshot is written when the second turn starts and carries
    the same setting unless the user changed it in between.  An inference, so each record
    says so (`tier_inferred`).  A file with no snapshot at all -- a one-turn `codex exec`
    run, or an older Codex -- keeps None: there is nothing to infer from.
    """
    if first_tier is None:
        return
    for stream in ('explicit', 'legacy'):
        for rec in res[stream]:
            if rec.get('tier') is None:
                rec['tier'] = first_tier
                rec['tier_inferred'] = True


def _ends_response(outer, payload):
    """Whether a record means no response is still in flight: a turn opening or aborting.

    The start frozen at a response's first output is otherwise cleared only by that
    response's usage record, and an interrupted response never writes one: the next turn's
    first response was then timed from the interrupted request, idle time and all.
    """
    if outer == 'turn_context':
        return True
    return outer == 'event_msg' and payload.get('type') == 'turn_aborted'


def _timing_side(payload):
    """``'input'``, ``'output'`` or None: which side of a model call an item is, for timing.

    Narrower than `classify.item_role`, which counts anything it does not recognise as
    input -- right for attributing prompt content, wrong for timing, where an unknown item
    written while a response streams (a web search the model ran, local bookkeeping) would
    pass for the request's start.  Only known inputs move the start; unknown items are
    ignored; anything the model emits (``*_call``) counts as output.
    """
    t = payload.get('type')
    if t == 'message':
        role = payload.get('role')
        if role == 'assistant':
            return 'output'
        return 'input' if role in ('user', 'developer', 'system') else None
    if not isinstance(t, str):
        return None
    if t.endswith('_output') or t == 'agent_message':
        return 'input'
    if t in classify.OUTPUT_ITEMS or t.endswith('_call'):
        return 'output'
    return None


def _is_model_call(rec, usage, prev_state, stream):
    """Whether this usage mark represents a distinct model call, for resend costing.

    Applies the *file-local* half of the ledger's rules (ARCHITECTURE.md 2.4, 2.5): a
    zero/zero context snapshot is not a response, and -- on the legacy stream only -- a
    record identical in both usage and cumulative counters to its immediate predecessor is a
    repeat.  The explicit stream is deduplicated by `response_id` instead, and two genuine
    explicit responses may legitimately report identical usage, so the repeat rule must not
    be applied to it.

    The cross-file half -- ancestor replay in fork children -- cannot be decided here, so a
    fork child's replayed prompts are still counted.
    """
    if ((usage.get('input_tokens') or 0) == 0 and (usage.get('output_tokens') or 0) == 0
            and (usage.get('total_tokens') or 0) > 0):
        return False
    if stream != 'legacy':
        return True
    return _state_of(rec, usage) != prev_state


def _lcp_at(seq, a0, a1, b0, b1):
    """Length of the common prefix of ``seq[a0:a1]`` and ``seq[b0:b1]``, without slicing."""
    n = min(a1 - a0, b1 - b0)
    i = 0
    while i < n and seq[a0 + i] == seq[b0 + i]:
        i += 1
    return i


def epoch(ts):
    """Seconds since the epoch for a rollout ISO-8601 timestamp, or None.

    Rate-limit windows are quoted as absolute unix seconds, so the two have to meet on one
    scale.  Record timestamps carry an explicit offset (``Z`` in every observed build); one
    without any offset is read as UTC rather than as local wall-clock, because a rollout is
    written in UTC and guessing the reader's zone would shift a window by hours.
    """
    if not isinstance(ts, str) or len(ts) < 16:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def note_rate_limits(rl, ts, acc, ctr):
    """Fold one server rate-limit snapshot into the per-file accumulator.

    This is the only figure in the system that is **reported rather than derived**: it does
    not come from re-tokenizing anything, and it is kept in its own structure, and labelled
    as such in the report, so it can never be mistaken for a measurement this tool made.

    Both slots are read, and a window is identified by ``window_minutes``, never by the slot
    name.  The weekly window has moved between them across CLI versions: in this corpus it
    is ``secondary`` behind a 5-hour ``primary`` in older records and ``primary`` alone in
    newer ones.  Keying on the name would chart a 5-hour window as a weekly one.

    Aggregation here is per exact ``resets_at``.  Grouping into real windows is deliberately
    *not* done at this level: while usage sits at 0% the server re-quotes ``resets_at`` as
    now-plus-seven-days on every call, so one idle window emits hundreds of distinct values,
    and only a corpus-wide view can tell a slide from a genuine new window
    (:func:`tokencounter.analyze.rate_limit_windows`).
    """
    if not isinstance(rl, dict):
        return
    ctr['rate_limit_snapshots'] += 1
    t = epoch(ts)
    usable = False
    for slot in ('primary', 'secondary'):
        w = rl.get(slot)
        if not isinstance(w, dict):
            continue
        wm = w.get('window_minutes')
        if not isinstance(wm, int) or isinstance(wm, bool) or wm <= 0:
            ctr['rate_limit_no_window'] += 1
            continue
        ra = w.get('resets_at')
        if isinstance(ra, bool) or not isinstance(ra, (int, float)):
            # Older builds quote a relative `resets_in_seconds`.  Anchoring it to the
            # record's own timestamp puts both shapes on one timeline instead of dropping
            # the older one, which would leave a hole in the window history.
            rs = w.get('resets_in_seconds')
            ra = (t + rs) if (isinstance(rs, (int, float)) and not isinstance(rs, bool)
                              and t is not None) else None
        if ra is None:
            ctr['rate_limit_no_reset'] += 1
            continue
        ra = int(ra)
        usable = True
        pct = w.get('used_percent')
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            pct = None
        key = f'{wm}:{ra}'
        a = acc.get(key)
        if a is None:
            a = acc[key] = {
                'window_minutes': wm, 'resets_at': ra, 'slot': slot,
                'first_ts': t, 'last_ts': t, 'n': 0,
                'first_pct': pct, 'last_pct': pct, 'min_pct': pct, 'max_pct': pct,
                'plan_type': rl.get('plan_type'), 'limit_id': rl.get('limit_id'),
                'reached': 0, 'reached_type': None, 'points': {},
            }
        a['n'] += 1
        if rl.get('rate_limit_reached_type'):
            a['reached'] += 1
            a['reached_type'] = rl.get('rate_limit_reached_type')
            ctr['rate_limit_reached'] += 1
        if t is not None:
            if a['first_ts'] is None or t < a['first_ts']:
                a['first_ts'], a['first_pct'] = t, pct
            if a['last_ts'] is None or t >= a['last_ts']:
                a['last_ts'], a['last_pct'] = t, pct
            if pct is not None:
                a['points'][int(t // RL_BUCKET_S)] = pct
        if pct is not None:
            a['min_pct'] = pct if a['min_pct'] is None else min(a['min_pct'], pct)
            a['max_pct'] = pct if a['max_pct'] is None else max(a['max_pct'], pct)
    if not usable:
        ctr['rate_limit_unusable'] += 1


def finish_rate_limits(acc):
    """Accumulator to a JSON-serialisable list, oldest reset first."""
    out = []
    for a in acc.values():
        w = dict(a)
        w['points'] = [[b * RL_BUCKET_S, p] for b, p in sorted(a['points'].items())]
        out.append(w)
    out.sort(key=lambda w: (w['resets_at'], w['window_minutes']))
    return out


def metrics_only(path):
    """Usage ledger inputs alone -- every line parsed, nothing tokenized."""
    return _extract(path, full=False)


def process(path, vocab=None, num_threads=1):
    """Full extraction: usage, content attribution, images, prompt reconstruction."""
    return _extract(path, full=True, vocab=vocab, num_threads=num_threads)


def _extract(path, full, vocab=None, num_threads=1):
    cpu0 = time.process_time()
    ctr = collections.Counter()
    res = {
        'path': path,
        'date': rollout.file_date(path),
        'session_id': None, 'thread_id': None, 'parent_thread_id': None,
        'started_at': None, 'cli_version': None, 'cwd': None, 'originator': None,
        'context_window': None, 'agent_role': None,
        'explicit': [], 'legacy': [],
        'models': {}, 'efforts': {},
        'cat_tokens': {}, 'cat_chars': {}, 'cat_items': {},
        'turns': [],
        'images': {'count': 0, 'lo': 0, 'hi': 0, 'unknown': 0, 'formats': {}, 'bytes': 0},
        'responses': [],
        'hot_items': [], 'resend_cost': 0, 'unique_tokens': 0,
        'rate_limits': [],
        # Every snapshot in which Codex logged a limit as reached, as epoch seconds, and
        # where the file's opening burst of records ends (OPENING_BURST_GAP_S): a fork
        # child's replayed copies of its parent's snapshots fall inside that burst.
        'limit_events': [], 'opening_burst_end': None,
        # Timing (ARCHITECTURE.md 5.8): when each turn opened, indexed by turn, and every
        # tool call as [name, call time in epoch seconds, seconds from the call to its output].
        'turn_starts': [], 'tool_times': [],
        # Every turn Codex logged as aborted (interrupted, replaced by a new turn, a review
        # ended, a budget limit), as epoch seconds.  A response cut off by one writes no
        # usage record, so its cost is not in any figure; these count the turns.
        'turn_aborts': [],
        'counters': {},
        'error': None,
    }
    try:
        st = os.stat(path)
        res['size'], res['mtime_ns'] = st.st_size, st.st_mtime_ns
        res['prefix_hash'] = rollout.prefix_hash(path)
    except OSError as exc:
        res['error'] = f'{exc.__class__.__name__}: {exc}'
        res['cpu_s'] = round(time.process_time() - cpu0, 4)
        return res

    # Every complete line is parsed, on both paths.  Skipping non-candidate lines saved 1.0s
    # across the corpus and made an object-shaped corrupt record invisible to the ledger.
    hints = None

    model = effort = None
    # The processing tier requested from here on (`_service_tier`), None until a snapshot,
    # and the first snapshot's, for the records before it (`_backfill_tier`).
    tier = first_tier = None
    # Web searches since each stream's last charged record.  Per stream, because a file with
    # both streams charges only one of them, and a search must land on the record charged.
    ws_pending = {'explicit': 0, 'legacy': 0}
    turn_index = -1
    seen_rid = set()
    call_names = {}
    prev_state = None
    prev_total_in = None
    rl_acc = {}                      # exact (window_minutes, resets_at) -> snapshot aggregate

    # When each request went out.  The rollout stamps a record when it is written, and has no
    # send time, but a request is sent as soon as its prompt is complete: the user's message
    # or the last tool output.  So the latest input-side record is the request's start --
    # as it stood when the response's FIRST output item arrived.  Freezing it there matters
    # because a tool can run while its response is still streaming, and its output, written
    # before the response's usage record, would otherwise pass for the request's start.
    anchor_ts = None                 # latest input-side record: a turn opening, or an input item
    compact_ts = None                # latest `compacted` record
    req_ts, req_frozen = None, False
    prev_full = None                 # previous legacy record's full state, as the ledger compares
    call_at = {}                     # call_id -> (tool name, call timestamp), awaiting output
    burst_open, burst_end = True, None

    # Deferred tokenization: collect texts across the file, encode once, then assemble.
    texts = []                       # every text destined for the tokenizer
    items = []                       # prompt-item skeletons, in file order
    pending = []                     # items not yet attributed to a response
    usage_marks = []                 # (stream, record index, pending item indices)

    enc = _enc(vocab) if full else None

    def add_item(kind, segs, imgs, role, tool=None):
        """Record one prompt item; its token count is filled in after batch encoding."""
        idx = []
        chars_by_cat = collections.Counter()
        preview = ''
        # Content digest, used to decide where two prompts diverge.  It must cover the actual
        # bytes: hashing only (kind, token count) would make two different items of equal
        # length collide, which lengthens the apparent common prefix and manufactures
        # cache-divergence leads.  Opaque segments count as content -- a different encrypted
        # reasoning blob is a different prompt.
        sig = hashlib.blake2b(digest_size=8)
        sig.update(kind.encode())
        for cat, txt in segs:
            sig.update(cat.encode())
            sig.update(len(txt).to_bytes(8, 'little'))
            sig.update(txt.encode('utf-8', 'replace'))
            if cat in classify.OPAQUE:
                chars_by_cat[cat] += len(txt)
                continue
            if not preview:
                preview = txt[:PREVIEW_CHARS]
            idx.append((cat, len(texts)))
            texts.append(txt)
        img_lo = img_hi = 0
        for url in imgs:
            est = imagelib.estimate(url)
            res['images']['count'] += 1
            res['images']['bytes'] += len(url) if isinstance(url, str) else 0
            f = est['format'] or 'unknown'
            res['images']['formats'][f] = res['images']['formats'].get(f, 0) + 1
            if est['format'] is None:
                res['images']['unknown'] += 1
            img_lo += est['lo'] or 0
            img_hi += est['hi'] if est['hi'] is not None else (est['lo'] or 0)
            sig.update(str(est['width']).encode())
            sig.update(str(est['height']).encode())
            # Dimensions, length and a prefix all still collide for two images that differ
            # only in the middle, so hash the whole payload.  It is 1.8 GB across the corpus
            # and roughly 2s spread over the pool -- cheap enough not to approximate.
            if isinstance(url, str):
                sig.update(len(url).to_bytes(8, 'little'))
                sig.update(url.encode('utf-8', 'replace'))
            else:
                sig.update(b'?')
        res['images']['lo'] += img_lo
        res['images']['hi'] += img_hi
        items.append({'kind': kind, 'role': role, 'idx': idx,
                      'opaque': dict(chars_by_cat), 'img_lo': img_lo, 'img_hi': img_hi,
                      'turn': turn_index, 'preview': preview[:PREVIEW_CHARS],
                      'tool': tool, 'sig': sig.digest()})
        pending.append(len(items) - 1)

    def records():
        """Iterate records, surviving a malformed one rather than failing the whole run.

        A pool worker that raises takes the entire 30-second run with it, and a single odd
        payload in 1,950 files is not worth that.  The failure is counted, not swallowed.
        """
        it = rollout.iter_records(path, hints, damage=ctr)
        while True:
            try:
                yield next(it)
            except StopIteration:
                return
            except Exception as exc:
                ctr['record_errors'] += 1
                res['error'] = res['error'] or f'{exc.__class__.__name__}: {exc}'
                return

    for outer, payload, ts, _raw_len in records():
        if burst_open:
            rt = epoch(ts)
            if rt is not None:
                if burst_end is not None and rt - burst_end > OPENING_BURST_GAP_S:
                    burst_open = False
                    # A fork child's opening burst is its parent's replayed history.  A
                    # search the parent made after its last usage record -- before an
                    # interruption -- would otherwise land on the child's first response,
                    # and the parent's own file already counts it.
                    if res['parent_thread_id']:
                        ws_pending['explicit'] = ws_pending['legacy'] = 0
                else:
                    burst_end = rt if burst_end is None else max(burst_end, rt)

        if outer == 'session_meta':
            if res['session_id'] is None:
                res['session_id'] = payload.get('session_id')
                res['thread_id'] = payload.get('id') or payload.get('session_id')
                res['started_at'] = ts or payload.get('timestamp')
                res['cli_version'] = payload.get('cli_version')
                res['cwd'] = payload.get('cwd')
                res['originator'] = payload.get('originator')
                res['context_window'] = payload.get('context_window')
                res['parent_thread_id'] = (payload.get('parent_thread_id')
                                           or payload.get('forked_from_id'))
                res['agent_role'] = payload.get('agent_role') or payload.get('agent_nickname')
                if payload.get('dynamic_tools'):
                    ctr['dynamic_tools_files'] = 1
                if full:
                    segs, imgs = classify.session_meta(payload)
                    if segs:
                        add_item('session_meta', segs, imgs, 'input')
            else:
                ctr['second_session_meta'] += 1
                if payload.get('id') and payload.get('id') != res['thread_id']:
                    ctr['fork_header'] += 1
            continue

        if outer == 'turn_context':
            turn_index += 1
            res['turn_starts'].append(ts)
            anchor_ts = ts
            if _ends_response(outer, payload):
                req_ts, req_frozen = None, False
            model = payload.get('model') or model
            effort = payload.get('effort') or effort
            if model:
                res['models'][model] = res['models'].get(model, 0) + 1
            if effort:
                res['efforts'][effort] = res['efforts'].get(effort, 0) + 1
            if not payload.get('root_turn_id'):
                ctr['turn_context_no_root'] += 1
            ctr['turn_context'] += 1
            continue

        if outer == 'world_state':
            anchor_ts = ts                          # part of the next prompt, like any input
            if full:
                segs, imgs = classify.world_state(payload)
                if segs:
                    add_item('world_state', segs, imgs, 'input')
            continue

        if outer == 'compacted':
            compact_ts = ts
            ctr['compacted'] += 1
            if payload.get('latest_token_usage_record'):
                ctr['compacted_usage_copies'] += 1      # saved state, never charged
            if full:
                rh = payload.get('replacement_history')
                if isinstance(rh, list):
                    items.append({'kind': 'COMPACT_RESET', 'role': 'input', 'idx': [],
                                  'opaque': {}, 'img_lo': 0, 'img_hi': 0,
                                  'turn': turn_index})
                    pending.append(len(items) - 1)
                    for el in rh:
                        if isinstance(el, dict):
                            segs, imgs = classify.response_item(el)
                            add_item(el.get('type') or 'message', segs, imgs,
                                     classify.item_role(el))
            continue

        if outer == 'token_usage_record':
            rid = payload.get('response_id')
            if rid is not None:
                if rid in seen_rid:
                    ctr['explicit_dup_response_id'] += 1
                    continue
                seen_rid.add(rid)
            u = payload.get('usage') or {}
            # A context snapshot is never charged, so searches wait for the next record.
            ws = 0
            if _is_model_call(None, u, None, 'explicit'):
                ws, ws_pending['explicit'] = ws_pending['explicit'], 0
            rec = {'usage': u, 'ts': ts, 'model': model, 'effort': effort,
                   'tier': tier, 'web_search': ws,
                   'response_id': rid, 'i': len(res['explicit']),
                   'req_ts': _request_start(req_ts, req_frozen, anchor_ts),
                   'turn': turn_index}
            req_ts, req_frozen = None, False
            res['explicit'].append(rec)
            usage_marks.append(('explicit', rec['i'], list(pending)))
            pending = []
            continue

        if outer == 'event_msg':
            et = payload.get('type')
            if et == 'thread_settings_applied':
                got = _service_tier(payload)
                if got is not None:
                    tier = got
                    if first_tier is None:
                        first_tier = got
                    ctr['thread_settings'] += 1
                continue
            if et == 'turn_aborted':
                rt = epoch(ts)
                if rt is None:
                    ctr['turn_abort_no_time'] += 1
                else:
                    res['turn_aborts'].append(round(rt, 3))
            if et != 'token_count':
                if _ends_response(outer, payload):
                    req_ts, req_frozen = None, False
                continue
            # Before the usage checks below, not after: a `token_count` with `info: null`
            # still carries a rate-limit snapshot, and there are enough of them that
            # reading limits only on the usage path would lose window boundaries.
            rl = payload.get('rate_limits')
            note_rate_limits(rl, ts, rl_acc, ctr)
            # One event a snapshot, whichever window it names: a refused request is one
            # refusal even when both windows are full.
            if isinstance(rl, dict) and rl.get('rate_limit_reached_type'):
                rt = epoch(ts)
                if rt is None:
                    ctr['limit_event_no_time'] += 1
                else:
                    res['limit_events'].append(round(rt, 3))
            info = payload.get('info')
            if not info:
                ctr['info_null'] += 1
                continue
            last = info.get('last_token_usage')
            if not last:
                ctr['info_no_last'] += 1
                continue
            total = info.get('total_token_usage') or {}
            ti = total.get('input_tokens')
            if prev_total_in is not None and ti is not None and ti < prev_total_in:
                ctr['cumulative_decrease'] += 1
            if ti is not None:
                prev_total_in = ti
            li = last.get('input_tokens') or 0
            lo_ = last.get('output_tokens') or 0
            lt = last.get('total_tokens') or 0
            if lt != li + lo_:
                ctr['arith_violation'] += 1
            state = (tuple(last.get(f) for f in ('input_tokens', 'output_tokens',
                                                 'total_tokens')),)
            if state == prev_state:
                ctr['raw_repeat'] += 1
            prev_state = state
            rec = {'last': last, 'total': total, 'ts': ts, 'model': model,
                   'effort': effort, 'tier': tier, 'web_search': 0,
                   'i': len(res['legacy']),
                   'req_ts': _request_start(req_ts, req_frozen, anchor_ts),
                   'turn': turn_index}
            # Only a record the ledger can charge ends a response.  A repeat of the previous
            # state (a rate-limit refresh re-sending the last usage) or a context snapshot
            # can arrive mid-stream, and clearing the frozen start there would date the next
            # charged response from whatever input came in between.
            full_state = _state_of(rec, last)
            if not (full_state == prev_full
                    or (li == 0 and lo_ == 0 and lt > 0)):
                req_ts, req_frozen = None, False
                rec['web_search'], ws_pending['legacy'] = ws_pending['legacy'], 0
            prev_full = full_state
            res['legacy'].append(rec)
            usage_marks.append(('legacy', rec['i'], list(pending)))
            pending = []
            continue

        if outer == 'response_item':
            t = payload.get('type')
            if t == 'web_search_call':
                ws_pending['explicit'] += 1
                ws_pending['legacy'] += 1
                ctr['web_search_calls'] += 1
            # Timing reads items on both passes; it needs only their side and their stamps.
            side = _timing_side(payload)
            if side == 'output':
                if not req_frozen:
                    req_ts, req_frozen = _request_anchor(anchor_ts, compact_ts), True
            elif side == 'input':
                anchor_ts = ts
            if t in TOOL_CALLS:
                cid = payload.get('call_id')
                if isinstance(cid, str) and cid:
                    name = payload.get('name')
                    call_at[cid] = (name if isinstance(name, str) and name else t, ts)
            elif t in TOOL_OUTPUTS:
                cid = payload.get('call_id')
                got = call_at.pop(cid, None) if isinstance(cid, str) else None
                if got is None:
                    ctr['tool_output_unmatched'] += 1
                else:
                    # The call's own time travels with it: a fork child's replayed history
                    # is stamped with the child's creation time, and only the reader of the
                    # ledger knows where the child's own work begins (tokencounter.latency).
                    a, b = epoch(got[1]), epoch(ts)
                    res['tool_times'].append(
                        [got[0], None if a is None else round(a, 3),
                         None if a is None or b is None else round(b - a, 3)])
            if not full:
                continue
            segs, imgs = classify.response_item(payload)
            tool = None
            if t in TOOL_CALLS:
                tool = payload.get('name')
                cid = payload.get('call_id')
                if cid and tool:
                    call_names[cid] = tool
            elif t in TOOL_OUTPUTS:
                tool = call_names.get(payload.get('call_id'))
            elif t == 'reasoning':
                for _s in (payload.get('summary') or []):
                    ctr['reasoning_summaries'] += 1
            add_item(t or 'other', segs, imgs, classify.item_role(payload), tool)
            continue

    _backfill_tier(res, first_tier)
    res['rate_limits'] = finish_rate_limits(rl_acc)
    res['opening_burst_end'] = None if burst_end is None else round(burst_end, 3)
    if call_at:
        # Interrupted, still running when the file was read, or its output never written.
        ctr['tool_no_output'] += len(call_at)

    if not res['explicit'] and not res['legacy']:
        ctr['no_usage_data'] = 1

    if full:
        try:
            lengths = _token_lengths(texts, enc, num_threads)
            _assemble(res, items, usage_marks, lengths, ctr)
        except Exception as exc:
            # Usage is already extracted and stays valid; only attribution is lost.
            ctr['attribution_errors'] += 1
            res['error'] = res['error'] or f'{exc.__class__.__name__}: {exc}'

    res['counters'] = dict(ctr)
    res['cpu_s'] = round(time.process_time() - cpu0, 4)
    return res


def _assemble(res, items, usage_marks, lengths, ctr):
    """Fill token counts, per-category and per-turn attribution, and prompt prefixes."""
    cat_tokens = collections.Counter()
    cat_chars = collections.Counter()
    cat_items = collections.Counter()
    turn_cat = collections.defaultdict(collections.Counter)
    unique_tokens = 0

    for it in items:
        tot = 0
        seen_cats = set()
        for cat, ti in it['idx']:
            n = lengths[ti]
            tot += n
            cat_tokens[cat] += n
            turn_cat[it['turn']][cat] += n
            seen_cats.add(cat)
        for cat, ch in it['opaque'].items():
            cat_chars[cat] += ch
            seen_cats.add(cat)
        if it['img_lo'] or it['img_hi']:
            cat_tokens['image'] += it['img_lo']
            turn_cat[it['turn']]['image'] += it['img_lo']
            seen_cats.add('image')
        for cat in seen_cats:
            cat_items[cat] += 1
        it['tokens'] = tot + it['img_lo']
        unique_tokens += it['tokens']

    # Chained content digests, one per prompt item.  Comparing two chains element-wise gives
    # the position where the prompts diverge.  The chain is continuous; a compaction starts a
    # fresh segment, so hashes either side of a reset never compare equal and a compacted
    # prompt correctly shows no stable prefix rather than a spurious one.
    chain, cum, seg_of = [], [], []
    running, seg = 0, 0
    prev = b'seg0'
    for it in items:
        if it['kind'] == 'COMPACT_RESET':
            seg += 1
            # Seed each segment distinctly.  Reseeding to a constant means identical content
            # either side of a compaction hashes identically, so a compacted prompt would
            # report a stable prefix that the provider never saw.
            running, prev = 0, f'seg{seg}'.encode()
            it['_reset'] = True
            it['_seg_base'] = len(chain)     # where this segment's first item will land
            continue
        prev = _hash(prev, it['sig'])
        chain.append(prev)
        running += it['tokens']
        cum.append(running)
        seg_of.append(seg)
        it['_pos'] = len(chain) - 1

    def seg_start(pos):
        """First chain index belonging to the same segment as `pos`."""
        s = seg_of[pos]
        lo, hi = 0, pos
        while lo < hi:
            mid = (lo + hi) // 2
            if seg_of[mid] < s:
                lo = mid + 1
            else:
                hi = mid
        return lo

    # Walk the usage marks, attributing prompts.
    p_start, p_end = 0, 0             # chain range currently in the prompt
    q_start, q_end = 0, 0             # the previous response's prompt range
    prompt_ranges = []
    idx_by_kind = {'explicit': res['explicit'], 'legacy': res['legacy']}
    # Resend costing must count each model call once.  In a both-stream file every response
    # appears on BOTH streams, so counting every usage mark doubled `resends` and
    # `resend_cost` for 602 of 1,952 files.  Count only the stream the ledger will charge --
    # the same preference rule as ledger.build (explicit when present).
    preferred = _charged_stream(res)
    prev_state = None
    for stream, ri, pend in usage_marks:
        rec = idx_by_kind[stream][ri]
        usage = rec.get('usage') or rec.get('last') or {}
        # This response's own output items are the trailing contiguous run of output items.
        split = len(pend)
        while split > 0 and items[pend[split - 1]]['role'] == 'output':
            split -= 1
        for i in pend[:split]:
            it = items[i]
            if it.get('_reset'):
                # A compaction empties the prompt; the new segment starts here even if no
                # item has landed in it yet.  Falling back to 0 put later responses in the
                # wrong segment and misattributed their resends to pre-compaction items.
                p_start = p_end = it['_seg_base']
            elif '_pos' in it:
                p_end = it['_pos'] + 1
        # `cum` restarts at zero in each segment, so no cross-segment subtraction is needed.
        recon = cum[p_end - 1] if p_end > p_start else 0
        if stream == preferred and _is_model_call(rec, usage, prev_state, stream):
            prompt_ranges.append((p_start, p_end))
        if stream == preferred:
            prev_state = _state_of(rec, usage)
        k = _lcp_at(chain, p_start, p_end, q_start, q_end)
        stable = cum[p_start + k - 1] if k else 0
        # The comparison baseline is the previous REQUEST's prompt, captured before this
        # response's own output is folded in -- extending it with the output overstated the
        # shared prefix by the size of the assistant turn, inflating cache-divergence leads.
        # And only the charged stream may move it: in a both-stream file the legacy mirror
        # lands right after the explicit record, by which point that output *has* been folded
        # in below, and capturing the baseline there put the overstatement back for every
        # both-stream file while the explicit-only test kept passing.
        if stream == preferred:
            q_start, q_end = p_start, p_end
        res['responses'].append({
            'ts': rec.get('ts'),
            'model': rec.get('model'),
            'effort': rec.get('effort'),
            'stream': stream,
            'index': ri,
            'recon_input': recon,
            'stable_prefix': stable,
            'prompt_items': p_end - p_start,
            'new_items': (p_end - p_start) - k,
            'reported_input': usage.get('input_tokens'),
            'reported_cached': usage.get('cached_input_tokens'),
        })
        # This response's own output is part of the NEXT request's prompt, so advance the
        # prompt range past it -- but the baseline above stays at the request boundary.
        for i in pend[split:]:
            it = items[i]
            if it.get('_reset'):
                p_start = p_end = it['_seg_base']
            elif '_pos' in it:
                p_end = it['_pos'] + 1

    # Per-item resend cost: tokens x number of prompts that carried the item.  This is the
    # actionable ranking -- a 20k-token tool output resent 40 times costs 800k input tokens.
    ends_by_start = collections.defaultdict(list)
    for s, e in prompt_ranges:
        ends_by_start[s].append(e)
    for v in ends_by_start.values():
        v.sort()
    hot = []
    for it in items:
        q = it.get('_pos')
        if q is None or not it.get('tokens'):
            continue
        ends = ends_by_start.get(seg_start(q))
        if not ends:
            continue
        resends = len(ends) - bisect.bisect_right(ends, q)
        if resends <= 0:
            continue
        cats = [c for c, _ in it['idx']] or list(it['opaque'])
        hot.append({'kind': it['kind'], 'tool': it.get('tool'),
                    'cat': collections.Counter(cats).most_common(1)[0][0] if cats else 'other',
                    'tokens': it['tokens'], 'resends': resends,
                    'cost': it['tokens'] * resends, 'turn': it['turn'],
                    'preview': it.get('preview') or ''})
    hot.sort(key=lambda h: -h['cost'])
    res['hot_items'] = hot[:HOT_ITEMS]
    res['resend_cost'] = sum(h['cost'] for h in hot)

    res['cat_tokens'] = dict(cat_tokens)
    res['cat_chars'] = dict(cat_chars)
    res['cat_items'] = dict(cat_items)
    res['unique_tokens'] = unique_tokens
    res['turns'] = [{'turn': t, 'cats': dict(c)} for t, c in sorted(turn_cat.items())]
