"""Claude's local report model, built only from the canonical captured ledger.

Recorded token totals, a byte inventory, transcript timing and API list value are separate
measurements. Coverage is supplied by history and passes through without interpretation.
"""
import collections
import copy
import datetime
import math
import re
import time

from . import composition, latency, pricing, windows
from .ledger import _day, _day_span, _family_summary, _iso, _local_day, epoch
from .models import COUNTER_NAMES, RenderProfile, ReportModel, bump

CAT_BUCKET_S = 3600
MAX_POINTS = 120        # chart points per window, as Codex's analyzer caps them
UNSUPPORTED = {'available': False,
               'reason': 'Claude transcripts do not establish per-content token attribution.'}
COUNTS = ('responses', 'input', 'cached', 'output', 'reasoning')


def _downsample(points, limit=MAX_POINTS):
    """Keep at most `limit` points, preserving the first and last."""
    if len(points) <= limit:
        return points
    step = len(points) / float(limit - 1)
    keep = [points[int(i * step)] for i in range(limit - 1)]
    keep.append(points[-1])
    return keep


def _counts(rows):
    return {'responses': len(rows),
            'input': sum(r['usage']['input_tokens'] for r in rows),
            'cached': sum(r['usage']['cached_input_tokens'] for r in rows),
            'output': sum(r['usage']['output_tokens'] for r in rows),
            'reasoning': sum(r['usage']['reasoning_output_tokens'] or 0 for r in rows)}


def _now(value):
    if value is None:
        return time.time()
    if isinstance(value, str):
        value = epoch(value)
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('invalid analysis time')
    return value


def _selected(item, since, until):
    day = item.get('local_day') or _day(item.get('ts'))
    return ((since is None or day is not None and day >= since)
            and (until is None or day is not None and day <= until))


def _timing(ledger, rows, since=None):
    """Feed Claude's frozen intervals to the unchanged shared fitter/aggregator."""
    counters, files, charged = {}, {}, collections.defaultdict(list)
    selected_streams = {r['stream_id'] for r in rows}
    for stream in selected_streams:
        files[stream] = {'turn_starts': [], 'tool_times': []}
    turns = {(t['stream_id'], t['turn']): t for t in ledger['turns'] if t['start'] is not None}
    for (stream, turn), fact in turns.items():
        if stream in files:
            starts = files[stream]['turn_starts']
            while len(starts) <= turn:
                starts.append(None)
            starts[turn] = fact['start']
    accepted = []
    for row in rows:
        end = epoch(row['ts'])
        if since is not None and end is not None and end < since:
            continue
        if row['partial']:
            bump(counters, 'latency_partial_response')
            continue
        if row.get('replayed') or row['timestamp_quality'] != 'original':
            bump(counters, 'latency_copy_timestamp_unverified')
            continue
        if row['tier'] not in pricing.TIER_CLASSES:
            bump(counters, 'latency_unknown_speed')
        charged[row['stream_id']].append(row)
        start = epoch(row['req_ts'])
        if start is not None and end is not None and 0 < end - start <= latency.RESPONSE_CAP_S:
            accepted.append(row)
    # Tools are already matched on the parent chain and deduplicated by the ledger.
    # No parent_thread_id is supplied: Claude does not use Codex's opening-burst rule.
    for tool in ledger['tools']:
        end = epoch(tool['end_ts'])
        if tool['stream_id'] in files and (since is None or end is not None and end >= since):
            files[tool['stream_id']]['tool_times'].append(
                (tool['name'], epoch(tool['ts']), tool['seconds']))
    model, shared = latency.build(files, charged, tier_groups=True)
    for name, value in shared.items():
        bump(counters, name, value)
    # The shared function has no turn/tool summaries when no response is timed.
    # Keep their independently observed intervals available even in that case.
    if not model['available']:
        model.update(responses=latency._summary([]), groups=[], tier_groups=[], daily=[], tools=[],
                     tools_total=0, method={'floor_quantile': latency.FLOOR_Q,
                                           'fit_min': latency.FIT_MIN, 'fit_max': latency.FIT_MAX,
                                           'response_cap_s': latency.RESPONSE_CAP_S,
                                           'tool_cap_s': latency.TOOL_CAP_S, 'turn_cap_s': latency.TURN_CAP_S})
        tool_groups = collections.defaultdict(list)
        for file in files.values():
            for name, start, seconds in file['tool_times']:
                if start is not None and seconds is not None and 0 < seconds <= latency.TOOL_CAP_S:
                    tool_groups[name].append(seconds)
        model['tools'] = sorted([dict(latency._summary(v), tool=k) for k, v in tool_groups.items()],
                                key=lambda t: (-t['total_s'], t['tool']))[:50]
        model['tools_total'] = len(tool_groups)
        bump(counters, 'tool_calls_timed', sum(len(v) for v in tool_groups.values()))
    # Last accepted end defines a turn, including an accepted response whose individual
    # interval is unavailable. Model share includes only timed, nonoverlapping intervals.
    ends, work = {}, collections.Counter()
    for row in charged.values():
        for r in row:
            end = epoch(r['ts'])
            if end is not None:
                key = (r['stream_id'], r['turn'])
                ends[key] = max(ends.get(key, end), end)
    for row in accepted:
        work[(row['stream_id'], row['turn'])] += epoch(row['ts']) - epoch(row['req_ts'])
    turn_values, model_s = [], 0.0
    # Recompute turn diagnostics once, since accepted-end semantics include untimed rows.
    counters.pop('turn_no_start', None)
    counters.pop('turn_over_cap', None)
    for key, end in ends.items():
        start = epoch(turns.get(key, {}).get('start'))
        if start is None or end <= start:
            bump(counters, 'turn_no_start')
        elif end - start > latency.TURN_CAP_S:
            bump(counters, 'turn_over_cap')
        else:
            turn_values.append(end - start)
            model_s += min(end - start, work[key])
    total_s = sum(turn_values)
    model['turns'] = dict(latency._summary(turn_values), model_s=round(model_s, 3),
                          model_share=round(model_s / total_s, 4) if total_s else None)
    model.pop('hours', None)
    model.pop('utc_hours', None)
    model.pop('utc_weekdays', None)
    tier_groups = [g for g in model['tier_groups'] if g['tier'] in pricing.TIER_CLASSES]
    for field, groups, counter in (('groups', model['groups'], 'latency_groups_omitted'),
                                   ('tier_groups', tier_groups, 'latency_tier_groups_omitted')):
        groups.sort(key=lambda g: (-g['total_s'], g['model'], g['effort'], g.get('tier', '')))
        bump(counters, counter, max(0, len(groups) - 50))
        model[field] = groups[:50]
    # Captured local calendar assignments, including retained history, own these buckets.
    day_values = collections.defaultdict(list)
    day_above = collections.defaultdict(list)
    samples = []
    by_group = collections.defaultdict(list)
    for row in accepted:
        group = (row['model'], row['effort'] or 'unknown')
        by_group[group].append(len(samples))
        samples.append((epoch(row['req_ts']), epoch(row['ts']) - epoch(row['req_ts']),
                        row['usage']['output_tokens'],
                        row['usage']['input_tokens'] - row['usage']['cached_input_tokens']))
    fits = latency._fit_groups(samples, by_group)
    spans = {}
    for row in accepted:
        day = row['local_day']
        if day is None:
            continue
        d = epoch(row['ts']) - epoch(row['req_ts'])
        day_values[day].append(d)
        spans[day] = row['day_start'], row['day_end']
        fit = fits.get((row['model'], row['effort'] or 'unknown'))
        if fit is not None:
            u = row['usage']
            day_above[day].append(max(0, d - fit['a'] - fit['b'] * u['output_tokens']
                                      - fit['c'] * (u['input_tokens'] - u['cached_input_tokens'])))
    model['daily'] = [dict(latency._summary(v), date=d, start=spans[d][0], end=spans[d][1],
                           median_above_s=latency._r(latency._pct(sorted(day_above[d]), .5)))
                      for d, v in sorted(day_values.items())]
    ends = [epoch(r['ts']) for r in accepted]
    model['plan'], model['plan_source'], plan_q = (
        windows.plan_for_interval(min(ends), max(ends), ledger['account_snapshots'])
        if ends else (None, None, {}))
    for name, value in plan_q.items():
        bump(counters, name, value)
    return model, counters


def _rate_limits(built, rows, prices, now, quote_row):
    """Legacy chart fields beside the separate observation-span numerator."""
    by_key = {r['response_key']: r for r in rows}
    table = prices[0] if isinstance(prices, tuple) else prices
    row_prices, ends = {}, {}
    for row in rows:
        key = row['response_key']
        ends[key] = epoch(row['ts'])
        quote = quote_row(row, table)
        row_prices[key] = ((quote['tokens_usd'] or 0) + quote['web_search_usd']
                           if quote['tokens_usd'] is not None or quote['web_search_usd'] else None)
    records = []
    for index, window in enumerate(built):
        members = [by_key[k] for k in window['nominal_rows']]
        running, value, priced = collections.Counter(), 0.0, False
        cum_points, usd_points = [], []
        for row in members:
            u, key = row['usage'], row['response_key']
            running.update(input=u['input_tokens'], cached=u['cached_input_tokens'], output=u['output_tokens'])
            # Legacy columns: timestamp, cumulative input, cumulative uncached, output.
            # Their input + output is the nominal recorded-token curve.
            cum_points.append([ends[key], running['input'], running['input'] - running['cached'], running['output']])
            quote = row_prices[key]
            value += quote or 0
            priced = priced or quote is not None
            usd_points.append([ends[key], round(value, 6)])
        local = dict(window)
        local.update(index=index, reset_at=window['nominal_start'], reset_at_iso=_iso(window['nominal_start']),
                     resets_at=window['reset_at'], resets_at_iso=_iso(window['reset_at']),
                     reset_inferred=True, anchor_inferred=True, anchor=window['nominal_start'],
                     tokens=dict(window['nominal_counts'], uncached=window['nominal_counts']['input'] - window['nominal_counts']['cached']),
                     observation_counts=dict(window['counts']),
                     cum_points=_downsample(cum_points), usd=round(value, 6) if priced else None,
                     usd_points=_downsample(usd_points) if priced else [], late_points=0,
                     observations=len(window['readings']), expired=window['reset_at'] <= now,
                     start=windows._wire_entry(window)['start'], window_minutes=300 if window['kind'] == 'session' else 10080,
                     **window['counts'])
        records.append(local)
    weekly = [w for w in records if w['kind'] == 'weekly_all']
    current = max(weekly, key=lambda w: (max(epoch(r['ts']) or 0 for r in w['readings']), w['resets_at'])) if weekly else None
    # Scoped and five-hour observations stay distinct local series, with their own kinds.
    return {'available': bool(records), 'reason': None if records else 'no structured limit readings in range',
            'window_minutes': 10080, 'weekly': bool(weekly), 'windows': records,
            'windows_total': len(records), 'current': current,
            'observations': sum(w['observations'] for w in weekly),
            'quotes': len({r['resets_at'] for w in weekly for r in w['readings']}),
            'idle_windows': sum(w['peak_pct'] == 0 for w in weekly),
            'overlapping': sum('window_overlapping_span' in w['withheld_reasons'] for w in weekly),
            'late_readings': 0, 'boundary_without_drop': 0,
            'other_windows': [{'kind': kind, 'window_minutes': 300 if kind == 'session' else 10080,
                               'observations': sum(w['observations'] for w in records if w['kind'] == kind)}
                              for kind in ('weekly_scoped', 'session') if any(w['kind'] == kind for w in records)],
            'plans': dict(collections.Counter(w['plan'] for w in weekly if w['plan'])), 'now': now}


def _logged_turns(ledger, rows, counters):
    keys = {(r['stream_id'], r['turn']) for r in rows}
    values = []
    for fact in ledger['turns']:
        ms = fact['logged_duration_ms']
        if (fact['stream_id'], fact['turn']) not in keys or ms is None:
            continue
        if type(ms) is not int or ms <= 0:
            bump(counters, 'logged_turn_invalid')
        elif ms / 1000 > latency.TURN_CAP_S:
            bump(counters, 'logged_turn_over_cap')
        else:
            values.append(ms / 1000)
    return dict(latency._summary(values), available=bool(values))


def analyze(ledger, *, scope=None, since=None, until=None, session=None,
            live_only=False, metrics_only=False, prices=None, now=None) -> ReportModel:
    current = _now(now)
    for bound in (since, until):
        if bound is not None:
            datetime.date.fromisoformat(bound)
    if since is not None and until is not None and since > until:
        raise ValueError('inverted analysis range')
    prices = pricing.load() if prices is None else prices
    quotes = {}

    def quote_row(row, table):
        key = id(row)
        if key not in quotes:
            quotes[key] = pricing.price_row(row, table)
        return quotes[key]

    def summarize(selected):
        return pricing._summarize(selected, prices, quote_row)
    focused = {info['family_id'] for info in ledger.get('_source_families', {}).values()
               if info['session_id'] == session or info['family_id'] == session} if session is not None else set()
    def source_matches(source_id):
        info = ledger.get('_source_families', {}).get(source_id, {})
        return session is None or info.get('family_id') == session or info.get('family_id') in focused or info.get('stream_id') == session
    rows = [r for r in ledger['rows'] if _selected(r, since, until)
            and (session is None or r['family_id'] == session or r['family_id'] in focused or r['stream_id'] == session)
            and (not live_only or not r.get('archived'))]
    counters = dict(ledger['counters'])
    if (since is not None or until is not None) and any(counters.get(n) for n in (
            'unparseable_records', 'non_object_records', 'malformed_records', 'trailing_partial_lines')):
        bump(counters, 'damage_outside_window')
    groups, families, days = (collections.defaultdict(list) for _ in range(3))
    for row in rows:
        groups[row['model']].append(row)
        if row['family_id'] is not None:
            families[row['family_id']].append(row)
        if row['local_day'] is not None:
            days[row['local_day']].append(row)
    sessions = []
    for family, family_rows in families.items():
        original = ledger['families'][family]
        attribution = {r['source_id']: {'orphan_main': original['orphan_main']} for r in family_rows}
        summary = _family_summary(family, family_rows, attribution)
        summary['local_day'] = original['local_day']
        summary.update(session_id=family, threads=len(summary['streams']), first=summary['start'],
                       last=summary['end'], uncached=summary['input'] - summary['cached'],
                       api_usd=summarize(family_rows)['usd'])
        sessions.append(summary)
    sessions.sort(key=lambda s: (-s['input'], s['family_id']))
    daily = []
    for day, day_rows in sorted(days.items()):
        counts = _counts(day_rows)
        counts.update(date=day, start=day_rows[0]['day_start'], end=day_rows[0]['day_end'],
                      sessions=sum(s['local_day'] == day for s in sessions),
                      models={m: sum(r['usage']['input_tokens'] for r in day_rows if r['model'] == m)
                              for m in sorted({r['model'] for r in day_rows})},
                      uncached=counts['input'] - counts['cached'],
                      api_usd=summarize(day_rows)['usd'])
        daily.append(counts)
    model_rows = []
    for name, named_rows in groups.items():
        counts = _counts(named_rows)
        counts.update(model=name, uncached=counts['input'] - counts['cached'],
                      api_usd=summarize(named_rows)['usd'])
        model_rows.append(counts)
    model_rows.sort(key=lambda m: (-m['input'], m['model']))
    live_snapshots = {tuple(k) for k in ledger.get('_live_snapshot_keys', [])}
    content = [] if metrics_only else [f for f in ledger['content'] if _selected(f, since, until)
                                      and (session is None or f['family_id'] == session or f['family_id'] in focused or f['family_id'] in families)
                                      and (not live_only or f['item_key'] in ledger.get('_live_content_keys', {})
                                           or f['snapshot'] and (f['family_id'], f['category'], f['body_digest']) in live_snapshots)]
    cat_bytes, cat_items = collections.Counter(), collections.Counter()
    series = collections.defaultdict(collections.Counter)
    image_facts = []
    for fact in content:
        if fact['image'] is not None:
            image_facts.append(copy.deepcopy(fact['image']))
            continue
        cat_items[fact['category']] += 1
        if fact['utf8_bytes'] is None:
            continue
        cat_bytes[fact['category']] += fact['utf8_bytes']
        stamp = epoch(fact['ts'])
        if stamp is not None:
            series[int(stamp // CAT_BUCKET_S) * CAT_BUCKET_S][fact['category']] += fact['utf8_bytes']
    value = summarize(rows)
    for name, count in value.items():
        if name in COUNTER_NAMES and type(count) is int:
            # Some evidence flags are already on the ledger; pricing is their final count.
            if count:
                counters[name] = count
    timing_view = dict(ledger, tools=[t for t in ledger['tools']
                                    if _selected({'ts': t['end_ts']}, since, until)
                                    and (not live_only or t['tool_key'] in ledger.get('_live_tool_keys', {}))],
                       turns=[dict(t, logged_duration_ms=None, logged_record_key=None)
                              if live_only and t['logged_record_key'] not in ledger.get('_live_logged_keys', {}) else t
                              for t in ledger['turns']])
    lat, lat_q = _timing(timing_view, rows)
    for name, count in lat_q.items():
        bump(counters, name, count)
    share_lat, _ = _timing(timing_view, rows, since=current - 30 * 86400)
    readings = [r for r in ledger['limits'] if _selected(r, since, until) and source_matches(r['source_id'])
                and (not live_only or r['reading_key'] in ledger.get('_live_limit_keys', {}))]
    built_windows, window_q = windows.build_windows(readings, rows,
        account_snapshots=ledger['account_snapshots'], established=ledger.get('_reset_clusters', ()), now=current)
    for name, count in window_q.items():
        bump(counters, name, count)
    rate_limits = _rate_limits(built_windows, rows, prices, current, quote_row)
    logged = _logged_turns(timing_view, rows, counters)
    event_days = collections.Counter()
    undated = 0
    selected_sources = {r['source_id'] for r in rows}
    for event in ledger['events']:
        if not _selected(event, since, until) or not source_matches(event['source_id']):
            continue
        if live_only and event['event_key'] not in ledger.get('_live_event_keys', {}):
            continue
        day = _day(event['ts'])
        if day is None:
            undated += 1
        else:
            event_days[day] += 1
    totals = _counts(rows)
    totals.update(total_tokens=totals['input'] + totals['output'], uncached=totals['input'] - totals['cached'],
                  cache_hit=totals['cached'] / totals['input'] if totals['input'] else None,
                  sessions=len(sessions), threads=len({r['stream_id'] for r in rows}),
                  files=len(selected_sources), bytes=sum(cat_bytes.values()), input_source='recorded',
                  base_input=sum(r['base_input_tokens'] for r in rows),
                  cache_creation=sum(r['cache_creation_input_tokens'] for r in rows),
                  cache_write_5m=sum(r['cache_write_5m'] or 0 for r in rows),
                  cache_write_1h=sum(r['cache_write_1h'] or 0 for r in rows),
                  cache_write_unknown=sum(r['cache_creation_input_tokens'] - (r['cache_write_5m'] or 0)
                                          - (r['cache_write_1h'] or 0) for r in rows),
                  thinking_unavailable=sum(r['usage']['reasoning_output_tokens'] is None for r in rows),
                  archived_responses=sum(bool(r.get('archived')) for r in rows))
    report_scope = dict(scope or {})
    report_scope.update(since=since, until=until, session=session, live_only=live_only, metrics_only=metrics_only)
    checks = [c for c in ledger['cost_checks'] if _selected(c, since, until) and source_matches(c['source_id'])]
    return {'schema': 1, 'client': 'claude-usage',
            'generated_at': datetime.datetime.fromtimestamp(current, datetime.timezone.utc).isoformat(timespec='seconds'),
            'scope': report_scope, 'totals': totals, 'daily': daily, 'models': model_rows, 'sessions': sessions,
            'categories': [{'category': c, 'label': composition.LABELS[c], 'bytes': cat_bytes[c],
                            'items': cat_items[c]} for c in composition.CATEGORIES],
            'cat_series': [[t, dict(c)] for t, c in sorted(series.items())], 'cat_bucket_s': CAT_BUCKET_S,
            'images': {'count': len(image_facts), 'facts': image_facts,
                       'estimated_visual_tokens': sum(f['estimated_visual_tokens'] or 0 for f in image_facts)},
            'rate_limits': rate_limits,
            'limit_events': {'total': sum(event_days.values()) + undated, 'undated': undated,
                             'daily': [dict(date=d, n=n, start=_day_span(d)[0], end=_day_span(d)[1])
                                       for d, n in sorted(event_days.items())]},
            'latency': lat, '_share_latency': share_lat, 'api_value': value,
            'quality': dict(sorted(counters.items())),
            'coverage': ledger['coverage'],
            'account': ledger.get('account', {'available': False, 'reason': 'not requested'}),
            'logged_turns': logged, 'crosschecks': pricing._crosscheck(checks, rows, prices, quote_row),
            'reconciliation': dict(UNSUPPORTED), 'resend_cost': dict(UNSUPPORTED),
            'amplification': dict(UNSUPPORTED), 'cache_leads': dict(UNSUPPORTED)}


def public_model(model) -> ReportModel:
    """Allow-list public aggregates; identities, paths and inferred images stay local."""
    def fields(value, names):
        return {k: copy.deepcopy(value[k]) for k in names if k in value}

    def named_counts(value):
        result = fields(value, COUNTS + ('uncached', 'api_usd'))
        name = value.get('model')
        result['model'] = name if isinstance(name, str) and pricing.MODEL_RE.fullmatch(name) else 'unknown'
        return result

    def safe_latency(value):
        out = fields(value, ('available', 'reason', 'responses', 'turns', 'tools_total', 'method', 'plan', 'plan_source'))
        for key in ('responses', 'turns'):
            if key in out:
                out[key] = fields(out[key], ('n', 'total_s', 'median_s', 'p90_s', 'model_s', 'model_share',
                                             'work_s', 'above_s', 'unfit_s', 'above_share', 'fitted_share'))
        out['daily'] = [fields(v, ('date', 'n', 'total_s', 'median_s', 'p90_s', 'median_above_s', 'start', 'end'))
                        for v in value.get('daily', [])]
        for key in ('groups', 'tier_groups'):
            out[key] = []
            for group in value.get(key, []):
                g = fields(group, ('n', 'total_s', 'median_s', 'p90_s', 'output', 'above_s',
                                   'above_share', 'median_above_s'))
                g['model'] = named_counts(group)['model']
                g['effort'] = group['effort'] if group.get('effort') in (
                    'low', 'medium', 'high', 'xhigh', 'max', 'none') else 'unknown'
                if key == 'tier_groups':
                    if group.get('tier') not in pricing.TIER_CLASSES:
                        continue
                    g['tier'] = group['tier']
                fit = group.get('fit')
                g['fit'] = None if fit is None else fields(fit, ('overhead_s', 'output_tps', 'uncached_input_tps', 'samples'))
                out[key].append(g)
        out['tools'] = [fields(v, ('n', 'total_s', 'median_s', 'p90_s')) for v in value.get('tools', [])]
        if 'method' in out:
            out['method'] = fields(out['method'], ('floor_quantile', 'fit_min', 'fit_max',
                                                  'response_cap_s', 'tool_cap_s', 'turn_cap_s'))
        return out

    out = fields(model, ('schema', 'client', 'generated_at', 'cat_bucket_s'))
    out['scope'] = fields(model['scope'], ('since', 'until', 'live_only', 'metrics_only'))
    out['totals'] = fields(model['totals'], COUNTS + ('uncached', 'total_tokens', 'cache_hit', 'sessions',
                                                    'threads', 'files', 'bytes', 'input_source', 'base_input',
                                                    'cache_creation', 'archived_responses', 'cache_write_5m',
                                                    'cache_write_1h', 'cache_write_unknown', 'thinking_unavailable'))
    out['models'] = [named_counts(m) for m in model['models']]
    out['daily'] = []
    for day in model['daily']:
        d = fields(day, COUNTS + ('date', 'start', 'end', 'sessions', 'uncached', 'api_usd'))
        d['models'] = {m: n for m, n in day['models'].items() if pricing.MODEL_RE.fullmatch(m)}
        out['daily'].append(d)
    out['sessions'] = [dict(named_counts(s), **fields(s, ('threads', 'active_s', 'local_day', 'orphan_main')))
                       for s in model['sessions']]
    out['categories'] = [fields(c, ('category', 'label', 'bytes', 'items')) for c in model['categories']
                         if c['category'] in composition.CATEGORIES]
    out['cat_series'] = [[t, {k: v for k, v in c.items() if k in composition.CATEGORIES}]
                         for t, c in model['cat_series']]
    # Wire fields plus chart aggregates. Reading identities and source paths stay local.
    out['rate_limits'] = fields(model['rate_limits'], ('available', 'reason', 'window_minutes', 'weekly',
        'windows_total', 'observations', 'quotes', 'idle_windows', 'overlapping', 'late_readings',
        'boundary_without_drop', 'other_windows', 'plans', 'now'))
    out['rate_limits']['windows'] = []
    for window in model['rate_limits']['windows']:
        if window['kind'] != 'weekly_all':
            continue
        w = fields(window, windows.WIRE_FIELDS)
        w['split'] = [dict(fields(s, windows.SPLIT_FIELDS), model=named_counts(s)['model'])
                      for s in window['split'] if s.get('tier') in pricing.TIER_CLASSES]
        w.update(fields(window, ('kind', 'index', 'reset_at', 'reset_at_iso', 'resets_at', 'resets_at_iso',
            'reset_inferred', 'anchor_inferred', 'anchor', 'tokens', 'observation_counts', 'cum_points',
            'pct_points', 'usd', 'usd_points', 'late_points', 'observations', 'expired', 'last_pct',
            'peak_pct', 'observation_start', 'observation_end')))
        w['readings'] = [fields(r, ('ts', 'percent', 'source'))
                         for r in window['readings']]
        out['rate_limits']['windows'].append(w)
    current = model['rate_limits'].get('current')
    out['rate_limits']['current'] = next((w for w in out['rate_limits']['windows']
        if current and w['index'] == current['index']), None)
    events = model['limit_events']
    out['limit_events'] = dict(fields(events, ('total', 'undated')),
                               daily=[fields(d, ('date', 'n', 'start', 'end')) for d in events['daily']])
    out['latency'] = safe_latency(model.get('_share_latency', model['latency']))
    value = model['api_value']
    out['api_value'] = fields(value, ('available', 'reason', 'usd', 'usd_high', 'tokens_usd',
                                       'web_search_usd', 'web_search_calls', 'as_of', 'responses', 'priced',
                                       'unpriced', 'unpriced_by_reason') + tuple(pricing.ASSUMPTIONS)
                                 + ('unpriced_input', 'unpriced_output', 'unpriced_model', 'unpriced_speed', 'unpriced_geography'))
    out['api_value']['by_model'] = [dict(model=named_counts(v)['model'], **fields(v, ('usd', 'responses')))
                                  for v in value['by_model']]
    out['api_value']['unpriced_models'] = [dict(named_counts(v), **fields(v, ('reason',)))
                                          for v in value['unpriced_models']]
    out['api_value']['prices'] = fields(value['prices'], ('as_of', 'source', 'models', 'default'))
    out['quality'] = {k: v for k, v in model['quality'].items() if k in COUNTER_NAMES}
    coverage = model['coverage']
    out['coverage'] = fields(coverage, ('history_available', 'history_committed', 'token_bound',
                                       'calendar_completeness', 'captured_responses', 'archived_responses',
                                       'safe_months', 'withheld_months', 'windows_replace_safe', 'window_reasons'))
    # AccountInfo identity and detailed cost-state/source diagnostics are local-only.
    out['account'] = {'available': False, 'reason': 'local account details omitted'}
    out['crosschecks'] = {'checks': [], 'unmatched_models': []}
    out['logged_turns'] = fields(model['logged_turns'], ('available', 'n', 'total_s', 'median_s', 'p90_s'))
    for key in ('reconciliation', 'resend_cost', 'amplification', 'cache_leads'):
        out[key] = dict(UNSUPPORTED)
    return out


def claude_profile(model) -> RenderProfile:
    """All Claude wording and chart choices, kept out of the shared defaults."""
    t = model['totals']
    number = lambda n: format(n or 0, ',')
    output_note = '{n} recorded thinking tokens'.format(n=number(t['reasoning']))
    if t.get('thinking_unavailable'):
        output_note += ' · unavailable for {m} responses'.format(m=number(t['thinking_unavailable']))
    notes = [
        'Recorded input includes base input, cache creation and cache reads. Recorded output already includes thinking when that subset is available.',
        'Captured usage can omit calls without transcript usage, activity on other devices, and transcripts removed before the first capture.',
        'Quota points are sparse recorded readings. The report does not derive quota percentages from tokens.',
        'API list value is a comparison at the vendored price table, not a bill. Missing settings, fees and unpriced models are shown separately.',
    ]
    if model.get('latency', {}).get('plan') or model.get('rate_limits', {}).get('plans'):
        notes.extend([
            'Historical plan labels use captured account observations and subscriptionCreatedAt; they are not plan records recovered from transcripts.',
            'A plan change that leaves subscriptionCreatedAt unchanged is not detectable until a later run observes the new tier. A window that ended before that run can therefore carry the old plan.',
        ])
    return {
        'vendor': 'claude', 'title': 'Claude Code Token Report',
        'kickers': {
            'clinical': 'Claude Code usage, recorded locally',
            'matisse': 'Papiers découpés — Claude Code usage, cut from local records',
            'nocturne': 'Nocturne in blue and gold — Claude Code usage, recorded locally',
        },
        'recorded_by': 'Claude Code', 'input_tile_label': 'Recorded input',
        'input_tile_note': 'base input + cache writes + cache reads · {n} responses'.format(n=number(t['responses'])),
        'output_tile_label': 'Output', 'output_tile_note': output_note,
        'cache_tile_label': 'Cache hit',
        'cache_tile_note': '{reads} read · {w5} 5m writes · {w1} 1h writes · {unknown} writes with unknown TTL'.format(
            reads=number(t['cached']), w5=number(t.get('cache_write_5m')),
            w1=number(t.get('cache_write_1h')), unknown=number(t.get('cache_write_unknown'))),
        'sessions_unit': '{n} streams'.format(n=number(t['threads'])),
        'largest_session_label': 'Largest session', 'api_tile_label': 'API list value',
        'api_tile_note': 'captured usage at Anthropic list prices; assumptions and exclusions in JSON and the terminal summary',
        'composition_title': 'Visible text inventory', 'composition_unit': 'UTF-8 bytes in view',
        'composition_accessibility': 'visible text inventory by category',
        'composition_empty': 'No captured text bytes in the visible range.',
        'metrics_only_note': 'Text inventory was skipped with --metrics-only.',
        'composition_note': 'Byte shares describe captured text and saved snapshots. They are not Claude token shares or a reconstruction of the full API prompt.',
        'sparse_limit_points': True, 'weekly_label': 'weekly all-model',
        'missing_weekly': 'No weekly all-model readings in range.',
        'missing_limits': 'No structured limit readings in range.',
        'limit_source_note': 'last recorded weekly reading',
        'expired_reading': 'Reset since the last weekly reading; no current percentage recorded.',
        'anchor_tooltip': 'nominal seven-day start, inferred from reset',
        'observation_tooltip': 'captured usage between the first reading and the first peak reading',
        'refusal_tooltip': 'weekly 429 refusal: 100%, assumed all-model',
        'percentage_legend': 'recorded /usage and weekly 429 points',
        'limit_metric': 'tokens', 'limit_metric_choices': ['tokens', 'usd'],
        'limit_metric_labels': {'tokens': 'Recorded tokens', 'usd': 'API list value'},
        'token_chart_accessibility': 'cumulative recorded input and output per nominal weekly window',
        'timing_note': 'Response time is the interval from the last known prompt-side record to the last response record.',
        'price_source_note': 'Anthropic API price table · {as_of} · vendored locally'.format(
            as_of=model['api_value'].get('as_of') or 'unavailable'),
        'brand_link': 'https://tokenusage.dev', 'standing_notes': notes,
    }
