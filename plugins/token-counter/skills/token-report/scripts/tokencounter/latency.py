"""Response time, turn time and tool time, from the rollout's own timestamps.

A rollout stamps each record when it is written.  Nothing records when a request was sent,
but a request goes out as soon as its prompt is complete -- the user's message or the last
tool output -- so the worker keeps, for every usage record, the latest input-side record
before that response's first output item (``req_ts``).  A response's time is then its usage
record's stamp minus the later of that and the previous charged response's stamp.  It is one
number: network, the server's queue, reading the prompt and writing the answer, and any
retry, all together.  See ARCHITECTURE.md section 5.8.

The rollout cannot say how much of that was waiting in a queue.  What it can support is an
**estimate**, labelled as one everywhere it appears: per model and effort, fit the time the
fastest responses take for their amount of work -- a line under the lowest tenth, in output
tokens and uncached input tokens -- and call anything above that line time above the
fastest pace.  That excess is mostly queueing and retries, but a slow stretch of generation
and ordinary variation land there too, so it is never called queue time.

Only charged ledger rows are timed, so a repeated or context-snapshot record never becomes
a sample; a replayed row the ledger charges anyway (an ambiguous one-record match, or every
match under ``--no-replay-exclusion``) is flagged by the ledger and left out here.
"""
import collections
import datetime
import operator

from . import worker

# A response longer than this has a start that is not its request's: an anchor from before a
# sleep, or a record written late.  Excluded and counted, never clipped.
RESPONSE_CAP_S = 3600
# Tool time includes any wait for the user to approve the call, and a turn includes all of
# its tools, so both get a longer allowance -- but an approval left overnight is not latency.
TOOL_CAP_S = 2 * 3600
TURN_CAP_S = 2 * 3600

# The pace line sits under this share of responses: the time the work takes on a fast call.
FLOOR_Q = 0.10
# Fewer responses than this in a model and effort, and the line is not drawn for it.
FIT_MIN = 40
# Responses used to place the line, evenly spaced in time.  The fit is three numbers; four
# thousand samples pin them far more tightly than the timestamps' own jitter.
FIT_MAX = 4000
FIT_ITERS = 80
# A token column enters the fit only with this many distinct values: one response with
# uncached input among thousands without would otherwise set the input rate on its own.
FIT_DISTINCT = 10
TOOLS_KEPT = 50


def _pct(sorted_vals, p):
    """Linear-interpolated percentile of an ascending list, or None when empty."""
    n = len(sorted_vals)
    if not n:
        return None
    k = (n - 1) * p
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _summary(vals):
    s = sorted(vals)
    return {'n': len(s), 'total_s': round(sum(s), 3),
            'median_s': _r(_pct(s, .5)), 'p90_s': _r(_pct(s, .9))}


def _r(x, nd=3):
    return None if x is None else round(x, nd)


# --------------------------------------------------------------------------- the pace line

def _solve(a, b):
    """Solve a small dense system by Gaussian elimination; None when it is singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def _quantile_ls(ys, cols, q):
    """Linear quantile regression by iteratively reweighted least squares.

    The check loss weighs a residual above the line by `q` and one below by ``1 - q``;
    dividing by the residual's size turns that into a least-squares problem to iterate.
    `cols` are whole columns, so every sum runs as one ``sum(map(mul, ...))``.
    """
    k = len(cols)
    w = [1.0] * len(ys)
    eps = max(1e-3, 1e-4 * (_pct(sorted(ys), .5) or 0))
    beta = None
    for _ in range(FIT_ITERS):
        wx = [list(map(operator.mul, w, c)) for c in cols]
        a = [[0.0] * k for _ in range(k)]
        for i in range(k):
            for j in range(i, k):
                a[i][j] = a[j][i] = sum(map(operator.mul, wx[i], cols[j]))
        nb = _solve(a, [sum(map(operator.mul, wx[i], ys)) for i in range(k)])
        if nb is None:
            return beta
        done = beta is not None and all(abs(u - v) <= 1e-6 + 1e-5 * abs(v)
                                        for u, v in zip(nb, beta))
        beta = nb
        if done:
            break
        pred = [beta[0] * v for v in cols[0]]
        for bj, c in zip(beta[1:], cols[1:]):
            pred = [p + bj * v for p, v in zip(pred, c)]
        q1 = 1 - q
        w = [(q if y > p else q1) / (y - p if y - p > eps else p - y if p - y > eps else eps)
             for y, p in zip(ys, pred)]
    return beta


def floor_fit(samples, q=FLOOR_Q):
    """The pace line under the fastest `q` of responses: ``seconds = a + b*out + c*uncached``.

    `samples` are ``(seconds, output_tokens, uncached_input_tokens)``.  Every coefficient is
    held at zero or above -- a negative overhead or a negative cost per token is not a pace,
    it is the fit bending to noise -- by dropping the most negative term and refitting.
    Returns ``{'a', 'b', 'c'}`` in seconds and seconds per token, or None.
    """
    if len(samples) < 2:
        return None
    ys = [float(d) for d, _, _ in samples]
    # Thousands of tokens, so the three columns sit near one scale and the solve is stable.
    data = {0: [1.0] * len(samples),
            1: [o / 1000.0 for _, o, _ in samples],
            2: [u / 1000.0 for _, _, u in samples]}
    # A column that barely varies cannot be told from the intercept.
    terms = [0] + [t for t in (1, 2) if len(set(data[t])) >= FIT_DISTINCT]
    while terms:
        beta = _quantile_ls(ys, [data[t] for t in terms], q)
        if beta is None:
            # Singular: two columns that move together, or too few distinct points.  The
            # last token column goes, as a negative one would, rather than the whole fit.
            if len(terms) == 1:
                return None
            terms.pop()
            continue
        worst = min(zip(beta, terms))
        if worst[0] >= 0:
            got = dict(zip(terms, beta))
            return {'a': got.get(0, 0.0), 'b': got.get(1, 0.0) / 1000.0,
                    'c': got.get(2, 0.0) / 1000.0}
        terms.remove(worst[1])
    return None


def _evenly(items, n):
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


# --------------------------------------------------------------------------- the report

def _response_start(req, prev_end):
    """The later of the request's own stamp and the previous charged response's end.

    After a compaction, or two responses with no input between them, the latest input is
    from before the previous response, and the request only went out when that one was
    done.  Taking the input stamp alone charges this response with the previous one's time.
    """
    if req is None or prev_end is None:
        return prev_end if req is None else req
    return req if req > prev_end else prev_end


def _timed_start(req, prev_end):
    """A response's start, or None when it has no request stamp of its own.

    The previous response's end is a lower bound on a start, never a start by itself: alone
    it times the whole gap between two responses -- tools, the user typing -- and every row
    from an extractor that predates `req_ts` (an archived payload, say) would be one.
    """
    return None if req is None else _response_start(req, prev_end)


def _is_replay(row):
    """Whether the ledger matched this charged row as a fork child's replayed history."""
    return bool(row.get('replayed'))


def _own_work(call_t, live_start):
    """Whether a tool call belongs to this file's own work, not to replayed history.

    A fork child replays its parent's history stamped with the child's creation time, so
    its tool calls sit before the child's first timed response, a few milliseconds apart.
    """
    return call_t >= live_start


def _local(t, tz):
    try:
        return (datetime.datetime.fromtimestamp(t, tz) if tz is not None
                else datetime.datetime.fromtimestamp(t))
    except (OverflowError, OSError, ValueError):
        return None


def build(files, charged, tz=None, since=None):
    """``(latency model, data-quality counters)`` for the files in scope.

    `files` path -> FileResult, `charged` path -> the ledger's charged rows, in file order.
    `tz` pins the zone the hour-of-day and daily buckets are read in, for tests; ``None``
    is the machine's own, as everywhere else in the report.  `since`, in epoch seconds,
    leaves out responses that ended before it -- after they have served as the floor of the
    next one's start, which dropping their rows beforehand would lose.
    """
    q = collections.Counter()
    samples = []                        # (start, seconds, out, uncached, group, path, turn)
    turn_end = {}                       # (path, turn) -> latest response end
    tools = collections.defaultdict(list)

    for path in sorted(files):
        fr = files[path]
        rows = charged.get(path) or []
        prev_end = None
        live_start = None
        # Only a fork child replays history, so only its tool calls need the boundary.
        forked = bool(fr.get('parent_thread_id'))
        for r in rows:
            # Charged, but stamped with the child's creation time: not timed, and not where
            # the file's own work -- the replay boundary for its tool calls -- begins.
            if _is_replay(r):
                q['latency_replayed'] += 1
                continue
            end = worker.epoch(r.get('ts'))
            req = worker.epoch(r.get('req_ts'))
            start = _timed_start(req, prev_end)
            if end is not None:
                prev_end = end if prev_end is None else max(prev_end, end)
            if end is None:
                q['latency_no_end'] += 1
                continue
            if start is None:
                q['latency_no_start'] += 1
                continue
            if live_start is None:
                live_start = start
            if since is not None and end < since:
                continue
            d = end - start
            if d <= 0:
                q['latency_nonpositive'] += 1
                continue
            if d > RESPONSE_CAP_S:
                q['latency_over_cap'] += 1
                continue
            u = r.get('usage') or {}
            out = u.get('output_tokens') or 0
            unc = max(0, (u.get('input_tokens') or 0) - (u.get('cached_input_tokens') or 0))
            group = (r.get('model') or 'unknown', r.get('effort') or 'unknown')
            samples.append((start, d, out, unc, group, path, r.get('turn')))
            key = (path, r.get('turn'))
            turn_end[key] = max(turn_end.get(key, end), end)

        # A fork child with no timed response has no work of its own to set the replay
        # boundary with (`_own_work`), so its tool calls are counted and left out.  A file
        # that declares no parent replays nothing: a tool it ran before its first timed
        # response -- in a turn that was interrupted, say -- is its own.
        for name, call_t, d in (fr.get('tool_times') or []):
            if forked and live_start is None:
                q['tool_without_response'] += 1
                continue
            if call_t is None or d is None:
                q['tool_no_time'] += 1
            elif forked and not _own_work(call_t, live_start):
                q['tool_replayed'] += 1
            elif d <= 0:
                q['tool_nonpositive'] += 1
            elif d > TOOL_CAP_S:
                q['tool_over_cap'] += 1
            else:
                tools[name if isinstance(name, str) and name else 'unknown'].append(d)

    q['latency_samples'] = len(samples)
    if not samples:
        return ({'available': False,
                 'reason': 'no response could be timed' if any(charged.values())
                 else 'no charged responses in range'}, q)

    # -- the pace line, per model and effort ----------------------------------------------
    by_group = collections.defaultdict(list)          # group -> indices into `samples`
    for i, s in enumerate(samples):
        by_group[s[4]].append(i)
    fits = {}
    for g, idx in by_group.items():
        if len(idx) < FIT_MIN:
            continue
        picked = _evenly(sorted(idx, key=lambda i: samples[i][0]), FIT_MAX)
        f = floor_fit([samples[i][1:4] for i in picked])
        if f is not None:
            fits[g] = f

    # Time above the line, per response; None where its group has no line.
    above_of = []
    work = above = unfit = 0.0
    local_of = {}                      # quarter-hour bucket -> (local hour, local day)
    hours = collections.defaultdict(lambda: ([], []))
    days = collections.defaultdict(lambda: ([], []))
    for start, d, out, unc, g, _path, _turn in samples:
        f = fits.get(g)
        if f is None:
            unfit += d
            ab = None
        else:
            ab = max(0.0, d - (f['a'] + f['b'] * out + f['c'] * unc))
            work += d - ab
            above += ab
        above_of.append(ab)
        # One conversion per quarter hour, not per response: every zone in use offsets UTC
        # by a whole number of quarter hours, and changes its offset on that grid too, so
        # everything in one UTC quarter hour shares one local hour and one local day.
        hb = int(start // 900)
        lt = local_of.get(hb, False)
        if lt is False:
            t = _local(hb * 900, tz)
            lt = local_of[hb] = None if t is None else (t.hour, t.date().isoformat())
        if lt is not None:
            for bucket, k in ((hours, lt[0]), (days, lt[1])):
                bucket[k][0].append(d)
                if ab is not None:
                    bucket[k][1].append(ab)

    def split(idx):
        fitted = [i for i in idx if above_of[i] is not None]
        if not fitted:
            # No line, so no estimate: unavailable, never a measured zero.
            return {'above_s': None, 'above_share': None, 'median_above_s': None}
        ab = [above_of[i] for i in fitted]
        t = sum(samples[i][1] for i in fitted)
        return {'above_s': _r(sum(ab)), 'above_share': _r(sum(ab) / t, 4) if t else None,
                'median_above_s': _r(_pct(sorted(ab), .5))}

    groups = []
    for g, idx in by_group.items():
        f = fits.get(g)
        row = dict(_summary([samples[i][1] for i in idx]), model=g[0], effort=g[1],
                   output=sum(samples[i][2] for i in idx), **split(idx))
        row['fit'] = None if f is None else {
            'overhead_s': _r(f['a']),
            'output_tps': _r(1 / f['b'], 1) if f['b'] > 0 else None,
            'uncached_input_tps': _r(1 / f['c'], 0) if f['c'] > 0 else None,
            'samples': min(len(idx), FIT_MAX),
        }
        groups.append(row)
    groups.sort(key=lambda r: -r['total_s'])

    # -- turns: from the turn opening to its last response ---------------------------------
    model_in_turn = collections.Counter()
    for s in samples:
        model_in_turn[(s[5], s[6])] += s[1]
    turn_vals, turn_total, turn_model = [], 0.0, 0.0
    for (path, t), end in turn_end.items():
        starts = files[path].get('turn_starts') or []
        if t is None or t < 0 or t >= len(starts):
            q['turn_no_start'] += 1
            continue
        st = worker.epoch(starts[t])
        if st is None or end <= st:
            q['turn_no_start'] += 1
            continue
        d = end - st
        if d > TURN_CAP_S:
            q['turn_over_cap'] += 1
            continue
        turn_vals.append(d)
        turn_total += d
        # A response's start can precede its turn's stamp by the write order of the two
        # records; the model's share of a turn is never more than the turn.
        turn_model += min(d, model_in_turn[(path, t)])

    tool_rows = sorted((dict(_summary(v), tool=k) for k, v in tools.items()),
                       key=lambda r: -r['total_s'])
    q['tool_calls_timed'] = sum(len(v) for v in tools.values())

    allv = [s[1] for s in samples]
    lat = {
        'available': True,
        # `above_share` is over the fitted models' time only; `fitted_share` says how much
        # of all response time that is, so a headline built on it can say so.
        'responses': dict(_summary(allv), work_s=_r(work), above_s=_r(above),
                          unfit_s=_r(unfit),
                          above_share=_r(above / (work + above), 4) if work + above else None,
                          fitted_share=_r((work + above) / sum(allv), 4)),
        'groups': groups,
        'hours': [{'hour': h, 'n': len(v[0]), 'median_s': _r(_pct(sorted(v[0]), .5)),
                   'median_above_s': _r(_pct(sorted(v[1]), .5))}
                  for h, v in sorted(hours.items())],
        'daily': [{'date': k, 'n': len(v[0]), 'median_s': _r(_pct(sorted(v[0]), .5)),
                   'p90_s': _r(_pct(sorted(v[0]), .9)),
                   'median_above_s': _r(_pct(sorted(v[1]), .5))}
                  for k, v in sorted(days.items())],
        'turns': dict(_summary(turn_vals), model_s=_r(turn_model),
                      model_share=_r(turn_model / turn_total, 4) if turn_total else None),
        'tools': tool_rows[:TOOLS_KEPT],
        'tools_total': len(tool_rows),
        'method': {'floor_quantile': FLOOR_Q, 'fit_min': FIT_MIN, 'fit_max': FIT_MAX,
                   'response_cap_s': RESPONSE_CAP_S, 'tool_cap_s': TOOL_CAP_S,
                   'turn_cap_s': TURN_CAP_S},
    }
    return lat, q
