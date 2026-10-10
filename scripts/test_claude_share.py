"""Safe Claude sharing: invented facts, disposable state and loopback HTTP only."""
import base64
import builtins
import contextlib
import copy
import datetime
import gzip
import http.server
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'plugins/token-counter-claude/skills/token-share/scripts'))
sys.path.insert(0, str(REPO / 'scripts/fixtures'))
import share
import claude_cases as cases
from tokencounter import account, analyze, history, ledger, paths, share_validate, windows, worker

NOW = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc).timestamp()
ISO_NOW = '2026-10-01T00:00:00Z'
RESULTS, VALID, VALID_REPORTS = [], [], []
TOKEN = 'tu1.' + 'A' * 20 + '.' + 'b' * 43
SENTINELS = ('PRIVATE-PROMPT-DO-NOT-SHARE', 'PRIVATE-OUTPUT-DO-NOT-SHARE',
             'PRIVATE-THINKING-DO-NOT-SHARE', 'PRIVATE-TOOL-DO-NOT-SHARE',
             'PRIVATE-PROJECT-DO-NOT-SHARE', 'private@example.invalid',
             'PRIVATE-ORGANIZATION-DO-NOT-SHARE')
STRICTER = {
    'client_name', 'client_version', 'handle_invalid', 'session_id_16',
    'window_minutes_weekly', 'payload_body_budget', 'report_body_budget',
    'report_gzip', 'report_size', 'report_doctype',
}


def expect(name, got, expected=True):
    RESULTS.append((name, got == expected, ''))


def valid(payload):
    issues = share_validate.validate_payload(payload, now=NOW)
    expect('locally valid payload', issues, [])
    if not issues: VALID.append(copy.deepcopy(payload))
    return payload


def _view(case):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / 'projects'
        files = cases.write_corpus(root, case)
        results = {str(p): worker.extract(p) for p in files}
        with history.History(paths.resolve_paths(root)['history_path']) as store:
            built = store.capture(results, now=NOW)
            store.commit()
            built['coverage']['history_committed'] = True
            store.coverage(built, endpoint=share.DEFAULT_API)
            return built


def _build(case=None):
    built = _view(case or cases.baseline_B())
    p, notes = share.build_payload(built, built['coverage'], handle='tester', now=NOW)
    valid(p)
    return built, p, notes


def _receipt(p, notes):
    return dict(status='confirmed', payload=share_validate.payload_bytes(p), contributors=notes['contributors'])


def _missing_speed_case():
    source = cases.baseline_B()
    row = next(r for r in next(iter(source['files'].values())) if r.get('apiBlockIndex') == 1)
    a = copy.deepcopy(row)
    b = copy.deepcopy(row)
    for i, r in enumerate((a, b), 1):
        r.update(uuid='speed-%d' % i, requestId='speed-request-%d' % i, apiBlockIndex=0,
                 parentUuid=None, timestamp='2026-09-10T' + ('18:30:00Z' if i == 1 else '19:00:00Z'))
        r['message'].update(id='speed-message-%d' % i, content=[], stop_reason='end_turn')
    b['message']['usage'].pop('speed')
    readings = [r for r in next(iter(cases.quota_W()['files'].values()))
                if r['type'] == 'system' and r['timestamp'][11:16] in ('18:00', '19:00')]
    records = sorted([a, b] + readings, key=lambda r: r['timestamp'])
    for r in records: r['sessionId'] = 'speed-family'
    return {'files': {'project/speed-family.jsonl': records}}


def _top_view():
    files = {}
    for i in range(1, 22):
        family = 'family-%02d' % i
        usage = cases._usage(i * 5, 0, 0, 0, 0)
        records = [cases.assistant_record(family + '-m%d' % j, family + '-r%d' % j,
            '2026-09-10T00:00:%02dZ' % (0 if j == 0 else 22 - i), usage, uuid=family + '-u%d' % j)
            for j in range(2)]
        for r in records: r['sessionId'] = family
        files['project/' + family + '.jsonl'] = records
    return _view({'files': files})


def test_session_top_union():
    built = _top_view()
    p, _n = share.build_payload(built, built['coverage'], now=NOW)
    valid(p)
    expect('top fixture daily counts', [(d['responses'], d['input'], d['sessions']) for d in p['days']], [(42, 2310, 21)])
    expected = {share.session_hash('family-%02d' % i) for i in list(range(1, 11)) + list(range(12, 22))}
    expect('top ten union selects 1-10 and 12-21', {s['id'] for s in p['sessions']}, expected)
    expect('top union deterministic', share.select_sessions(list(reversed(built['rows'])), built['families'], ['2026-09']), p['sessions'])
    expect('session domain separation', share.session_hash('invented-family'),
           __import__('hashlib').sha256(b'tokenusage.dev:claude-code:v1\0invented-family').hexdigest()[:16])
    expect('unique end gaps and idle bound', share.active_seconds([1801, 0, 0, 1, 3602]), 1801)


def test_unknown_speed_shared_count():
    _b, p, n = _build(_missing_speed_case())
    expect('unknown speed preserves totals', [(d['responses'], d['input'], d['output']) for d in p['days']], [(2, 200, 16)])
    expect('known day tier is partial', [(d['tiers']['standard']['responses'], d['tiers']['standard']['input'], d['tiers']['standard']['output']) for d in p['days']], [(1, 100, 8)])
    expect('known window split is partial', [(s['responses'], s['input'], s['output']) for s in p['windows'][0]['split']], [(1, 100, 8)])
    text = share.describe(p, n, handle=None, api=share.DEFAULT_API)
    expect('dry run missing speed count', '1 shared responses had no recorded speed; their counts are outside tiers and split.' in text)


def test_cache_creation_subset():
    _b, p, _n = _build(cases.quota_W())
    s = p['windows'][0]['split'][0]
    expect('W observation counts', (p['windows'][0]['responses'], p['windows'][0]['input'], p['windows'][0]['cached'], p['windows'][0]['output']), (2, 700, 280, 70))
    expect('cache creation subsets', (s['cache_write_5m'], s['cache_write_1h']), (20, 40))
    expect('observation first and peak', (p['windows'][0]['first_pct'], p['windows'][0]['peak_pct']), (20, 25))
    bad = copy.deepcopy(p)
    bad['windows'][0]['split'][0]['cache_write_5m'] = 1000
    expect('cache creation rejected', 'split_cache_write' in share_validate.validate_payload(bad, now=NOW))


def test_known_tier_subset():
    _b, p, _n = _build(_missing_speed_case())
    bad = copy.deepcopy(p)
    bad['days'][0]['tiers']['standard']['input'] = 201
    expect('tier counts within parent', 'tier_input_total' in share_validate.validate_payload(bad, now=NOW))


def test_month_guard_steps():
    built, p, n = _build()
    receipt = _receipt(p, n)
    receipt['status'] = 'unknown'
    responses = n['contributors']['months']['2026-09']['responses']
    key = next(iter(responses))
    for field in ('history_available', 'history_committed', 'token_bound'):
        changed = copy.deepcopy(built)
        changed['coverage'][field] = False
        expect('month guard ' + field, history.month_coverage(changed, [receipt])['safe_months'], [])
    for field in ('history_integrity_failed', 'history_schema_unsupported', 'history_commit_failed'):
        changed = copy.deepcopy(built)
        changed['counters'][field] = 1
        expect('month guard ' + field, history.month_coverage(changed, [receipt])['safe_months'], [])
    for field in ('input', 'cached', 'output', 'reasoning'):
        previous = copy.deepcopy(receipt)
        previous['contributors']['months']['2026-09']['responses'][key]['counts'][field] += 1
        expect('month guard decrease ' + field, history.month_coverage(built, [previous])['safe_months'], [])
    missing = copy.deepcopy(built)
    missing['rows'] = [r for r in built['rows'] if r['response_key'] != key]
    expect('month guard missing contributor', history.month_coverage(missing, [receipt])['safe_months'], [])
    calendar = copy.deepcopy(receipt)
    calendar['contributors']['months']['2026-09']['responses'][key]['calendar']['day_start'] += 1
    expect('month guard frozen calendar', history.month_coverage(built, [calendar])['safe_months'], [])
    session = copy.deepcopy(receipt)
    session['contributors']['months']['2026-09']['sessions'][0]['responses'].append('invented-missing')
    expect('month guard selected session evidence', history.month_coverage(built, [session])['safe_months'], [])
    facts = copy.deepcopy(receipt)
    facts['contributors']['months']['2026-09']['sessions'][0]['facts'].append(
        {'kind': 'links', 'key': 'invented-missing', 'source_id': 'invented-source', 'digest': 'invented-digest'})
    expect('month guard selected session facts', history.month_coverage(built, [facts])['safe_months'], [])
    expect('unknown preparation guards month', history.month_coverage(missing, [receipt])['safe_months'], [])
    reduced = copy.deepcopy(receipt)
    reduced['contributors']['months']['2026-09']['responses'][key]['counts']['input'] += 1
    expect('unknown preparation guards decreased counts', history.month_coverage(built, [reduced])['safe_months'], [])
    expect('retired receipts do not guard', history.month_coverage(built, [dict(reduced, status='retired')])['safe_months'], ['2026-09'])


def test_month_withholding():
    built, p, n = _build()
    changed = copy.deepcopy(built)
    extra = copy.deepcopy(built['rows'][0])
    extra.update(response_key='invented-october', family_id=None, local_day='2026-10-01', ts=ISO_NOW)
    changed['rows'].append(extra)
    receipt = _receipt(p, n)
    receipt['contributors']['months']['2026-09']['responses']['missing'] = copy.deepcopy(history._row_snapshot(built['rows'][0]))
    coverage = history.month_coverage(changed, [receipt])
    new, notes = share.build_payload(changed, coverage, now=NOW)
    valid(new)
    expect('unsafe month withheld whole', {d['date'][:7] for d in new['days']}, {'2026-10'})
    expect('session touching unsafe month omitted', new['sessions'], [])
    expect('exact withheld month text', history.MONTH_REASON.format(month='2026-09') in share.describe(new, notes, handle=None, api=share.DEFAULT_API))


def test_window_omission_preserves_store_semantics():
    built, p, n = _build(cases.quota_W())
    coverage = dict(built['coverage'], _published=[_receipt(p, n)])
    repeated, _n = share.build_payload(built, coverage, now=NOW)
    valid(repeated)
    expect('published window reproduced', repeated.get('windows'), p['windows'])
    # Reconstruct the published interval even when a later decline makes the
    # new full-cluster candidate unshareable; original endpoints stay reproducible.
    extended = copy.deepcopy(built)
    late = copy.deepcopy(extended['limits'][-1])
    late.update(reading_key='invented-late-decline', percent=24, ts='2026-09-10T21:00:00Z')
    extended['limits'].append(late)
    kept, _n = share.build_payload(extended, coverage, now=NOW)
    valid(kept)
    expect('published interval retained across new decline', kept.get('windows'), p['windows'])
    damaged = copy.deepcopy(coverage)
    damaged['windows_replace_safe'] = False
    damaged['window_reasons'] = [history.WINDOW_REASON]
    omitted, _n = share.build_payload(built, damaged, now=NOW)
    valid(omitted)
    expect('unsafe windows omit whole key', 'windows' not in omitted)
    empty, _p, _n = _build()
    expect('empty windows omitted', 'windows' not in _p)
    expect('window union budget is whole list', share._window_budget([dict(p['windows'][0], split=p['windows'][0]['split'] * 51)]))
    expect('window total split budget', share._window_budget([dict(p['windows'][0], split=p['windows'][0]['split'] * 50)] * 21))
    expect('window count budget', share._window_budget([dict(p['windows'][0], split=[])] * 1001))


def test_retained_window_touching_withheld_month():
    built, p, n = _build(cases.quota_W())
    changed = copy.deepcopy(built)
    extra = copy.deepcopy(built['rows'][0])
    extra.update(response_key='invented-october', family_id=None, local_day='2026-10-01', ts=ISO_NOW)
    extra['usage']['input_tokens'] = 10000
    changed['rows'].append(extra)
    coverage = dict(built['coverage'], safe_months=['2026-10'], _published=[_receipt(p, n)],
                    withheld_months={'2026-09': [history.MONTH_REASON.format(month='2026-09')]})
    new, notes = share.build_payload(changed, coverage, now=NOW)
    valid(new)
    expect('retained window cannot use withheld month', 'windows' not in new)
    expect('retained window withheld month diagnostic', notes['counters']['windows_withheld_retention'], 1)


def test_full_window_union_budget():
    built, _p, _n = _build(cases.quota_W())
    observed, q = windows.build_windows(built['limits'], built['rows'], now=NOW)
    windows_many = [dict(observed[0], nominal_start=observed[0]['nominal_start'] + i,
                         window_key='invented-budget-%d' % i) for i in range(1001)]
    with mock.patch.object(windows, 'build_windows', return_value=(windows_many, q)):
        new, notes = share.build_payload(built, built['coverage'], now=NOW)
    valid(new)
    expect('oversized full union omitted whole', 'windows' not in new)
    expect('oversized full union diagnostic', notes['counters'].get('windows_withheld_schema_budget', 0), 1)


def test_window_union_and_plan_reassessment():
    built, p, n = _build(cases.quota_W())
    old = _receipt(p, n)
    changed = copy.deepcopy(built)
    for reading in list(built['limits']):
        r = copy.deepcopy(reading)
        r['reading_key'] += '-next'
        r['ts'] = share._iso(worker.epoch(r['ts']) + 604800)
        r['resets_at'] += 604800
        changed['limits'].append(r)
    for row in list(built['rows']):
        r = copy.deepcopy(row)
        r['response_key'] += '-next'
        r['ts'] = share._iso(worker.epoch(r['ts']) + 604800)
        r['local_day'] = '2026-09-18'
        changed['rows'].append(r)
    changed['account_snapshots'] = [cases.account_snapshot('2026-09-10T21:00:00Z')]
    coverage = dict(built['coverage'], _published=[old])
    new, _n = share.build_payload(changed, coverage, now=NOW)
    valid(new)
    expect('window union retains W1 and adds W2', len(new.get('windows', [])), 2)
    expect('retained window reevaluates plan', (new.get('windows') or [{}])[0].get('plan'), 'claude:max-5x')
    changed['account_snapshots'].append(cases.account_snapshot('2026-09-20T21:00:00Z', rate_limit_tier='default_claude_max_20x'))
    null, _n = share.build_payload(changed, coverage, now=NOW)
    valid(null)
    expect('later conflict clears retained plan', (null.get('windows') or [{}])[0].get('plan'), None)
    expect('advanced counts cannot decrease', share._advances(dict(p['windows'][0], input=699), p['windows'][0]), False)


def test_window_advancement_decisions():
    built, p, n = _build(cases.quota_W())
    old = p['windows'][0]
    for field, value in [('input', 699), ('responses', 1), ('cached', 279), ('output', 69),
                         ('first_pct', 19), ('peak_pct', 24), ('start', ISO_NOW)]:
        expect('window cannot decrease ' + field, share._advances(dict(old, **{field: value}), old), False)
    for field in share_validate.WINDOW_COUNTS + ('cache_write_5m', 'cache_write_1h'):
        new = copy.deepcopy(old)
        new['split'][0][field] -= 1
        expect('window split cannot decrease ' + field, share._advances(new, old), False)
    damaged = _receipt(p, n)
    damaged['contributors']['windows'][0]['readings'] = []
    observed, _q = windows.build_windows(built['limits'], built['rows'], now=NOW)
    merged, _evidence = share._window_union(p['windows'], observed, dict(built['coverage'], _published=[damaged]), built)
    expect('unreproduced retained union omitted whole', merged, None)


def test_session_selection_and_budgets():
    built = _top_view()
    family = max(built['families'], key=lambda f: sum(r['usage']['input_tokens'] for r in built['rows'] if r['family_id'] == f))
    changed = copy.deepcopy(built)
    members = [r for r in changed['rows'] if r['family_id'] == family]
    members[-1]['ts'] = '2027-12-20T00:00:00Z'
    selected = share.select_sessions(changed['rows'], changed['families'], ['2026-09'])
    expect('out of bounds sessions omitted', share.session_hash(family) not in {s['id'] for s in selected})
    many, families = [], {}
    # 160 first-response months; two distinct end gaps per family. Newer months
    # win the independent global cap; safe_months need not be contiguous.
    for month in range(160):
        d = datetime.datetime(2010 + month // 12, month % 12 + 1, 10, tzinfo=datetime.timezone.utc)
        day, start = d.date().isoformat(), d.timestamp()
        for i in range(20):
            name = 'budget-%d-%d' % (month, i)
            model = 'claude-opus-5-5'
            families[name] = {'local_day': day}
            for j in range(2):
                many.append(dict(family_id=name, local_day=day, ts=share._iso(start + j * (20 - i)), model=model,
                                 usage={'input_tokens': i, 'cached_input_tokens': 0, 'output_tokens': 1, 'reasoning_output_tokens': 0}))
    safe = sorted({r['local_day'][:7] for r in many})
    selected = share.select_sessions(many, families, safe)
    expect('global session budget', len(selected), 3000)
    expect('newest first months win global cap', min(s['day'][:7] for s in selected) > min(safe))


def test_withheld_session_diagnostic_selection():
    built = _top_view()
    for input_count, expected in ((110, 0), (10, 1)):
        changed = copy.deepcopy(built)
        family = next(f for f in built['families'] if sum(r['usage']['input_tokens'] for r in built['rows'] if r['family_id'] == f) == input_count)
        next(r for r in changed['rows'] if r['family_id'] == family)['local_day'] = '2026-10-01'
        coverage = dict(changed['coverage'], safe_months=['2026-09'],
                        withheld_months={'2026-10': [history.MONTH_REASON.format(month='2026-10')]})
        p, n = share.build_payload(changed, coverage, now=NOW)
        valid(p)
        expect('withheld sessions count selected family ' + str(input_count), n['counters'].get('sessions_withheld_month', 0), expected)


def test_payload_size_whole_month():
    built, _p, _n = _build()
    with mock.patch.object(share_validate, 'MAX_BODY_BYTES', 200):
        p, n = share.build_payload(built, built['coverage'], now=NOW)
    expect('oversized month withheld whole', p['days'], [])
    expect('oversized month diagnostic', n['counters'].get('months_withheld_schema_budget', 0), 1)
    many = copy.deepcopy(built)
    many['rows'][0]['usage']['input_tokens'] = 20000000001
    p, _n = share.build_payload(many, many['coverage'], now=NOW)
    expect('input bound withholds whole month', p['days'], [])
    extra = copy.deepcopy(built['rows'][0])
    extra.update(response_key='invented-october', family_id=None, local_day='2026-10-01', ts=ISO_NOW)
    many = copy.deepcopy(built)
    many['rows'].append(extra)
    coverage = dict(many['coverage'], safe_months=['2026-09', '2026-10'])
    with mock.patch.object(share_validate, 'MAX_DAYS', 1):
        p, n = share.build_payload(many, coverage, now=NOW)
    valid(p)
    expect('day budget withholds oldest whole month', [d['date'] for d in p['days']], ['2026-10-01'])


def test_session_summary_bounds_and_model():
    built = _top_view()
    family = next(iter(built['families']))
    rows = [copy.deepcopy(r) for r in built['rows'] if r['family_id'] == family]
    rows[0]['model'], rows[1]['model'] = 'claude-zeta', 'claude-alpha'
    expect('session dominant model lexical tie', share._session_summary(family, rows, built['families'][family], ['2026-09'])['model'], 'claude-alpha')
    rows[1]['ts'] = '2027-12-20T00:00:00Z'
    expect('pure session span bound', share._session_summary(family, rows, built['families'][family], ['2026-09']), None)
    rows = [dict(rows[0], ts=share._iso(NOW - 2700000 + i * 1800)) for i in range(1501)]
    expect('pure session active bound', share._session_summary(family, rows, built['families'][family], ['2026-09']), None)


def test_latency_group_budgets():
    built, _p, _n = _build()
    lat, q = analyze._timing(built, built['rows'], since=NOW - 30 * 86400)
    row = dict(model='claude-opus-5-5', effort='high', n=1, median_s=1, p90_s=1, fit={}, above_share=None)
    lat['groups'] = [dict(row, model='invented-%d' % i) for i in range(70)]
    lat['tier_groups'] = [dict(row, model='invented-%d' % i, tier='standard') for i in range(65)]
    with mock.patch.object(analyze, '_timing', return_value=(lat, q)):
        summary = share.latency_summary(built, ['2026-09'], now=NOW)
    expect('independent latency group budgets', (len(summary['groups']), len(summary['tier_groups'])), (50, 50))


def test_shared_timing_diagnostics():
    _built, _p, notes = _build(_missing_speed_case())
    expect('shared timing unknown speed diagnostic', notes['counters'].get('latency_unknown_speed', 0), 1)
    expect('shared timing missing start diagnostic', notes['counters'].get('latency_no_start', 0), 2)


def _forbidden(value):
    banned = {'api_value', 'api_usd', 'utc_hours', 'utc_weekdays', 'email', 'account', 'source_id',
              'response_key', 'family_id', 'session_id', 'stream_id', 'path', 'cwd', 'title', 'content', 'tools'}
    if isinstance(value, dict):
        return bool(set(value) & banned) or any(_forbidden(v) for v in value.values())
    if isinstance(value, list):
        return any(_forbidden(v) for v in value)
    return False


def test_no_forbidden_fields_recursive():
    built, p, _n = _build(cases.quota_W_timed())
    expect('wire recursive allow list', _forbidden(p), False)
    expect('wire exact root keys', set(p) <= {'schema', 'client', 'handle', 'generated_at', 'days', 'sessions', 'windows', 'latency'})
    expect('wire exact window keys', set(p['windows'][0]) <= set(share.WINDOW_FIELDS))
    built['account_snapshots'] = [cases.account_snapshot('2026-09-10T21:00:00Z')]
    lat = share.latency_summary(built, ['2026-09'], now=NOW)
    expect('latency Q5 plan rule', lat['plan'], 'claude:max-5x')
    built['account_snapshots'].append(cases.account_snapshot('2026-09-20T00:00:00Z', rate_limit_tier='default_claude_max_20x'))
    expect('latency later plan conflict', share.latency_summary(built, ['2026-09'], now=NOW)['plan'], None)


def _private_case():
    case = cases.baseline_B()
    for r in next(iter(case['files'].values())):
        if isinstance(r.get('message'), dict):
            if r['type'] == 'assistant':
                r['message']['content'] = [{'type': 'text', 'text': SENTINELS[1]},
                    {'type': 'thinking', 'thinking': SENTINELS[2]},
                    {'type': 'tool_use', 'id': 'invented-call', 'name': SENTINELS[3], 'input': {'private': SENTINELS[3]}}]
            else: r['message']['content'] = SENTINELS[0]
        r['cwd'], r['title'] = SENTINELS[4], SENTINELS[4]
    return case


def test_public_html_no_sensitive_markers():
    built, p, _n = _build(_private_case())
    built['account'] = {'available': True, 'email': SENTINELS[5], 'organization_name': SENTINELS[6]}
    for style in ('clinical', 'matisse', 'nocturne'):
        model, page = share.build_public_report(built, built['coverage'], style=style, now=NOW)
        combined = page.decode() + json.dumps(model) + json.dumps(p)
        expect('public sensitive markers ' + style, not any(s in combined for s in SENTINELS))
        expect('public identifiers ' + style, not any(f in combined for f in built['families']))
        expect('public model identity ' + style, model['account']['available'], False)
        body = share.report_body(page)
        expect('public report valid ' + style, share_validate.validate_report(body), [])
        VALID_REPORTS.append(body)


def test_public_html_no_external_references():
    built, _p, _n = _build()
    _m, page = share.build_public_report(built, built['coverage'], now=NOW)
    text = page.decode()
    anchor = '<a href="https://tokenusage.dev">'
    expect('brand anchor exactly once', text.count(anchor), 1)
    text = text.replace(anchor, '')
    expect('only deliberate brand external reference', 'http://' not in text and 'https://' not in text)
    import re
    expect('no runtime networking', re.search(r'\b(?:fetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|import\s*\(|action\s*=)', text) is None)


def test_local_account_identity_public_stripped():
    built, _p, _n = _build()
    built['account'] = {'available': True, 'email': SENTINELS[5]}
    local = analyze.analyze(built, now=NOW)
    expect('local identity present', local['account']['email'], SENTINELS[5])
    public = analyze.public_model(local)
    expect('public identity stripped', SENTINELS[5] not in json.dumps(public))


def test_history_account_allow_list():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / 'projects'
        files = cases.write_corpus(root, cases.baseline_B())
        resolved = paths.resolve_paths(root)
        resolved['account_path'].write_text(json.dumps({'oauthAccount': dict(organizationType='claude_max',
            organizationRateLimitTier='default_claude_max_5x', subscriptionCreatedAt='2026-01-01T00:00:00Z',
            emailAddress=SENTINELS[5], organizationName=SENTINELS[6], accessToken='PRIVATE-ACCESS')}))
        got = account.read_account(resolved, now=NOW)
        expect('account exact snapshot shape', set(got['snapshot']), set(history.SNAPSHOT_FIELDS))
        with history.History(resolved['history_path']) as store:
            store.capture({str(p): worker.extract(p) for p in files}, account=got['snapshot'], now=NOW)
            store.commit()
            blobs = '\n'.join(json.dumps(store._decode(blob, digest)) for blob, digest in store.db.execute('SELECT payload,payload_sha256 FROM facts'))
            expect('history excludes account identity', not any(s in blobs for s in (SENTINELS[5], SENTINELS[6], 'PRIVATE-ACCESS')))


class Stub(http.server.BaseHTTPRequestHandler):
    calls = []
    mode = 'normal'
    before_post = None

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _call(self, method):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body = json.loads(raw) if raw else None
        self.calls.append((method, self.path, self.headers.get('Authorization'), raw, body))
        return body

    def do_POST(self):
        body = self._call('POST')
        if Stub.before_post: Stub.before_post(self.calls[-1])
        if self.mode == 'unknown':
            self.close_connection = True
            return
        if self.mode == 'redirect':
            self.send_response(307)
            self.send_header('Location', 'https://example.invalid/api/share')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        VALID.append(body)
        authenticated = self.headers.get('Authorization') is not None
        answer = {'handle': body.get('handle', 'tester'), 'created': not authenticated}
        if not authenticated: answer['token'] = TOKEN
        self._send(200 if authenticated else 201, answer)

    def do_PUT(self):
        body = self._call('PUT')
        VALID_REPORTS.append(body)
        self._send(500 if self.mode == 'report-fails' else 200, {'message': 'PRIVATE-SERVER-MESSAGE', 'url': '/r/tester'})

    def do_DELETE(self):
        self._call('DELETE')
        self._send(200, {'deleted': True})


@contextlib.contextmanager
def server(mode='normal'):
    srv = http.server.HTTPServer(('127.0.0.1', 0), Stub)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    Stub.calls, Stub.mode, Stub.before_post = [], mode, None
    thread.start()
    try:
        yield 'http://127.0.0.1:%d/api' % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()
        Stub.before_post = None


def _run(root, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(share.time, 'time', return_value=NOW):
        code = share.main(['--sessions-root', str(root), '--quiet', *args])
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def corpus(case=None):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / 'projects'
        cases.write_corpus(root, case or cases.baseline_B())
        yield root, paths.resolve_paths(root)


def test_dry_run_zero_requests():
    with corpus() as (root, resolved):
        with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('dry network attempt')) as connect:
            rc, out, err = _run(root)
        expect('dry run succeeds', rc, 0)
        expect('dry run socket guard', connect.call_count, 0)
        expect('dry run no token file', resolved['share_state_path'].exists(), False)
        expect('dry run captures history', resolved['history_path'].exists())
        expect('dry run writes public preview', resolved['shared_report_path'].exists())
        expect('dry run disclosure', share.CALENDAR in out and share.PLAN_ASSUMPTION in out)
        expect('dry run no private output', not any(s in out + err for s in SENTINELS))
        out_path = root.parent / 'payload.json'
        with mock.patch.object(share, 'request', side_effect=AssertionError('out request')):
            rc, out, _err = _run(root, '--out', str(out_path))
        expect('out dry run', rc, 0)
        valid(json.loads(out_path.read_bytes()))
        expect('out exact compact bytes', out_path.read_bytes(), share_validate.payload_bytes(json.loads(out_path.read_bytes())))


def test_archived_whole_month_sharing():
    with corpus() as (root, resolved), server() as api:
        rc, _o, _e = _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
        expect('archive fixture first share', rc, 0)
        previous, before = Stub.calls[0][4], len(Stub.calls)
        for file in root.rglob('*.jsonl'):
            file.unlink()
        target = root.parent / 'archived-payload.json'
        with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('archive dry network')):
            rc, out, _e = _run(root, '--api', api, '--rebuild', '--no-report', '--out', str(target))
        payload = valid(json.loads(target.read_bytes()))
        expect('archive dry run zero requests', rc == 0 and len(Stub.calls) == before)
        expect('archive entire captured month counts', payload['days'], previous['days'])
        expect('archive entire first-capture families', payload['sessions'], previous['sessions'])
        expected = 'captured history: {} responses from transcripts no longer on disk'.format(sum(d['responses'] for d in previous['days']))
        expect('archive exact retention disclosure', share.ARCHIVED in out and expected in out)


def test_yes_exact_prepared_payload():
    with corpus() as (root, resolved), server() as api:
        _rc, _out, _err = _run(root, '--api', api)
        first_page = resolved['shared_report_path'].read_bytes()
        observed = []
        def prepared(call):
            with history.History(resolved['history_path']) as store:
                receipts = store._load_submissions()
                observed.append(receipts[-1]['payload'] == call[3] and receipts[-1]['status'] == 'prepared')
                observed.append(set(receipts[-1]['contributors']['months']) == {'2026-09'})
                observed.append(receipts[-1]['contributors'] == prepared_args[0])
        Stub.before_post = prepared
        original, prepared_args = history.History.prepare, []
        def record_prepare(store, raw, contributors, **kwargs):
            prepared_args.append(copy.deepcopy(contributors))
            return original(store, raw, contributors, **kwargs)
        with mock.patch.object(history.History, 'prepare', record_prepare):
            rc, out, err = _run(root, '--api', api, '--handle', 'Tester', '--yes')
        expect('yes succeeds', rc, 0)
        expect('yes durable exact preparation', observed, [True, True, True])
        expect('yes one POST then PUT', [c[0] for c in Stub.calls], ['POST', 'PUT'])
        expect('first POST no token', Stub.calls[0][2], None)
        expect('preview bytes stable across dry and yes', resolved['shared_report_path'].read_bytes(), first_page)
        state = share.load_state(resolved)
        expect('state exact root shape', set(state), {'schema', 'active_endpoint', 'endpoints'})
        entry = state['endpoints'][api]
        expect('state exact entry shape', set(entry), set(share.STATE_FIELDS))
        expect('report reader shares endpoint and binding', share.reportcli._receipt_binding(resolved), (api, entry['token_binding']))
        expect('token never printed', TOKEN not in out + err)
        with history.History(resolved['history_path']) as store:
            expect('receipt confirmed', store._load_submissions()[0]['status'], 'confirmed')
            expect('receipt history UUID bound', entry['history_uuid'], store.history_uuid)
        if os.name != 'nt': expect('private share state mode', stat.S_IMODE(resolved['share_state_path'].stat().st_mode), 0o600)
        rc, _out, _err = _run(root, '--api', api, '--yes', '--no-report')
        expect('existing token update succeeds', rc, 0)
        expect('existing token authenticated', Stub.calls[-1][2], 'Bearer ' + TOKEN)
        expect('saved handle omitted on update', 'handle' not in Stub.calls[-1][4])


def test_report_upload_exact_preview_bytes():
    with corpus(_private_case()) as (root, resolved), server() as api:
        rc, _out, _err = _run(root, '--api', api, '--handle', 'tester', '--yes', '--style', 'matisse')
        expect('report upload succeeds', rc, 0)
        body = Stub.calls[-1][4]
        sent = gzip.decompress(base64.b64decode(body['html_gz']))
        expect('report uploaded exact preview bytes', sent, resolved['shared_report_path'].read_bytes())
        expect('chosen public style', b'data-style="matisse"' in sent)
        expect('report bearer header', Stub.calls[-1][2], 'Bearer ' + TOKEN)


def test_separate_codex_token():
    with corpus() as (root, resolved):
        codex = root.parent / 'codex'
        (codex / 'token-counter').mkdir(parents=True)
        codex_token = codex / 'token-counter/share.json'
        marker = b'{"endpoints":{"https://tokenusage.dev/api":{"token":"PRIVATE-CODEX-TOKEN"}}}'
        codex_token.write_bytes(marker)
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex)}), server() as api:
            rc, out, err = _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
            expect('Claude independent first token', rc == 0 and Stub.calls[0][2] is None)
        expect('Codex state bytes untouched', codex_token.read_bytes(), marker)
        expect('Claude token filename', resolved['share_state_path'].name, 'claude-share.json')
        expect('Codex token absent from output', 'PRIVATE-CODEX-TOKEN' not in out + err)


def test_endpoint_token_separation():
    with corpus() as (root, resolved):
        with server() as first:
            rc, _o, _e = _run(root, '--api', first, '--handle', 'tester', '--yes', '--no-report')
            expect('first endpoint share', rc, 0)
        with server() as second:
            rc, _o, _e = _run(root, '--api', second, '--handle', 'tester', '--yes', '--no-report')
            expect('second endpoint share', rc, 0)
            expect('endpoint token never reused', Stub.calls[0][2], None)
        state = share.load_state(resolved)
        expect('both endpoint entries retained', set(state['endpoints']), {first, second})
        context = share.reportcli.RECEIPT_ENDPOINT.set(first)
        try:
            expect('explicit endpoint collection binding', share.reportcli._receipt_binding(resolved), (first, state['endpoints'][first]['token_binding']))
        finally: share.reportcli.RECEIPT_ENDPOINT.reset(context)
    expect('normalized endpoint', share.normalize_endpoint('HTTPS://TOKENUSAGE.DEV:443/api///'), share.DEFAULT_API)
    expect('IPv6 loopback allowed', share.normalize_endpoint('http://[::1]:8080/api/'), 'http://[::1]:8080/api')


def test_endpoint_binding_decisions():
    first, second = share.DEFAULT_API, 'https://other.invalid/api'
    entry = dict(token=TOKEN, handle='tester', history_uuid='invented-uuid', token_binding='invented-binding')
    state = dict(schema=1, active_endpoint=first, endpoints={first: entry})
    expect('pure endpoint token separation', share._endpoint_entry(state, second), {})
    expect('pure endpoint selected binding', share._endpoint_entry(state, first), entry)
    coverage = dict(token_bound=True)
    expect('matching UUID and receipt binding', share._check_binding(entry, SimpleNamespace(history_uuid='invented-uuid'), coverage))
    expect('mismatched history UUID refuses', share._check_binding(entry, SimpleNamespace(history_uuid='different'), coverage), False)
    expect('unbound receipt refuses', share._check_binding(entry, SimpleNamespace(history_uuid='invented-uuid'), dict(token_bound=False)), False)


def test_normalized_endpoint_decisions():
    expect('pure endpoint normalization', share.normalize_endpoint('HTTPS://TOKENUSAGE.DEV:443/api///'), share.DEFAULT_API)
    expect('pure loopback normalization', share.normalize_endpoint('http://[::1]:8080/api/'), 'http://[::1]:8080/api')
    try:
        share.normalize_endpoint('http://example.invalid/api')
        rejected = False
    except ValueError:
        rejected = True
    expect('HTTP outside loopback refuses', rejected)


def test_api_env_matches_codex():
    source = (REPO / 'plugins/token-counter/skills/token-share/scripts/share.py').read_text(encoding='utf-8')
    expect('Codex uses TOKENUSAGE_API', "os.environ.get('TOKENUSAGE_API') or DEFAULT_API" in source)
    with mock.patch.dict(os.environ, {'TOKENUSAGE_API': 'http://127.0.0.1:12345/api/'}):
        expect('Claude same API env', share.parse_args([]).api, 'http://127.0.0.1:12345/api')


def test_first_timeout_no_retry():
    with corpus() as (root, resolved), server('unknown') as api:
        rc, out, err = _run(root, '--api', api, '--handle', 'tester', '--yes')
        expect('first unknown outcome', rc, 6)
        expect('first unknown no retry', [c[0] for c in Stub.calls], ['POST'])
        expect('first unknown exact warning', share.UNKNOWN in err)
        expect('first unknown no token', resolved['share_state_path'].exists(), False)
        with history.History(resolved['history_path']) as store:
            expect('first outcome marked unknown', [r['status'] for r in store._load_submissions()], ['unknown'])


def test_counts_success_report_failure():
    with corpus() as (root, resolved), server('report-fails') as api:
        rc, out, err = _run(root, '--api', api, '--handle', 'tester', '--yes')
        expect('counts success report failure exit', rc, 8)
        expect('counts success report failure message', 'Counts succeeded; report upload failed' in err)
        expect('server message is never echoed', 'PRIVATE-SERVER-MESSAGE' not in out + err)
        expect('counts success token kept', share.load_state(resolved)['endpoints'][api]['token'], TOKEN)
        with history.History(resolved['history_path']) as store:
            expect('counts success receipt confirmed', store._load_submissions()[0]['status'], 'confirmed')


def test_counts_success_local_state_failures():
    # The server holds the share; the message must say so, and nothing else is sent.
    with corpus() as (root, resolved), server() as api:
        with mock.patch.object(share, 'save_state', side_effect=OSError('PRIVATE-PATH')):
            rc, _out, err = _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
        expect('token save failure exit', rc, 5)
        expect('token save failure message', 'Counts succeeded; the share token could not be saved' in err)
        expect('token save failure one POST only', [c[0] for c in Stub.calls], ['POST'])
        expect('token save failure no state file', resolved['share_state_path'].exists(), False)
        expect('token save failure path not echoed', 'PRIVATE-PATH' not in err)
        with history.History(resolved['history_path']) as store:
            expect('token save failure keeps prepared receipt', [r['status'] for r in store._load_submissions()], ['prepared'])
    with corpus() as (root, resolved), server() as api:
        _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
        with mock.patch.object(history.History, 'retire_token', side_effect=sqlite3.Error('PRIVATE-DB')):
            rc, _out, err = _run(root, '--api', api, '--delete', '--yes')
        expect('delete retirement failure exit', rc, 5)
        expect('delete retirement failure message', 'Public deletion succeeded; local receipt retirement failed.' in err)
        expect('delete retirement failure drops dead token', share.load_state(resolved)['endpoints'], {})
        expect('delete retirement failure db text not echoed', 'PRIVATE-DB' not in err)


def test_delete_dry_run():
    with corpus() as (root, _resolved), server() as api:
        _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
        before = len(Stub.calls)
        with mock.patch.object(share.reportcli, 'collect', side_effect=AssertionError('deletion collected account')):
            for flag in ('--delete', '--delete-report'):
                rc, out, _err = _run(root, '--api', api, flag)
                expect('deletion dry run ' + flag, rc == 0 and len(Stub.calls) == before and 'Dry run' in out)


def test_delete_yes():
    with corpus() as (root, resolved), server() as api:
        _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
        with mock.patch.object(account, 'read_account', side_effect=AssertionError('deletion account read')):
            rc, _o, _e = _run(root, '--api', api, '--delete-report', '--yes')
            expect('delete report only', rc == 0 and Stub.calls[-1][:3] == ('DELETE', '/api/report', 'Bearer ' + TOKEN))
            expect('delete report keeps token', share.load_state(resolved)['endpoints'][api]['token'], TOKEN)
            with history.History(resolved['history_path']) as store:
                expect('delete report keeps receipt', store._load_submissions()[0]['status'], 'confirmed')
            rc, _o, _e = _run(root, '--api', api, '--delete', '--yes')
            expect('delete yes route', rc == 0 and Stub.calls[-1][:3] == ('DELETE', '/api/share', 'Bearer ' + TOKEN))
        expect('delete removes endpoint token', share.load_state(resolved)['endpoints'], {})
        with history.History(resolved['history_path']) as store:
            expect('delete retires receipt', store._load_submissions()[0]['status'], 'retired')
            expect('delete preserves captured usage', len(store.load()[1]), 2)


def test_lost_history_recovery_message():
    with corpus() as (root, resolved), server() as api:
        _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
        resolved['history_path'].unlink()
        before = len(Stub.calls)
        rc, _out, err = _run(root, '--api', api, '--yes', '--no-report')
        expect('lost history no replacement', rc == 4 and len(Stub.calls) == before)
        expect('lost history exact recovery', err.strip(), history.TOKEN_REASON + '\n' + share.RECOVERY)
        rc, _out, _err = _run(root, '--api', api, '--delete', '--yes')
        expect('lost history can delete', rc == 0 and Stub.calls[-1][0] == 'DELETE')
    with corpus() as (root, resolved), server() as api:
        _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
        state = share.load_state(resolved)
        state['endpoints'][api]['history_uuid'] = 'different-history'
        share.save_state(resolved, state)
        before = len(Stub.calls)
        rc, _o, err = _run(root, '--api', api, '--yes', '--no-report')
        expect('mismatched UUID no replacement', rc == 4 and len(Stub.calls) == before and share.RECOVERY in err)


def test_commit_and_integrity_failure_no_send():
    with corpus() as (root, _resolved), server() as api:
        def failed_commit(store):
            store.history_committed = False
            store.counters['history_commit_failed'] = 1
        with mock.patch.object(history.History, 'commit', failed_commit):
            rc, _o, _e = _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
        expect('commit failure no request', rc != 0 and Stub.calls == [])
    with corpus() as (root, resolved), server() as api:
        _run(root, '--api', api, '--no-report')
        import sqlite3
        with contextlib.closing(sqlite3.connect(str(resolved['history_path']))) as db:
            db.execute("UPDATE response_copies SET payload_sha256='damaged'")
            db.commit()
        rc, _o, _e = _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
        expect('integrity failure no request', rc != 0 and Stub.calls == [])
    with corpus() as (root, _resolved), server() as api:
        original = history.History.prepare
        def damaged_prepare(store, *args, **kwargs):
            ident = original(store, *args, **kwargs)
            store.db.execute("UPDATE submissions SET payload_sha256='damaged'")
            store.db.commit()
            return ident
        with mock.patch.object(history.History, 'prepare', damaged_prepare):
            rc, _o, _e = _run(root, '--api', api, '--handle', 'tester', '--yes', '--no-report')
        expect('prepared integrity failure no request', rc != 0 and Stub.calls == [])


def test_report_size_before_send():
    with corpus() as (root, _resolved), server() as api:
        with mock.patch.object(share_validate, 'MAX_REPORT_BYTES', 100):
            rc, _o, err = _run(root, '--api', api, '--handle', 'tester', '--yes')
        expect('oversized report refuses before counts', rc == 3 and Stub.calls == [] and 'report_size' in err)


def test_html_doctype_gzip():
    page = b'<!doctype html><html><title>invented</title></html>'
    body = share.report_body(page)
    expect('deterministic gzip', share.report_body(page), body)
    expect('gzip exact round trip', gzip.decompress(base64.b64decode(body['html_gz'])), page)
    expect('doctype accepted', share_validate.validate_report(body), [])
    VALID_REPORTS.append(body)
    expect('doctype rejected', share_validate.validate_report(share.report_body(b'<html>invented</html>')), ['report_doctype'])
    invalid = dict(body, html_gz=base64.b64encode(b'plain bytes').decode())
    expect('invalid gzip rejected', share_validate.validate_report(invalid), ['report_gzip'])
    expect('truncated gzip rejected', 'report_gzip' in share_validate.validate_report(dict(body,
        html_gz=base64.b64encode(base64.b64decode(body['html_gz'])[:-4]).decode())))
    expect('report envelope size', 'report_body_budget' in share_validate.validate_report(dict(body, ignored='a' * 2000000)))
    # Incompressible bytes exercise the compressed size limit too.
    expect('compressed report bound', bool(share_validate.validate_report(share.report_body(page + os.urandom(910000)))))


def test_cli_options_and_redirects():
    conflicts = [('--yes', '--out', 'p'), ('--delete', '--delete-report'), ('--delete', '--handle', 'tester'),
                 ('--delete-report', '--out', 'p'), ('--fast', '--procs', '2'), ('--procs', '0'),
                 ('--since', '2026-01-01'), ('--session', 'x'), ('--live-only',), ('--forget',), ('--token', 'x'),
                 ('--api', 'http://example.invalid/api'), ('--api', 'https://u:p@example.invalid/api'),
                 ('--api', 'https://example.invalid/api?q=1'), ('--api', 'https://example.invalid/api#fragment')]
    for args in conflicts:
        with contextlib.redirect_stderr(io.StringIO()):
            try: share.parse_args(args)
            except SystemExit as exc: code = exc.code
            else: code = 0
        expect('CLI rejects ' + args[0] + ' ' + args[-1], code, 2)
    with server('redirect') as api:
        status, _body = share.request('POST', api + '/share', body={'handle': 'tester'}, token=TOKEN)
        expect('redirect refused without forwarding token', status == 307 and len(Stub.calls) == 1)


def test_collection_once_and_network_integrity_order():
    with corpus() as (root, _resolved), server() as api:
        trace = []
        def wrap(name):
            original = getattr(history.History, name)
            def tracked(store, *args, **kwargs):
                trace.append(name)
                return original(store, *args, **kwargs)
            return tracked
        request = share.request
        def tracked_request(*args, **kwargs):
            trace.append('request-' + args[0])
            return request(*args, **kwargs)
        with contextlib.ExitStack() as stack:
            for name in ('capture', 'commit', 'coverage', 'check_integrity', 'prepare', 'confirm'):
                stack.enter_context(mock.patch.object(history.History, name, wrap(name)))
            stack.enter_context(mock.patch.object(share, 'request', tracked_request))
            rc, _o, _e = _run(root, '--api', api, '--handle', 'tester', '--yes')
        expect('collection capture once', trace.count('capture'), 1)
        expect('collection coverage once', trace.count('coverage'), 1)
        expect('capture commit coverage order', trace.index('capture') < trace.index('commit') < trace.index('coverage'))
        expect('explicit integrity prepare request order', trace[trace.index('prepare') - 1], 'check_integrity')
        expect('prepared receipt checked before POST', trace[trace.index('request-POST') - 1], 'check_integrity')
        expect('confirmed receipt checked before PUT', trace[trace.index('request-PUT') - 1], 'check_integrity')
        expect('confirm before report request', trace.index('confirm') < trace.index('request-PUT'))
        expect('collection send succeeds', rc, 0)


def test_atomic_state_write():
    with corpus() as (_root, resolved):
        resolved['state_dir'].mkdir(parents=True, exist_ok=True)
        victim = resolved['state_dir'] / 'victim'
        victim.write_bytes(b'untouched')
        planted = Path(str(resolved['share_state_path']) + '.tmp')
        try: planted.symlink_to(victim)
        except (OSError, NotImplementedError): planted.write_bytes(b'planted')
        state = {'schema': 1, 'active_endpoint': share.DEFAULT_API, 'endpoints': {
            share.DEFAULT_API: dict(token=TOKEN, handle='tester', history_uuid='invented-uuid', token_binding='invented-binding')}}
        share.save_state(resolved, state)
        expect('atomic state round trip', share.load_state(resolved), state)
        expect('state reader exact format', share.reportcli._receipt_binding(resolved), (share.DEFAULT_API, 'invented-binding'))
        expect('planted temp ignored', victim.read_bytes(), b'untouched')
        original = resolved['share_state_path'].read_bytes()
        with mock.patch.object(share.os, 'replace', side_effect=OSError('PRIVATE-PATH')):
            try: share.save_state(resolved, state)
            except OSError: pass
        expect('failed save preserves token file', resolved['share_state_path'].read_bytes(), original)
        expect('failed save cleans temporary', list(resolved['state_dir'].glob('.claude-share-*.tmp')), [])


def _contract_base():
    return {'schema': 1, 'client': dict(share.CLIENT), 'handle': 'tester', 'generated_at': ISO_NOW,
        'days': [dict(date='2026-09-10', responses=4, input=1000, cached=400, output=100, reasoning=20, sessions=1,
                      tiers={'standard': dict(responses=2, input=200, cached=40, output=20, reasoning=4)})],
        'sessions': [dict(id='a' * 16, day='2026-09-10', start='2026-09-10T00:00:00Z', end='2026-09-10T00:00:10Z',
                          active_s=10, responses=2, input=200, cached=40, output=20, reasoning=4, model='claude-opus-5-5')],
        'windows': [dict(start='2026-09-10T00:00:00Z', window_minutes=10080, plan=None, first_pct=20, peak_pct=25,
                        responses=2, input=100, cached=40, output=10,
                        split=[dict(model='claude-opus-5-5', tier='standard', responses=2, input=100, cached=40, output=10,
                                    cache_write_5m=10, cache_write_1h=20)])],
        'latency': dict(**{'from': '2026-09-10', 'to': '2026-09-10'}, plan=None,
            responses=dict(n=4, median_s=9, p90_s=10), turns=dict(n=1, median_s=10, p90_s=11, model_share=.7),
            groups=[dict(model='claude-opus-5-5', effort='high', n=2, median_s=9, p90_s=10, overhead_s=None, output_tps=None, above_share=None)],
            tier_groups=[dict(model='claude-opus-5-5', effort='high', tier='standard', n=2, median_s=9, p90_s=10)])}


def rejection_cases():
    """(literal clause, required local issue, wire fixture, stricter-than-harness)."""
    out = []
    def add(clause, issue, path, value, strict=False):
        p = _contract_base()
        at = p
        for k in path[:-1]: at = at[k]
        at[path[-1]] = value
        out.append((clause, issue, p, strict))
    # Every Zod field family and numerical bound, followed by every 422 clause.
    shapes = [('schema', 'payload_schema', ['schema'], 2), ('client', 'client_shape', ['client'], []),
        ('client name shape', 'client_name', ['client', 'name'], ''), ('client version shape', 'client_version', ['client', 'version'], ''),
        ('generated_at', 'generated_at_invalid', ['generated_at'], 'private text'),
        ('days', 'days_shape', ['days'], {}), ('day date', 'day_date', ['days', 0, 'date'], '2026-02-30'),
        ('session ID shape', 'session_id', ['sessions', 0, 'id'], 'PRIVATE-ID'),
        ('session timestamp', 'session_instant', ['sessions', 0, 'end'], 'not-an-instant'),
        ('session model', 'session_model', ['sessions', 0, 'model'], 'x' * 81),
        ('window plan', 'window_plan', ['windows', 0, 'plan'], 'x' * 41),
        ('window duration shape', 'window_minutes', ['windows', 0, 'window_minutes'], 0),
        ('window percent', 'window_percent', ['windows', 0, 'peak_pct'], 100.01),
        ('split model', 'split_model', ['windows', 0, 'split', 0, 'model'], 'x' * 81),
        ('split tier', 'split_tier', ['windows', 0, 'split', 0, 'tier'], 'PRIVATE-TIER'),
        ('latency date', 'latency_dates', ['latency', 'from'], '2026-02-30'),
        ('timing n', 'timing_count', ['latency', 'responses', 'n'], 0),
        ('turn bool n is not zero', 'timing_count', ['latency', 'turns', 'n'], False),
        ('timing seconds', 'timing_seconds', ['latency', 'responses', 'median_s'], -1),
        ('group model', 'group_model', ['latency', 'groups', 0, 'model'], 'x' * 81),
        ('group effort', 'group_effort', ['latency', 'groups', 0, 'effort'], 'x' * 41),
        ('group fit', 'group_fit', ['latency', 'groups', 0, 'overhead_s'], -1),
        ('group share', 'group_share', ['latency', 'groups', 0, 'above_share'], 1.01),
        ('group tier', 'group_tier', ['latency', 'tier_groups', 0, 'tier'], 'no-tier')]
    shapes += [('day row', 'day_shape', ['days', 0], None), ('tiers row', 'day_tiers_shape', ['days', 0, 'tiers'], []),
        ('tier counts', 'tier_counts', ['days', 0, 'tiers', 'standard', 'responses'], False),
        ('sessions array', 'sessions_shape', ['sessions'], {}), ('session row', 'session_shape', ['sessions', 0], []),
        ('session day', 'session_day', ['sessions', 0, 'day'], 'private text'),
        ('windows array', 'windows_shape', ['windows'], {}), ('window row', 'window_shape', ['windows', 0], []),
        ('window start', 'window_start', ['windows', 0, 'start'], 'private text'),
        ('split array', 'split_shape', ['windows', 0, 'split'], {}), ('split row', 'split_row_shape', ['windows', 0, 'split', 0], []),
        ('latency row', 'latency_shape', ['latency'], []), ('timing row', 'timing_shape', ['latency', 'responses'], []),
        ('groups array', 'groups_shape', ['latency', 'groups'], {}), ('group row', 'group_shape', ['latency', 'groups', 0], []),
        ('tier groups array', 'tier_groups_shape', ['latency', 'tier_groups'], {})]
    for args in shapes: add(*args)
    for field in share_validate.COUNT_FIELDS + ('sessions',):
        for v in (-1, True, '2', 1.5): add('day strict count ' + field + str(v), 'day_counts', ['days', 0, field], v)
    for field in share_validate.COUNT_FIELDS + ('active_s',):
        add('session strict count ' + field, 'session_counts', ['sessions', 0, field], -1)
    for field in share_validate.WINDOW_COUNTS:
        add('window strict count ' + field, 'window_counts', ['windows', 0, field], -1)
        add('split strict count ' + field, 'split_counts', ['windows', 0, 'split', 0, field], -1)
    for key, count in (('days', 4001), ('sessions', 3001), ('windows', 1001)):
        add(key + ' budget', key + '_budget', [key], [_contract_base()[key][0]] * count)
    add('split row budget', 'split_budget', ['windows', 0, 'split'], [_contract_base()['windows'][0]['split'][0]] * 51)
    p = _contract_base()
    p['windows'] *= 21
    p['windows'] = [dict(w, split=w['split'] * 50) for w in p['windows']]
    out.append(('split total budget', 'split_total_budget', p, False))
    for key in ('groups', 'tier_groups'):
        add(key + ' budget', key + '_budget', ['latency', key], [_contract_base()['latency'][key][0]] * 51)
    arithmetic = [('day before', 'day_before', ['days', 0, 'date'], '2025-03-31'),
        ('day future', 'day_future', ['days', 0, 'date'], '2026-10-03'),
        ('day responses', 'day_no_responses', ['days', 0, 'responses'], 0),
        ('day cached', 'day_cached', ['days', 0, 'cached'], 1001),
        ('day reasoning', 'day_reasoning', ['days', 0, 'reasoning'], 101),
        ('day input bound', 'day_input_bound', ['days', 0, 'input'], 20000000001),
        ('day sessions', 'day_sessions', ['days', 0, 'sessions'], 5),
        ('tier cached', 'tier_cached', ['days', 0, 'tiers', 'standard', 'cached'], 201),
        ('tier reasoning', 'tier_reasoning', ['days', 0, 'tiers', 'standard', 'reasoning'], 21),
        ('tier responses', 'tier_no_responses', ['days', 0, 'tiers', 'standard', 'responses'], 0),
        ('session reversal', 'session_reversed', ['sessions', 0, 'end'], '2026-09-09T00:00:00Z'),
        ('session span', 'session_span', ['sessions', 0, 'start'], '2025-01-01T00:00:00Z'),
        ('session future', 'session_future', ['sessions', 0, 'end'], '2026-10-02T00:00:00Z'),
        ('session active span', 'session_active_span', ['sessions', 0, 'active_s'], 12),
        ('session active bound', 'session_active_bound', ['sessions', 0, 'active_s'], 2592001),
        ('session day range', 'session_day_range', ['sessions', 0, 'day'], '2025-03-31'),
        ('session day missing', 'session_day_missing', ['sessions', 0, 'day'], '2026-09-11'),
        ('session response', 'session_no_responses', ['sessions', 0, 'responses'], 0),
        ('session cached', 'session_cached', ['sessions', 0, 'cached'], 201),
        ('session reasoning', 'session_reasoning', ['sessions', 0, 'reasoning'], 21),
        ('session total', 'session_input_total', ['sessions', 0, 'input'], 1001),
        ('window before', 'window_before', ['windows', 0, 'start'], '2025-03-31T00:00:00Z'),
        ('window future', 'window_future', ['windows', 0, 'start'], '2026-10-01T00:10:01Z'),
        ('window first above peak', 'window_first_peak', ['windows', 0, 'first_pct'], 26),
        ('window cached', 'window_cached', ['windows', 0, 'cached'], 101),
        ('window responses', 'window_no_responses', ['windows', 0, 'responses'], 0),
        ('window total', 'window_input_total', ['windows', 0, 'input'], 1001),
        ('split cached', 'split_cached', ['windows', 0, 'split', 0, 'cached'], 101),
        ('cache creation', 'split_cache_write', ['windows', 0, 'split', 0, 'cache_write_5m'], 41),
        ('split responses', 'split_no_responses', ['windows', 0, 'split', 0, 'responses'], 0),
        ('latency reversed', 'latency_reversed', ['latency', 'from'], '2026-09-11'),
        ('latency before', 'latency_before', ['latency', 'from'], '2025-03-31'),
        ('latency future', 'latency_future', ['latency', 'to'], '2026-10-03'),
        ('latency span', 'latency_span', ['latency', 'from'], '2026-01-01'),
        ('response median', 'response_median', ['latency', 'responses', 'median_s'], 11),
        ('response cap', 'response_p90', ['latency', 'responses', 'p90_s'], 3600.001),
        ('response total', 'response_total', ['latency', 'responses', 'n'], 5),
        ('turn median', 'turn_median', ['latency', 'turns', 'median_s'], 12),
        ('turn cap', 'turn_p90', ['latency', 'turns', 'p90_s'], 7200.001),
        ('turn total', 'turn_total', ['latency', 'turns', 'n'], 5)]
    for args in arithmetic: add(*args)
    for field in share_validate.COUNT_FIELDS:
        add('tier parent ' + field, 'tier_' + field + '_total', ['days', 0, 'tiers', 'standard', field], _contract_base()['days'][0][field] + 1)
    for field in share_validate.WINDOW_COUNTS:
        add('split parent ' + field, 'split_' + field + '_total', ['windows', 0, 'split', 0, field], _contract_base()['windows'][0][field] + 1)
    for key, issue, path in (('days', 'day_duplicate', ['days']), ('sessions', 'session_duplicate', ['sessions']),
                             ('windows', 'window_duplicate', ['windows']), ('groups', 'group_duplicate', ['latency', 'groups']),
                             ('tier_groups', 'tier_group_duplicate', ['latency', 'tier_groups']),
                             ('split', 'split_duplicate', ['windows', 0, 'split'])):
        p = _contract_base()
        at = p
        for k in path: at = at[k]
        at.append(copy.deepcopy(at[0]))
        out.append((key + ' duplicate', issue, p, False))
    for key, prefix in (('groups', 'group'), ('tier_groups', 'tier_group')):
        for field, v, suffix in (('n', 5, 'total'), ('median_s', 11, 'median'), ('p90_s', 3601, 'p90')):
            add(key + ' ' + suffix, prefix + '_' + suffix, ['latency', key, 0, field], v)
    for key, field, cap in (('utc_hours', 'hour', 24), ('utc_weekdays', 'day', 7)):
        base = {field: 1, 'n': 1, 'median_s': 10}
        for clause, issue, buckets in [('duplicate', 'clock_duplicate', [base, base]),
            ('median', 'clock_median', [dict(base, median_s=3601)]),
            ('total', 'clock_total', [dict(base, n=5)]), ('shape', 'clock_key', [dict(base, **{field: cap + 1})]),
            ('budget', 'clock_budget', [base] * (cap + 1))]:
            add(key + ' ' + clause, issue, ['latency', key], buckets)
        for clause, issue, buckets in [('array', 'clock_shape', {}), ('row', 'clock_shape', [None]),
            ('count', 'clock_count', [dict(base, n=0)]), ('seconds', 'clock_seconds', [dict(base, median_s=-1)]),
            ('above', 'clock_above', [dict(base, median_above_s=-1)])]:
            add(key + ' ' + clause, issue, ['latency', key], buckets)
    # Explicitly prove the additional client restrictions, without pretending the
    # unchanged vendored safeParse enforces these stricter clauses.
    for args in [('client_name', 'client_name', ['client', 'name'], 'other-client', True),
                 ('client_version', 'client_version', ['client', 'version'], '9.9.9', True),
                 ('handle_invalid', 'handle_invalid', ['handle'], 'bad--handle', True),
                 ('session_id_16', 'session_id', ['sessions', 0, 'id'], 'a' * 12, True),
                 ('window_minutes_weekly', 'window_minutes', ['windows', 0, 'window_minutes'], 300, True),
                 ('payload_body_budget', 'payload_body_budget', ['ignored'], 'x' * 2000000, True)]:
        add(*args)
    return out


def contract(payloads):
    proc = subprocess.run(['node', str(REPO / 'scripts/server_contract/check.mjs'), '--now', ISO_NOW],
        input=''.join(json.dumps(p, ensure_ascii=False, separators=(',', ':')) + '\n' for p in payloads),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', check=False)
    if proc.returncode: raise RuntimeError('vendored contract harness failed')
    return [json.loads(line) for line in proc.stdout.splitlines()]


def test_every_local_validation_rejection_matches_server():
    fixtures = rejection_cases()
    answers = contract([f[2] for f in fixtures])
    expect('NDJSON rejection answer count', len(answers), len(fixtures))
    for (clause, issue, payload, strict), answer in zip(fixtures, answers):
        issues = share_validate.validate_payload(payload, now=NOW)
        expect('local clause ' + clause, issue in issues)
        expect('fixed issue catalogue ' + clause, set(issues) <= share_validate.ISSUES)
        expect('server clause ' + clause, answer['shareAccepted'], strict)
    reports = report_rejection_cases()
    for (body, issue, accepted), answer in zip(reports, contract([r[0] for r in reports])):
        expect('report local ' + issue, issue in share_validate.validate_report(body))
        expect('report envelope parity ' + issue, answer['report']['success'], accepted)
    expect('payload object rejected', share_validate.validate_payload([], now=NOW), ['payload_object'])
    expect('report object rejected', share_validate.validate_report([]), ['report_object'])


def report_rejection_cases():
    good = share.report_body(b'<!doctype html>')
    return [({}, 'report_schema', False), ([], 'report_object', False),
        (dict(good, schema=2), 'report_schema', False), (dict(good, client=[]), 'client_shape', False),
        (dict(good, client=dict(share.CLIENT, name='other-client')), 'client_name', True),
        (dict(good, client=dict(share.CLIENT, version='9.9.9')), 'client_version', True),
        (dict(good, html_gz='!'), 'report_base64', False), (dict(good, html_gz='A' * 1200001), 'report_base64', False),
        (share.report_body(b'<html>'), 'report_doctype', True),
        (dict(good, html_gz=base64.b64encode(b'plain').decode()), 'report_gzip', True),
        (share.report_body(b'<!doctype html>' + b'a' * 8000001), 'report_size', True),
        (dict(good, ignored='a' * 2000000), 'report_body_budget', True)]


def test_every_valid_payload_passes_server():
    valid(_contract_base())
    answers = contract(VALID + VALID_REPORTS)
    for i, answer in enumerate(answers[:len(VALID)]):
        expect('valid server payload %d' % i, answer['shareAccepted'])
    for i, answer in enumerate(answers[len(VALID):]):
        expect('valid server report envelope %d' % i, answer['report']['success'])


def test_validator_extreme_numbers_and_gzip():
    for value in (10 ** 400, float('nan'), float('inf')):
        p = _contract_base()
        p['days'][0]['input'] = value
        issues = share_validate.validate_payload(p, now=NOW)
        expect('extreme number returns catalogue', bool(issues) and set(issues) <= share_validate.ISSUES)
    body = share.report_body(b'<!doctype html>')
    gz = bytearray(base64.b64decode(body['html_gz']))
    gz[10:14] = b'\xff\xff\xff\xff'
    expect('corrupt deflate returns catalogue', set(share_validate.validate_report(dict(body,
        html_gz=base64.b64encode(gz).decode()))) <= share_validate.ISSUES)


def test_json_number_and_instant_semantics():
    p = _contract_base()
    p['schema'], p['windows'][0]['window_minutes'] = 1.0, 10080.0
    p['days'][0]['responses'] = 4.0
    p['latency']['turns'] = {'n': 0.0}
    valid(p)
    expect('JSON integer floats mirror JavaScript', share_validate.validate_payload(p, now=NOW), [])
    bad = _contract_base()
    bad['latency']['turns']['n'] = False
    expect('Boolean zero turn is rejected', 'timing_count' in share_validate.validate_payload(bad, now=NOW))
    p = _contract_base()
    p['generated_at'] = '0000-02-29T00:00:00Z'
    valid(p)
    expect('ISO year zero follows server calendar', share_validate.validate_payload(p, now=NOW), [])
    p = _contract_base()
    end = worker.epoch(p['sessions'][0]['end'])
    p['sessions'][0].update(start=share._iso(end - share_validate.MAX_SPAN_S), end=share._iso(end + .0009))
    valid(p)
    p = _contract_base()
    p['sessions'][0]['end'] = share._iso(NOW + 600.0009)
    valid(p)
    expect('server millisecond future boundary', share_validate.validate_payload(p, now=NOW), [])
    p = _contract_base()
    p['generated_at'] = '9999-12-31T23:59:59.999Z'
    valid(p)
    body = dict(share.report_body(b'<!doctype html>'), schema=1.0)
    expect('report JSON literal numeric equality', share_validate.validate_report(body), [])
    VALID_REPORTS.append(body)


# One pure decision assertion per payload issue complements the full NDJSON matrix.
# Mutation tests use these same fixtures without running the Node process again.
VALIDATION_DECISIONS = {}
for _clause, _issue, _fixture, _strict in rejection_cases():
    if _issue in VALIDATION_DECISIONS:
        continue
    _name = 'test_validation_' + _issue
    def _make_validation_test(issue, fixture, name):
        def test():
            expect('local validation rejects ' + issue, issue in share_validate.validate_payload(fixture, now=NOW))
        test.__name__ = name
        return test
    globals()[_name] = _make_validation_test(_issue, _fixture, _name)
    VALIDATION_DECISIONS[_issue] = _name

REPORT_VALIDATION_DECISIONS = {}
for _fixture, _issue, _strict in report_rejection_cases():
    if _issue in REPORT_VALIDATION_DECISIONS:
        continue
    _name = 'test_report_validation_' + _issue
    def _make_report_validation_test(issue, fixture, name):
        def test():
            expect('local report validation rejects ' + issue, issue in share_validate.validate_report(fixture))
        test.__name__ = name
        return test
    globals()[_name] = _make_report_validation_test(_issue, _fixture, _name)
    REPORT_VALIDATION_DECISIONS[_issue] = _name


SHARE_DECISIONS = ('test_month_guard_steps', 'test_month_withholding', 'test_window_omission_preserves_store_semantics',
    'test_window_union_and_plan_reassessment', 'test_session_top_union', 'test_session_selection_and_budgets',
    'test_payload_size_whole_month', 'test_cache_creation_subset', 'test_known_tier_subset',
    'test_unknown_speed_shared_count', 'test_endpoint_binding_decisions', 'test_normalized_endpoint_decisions',
    'test_window_advancement_decisions', 'test_session_summary_bounds_and_model', 'test_latency_group_budgets',
    'test_retained_window_touching_withheld_month', 'test_full_window_union_budget',
    'test_withheld_session_diagnostic_selection', 'test_shared_timing_diagnostics',
    'test_json_number_and_instant_semantics') + tuple(VALIDATION_DECISIONS.values()) + tuple(REPORT_VALIDATION_DECISIONS.values())


@contextlib.contextmanager
def offline():
    original_connect = socket.socket.connect
    original_open, original_io_open = builtins.open, io.open
    def connect(sock, address):
        try: allowed = ipaddress.ip_address(address[0]).is_loopback
        except (ValueError, TypeError): allowed = False
        if not allowed: raise AssertionError('test attempted non-loopback networking')
        return original_connect(sock, address)
    def guarded(original):
        def opened(file, *args, **kwargs):
            if not isinstance(file, int) and Path(file).name.lower() == '.credentials.json':
                raise AssertionError('credentials open attempt')
            return original(file, *args, **kwargs)
        return opened
    with mock.patch.object(socket.socket, 'connect', connect), mock.patch.object(builtins, 'open', guarded(original_open)), mock.patch.object(io, 'open', guarded(original_io_open)):
        yield


def main() -> int:
    tests = [v for name, v in list(globals().items()) if name.startswith('test_')
             and name not in ('test_every_valid_payload_passes_server', 'test_every_local_validation_rejection_matches_server')]
    with offline():
        for test in tests + [test_every_local_validation_rejection_matches_server, test_every_valid_payload_passes_server]:
            start = len(RESULTS)
            test()
            failures = [name for name, ok, _detail in RESULTS[start:] if not ok]
            print('[%s] %s (%d assertions)' % ('FAIL' if failures else 'PASS', test.__name__, len(RESULTS) - start))
            for failure in failures: print('  ' + failure)
    bad = sum(not ok for _name, ok, _detail in RESULTS)
    print('[PASS] credentials open-spy and non-loopback socket guard')
    print('\n%d/%d Claude share assertions passed' % (len(RESULTS) - bad, len(RESULTS)))
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
