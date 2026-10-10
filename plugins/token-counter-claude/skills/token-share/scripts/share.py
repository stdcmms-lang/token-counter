#!/usr/bin/env python3
"""Share captured Claude Code usage. Without --yes this sends nothing."""
import argparse
import base64
import collections
import contextlib
import copy
import datetime
import gzip
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import List, Optional

HERE = Path(__file__).resolve().parent
REPORT = HERE.parent.parent / 'token-report' / 'scripts'
sys.path.insert(0, str(REPORT))
import report as reportcli  # noqa: E402 -- also enforces Python 3.8
from tokencounter import analyze, history, paths, pricing, render, share_validate, windows  # noqa: E402
from tokencounter.models import COUNTER_NAMES, bump  # noqa: E402
from tokencounter.worker import epoch  # noqa: E402

CLIENT = {'name': 'claude-usage', 'version': '0.1.0'}
SCHEMA = 1
DEFAULT_API = 'https://tokenusage.dev/api'
RECOVERY = 'Recovery: run share.py --delete --yes using the retained token, then start a fresh first share with share.py --handle HANDLE --yes.'
UNKNOWN = 'First share outcome unknown; no token was received and no automatic retry was made.'
CALENDAR = 'Calendar completeness is unknown; usage missing before the first capture cannot be recovered.'
ARCHIVED = 'Captured usage includes retained history from transcripts that disappeared.'
PLAN_NOTE = 'Historical plan labels use captured account observations and subscriptionCreatedAt; they are not plan records recovered from transcripts.'
PLAN_ASSUMPTION = 'A plan change that leaves subscriptionCreatedAt unchanged is not detectable until a later run observes the new tier. A window that ended before that run can therefore carry the old plan.'
COUNTS = share_validate.COUNT_FIELDS
WINDOW_FIELDS = ('start', 'window_minutes', 'plan', 'first_pct', 'peak_pct') + share_validate.WINDOW_COUNTS + ('split',)

# Exact file format (no legacy/token-import format):
# {"schema":1, "active_endpoint":NORMALIZED_API, "endpoints":{
#   NORMALIZED_API:{"token":TOKEN, "handle":HANDLE, "history_uuid":UUID,
#                   "token_binding":OPAQUE_RECEIPT_GENERATION_ID}}}
# Only a successful POST creates an entry. The opaque binding is allocated before
# prepare(), so the first receipt and every authenticated replacement agree.
STATE_FIELDS = frozenset(('token', 'handle', 'history_uuid', 'token_binding'))


def normalize_endpoint(value):
    try:
        u = urllib.parse.urlsplit(value)
        host = (u.hostname or '').lower().encode('idna').decode('ascii')
        port = u.port
        loopback = host == 'localhost'
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or u.username is not None or u.password is not None or u.query or u.fragment
                or u.scheme not in ('https', 'http') or u.scheme == 'http' and not loopback
                or any(c.isspace() for c in value) or '\\' in value):
            raise ValueError()
        authority = '[' + host + ']' if ':' in host else host
        if port is not None and port != (443 if u.scheme == 'https' else 80):
            authority += ':' + str(port)
        path = u.path.rstrip('/')
        if any(p in ('.', '..') for p in path.split('/')):
            raise ValueError()
        return urllib.parse.urlunsplit((u.scheme, authority, path, '', ''))
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('api_invalid') from None


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog='share.py', description=__doc__, allow_abbrev=False)
    ap.add_argument('--handle', metavar='HANDLE')
    send = ap.add_mutually_exclusive_group()
    send.add_argument('--yes', action='store_true')
    send.add_argument('--out', metavar='PATH')
    actions = ap.add_mutually_exclusive_group()
    actions.add_argument('--delete', action='store_true')
    actions.add_argument('--delete-report', action='store_true')
    ap.add_argument('--no-report', action='store_true')
    ap.add_argument('--style', choices=[s for s, _ in render.STYLES], default='clinical')
    ap.add_argument('--api', default=os.environ.get('TOKENUSAGE_API') or DEFAULT_API)
    ap.add_argument('--sessions-root', metavar='PATH')
    ap.add_argument('--no-account', action='store_true')
    ap.add_argument('--prices', metavar='PATH')
    ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--rebuild', action='store_true')
    pool = ap.add_mutually_exclusive_group()
    pool.add_argument('--procs', type=int, metavar='N')
    pool.add_argument('--fast', action='store_true')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--version', action='version', version='token-counter-claude ' + CLIENT['version'])
    a = ap.parse_args(argv)
    if (a.delete or a.delete_report) and (a.out or a.handle or a.no_report
            or a.no_account or a.prices or a.no_cache or a.rebuild or a.fast or a.procs is not None
            or a.style != 'clinical'):
        ap.error('deletion is an exclusive action')
    try:
        a.api = normalize_endpoint(a.api)
    except ValueError:
        ap.error('--api requires HTTPS or HTTP loopback without credentials, query or fragment')
    if a.handle is not None:
        a.handle = a.handle.strip().lower()
        if not share_validate.valid_handle(a.handle):
            ap.error('--handle must be 3-24 lowercase letters, digits and single hyphens')
    # Use the report's own worker/default rules and its complete collection path.
    rargs = ['--quiet', '--no-open']
    if a.procs is not None: rargs += ['--procs', str(a.procs)]
    if a.fast: rargs += ['--fast']
    a.procs = reportcli.parse_args(rargs).procs
    a.metrics_only = False
    return a


def _iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def session_hash(family_id) -> str:
    return hashlib.sha256(b'tokenusage.dev:claude-code:v1\0' + family_id.encode('utf-8')).hexdigest()[:16]


def active_seconds(epochs) -> int:
    ordered = sorted(set(epochs))
    return int(round(sum(b - a for a, b in zip(ordered, ordered[1:]) if 0 <= b - a <= 1800)))


def _counts(rows):
    return dict(responses=len(rows), input=sum(r['usage']['input_tokens'] for r in rows),
                cached=sum(r['usage']['cached_input_tokens'] for r in rows),
                output=sum(r['usage']['output_tokens'] for r in rows),
                reasoning=sum(r['usage']['reasoning_output_tokens'] or 0 for r in rows))


def _session_summary(family, rows, info, safe_months):
    if (not rows or any(r['local_day'] is None or r['local_day'][:7] not in safe_months for r in rows)
            or not info.get('local_day') or info['local_day'][:7] not in safe_months):
        return None
    ends = [epoch(r['ts']) for r in rows]
    if any(e is None for e in ends):
        return None
    start, end, active = min(ends), max(ends), active_seconds(ends)
    models = collections.Counter()
    for r in rows:
        models[r['model']] += r['usage']['input_tokens']
    model = min(models, key=lambda m: (-models[m], m))
    if end - start > share_validate.MAX_SPAN_S or active > share_validate.MAX_ACTIVE_S or len(model) > 80:
        return None
    return dict(id=session_hash(family), day=info['local_day'], start=_iso(start), end=_iso(end),
                active_s=active, model=model, **_counts(rows))


def _session_rank(s):
    return (-s['input'] - s['output'], -s['active_s'], s['id'])


def select_sessions(rows, families, safe_months) -> List[dict]:
    groups = collections.defaultdict(list)
    for row in rows:
        if row['family_id'] is not None:
            groups[row['family_id']].append(row)
    months = collections.defaultdict(list)
    for family, members in sorted(groups.items()):
        summary = _session_summary(family, members, families[family], safe_months)
        if summary is not None:
            months[summary['day'][:7]].append(summary)
    selected = []
    for month in sorted(months, reverse=True):
        members = months[month]
        active = sorted(members, key=lambda s: (-s['active_s'], -s['input'] - s['output'], s['id']))[:10]
        tokens = sorted(members, key=_session_rank)[:10]
        union = {s['id']: s for s in active + tokens}
        selected.extend(sorted(union.values(), key=_session_rank))
    return selected[:share_validate.MAX_SESSIONS]


def _wire_group(g):
    fit = g.get('fit') or {}
    out = {k: g[k] for k in ('model', 'effort', 'n', 'median_s', 'p90_s')}
    out.update(overhead_s=fit.get('overhead_s'), output_tps=fit.get('output_tps'), above_share=g.get('above_share'))
    if 'tier' in g: out['tier'] = g['tier']
    return out


def _latency_with_counters(ledger, safe_months, *, now):
    rows = [r for r in ledger['rows'] if r['local_day'] and r['local_day'][:7] in safe_months]
    current = analyze._now(now)
    lat, counters = analyze._timing(ledger, rows, since=current - 30 * 86400)
    # Ephemeral only: neither history nor a report/payload serializes this adapter.
    ledger['_share_timing'] = {'now': current, 'rows': tuple(r['response_key'] for r in rows), 'model': lat}
    if not lat['available'] or not lat['daily']:
        return None, counters
    r, t = lat['responses'], lat['turns']
    dates = [d['date'] for d in lat['daily']]
    return {'from': min(dates), 'to': max(dates), 'plan': lat['plan'],
            'responses': {k: r[k] for k in ('n', 'median_s', 'p90_s')},
            'turns': {k: t[k] for k in ('n', 'median_s', 'p90_s', 'model_share')} if t['n'] else None,
            'groups': [_wire_group(g) for g in lat['groups'][:50]],
            'tier_groups': [_wire_group(g) for g in lat['tier_groups'][:50]]}, counters


def latency_summary(ledger, safe_months, *, now) -> Optional[dict]:
    return _latency_with_counters(ledger, safe_months, now=now)[0]


def _wire_window(w):
    return {k: copy.deepcopy(w[k]) for k in WINDOW_FIELDS if k in w}


def _window_in_months(window, rows, safe):
    keys = set(window['rows'])
    return all(r['local_day'] and r['local_day'][:7] in safe for r in rows if r['response_key'] in keys)


def _advances(new, old):
    if (epoch(new['start']) != epoch(old['start']) or new['first_pct'] != old['first_pct']
            or new['peak_pct'] < old['peak_pct']
            or any(new[k] < old[k] for k in share_validate.WINDOW_COUNTS)):
        return False
    split = {(s['model'], s['tier']): s for s in new.get('split', [])}
    for row in old.get('split', []):
        current = split.get((row['model'], row['tier']))
        if current is None or any(current.get(k, 0) < row.get(k, 0) for k in
                share_validate.WINDOW_COUNTS + ('cache_write_5m', 'cache_write_1h')):
            return False
    return True


def _window_union(entries, built, coverage, ledger):
    """Reproduce retained spans, then advance them; never prune published entries."""
    if entries is None:
        return None, []
    union = {epoch(w['start']): _wire_window(w) for w in entries}
    local = {epoch(_iso(w['nominal_start'])): w for w in built if w['kind'] == 'weekly_all' and w['shareable']}
    evidence = {key: w for key, w in local.items() if key in union}
    for receipt in coverage.get('_published', []):
        prior = json.loads(receipt['payload'])
        retained = receipt['contributors'].get('windows', [])
        for old in prior.get('windows', []):
            key = epoch(old['start'])
            source = next((w for w in retained if epoch(w.get('start')) == key), None)
            if source is None or not source.get('readings'):
                return None, []
            reconstructed, _q = windows.build_windows(source['readings'], ledger['rows'],
                account_snapshots=ledger['account_snapshots'], established=ledger.get('_reset_clusters', ()))
            candidate = next((w for w in reconstructed if w['kind'] == 'weekly_all'
                              and epoch(_iso(w['nominal_start'])) == key and w['shareable']), None)
            if candidate is None:
                return None, []
            wire = _wire_window(windows._wire_entry(candidate))
            if not _advances(wire, old):
                return None, []
            current = union.get(key)
            if current is None or not _advances(current, wire):
                union[key], evidence[key] = wire, candidate
    out = [union[k] for k in sorted(union)]
    return out or None, [evidence[k] for k in sorted(evidence) if k in union]


def _window_budget(entries):
    return (entries is not None and (len(entries) > share_validate.MAX_WINDOWS
            or any(len(w.get('split', [])) > share_validate.MAX_SPLIT_ROWS for w in entries)
            or sum(len(w.get('split', [])) for w in entries) > share_validate.MAX_SPLIT_TOTAL))


def _contributors(ledger, payload, window_evidence):
    by_family = collections.defaultdict(list)
    months = {}
    safe = {d['date'][:7] for d in payload['days']}
    for row in ledger['rows']:
        by_family[row['family_id']].append(row)
        if row['local_day'] and row['local_day'][:7] in safe:
            months.setdefault(row['local_day'][:7], {'responses': {}, 'sessions': []})['responses'][row['response_key']] = history._row_snapshot(row)
    for family, rows in by_family.items():
        if family is None or session_hash(family) not in {s['id'] for s in payload['sessions']}:
            continue
        source_ids = {r['source_id'] for r in rows}
        facts = [f for f in ledger.get('_retention', {}).get('fact_digests', [])
                 if f['source_id'] in source_ids and f['kind'] in ('links', 'turns', 'calendar')]
        month = ledger['families'][family]['local_day'][:7]
        months[month]['sessions'].append({'responses': sorted(r['response_key'] for r in rows), 'facts': facts})
    rows = {r['response_key']: r for r in ledger['rows']}
    return {'months': months, 'windows': [dict(start=_iso(w['nominal_start']), window_key=w['window_key'],
        observation_start=w['observation_start'], observation_end=w['observation_end'],
        rows=list(w['rows']), responses={k: history._row_snapshot(rows[k]) for k in w['rows']},
        readings=copy.deepcopy(w['readings'])) for w in window_evidence]}


def _days(ledger, safe):
    days = collections.defaultdict(list)
    for row in ledger['rows']:
        if row['local_day'] and row['local_day'][:7] in safe:
            days[row['local_day']].append(row)
    out = []
    for day, members in sorted(days.items()):
        tiers = {t: _counts([r for r in members if r['tier'] == t]) for t in pricing.TIER_CLASSES
                 if any(r['tier'] == t for r in members)}
        out.append(dict(date=day, sessions=sum(f['local_day'] == day for f in ledger['families'].values()),
                        tiers=tiers, **_counts(members)))
    return out


def _withheld_session_count(rows, families, safe):
    unsafe = {r['family_id'] for r in rows if r['family_id'] is not None
              and (not r['local_day'] or r['local_day'][:7] not in safe)}
    unsafe.update(f for f, info in families.items() if not info['local_day'] or info['local_day'][:7] not in safe)
    if not unsafe:
        return 0
    months = {r['local_day'][:7] for r in rows if r['local_day']}
    months.update(info['local_day'][:7] for info in families.values() if info['local_day'])
    candidates = {s['id'] for s in select_sessions(rows, families, months)}
    return len(candidates & {session_hash(f) for f in unsafe})


def build_payload(ledger, coverage, *, handle=None, now=None) -> tuple:
    current = analyze._now(now)
    safe = set(coverage['safe_months'])
    withheld = copy.deepcopy(coverage['withheld_months'])
    counters = dict(ledger['counters'])
    decisions = {}
    built, window_q = windows.build_windows(ledger['limits'], ledger['rows'],
        account_snapshots=ledger['account_snapshots'], established=ledger.get('_reset_clusters', ()), now=current)
    for name, value in window_q.items():
        counters[name] = max(counters.get(name, 0), value)
    while True:
        payload = {'schema': SCHEMA, 'client': dict(CLIENT), 'generated_at': _iso(current),
                   'days': _days(ledger, safe), 'sessions': select_sessions(ledger['rows'], ledger['families'], safe)}
        if handle is not None: payload['handle'] = handle
        entries, reasons = windows.wire_windows(built, coverage)
        by_start = {w['nominal_start']: w for w in built}
        entries = None if entries is None else [_wire_window(w) for w in entries
            if _window_in_months(by_start[epoch(w['start'])], ledger['rows'], safe)]
        entries, evidence = _window_union(entries, built, coverage, ledger)
        window_reason = 'windows_withheld_retention' if entries is None and coverage.get('_published') else None
        if any(not _window_in_months(w, ledger['rows'], safe) for w in evidence):
            entries, evidence, window_reason = None, [], 'windows_withheld_retention'
        if _window_budget(entries):
            entries, evidence, window_reason = None, [], 'windows_withheld_schema_budget'
        if entries:
            payload['windows'] = entries
        lat, timing_q = _latency_with_counters(ledger, safe, now=current)
        if lat: payload['latency'] = lat
        # A retained window touching a withheld month must not exceed the submitted
        # day totals. Omitting the whole key also preserves server storage semantics.
        issues = share_validate.validate_payload(payload, now=current)
        if any(i.startswith(('window_', 'split_')) for i in issues):
            payload.pop('windows', None)
            evidence, window_reason = [], 'windows_withheld_retention'
        bad_months = set()
        latest = datetime.datetime.fromtimestamp(current + 86400, datetime.timezone.utc).date().isoformat()
        for month in sorted(safe, reverse=True):
            days = [d for d in payload['days'] if d['date'][:7] == month]
            if (any(not share_validate.EARLIEST_DAY <= d['date'] <= latest or d['input'] > 20000000000 for d in days)
                    or len(days) > share_validate.MAX_DAYS
                    or len(share_validate.payload_bytes(dict(payload, days=days, sessions=[], windows=[]))) > share_validate.MAX_BODY_BYTES):
                bad_months.add(month)
        if not bad_months and (len(payload['days']) > share_validate.MAX_DAYS
                or len(share_validate.payload_bytes(payload)) > share_validate.MAX_BODY_BYTES):
            if safe: bad_months.add(min(safe))
        if not bad_months:
            break
        for month in bad_months:
            safe.remove(month)
            withheld[month] = [history.MONTH_REASON.format(month=month)]
            decisions[month] = ['months_withheld_schema_budget']
            bump(counters, 'months_withheld_schema_budget')
    if window_reason:
        bump(counters, window_reason)
    for name, value in timing_q.items():
        bump(counters, name, value)
    counters['months_withheld'] = len(withheld)
    counters['sessions_withheld_month'] = _withheld_session_count(ledger['rows'], ledger['families'], safe)
    shared = [r for r in ledger['rows'] if r['local_day'] and r['local_day'][:7] in safe]
    decisions = dict(coverage.get('_month_reasons', {}), **decisions)
    notes = {'safe_months': sorted(safe), 'withheld_months': withheld, 'month_reasons': decisions,
             'window_reasons': reasons, 'window_decision': window_reason,
             'sessions_total': len(ledger['families']), 'speed_unrecorded': sum(r['tier'] not in pricing.TIER_CLASSES for r in shared),
             'archived_responses': sum(bool(r.get('archived')) for r in shared), 'counters': counters,
             'contributors': _contributors(ledger, payload, evidence)}
    return payload, notes


def build_public_report(ledger, coverage, *, style=None, now=None) -> tuple:
    selected = dict(ledger)
    safe = set(coverage['safe_months'])
    selected['rows'] = [r for r in ledger['rows'] if r['local_day'] and r['local_day'][:7] in safe]
    selected['limits'] = [r for r in ledger['limits'] if (analyze._day(r.get('ts')) or '')[:7] in safe]
    selected['events'] = [r for r in ledger['events'] if (analyze._day(r.get('ts')) or '')[:7] in safe]
    selected['content'] = [f for f in ledger['content'] if (analyze._day(f.get('ts')) or '')[:7] in safe]
    selected['coverage'] = {k: copy.deepcopy(v) for k, v in coverage.items() if not k.startswith('_')}
    model = analyze.public_model(analyze.analyze(selected, prices=ledger.get('_share_prices'), now=now))
    html = render.render(model, public=True, style=style or 'clinical', profile=analyze.claude_profile(model))
    return model, html.encode('utf-8')


def report_body(html_bytes) -> dict:
    return {'schema': SCHEMA, 'client': dict(CLIENT),
            'html_gz': base64.b64encode(gzip.compress(html_bytes, compresslevel=9, mtime=0)).decode('ascii')}


def load_state(paths) -> dict:
    try:
        with open(paths['share_state_path'], 'rb') as fh:
            state = json.load(fh)
        if (not isinstance(state, dict) or set(state) != {'schema', 'active_endpoint', 'endpoints'}
                or type(state['schema']) is not int or state['schema'] != 1 or not isinstance(state['endpoints'], dict)
                or normalize_endpoint(state['active_endpoint']) != state['active_endpoint']):
            raise ValueError()
        for endpoint, entry in state['endpoints'].items():
            if (normalize_endpoint(endpoint) != endpoint or not isinstance(entry, dict) or set(entry) != STATE_FIELDS
                    or any(not isinstance(entry[k], str) or not entry[k] for k in STATE_FIELDS)
                    or not share_validate.valid_handle(entry['handle'])):
                raise ValueError()
        return state
    except FileNotFoundError:
        return {'schema': 1, 'active_endpoint': DEFAULT_API, 'endpoints': {}}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        # A damaged credential file must never silently become a first share.
        raise ValueError('share_state_invalid') from None


def save_state(paths, state) -> None:
    target = paths['share_state_path']
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.claude-share-', suffix='.tmp', dir=str(target.parent))
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, 'wb') as fh:
            fd = None
            fh.write(share_validate.payload_bytes(state))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, target)
    except BaseException:
        if fd is not None: os.close(fd)
        with contextlib.suppress(OSError): os.remove(temporary)
        raise


def request(method, url, *, body=None, token=None, timeout=60):
    """The only network function; JSON bytes are preserved and redirects refused."""
    normalize_endpoint(url)
    data = body if isinstance(body, bytes) else None if body is None else share_validate.payload_bytes(body)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('User-Agent', CLIENT['name'] + '/' + CLIENT['version'])
    req.add_header('Accept', 'application/json')
    if data is not None: req.add_header('Content-Type', 'application/json')
    if token: req.add_header('Authorization', 'Bearer ' + token)
    redirect = urllib.request.HTTPRedirectHandler()
    redirect.redirect_request = lambda req, fp, code, msg, headers, newurl: None
    opener = urllib.request.build_opener(redirect)  # environment proxies apply, as in Codex's share
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw, status = resp.read(share_validate.MAX_BODY_BYTES + 1), resp.status
    except urllib.error.HTTPError as exc:
        with exc:
            raw, status = exc.read(share_validate.MAX_BODY_BYTES + 1), exc.code
    if len(raw) > share_validate.MAX_BODY_BYTES:
        return status, None
    try:
        return status, json.loads(raw.decode('utf-8')) if raw else None
    except (ValueError, UnicodeError):
        return status, None


def describe(payload, notes, *, handle, api) -> str:
    days = payload['days']
    lines = ['{} responses | {} recorded input | {} cached | {} output | {} recorded reasoning'.format(
        *(sum(d[k] for d in days) for k in COUNTS)),
        '{} days | {} selected sessions of {}'.format(len(days), len(payload['sessions']), notes['sessions_total'])]
    for month in notes['safe_months']:
        lines.append('Month {}: safe (captured contributors retained; whole captured month).'.format(month))
    for month, reasons in sorted(notes['withheld_months'].items()):
        lines.append(history.MONTH_REASON.format(month=month))
        lines.append('  ' + ', '.join(notes['month_reasons'].get(month) or ['months_withheld_missing_contributors']))
    if 'windows' in payload:
        lines.append('Windows: send {} retained comparison windows.'.format(len(payload['windows'])))
    else:
        if notes['window_decision'] or not notes.get('windows_replace_safe', True):
            lines.append(history.WINDOW_REASON)
        lines.append('Windows key: omitted.')
    for reasons in notes['window_reasons'].values():
        if reasons: lines.append('  ' + ', '.join(reasons))
    lat = payload.get('latency')
    lines.append('Timing: {} responses, median {}s, p90 {}s.'.format(
        lat['responses']['n'], lat['responses']['median_s'], lat['responses']['p90_s']) if lat else 'Timing: no accepted responses in the last 30 days.')
    if notes['archived_responses']:
        lines.extend([ARCHIVED, 'captured history: {} responses from transcripts no longer on disk'.format(notes['archived_responses'])])
    lines.extend([CALENDAR, PLAN_NOTE, PLAN_ASSUMPTION,
        '{} shared responses had no recorded speed; their counts are outside tiers and split.'.format(notes['speed_unrecorded'])])
    for name in ('window_rows_fast', 'window_rows_us', 'window_rows_haiku_long'):
        lines.append('{}: {}'.format(name, notes['counters'].get(name, 0)))
    for name, value in sorted(notes['counters'].items()):
        if value and name in COUNTER_NAMES and name not in ('window_rows_fast', 'window_rows_us', 'window_rows_haiku_long'):
            lines.append('{}: {}'.format(name, value))
    return '\n'.join(lines)


def _refuse_binding():
    print(history.TOKEN_REASON, file=sys.stderr)
    print(RECOVERY, file=sys.stderr)
    return 4


def _check_binding(entry, store, coverage):
    return (not entry or store.history_uuid == entry['history_uuid'] and coverage['token_bound'])


def _endpoint_entry(state, endpoint):
    return state['endpoints'].get(endpoint, {})


def _delete(a, resolved, state, mine):
    if not mine:
        print('No share token for this endpoint.', file=sys.stderr)
        return 2
    if not a.yes:
        print('Would delete the uploaded report page.' if a.delete_report else 'Would delete the public usage and report page.')
        print('Dry run: nothing was sent.')
        return 0
    store = history.History(resolved['history_path']) if resolved['history_path'].exists() else None
    try:
        if store is not None: store.check_integrity()
        try:
            status, _body = request('DELETE', a.api + ('/report' if a.delete_report else '/share'), token=mine['token'])
        except (OSError, http.client.HTTPException):
            print('Deletion outcome unknown; no automatic retry was made.', file=sys.stderr)
            return 6
        if status != 200:
            print('Deletion refused (HTTP {}).'.format(status), file=sys.stderr)
            return 7
        if a.delete:
            # The server no longer knows this token, so the entry goes even when the
            # local receipts could not be retired; keeping it would resend a dead token.
            retired = True
            if store is not None and store.history_available and store.history_uuid == mine['history_uuid']:
                try:
                    store.retire_token(mine['token_binding'])
                    store.commit()
                except sqlite3.Error:
                    store.history_committed = False
                retired = store.history_committed
            state['endpoints'].pop(a.api, None)
            state['active_endpoint'] = a.api
            save_state(resolved, state)
            if not retired:
                print('Public deletion succeeded; local receipt retirement failed.', file=sys.stderr)
                return 5
        print('Deleted the uploaded report page.' if a.delete_report else 'Deleted the public usage and report page.')
        return 0
    finally:
        if store is not None: store.close()


def main(argv=None) -> int:
    a = parse_args(argv)
    resolved = paths.resolve_paths(a.sessions_root)
    try:
        state = load_state(resolved)
    except ValueError:
        print('Share state unavailable; sharing is disabled.', file=sys.stderr)
        return 4
    mine = _endpoint_entry(state, a.api)
    if a.delete or a.delete_report:
        try:
            return _delete(a, resolved, state, mine)
        except (OSError, ValueError, sqlite3.Error):
            print('Could not save deletion state.', file=sys.stderr)
            return 5
    if mine and not resolved['history_path'].exists():
        return _refuse_binding()
    if a.yes and not mine and not a.handle:
        print('The first share requires --handle HANDLE.', file=sys.stderr)
        return 2
    current, started = time.time(), time.perf_counter()
    context = reportcli.RECEIPT_ENDPOINT.set(a.api)
    holder = {}
    store_context = reportcli.RECEIPT_STORE.set(holder)
    try:
        captured = reportcli.collect(a, now=current)
    except BaseException:
        if holder.get('store') is not None:
            holder['store'].close()
        raise
    finally:
        reportcli.RECEIPT_STORE.reset(store_context)
        reportcli.RECEIPT_ENDPOINT.reset(context)
    if a._history_failed:
        if holder.get('store') is not None:
            holder['store'].close()
        return 4
    store = holder.get('store') or history.History(resolved['history_path'])
    posted = saved = False  # what the server already holds when a local step fails below
    try:
        intact, _q = store.check_integrity()  # prepare() itself does not verify integrity
        coverage = copy.deepcopy(captured['coverage'])
        if not _check_binding(mine, store, coverage):
            return _refuse_binding()
        if not intact or not coverage['history_committed']:
            print(history.UNAVAILABLE_REASON, file=sys.stderr)
            return 4
        receipts = store._load_submissions()
        coverage['_published'] = [r for r in receipts if r['endpoint'] == a.api
            and r['token_binding'] == mine.get('token_binding') and r['status'] in history.ACTIVE_STATUSES]
        coverage['_month_reasons'] = {m: sorted(v) for m, v in history._month_failures(captured, coverage['_published']).items() if v}
        captured['_share_prices'] = pricing.load(a.prices)
        payload, notes = build_payload(captured, coverage, handle=a.handle if a.handle != mine.get('handle') else None, now=current)
        print(describe(payload, notes, handle=a.handle or mine.get('handle'), api=a.api))
        issues = share_validate.validate_payload(payload, now=current)
        if issues:
            print('Local validation: ' + ', '.join(issues), file=sys.stderr)
            return 3
        coverage.update(safe_months=notes['safe_months'], withheld_months=notes['withheld_months'])
        page, upload = None, None
        if not a.no_report:
            _model, page = build_public_report(captured, coverage, style=a.style, now=current)
            upload = report_body(page)
            issues = share_validate.validate_report(upload)
            if issues:
                print('Local report validation: ' + ', '.join(issues), file=sys.stderr)
                return 3
            resolved['shared_report_path'].write_bytes(page)
            print('Public preview: {} bytes.'.format(len(page)))
        raw = share_validate.payload_bytes(payload)
        if a.out:
            target = Path(a.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        print('Timing collection: {:.1f}s; total: {:.1f}s.'.format(
            sum(a._timings.values()), time.perf_counter() - started))
        if not a.yes:
            print('Dry run: nothing was sent.')
            return 0
        if not payload['days']:
            print('No safe captured days to submit.', file=sys.stderr)
            return 3
        if not store.check_integrity()[0]:
            print(history.UNAVAILABLE_REASON, file=sys.stderr)
            return 4
        binding = mine.get('token_binding') or uuid.uuid4().hex
        submission = store.prepare(raw, notes['contributors'], endpoint=a.api, token_binding=binding)
        if not store.check_integrity()[0]:
            print(history.UNAVAILABLE_REASON, file=sys.stderr)
            return 4
        try:
            status, body = request('POST', a.api + '/share', body=raw, token=mine.get('token'))
        except (OSError, http.client.HTTPException):
            store.mark_unknown(submission)
            store.commit()
            print(UNKNOWN if not mine else 'Share outcome unknown; prepared evidence was retained and no automatic retry was made.', file=sys.stderr)
            return 6
        if status not in (200, 201) or not isinstance(body, dict) or not share_validate.valid_handle(body.get('handle')):
            store.mark_unknown(submission)
            store.commit()
            print('Count submission refused or unconfirmed (HTTP {}).'.format(status), file=sys.stderr)
            if not mine and status in (200, 201): print(UNKNOWN, file=sys.stderr)
            return 7
        token = body.get('token') or mine.get('token')
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            store.mark_unknown(submission)
            store.commit()
            print(UNKNOWN, file=sys.stderr)
            return 6
        posted = True
        entry = dict(token=token, handle=body['handle'], history_uuid=store.history_uuid, token_binding=binding)
        state['active_endpoint'] = a.api
        state['endpoints'][a.api] = entry
        save_state(resolved, state)
        saved = True
        store.confirm(submission, handle=entry['handle'])
        store.commit()
        if not store.history_committed:
            print('Counts succeeded; receipt confirmation failed.', file=sys.stderr)
            return 5
        origin = urllib.parse.urlsplit(a.api)
        site = urllib.parse.urlunsplit((origin.scheme, origin.netloc, '', '', ''))
        print('Shared: ' + site + '/u/' + entry['handle'])
        if upload is not None:
            if not store.check_integrity()[0]:
                print('Counts succeeded; report upload was disabled by history integrity.', file=sys.stderr)
                return 8
            try:
                status, _body = request('PUT', a.api + '/report', body=upload, token=token)
            except (OSError, http.client.HTTPException):
                print('Counts succeeded; report upload failed.', file=sys.stderr)
                return 8
            if status != 200:
                print('Counts succeeded; report upload failed (HTTP {}).'.format(status), file=sys.stderr)
                return 8
            print('Report: ' + site + '/r/' + entry['handle'])
        return 0
    except (OSError, ValueError, sqlite3.Error):
        if not posted:
            print('Share state or prepared receipt could not be committed; no further request was made.', file=sys.stderr)
        elif not saved:
            print('Counts succeeded; the share token could not be saved, so this share cannot be '
                  'updated or deleted from here.', file=sys.stderr)
        else:
            print('Counts succeeded; receipt confirmation failed.', file=sys.stderr)
        return 5
    finally:
        store.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Interrupted.', file=sys.stderr)
        raise SystemExit(130)
