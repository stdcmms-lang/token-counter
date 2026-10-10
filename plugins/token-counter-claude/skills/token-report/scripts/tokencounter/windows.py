"""Sparse sourced quota readings, captured plan evidence and schema-1 numerators.

Reset clustering never uses percentage drops or a calendar grid to move an anchor.
Charts cover nominal intervals; comparison counts cover (first reading, first peak].
All functions are local, content-free decisions and perform no I/O.
"""
import collections
import copy
import datetime
import hashlib
import json
import math
import statistics

from . import account, pricing
from .models import bump
from .worker import epoch

WEEK_S = 604800
KINDS = ('weekly_all', 'weekly_scoped', 'session')
COUNTS = ('responses', 'input', 'cached', 'output', 'reasoning')
WIRE_FIELDS = ('start', 'window_minutes', 'plan', 'first_pct', 'peak_pct') + COUNTS + ('split',)
SPLIT_FIELDS = ('model', 'tier', 'responses', 'input', 'cached', 'output',
                'cache_write_5m', 'cache_write_1h')
ANCHOR_FIELDS = ('window_key', 'kind', 'scope_key', 'reset_at', 'min_reset', 'max_reset', 'grid_phase')
WITHHOLDING = ('window_single_reading', 'window_zero_delta', 'limit_percent_decreased',
               'limit_quote_conflict', 'window_unavailable_timestamps', 'window_partial_response',
               'window_unknown_cache_ttl', 'window_overlapping_span', 'limit_reset_cluster_ambiguous')


def _stamp(value):
    if isinstance(value, str):
        return epoch(value)
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _key(kind, scope, reset):
    raw = json.dumps([kind, scope, reset], separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _canonical(resets):
    # floor(x + .5), rather than Python's banker's rounding: median ties go upward.
    return float(math.floor(statistics.median(resets) + .5))


def _fits(cluster, reset, tolerance):
    return max(cluster['max_reset'], reset) - min(cluster['min_reset'], reset) <= tolerance


def _grid_departure(reset, phase):
    remainder = (reset - phase) % WEEK_S
    return min(remainder, WEEK_S - remainder) > 2.0


def _anchors(clusters):
    """Only these scalar fields enter history metadata; no observation bodies or IDs."""
    return [{k: c[k] for k in ANCHOR_FIELDS} for c in clusters]


def _checked_anchors(established):
    out = []
    for anchor in established:
        if not isinstance(anchor, dict) or any(k not in anchor for k in ANCHOR_FIELDS):
            raise ValueError('invalid reset anchor metadata')
        c = {k: anchor[k] for k in ANCHOR_FIELDS}
        if (c['kind'] not in KINDS or not isinstance(c['window_key'], str)
                or len(c['window_key']) != 64 or any(x not in '0123456789abcdef' for x in c['window_key'])
                or c['scope_key'] is not None and not isinstance(c['scope_key'], str)
                or c['kind'] == 'weekly_scoped' and not c['scope_key']
                or any(_stamp(c[k]) is None for k in ('reset_at', 'min_reset', 'max_reset'))
                or c['min_reset'] > c['max_reset'] or c['max_reset'] - c['min_reset'] > 2.0
                or c['grid_phase'] is not None and _stamp(c['grid_phase']) is None):
            raise ValueError('invalid reset anchor metadata')
        out.append(c)
    if len({c['window_key'] for c in out}) != len(out):
        raise ValueError('duplicate reset anchor metadata')
    return out


def cluster_resets(readings, *, established=(), tolerance_s=2.0) -> tuple:
    """Return (clusters, counters). Established anchors and grid phase never move."""
    if type(tolerance_s) not in (int, float) or not math.isfinite(tolerance_s) or tolerance_s < 0:
        raise ValueError('invalid reset tolerance')
    counters, groups = {}, collections.defaultdict(list)
    anchors = _checked_anchors(established)
    for anchor in anchors:
        groups[(anchor['kind'], anchor['scope_key'])].append(
            dict(anchor, readings=[], established=True, ambiguous=False))
    selected = collections.defaultdict(list)
    for reading in readings:
        kind, scope = reading.get('kind'), reading.get('scope_key')
        if kind not in KINDS:
            bump(counters, 'limit_unknown_kind')
            continue
        if kind == 'weekly_scoped' and not scope:
            bump(counters, 'limit_unknown_scope')
            continue
        reset = _stamp(reading.get('resets_at'))
        if reset is None:
            bump(counters, 'limit_reset_invalid')
            continue
        selected[(kind, scope if kind == 'weekly_scoped' else None)].append((reset, reading))
    for identity, entries in sorted(selected.items(), key=lambda kv: (KINDS.index(kv[0][0]), kv[0][1] or '')):
        clusters = groups[identity]
        for reset, reading in sorted(entries, key=lambda x: (x[0], x[1].get('reading_key', ''))):
            fits = [c for c in clusters if c['established'] and _fits(c, reset, tolerance_s)]
            if len(fits) > 1:
                bump(counters, 'limit_reset_cluster_ambiguous')
                for c in fits:
                    c['ambiguous'] = True
                continue  # Never attach ambiguous evidence to either published anchor.
            if not fits:
                fits = [c for c in clusters if not c['established'] and _fits(c, reset, tolerance_s)]
            if fits:
                c = fits[-1]
            else:
                c = dict(kind=identity[0], scope_key=identity[1], min_reset=reset, max_reset=reset,
                         readings=[], established=False, ambiguous=False, grid_phase=None)
                clusters.append(c)
            c['readings'].append(copy.deepcopy(reading))
            c['min_reset'], c['max_reset'] = min(c['min_reset'], reset), max(c['max_reset'], reset)
        for c in clusters:
            if not c['established']:
                c['reset_at'] = _canonical([_stamp(r['resets_at']) for r in c['readings']])
                c['window_key'] = _key(c['kind'], c['scope_key'], c['reset_at'])
    out = sorted([c for clusters in groups.values() for c in clusters],
                 key=lambda c: (KINDS.index(c['kind']), c['scope_key'] or '', c['reset_at']))
    weekly = [c for c in out if c['kind'] == 'weekly_all']
    established_weekly = [c for c in anchors if c['kind'] == 'weekly_all']
    phase = next((c['grid_phase'] for c in established_weekly if c['grid_phase'] is not None),
                 established_weekly[0]['reset_at'] if established_weekly else
                 weekly[0]['reset_at'] if weekly else None)
    for c in weekly:
        c['grid_phase'] = phase
        if c['readings'] and _grid_departure(c['reset_at'], phase):
            bump(counters, 'limit_reset_grid_departures')
    return out, counters


def observation_span(readings) -> tuple:
    """Return (span, counters); plateau readings retain their sourced points only."""
    counters, points = {}, set()
    unavailable = False
    for r in readings:
        pct, ts = r.get('percent'), _stamp(r.get('ts'))
        if type(pct) not in (int, float) or not math.isfinite(pct) or not 0 <= pct <= 100:
            bump(counters, 'limit_reading_invalid')
            unavailable = True
        elif ts is None:
            unavailable = True
        else:
            points.add((ts, pct))
    points = sorted(points)
    if unavailable:
        bump(counters, 'window_unavailable_timestamps')
    if len(points) < 2:
        bump(counters, 'window_single_reading')
    first = points[0][1] if points else None
    peak = max(p for _t, p in points) if points else None
    if first is None or peak <= first:
        bump(counters, 'window_zero_delta')
    by_time = collections.defaultdict(set)
    for ts, pct in points:
        by_time[ts].add(pct)
    if any(len(pcts) > 1 for pcts in by_time.values()):
        bump(counters, 'limit_quote_conflict')
    if any(b[1] < a[1] for a, b in zip(points, points[1:]) if b[0] > a[0]):
        bump(counters, 'limit_percent_decreased')
    return {'observation_start': points[0][0] if points else None,
            'observation_end': next((t for t, p in points if p == peak), None),
            'first_pct': first, 'peak_pct': peak, 'last_pct': points[-1][1] if points else None,
            'pct_points': [list(p) for p in points]}, counters


def plan_for_interval(start, end, account_snapshots) -> tuple:
    """Return (plan, source, counters) using all four captured-snapshot conditions."""
    counters = {}
    start, end = _stamp(start), _stamp(end)
    mapped = []
    for snapshot in account_snapshots:
        observed = _stamp(snapshot.get('observed_at'))
        if observed is None:
            continue
        plan, diagnostics = account.map_plan(snapshot.get('organization_type'), snapshot.get('rate_limit_tier'))
        mapped.append((observed, snapshot, plan, diagnostics))
    mapped.sort(key=lambda s: (s[0], json.dumps(s[1], sort_keys=True, separators=(',', ':'))))
    # Earlier snapshots beyond the latest predecessor do not veto the interval.
    predecessor = [s for s in mapped if start is not None and s[0] < start]
    latest_before = predecessor[-1:]  # one latest predecessor, if present
    applicable = latest_before + [s for s in mapped if start is not None and s[0] >= start]
    for _t, _s, _p, diagnostics in applicable:
        for name, value in diagnostics.items():
            bump(counters, name, value)
    known = {p for _t, _s, p, _d in applicable if p is not None}
    if len(known) > 1:
        bump(counters, 'account_plan_conflict')  # once per attribution, never per row
    witness = mapped[-1][1] if mapped else None
    created = _stamp(witness.get('subscription_created_at')) if witness is not None else None
    if witness is not None and created is None:
        bump(counters, 'account_subscription_date_missing')
    plan = next(iter(known)) if len(known) == 1 else None
    if (start is None or end is None or end < start or not mapped or plan is None
            or any(p != plan for _t, _s, p, _d in applicable)
            or created is None or created > start):
        bump(counters, 'window_unknown_plan')
        return None, None, counters
    return plan, 'account', counters


def _in_span(stamp, start, end):
    return stamp is not None and start is not None and end is not None and start < stamp <= end


def _in_nominal(stamp, start, end):
    return stamp is not None and start <= stamp < end


def _counts(rows):
    return dict(responses=len(rows), input=sum(r['usage']['input_tokens'] for r in rows),
                cached=sum(r['usage']['cached_input_tokens'] for r in rows),
                output=sum(r['usage']['output_tokens'] for r in rows),
                reasoning=sum(r['usage']['reasoning_output_tokens'] or 0 for r in rows))


def _timing_safe(row, stamp):
    return (stamp is not None and row.get('timestamp_quality') == 'original' and not row.get('replayed')
            and all(type(row.get('usage', {}).get(k)) is int and row['usage'][k] >= 0
                    for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')))


def _ttl_complete(row):
    a, b = row.get('cache_write_5m'), row.get('cache_write_1h')
    return (row.get('cache_creation_input_tokens') == 0 or
            row.get('cache_ttl_complete') is True and type(a) is int and type(b) is int
            and a >= 0 and b >= 0 and a + b == row['cache_creation_input_tokens'])


def _split(rows):
    groups = collections.defaultdict(list)
    for r in rows:
        if r.get('tier') in pricing.TIER_CLASSES:
            model = pricing.canonical_model(r['model'])[0]
            groups[(model, r['tier'])].append(r)
    out = []
    for (model, tier), members in sorted(groups.items()):
        c = _counts(members)
        c.pop('reasoning')
        out.append(dict(c, model=model, tier=tier,
                        cache_write_5m=sum(r.get('cache_write_5m') or 0 for r in members)
                        if all(_ttl_complete(r) for r in members) else None,
                        cache_write_1h=sum(r.get('cache_write_1h') or 0 for r in members)
                        if all(_ttl_complete(r) for r in members) else None))
    return out


def _row_diagnostics(rows):
    return {'window_unknown_speed': sum(r.get('tier') not in pricing.TIER_CLASSES for r in rows),
            'window_rows_fast': sum(r.get('tier') == 'fast' for r in rows),
            'window_rows_us': sum(r.get('inference_geo') == 'us' for r in rows),
            'window_rows_haiku_long': sum(r['model'] == 'claude-haiku-5-5'
                                          and r['usage']['input_tokens'] > 100000 for r in rows)}


def _overlaps(a, b):
    return (None not in (a['observation_start'], a['observation_end'],
                         b['observation_start'], b['observation_end'])
            and max(a['observation_start'], b['observation_start'])
            < min(a['observation_end'], b['observation_end']))


def build_windows(readings, rows, *, account_snapshots=(), established=(), now=None) -> tuple:
    """Return (windows, counters), including separate scoped and five-hour series."""
    clusters, counters = cluster_resets(readings, established=established)
    timed = sorted([(_stamp(r.get('ts')), r) for r in rows],
                   key=lambda pair: (pair[0] is None, pair[0] or 0, pair[1]['response_key']))
    windows = []
    for cluster in clusters:
        if not cluster['readings']:
            continue  # Established metadata alone never invents an observation window.
        span, reasons = observation_span(cluster['readings'])
        reset = cluster['reset_at']
        nominal = reset - (300 * 60 if cluster['kind'] == 'session' else WEEK_S)
        members = [r for ts, r in timed if _in_span(ts, span['observation_start'], span['observation_end'])]
        nominal_members = [r for ts, r in timed if _in_nominal(ts, nominal, reset)]
        if any(ts is None or _in_span(ts, span['observation_start'], span['observation_end'])
               and not _timing_safe(r, ts) for ts, r in timed):
            reasons['window_unavailable_timestamps'] = 1
        if any(r.get('partial') for r in members):
            reasons['window_partial_response'] = 1
        ttl_complete = all(_ttl_complete(r) for r in members)
        if not ttl_complete:
            reasons['window_unknown_cache_ttl'] = 1
        if cluster['ambiguous']:
            reasons['limit_reset_cluster_ambiguous'] = 1
        plan, source, plan_q = (plan_for_interval(span['observation_start'], span['observation_end'], account_snapshots)
                                if cluster['kind'] == 'weekly_all' else (None, None, {}))
        # Counts below are decisions per window, except row diagnostics, which count rows.
        if cluster['kind'] == 'weekly_all':
            for name, value in dict(reasons, **plan_q).items():
                if name != 'limit_reset_cluster_ambiguous':  # already counted per ambiguous reading
                    bump(counters, name, value)
            for name, value in _row_diagnostics(members).items():
                bump(counters, name, value)
        speed_missing = sum(r.get('tier') not in pricing.TIER_CLASSES for r in members)
        windows.append(dict(span, window_key=cluster['window_key'], kind=cluster['kind'],
                            scope_key=cluster['scope_key'], nominal_start=nominal, reset_at=reset,
                            readings=cluster['readings'], rows=[r['response_key'] for r in members],
                            counts=_counts(members), nominal_rows=[r['response_key'] for r in nominal_members],
                            nominal_counts=_counts(nominal_members),
                            cache_write_5m=sum(r.get('cache_write_5m') or 0 for r in members) if ttl_complete else None,
                            cache_write_1h=sum(r.get('cache_write_1h') or 0 for r in members) if ttl_complete else None,
                            split=_split(members), split_complete=not speed_missing,
                            speed_unrecorded_responses=speed_missing, plan=plan, plan_source=source,
                            shareable=cluster['kind'] == 'weekly_all' and not reasons,
                            withheld_reasons=sorted(k for k in reasons if k in WITHHOLDING),
                            estimator_eligible=cluster['kind'] == 'weekly_all' and not reasons
                            and span['peak_pct'] - span['first_pct'] >= 5 and not speed_missing and plan is not None))
    all_model = [w for w in windows if w['kind'] == 'weekly_all']
    for w in all_model:
        if any(other is not w and _overlaps(w, other) for other in all_model):
            w['shareable'] = w['estimator_eligible'] = False
            w['withheld_reasons'].append('window_overlapping_span')
            w['withheld_reasons'].sort()
            bump(counters, 'window_overlapping_span')
    return windows, counters


def _wire_entry(window):
    return dict(start=datetime.datetime.fromtimestamp(window['nominal_start'], datetime.timezone.utc).isoformat(),
                window_minutes=10080, first_pct=window['first_pct'], peak_pct=window['peak_pct'],
                plan=window['plan'], split=[{k: v for k, v in r.items() if k in SPLIT_FIELDS and v is not None}
                                          for r in window['split']], **window['counts'])


def wire_windows(windows, coverage) -> tuple:
    """Return (entries-or-None, reasons). None means omit the whole replacement key."""
    reasons = {w['window_key']: list(w['withheld_reasons']) for w in windows if w['withheld_reasons']}
    if not coverage.get('windows_replace_safe') or coverage.get('window_reasons'):
        reasons['coverage'] = list(coverage.get('window_reasons') or ['windows_withheld_retention'])
        return None, reasons
    return [_wire_entry(w) for w in windows if w['kind'] == 'weekly_all' and w['shareable']], reasons
