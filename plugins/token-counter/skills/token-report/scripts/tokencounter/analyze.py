"""Aggregation into the report model.

Everything here consumes the canonical ledger (:mod:`tokencounter.ledger`), never the raw
usage streams, so no figure can silently double-count.  Measurements and inferences are kept
in separate fields and labelled as such in the report; see ARCHITECTURE.md section 5.2.
"""
import bisect
import collections
import datetime
import functools
import re

from . import classify, latency, pricing, worker

# A response is a cache-divergence *lead* when a large stable prompt prefix was not covered
# by reported caching.  This is a hypothesis generator, not an attribution: the rollout
# records no cache key, no send timestamp and no TTL, so causation is unrecoverable.
LEAD_MIN_GAP = 20_000
LEAD_LIMIT = 40
DEEP_DIVE_SESSIONS = 30

# The weekly rate-limit window, in minutes, as the server quotes it.
ALWAYS_QUALITY = ('replay_exclusion_applied', 'damage_outside_window',
                  'unparseable_records', 'unparseable_usage_records', 'non_object_records',
                  'malformed_records', 'extraction_errors', 'archived_entries')

WEEKLY_MINUTES = 10080

# Two `resets_at` quotes this close describe the same window.  Measured, not guessed: across
# this corpus every window that ever reported non-zero usage holds its `resets_at` to within
# one minute for its whole life, while genuinely distinct windows are days apart.  An hour is
# two orders of magnitude above the observed jitter and two orders below the observed gap.
CLUSTER_TOL_S = 3600
# How far past its successor's opening a window may still be drawn.  Zero: the server keeps
# reporting a replaced window to some sessions -- on this corpus one session was still being
# served the previous week 1.6 days into the next one -- and drawing both puts two limit
# curves on the chart at the same moment.  Tunable only so its removal can be tested.
LATE_TOL_S = 0

# Points kept per window on the cumulative chart, after bucketing.  The chart is 980px wide
# and shares that width across every window drawn, so a window gets ~60px: 120 points is
# already far denser than the display can show, and the series are embedded in the page.
CHART_BUCKET_S = 900
MAX_POINTS = 120
MAX_CHART_WINDOWS = 16

# Tokenized content is deduplicated per file, so a file is the finest unit its categories can
# honestly be placed on the timeline: they are attributed to the hour the file starts.  The
# bucket exists so the series stays bounded -- a corpus of thousands of files collapses to the
# hours that were actually worked, and the report page filters it as the charts are zoomed.
CAT_BUCKET_S = 3600


_OFFSET_RE = re.compile(r'(Z|[+-]\d{2}:?\d{2})$')


@functools.lru_cache(maxsize=1 << 18)
def _local_day(minute_prefix, offset):
    """Local calendar day for an ISO-8601 minute plus its UTC offset.

    Rollout record timestamps are UTC (``...Z``) but the directory layout and file names use
    **local** dates, and `--since`/`--until` filter on those.  Bucketing on the raw prefix
    would put a 00:24 local session in the previous day for anyone east of Greenwich.
    Any explicit offset is honoured, not just ``Z`` -- treating ``-04:00`` as wall-clock put
    it on the wrong day.  Resolution is the **minute**: truncating to the hour first was also
    wrong, because a half-hour offset such as ``-04:30`` then lands a day early.  Memoised, so
    136k responses cost far fewer conversions.
    """
    if offset is None:
        return minute_prefix[:10]        # no offset at all: take the date as written
    try:
        dt = datetime.datetime.fromisoformat(minute_prefix + ':00' + offset)
    except ValueError:
        return None
    return dt.astimezone().date().isoformat()


def _day_span(date_str, tz=None):
    """Local midnight to local midnight for a ``YYYY-MM-DD`` bucket, as unix seconds.

    Daily buckets are *local* calendar days (:func:`_local_day`), so both ends are taken from
    the calendar -- the naive date, and the naive date plus one -- and localised only then.
    Adding a day to the *localised* start was the first version, and on the fixed offset
    ``astimezone()`` attaches that is +86,400 s: an hour late on the day the clock springs
    forward and an hour early on the day it falls back, so the bar overlapped or gapped its
    neighbour twice a year.  `tz` pins the zone for tests; ``None`` is the machine's.
    """
    try:
        d = datetime.date.fromisoformat(str(date_str))
    except (ValueError, TypeError):
        return None, None               # 'unknown', or a date the filename never carried

    def local(naive):
        return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()

    try:
        start = datetime.datetime(d.year, d.month, d.day)
        return (local(start).timestamp(),
                local(start + datetime.timedelta(days=1)).timestamp())
    except (OverflowError, OSError, ValueError):
        return None, None


def _day(ts, fallback=None):
    if isinstance(ts, str) and len(ts) >= 16:
        m = _OFFSET_RE.search(ts)
        if m:
            off = '+00:00' if m.group(1) == 'Z' else m.group(1)
            if len(off) == 5:            # +0800 -> +08:00
                off = off[:3] + ':' + off[3:]
        else:
            off = None
        got = _local_day(ts[:16], off)
        if got:
            return got
    return fallback


def _charged_keys(fr, rows):
    return {(r['stream'], r['index']) for r in rows if r.get('index') is not None}


def tiktoken_inputs(fr):
    """``(stream, index) -> tokens`` in each response's reconstructed prompt, counted with
    tiktoken (§5.7).  Empty for a file extracted without the tokenizer, whose responses then
    keep the input Codex recorded.

    A prompt that reconstructs to nothing is left out too.  Every request carries at least
    its instructions, so zero means the reconstruction found no prompt (an empty range, or
    only opaque items), not that the request was free: counting it would drop the response's
    input silently, where leaving it out keeps Codex's figure and counts the fallback.
    """
    return {(r.get('stream'), r.get('index')): r['recon_input']
            for r in (fr.get('responses') or []) if r.get('recon_input')}


def _iso(epoch_s):
    """Local-time ISO string for a unix timestamp.  Windows are read by a human, in their
    own zone; the raw epoch is kept beside it for anything that needs to compute."""
    if epoch_s is None:
        return None
    try:
        return (datetime.datetime.fromtimestamp(epoch_s)
                .astimezone().isoformat(timespec='minutes'))
    except (OverflowError, OSError, ValueError):
        return None


def _merge_window_quotes(files):
    """All rate-limit quotes in scope, merged across files by exact ``resets_at``."""
    merged = {}
    for fr in files.values():
        for w in (fr.get('rate_limits') or []):
            key = (w.get('window_minutes'), w.get('resets_at'))
            if key[0] is None or key[1] is None:
                continue
            m = merged.get(key)
            if m is None:
                m = merged[key] = {
                    'window_minutes': key[0], 'resets_at': key[1], 'slot': w.get('slot'),
                    'first_ts': w.get('first_ts'), 'last_ts': w.get('last_ts'),
                    'first_pct': w.get('first_pct'), 'last_pct': w.get('last_pct'),
                    'max_pct': w.get('max_pct'), 'min_pct': w.get('min_pct'),
                    'n': 0, 'files': 0, 'reached': 0, 'reached_type': None,
                    'plan_type': w.get('plan_type'), 'limit_id': w.get('limit_id'),
                    'points': {},
                }
            m['n'] += w.get('n') or 0
            m['files'] += 1
            m['reached'] += w.get('reached') or 0
            m['reached_type'] = m['reached_type'] or w.get('reached_type')
            for field, better in (('first_ts', min), ('last_ts', max)):
                a, b = m[field], w.get(field)
                if b is not None:
                    if a is None:
                        m[field] = b
                    elif better(a, b) == b and a != b:
                        m[field] = b
                        m['first_pct' if field == 'first_ts' else 'last_pct'] = (
                            w.get('first_pct' if field == 'first_ts' else 'last_pct'))
            for field, better in (('max_pct', max), ('min_pct', min)):
                b = w.get(field)
                if b is not None:
                    m[field] = b if m[field] is None else better(m[field], b)
            for t, pct in (w.get('points') or []):
                # Two files writing the same bucket agree on the server's answer, so last
                # writer wins rather than averaging a step function into a slope.
                m['points'][t] = pct
    return merged


def _cluster(quotes):
    """Group ``resets_at`` quotes into the windows they actually describe.

    The server does not hold ``resets_at`` still.  While the weekly limit reads 0% it
    re-quotes the reset as *now plus seven days* on every single call, so one untouched
    window emits hundreds of distinct values -- 1,355 of them across this corpus for roughly
    thirty real windows.  Keying a window on the raw integer therefore shatters it.

    Quotes within :data:`CLUSTER_TOL_S` are chained into one cluster.  A cluster that never
    reported usage above 0% is an idle slide, not a window that was consumed, and is flagged
    so the chart does not draw a segment for a week that never happened.
    """
    out = []
    for q in sorted(quotes, key=lambda q: q['resets_at']):
        if out and q['resets_at'] - out[-1]['resets_at_max'] <= CLUSTER_TOL_S:
            c = out[-1]
        else:
            c = {'resets_at_min': q['resets_at'], 'resets_at_max': q['resets_at'],
                 'first_ts': None, 'last_ts': None, 'first_pct': None, 'last_pct': None,
                 'peak_pct': None, 'observations': 0, 'files': 0, 'quotes': 0,
                 'reached': 0, 'reached_type': None, 'plan_type': None, 'limit_id': None,
                 'window_minutes': q['window_minutes'], 'points': {}}
            out.append(c)
        c['resets_at_min'] = min(c['resets_at_min'], q['resets_at'])
        c['resets_at_max'] = max(c['resets_at_max'], q['resets_at'])
        c['observations'] += q['n']
        c['files'] += q['files']
        c['quotes'] += 1
        c['reached'] += q['reached']
        c['reached_type'] = c['reached_type'] or q.get('reached_type')
        c['plan_type'] = q.get('plan_type') or c['plan_type']
        c['limit_id'] = q.get('limit_id') or c['limit_id']
        if q['max_pct'] is not None:
            c['peak_pct'] = (q['max_pct'] if c['peak_pct'] is None
                             else max(c['peak_pct'], q['max_pct']))
        if q['first_ts'] is not None and (c['first_ts'] is None or q['first_ts'] < c['first_ts']):
            c['first_ts'], c['first_pct'] = q['first_ts'], q['first_pct']
        if q['last_ts'] is not None and (c['last_ts'] is None or q['last_ts'] > c['last_ts']):
            c['last_ts'], c['last_pct'] = q['last_ts'], q['last_pct']
        c['points'].update(q['points'])
    for c in out:
        c['idle'] = not c['peak_pct']
        # A consumed window freezes its reset, so the earliest quote carries the true
        # anchor; an idle window's anchor slides with the clock and is not reported.
        c['anchor'] = (None if c['idle']
                       else c['resets_at_min'] - c['window_minutes'] * 60)
    return out


def _downsample(points, limit=MAX_POINTS):
    """Keep at most `limit` points, preserving the first and last."""
    if len(points) <= limit:
        return points
    step = len(points) / float(limit - 1)
    keep = [points[int(i * step)] for i in range(limit - 1)]
    keep.append(points[-1])
    return keep


def rate_limit_windows(files, responses, now=None, newest=MAX_CHART_WINDOWS, usd=None):
    """Reported weekly-limit windows, with locally measured token usage inside each.

    Two independent series meet here and are deliberately never combined into one number:

    * ``peak_pct`` and the percentage curve are **reported by the server**.  Nothing in this
      tool derives them, and no token count is translated into them.
    * ``tokens`` and the cumulative curve are **measured here**, from the canonical ledger:
      Codex's recorded input, cached and output, plus the tiktoken count of the input when
      the responses carry one.

    Placing them on one time axis lets the reader see how a week was spent without this tool
    asserting a tokens-per-percent exchange rate it cannot know -- the limit's own unit is
    unpublished, and weighting by model, effort and cache state is not visible in a rollout.

    A window boundary is taken from **where the reported percentage drops**, not from
    arithmetic on ``resets_at``.  Resets in this corpus are frequently early: a window
    reading 99% is replaced, inside a single rollout file and one minute later, by a fresh
    window reading 0%, with a reset seven days out from that instant rather than from the
    previous window's. A seven-day grid would put every boundary in the wrong place.

    `responses` is ``[(epoch, input, cached, output), ...]`` for charged responses, with a
    fifth element, the tiktoken count of the response's input, when the report counts input
    with tiktoken.  Then each window's ``tokens`` gains ``tiktoken_input`` and each
    ``cum_points`` row a fifth column holding its running total.  `newest` bounds how many
    of the newest windows are returned (the chart's worth by default); ``None`` returns all
    of them.

    `usd` is ``[(epoch, dollars), ...]``, each charged response's API value, with dollars
    ``None`` for a response that could not be priced.  Each window gains ``usd`` and
    ``usd_points``, its running total ``[t, dollars]``, split at the same boundaries as the
    tokens.  A window none of whose responses was priced has ``usd`` None and no points,
    rather than a total of $0.
    """
    now = now if now is not None else datetime.datetime.now().timestamp()
    merged = _merge_window_quotes(files)
    if not merged:
        return {'available': False, 'reason': 'no rate-limit snapshots in range',
                'windows': [], 'current': None, 'observations': 0}

    lengths = collections.Counter()
    for (wm, _), q in merged.items():
        lengths[wm] += q['n']
    target = WEEKLY_MINUTES if WEEKLY_MINUTES in lengths else max(lengths)
    weekly = [q for (wm, _), q in merged.items() if wm == target]
    others = [{'window_minutes': wm, 'observations': n}
              for wm, n in sorted(lengths.items()) if wm != target]

    clusters = _cluster(weekly)
    live = [c for c in clusters if not c['idle']]
    live.sort(key=lambda c: (c['first_ts'] is None, c['first_ts']))

    # The boundary is the instant the replacement window is first seen, except for the
    # oldest window in range, whose predecessor may sit outside the corpus entirely: there
    # the reported anchor is the best available start, and the first observation bounds it.
    for i, c in enumerate(live):
        if i == 0:
            c['reset_at'] = (min(x for x in (c['anchor'], c['first_ts']) if x is not None)
                             if (c['anchor'] or c['first_ts']) else None)
            c['reset_inferred'] = True
        else:
            c['reset_at'] = c['first_ts']
            c['reset_inferred'] = False

    overlapping = sum(1 for a, b in zip(live, live[1:])
                      if a['last_ts'] and b['first_ts'] and b['first_ts'] < a['last_ts'])

    # A boundary is only credible if the replacement window opens *lower* than the window it
    # replaces: that drop is the reset.  A split where the percentage keeps climbing is more
    # likely one window whose quoted reset moved far enough to escape the cluster tolerance,
    # and the cumulative curve would then restart mid-week for no reason.  Counted and
    # published rather than quietly merged, because the logs cannot settle which it was.
    no_drop = sum(1 for a, b in zip(live, live[1:])
                  if a['last_pct'] is not None and b['first_pct'] is not None
                  and b['first_pct'] >= a['last_pct'])

    # The percentage series is clipped where the next window opens, which is the rule the
    # token attribution below already follows.  A reading that arrives for a window after
    # its successor was reported is the server contradicting itself; it is counted here and
    # published rather than drawn, because the logs cannot settle which reading was right.
    late_total = 0
    for i, c in enumerate(live):
        c['late_points'], c['late_peak'] = 0, None
        nxt = live[i + 1]['reset_at'] if i + 1 < len(live) else None
        if nxt is None:
            continue
        keep = {}
        for t, p in c['points'].items():
            if t <= nxt + LATE_TOL_S:
                keep[t] = p
            else:
                c['late_points'] += 1
                c['late_peak'] = p if c['late_peak'] is None else max(c['late_peak'], p)
        c['points'] = keep
        late_total += c['late_points']

    # `bounds` carries each boundary with the window it belongs to.  A live window with no
    # readable timestamp has no `reset_at`; leaving it out of a bare list of boundaries
    # shifted every later window's tokens one slot back, silently.
    bounds = [(c['reset_at'], i) for i, c in enumerate(live) if c['reset_at'] is not None]
    keys = [b for b, _ in bounds]
    acc = [collections.Counter() for _ in live]
    series = [[] for _ in live]
    counted = any(len(r) > 4 for r in responses)
    for r in _in_time_order(responses):
        if not keys:
            break
        t, inp, cch, out = r[:4]
        j = bisect.bisect_right(keys, t) - 1
        if j < 0:
            continue          # charged before the oldest window in range: not attributable
        i = bounds[j][1]
        a = acc[i]
        a['input'] += inp
        a['cached'] += cch
        a['output'] += out
        a['responses'] += 1
        point = (t, a['input'], a['input'] - a['cached'], a['output'])
        if counted:
            a['tiktoken_input'] += r[4] if len(r) > 4 else inp
            point += (a['tiktoken_input'],)
        series[i].append(point)

    usd_acc = [0.0 for _ in live]
    usd_seen = [False for _ in live]
    usd_series = [[] for _ in live]
    for t, v in _in_time_order(usd or []):
        if not keys:
            break
        j = bisect.bisect_right(keys, t) - 1
        if j < 0:
            continue
        i = bounds[j][1]
        usd_acc[i] += v or 0.0
        usd_seen[i] = usd_seen[i] or v is not None
        usd_series[i].append((t, usd_acc[i]))

    out_windows = []
    for i, c in enumerate(live):
        pts = [[int(p[0])] + list(p[1:]) for p in _bucket_last(series[i])]
        pct = _downsample(sorted([int(t), p] for t, p in c['points'].items()))
        a = acc[i]
        out_windows.append({
            'index': i,
            'reset_at': c['reset_at'], 'reset_at_iso': _iso(c['reset_at']),
            'reset_inferred': c['reset_inferred'],
            'resets_at': c['resets_at_max'], 'resets_at_iso': _iso(c['resets_at_max']),
            'anchor': c['anchor'], 'anchor_iso': _iso(c['anchor']),
            'slide_s': c['resets_at_max'] - c['resets_at_min'],
            'first_ts': c['first_ts'], 'last_ts': c['last_ts'],
            'first_ts_iso': _iso(c['first_ts']), 'last_ts_iso': _iso(c['last_ts']),
            'peak_pct': c['peak_pct'], 'last_pct': c['last_pct'],
            'first_pct': c['first_pct'],
            'observations': c['observations'], 'files': c['files'], 'quotes': c['quotes'],
            'late_points': c['late_points'], 'late_peak': c['late_peak'],
            'plan_type': c['plan_type'], 'limit_id': c['limit_id'],
            'reached': c['reached'], 'reached_type': c['reached_type'],
            'expired': bool(c['resets_at_max'] and c['resets_at_max'] < now),
            'tokens': dict({'input': a['input'], 'cached': a['cached'],
                            'uncached': a['input'] - a['cached'], 'output': a['output'],
                            'responses': a['responses']},
                           **({'tiktoken_input': a['tiktoken_input']} if counted else {})),
            'pct_points': pct,
            'cum_points': _downsample(pts),
            'usd': round(usd_acc[i], 6) if usd_seen[i] else None,
            'usd_points': _downsample([[int(p[0]), round(p[1], 6)]
                                       for p in _bucket_last(usd_series[i])])
                          if usd_seen[i] else [],
        })

    idle = [c for c in clusters if c['idle']]
    latest_idle = max(idle, key=lambda c: c['last_ts'] or 0) if idle else None
    current = out_windows[-1] if out_windows else None
    # An idle slide seen *after* the last consumed window is the live state: the limit has
    # been reset and nothing has been spent against it yet.
    if latest_idle and current and latest_idle['last_ts'] and current['last_ts'] and \
            latest_idle['last_ts'] > current['last_ts']:
        current = {'index': None, 'idle': True,
                   'reset_at': latest_idle['first_ts'],
                   'reset_at_iso': _iso(latest_idle['first_ts']),
                   'reset_inferred': True,
                   'resets_at': latest_idle['resets_at_max'],
                   'resets_at_iso': _iso(latest_idle['resets_at_max']),
                   'peak_pct': 0.0, 'last_pct': 0.0,
                   'observations': latest_idle['observations'],
                   'plan_type': latest_idle['plan_type'],
                   'tokens': {'input': 0, 'cached': 0, 'uncached': 0, 'output': 0,
                              'responses': 0},
                   'expired': bool(latest_idle['resets_at_max']
                                   and latest_idle['resets_at_max'] < now)}

    return {
        'available': True,
        'window_minutes': target,
        'weekly': target == WEEKLY_MINUTES,
        'windows': out_windows if newest is None else out_windows[-newest:],
        'windows_total': len(out_windows),
        'current': current,
        'observations': sum(q['n'] for q in weekly),
        'quotes': len(weekly),
        'idle_windows': len(idle),
        'overlapping': overlapping,
        'late_readings': late_total,
        'boundary_without_drop': no_drop,
        'other_windows': others,
        'plans': dict(collections.Counter(
            c['plan_type'] for c in live if c['plan_type'])),
        'now': now,
    }


def replay_end(fr):
    """Where a file's replayed history ends, in epoch seconds, or ``None`` when it has none.

    A fork child replays its parent's records stamped with its own creation time (section
    2.5), all inside the file's opening burst, so in a file that declares a parent a record
    stamped at or before the burst's end is the parent's, already counted in the parent's
    file.  One rule for every per-record signal read here; the worker applies the same one
    to rate-limit snapshots as it reads them.
    """
    return fr.get('opening_burst_end') if fr.get('parent_thread_id') else None


def limit_events(files, tz=None):
    """``(events, data-quality counters)``: the snapshots in which Codex logged a rate limit
    as reached, counted per local day.

    Reported, like the window percentages: an event is a snapshot whose
    ``rate_limit_reached_type`` is set, one per snapshot whichever window it names.  A fork
    child replays its parent's snapshots stamped with its own creation time (section 2.5),
    so in a file that declares a parent, an event inside the file's opening burst of
    records is the parent's, already counted in the parent's file, and is left out and
    counted as ``limit_events_replayed``.  `tz` pins the zone the days are read in, for
    tests; ``None`` is the machine's, as everywhere else in the report.
    """
    q = collections.Counter()
    days = collections.Counter()
    for _path, fr in sorted(files.items()):
        burst = replay_end(fr)
        for t in (fr.get('limit_events') or []):
            if burst is not None and t <= burst:
                q['limit_events_replayed'] += 1
                continue
            try:
                dt = (datetime.datetime.fromtimestamp(t, tz) if tz is not None
                      else datetime.datetime.fromtimestamp(t))
            except (OverflowError, OSError, ValueError, TypeError):
                q['limit_event_no_time'] += 1
                continue
            days[dt.date().isoformat()] += 1
    q['limit_events'] = sum(days.values())
    return ({'total': q['limit_events'],
             'daily': [{'date': d, 'n': n, 'start': _day_span(d, tz)[0],
                        'end': _day_span(d, tz)[1]}
                       for d, n in sorted(days.items())]}, q)


def turn_aborts(files):
    """``(aborted turns, replayed copies left out)``: the turns Codex logged as aborted --
    interrupted, replaced by a new turn, a review ended, or a budget limit reached.

    A response cut off by an abort never writes a usage record, so whatever it cost is in no
    figure; this is how many turns that could have happened in.  (A stream that drops is
    retried, and one that finally fails is logged as an error, not as an abort.)  A fork child
    replays its parent's records inside its opening burst, so in a file that declares a
    parent, an abort inside that burst is the parent's and is left out, as `limit_events`
    does.
    """
    n = replayed = 0
    for _path, fr in sorted(files.items()):
        burst = replay_end(fr)
        for t in (fr.get('turn_aborts') or []):
            if burst is not None and t <= burst:
                replayed += 1
            else:
                n += 1
    return n, replayed


# Unpriced models listed in `api_value`, most responses first.
UNPRICED_MODELS = 20


class ApiValue:
    """The API list-price value of charged rows, accumulated one row at a time.

    Priced per response by :mod:`tokencounter.pricing`; a response it cannot price is
    counted with its reason and its tokens, never folded in at a guessed rate.  `loaded` is
    what :func:`pricing.load` returns: ``(table, None)`` or ``(None, reason)``.
    """

    def __init__(self, loaded):
        self.table, self.reason = loaded
        self.usd = 0.0
        self.ws_usd = 0.0
        self.ws_calls = 0
        self.priced = 0
        self.unpriced = collections.Counter()
        self.unpriced_models = collections.defaultdict(collections.Counter)
        self.tiers = collections.Counter()
        self.tier_unrecorded = 0
        self.tier_inferred = 0
        # The same responses with every unrecorded tier priced at Fast, where the model has a
        # Fast rate: Codex writes no tier for a one-turn thread, and Fast costs 2x or more.
        self.usd_high = 0.0
        self.long = 0
        self.by_model = collections.defaultdict(collections.Counter)

    @property
    def on(self):
        return self.table is not None

    def add(self, r):
        """Price one charged row: its dollars, web search fees included, or None when there
        is no price table.  An unpriced row still adds its web search fees."""
        if self.table is None:
            return None
        n_ws = pricing._n(r.get('web_search'))
        ws = pricing.web_search(self.table, r)
        self.ws_calls += n_ws
        self.ws_usd += ws
        usd, why = pricing.price(self.table, r)
        if why:
            u = r.get('usage') or {}
            self.unpriced[why] += 1
            um = self.unpriced_models[(r.get('model') or 'unknown', why)]
            um['responses'] += 1
            um['input'] += pricing._n(u.get('input_tokens'))
            um['output'] += pricing._n(u.get('output_tokens'))
            return ws
        self.priced += 1
        self.usd += usd
        self.tiers[pricing.tier_name(r.get('tier'))] += 1
        if r.get('tier_inferred'):
            self.tier_inferred += 1
        if r.get('tier') is None:
            self.tier_unrecorded += 1
            fast, why_fast = pricing.price(self.table, dict(r, tier='priority'))
            self.usd_high += usd if why_fast else max(fast, usd)
        else:
            self.usd_high += usd
        if pricing.is_long(self.table, r):
            self.long += 1
        bm = self.by_model[pricing.lookup(self.table, r.get('model'))]
        bm['usd'] += usd
        bm['responses'] += 1
        return usd + ws

    def model(self, files):
        aborted, aborted_replayed = turn_aborts(files)
        if self.table is None:
            return {'available': False, 'reason': self.reason,
                    'aborted_turns': aborted}
        unpriced = sum(self.unpriced.values())
        out = {
            'available': self.priced > 0,
            'reason': None,
            # Dollars: priced responses' tokens plus every web search's per-call fee.
            'usd': round(self.usd + self.ws_usd, 6),
            # `usd` with every response of unrecorded tier priced at Fast instead: the most
            # the same responses could have cost.  Equal to `usd` when every tier is known.
            'usd_high': round(self.usd_high + self.ws_usd, 6),
            'tokens_usd': round(self.usd, 6),
            'web_search_usd': round(self.ws_usd, 6),
            'web_search_calls': self.ws_calls,
            'prices': {'as_of': self.table.get('as_of'), 'source': self.table.get('source'),
                       'models': len(self.table['models']),
                       'default': self.table.get('path') == pricing.DEFAULT_PRICES},
            'responses': self.priced + unpriced,
            'priced': self.priced,
            'unpriced': unpriced,
            'unpriced_by_reason': dict(self.unpriced),
            'unpriced_models': [
                dict(c, model=m, reason=why) for (m, why), c in sorted(
                    self.unpriced_models.items(), key=lambda kv: -kv[1]['responses'])
            ][:UNPRICED_MODELS],
            # Priced at standard: their file holds no tier snapshot at all.
            'tier_unrecorded': self.tier_unrecorded,
            # A thread's first turn, given the tier of the file's first snapshot.
            'tier_inferred': self.tier_inferred,
            'tiers': dict(self.tiers),
            'long_context': self.long,
            'aborted_turns': aborted,
            'aborted_turns_replayed': aborted_replayed,
            'by_model': [{'model': m, 'usd': round(v['usd'], 6), 'responses': v['responses']}
                         for m, v in sorted(self.by_model.items(), key=lambda kv: -kv[1]['usd'])],
        }
        if not self.priced:
            seen = sorted({m for m, _ in self.unpriced_models})
            out['reason'] = ('no responses in range' if not unpriced else
                             'no response could be priced (models: '
                             + ', '.join(seen[:6]) + (' ...' if len(seen) > 6 else '') + ')')
        return out


def _in_time_order(responses):
    """Charged responses oldest first, dropping those whose timestamp could not be read.

    Responses reach this module in file order -- files are read one at a time, and sessions
    overlap, so a file read later routinely carries responses older than the one before it.
    A running total accumulated in that order and then plotted against time would say how
    much had been *read* by each moment rather than how much had been *spent*, and the
    cumulative curve would step backwards at every file boundary.  Sorting the points after
    accumulating them, as `_bucket_last` does, cannot undo that: the damage is in the values,
    not the order they are held in.  So the ordering has to happen before the running total.
    """
    return sorted((r for r in responses if r[0] is not None), key=lambda r: r[0])


def _bucket_last(points):
    """Last cumulative value in each time bucket, oldest first.

    Cumulative series are step functions; the last value in a bucket is the state at its
    end, and averaging would invent intermediate totals that never existed.
    """
    if not points:
        return []
    points.sort(key=lambda p: p[0])
    out = []
    cur = None
    for p in points:
        b = int(p[0] // CHART_BUCKET_S)
        if b != cur:
            out.append(list(p))
            cur = b
        else:
            out[-1] = list(p)
    return out


def analyze(files, charged, counters, scope=None, focus=None, extra_quality=None,
            account=None, prices=None):
    """Build the full report model.

    `files`   path -> FileResult
    `charged` path -> charged usage rows (the canonical ledger)
    `counters` corpus-wide data-quality counters from the ledger
    `account`  identity record from :mod:`tokencounter.account`, or None
    `prices`   what :func:`pricing.load` returned; None loads the vendored table
    """
    api = ApiValue(pricing.load() if prices is None else prices)
    # Days with anything valued: a priced response or a search fee.  The rest report None
    # rather than 0, which would read as free.
    api_days = set()
    totals = collections.Counter()
    daily = collections.defaultdict(collections.Counter)
    # Kept beside `daily` rather than nested inside it: `daily` is a Counter, and a Counter
    # holding a Counter does not survive the arithmetic done on it.
    daily_models = collections.defaultdict(collections.Counter)
    by_model = collections.defaultdict(collections.Counter)
    by_effort = collections.defaultdict(collections.Counter)
    cat_tokens = collections.Counter()
    cat_chars = collections.Counter()
    cat_items = collections.Counter()
    # Categories on the timeline, so the report's composition chart can follow the same
    # viewport as the time charts.  Keyed by bucket start, one Counter of tokens per bucket.
    cat_buckets = collections.defaultdict(collections.Counter)
    sessions = {}
    images = collections.Counter()
    image_formats = collections.Counter()
    leads = []
    hot = []
    residual = collections.Counter()
    quality = collections.Counter(extra_quality or {})
    # "No damage found" and "damage not looked for" are different statements, and an absent
    # key cannot tell them apart.  These are always present, at zero if nothing happened.
    # The renderer used to force them into its counter table; the model carries them now.
    for _k in ALWAYS_QUALITY:
        quality.setdefault(_k, 0)
    # Charged responses on the absolute timeline, for the per-window cumulative curve.
    resp_ts = []
    resp_usd = []
    # Input is shown as tiktoken counted it wherever a response has a reconstructed prompt
    # (§5.7); output, cached and the limit stay Codex's.  Both are accumulated, because
    # whether any response was counted is only known at the end, and share.py and the JSON
    # keep reading the recorded figures under their old keys.
    daily_models_tk = collections.defaultdict(collections.Counter)
    tk_found = tk_missing = tk_missing_input = 0

    for path, fr in sorted(files.items()):
        if fr.get('error'):
            # Counted, not skipped: an error partway through a file still leaves a valid
            # append-only prefix of usage records, and dropping the file loses more than it
            # protects.  A file that failed to even stat contributes nothing anyway.
            quality['extraction_errors'] += 1
        rows = charged.get(path) or []
        sid = fr.get('session_id') or path
        s = sessions.get(sid)
        if s is None:
            s = sessions[sid] = {
                'session_id': sid, 'threads': 0, 'files': [], 'cwd': fr.get('cwd'),
                'first': None, 'last': None, 'models': collections.Counter(),
                'input': 0, 'cached': 0, 'output': 0, 'reasoning': 0, 'responses': 0,
                'tiktoken_input': 0, 'unique_tokens': 0, 'cats': collections.Counter(),
                'cli': fr.get('cli_version'), 'resend_cost': 0,
                'recon': 0, 'reported': 0, 'hot': [],
                'api_usd': None,
            }
        s['threads'] += 1
        s['files'].append(path)
        s['unique_tokens'] += fr.get('unique_tokens') or 0
        s['resend_cost'] += fr.get('resend_cost') or 0
        st = fr.get('started_at')
        if st:
            s['first'] = st if s['first'] is None else min(s['first'], st)
            s['last'] = st if s['last'] is None else max(s['last'], st)

        ct = fr.get('cat_tokens') or {}
        if ct:
            # A file's content is placed at the moment the file opens; no finer stamp exists,
            # because deduplication ran over the file as a whole.
            bt = worker.epoch(fr.get('started_at'))
            if bt is None:
                bt = _day_span(fr.get('date'))[0]
            if bt is not None:
                cat_buckets[int(bt // CAT_BUCKET_S) * CAT_BUCKET_S].update(ct)
        for k, v in ct.items():
            cat_tokens[k] += v
            s['cats'][k] += v
        for k, v in (fr.get('cat_chars') or {}).items():
            cat_chars[k] += v
        for k, v in (fr.get('cat_items') or {}).items():
            cat_items[k] += v
        totals['unique_tokens'] += fr.get('unique_tokens') or 0
        totals['resend_cost'] += fr.get('resend_cost') or 0
        totals['bytes'] += fr.get('size') or 0

        im = fr.get('images') or {}
        images['count'] += im.get('count') or 0
        images['lo'] += im.get('lo') or 0
        images['hi'] += im.get('hi') or 0
        images['unknown'] += im.get('unknown') or 0
        images['bytes'] += im.get('bytes') or 0
        for k, v in (im.get('formats') or {}).items():
            image_formats[k] += v

        for k, v in (fr.get('counters') or {}).items():
            quality[k] += v

        for h in (fr.get('hot_items') or []):
            hot.append(dict(h, session_id=sid, path=path))
            s['hot'].append(h)

        # -- charged usage ----------------------------------------------------
        first_row = True
        counted = tiktoken_inputs(fr)
        for r in rows:
            u = r['usage']
            inp = u.get('input_tokens') or 0
            cch = u.get('cached_input_tokens') or 0
            out = u.get('output_tokens') or 0
            rsn = u.get('reasoning_output_tokens') or 0
            tk = counted.get((r.get('stream'), r.get('index')))
            if tk is None:
                # No reconstructed prompt for this one (its file was extracted without the
                # tokenizer, or failed part-way): it keeps Codex's figure, and says so.
                tk_missing += 1
                tk_missing_input += inp
                tk = inp
            else:
                tk_found += 1
            d = _day(r.get('ts'), fr.get('date')) or 'unknown'
            priced_before = api.priced
            usd = api.add(r)
            if usd is not None and not (usd or api.priced > priced_before):
                usd = None                  # unpriced, and no search fee either
            resp_ts.append((worker.epoch(r.get('ts')), inp, cch, out, tk))
            resp_usd.append((resp_ts[-1][0], usd))
            totals['responses'] += 1
            totals['input'] += inp
            totals['cached'] += cch
            totals['output'] += out
            totals['reasoning'] += rsn
            totals['tiktoken_input'] += tk
            dd = daily[d]
            dd['responses'] += 1
            if usd is not None:
                dd['api_usd'] += usd
                api_days.add(d)
                s['api_usd'] = (s['api_usd'] or 0.0) + usd
            dd['input'] += inp
            dd['cached'] += cch
            dd['output'] += out
            dd['tiktoken_input'] += tk
            m = r.get('model') or 'unknown'
            daily_models[d][m] += inp
            daily_models_tk[d][m] += tk
            bm = by_model[m]
            bm['responses'] += 1
            bm['input'] += inp
            bm['cached'] += cch
            bm['output'] += out
            bm['reasoning'] += rsn
            bm['tiktoken_input'] += tk
            e = r.get('effort') or 'unknown'
            be = by_effort[e]
            be['responses'] += 1
            be['input'] += inp
            be['cached'] += cch
            be['output'] += out
            be['tiktoken_input'] += tk
            s['responses'] += 1
            s['input'] += inp
            s['cached'] += cch
            s['output'] += out
            s['reasoning'] += rsn
            s['tiktoken_input'] += tk
            s['models'][m] += 1
            if first_row and cch > 0:
                quality['first_response_cached'] += 1
            first_row = False

        # -- reconstruction and cache leads, restricted to charged responses ---
        keep = _charged_keys(fr, rows)
        for resp in (fr.get('responses') or []):
            if (resp.get('stream'), resp.get('index')) not in keep:
                continue
            rep = resp.get('reported_input')
            rec = resp.get('recon_input') or 0
            if rep:
                residual['reported'] += rep
                residual['recon'] += rec
                residual['n'] += 1
                s['reported'] += rep
                s['recon'] += rec
            cached = resp.get('reported_cached') or 0
            stable = resp.get('stable_prefix') or 0
            gap = stable - cached
            if gap >= LEAD_MIN_GAP:
                leads.append({
                    'session_id': sid, 'ts': resp.get('ts'), 'model': resp.get('model'),
                    'stable_prefix': stable, 'reported_cached': cached, 'gap': gap,
                    'reported_input': rep, 'recon_input': rec,
                    'prompt_items': resp.get('prompt_items'),
                    'new_items': resp.get('new_items'),
                })

    # tiktoken counts are shown only when the run tokenized and at least one charged response
    # has a reconstructed prompt.  Otherwise every "tiktoken" figure above is just Codex's
    # copied over, and publishing it under that name would be false.
    measured = bool(tk_found) and not (scope or {}).get('metrics_only')
    shown = 'tiktoken_input' if measured else 'input'
    if measured:
        quality['tiktoken_input_fallback'] = tk_missing
        quality['tiktoken_input_fallback_tokens'] = tk_missing_input
    else:
        resp_ts = [r[:4] for r in resp_ts]

    for s in sessions.values():
        if not measured:
            s['tiktoken_input'] = None
        s['api_usd'] = None if s['api_usd'] is None else round(s['api_usd'], 6)
        s['models'] = dict(s['models'])
        s['cats'] = dict(s['cats'])
        s['uncached'] = s['input'] - s['cached']
        s['hot'] = sorted(s['hot'], key=lambda h: -h['cost'])[:10]
        s['files'] = len(s['files'])

    leads.sort(key=lambda x: -x['gap'])
    hot.sort(key=lambda h: -h['cost'])

    # Sorted by the input the page shows: its top-session tile is the first of these.
    sess_list = sorted(sessions.values(), key=lambda x: -x[shown])
    deep = [s['session_id'] for s in sess_list[:DEEP_DIVE_SESSIONS]]
    if focus and focus not in deep:
        deep.append(focus)

    # Response, turn and tool time from the records' own stamps (§5.8).  Its counters join
    # the data-quality set, so a sample left out is counted where the report can see it.
    lat, lat_q = latency.build(files, charged)
    quality.update(lat_q)
    # The same local-midnight spans the daily bars carry, so the response-time chart sits on
    # the shared time axis without re-deriving them (and their DST handling) in the page.
    for d in (lat.get('daily') or []):
        d['start'], d['end'] = _day_span(d['date'])
    # Drawn as bars on the response-time chart, and kept apart from `latency`: a day the
    # limit blocked outright has events and no timed response.
    lim, lim_q = limit_events(files)
    quality.update(lim_q)

    inp, cch = totals['input'], totals['cached']
    uniq = totals['unique_tokens'] or 0
    model = {
        'generated_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'scope': scope or {},
        'account': account or {'available': False, 'reason': 'not requested'},
        'rate_limits': rate_limit_windows(files, resp_ts, usd=resp_usd),
        'limit_events': lim,
        # What the recorded usage would cost at OpenAI's API list prices (tokencounter.pricing).
        'api_value': api.model(files),
        'totals': {
            'files': len(files),
            'sessions': len(sessions),
            'threads': sum(s['threads'] for s in sessions.values()),
            'responses': totals['responses'],
            'input': inp,
            # The input the page shows, and which of the two it is.  `input` above stays
            # Codex's recorded figure, as `cached`, `uncached` and `cache_hit` are measured
            # against it: a cached count over a tiktoken count would be a ratio of two
            # different measurements.
            'tiktoken_input': totals['tiktoken_input'] if measured else None,
            'input_source': 'tiktoken' if measured else 'recorded',
            'cached': cch,
            'uncached': inp - cch,
            'output': totals['output'],
            'reasoning': totals['reasoning'],
            'unique_tokens': uniq,
            'resend_cost': totals['resend_cost'],
            # The per-file worker cannot remove cross-file ancestor replay, so resend cost
            # slightly exceeds the summed reconstructed prompts it should equal.  Publish
            # the gap beside it instead of presenting the larger number bare.
            'resend_overcount': totals['resend_cost'] - residual['recon'],
            'bytes': totals['bytes'],
            'cache_hit': (cch / inp) if inp else None,
            'amplification': (inp / uniq) if uniq else None,
        },
        # `start`/`end` are the bucket's own local-midnight span: the report draws the daily
        # bars on a real time axis shared with the limit chart, and a renderer re-deriving the
        # span from the date string would have to repeat the DST handling done here.
        'daily': [dict(v, date=d, uncached=v['input'] - v['cached'],
                       api_usd=round(v['api_usd'], 6) if d in api_days else None,
                       models=dict(daily_models[d]),
                       **({'tiktoken_models': dict(daily_models_tk[d])} if measured
                          else {'tiktoken_input': None}),
                       start=_day_span(d)[0], end=_day_span(d)[1])
                  for d, v in sorted(daily.items())],
        # Categories on the timeline: the report filters these to whatever range is
        # on screen, and the bucket length travels with them so a consumer does not
        # have to know it.
        'cat_series': [[t, dict(c)] for t, c in sorted(cat_buckets.items())],
        'cat_bucket_s': CAT_BUCKET_S,
        'sessions': sess_list,
        'deep_dive': deep,
        # Keyed on `deep`, not on the top-N slice: `--session` adds a focus session that may
        # not be in the top N, and it would otherwise appear in the picker with no data.
        'turns': {sid: _turn_profile(files, sessions[sid])
                  for sid in deep if sid in sessions},
        'models': [dict(v, model=m, uncached=v['input'] - v['cached'],
                        tiktoken_input=v['tiktoken_input'] if measured else None)
                   for m, v in sorted(by_model.items(), key=lambda kv: -kv[1][shown])],
        'efforts': [dict(v, effort=e, uncached=v['input'] - v['cached'],
                         tiktoken_input=v['tiktoken_input'] if measured else None)
                    for e, v in sorted(by_effort.items(), key=lambda kv: -kv[1][shown])],
        'categories': [{'category': c, 'tokens': cat_tokens.get(c, 0),
                        'chars': cat_chars.get(c, 0), 'items': cat_items.get(c, 0),
                        'opaque': c in classify.OPAQUE}
                       for c in classify.CATEGORIES
                       if cat_tokens.get(c) or cat_chars.get(c)],
        'images': {'count': images['count'], 'lo': images['lo'], 'hi': images['hi'],
                   'unknown': images['unknown'], 'bytes': images['bytes'],
                   'formats': dict(image_formats)},
        'residual': {
            'responses': residual['n'],
            'reported': residual['reported'],
            'reconstructed': residual['recon'],
            'residual': residual['reported'] - residual['recon'],
            'coverage': (residual['recon'] / residual['reported'])
                        if residual['reported'] else None,
            'opaque_reasoning_chars': cat_chars.get('reasoning_blob', 0),
        },
        'latency': lat,
        'leads': leads[:LEAD_LIMIT],
        'leads_total': len(leads),
        'hot_items': hot[:50],
        'counters': dict(counters),
        'quality': dict(quality),
    }
    return model


def _turn_profile(files, session):
    """Context composition per turn, summed across the session's threads."""
    acc = collections.defaultdict(collections.Counter)
    for path in files:
        fr = files[path]
        if (fr.get('session_id') or path) != session['session_id']:
            continue
        for t in (fr.get('turns') or []):
            if t['turn'] < 0:
                continue
            acc[t['turn']].update(t['cats'])
    return [{'turn': t, 'cats': dict(c)} for t, c in sorted(acc.items())][:120]
