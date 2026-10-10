"""Claude timing/events and local analyzer acceptance, using invented fixtures only."""
import copy
import datetime
import importlib.util
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('claude_pipeline_cases', str(HERE / 'test_claude_pricing.py'))
fixtures = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixtures
spec.loader.exec_module(fixtures)
analyze, cases, corpus = fixtures.analyze, fixtures.cases, fixtures.corpus
ledger, worker, pricing = fixtures.ledger, fixtures.worker, fixtures.pricing
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
        expect('round five windows unavailable', (model['rate_limits']['windows'], model['quality']['windows_unavailable']), ([], 1))
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
