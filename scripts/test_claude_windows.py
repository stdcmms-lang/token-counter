"""Round-6 sparse quota and captured-account decisions, with invented W only."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('claude_windows_fixtures', str(HERE / 'test_claude_pricing.py'))
fixtures = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixtures
spec.loader.exec_module(fixtures)
from claude_counter import account, windows  # noqa: E402

cases, corpus, offline = fixtures.cases, fixtures.corpus, fixtures.offline
worker, ledger, history, analyze = fixtures.worker, fixtures.ledger, fixtures.history, fixtures.analyze
RESULTS = []
RESET = worker.epoch('2026-09-17T16:00:00Z')
START = worker.epoch('2026-09-10T18:00:00Z')
END = worker.epoch('2026-09-10T19:00:00Z')
SAFE = {'windows_replace_safe': True, 'window_reasons': []}


def expect(name, got, expected):
    RESULTS.append((name, got == expected, 'expected %r, got %r' % (expected, got)))


def _records(case):
    return next(iter(case['files'].values()))


def _assistant(case, index=3):
    return [r for r in _records(case) if r['type'] == 'assistant'][index - 1]


def _reports(case):
    return [r for r in _records(case) if 'usageReport' in r]


def _limit(record):
    return record['usageReport']['rate_limits']['limits'][0]


def _quote(offset=0, percent=20, ts='2026-09-10T18:00:00Z', kind='weekly_all', scope=None, key=None):
    return dict(reading_key=key or str((offset, percent, ts, kind, scope)), source_id='invented-source',
                kind=kind, scope_key=scope, resets_at=RESET + offset, percent=percent, ts=ts, source='usage_report')


def _anchor(offset=0, phase=None):
    reset = RESET + offset
    key = hashlib.sha256(json.dumps(['weekly_all', None, reset], separators=(',', ':')).encode()).hexdigest()
    return dict(window_key=key, kind='weekly_all', scope_key=None, reset_at=reset,
                min_reset=reset, max_reset=reset, grid_phase=RESET if phase is None else phase)


def _build(case=None, snapshots=(), edit=None, established=()):
    with corpus(case or cases.quota_W()) as (result, _files, _root):
        if edit:
            edit(result)
        built, counters = windows.build_windows(result['limits'], result['rows'],
            account_snapshots=snapshots, established=established)
        return built, counters, result


def _w(case=None, snapshots=(), edit=None, established=()):
    built, q, result = _build(case, snapshots, edit, established)
    return built[0], q, result


def _snapshot(time='21:00', tier='default_claude_max_5x', created='17:00', organization='claude_max'):
    return cases.account_snapshot('2026-09-10T' + time + ':00Z', organization_type=organization,
        rate_limit_tier=tier, subscription_created_at='2026-09-10T' + created + ':00Z' if created else None)


def _decision(snapshots, start=START, end=END):
    return windows.plan_for_interval(start, end, snapshots)


def test_reset_jitter_cluster():
    w, q, _ = _w()
    # Median of59.600/00.470/00.470 is00.470; nearest integer is16:00:00Z.
    expect('jitter canonical reset', (w['reset_at'], len(w['readings'])), (RESET, 3))
    expect('jitter has no grid departure', q.get('limit_reset_grid_departures', 0), 0)


def test_cluster_not_neighbor_chained():
    clusters, _ = windows.cluster_resets([_quote(3), _quote(0), _quote(1.5)])
    # 0..1.5 spans1.5<=2; adding3 spans3>2 despite neighbor gap1.5.
    expect('whole cluster span not neighbor distance', [len(c['readings']) for c in clusters], [2, 1])


def test_cluster_tolerance_inclusive():
    clusters, _ = windows.cluster_resets([_quote(0), _quote(2)])
    # Whole span exactly2 seconds is accepted in a single cluster.
    expect('two second tolerance inclusive', len(clusters), 1)


def test_canonical_reset_rounding():
    clusters, _ = windows.cluster_resets([_quote(0), _quote(1)])
    # Median(0,1)=.5; tie rounds upward to1, never to the even integer0.
    expect('canonical median tie upward', clusters[0]['reset_at'], RESET + 1)


def test_established_anchor_stable():
    clusters, _ = windows.cluster_resets([_quote(.8), _quote(1.1)], established=[_anchor()])
    # New median.95 would round to1; captured anchor0 and its phase remain0.
    expect('captured anchor and phase stable', (clusters[0]['reset_at'], clusters[0]['grid_phase']), (RESET, RESET))


def test_ambiguous_cluster_withheld():
    readings = [_quote(0), _quote(1.5, 22, '2026-09-10T18:30:00Z'),
                _quote(3, 25, '2026-09-10T19:00:00Z')]
    with corpus(cases.quota_W()) as (result, _files, _root):
        built, q = windows.build_windows(readings, result['rows'], established=[_anchor(), _anchor(3)])
    # 1.5 fits both captured0 and3: two anchors remain, both flagged; ambiguous quote counted once.
    expect('ambiguous established anchors never merged',
           (len(built), q.get('limit_reset_cluster_ambiguous'), all('limit_reset_cluster_ambiguous' in w['withheld_reasons'] for w in built)),
           (2, 1, True))


def test_reset_grid_diagnostic():
    for offset, departures in ((windows.WEEK_S, 0), (windows.WEEK_S + 4, 1)):
        readings = [_quote(offset, 20), _quote(offset + .2, 25, '2026-09-10T19:00:00Z')]
        with corpus(cases.quota_W()) as (result, _files, _root):
            built, q = windows.build_windows(readings, result['rows'], established=[_anchor()])
        # +7d has distance0; +7d+4s has distance4>2, once for two readings, no snapping/withholding.
        expect('grid diagnostics only offset%d' % offset,
               (q.get('limit_reset_grid_departures', 0), built[0]['reset_at'], built[0]['shareable']),
               (departures, RESET + offset, True))
    # An arbitrary account phase shifted123s is valid; there is no universal weekday/hour.
    c, q = windows.cluster_resets([_quote(123), _quote(123 + windows.WEEK_S)])
    expect('grid phase learned from account', ([x['grid_phase'] for x in c], q.get('limit_reset_grid_departures', 0)),
           ([RESET + 123, RESET + 123], 0))


def test_first_to_first_peak_span():
    w, _q, _ = _w()
    # Earliest20 at18:00; first25 at19:00, despite plateau at20:00.
    expect('first to earliest peak', (w['first_pct'], w['peak_pct'], w['last_pct'], w['observation_start'], w['observation_end']),
           (20, 25, 25, START, END))


def test_boundary_open_closed():
    w, _q, result = _w()
    # 18:00 input200 is excluded;18:30 input300 and19:00 input400 are included ->700/280/70.
    expect('observation boundaries open closed', w['counts'], dict(responses=2, input=700, cached=280, output=70, reasoning=0))
    expect('span contributing response keys', w['rows'], [r['response_key'] for r in result['rows'][2:4]])


def test_plateau_does_not_extend_span():
    w, _q, _ = _w()
    # Later25 at20:00 never adds the19:30 input500 row: numerator stays700.
    expect('plateau leaves first peak endpoint', (w['observation_end'], w['counts']['input']), (END, 700))
    case = cases.quota_W()
    _records(case).remove(_reports(case)[1])
    w, _q, _ = _w(case)
    # Removing first peak19:00 moves endpoint20:00:300+400+500=1200; reads120+160+200=480.
    expect('removed first peak advances span', (w['observation_end'], w['counts']),
           (END + 3600, dict(responses=3, input=1200, cached=480, output=120, reasoning=0)))


def test_single_reading_withheld():
    case = cases.quota_W()
    for report in _reports(case)[1:]:
        _records(case).remove(report)
    w, q, _ = _w(case)
    # Only20 at18:00: one observation, no positive span, no comparison.
    expect('single reading withheld', (w['shareable'], 'window_single_reading' in w['withheld_reasons'], q.get('window_single_reading')), (False, True, 1))


def test_zero_delta_withheld():
    case = cases.quota_W()
    for report in _reports(case):
        _limit(report)['percent'] = 20
    w, q, _ = _w(case)
    # Three sourced times all20: delta20-20=0, despite multiple observations.
    expect('zero delta withheld', (w['shareable'], 'window_zero_delta' in w['withheld_reasons'], q.get('window_zero_delta')), (False, True, 1))


def test_percent_drop_withheld():
    case = cases.quota_W()
    _records(case).append(cases._reading('19:15', 24, '2026-09-17T16:00:00Z', None))
    w, q, _ = _w(case)
    #25 at19:00 ->24 at19:15 is a decline; later25 at20:00 cannot repair it.
    expect('percent decrease withheld', (w['shareable'], 'limit_percent_decreased' in w['withheld_reasons'], q.get('limit_percent_decreased')), (False, True, 1))


def test_same_stamp_conflict():
    case = cases.quota_W()
    report = copy.deepcopy(_reports(case)[0])
    report['uuid'] = 'W-conflicting-quote'
    _limit(report)['percent'] = 21
    _records(case).append(report)
    w, q, _ = _w(case)
    #20 and21 at the same18:00 conflict, counted once for the cluster.
    expect('same timestamp conflict withheld', (w['shareable'], 'limit_quote_conflict' in w['withheld_reasons'], q.get('limit_quote_conflict')), (False, True, 1))


def test_429_is_full_reading():
    case = cases.quota_W()
    for report in _reports(case)[1:]:
        _records(case).remove(report)
    _records(case).append(cases.quota_429())
    w, _q, result = _w(case)
    #20 at18:00 ->100 at19:30 includes300+400+500=1200, reads480, output120; error itself adds0.
    expect('weekly refusal sourced full reading', (w['first_pct'], w['peak_pct'], w['observation_end'], w['counts']),
           (20, 100, END + 1800, dict(responses=3, input=1200, cached=480, output=120, reasoning=0)))
    expect('weekly refusal event and assumption once', (len(result['events']), result['counters'].get('limit_429_assumed_all_models'),
           sum(r['source'] == 'quota_429' for r in w['readings']), w['shareable']), (1, 1, 1, True))


def test_429_only_single_reading():
    case = cases.quota_W()
    case['files'][next(iter(case['files']))] = [cases.quota_429()]
    w, q, result = _w(case)
    #One sourced100 refusal has first=peak=100 and no captured responses: single-reading refusal.
    expect('429 only is single reading', (w['peak_pct'], w['shareable'], q.get('window_single_reading'), len(result['events'])), (100, False, 1, 1))


def test_429_without_weekly_rejection_has_no_percent():
    for kwargs in ({'kind': 'five_hour'}, {'status': 'allowed'}, {'reset': None}, {'kind': 'seven_day_sonnet'}):
        case = cases.quota_W()
        case['files'][next(iter(case['files']))] = [cases.quota_429(**kwargs)]
        built, _q, result = _build(case)
        #A nonweekly/nonrejected/invalid-reset429 still supplies one event, zero full readings.
        expect('nonweekly or nonrejected refusal has no percent ' + str(kwargs),
               (len(built), len(result['limits']), len(result['events'])), (0, 0, 1))


def test_weekly_all_only():
    case = cases.quota_W()
    for report in _reports(case):
        _limit(report)['kind'] = 'session'
    built, _q, _ = _build(case)
    entries, _notes = windows.wire_windows(built, SAFE)
    #Known five-hour observations remain local; none becomes a weekly comparison.
    expect('wire only weekly all', (len(built), entries), (1, []))


def test_scoped_not_aliased_to_all():
    case = cases.quota_W()
    for report in _reports(case):
        limit = _limit(report)
        limit.update(kind='weekly_scoped', scope={'model': 'claude-sonnet-5-5'})
    built, _q, _ = _build(case)
    #A scoped weekly series is distinct and never supplies the all-model numerator.
    expect('scoped never aliases all model', ([w['kind'] for w in built], windows.wire_windows(built, SAFE)[0]), (['weekly_scoped'], []))
    readings = [_quote(scope='scope-a', kind='weekly_scoped'), _quote(scope='scope-b', kind='weekly_scoped')]
    clusters, _ = windows.cluster_resets(readings)
    expect('scoped identities remain separate', len(clusters), 2)  #Two scopes with identical resets ->two clusters.


def test_five_hour_not_weekly():
    case = cases.quota_W()
    for report in _reports(case):
        _limit(report).update(kind='session', resets_at='2026-09-10T21:00:00Z')
    w, _q, _ = _w(case)
    #21:00 minus5h=16:00; never subtract7d or share a session window.
    expect('session nominal five hours', (w['reset_at'] - w['nominal_start'], w['shareable']), (18000, False))


def test_unknown_plan_null():
    w, q, _ = _w()
    #No captured account witness: null plan/source, one unknown window, still shareable.
    expect('unknown plan does not withhold', (w['plan'], w['plan_source'], q.get('window_unknown_plan'), w['shareable']), (None, None, 1, True))


def test_plan_from_current_snapshot():
    plan, source, q = _decision([_snapshot()])
    #P5 captured21:00 is considered after19:00; subscription17:00<=18:00.
    expect('current captured snapshot attributes plan', (plan, source, q), ('claude:max-5x', 'account', {}))


def test_plan_null_before_subscription():
    plan, source, q = _decision([_snapshot(created='20:00')])
    #Subscription20:00>observation start18:00: condition4 fails once.
    expect('subscription start guard', (plan, source, q.get('window_unknown_plan')), (None, None, 1))


def test_plan_conflict_in_span():
    plan, _source, q = _decision([_snapshot('18:15'), _snapshot('18:45', 'default_claude_max_20x')])
    #Two applicable known plansP5/P20 disagree; one attribution, one conflict.
    expect('in span plan conflict once', (plan, q.get('account_plan_conflict'), q.get('window_unknown_plan')), (None, 1, 1))


def test_plan_changed_after_span():
    plan, _source, q = _decision([_snapshot('17:00', 'default_claude_max_20x'), _snapshot('20:00')])
    #Latest predecessorP20 and later(after end)P5 disagree; both conditions2/3 apply.
    expect('after span tier change invalidates plan', (plan, q.get('account_plan_conflict')), (None, 1))


def test_latency_plan_same_rule():
    scenarios = [([_snapshot()], 'claude:max-5x', 0),
                 ([_snapshot(created='17:30')], None, 0),
                 ([_snapshot('18:15'), _snapshot('18:45', 'default_claude_max_20x')], None, 1),
                 ([_snapshot('17:00', 'default_claude_max_20x', '16:00'), _snapshot('20:00', created='16:00')], None, 1),
                 ([_snapshot('18:15', organization='claude_team'), _snapshot('20:00')], None, 0),
                 ([_snapshot(created=None)], None, 0),
                 ([_snapshot(organization='claude_team')], None, 0),
                 ([_snapshot(tier='future-max')], None, 0)]
    for i, (snapshots, expected, conflicts) in enumerate(scenarios):
        with corpus(cases.quota_W_timed()) as (result, _files, _root):
            result['account_snapshots'] = snapshots
            lat, q = analyze._timing(result, result['rows'])
        #Five10-second samples end17:00..19:30. Same four rules, creation guard at17:00, not18:00.
        expect('latency captured snapshot rule%d' % i,
               (lat['plan'], lat['plan_source'], q.get('account_plan_conflict', 0), lat['responses']['n']),
               (expected, 'account' if expected else None, conflicts, 5))


def test_snapshot_null_blocks_plan():
    plan, _source, q = _decision([_snapshot('18:15', organization='claude_team'), _snapshot('20:00')])
    #Null at18:15 blocks condition2; null/P5 is not a conflict between two known plans.
    expect('null snapshot blocks without known disagreement', (plan, q.get('account_plan_conflict', 0), q.get('window_unknown_plan')), (None, 0, 1))


def test_subscription_date_missing_null():
    plan, _source, q = _decision([_snapshot(created=None)])
    #Latest P5 witness has no parsed creation date: null, date-missing1, unknown1.
    expect('missing witness subscription date', (plan, q.get('account_subscription_date_missing'), q.get('window_unknown_plan')), (None, 1, 1))


def test_team_max_tier_not_personal_max():
    plan, _source, q = _decision([_snapshot(organization='claude_team')])
    #Team organization with the known5x literal still maps null, not a personalMax plan.
    expect('team tier never personal Max', (plan, q.get('account_plan_unknown'), q.get('window_unknown_plan')), (None, 1, 1))


def test_unknown_max_tier_null():
    plan, _source, q = _decision([_snapshot(tier='future-max')])
    #Unknown nonempty Max tier maps null; unrecognized1, unknown plan1.
    expect('unknown Max tier diagnostic', (plan, q.get('account_tier_unrecognized'), q.get('window_unknown_plan')), (None, 1, 1))


def test_plan_latest_predecessor_only():
    plan, source, q = _decision([_snapshot('16:00', 'default_claude_max_20x', '15:00'),
                                _snapshot('17:00', created='15:00'), _snapshot('20:00', created='15:00')])
    #Old16:00P20 is superseded by latest predecessor17:00P5; applicableP5/P5 agrees.
    expect('only latest predecessor vetoes', (plan, source, q.get('account_plan_conflict', 0)), ('claude:max-5x', 'account', 0))


def test_plan_predecessor_null_blocks():
    plan, _source, q = _decision([_snapshot('17:00', organization='claude_team'), _snapshot('20:00')])
    #Latest predecessor null blocks condition3 even though every post-start snapshot isP5.
    expect('null latest predecessor blocks', (plan, q.get('window_unknown_plan')), (None, 1))


def test_plan_predecessor_only_witness():
    plan, source, q = _decision([_snapshot('17:00')])
    #One P5 snapshot before start is still a witness; condition2's empty set is consistent.
    expect('predecessor only witness accepted', (plan, source, q), ('claude:max-5x', 'account', {}))


def test_plan_latest_witness_date():
    plan, _source, q = _decision([_snapshot('18:15'), _snapshot('21:00', created=None)])
    #Older valid date cannot substitute for latest21:00 witness's missing date.
    expect('latest witness owns creation guard', (plan, q.get('account_subscription_date_missing')), (None, 1))


def test_plan_at_start_inclusive():
    plan, _source, q = _decision([_snapshot('18:00', organization='claude_team'), _snapshot('21:00')])
    #Null exactly at18:00 is post-start evidence (>=), rather than an ignorable older snapshot.
    expect('snapshot at start participates', (plan, q.get('window_unknown_plan')), (None, 1))


def test_plan_mapping_table():
    mapping = [('claude_max', 'default_claude_max_5x', 'claude:max-5x', {}),
               ('claude_max', 'default_claude_max_20x', 'claude:max-20x', {}),
               ('claude_pro', None, 'claude:pro', {}), ('claude_pro', '', 'claude:pro', {}),
               ('claude_pro', 'future-pro', 'claude:pro', {'account_tier_unrecognized': 1}),
               ('claude_pro', 'default_claude_max_5x', None, {'account_fields_contradictory': 1, 'account_plan_unknown': 1}),
               ('claude_pro', 'default_claude_max_20x', None, {'account_fields_contradictory': 1, 'account_plan_unknown': 1}),
               ('claude_max', None, None, {'account_plan_unknown': 1}),
               ('claude_max', '', None, {'account_plan_unknown': 1}),
               ('claude_max', 'unknown', None, {'account_tier_unrecognized': 1, 'account_plan_unknown': 1}),
               ('claude_team', 'default_claude_max_5x', None, {'account_plan_unknown': 1}),
               ('claude_enterprise', 'default_claude_max_20x', None, {'account_plan_unknown': 1}),
               (None, 'default_claude_max_5x', None, {'account_plan_unknown': 1}),
               ('future-org', 'default_claude_max_20x', None, {'account_plan_unknown': 1})]
    #Closed table: organization+knownMax tier establishesMax; Pro's unfamiliar tier is diagnostic only.
    expect('closed account mapping table', [account.map_plan(org, tier) for org, tier, _p, _q in mapping],
           [(p, q) for _org, _tier, p, q in mapping])


def test_partial_window_withheld():
    case = cases.quota_W()
    _assistant(case)['isAbortedMidStream'] = True
    w, q, _ = _w(case)
    #Contributing18:30 row remains input300 in local totals but is explicit partial: window withheld1.
    expect('partial contributing response withheld', (w['counts']['input'], w['shareable'], 'window_partial_response' in w['withheld_reasons'], q.get('window_partial_response')), (700, False, True, 1))


def test_unknown_speed_window_partial_split():
    case = cases.quota_W()
    del _assistant(case)['message']['usage']['speed']
    w, q, _ = _w(case)
    #Missing18:30 speed leaves full700/280/70 but only19:00=400/160/40 in recognized split.
    expect('unknown speed accepted outside split',
           (w['shareable'], w['counts']['input'], w['split_complete'], w['speed_unrecorded_responses'], q.get('window_unknown_speed'), w['withheld_reasons']),
           (True, 700, False, 1, 1, []))
    expect('known speed split exact counts', w['split'], [dict(model='claude-opus-5-5', tier='standard',
           responses=1, input=400, cached=160, output=40, cache_write_5m=10, cache_write_1h=20)])


def test_unknown_ttl_window_withheld():
    case = cases.quota_W()
    del _assistant(case)['message']['usage']['cache_creation']
    w, q, _ = _w(case)
    #Creation30 is nonzero with no exact5m/1h split: unknownTTL1; local required counts unchanged.
    expect('unknown nonzero TTL withheld', (w['shareable'], w['counts']['input'], w['cache_write_5m'],
           'window_unknown_cache_ttl' in w['withheld_reasons'], q.get('window_unknown_cache_ttl')), (False, 700, None, True, 1))


def test_unavailable_timestamps_withheld():
    for field, value in (('ts', None), ('ts', 'invalid'), ('timestamp_quality', 'copied'), ('replayed', True)):
        w, q, _ = _w(edit=lambda result: result['rows'][2].update({field: value}))
        #An unassignable end or unverified copy could change the numerator: withhold once, never invent a time.
        expect('required response timing withheld ' + field + str(value),
               (w['shareable'], 'window_unavailable_timestamps' in w['withheld_reasons'], q.get('window_unavailable_timestamps')), (False, True, 1))
    span, q = windows.observation_span([_quote(), _quote(percent=25, ts='invalid')])
    expect('required reading timestamp withheld', q.get('window_unavailable_timestamps'), 1)  #Missing peak time cannot form the span.


def test_overlapping_span_withheld():
    with corpus(cases.quota_W()) as (result, _files, _root):
        readings = result['limits'] + [_quote(windows.WEEK_S, 10, '2026-09-10T18:30:00Z'),
                                     _quote(windows.WEEK_S, 20, '2026-09-10T19:30:00Z')]
        built, q = windows.build_windows(readings, result['rows'])
    #Spans(18,19] and(18:30,19:30] overlap for30 minutes: both withheld, counter2.
    expect('overlapping all model spans withheld', (all(not w['shareable'] for w in built), q.get('window_overlapping_span')), (True, 2))


def test_server_pricing_diagnostics_only():
    for name, change, expected_model, tier, total in (
        ('window_rows_fast', lambda r: r['message']['usage'].update(speed='fast'), 'claude-opus-5-5', 'fast', 700),
        ('window_rows_us', lambda r: r['message']['usage'].update(inference_geo='us'), 'claude-opus-5-5', 'standard', 700),
        ('window_rows_haiku_long', lambda r: (r['message'].update(model='claude-haiku-5-5'),
             r['message']['usage'].update(input_tokens=99851)), 'claude-haiku-5-5', 'standard', 100401)):
        case = cases.quota_W()
        change(_assistant(case))
        #Before-first and after-first-peak rows also carry the setting; they must not
        #increase these observation-span diagnostics beyond the one contributing row.
        change(_assistant(case, 1))
        change(_assistant(case, 5))
        w, q, _ = _w(case)
        #Fast/US preserve700; Haiku prompt99851+30+120=100001 plus other row400 ->100401.
        expect('server pricing diagnostic only ' + name,
               (w['shareable'], q.get(name), w['counts']['input'], any(s['model'] == expected_model and s['tier'] == tier for s in w['split'])),
               (True, 1, total, True))
    case = cases.quota_W()
    _assistant(case)['message']['model'] = 'claude-sonnet-5-5'
    w, _q, _ = _w(case)
    expect('Sonnet catalog difference never withholds', (w['shareable'], w['counts']['input']), (True, 700))  #Server's.20 vs local.10 is not a quota-evidence defect.


def test_sparse_points_no_synthetic_endpoints():
    w, _q, _ = _w()
    #Only sourced18:00/19:00/20:00 at20/25/25; no nominal opening0 or synthesized reset100.
    expect('sparse sourced percentage points', w['pct_points'], [[START, 20], [END, 25], [END + 3600, 25]])
    case = cases.quota_W()
    _records(case).append(cases.quota_429())
    #Refusal100 at19:30 followed by plateau25 is a decline, yet100 remains a sourced point locally.
    w, _q, _ = _w(case)
    expect('refusal remains sourced percentage point', [END + 1800, 100] in w['pct_points'], True)


def test_nominal_chart_observation_share():
    w, _q, _ = _w()
    entries, _notes = windows.wire_windows([w], SAFE)
    #Chart100+200+300+400+500=1500,600reads,150output; share300+400=700,280reads,70output.
    expect('nominal counts distinct from share', (w['nominal_counts'], entries[0]['input'], entries[0]['responses']),
           (dict(responses=5, input=1500, cached=600, output=150, reasoning=0), 700, 2))
    expect('span exact cache writes', (w['cache_write_5m'], w['cache_write_1h']), (20, 40))  #10+10=20;20+20=40.
    expect('wire fields only', set(entries[0]), set(windows.WIRE_FIELDS))  #No local identifiers/points or inferred endpoints.
    expect('wire recognized split full counts', entries[0]['split'], [dict(model='claude-opus-5-5', tier='standard',
           responses=2, input=700, cached=280, output=70, cache_write_5m=20, cache_write_1h=40)])


def test_nominal_chart_boundaries():
    def add_boundaries(result):
        for key, stamp in (('nominal-boundary', '2026-09-10T16:00:00Z'), ('reset-boundary', '2026-09-17T16:00:00Z')):
            row = copy.deepcopy(result['rows'][0])
            row.update(response_key=key, ts=stamp)
            result['rows'].append(row)
    w, _q, _ = _w(edit=add_boundaries)
    #Start row+100 enters chart; reset row+100 excluded. Original share stays700.
    expect('nominal interval includes start excludes reset',
           (w['nominal_counts']['input'], w['counts']['input'], 'nominal-boundary' in w['nominal_rows'], 'reset-boundary' in w['nominal_rows']),
           (1600, 700, True, False))


def test_small_delta_shareable_ineligible():
    case = cases.quota_W()
    for report in _reports(case)[1:]:
        _limit(report)['percent'] = 24
    w, _q, _ = _w(case, [_snapshot()])
    #Delta24-20=4<5 is positive and shareable, locally estimator-ineligible.
    expect('small positive delta accepted', (w['shareable'], w['estimator_eligible']), (True, False))


def test_wire_coverage_guard():
    w, _q, _ = _w()
    for coverage in ({'windows_replace_safe': False, 'window_reasons': ['retained window unsafe']},
                     {'windows_replace_safe': True, 'window_reasons': ['retained window unsafe']}):
        entries, reasons = windows.wire_windows([w], coverage)
        #Either unsafe flag or a retained reason means omission(None), never an empty replacement.
        expect('coverage forbids window replacement ' + str(coverage['windows_replace_safe']),
               (entries, reasons['coverage']), (None, ['retained window unsafe']))
    w['shareable'] = False
    w['withheld_reasons'] = ['window_partial_response']
    entries, reasons = windows.wire_windows([w], SAFE)
    expect('wire reports per window withholding', (entries, reasons[w['window_key']]), ([], ['window_partial_response']))  #New unsafe window is omitted individually.


def test_captured_anchor_metadata():
    with corpus(cases.quota_W()) as (_result, files, root), history.History(root / 'history.db') as store:
        precommit = store.capture(files)
        store.commit()
        loaded = store.load()
        retained = ledger.build([], history=loaded)
        #Capture persists canonical reset/phase0 as scalars; precommit coverage remains withheld.
        expect('captured anchors survive reload',
               (retained['_reset_clusters'][0]['reset_at'], retained['_reset_clusters'][0]['grid_phase'], precommit['coverage']['safe_months']),
               (RESET, RESET, []))
        expect('captured anchor metadata integrity', store.check_integrity()[0], True)
        #A later capture moves the quote median above.5, while the captured reset stays0.
        later = cases.quota_W()
        for report in _reports(later):
            _limit(report)['resets_at'] = '2026-09-17T16:00:01.100000Z'
        for record in _records(later):
            record['uuid'] += '-later'
        paths = cases.write_corpus(root / 'later-projects', later)
        next_capture = store.capture([worker.extract(path) for path in paths])
        store.commit()
        expect('later captures retain established reset',
               next_capture['_reset_clusters'][0]['reset_at'], RESET)
        store.db.execute("UPDATE meta SET v='[]' WHERE k='reset_clusters'")
        expect('damaged anchor metadata rejected', store.check_integrity()[0], False)  #Checksum protects fixed anchors from undetected edits.


# Each pure decision test is paired; SQL metadata persistence is covered separately.
WINDOWS_DECISIONS = tuple(sorted(n for n in globals() if n.startswith('test_') and n != 'test_captured_anchor_metadata'))


def main():
    RESULTS.clear()
    tests = sorted(n for n in globals() if n.startswith('test_'))
    with offline():
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
