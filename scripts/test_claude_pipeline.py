"""Claude timing/events and local analyzer acceptance, using invented fixtures only."""
import copy
import contextlib
import datetime
import importlib.util
import io
import json
from pathlib import Path
import sys
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('claude_pipeline_cases', str(HERE / 'test_claude_pricing.py'))
fixtures = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixtures
spec.loader.exec_module(fixtures)
analyze, cases, corpus = fixtures.analyze, fixtures.cases, fixtures.corpus
ledger, worker, pricing = fixtures.ledger, fixtures.worker, fixtures.pricing
from claude_counter import account, history, index, paths, render, rollout


def _report_module():
    aliases = {name.replace('claude_counter', 'tokencounter', 1): module
               for name, module in list(sys.modules.items())
               if name == 'claude_counter' or name.startswith('claude_counter.')}
    saved = list(sys.path)
    try:
        with mock.patch.dict(sys.modules, aliases):
            return fixtures._load('claude_report_cli', fixtures.LIB.parent / 'report.py')
    finally:
        sys.path[:] = saved


report = _report_module()
NOW = datetime.datetime(2026, 9, 11, tzinfo=datetime.timezone.utc).timestamp()
RESULTS = []


def expect(name, got, expected):
    RESULTS.append((name, got == expected, 'expected %r, got %r' % (expected, got)))


def _ts(seconds):
    return (datetime.datetime(2026, 9, 10, tzinfo=datetime.timezone.utc)
            + datetime.timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def _lat(case):
    with corpus(case) as (result, _files, _root):
        report = analyze.analyze(result, now=_ts(100))
        return report['latency'], report['quality']


def _durations(lat):
    return sorted((g['model'], g['n'], g['total_s']) for g in lat['groups'])


def test_analyzer_B():
    with corpus(fixtures._b()) as (result, _files, _root):
        # B: input=(10+20+30+40)+(5+0+100+50)=255; reads40+50=90;
        # output8+10=18 includes thinking3+4=7. Response intervals10,8 ->median9,p90=9.8.
        # Turn0..20 includes18 model seconds ->.9; logged19000ms stays19; tool10..12=2.
        model = analyze.analyze(result, scope={'root': 'invented-local-path'}, now=_ts(100))
        expect('B captured totals', tuple(model['totals'][k] for k in ('responses', 'input', 'cached', 'output', 'reasoning')),
               (2, 255, 90, 18, 7))
        expect('B disjoint prompt components', (model['totals']['base_input'], model['totals']['cache_creation'],
                                               model['totals']['cached'], model['totals']['total_tokens']), (15, 150, 90, 273))
        expect('B byte inventory', {c['category']: c['bytes'] for c in model['categories'] if c['bytes']},
               {'user_message': 4, 'tool_call_input': 9, 'tool_output': 3, 'assistant_message': 2})
        expect('eleven categories display order', [c['label'] for c in model['categories']], list(fixtures.composition.LABELS.values()))
        expect('B hourly composition', (model['cat_bucket_s'], sum(sum(c.values()) for _t, c in model['cat_series'])), (3600, 18))
        response = model['latency']['responses']
        expect('B response timing', (response['n'], response['median_s'], response['p90_s']), (2, 9, 9.8))
        expect('B computed turn', (model['latency']['turns']['total_s'], model['latency']['turns']['model_share']), (20, .9))
        expect('B logged turn', (model['logged_turns']['n'], model['logged_turns']['total_s']), (1, 19))
        expect('B tool timing', (model['latency']['tools'][0]['n'], model['latency']['tools'][0]['total_s']), (1, 2))
        # R1(548 microdollars+.01 search)+R2(1030 microdollars)=.011578.
        expect('B analyzer list value', (model['api_value']['usd'], model['api_value']['usd_high']), (.011578, .011578))
        for key in ('reconciliation', 'resend_cost', 'amplification', 'cache_leads'):
            expect('unsupported ' + key, model[key], {'available': False, 'reason': 'Claude transcripts do not establish per-content token attribution.'})
        expect('coverage passed through by identity', model['coverage'] is result['coverage'], True)
        expect('B has no quota readings or unavailable counter',
               (model['rate_limits']['windows'], 'windows_unavailable' in model['quality']), ([], False))
        expect('B one family one stream', (len(model['sessions']), model['sessions'][0]['threads'], model['sessions'][0]['active_s']), (1, 1, 10))
        day = model['daily'][0]
        expect('captured daily chart spans', (day['start'], day['end'], day['sessions'], day['input']),
               (result['rows'][0]['day_start'], result['rows'][0]['day_end'], 1, 255))
        model['account'] = {'email': 'invented-private-email', 'organization_name': 'invented-private-company',
                            'snapshot': {'current_plan': 'claude:max-5x'}}
        model['sessions'][0]['cwd'] = 'invented-private-path'
        model['sessions'][0]['files'] = ['invented-private-path']
        model['latency']['groups'][0]['requestId'] = 'invented-private-request'
        model['_share_latency']['groups'][0]['requestId'] = 'invented-private-request'
        public = analyze.public_model(model)
        encoded = json.dumps(public)
        sentinels = ['invented-private', 'invented-local-path', 'synthetic_tool',
                     result['rows'][0]['source_id'], result['rows'][0]['response_key'],
                     result['rows'][0]['family_id'], result['rows'][0]['stream_id']]
        expect('public model strips identity and local fields', any(s in encoded for s in sentinels), False)
        expect('public omits images and private timing adapter', ('images' in public, '_share_latency' in public), (False, False))
        expect('no UTC calendar arrays', any(k in encoded for k in ('utc_hours', 'utc_weekdays')), False)


def test_analyzer_W():
    with corpus(cases.quota_W_timed()) as (result, _files, _root):
        result['account_snapshots'] = [cases.account_snapshot('2026-09-10T21:00:00Z',
                                                           subscription_created_at='2026-09-10T16:00:00Z')]
        model = analyze.analyze(result, now='2026-09-18T00:00:00Z')
        limits, window = model['rate_limits'], model['rate_limits']['windows'][0]
        legacy = ('index', 'reset_at', 'reset_at_iso', 'resets_at', 'resets_at_iso', 'peak_pct',
                  'last_pct', 'tokens', 'pct_points', 'cum_points', 'usd', 'usd_points', 'late_points')
        expect('W legacy window contract', all(k in window for k in legacy), True)
        top = ('available', 'window_minutes', 'weekly', 'windows', 'windows_total', 'current',
               'observations', 'quotes', 'idle_windows', 'overlapping', 'late_readings',
               'boundary_without_drop', 'other_windows', 'plans', 'now')
        expect('W Codex top level rate limit contract', all(k in limits for k in top), True)
        nominal, reset = worker.epoch('2026-09-10T16:00:00Z'), worker.epoch('2026-09-17T16:00:00Z')
        first, peak = worker.epoch('2026-09-10T18:00:00Z'), worker.epoch('2026-09-10T19:00:00Z')
        #Median reset00.470 rounds00; nominal=reset-604800; all late points0.
        expect('W nominal inferred anchor and expiry',
               (window['reset_at'], window['reset_at_iso'], window['resets_at'], window['resets_at_iso'],
                window['reset_inferred'], window['anchor_inferred'], window['late_points'], window['expired']),
               (nominal, analyze._iso(nominal), reset, analyze._iso(reset), True, True, 0, True))
        #First20 at18:00; first peak25 at19:00; plateau25 at20:00 retains only a sparse point.
        expect('W observation chart points', (window['first_pct'], window['peak_pct'], window['last_pct'],
               window['observation_start'], window['observation_end'], window['pct_points']),
               (20, 25, 25, first, peak, [[first, 20], [peak, 25], [peak + 3600, 25]]))
        #Nominal100+200+300+400+500=1500; reads40+80+120+160+200=600;output150.
        expect('W chart nominal hand counts', window['tokens'],
               dict(responses=5, input=1500, cached=600, output=150, reasoning=0, uncached=900))
        #Observation300+400=700;reads120+160=280;output30+40=70;5m10+10=20;1h20+20=40.
        expect('W share observation hand counts', (window['counts'], window['cache_write_5m'], window['cache_write_1h']),
               (dict(responses=2, input=700, cached=280, output=70, reasoning=0), 20, 40))
        #Cumulative nominal input+output:110;110+220=330;+330=660;+440=1100;+550=1650.
        expect('W nominal cumulative hand values', [p[1] + p[3] for p in window['cum_points']], [110, 330, 660, 1100, 1650])
        #Opus5.5 row values448/896/1434/1882/2240 microdollars sum6900, no search fee.
        expect('W nominal default price hand values', (window['usd'], [p[1] for p in window['usd_points']]),
               (.0069, [.000448, .001344, .002778, .00466, .0069]))
        expect('W latency captured account plan', (model['latency']['plan'], model['latency']['plan_source']), ('claude:max-5x', 'account'))
        public = analyze.public_model(model)
        safe = public['rate_limits']['windows'][0]
        expect('public W preserves wire fields and chart aggregates',
               set(('start', 'window_minutes', 'plan', 'first_pct', 'peak_pct', 'responses', 'input', 'cached', 'output', 'reasoning', 'split')).issubset(safe)
               and safe['cum_points'] == window['cum_points'] and safe['usd_points'] == window['usd_points']
               and safe['anchor_inferred'], True)
        expect('public W reading provenance is allow-listed',
               all(set(r) <= {'ts', 'percent', 'source'} for r in safe['readings']), True)
        encoded = json.dumps(public)
        sentinels = [window['window_key']] + [r['reading_key'] for r in result['limits']] + [r['response_key'] for r in result['rows']]
        expect('public W strips reading and response identities', any(s in encoded for s in sentinels), False)
        #Subscription17:30 permits quota start18:00, but is after earliest accepted latency end17:00.
        result['account_snapshots'] = [cases.account_snapshot('2026-09-10T21:00:00Z',
                                                           subscription_created_at='2026-09-10T17:30:00Z')]
        model = analyze.analyze(result, now='2026-09-10T22:00:00Z')
        expect('W latency applies earliest sample subscription guard',
               (model['rate_limits']['windows'][0]['plan'], model['latency']['plan'], model['latency']['plan_source']),
               ('claude:max-5x', None, None))


def test_frozen_stream_anchor():
    case = fixtures._b()
    records = fixtures._records(case)
    arriving = cases._user('mid-stream-result', _ts(7), [{'type': 'tool_result', 'tool_use_id': 'early', 'content': ''}], 'B-R1-0')
    records[2]['parentUuid'] = arriving['uuid']
    records.insert(2, arriving)
    lat, _q = _lat(case)
    # R1 froze0 at first block5, despite input arriving7; terminal10 ->10 seconds.
    expect('stream anchor frozen at first block', (lat['responses']['n'], lat['responses']['total_s']), (2, 18))


def test_previous_end_floor():
    case = fixtures._b()
    fixtures._records(case)[3]['timestamp'] = _ts(7)
    lat, _q = _lat(case)
    # R2 candidate7 floors at previous end10; terminal20 ->10 seconds.
    expect('previous response end floors start', lat['responses']['total_s'], 20)


def test_floor_without_anchor():
    records = [fixtures._r1(), copy.deepcopy(fixtures._records(fixtures._b())[4])]
    records[1]['parentUuid'] = records[0]['uuid']
    lat, q = _lat(fixtures._case(records))
    expect('floor never invents missing start', (lat['responses']['n'], q.get('latency_no_start')), (0, 2))


def test_compaction_floor():
    case = fixtures._b()
    records = fixtures._records(case)
    summary = cases._user('summary', _ts(18), 'saved summary', 'B-U1')
    summary['isCompactSummary'] = True
    records[4]['parentUuid'] = summary['uuid']
    records.insert(4, summary)
    records.insert(4, {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'boundary',
                       'timestamp': _ts(17), 'compactMetadata': {'durationMs': 1000}})
    lat, _q = _lat(case)
    # R1=10; summary floor18 ->R2=2; no extra charged compaction.
    expect('compaction summary floors response start', (lat['responses']['n'], lat['responses']['total_s']), (2, 12))


def _child_case(*, prompt=True, streaming_input=False, main_end=1):
    user = cases._user('parent-U', _ts(0), 'task')
    spawn = cases.assistant_record('parent-M', 'parent-request', _ts(main_end), cases._usage(10, 0, 0, 0, 1),
                                   uuid='spawn', parent_uuid=user['uuid'])
    fork = {'type': 'fork-context-ref', 'parentSessionId': 'A', 'agentId': 'child', 'parentLastUuid': 'spawn'}
    copied_user, copied_spawn = copy.deepcopy(user), copy.deepcopy(spawn)
    copied_user['timestamp'] = copied_spawn['timestamp'] = _ts(2)
    records = [fork, copied_user, copied_spawn]
    parent = 'spawn'
    if prompt:
        task = cases._user('child-task', _ts(2), 'own task', parent)
        records.append(task)
        parent = task['uuid']
    r0 = cases.assistant_record('child-M', 'child-request', _ts(4), cases._usage(20, 0, 0, 0, 1),
                               uuid='child-0', parent_uuid=parent)
    r1 = cases.assistant_record('child-M', 'child-request', _ts(6), cases._usage(20, 0, 0, 0, 2),
                               uuid='child-1', parent_uuid='child-0', block_index=1)
    records.append(r0)
    if streaming_input:
        extra = cases._user('child-later', _ts(5), 'later input', 'child-0')
        records.append(extra)
        r1['parentUuid'] = extra['uuid']
    records.append(r1)
    case = fixtures._case([user, spawn])
    case['files']['synthetic-project/A/subagents/agent-child.jsonl'] = records
    return case


def test_parallel_streams():
    lat, _q = _lat(_child_case(main_end=10))
    # Main0..10 and child2..6 sum14 transcript seconds, in separate streams.
    expect('parallel streams have independent floors', (lat['responses']['n'], lat['responses']['total_s']), (2, 14))


def test_subagent_own_prompt_anchor():
    with corpus(_child_case()) as (result, _files, _root):
        child = next(r for r in result['rows'] if r['usage']['input_tokens'] == 20)
        expect('child own prompt anchors first response', worker.epoch(child['ts']) - worker.epoch(child['req_ts']), 4)


def test_subagent_anchor_first_block_freeze():
    with corpus(_child_case(streaming_input=True)) as (result, _files, _root):
        child = next(r for r in result['rows'] if r['usage']['input_tokens'] == 20)
        # First block4 freezes2; input5 cannot move it; terminal6 ->4 seconds.
        expect('child anchor remains frozen', worker.epoch(child['ts']) - worker.epoch(child['req_ts']), 4)


def test_subagent_missing_prompt_no_parent_fallback():
    lat, q = _lat(_child_case(prompt=False))
    expect('missing child prompt has no parent fallback', (lat['responses']['n'], q.get('latency_subagent_prompt_missing')), (1, 1))


def test_tool_parallel_not_wall_sum():
    record = cases.assistant_record('tools-M', 'tools-request', _ts(5), cases._usage(10, 0, 0, 0, 1),
                                   uuid='tools-0', parent_uuid='U', content=[{'type': 'tool_use', 'id': 'a', 'name': 'a', 'input': {}}])
    next_block = copy.deepcopy(record)
    next_block.update(uuid='tools-1', parentUuid='tools-0', apiBlockIndex=1, timestamp=_ts(6))
    next_block['message']['content'] = [{'type': 'tool_use', 'id': 'b', 'name': 'b', 'input': {}}]
    records = [cases._user('U', _ts(0), 'task'), record, next_block,
               cases._user('result-a', _ts(9), [{'type': 'tool_result', 'tool_use_id': 'a', 'content': ''}], 'tools-1'),
               cases._user('result-b', _ts(10), [{'type': 'tool_result', 'tool_use_id': 'b', 'content': ''}], 'result-a')]
    lat, _q = _lat(fixtures._case(records))
    expect('parallel tools retain each interval', sorted((t['n'], t['total_s']) for t in lat['tools']), [(1, 4), (1, 4)])
    expect('no family tool wall-time claim', 'wall_s' in lat, False)


def test_logged_turn_separate():
    with corpus(fixtures._b()) as (result, _files, _root):
        model = analyze.analyze(result, now=_ts(100))
        expect('logged turn never replaces computed turn', (model['latency']['turns']['total_s'], model['logged_turns']['total_s']), (20, 19))


def _direct(rows, tools=(), turns=()):
    with corpus(fixtures._b()) as (result, _files, _root):
        result['rows'] = rows
        result['tools'], result['turns'] = list(tools), list(turns)
        return result


def _timed_row(index, seconds, effort='high', start=0):
    row = fixtures._row()
    row.update(response_key='invented-%d' % index, stream_id='stream-%d' % index,
               req_ts=_ts(start), ts=_ts(start + seconds), effort=effort, turn=0, index=0)
    return row


def test_caps():
    rows = [_timed_row(0, 3600), _timed_row(1, 3600.001), _timed_row(2, 0), _timed_row(3, -1)]
    tools = [{'tool_key': 'tool%d' % i, 'stream_id': 'stream-0', 'name': 'synthetic',
              'ts': _ts(0), 'end_ts': _ts(d), 'seconds': d} for i, d in enumerate((7200, 7200.001, 0, -1))]
    # Two response intervals each3600 fit individually while their genuine turn may cap.
    rows += [_timed_row(4, 3600, start=3600), _timed_row(5, 3600, start=3600.001)]
    turns = [{'stream_id': r['stream_id'], 'turn': 0, 'start': _ts(0), 'logged_duration_ms': None,
              'logged_record_key': None, 'record_key': 'turn'+r['stream_id']} for r in rows]
    lat, q = analyze._timing(_direct(rows, tools, turns), rows)
    expect('response cap inclusive', (lat['responses']['n'], q.get('latency_over_cap'), q.get('latency_nonpositive')), (3, 1, 2))
    expect('tool cap inclusive', (lat['tools'][0]['n'], lat['tools'][0]['total_s'], q.get('tool_over_cap'), q.get('tool_nonpositive')), (1, 7200, 1, 2))
    expect('turn cap inclusive', (lat['turns']['n'], lat['turns']['total_s'], q.get('turn_over_cap')), (3, 14400.001, 1))
    logged = _direct(rows, turns=[dict(turns[0], logged_duration_ms=7200000), dict(turns[1], logged_duration_ms=7200001)])
    counters = {}
    summary = analyze._logged_turns(logged, rows, counters)
    expect('logged turn cap inclusive', (summary['n'], summary['total_s'], counters.get('logged_turn_over_cap')), (1, 7200, 1))


def _fit_rows(n, effort='high'):
    rows = []
    for i in range(n):
        # Independent varied columns; duration=1 +output/10 +uncached/1000.
        out, unc = i + 1, ((i * 13) % 47) * 100 + 100
        row = _timed_row(i, 1 + out / 10 + unc / 1000, effort)
        row['usage'].update(input_tokens=unc, cached_input_tokens=0, output_tokens=out,
                            total_tokens=unc + out, reasoning_output_tokens=0)
        rows.append(row)
    return rows


def test_fit_minimum():
    for n, expected in ((39, False), (40, True)):
        rows = _fit_rows(n)
        lat, _q = analyze._timing(_direct(rows), rows)
        expect('fit minimum%d' % n, lat['groups'][0]['fit'] is not None, expected)
    # The unchanged fitter evenly caps at4000 without relabelling the sample group.
    from claude_counter import latency
    expect('even sample maximum', (len(latency._evenly(list(range(4100)), 4000)), latency.FLOOR_Q), (4000, .1))


def test_effort_not_relabelled():
    for effort in ('xhigh', 'max'):
        rows = _fit_rows(40, effort)
        lat, _q = analyze._timing(_direct(rows), rows)
        expect('actual effort preserved ' + effort, (lat['groups'][0]['effort'], lat['tier_groups'][0]['effort']), (effort, effort))


def test_replayed_timing_excluded():
    rows = [_timed_row(0, 10), _timed_row(1, 10), _timed_row(2, 10)]
    rows[1]['timestamp_quality'] = 'unknown'
    rows[2]['partial'] = True
    lat, q = analyze._timing(_direct(rows), rows)
    expect('partial and unverified timing excluded', (lat['responses']['n'], q.get('latency_copy_timestamp_unverified'),
                                                      q.get('latency_partial_response')), (1, 1, 1))
    case = fixtures._b()
    case['files']['synthetic-project/C.jsonl'] = copy.deepcopy(fixtures._records(case))
    lat, _q = _lat(case)
    expect('copied responses produce no timing duplicates', lat['responses']['n'], 2)


def test_unknown_speed_mixed_only():
    row = fixtures._row(lambda r: r['message']['usage'].pop('speed'))
    lat, q = analyze._timing(_direct([row]), [row])
    expect('unknown speed mixed only', (lat['groups'][0]['n'], lat['tier_groups'], q.get('latency_unknown_speed')), (1, [], 1))


def _error(status=429):
    record = fixtures._r1()
    record.update(uuid='refusal', isApiErrorMessage=True, apiErrorStatus=status,
                  requestId='error-request',
                  quotaLimits={'status': 'rejected', 'rateLimitType': 'seven_day', 'resetsAt': _ts(7 * 86400)})
    record['message'].update(id='error-message', model='<synthetic>')
    return record


def test_429_daily_dedup():
    error = _error()
    case = fixtures._case([error, copy.deepcopy(error)])
    case['files']['synthetic-project/C.jsonl'] = [copy.deepcopy(error)]
    with corpus(case) as (result, _files, _root):
        model = analyze.analyze(result, now=_ts(100))
        expect('429 copies one daily event and full reading', (model['limit_events']['total'], len(result['limits']),
                                                              model['limit_events']['daily'][0]['n']), (1, 1, 1))


def test_429_not_usage():
    with corpus(fixtures._case([_error()])) as (result, _files, _root):
        model = analyze.analyze(result, now=_ts(100))
        expect('429 synthetic positive usage never charged', (model['totals']['responses'], model['totals']['input'], model['api_value']['usd']), (0, 0, 0))


def test_server_error_not_rate_event():
    with corpus(fixtures._case([_error(500)])) as (result, _files, _root):
        expect('500 is not a rate event', (result['events'], result['limits']), ([], []))


def test_filters_daily_history_and_group_limits():
    with corpus(fixtures._b()) as (result, _files, _root):
        family = next(iter(result['families']))
        model = analyze.analyze(result, session=family, metrics_only=True, now=_ts(100))
        expect('metrics only preserves recorded totals', (model['totals']['input'], model['totals']['bytes']), (255, 0))
        expect('since until local dates inclusive', analyze.analyze(result, since=result['rows'][0]['local_day'],
                                                                   until=result['rows'][0]['local_day'], now=_ts(100))['totals']['responses'], 2)
        expect('filter absent session', analyze.analyze(result, session='absent', now=_ts(100))['totals']['responses'], 0)
        frozen = copy.deepcopy(result)
        for row in frozen['rows']:
            row.update(local_day='2026-08-31', day_start=1, day_end=2, archived=True)
        for family_summary in frozen['families'].values():
            family_summary['local_day'] = '2026-08-31'
        report = analyze.analyze(frozen, now=_ts(100))
        expect('retained calendar owns usage buckets', [(d['date'], d['start'], d['end']) for d in report['daily']], [('2026-08-31', 1, 2)])
        expect('retained calendar owns latency buckets', [(d['date'], d['start'], d['end']) for d in report['latency']['daily']], [('2026-08-31', 1, 2)])
        expect('live only excludes archived responses', analyze.analyze(frozen, live_only=True, now=_ts(100))['totals']['responses'], 0)
        public = analyze.public_model(analyze.analyze(result, now=_ts(31 * 86400)))
        expect('public latency uses last thirty days', public['latency']['responses']['n'], 0)
    rows = [_timed_row(i, i + 1) for i in range(51)]
    for i, row in enumerate(rows):
        row['model'] = 'claude-test-%d' % i
    lat, q = analyze._timing(_direct(rows), rows)
    expect('mixed and tier group arrays independently capped', (len(lat['groups']), len(lat['tier_groups']),
                                                                q.get('latency_groups_omitted'), q.get('latency_tier_groups_omitted')), (50, 50, 1, 1))
    expect('largest response time groups retained first', (lat['groups'][0]['total_s'], lat['groups'][-1]['total_s']), (51, 2))


def test_prompt_growth_diagnostics():
    case = fixtures._b()
    fixtures._records(case)[4]['message']['usage'] = cases._usage(5, 0, 0, 0, 1)
    with corpus(case) as (result, files, _root):
        expect('negative prompt growth counted once', (files[0]['counters'].get('prompt_growth_negative'), result['counters'].get('prompt_growth_negative')), (1, 1))
    fixtures._records(case)[4]['message']['model'] = 'claude-sonnet-5-5'
    with corpus(case) as (result, _files, _root):
        expect('model change skips prompt growth', (result['counters'].get('prompt_growth_model_change_skipped'), result['counters'].get('prompt_growth_negative', 0)), (1, 0))
    fixtures._records(case).insert(4, {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'compact', 'timestamp': _ts(18)})
    with corpus(case) as (result, _files, _root):
        expect('compaction skips prompt growth', (result['counters'].get('prompt_growth_compaction_skipped'), result['counters'].get('prompt_growth_negative', 0)), (1, 0))
    # A completed summary alone also establishes a context segment, in both modes.
    case = fixtures._b()
    records = fixtures._records(case)
    records[4]['message']['usage'] = cases._usage(5, 0, 0, 0, 1)
    summary = cases._user('growth-summary', _ts(18), 'invented summary', 'B-U1')
    summary['isCompactSummary'] = True
    records[4]['parentUuid'] = summary['uuid']
    records.insert(4, summary)
    for metrics in (False, True):
        with corpus(case, metrics_only=metrics) as (result, files, root):
            expect('summary-only growth segment mode%d' % metrics,
                   (files[0]['counters'].get('prompt_growth_compaction_skipped'),
                    result['counters'].get('prompt_growth_compaction_skipped'),
                    result['counters'].get('prompt_growth_negative', 0)), (1, 1, 0))
            with fixtures.history.History(root / 'growth.db') as store:
                store.capture(files)
                store.commit()
                archived = ledger.build([], history=store.load())
                expect('summary-only growth segment survives capture mode%d' % metrics,
                       (archived['counters'].get('prompt_growth_compaction_skipped'),
                        archived['counters'].get('prompt_growth_negative', 0)), (1, 0))


def test_live_only_inventory_and_events():
    case = fixtures._b()
    fixtures._records(case).append(_error())
    with corpus(case) as (_plain, files, root), fixtures.history.History(root / 'history.db') as store:
        store.capture(files)
        store.commit()
        shortened = fixtures._b()
        shortened['files'] = {next(iter(case['files'])): fixtures._records(shortened)[:4]}
        # U0, R1 blocks and tool result remain; R2, logged duration and429 are pruned.
        paths = cases.write_corpus(root / 'projects', shortened)
        live = [worker.extract(path) for path in paths]
        view = ledger.build(live, history=store.load())
        model = analyze.analyze(view, live_only=True, now=_ts(100))
        expect('live only excludes pruned inventory events and log',
               (model['totals']['input'], model['totals']['bytes'], model['limit_events']['total'], model['logged_turns']['n']), (100, 16, 0, 0))
        retained = analyze.analyze(view, now=_ts(100))
        expect('default retains inventory event and log',
               (retained['totals']['input'], retained['totals']['bytes'], retained['limit_events']['total'], retained['logged_turns']['n']), (255, 18, 1, 1))
        paths[0].unlink()
        archived = ledger.build([], history=store.load())
        model = analyze.analyze(archived, live_only=True, now=_ts(100))
        expect('history-only live view empty', (model['totals']['input'], model['totals']['bytes'], model['limit_events']['total']), (0, 0, 0))
    with corpus(fixtures._case([_error()])) as (result, _files, _root):
        model = analyze.analyze(result, session='A', now=_ts(100))
        expect('session filter keeps events without charged responses', model['limit_events']['total'], 1)


def test_first_family_day_survives_filter():
    with corpus(fixtures._b()) as (result, _files, _root):
        # A continuation's first captured charge is yesterday; selecting today does
        # not count it as another newly started session.
        row = result['rows'][1]
        row.update(local_day='2026-09-11', day_start=2, day_end=3)
        report = analyze.analyze(result, since='2026-09-11', now=_ts(100))
        expect('daily sessions use first captured family day', (report['daily'][0]['input'], report['daily'][0]['sessions']), (155, 0))


def _account_file(root):
    (root / '.claude.json').write_text(json.dumps({'oauthAccount': {
        'organizationType': 'claude_max', 'organizationRateLimitTier': 'default_claude_max_5x',
        'subscriptionCreatedAt': '2026-09-01T00:00:00Z', 'emailAddress': 'invented-identity<&>@example.invalid',
        'organizationName': 'invented-organization'}}), encoding='utf-8')


def _cli(root, *extra):
    stdout, stderr = io.StringIO(), io.StringIO()
    args = ['--sessions-root', str(root / 'projects'), '--procs', '1', '--no-open', '--quiet',
            '--out', str(root / 'page.html'), '--json', str(root / 'model.json')] + list(extra)
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
            mock.patch.object(report.time, 'time', return_value=NOW), \
            mock.patch.object(report, '_open', side_effect=AssertionError('browser attempted')):
        try:
            code = report.main(args)
        except SystemExit as exc:
            code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


def test_report_B_and_W():
    for label, case in (('B', fixtures._b()), ('W', cases.quota_W_timed())):
        with corpus(case) as (_built, _files, root):
            _account_file(root)
            code, stdout, stderr = _cli(root)
            model = json.loads((root / 'model.json').read_text(encoding='utf-8'))
            page = (root / 'page.html').read_text(encoding='utf-8')
            expect('CLI fixture outputs ' + label, (code, model['totals']['responses'] > 0,
                   'recorded input' in stdout, 'Claude Code Token Report' in page, stderr), (0, True, True, True, ''))
            expect('CLI escaped local account ' + label,
                   'invented-identity&lt;&amp;&gt;@example.invalid' in page and 'invented-identity<&>' in stdout, True)
            expect('CLI complete thinking note ' + label, 'recorded thinking tokens · unavailable' in page, False)
            expect('CLI postcommit history coverage ' + label,
                   (model['coverage']['history_available'], model['coverage']['history_committed'],
                    model['coverage']['safe_months']), (True, True, ['2026-09']))
            code, stdout, _stderr = _cli(root, '--json', '-')
            expect('CLI JSON stdout only ' + label, (code, json.loads(stdout)['totals'], stdout.rstrip().endswith('}')), (0, model['totals'], True))
            code, stdout, _stderr = _cli(root, '--public')
            public = (root / 'model.json').read_text(encoding='utf-8')
            page = (root / 'page.html').read_text(encoding='utf-8')
            expect('CLI public identity excluded ' + label, (code, any(s in public+page+stdout for s in
                   ('invented-identity', 'invented-organization', str(root)))), (0, False))
            code, _stdout, _stderr = _cli(root, '--metrics-only')
            minimal = json.loads((root / 'model.json').read_text(encoding='utf-8'))
            expect('CLI metrics-only usage ' + label,
                   (code, minimal['totals']['input'], minimal['totals']['bytes'], minimal['api_value']['usd']),
                   (0, model['totals']['input'], 0, model['api_value']['usd']))


def test_report_index_rebuild_and_warm():
    with corpus(fixtures._b()) as (_built, _files, root):
        with mock.patch.object(worker, 'extract', wraps=worker.extract) as extract:
            expect('CLI rebuild success', _cli(root, '--rebuild')[0], 0)
            expect('CLI rebuild fully extracts', extract.call_count, 1)
            cold = json.loads((root / 'model.json').read_text(encoding='utf-8'))
            extract.reset_mock()
            expect('CLI warm success', _cli(root)[0], 0)
            warm = json.loads((root / 'model.json').read_text(encoding='utf-8'))
            expect('CLI warm reuses index without extraction', extract.call_count, 0)
            expect('CLI warm complete JSON equality', warm, cold)
            extract.reset_mock()
            _cli(root, '--no-cache')
            expect('CLI no-cache extracts', extract.call_count, 1)


def test_report_prefix_and_options():
    case = fixtures._b()
    case['files'] = {name.replace('synthetic-session-B', 'synthetic-session-B-extended'): records
                     for name, records in case['files'].items()}
    for records in case['files'].values():
        for record in records:
            record['sessionId'] = 'synthetic-session-B-extended'
    case['files'].update(cases.quota_W_timed()['files'])
    with corpus(case) as (_built, _files, root):
        expect('CLI unique family prefix accepted', _cli(root, '--session', 'synthetic-session-B')[0], 0)
        code, _stdout, stderr = _cli(root, '--session', 'synthetic-session-')
        expect('CLI ambiguous prefix names candidate families',
               (code, 'synthetic-session-B' in stderr, 'synthetic-session-W' in stderr), (2, True, True))
        expect('CLI include-archived live-only conflict', _cli(root, '--live-only', '--include-archived')[0], 2)
        expect('CLI fast procs conflict', _cli(root, '--fast')[0], 2)
        options = report.parse_args(['--live-only', '--no-open'])
        expect('CLI live-only default alias accepted', (options.live_only, options.include_archived), (True, False))
        with mock.patch.object(report.os, 'cpu_count', return_value=8):
            expect('CLI worker defaults', (report.parse_args([]).procs, report.parse_args(['--fast']).procs), (4, 8))
    with corpus(fixtures._case([_error()])) as (_built, _files, root):
        expect('CLI refusal-only family prefix accepted', _cli(root, '--session', 'A')[0], 0)
        model = json.loads((root / 'model.json').read_text())
        expect('CLI refusal-only family keeps its event', (model['totals']['responses'], model['limit_events']['total']), (0, 1))


def test_report_retention_and_no_account():
    with corpus(fixtures._b()) as (_built, _files, root):
        _account_file(root)
        _cli(root, '--no-account')
        resolved = paths.resolve_paths(root / 'projects')
        with history.History(resolved['history_path']) as store:
            expect('CLI no-account captures no snapshot', store.load()[3], [])
        for file in (root / 'projects').rglob('*.jsonl'):
            file.unlink()
        code, stdout, stderr = _cli(root, '--no-account')
        model = json.loads((root / 'model.json').read_text(encoding='utf-8'))
        expect('CLI pruned file durable coverage', (code, model['totals']['input'], stderr), (0, 255, ''))
        expect('CLI retained coverage terminal line', 'captured history: 2 responses from transcripts no longer on disk' in stdout, True)
        _cli(root, '--live-only', '--no-account')
        expect('CLI live-only excludes archive', json.loads((root / 'model.json').read_text())['totals']['responses'], 0)


def test_report_history_failure_and_doctor():
    with corpus(fixtures._b()) as (_built, _files, root):
        resolved = paths.resolve_paths(root / 'projects')
        resolved['state_dir'].mkdir(parents=True)
        resolved['history_path'].write_bytes(b'invented-corrupt-history')
        before = resolved['history_path'].read_bytes()
        code, _stdout, stderr = _cli(root)
        model = json.loads((root / 'model.json').read_text())
        expect('CLI history failure degrades without reset',
               (code, model['totals']['input'], model['scope']['live_only'], resolved['history_path'].read_bytes()), (0, 255, True, before))
        expect('CLI quiet retains history warning and counter',
               (report.HISTORY_WARNING in stderr, model['quality'].get('history_unavailable')), (True, 1))
        _account_file(root)
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
        code, stdout, _stderr = _cli(root, '--doctor')
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
        expect('CLI doctor unavailable is read-only', (code, after == before), (1, True))
        expect('CLI doctor no account identity', 'invented-identity' in stdout or 'invented-organization' in stdout, False)
    with corpus(fixtures._b()) as (_built, _files, root):
        expect('CLI doctor healthy corpus without state', _cli(root, '--doctor')[0], 0)
        expect('CLI doctor creates no state', paths.resolve_paths(root / 'projects')['state_dir'].exists(), False)
        expect('CLI version exits zero', _cli(root, '--version')[0], 0)
        expect('CLI version creates no state', paths.resolve_paths(root / 'projects')['state_dir'].exists(), False)


def test_analysis_prices_each_row_once():
    with corpus(fixtures._b()) as (built, _files, _root):
        with mock.patch.object(pricing, 'price_row', wraps=pricing.price_row) as quote:
            analyze.analyze(built, now=NOW)
            expect('one price_row per row per analysis', quote.call_count, len(built['rows']))


def page_fixtures(destination):
    """Invented CLI pages for the Node counterpart; no live data or browser."""
    root, case, timed = Path(destination), fixtures._b(), cases.quota_W_timed()
    for record in next(iter(timed['files'].values())):
        if record['type'] == 'assistant':
            record['message']['usage'].pop('output_tokens_details', None)
    next(iter(timed['files'].values())).append(cases.quota_429('2026-09-10T20:30:00Z'))
    case['files'].update(timed['files'])
    cases.write_corpus(root / 'projects', case)
    _account_file(root)
    with fixtures.offline():
        for public in (False, True):
            for style, _label in render.STYLES:
                suffix = style + ('-public' if public else '-local')
                code, _stdout, _stderr = _cli(root, '--style', style,
                    '--out', str(root / (suffix + '.html')), '--json', str(root / (suffix + '.json')),
                    *(['--public'] if public else []))
                if code:
                    raise AssertionError('page fixture CLI failed')
        _cli(root, '--metrics-only', '--out', str(root / 'metrics.html'))
        _cli(root, '--since', '2026-10-01', '--out', str(root / 'empty.html'))
        _cli(root, '--session', 'synthetic-session-B', '--out', str(root / 'complete.html'))
        with mock.patch.dict(globals(), NOW=NOW+8*86400):
            _cli(root, '--out', str(root / 'expired.html'))
        scoped = copy.deepcopy(cases.quota_W_timed())
        for record in next(iter(scoped['files'].values())):
            if 'usageReport' in record:
                record['usageReport']['rate_limits']['limits'][0]['kind'] = 'session'
        scoped_root = root / 'scoped'
        cases.write_corpus(scoped_root / 'projects', scoped)
        _cli(scoped_root, '--out', str(root / 'weekly-missing.html'))


def main():
    RESULTS.clear()
    tests = sorted(n for n in globals() if n.startswith('test_'))
    with fixtures.offline():
        for name in tests:
            try:
                globals()[name]()
            except Exception as exc:
                RESULTS.append((name + ' invalid execution', False, type(exc).__name__))
    for name, ok, detail in RESULTS:
        print('[%s] %s%s' % ('PASS' if ok else 'FAIL', name, '' if ok else ': ' + detail))
    passed = sum(ok for _name, ok, _detail in RESULTS)
    print('\n%d/%d assertions passed (%d tests; network blocked)' % (passed, len(RESULTS), len(tests)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
