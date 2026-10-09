"""Tests for the token-share skill: the payload, what it must never carry, and the transport.

Run with ``python scripts/test_share.py``. No network: the server is a stub on localhost.
"""
import contextlib
import copy
import datetime
import gzip
import base64
import http.server
import io
import json
import os
import stat
import sys
import tempfile
import threading
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARE = os.path.join(REPO, 'plugins', 'token-counter', 'skills', 'token-share', 'scripts')
sys.path.insert(0, SHARE)

import share  # noqa: E402
from tokencounter import analyze, latency, ledger, pricing, rollout, worker  # noqa: E402

RESULTS = []
SECRET_PROMPT = 'PLEASE-NEVER-UPLOAD-THIS-PROMPT'
SECRET_CWD = '/home/someone/very-private-project'


def check(name, ok, detail=''):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f'\n        {detail}' if not ok else ''))


def _session(sid, stamps, inp=1000, cached=400, out=50, model='gpt-5-codex'):
    """A rollout with one response per timestamp, and content that must never be sent."""
    recs = [{'timestamp': stamps[0], 'type': 'session_meta',
             'payload': {'session_id': sid, 'id': sid, 'cwd': SECRET_CWD}}]
    for i, ts in enumerate(stamps):
        recs.append({'timestamp': ts, 'type': 'turn_context',
                     'payload': {'model': model, 'effort': 'high', 'cwd': SECRET_CWD}})
        recs.append({'timestamp': ts, 'type': 'response_item',
                     'payload': {'type': 'message', 'role': 'user',
                                 'content': [{'type': 'input_text', 'text': SECRET_PROMPT}]}})
        recs.append({'timestamp': ts, 'type': 'token_usage_record',
                     'payload': {'response_id': f'{sid}-r{i}', 'usage': {
                         'input_tokens': inp, 'cached_input_tokens': cached,
                         'output_tokens': out, 'reasoning_output_tokens': out // 2,
                         'total_tokens': inp + out}}})
    return recs


def _corpus(sessions):
    root = tempfile.mkdtemp()
    for sid, stamps, kw in sessions:
        day = stamps[0][:10].split('-')
        d = os.path.join(root, *day)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f'rollout-{stamps[0][:10]}T00-00-00-{sid}.jsonl'), 'w',
                  encoding='utf-8') as fh:
            for r in _session(sid, stamps, **kw):
                fh.write(json.dumps(r) + '\n')
    return root


def _build(root):
    results = {p: worker.metrics_only(p) for p in rollout.discover(root)}
    charged, counters = ledger.build(results)
    payload, notes = share.build_payload(results, charged, handle='tester')
    return results, charged, counters, payload, notes


def _utc(monkey=True):
    """Pin the local zone to UTC so local days are predictable."""
    os.environ['TZ'] = 'UTC'
    if hasattr(__import__('time'), 'tzset'):
        __import__('time').tzset()
    analyze._local_day.cache_clear()


# ---------------------------------------------------------------------------- payload

CORPUS = [
    # 3 responses, 10 min apart, then a 2-hour gap, then 2 more 5 min apart: 25 min active.
    ('11111111-aaaa', ['2026-08-31T10:00:00.000Z', '2026-08-31T10:10:00.000Z',
                       '2026-08-31T10:20:00.000Z', '2026-08-31T12:20:00.000Z',
                       '2026-08-31T12:25:00.000Z'], {}),
    ('22222222-bbbb', ['2026-09-01T09:00:00.000Z', '2026-09-01T09:01:00.000Z'],
     {'inp': 50_000, 'cached': 10_000, 'out': 2_000}),
    ('33333333-cccc', ['2026-09-01T23:50:00.000Z', '2026-09-02T00:10:00.000Z'], {}),
]


def test_payload_shape():
    _utc()
    results, charged, counters, p, notes = _build(_corpus(CORPUS))
    check('schema and client are stamped', p['schema'] == 1 and p['client']['name'] == 'token-counter')
    check('the handle rides along when given', p.get('handle') == 'tester')
    days = {d['date']: d for d in p['days']}
    check('days are the local days responses landed on',
          sorted(days) == ['2026-08-31', '2026-09-01', '2026-09-02'], str(sorted(days)))
    check('a day counts its responses and tokens',
          days['2026-08-31']['responses'] == 5 and days['2026-08-31']['input'] == 5000
          and days['2026-08-31']['cached'] == 2000 and days['2026-08-31']['output'] == 250,
          str(days['2026-08-31']))
    check('a session is counted on the day of its first response',
          days['2026-09-01']['sessions'] == 2 and days['2026-09-02']['sessions'] == 0,
          str({k: v['sessions'] for k, v in days.items()}))

    s = {x['id']: x for x in p['sessions']}
    first = s[share.session_hash('11111111-aaaa')]
    check('active time leaves out gaps over the idle cap', first['active_s'] == 25 * 60,
          str(first['active_s']))
    check('start and end are the first and last response',
          first['start'] == '2026-08-31T10:00:00Z' and first['end'] == '2026-08-31T12:25:00Z',
          str((first['start'], first['end'])))
    night = s[share.session_hash('33333333-cccc')]
    check('a session crossing midnight belongs to the day it started',
          night['day'] == '2026-09-01' and night['active_s'] == 20 * 60, str(night))
    check('the model is named', first['model'] == 'gpt-5-codex')
    check('session ids are one-way hashes, 16 hex characters',
          all(len(x['id']) == 16 and all(c in '0123456789abcdef' for c in x['id'])
              for x in p['sessions']))


def test_payload_agrees_with_the_report():
    """The leaderboard must show what the local report shows, day for day."""
    _utc()
    results, charged, counters, p, _ = _build(_corpus(CORPUS))
    model = analyze.analyze(results, charged, counters, scope={'label': 'test'})
    rep = {d['date']: (d['responses'], d['input'], d['cached'], d['output']) for d in model['daily']}
    ours = {d['date']: (d['responses'], d['input'], d['cached'], d['output']) for d in p['days']}
    check('every day matches the report model', rep == ours, f'report={rep}\nshare={ours}')


def test_nothing_private_is_sent():
    _utc()
    *_, p, _ = _build(_corpus(CORPUS))
    blob = json.dumps(p)
    check('no prompt text in the payload', SECRET_PROMPT not in blob)
    check('no working directory in the payload', SECRET_CWD not in blob and 'someone' not in blob)
    check('no raw session id in the payload', '11111111-aaaa' not in blob)
    check('only the documented keys are sent',
          set(p) == {'schema', 'client', 'generated_at', 'days', 'sessions', 'windows', 'handle',
                     'api_value'}
          and all(set(d) == {'date', 'responses', 'input', 'cached', 'output', 'reasoning',
                             'sessions', 'api_usd', 'tiers'} for d in p['days'])
          and all(set(x) == {'id', 'day', 'start', 'end', 'active_s', 'responses', 'input',
                             'cached', 'output', 'reasoning', 'model', 'api_usd'}
                  for x in p['sessions'])
          and set(p['api_value']) == {'usd', 'usd_high', 'tokens_usd', 'web_search_usd',
                                      'web_search_calls', 'prices_as_of', 'prices_source',
                                      'prices_default', 'responses', 'priced', 'unpriced',
                                      'tier_unrecorded', 'tier_inferred', 'tiers',
                                      'aborted_turns'},
          json.dumps(p)[:400])
    *_, w, _ = _build(_corpus_file(_limit_records()))
    check('only the documented keys are sent per limit window',
          w['windows'] and all(set(x) == {'start', 'window_minutes', 'plan', 'first_pct',
                                          'peak_pct', 'responses', 'input', 'cached', 'output',
                                          'split'}
                               for x in w['windows']),
          json.dumps(w['windows'])[:400])


def _timed_records(day, seconds):
    """One response per entry of `seconds`: the prompt, the model's answer, then the usage
    record that many seconds after the prompt."""
    recs = [{'timestamp': f'{day}T10:00:00.000Z', 'type': 'session_meta',
             'payload': {'session_id': 'L', 'id': 'L', 'cwd': SECRET_CWD}}]
    t = datetime.datetime.fromisoformat(f'{day}T10:00:00+00:00')

    def stamp(x):
        return x.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    for i, s in enumerate(seconds):
        recs.append({'timestamp': stamp(t), 'type': 'response_item',
                     'payload': {'type': 'message', 'role': 'user',
                                 'content': [{'type': 'input_text', 'text': SECRET_PROMPT}]}})
        recs.append({'timestamp': stamp(t + datetime.timedelta(seconds=1)),
                     'type': 'response_item',
                     'payload': {'type': 'message', 'role': 'assistant',
                                 'content': [{'type': 'output_text', 'text': 'ok'}]}})
        t += datetime.timedelta(seconds=s)
        recs.append({'timestamp': stamp(t), 'type': 'token_usage_record',
                     'payload': {'response_id': f'L-r{i}', 'usage': {
                         'input_tokens': 1000 + i, 'cached_input_tokens': 0,
                         'output_tokens': 50, 'reasoning_output_tokens': 0,
                         'total_tokens': 1050 + i}}})
        t += datetime.timedelta(seconds=60)
    return recs


def test_latency():
    _utc()
    day = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=2)).strftime('%Y-%m-%d')
    results, charged, _, p, _ = _build(_corpus_file(_timed_records(day, [2, 4, 6, 8, 10])))
    lat = p.get('latency') or {}
    check('response times are sent as the server\'s one latency object',
          lat.get('from') == day and lat.get('to') == day
          and lat['responses'] == {'n': 5, 'median_s': 6.0, 'p90_s': 9.2}, str(lat))
    check('turns is null when no turn was timed', lat.get('turns') is None, str(lat))
    check('only the documented keys are sent for latency',
          set(lat) == {'from', 'to', 'responses', 'turns', 'groups', 'tier_groups', 'plan'}
          and all(set(g) == {'model', 'effort', 'n', 'median_s', 'p90_s', 'overhead_s',
                             'output_tps', 'above_share'} for g in lat['groups'])
          and all(set(g) == {'model', 'effort', 'tier', 'n', 'median_s', 'p90_s',
                             'overhead_s', 'output_tps', 'above_share'}
                  for g in lat['tier_groups']), str(lat))
    check('groups split the timed responses, one per model and effort',
          [(g['model'], g['effort'], g['n']) for g in lat['groups']]
          == [('unknown', 'unknown', 5)], str(lat['groups']))
    model = analyze.analyze(results, charged, {}, scope={'label': 'test'})
    r = model['latency']['responses']
    check('the figures are the report\'s',
          (r['n'], r['median_s'], r['p90_s']) == (5, 6.0, 9.2), str(r))
    check('no prompt text rides along', SECRET_PROMPT not in json.dumps(p))
    *_, q, _ = _build(_corpus(CORPUS))
    check('a corpus that cannot be timed sends no latency', 'latency' not in q, str(q.keys()))
    *_, old, _ = _build(_corpus_file(_timed_records('2026-01-05', [2, 4, 6, 8, 10])))
    check('responses older than a month are not timed', 'latency' not in old, str(old.get('latency')))


def test_api_value():
    """The API value rides along per day, per session and in total, priced as the report
    prices it; a day with nothing priced sends null, and no table sends no field."""
    _utc()
    results, charged, counters, p, _ = _build(_corpus(CORPUS))
    av = p['api_value']
    table, _ = pricing.load()
    rate = table['models']['gpt-5-codex']['standard']
    # CORPUS: 7 responses of 1,000 in / 400 cached / 50 out, and 2 of 50,000 / 10,000 / 2,000.
    want = (7 * (600 * rate['input'] + 400 * rate['cached_input'] + 50 * rate['output'])
            + 2 * (40_000 * rate['input'] + 10_000 * rate['cached_input']
                   + 2_000 * rate['output'])) / 1e6
    check('the total is every response priced at the model\'s list price',
          abs(av['usd'] - want) < 1e-6 and av['priced'] == 9 and av['unpriced'] == 0,
          f"{av} want {want}")
    check('the days add up to the total',
          abs(sum(d['api_usd'] for d in p['days']) - av['usd']) < 1e-3, str(p['days']))
    model = analyze.analyze(results, charged, counters, scope={'label': 'test'})
    check('the total is the report\'s', abs(model['api_value']['usd'] - av['usd']) < 1e-4,
          f"report {model['api_value']['usd']} share {av['usd']}")
    check('the price table is named by its date', av['prices_as_of'] == table['as_of']
          and av['prices_default'] is True, str(av))

    *_, q, _ = _build(_corpus([('44444444-dddd', ['2026-09-03T10:00:00.000Z'],
                                {'model': 'not-a-priced-model'})]))
    check('a day with no priced response sends null, not zero',
          [d['api_usd'] for d in q['days']] == [None]
          and [x['api_usd'] for x in q['sessions']] == [None]
          and q['api_value']['priced'] == 0 and q['api_value']['unpriced'] == 1,
          json.dumps(q)[:400])

    recs = _session('55555555-eeee', ['2026-09-10T10:00:00.000Z'], model='not-a-priced-model')
    recs.insert(3, {'timestamp': '2026-09-10T10:00:00.000Z', 'type': 'response_item',
                    'payload': {'type': 'web_search_call', 'status': 'completed'}})
    *_, ws, _ = _build(_corpus_file(recs))
    check('an unpriced day still carries its search fee, so the days add up to the total',
          [d['api_usd'] for d in ws['days']] == [0.01] and ws['api_value']['usd'] == 0.01
          and ws['api_value']['priced'] == 0 and ws['api_value']['web_search_calls'] == 1,
          json.dumps(ws)[:400])

    root = _corpus(CORPUS)
    results = {pp: worker.metrics_only(pp) for pp in rollout.discover(root)}
    charged, _ = ledger.build(results)
    none, _ = share.build_payload(results, charged, prices=(None, 'test: no table'))
    check('without a price table nothing is sent under the name',
          'api_value' not in none and all(d['api_usd'] is None for d in none['days']),
          json.dumps(none)[:300])


def test_sessions_are_capped_per_month():
    _utc()
    # Gaps of 1..29 minutes, all inside the idle cap; session 28 has the longest.
    many = [(f'{i:08d}-dddd', [f'2026-09-03T{i % 24:02d}:00:00.000Z',
                               f'2026-09-03T{i % 24:02d}:{1 + i % 29:02d}:00.000Z'],
             {'inp': 1000 + i}) for i in range(40)]
    *_, p, notes = _build(_corpus(many))
    n = len(p['sessions'])
    check('at most twice the per-month cap is sent', n <= 2 * share.SESSIONS_PER_MONTH, str(n))
    check('the total session count is still reported', notes['sessions_total'] == 40)
    longest = max(p['sessions'], key=lambda x: x['active_s'])
    check('the longest session survives the cap', longest['active_s'] == 29 * 60, str(longest))
    biggest = max(p['sessions'], key=lambda x: x['input'])
    check('the biggest session survives the cap', biggest['input'] == 1039 * 2, str(biggest))
    check('every session is still counted in its day',
          sum(d['sessions'] for d in p['days']) == 40)


def test_damaged_counts_are_clamped():
    _utc()
    *_, p, notes = _build(_corpus([('44444444-eeee', ['2026-09-04T10:00:00.000Z'],
                                     {'inp': 10, 'cached': 20})]))
    check('cached above input is clamped, and counted',
          p['days'][0]['cached'] == 10 and notes['clamped_cached'] == 1, str(p['days']))


# ---------------------------------------------------------------------------- limit windows

WEEK = 7 * 86400
T0 = 1789000000     # 2026-09-10T00:26:40Z


def _tc(t, pct, resets_at, cum, inp=1000, cached=400, out=10, window=10080, plan='prolite'):
    """A `token_count` event: one response's usage beside the server's rate-limit snapshot."""
    iso = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat().replace(
        '+00:00', 'Z')
    last = {'input_tokens': inp, 'cached_input_tokens': cached, 'output_tokens': out,
            'reasoning_output_tokens': 0, 'total_tokens': inp + out}
    total = {'input_tokens': cum, 'cached_input_tokens': 0, 'output_tokens': 0,
             'reasoning_output_tokens': 0, 'total_tokens': cum}
    return {'timestamp': iso, 'type': 'event_msg',
            'payload': {'type': 'token_count',
                        'info': {'last_token_usage': last, 'total_token_usage': total},
                        'rate_limits': {'limit_id': 'codex', 'plan_type': plan,
                                        'rate_limit_reached_type': None,
                                        'primary': {'used_percent': pct,
                                                    'window_minutes': window,
                                                    'resets_at': resets_at},
                                        'secondary': None}}}


def _limit_records(first_pcts=(0.0, 10.0, 25.0, 40.0), window=10080, plan='prolite'):
    """One consumed window, then an early reset into a second, then an idle slide."""
    recs = [{'timestamp': '2026-09-10T00:26:40Z', 'type': 'session_meta',
             'payload': {'session_id': 'W', 'id': 'W', 'cwd': SECRET_CWD}}]
    cum = 0
    for i, pct in enumerate(first_pcts):
        cum += 1000
        recs.append(_tc(T0 + i * 3600, pct, T0 + WEEK, cum, window=window, plan=plan))
    t1 = T0 + len(first_pcts) * 3600
    for i, pct in enumerate((0.0, 5.0)):
        cum += 1000
        recs.append(_tc(t1 + i * 3600, pct, t1 + WEEK, cum, window=window, plan=plan))
    # Idle: 0% throughout, the reset re-quoted as now + 7 days on every call.
    t2 = t1 + 3 * 3600
    for i in range(3):
        recs.append(_tc(t2 + i * 60, 0.0, t2 + i * 60 + WEEK, cum, inp=0, cached=0, out=0,
                        window=window, plan=plan))
    return recs


def _corpus_file(recs):
    root = tempfile.mkdtemp()
    d = os.path.join(root, '2026', '09', '10')
    os.makedirs(d)
    with open(os.path.join(d, 'rollout-2026-09-10T00-00-00-W.jsonl'), 'w',
              encoding='utf-8') as fh:
        for r in recs:
            fh.write(json.dumps(r) + '\n')
    return root


def test_limit_windows():
    _utc()
    *_, p, _ = _build(_corpus_file(_limit_records()))
    w = p['windows']
    check('each consumed weekly window is sent, and an idle slide is not',
          len(w) == 2, json.dumps(w))
    a, b = w
    check('a window starts where it was first seen, and the next where the percentage drops',
          a['start'] == share._iso(T0) and b['start'] == share._iso(T0 + 4 * 3600),
          f"{a['start']} {b['start']}")
    check('the plan and the reported percentages ride along',
          a['plan'] == 'prolite' and a['first_pct'] == 0 and a['peak_pct'] == 40
          and b['peak_pct'] == 5 and a['window_minutes'] == 10080, json.dumps(a))
    check('tokens are split at the reset, not pooled',
          (a['responses'], a['input'], a['cached'], a['output']) == (4, 4000, 1600, 40)
          and (b['input'], b['output']) == (2000, 20), json.dumps(w))
    check('windows never hold more than the days',
          sum(x['input'] for x in w) <= sum(d['input'] for d in p['days']))

    # Logs that begin partway through a week: the window's first reading says how much of
    # the limit was already gone before this machine saw anything.
    *_, p, _ = _build(_corpus_file(_limit_records(first_pcts=(30.0, 45.0))))
    check('a window first seen partway through keeps its first reading',
          p['windows'][0]['first_pct'] == 30 and p['windows'][0]['peak_pct'] == 45,
          json.dumps(p['windows'][0]))

    *_, p, _ = _build(_corpus_file(_limit_records(window=300)))
    check('only weekly windows are sent', p['windows'] == [], json.dumps(p['windows']))

    # The server refuses a window dated before Codex shipped, and a repeated start; either
    # would fail the whole share, so neither is sent.
    results = {p: worker.metrics_only(p) for p in rollout.discover(_corpus_file(_limit_records()))}
    real = analyze.rate_limit_windows
    fake = real(results, [], newest=None)
    early = dict(fake['windows'][0], reset_at=1735689600)          # 2025-01-01
    twin = dict(fake['windows'][1], reset_at=fake['windows'][0]['reset_at'])
    analyze.rate_limit_windows = lambda *a, **k: dict(fake, windows=[early, fake['windows'][0], twin])
    try:
        sent = share.limit_windows(results, [], T0 + WEEK)
    finally:
        analyze.rate_limit_windows = real
    check('windows the server would refuse are dropped, not the share',
          [w['start'] for w in sent] == [share._iso(T0)], json.dumps(sent))

    *_, p, _ = _build(_corpus(CORPUS))
    check('logs without rate limits send an empty list', p['windows'] == [])


# ---------------------------------------------------------------------------- service tiers

F4_NOW = datetime.datetime(2026, 9, 28, 12, tzinfo=datetime.timezone.utc)
F4_START = worker.epoch('2026-09-10T10:00:00Z')
COUNT_KEYS = ('responses', 'input', 'cached', 'output', 'reasoning')
F4_TIERS = {
    'standard': dict(zip(COUNT_KEYS, (5, 1300, 140, 130, 29))),
    'fast': dict(zip(COUNT_KEYS, (2, 400, 250, 40, 25))),
    'ultrafast': dict(zip(COUNT_KEYS, (1, 300, 100, 30, 7))),
}
F4_SPLIT = [
    dict(model='m', tier='standard', responses=4, input=1000, cached=40, output=100),
    dict(model='m', tier='fast', responses=2, input=400, cached=250, output=40),
    dict(model='n', tier='standard', responses=1, input=300, cached=100, output=30),
    dict(model='n', tier='ultrafast', responses=1, input=300, cached=100, output=30),
]


def _quote(start, plan='prolite', peak=20, last=None):
    return {'window_minutes': 10080, 'resets_at': start + WEEK, 'slot': 'primary',
            'plan_type': plan, 'limit_id': 'codex', 'first_ts': start,
            'last_ts': start + 600 if last is None else last, 'first_pct': 0,
            'last_pct': peak, 'max_pct': peak, 'min_pct': 0, 'n': 2, 'reached': 0,
            'points': [[start, 0], [start + 600 if last is None else last, peak]]}


def _f4():
    files, charged = {}, {}
    raw = [('m', 'default', 100, 20, 10, 2, 6),
           ('m', 'default', 100, 20, 10, 2, 10),
           ('m', 'priority', 200, 50, 20, 5, 2),
           ('m', 'priority', 200, 250, 20, 25, 4),
           ('n', 'flex', 300, 100, 30, 7, 8),
           ('n', 'ultrafast', 300, 100, 30, 7, 12),
           ('m', None, 400, 0, 40, 9, 14),
           ('m', None, 400, 0, 40, 9, 16)]
    for i, (model, tier, inp, cached, out, reasoning, duration) in enumerate(raw):
        path = f'f4-{i // 2}'
        files.setdefault(path, {'session_id': path, 'date': '2026-09-10', 'turn_starts': [],
                                'tool_times': []})
        req = F4_START + 60 * i
        charged.setdefault(path, []).append({
            'model': model, 'effort': 'high', 'tier': tier, 'tier_inferred': i == 2,
            'req_ts': share._iso(req), 'ts': share._iso(req + duration), 'turn': None,
            'replayed': False, 'usage': {'input_tokens': inp, 'cached_input_tokens': cached,
                                       'output_tokens': out, 'reasoning_output_tokens': reasoning}})
    files['f4-0']['rate_limits'] = [_quote(F4_START)]
    return files, charged


@contextlib.contextmanager
def _fixture_utc():
    """Pin local day and timing conversions on Windows as well as POSIX."""
    def day(ts, fallback=None):
        epoch = worker.epoch(ts)
        return (datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).date().isoformat()
                if epoch is not None else fallback)
    with mock.patch.object(analyze, '_day', day), mock.patch.object(
            latency, '_local', lambda t, tz: datetime.datetime.fromtimestamp(t, datetime.timezone.utc)):
        yield


def _f4_latency():
    def group(model, tier, n, median, p90):
        g = dict(model=model, effort='high', n=n, median_s=median, p90_s=p90,
                 overhead_s=None, output_tps=None, above_share=None)
        if tier is not None:
            g['tier'] = tier
        return g
    return {'from': '2026-09-10', 'to': '2026-09-10', 'plan': 'prolite',
            'responses': {'n': 8, 'median_s': 9, 'p90_s': 14.6}, 'turns': None,
            'groups': [group('m', None, 6, 8, 15), group('n', None, 2, 10, 11.6)],
            'tier_groups': [group('m', 'standard', 4, 12, 15.4),
                            group('n', 'ultrafast', 1, 12, 12),
                            group('n', 'standard', 1, 8, 8), group('m', 'fast', 2, 3, 3.8)]}


def test_tier_partitions():
    files, charged = _f4()
    rates = {'input': 1, 'cached_input': .5, 'output': 2, 'cache_write': None}
    table = {'models': {m: {t: rates for t in ('standard', 'fast', 'flex', 'ultrafast')}
                        for m in ('m', 'n')}, 'long_context_threshold': 272000,
             'unit_tokens': 1000000, 'as_of': '2026-09-28', 'source': 'fixture', 'path': 'fixture'}
    for prices in ((None, 'disabled'), (table, None)):
        with _fixture_utc():
            p, notes = share.build_payload(files, charged, handle='ada', now=F4_NOW, prices=prices)
        d, w = p['days'][0], p['windows'][0]
        tiers = d['tiers']
        check('F4 Standard partition includes both absent-tier rows',
              tiers.get('standard') == F4_TIERS['standard'], str(tiers))
        check('F4 Flex contributes to Standard', F4_SPLIT[2] in w['split'], str(w['split']))
        check('F4 has a distinct Ultrafast partition',
              tiers.get('ultrafast') == F4_TIERS['ultrafast'], str(tiers))
        check('F4 has four exact model/tier split rows', w['split'] == F4_SPLIT, str(w['split']))
        check('F4 day contains the complete three-class partition', tiers == F4_TIERS, str(tiers))
        check('F4 ordinary day counts and clamps match the fixture',
              {k: d[k] for k in COUNT_KEYS} == dict(zip(COUNT_KEYS, (8, 2000, 490, 200, 61)))
              and d['date'] == '2026-09-10' and d['sessions'] == 4
              and notes['clamped_cached'] == notes['clamped_reasoning'] == 1, str((d, notes)))
        check('F4 day partitions equal every parent count, including zero fields',
              all(sum(t[k] for t in tiers.values()) == d[k] for k in COUNT_KEYS)
              and all(set(t) == set(COUNT_KEYS) for t in tiers.values()))
        check('F4 window totals, percentages and plan match the fixture',
              {k: v for k, v in w.items() if k != 'split'} == {
                  'start': '2026-09-10T10:00:00Z', 'window_minutes': 10080, 'plan': 'prolite',
                  'first_pct': 0, 'peak_pct': 20, 'responses': 8, 'input': 2000,
                  'cached': 490, 'output': 200}, str(w))
        check('F4 split sums exactly to its window in all four fields',
              all(sum(t[k] for t in w['split']) == w[k] for k in COUNT_KEYS[:-1]))
        wanted_sessions = []
        for i, (start, end, active, inp, cached, out, reasoning, model) in enumerate([
                ('10:00:06', '10:01:10', 64, 200, 40, 20, 4, 'm'),
                ('10:02:02', '10:03:04', 62, 400, 250, 40, 25, 'm'),
                ('10:04:08', '10:05:12', 64, 600, 200, 60, 14, 'n'),
                ('10:06:14', '10:07:16', 62, 800, 0, 80, 18, 'm')]):
            wanted_sessions.append(dict(id=share.session_hash(f'f4-{i}'), day='2026-09-10',
                                        start=f'2026-09-10T{start}Z', end=f'2026-09-10T{end}Z',
                                        active_s=active, responses=2, input=inp, cached=cached,
                                        output=out, reasoning=reasoning, model=model))
        check('F4 has all four exact session summaries and active times 64,62,64,62',
              [{k: v for k, v in s.items() if k != 'api_usd'} for s in p['sessions']]
              == wanted_sessions, str(p['sessions']))
        check('F4 local unrecorded count covers all dated shared responses',
              notes['tier_unrecorded'] == 2 and 'tier_unrecorded' not in d)
        check('F4 timing summaries and independent null fits match exactly',
              p['latency'] == _f4_latency(), str(p['latency']))
        check('F4 schema, version, generation time and handle are exact',
              (p['schema'], p['client'], p['generated_at'], p['handle']) ==
              (1, {'name': 'token-counter', 'version': '1.10.0'}, '2026-09-28T12:00:00Z', 'ada'))
        if prices[0] is None:
            check('unpriced F4 omits API value and sends null day and session values',
                  'api_value' not in p and d['api_usd'] is None
                  and all(s['api_usd'] is None for s in p['sessions']))
        else:
            check('F4 API-value tier counts keep their pricing meanings',
                  p['api_value']['tiers'] == {'standard': 4, 'fast': 2, 'flex': 1, 'ultrafast': 1}
                  and p['api_value']['tier_unrecorded'] == 2
                  and p['api_value']['tier_inferred'] == 1, str(p['api_value']))
        text = share.describe(p, notes, 'ada', share.DEFAULT_API)
        check('the F4 dry run always discloses absent tiers counted as Standard',
              'tier record  2 dated shared responses had no recorded tier; counted as Standard' in text)

    # Dated rows without timestamps remain in both day series; undated ones enter neither.
    extra = {'date': '2026-09-11', 'session_id': 'untimed'}
    row = {'tier': None, 'usage': {}}
    with _fixture_utc():
        p, notes = share.build_payload({'a': extra, 'b': {}}, {'a': [row], 'b': [row]},
                                       now=F4_NOW, prices=(None, 'disabled'))
    check('untimed dated rows partition with zero fields; undated rows are excluded',
          p['days'] == [{'date': '2026-09-11', 'responses': 1, 'input': 0, 'cached': 0,
                         'output': 0, 'reasoning': 0, 'sessions': 1, 'api_usd': None,
                         'tiers': {'standard': dict(zip(COUNT_KEYS, (1, 0, 0, 0, 0)))}}]
          and notes['tier_unrecorded'] == 1 and notes['undated_responses'] == 1
          and not p['windows'] and 'latency' not in p, str((p, notes)))


def test_tier_window_boundaries():
    files, charged = _f4()
    t0, t1 = F4_START, F4_START + 4 * 3600
    files['f4-0']['rate_limits'] = [_quote(t0, peak=40, last=t1 - 3600),
                                    _quote(t1, peak=5, last=t1 + 3600)]
    tokens, splits = [], []
    for i, r in enumerate(r for rows in charged.values() for r in rows):
        u = r['usage']
        inp, out = u['input_tokens'], u['output_tokens']
        cached = min(inp, u['cached_input_tokens'])
        tokens.append((t0 + i * 3600, inp, cached, out))
        splits.append((t0 + i * 3600, r['model'], pricing.tier_class(r['tier']), inp, cached, out))
    tokens += [(t0 - 1, 999, 999, 999), (None, 999, 999, 999)]
    splits += [(t0 - 1, 'old', 'fast', 999, 999, 999), (None, 'untimed', 'fast', 999, 999, 999)]
    tokens.reverse()
    splits.reverse()
    dollars = [(t, 1.0) for t, *_ in tokens]
    old = analyze.rate_limit_windows(files, tokens, now=F4_NOW.timestamp(), newest=None, usd=dollars)
    new = analyze.rate_limit_windows(files, tokens, now=F4_NOW.timestamp(), newest=None,
                                     usd=dollars, split=splits)
    a, b = new['windows']
    first = [dict(F4_SPLIT[0], responses=2, input=200, cached=40, output=20), F4_SPLIT[1]]
    second = [dict(F4_SPLIT[0], responses=2, input=800, cached=0, output=80), *F4_SPLIT[2:]]
    check('split exact-boundary row belongs to the new window',
          a['split'] == first and b['split'] == second, str([a['split'], b['split']]))
    check('reversed timelines, pre-window and untimed rows preserve both window totals',
          [tuple(w['tokens'][k] for k in COUNT_KEYS[:-1]) for w in (a, b)]
          == [(4, 600, 290, 60), (4, 1400, 200, 140)])
    stripped = copy.deepcopy(new)
    for w in [*stripped['windows'], stripped['current']]:
        if w:
            w.pop('split', None)
    check('split=None preserves every old analyzer field, token curve and dollar attribution',
          stripped == old and all('split' not in w for w in old['windows']))
    sent = share.limit_windows(files, tokens, F4_NOW.timestamp(), split=splits)
    check('sharing keeps the two complete boundary splits',
          [w['split'] for w in sent] == [first, second])


def _split_fixture(models_by_window):
    files = {'a': {'session_id': 'splits', 'date': '2026-09-10', 'rate_limits': []}}
    rows = []
    for i, models in enumerate(models_by_window):
        start = F4_START + i * 4 * 3600
        files['a']['rate_limits'].append(_quote(start))
        for j, model in enumerate(models):
            rows.append({'ts': share._iso(start + j + 1), 'model': model, 'tier': 'default',
                         'usage': {'input_tokens': 10 + j, 'cached_input_tokens': j,
                                   'output_tokens': 2, 'reasoning_output_tokens': 1}})
    p, notes = share.build_payload(files, {'a': rows}, now=F4_NOW, prices=(None, 'disabled'))
    timeline = [(worker.epoch(r['ts']), r['usage']['input_tokens'], r['usage']['cached_input_tokens'],
                 r['usage']['output_tokens']) for r in rows]
    old = share.limit_windows(files, timeline, F4_NOW.timestamp())
    return p, notes, old


def test_split_limits():
    fifty = [f'm{j:02d}' for j in range(50)]
    for n in (50, 51):
        p, notes, old = _split_fixture([[f'm{j:02d}' for j in range(n)]])
        w = p['windows'][0]
        if n == 50:
            check('50 distinct pairs retain a complete window split', len(w.get('split', [])) == 50)
        else:
            check('51-row split is absent and original totals are retained',
                  'split' not in w and w == old[0] and notes['window_split_overflow'] == 1, str(w))
        check(f'{n} pairs: split limits preserve windows, percentages and ordinary counts',
              [{k: v for k, v in w.items() if k != 'split'} for w in p['windows']] == old
              and sum(w['responses'] for w in p['windows']) == sum(d['responses'] for d in p['days']))
        if n == 51:
            check('per-window overflow has its exact dry-run note',
                  'note: 1 weekly windows had over 50 model/tier rows (split not sent)'
                  in share.describe(p, notes, None, share.DEFAULT_API))
    for n in (20, 21):
        p, notes, old = _split_fixture([fifty] * n)
        windows = p['windows']
        check(f'{n} windows: newest twenty complete splits fit the 1,000-row budget',
              sum(len(w.get('split', [])) for w in windows) == 1000
              and all(len(w.get('split', [])) == 50 for w in windows[-20:])
              and ('split' in windows[0]) == (n == 20)
              and notes['window_split_budget'] == n - 20, str(notes))
        check(f'{n} windows: budget omission preserves all selected windows and ordinary fields',
              [{k: v for k, v in w.items() if k != 'split'} for w in windows] == old
              and sum(w['responses'] for w in windows) == sum(d['responses'] for d in p['days']))
        if n == 21:
            check('total split overflow has its exact dry-run note',
                  'note: 1 older weekly windows were over the 1,000 split-row budget (split not sent)'
                  in share.describe(p, notes, None, share.DEFAULT_API))
    p, notes, _ = _split_fixture([fifty[:49] + ['x' * 80 + 'a', 'x' * 80 + 'b']])
    split = p['windows'][0].get('split', [])
    check('model truncation collisions coalesce before counting the 50-row bound',
          len(split) == 50 and next((r['responses'] for r in split if r['model'] == 'x' * 80), 0) == 2
          and notes['window_split_overflow'] == 0)
    p, _, _ = _split_fixture([['', None]])
    check('empty and absent models become one unknown split key',
          p['windows'][0]['split'] == [dict(model='unknown', tier='standard', responses=2,
                                          input=21, cached=1, output=4)])
    p, notes, old = _split_fixture([fifty + ['overflow']] + [['m']] * 104)
    check('newest-104 selection happens before split bounds and overflow notes',
          len(p['windows']) == 104 and notes['window_split_overflow'] == 0
          and all(len(w['split']) == 1 for w in p['windows'])
          and [{k: v for k, v in w.items() if k != 'split'} for w in p['windows']] == old)
    p, notes, _ = _split_fixture([fifty + ['overflow']] + [fifty] * 20)
    check('per-window omission happens before total-budget counting',
          notes['window_split_overflow'] == 1 and notes['window_split_budget'] == 0
          and sum(len(w.get('split', [])) for w in p['windows']) == 1000)


def test_latency_plan_context():
    from tokencounter import account
    files, charged = _f4()
    with _fixture_utc(), mock.patch.object(account, 'read', return_value={'plan': 'wrong'}) as lookup:
        p, _ = share.build_payload(files, charged, now=F4_NOW, prices=(None, 'disabled'))
        check('F4 timing plan context is prolite', p['latency']['plan'] == 'prolite')
        t0, t1 = F4_START, F4_START + 3600
        windows = [{'start': share._iso(t1), 'window_minutes': 10080, 'plan': 'pro'},
                   {'start': share._iso(t0), 'window_minutes': 10080, 'plan': 'prolite'}]
        base = {'model': 'm', 'effort': 'high', 'usage': {'input_tokens': 10, 'output_tokens': 1}}
        rows = [dict(base, req_ts=share._iso(t0 + 10), ts=share._iso(t0 + 20), tier='default'),
                dict(base, req_ts=share._iso(t1 - 2), ts=share._iso(t1), tier='priority')]
        lat = share.latency_summary({'a': {}}, {'a': rows}, F4_NOW.timestamp(), windows=windows)
        check('two-plan latency context is null', lat['plan'] is None, str(lat))
        check('two-plan timing groups and mixed 6/9.2 summary are retained',
              lat['responses'] == {'n': 2, 'median_s': 6, 'p90_s': 9.2}
              and lat['groups'][0]['n'] == 2 and sum(g['n'] for g in lat['tier_groups']) == 2)
        intervals = share._latency_plan_windows(windows)
        check('plan intervals are sorted and clipped at the next stored start',
              intervals == [(t0, t1, 'prolite'), (t1, t1 + WEEK, 'pro')])
        lat = share.latency_summary({'a': {}}, {'a': rows[1:]}, F4_NOW.timestamp(), windows=windows)
        check('completion exactly at a new interval start uses the new plan', lat['plan'] == 'pro')
        check('a completion at an interval stop without a successor has no plan',
              latency._plan_at(t1 + WEEK, intervals) is None)
        for label, ws in [('missing plan', [dict(windows[0], plan=None), windows[1]]),
                          ('no interval', []), ('omitted intervals', None),
                          ('outside interval', [dict(windows[1], window_minutes=1)])]:
            lat = share.latency_summary({'a': {}}, {'a': rows}, F4_NOW.timestamp(), windows=ws)
            check(f'{label}: the whole timing summary has null plan context', lat['plan'] is None)
        gap = share._latency_plan_windows([dict(windows[1], window_minutes=30), windows[0]])
        check('a quoted duration ends an interval before the next start, leaving the gap unknown',
              gap[0] == (t0, t0 + 1800, 'prolite') and latency._plan_at(t0 + 2000, gap) is None)
        excluded = [dict(base, ts=share._iso(t1 + 1), req_ts=None),
                    dict(base, ts=share._iso(t1 + 2), req_ts=share._iso(t1), replayed=True),
                    dict(base, ts=share._iso(t1 + 4000), req_ts=share._iso(t1 + 10))]
        lat = share.latency_summary({'a': {}, 'b': {}}, {'a': rows[:1], 'b': excluded},
                                    F4_NOW.timestamp(), windows=windows)
        check('untimed, replayed and over-cap responses do not change timing plan context',
              lat['plan'] == 'prolite' and lat['responses']['n'] == 1
              and sum(g['n'] for g in lat['tier_groups']) == 1, str(lat))
        check('timing plan context never looks up account information', lookup.call_count == 0)


def test_tier_group_limits():
    files = {'a': {}}
    rows = []
    # One mixed key has all three tiers; 50 other mixed keys give 51 mixed / 53 tier rows.
    for i in range(53):
        model = 'm00' if i < 3 else f'm{i - 2:02d}'
        req = F4_START + i * 120
        rows.append(dict(model=model, effort='high', tier=pricing.TIER_CLASSES[i % 3],
                         req_ts=share._iso(req), ts=share._iso(req + i + 1), usage={}))
    full, _ = latency.build(files, {'a': rows}, tier_groups=True)
    wire = share.latency_summary(files, {'a': rows}, F4_NOW.timestamp())
    check('mixed and tier arrays have independent 50-row caps in total-time order',
          len(full['groups']) == 51 and len(full['tier_groups']) == 53
          and wire['groups'] == [share._wire_group(g) for g in full['groups'][:50]]
          and wire['tier_groups'] == [share._wire_group(g) for g in full['tier_groups'][:50]])
    long_rows = [dict(rows[i], model='x' * 80 + suffix, effort='e' * 40 + suffix, tier='default')
                 for i, suffix in enumerate(('a', 'b'))]
    long, _ = latency.build(files, {'a': long_rows}, tier_groups=True)
    check('tier keys bound model and effort before grouping collisions',
          len(long['tier_groups']) == 1 and long['tier_groups'][0]['n'] == 2
          and long['tier_groups'][0]['model'] == 'x' * 80
          and long['tier_groups'][0]['effort'] == 'e' * 40)


# ---------------------------------------------------------------------------- transport

class Stub(http.server.BaseHTTPRequestHandler):
    calls = []
    token = 'tu1.' + 'A' * 20 + '.' + 'b' * 43

    def log_message(self, *a):
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n))
        auth = self.headers.get('Authorization')
        Stub.calls.append(('POST', self.path, auth, body))
        if auth and auth != f'Bearer {Stub.token}':
            return self._send(401, {'error': 'bad_token', 'message': 'not recognised'})
        if not auth:
            return self._send(201, {'handle': body['handle'], 'url': 'http://x/u/' + body['handle'],
                                    'token': Stub.token, 'created': True})
        return self._send(200, {'handle': body.get('handle') or 'tester',
                                'url': 'http://x/u/tester', 'created': False})

    def do_PUT(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n))
        auth = self.headers.get('Authorization')
        Stub.calls.append(('PUT', self.path, auth, body))
        if auth != f'Bearer {Stub.token}':
            return self._send(401, {'error': 'bad_token', 'message': 'not recognised'})
        page = gzip.decompress(base64.b64decode(body['html_gz']))
        self._send(200, {'handle': 'tester', 'url': 'http://x/r/tester', 'bytes': len(page)})

    def do_DELETE(self):
        Stub.calls.append(('DELETE', self.path, self.headers.get('Authorization'), None))
        self._send(200, {'deleted': True, 'handle': 'tester'})


@contextlib.contextmanager
def _server():
    srv = http.server.HTTPServer(('127.0.0.1', 0), Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    Stub.calls.clear()
    try:
        yield f'http://127.0.0.1:{srv.server_address[1]}/api'
    finally:
        srv.shutdown()


def _last(method):
    return next((c for c in reversed(Stub.calls) if c[0] == method), None)


def _page(call):
    return gzip.decompress(base64.b64decode(call[3]['html_gz'])).decode('utf-8')


def _run(root, home, *argv):
    import report as cli
    keep = os.environ.get('CODEX_HOME')
    os.environ['CODEX_HOME'] = home
    cli._OUT_DIR.clear()
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = share.main(['--sessions-root', root, '--procs', '1', '--quiet', *argv])
    finally:
        cli._OUT_DIR.clear()
        if keep is None:
            os.environ.pop('CODEX_HOME', None)
        else:
            os.environ['CODEX_HOME'] = keep
    return rc, out.getvalue(), err.getvalue()


def test_transport():
    _utc()
    root = _corpus(CORPUS)
    home = tempfile.mkdtemp()
    state = os.path.join(home, 'token-counter', 'share.json')
    with _server() as api:
        rc, out, _ = _run(root, home, '--api', api)
        check('a dry run sends nothing', rc == 0 and not Stub.calls and 'dry run' in out, out)
        check('a dry run says what would be shared', '2026-09' in out and 'never sent' in out, out)
        check('a dry run says what it sends about rate limits', 'limit window' in out, out)
        shared = os.path.join(home, 'token-counter', 'report-shared.html')
        check('a dry run writes the report page it would publish, and names it',
              os.path.exists(shared) and shared in out and 'anyone with the link' in out, out)
        with open(shared, encoding='utf-8') as fh:
            dry_page = fh.read()
        check('the shared page is the token report',
              dry_page.startswith('<!doctype html>') and 'Codex Token Report' in dry_page
              and 'data-style="clinical"' in dry_page)
        leaked = [x for x in (SECRET_PROMPT, SECRET_CWD, os.path.basename(SECRET_CWD),
                              *(sid for sid, _, _ in CORPUS), *(sid[:8] for sid, _, _ in CORPUS))
                  if x in dry_page]
        check('the shared page carries no prompt, directory or session id', not leaked, str(leaked))

        rc, _, err = _run(root, home, '--api', api, '--yes')
        check('a first share without a handle is refused locally',
              rc == 2 and not Stub.calls and '--handle' in err, err)

        rc, out, err = _run(root, home, '--api', api, '--handle', 'Tester', '--yes')
        check('a first share prints the profile URL, then the report URL last',
              rc == 0 and 'http://x/u/tester' in out
              and out.strip().endswith('report: http://x/r/tester'), out + err)
        check('the handle is sent lowercased', _last('POST')[3].get('handle') == 'tester')
        check('the first share sends no token', _last('POST')[2] is None)
        put = _last('PUT')
        check('the report page goes up after the numbers, under the new token',
              Stub.calls[-1] is put and put[1] == '/api/report'
              and put[2] == f'Bearer {Stub.token}' and put[3]['schema'] == 1)
        # Bytes, not text: the report writes the page in text mode, so on Windows the file
        # holds CRLF and is sent as it is, and a text-mode read would turn it back into LF.
        with open(shared, 'rb') as fh:
            check('the page sent is the page the dry run showed',
                  _page(put) == fh.read().decode('utf-8'))
        with open(state, encoding='utf-8') as fh:
            saved = json.load(fh)['endpoints'][api]
        check('the token is stored', saved.get('token') == Stub.token and saved['handle'] == 'tester')
        if os.name == 'posix':
            check('the token file is private', stat.S_IMODE(os.stat(state).st_mode) == 0o600,
                  oct(os.stat(state).st_mode))
        check('the token is never printed', Stub.token not in out + err)

        rc, out, _ = _run(root, home, '--api', api, '--yes')
        check('a later share authenticates and leaves the handle out',
              rc == 0 and _last('POST')[2] == f'Bearer {Stub.token}'
              and 'handle' not in _last('POST')[3], str(_last('POST')[:3]))

        n = len(Stub.calls)
        rc, out, _ = _run(root, home, '--api', api, '--yes', '--no-report')
        check('--no-report shares the numbers only',
              rc == 0 and [c[0] for c in Stub.calls[n:]] == ['POST'], str(Stub.calls[n:]))

        rc, out, _ = _run(root, home, '--api', api, '--yes', '--style', 'matisse')
        # Marked as chosen, so it opens in it over a style a reader remembered elsewhere.
        check('--style is the style the shared page opens in',
              rc == 0 and '<html lang="en" data-style="matisse" data-style-set>'
              in _page(_last('PUT'))
              and 'data-next="nocturne"' in _page(_last('PUT')), out)

        n = len(Stub.calls)
        rc, out, _ = _run(root, home, '--api', api, '--delete-report')
        check('--delete-report without --yes only says what it would do',
              rc == 0 and len(Stub.calls) == n and 'would take down' in out, out)
        rc, out, _ = _run(root, home, '--api', api, '--delete-report', '--yes')
        with open(state, encoding='utf-8') as fh:
            kept = json.load(fh)['endpoints'].get(api) or {}
        check('--delete-report takes the page down and keeps the token',
              rc == 0 and Stub.calls[-1][:2] == ('DELETE', '/api/report')
              and kept.get('token') == Stub.token and 'report_url' not in kept, out)

        n = len(Stub.calls)
        rc, out, _ = _run(root, home, '--api', api, '--delete')
        check('delete without --yes only says what it would do',
              rc == 0 and len(Stub.calls) == n and 'would delete' in out, out)
        rc, out, _ = _run(root, home, '--api', api, '--delete', '--yes')
        with open(state, encoding='utf-8') as fh:
            left = json.load(fh)['endpoints']
        check('delete calls the server and forgets the token',
              rc == 0 and Stub.calls[-1][:2] == ('DELETE', '/api/share') and api not in left,
              str(left))

        # A rejected token is reported, with the way out.
        with open(state, 'w', encoding='utf-8') as fh:
            json.dump({'endpoints': {api: {'token': 'tu1.' + 'Z' * 20 + '.' + 'z' * 43,
                                           'handle': 'tester'}}}, fh)
        rc, out, err = _run(root, home, '--api', api, '--yes')
        check('a rejected token explains --forget', rc == 7 and '--forget' in err, err)

        out_path = os.path.join(home, 'p.json')
        n = len(Stub.calls)
        rc, out, _ = _run(root, home, '--api', api, '--out', out_path)
        with open(out_path, encoding='utf-8') as fh:
            written = json.load(fh)
        check('--out writes the payload and sends nothing',
              rc == 0 and len(Stub.calls) == n and written['schema'] == 1, out)

    rc, _, err = _run(root, home, '--api', 'http://127.0.0.1:9/api', '--handle', 'x-y-z', '--yes',
                      '--forget')
    check('--forget works offline', rc == 0)
    rc, _, err = _run(root, home, '--api', 'http://127.0.0.1:9/api', '--handle', 'x-y-z', '--yes')
    check('an unreachable server is reported, not raised', rc == 6 and 'could not reach' in err, err)


def test_latency_floor_across_cutoff():
    """The first response of the month sent is still floored by the end of the one before
    it, though that one is older than the month and is not sent itself."""
    now_s = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc).timestamp()
    cut = now_s - share.LATENCY_DAYS * 86400
    iso = lambda x: (datetime.datetime.fromtimestamp(x, datetime.timezone.utc)
                     .isoformat().replace('+00:00', 'Z'))
    base = {'model': 'm', 'effort': 'e', 'turn': 0, 'usage': {'output_tokens': 1}}
    rows = [dict(base, req_ts=iso(cut - 100), ts=iso(cut - 5)),
            dict(base, req_ts=iso(cut - 50), ts=iso(cut + 20))]   # no input since: 25 s
    lat = share.latency_summary({'a': {'turn_starts': []}}, {'a': rows}, now_s)
    r = (lat or {}).get('responses') or {}
    check('a response just inside the month is timed from the previous response\'s end',
          r.get('n') == 1 and r.get('median_s') == 25.0, str(lat))
    check('the tier group keeps the same previous-response floor across the cutoff',
          lat['tier_groups'][0]['n'] == 1 and lat['tier_groups'][0]['median_s'] == 25.0)


def test_state_write_ignores_a_planted_temp_file():
    """The share token goes through a new file of its own: a symlink left at a fixed
    temporary name is never followed."""
    home = tempfile.mkdtemp()
    keep, keep_out = os.environ.get('CODEX_HOME'), list(share.reportcli._OUT_DIR)
    os.environ['CODEX_HOME'] = home
    share.reportcli._OUT_DIR.clear()
    try:
        p = share.state_path()
        victim = os.path.join(tempfile.mkdtemp(), 'victim')
        open(victim, 'w').close()
        try:
            os.symlink(victim, p + '.tmp')
        except (OSError, NotImplementedError):
            return                              # no symlinks here; nothing to plant
        share.save_state({'endpoints': {'x': {'token': 'SECRET-TOKEN'}}})
        with open(victim, encoding='utf-8') as fh:
            leaked = fh.read()
        got = share.load_state()
        mode = stat.S_IMODE(os.stat(p).st_mode)
    finally:
        share.reportcli._OUT_DIR[:] = keep_out
        if keep is None:
            os.environ.pop('CODEX_HOME', None)
        else:
            os.environ['CODEX_HOME'] = keep
    check('the share token is never written through a file planted at a fixed name',
          leaked == '' and got['endpoints']['x']['token'] == 'SECRET-TOKEN'
          and (mode == 0o600 or os.name == 'nt'), f'{leaked!r} {oct(mode)}')


def main():
    test_payload_shape()
    test_payload_agrees_with_the_report()
    test_nothing_private_is_sent()
    test_sessions_are_capped_per_month()
    test_damaged_counts_are_clamped()
    test_limit_windows()
    test_latency()
    test_api_value()
    test_transport()
    test_latency_floor_across_cutoff()
    test_tier_partitions()
    test_tier_window_boundaries()
    test_split_limits()
    test_latency_plan_context()
    test_tier_group_limits()
    test_state_write_ignores_a_planted_temp_file()
    bad = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f'\n{len(RESULTS) - bad}/{len(RESULTS)} passed')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
