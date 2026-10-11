"""Offline hand-calculated Claude pricing, inventory and image regressions (stdlib)."""
import base64
import contextlib
import copy
import datetime
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import urllib.request
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
LIB = REPO / 'plugins/token-counter-claude/skills/token-report/scripts/tokencounter'


def _load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(
        name, str(path), submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if 'claude_counter' not in sys.modules:
    _load('claude_counter', LIB / '__init__.py', package=True)
cases = _load('claude_pricing_cases', HERE / 'fixtures/claude_cases.py')
fetch = _load('fetch_anthropic_prices', HERE / 'fetch_anthropic_prices.py')
from claude_counter import analyze, composition, history, ledger, pricing, worker  # noqa: E402

RESULTS = []
PRICING_DECISIONS = (
    'test_price_table_finite_rates', 'test_unavailable_table_keeps_recorded_search_counts',
    'test_fast_R1', 'test_us_R1', 'test_fast_us_R1',
    'test_haiku_threshold', 'test_haiku_threshold_includes_reads', 'test_thinking_not_added',
    'test_web_fetch_zero_fee', 'test_missing_search_default_zero', 'test_unknown_ttl_assumed_5m',
    'test_missing_speed_standard_price_only', 'test_missing_speed_and_ttl_high',
    'test_not_available_geo_global', 'test_missing_geo_global', 'test_unknown_geo_unpriced',
    'test_opus46_fast_fallback', 'test_opus47_fast_unpriced', 'test_web_search_error_no_invented_fee',
)


def expect(name, got, expected):
    RESULTS.append((name, got == expected, 'expected %r, got %r' % (expected, got)))


def close(name, got, expected):
    RESULTS.append((name, got is not None and abs(got - expected) < 1e-12,
                    'expected %.12g, got %r' % (expected, got)))


@contextlib.contextmanager
def offline():
    def denied(*args, **kwargs):
        raise AssertionError('network access attempted')
    with mock.patch.object(socket, 'socket', denied), mock.patch.object(socket, 'create_connection', denied), \
            mock.patch.object(socket, 'getaddrinfo', denied), mock.patch.object(urllib.request, 'urlopen', denied):
        yield


def _b():
    return copy.deepcopy(cases.baseline_B())


def _records(case):
    return next(iter(case['files'].values()))


def _r1():
    record = copy.deepcopy(_records(_b())[2])
    record['parentUuid'] = 'B-U0'
    return record


def _case(records, session='A'):
    case = _b()
    case['files'] = {'synthetic-project/' + session + '.jsonl': copy.deepcopy(records)}
    return case


@contextlib.contextmanager
def corpus(case, metrics_only=False):
    with tempfile.TemporaryDirectory() as temp:
        paths = cases.write_corpus(Path(temp) / 'projects', case)
        files = [worker.extract(path, metrics_only=metrics_only) for path in paths]
        yield ledger.build(files), files, Path(temp)


def _table():
    table, reason = pricing.load()
    if reason is not None:
        raise AssertionError('vendored prices unavailable')
    return table


def _row(change=None):
    record = _r1()
    if change:
        change(record)
    with corpus(_case([_records(_b())[0], record])) as (result, _files, _root):
        return result['rows'][0]


def _price(change=None):
    return pricing.price_row(_row(change), _table())


def _setting(**kwargs):
    return lambda record: record['message']['usage'].update(kwargs)


def test_price_fixture_parse():
    fixture = HERE / 'fixtures/anthropic_pricing.md'
    # E5's byte digest, measured before copying. CI does not depend on ignored review files.
    expect('E5 fixture byte parity', hashlib.sha256(fixture.read_bytes()).hexdigest(),
           '67904e280b595d31f373c857844ed380072ae79d280d8f385640b5992d337ca9')
    table = fetch.parse_prices(fixture.read_text(encoding='utf-8'), as_of='2026-10-10', source_url=fetch.PRICING_URL)
    expect('offline vendored table parity', table, json.loads(Path(pricing.vendor_path()).read_text(encoding='utf-8')))
    records = table['models'].values()
    expect('table cardinalities', (len(table['models']), len(table['models']) + sum(r['long_context'] is not None for r in records),
                                   sum(r['fast'] is not None for r in records), len(table['aliases'])), (20, 21, 3, 1))
    # Every Q7 row, in dollars per million: hand transcription, independent of the parser.
    expected = {}
    for family in ('fable', 'mythos'):
        expected['claude-' + family + '-5-1'] = (10, 12.5, 20, .25, 50)
        expected['claude-' + family + '-5'] = (10, 12.5, 20, 1, 50)
    expected['claude-opus-5-5'] = (4, 5, 8, .2, 20)
    for v in ('5', '4-8', '4-7', '4-6', '4-5'):
        expected['claude-opus-' + v] = (5, 6.25, 10, .5, 25)
    for v in ('4-1', '4'):
        expected['claude-opus-' + v] = (15, 18.75, 30, 1.5, 75)
    expected.update({'claude-sonnet-5-5': (2, 2.5, 4, .1, 10), 'claude-sonnet-5': (2, 2.5, 4, .2, 10),
                     'claude-haiku-5-5': (.1, .125, .2, .01, .5), 'claude-haiku-4-5': (1, 1.25, 2, .1, 5),
                     'claude-haiku-3-5': (.8, 1, 1.6, .08, 4)})
    for v in ('4-6', '4-5', '4'):
        expected['claude-sonnet-' + v] = (3, 3.75, 6, .3, 15)
    expect('every Q7 base rate', {m: tuple(r[k] for k in pricing.RATE_KEYS) for m, r in table['models'].items()}, expected)
    expect('Haiku alternate rates', tuple(table['models']['claude-haiku-5-5']['long_context'][k] for k in pricing.RATE_KEYS),
           (.5, .625, 1, .05, 2.5))
    expect('three Fast rate records', {m: tuple(r['fast'][k] for k in pricing.RATE_KEYS)
                                      for m, r in table['models'].items() if r['fast'] is not None},
           {'claude-opus-5-5': (8, 10, 16, .4, 40), 'claude-opus-5': (10, 12.5, 20, 1, 50),
            'claude-opus-4-8': (10, 12.5, 20, 1, 50)})
    # The extractor estimates images without the price table; its model list and
    # resolution rule must say the same as the table's image_tier.
    expect('composition knows exactly the priced models', composition.KNOWN_MODELS, frozenset(table['models']))
    expect('image tiers agree with composition', {m: composition._image_limits(m) for m in table['models']},
           {m: (2576, 4784) if r['image_tier'] == 'high' else (1568, 1568) for m, r in table['models'].items()})


def test_price_fixture_offline():
    with tempfile.TemporaryDirectory() as temp, offline(), contextlib.redirect_stdout(io.StringIO()):
        out = Path(temp) / 'prices.json'
        args = ['--from', str(HERE / 'fixtures/anthropic_pricing.md'), '--as-of', '2026-10-10', '--out', str(out)]
        expect('offline parser writes local output', fetch.main(args), 0)
        expect('offline check equal', fetch.main(args + ['--check']), 0)
        out.write_text('{}', encoding='utf-8')
        expect('offline check differs exit one', fetch.main(args + ['--check']), 1)
        expect('offline check does not write', out.read_text(encoding='utf-8'), '{}')


def test_price_table_finite_rates():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'prices.json'
        for record_name in ('base', 'fast', 'long_context', 'fee'):
            for bad in (float('nan'), float('inf'), -1, True, '4', None):
                table = _table()
                if record_name == 'fee':
                    table['fees']['web_search_request'] = bad
                else:
                    model = 'claude-haiku-5-5' if record_name == 'long_context' else 'claude-opus-5-5'
                    target = table['models'][model] if record_name == 'base' else table['models'][model][record_name]
                    target['input'] = bad
                path.write_text(json.dumps(table), encoding='utf-8')
                expect('finite rates reject whole table', pricing.load(path), (None, 'price_table_invalid'))
        for key, value in (('long_context_threshold', True), ('long_context_threshold', 0),
                           ('long_context_threshold', 100000.0), ('long_context', {}), ('fast', {'input': 8})):
            table = _table()
            table['models']['claude-haiku-5-5'][key] = value
            path.write_text(json.dumps(table), encoding='utf-8')
            expect('malformed alternate rejects whole table', pricing.load(path), (None, 'price_table_invalid'))
        for aliases in ([], {'claude-alias': 'claude-missing'}, {'claude-opus-5-5': 'claude-haiku-5-5'},
                        {'bad identity': 'claude-opus-5-5'}, {'claude-a': 'claude-b', 'claude-b': 'claude-opus-5-5'}):
            table = _table()
            table['aliases'] = aliases
            path.write_text(json.dumps(table), encoding='utf-8')
            expect('malformed alias rejects whole table', pricing.load(path), (None, 'price_table_invalid'))
        expect('missing table is unavailable', pricing.load(Path(temp) / 'absent'), (None, 'price_table_unavailable'))


def test_unavailable_table_keeps_recorded_search_counts():
    row = _row()
    value = pricing.summarize([row], (None, 'price_table_unavailable'))
    # One recorded search is still one call; rejecting the table prices no dollars.
    expect('missing table keeps observed calls without fallback rates',
           (value['web_search_calls'], value['usd'], value['available'], value['price_table_unavailable']), (1, 0, False, 1))
    row['web_search_requests'] = None
    value = pricing.summarize([row], (None, 'price_table_invalid'))
    expect('invalid table keeps missing-count diagnostic',
           (value['search_count_unrecorded'], value['price_table_invalid'], value['unpriced_responses']), (1, 1, 1))


def test_price_B():
    with corpus(_b()) as (result, _files, _root):
        value = pricing.summarize(result['rows'], _table())
        # R1=(40+100+240+8+160)/1e6+.01; R2=(20+800+10+200)/1e6.
        expect('B list value', (value['usd'], value['usd_high'], value['tokens_usd'], value['web_search_usd']),
               (.011578, .011578, .001578, .01))
        for row in result['rows']:
            row['raw_model'] += '[1m]'
        expect('context suffix no surcharge', pricing.summarize(result['rows'], _table())['usd'], .011578)
        rows = [dict(_row(), web_search_requests=0) for _ in range(7)]
        # 7*548 microdollars = 3836, without rounding each intermediate price.
        expect('unrounded accumulation', pricing.summarize(rows, _table())['usd'], .003836)


def test_fast_R1():
    p = _price(_setting(speed='fast'))
    # (10*8+20*10+30*16+40*.4+8*40)/1e6 = .001096; search adds .01.
    close('Fast R1 tokens', p['tokens_usd'], .001096)
    close('Fast R1 total', p['tokens_usd'] + p['web_search_usd'], .011096)


def test_us_R1():
    p = _price(_setting(inference_geo='us'))
    # 548 microdollars *1.1 =602.8; search itself has no residency multiplier.
    close('US R1 tokens', p['tokens_usd'], .0006028)
    close('US R1 total', p['tokens_usd'] + p['web_search_usd'], .0106028)


def test_fast_us_R1():
    p = _price(_setting(speed='fast', inference_geo='us'))
    # 1096*1.1 =1205.6 microdollars; +.01 search.
    close('Fast US R1 tokens', p['tokens_usd'], .0012056)
    close('Fast US R1 total', p['tokens_usd'] + p['web_search_usd'], .0112056)


def _haiku(base, reads=0, writes=0):
    def change(record):
        record['message'].update(model='claude-haiku-5-5', usage=cases._usage(base, writes, 0, reads, 1))
    return _price(change)


def test_haiku_threshold():
    # 100000*.1+.5 =10000.5; 100001*.5+2.5 =50003 microdollars.
    close('Haiku threshold inclusive low band', _haiku(100000)['tokens_usd'], .0100005)
    close('Haiku threshold high band', _haiku(100001)['tokens_usd'], .050003)


def test_haiku_threshold_includes_reads():
    # Whole prompt 100001: 100001*.05+2.5 =5002.55 microdollars.
    close('Haiku threshold includes reads', _haiku(0, reads=100001)['tokens_usd'], .00500255)
    # Whole prompt from writes: 100001*.625+2.5 =62503.125 microdollars.
    close('Haiku threshold includes writes', _haiku(0, writes=100001)['tokens_usd'], .062503125)


def test_thinking_not_added():
    p = _price(_setting(output_tokens_details={'thinking_tokens': 8}))
    # Output remains8, so 548 microdollars + .01 search, even if all8 are thinking.
    close('thinking is already output', p['tokens_usd'] + p['web_search_usd'], .010548)


def test_web_fetch_zero_fee():
    p = _price(_setting(server_tool_use={'web_search_requests': 1, 'web_fetch_requests': 999}))
    # Fetch requests contribute0; R1 is still548 microdollars + .01 search.
    close('fetch fee zero', p['tokens_usd'] + p['web_search_usd'], .010548)


def test_missing_search_default_zero():
    p = _price(_setting(server_tool_use={'web_fetch_requests': 2}))
    # Unrecorded searches contribute0; tokens alone=548 microdollars.
    expect('missing search zero fee', (p['web_search_usd'], p['web_search_calls'], p['counters'].get('search_count_unrecorded')), (0, 0, 1))
    close('missing search tokens', p['tokens_usd'], .000548)


def _unknown_ttl(record):
    record['message']['usage'].pop('cache_creation')


def test_unknown_ttl_assumed_5m():
    p = _price(_unknown_ttl)
    # base40+reads8+output160+50*5=458; all1h uses50*8=400 ->608 microdollars.
    close('unknown TTL defaults all five minutes', p['tokens_usd'], .000458)
    close('unknown TTL high all one hour', p['tokens_usd_high'], .000608)
    close('unknown TTL total with search', p['tokens_usd'] + p['web_search_usd'], .010458)
    close('unknown TTL high with search', p['tokens_usd_high'] + p['web_search_usd'], .010608)
    expect('unknown TTL assumption counted', p['counters'].get('price_cache_ttl_assumed'), 1)


def test_missing_speed_standard_price_only():
    row = _row(lambda r: r['message']['usage'].pop('speed'))
    p = pricing.price_row(row, _table())
    # Standard548 microdollars; supported Fast1096; no recorded wire tier.
    close('missing speed Standard default', p['tokens_usd'], .000548)
    close('missing speed Fast high', p['tokens_usd_high'], .001096)
    expect('missing speed counter and tier', (p['counters'].get('speed_unrecorded'), row['tier']), (1, None))


def test_missing_speed_and_ttl_high():
    def change(record):
        _unknown_ttl(record)
        record['message']['usage'].pop('speed')
    p = _price(change)
    # Standard all5m=458; Fast all1h=(80+800+16+320)=1216 microdollars.
    close('missing speed TTL default', p['tokens_usd'], .000458)
    close('missing speed TTL joint high', p['tokens_usd_high'], .001216)
    expect('joint assumptions counted', (p['counters'].get('speed_unrecorded'), p['counters'].get('price_cache_ttl_assumed')), (1, 1))


def _global_default(p, name):
    # Missing/not_available geography always548 microdollars, with no US upper scenario.
    close(name, p['tokens_usd'], .000548)
    close(name + ' high', p['tokens_usd_high'], .000548)
    expect(name + ' counter', p['counters'].get('price_geography_default_global'), 1)


def test_not_available_geo_global():
    _global_default(_price(_setting(inference_geo='not_available')), 'unavailable geography global')


def test_missing_geo_global():
    _global_default(_price(lambda r: r['message']['usage'].pop('inference_geo')), 'missing geography global')


def test_unknown_geo_unpriced():
    p = _price(_setting(inference_geo='future-region'))
    expect('unknown geography unpriced', (p['tokens_usd'], p['unpriced_reason']), (None, 'geography'))
    row = _row(lambda r: r['message'].update(model='claude-opus-4-5'))
    row['inference_geo'] = 'us'
    expect('unsupported US model unpriced', pricing.price_row(row, _table())['unpriced_reason'], 'geography')


def test_unknown_model_named():
    row = _row(lambda r: r['message'].update(model='claude-haiku-4-5-20990101'))
    value = pricing.summarize([row], _table())
    expect('unknown dated model named', (value['unpriced_models'][0]['model'], value['unpriced_input']),
           ('claude-haiku-4-5-20990101', 100))
    expect('closed dated alias', pricing.canonical_model('claude-haiku-4-5-20251001[1m]', _table()),
           ('claude-haiku-4-5', 'claude-haiku-4-5-20251001[1m]', True))


def test_synthetic_unpriced_no_charge():
    for field in ('synthetic', 'api_error'):
        row = _row()
        if field == 'synthetic':
            row['raw_model'] = '<synthetic>'
        else:
            row['isApiErrorMessage'] = True
        value = pricing.summarize([row], _table())
        expect('excluded price ' + field, (value['responses'], value['usd'], value['web_search_calls']), (0, 0, 0))


def test_opus46_fast_fallback():
    row = _row(lambda r: r['message'].update(model='claude-opus-4-6'))
    row['speed'] = 'fast'
    p = pricing.price_row(row, _table())
    # Standard Opus4.6=(50+125+300+20+200)=695 microdollars.
    close('Opus 46 Fast falls back to Standard', p['tokens_usd'], .000695)
    expect('Opus 46 unsupported Fast counted', p['counters'].get('unsupported_fast_setting'), 1)


def test_opus47_fast_unpriced():
    row = _row(lambda r: r['message'].update(model='claude-opus-4-7'))
    row['speed'] = 'fast'
    p = pricing.price_row(row, _table())
    expect('Opus 47 Fast unpriced', (p['tokens_usd'], p['unpriced_reason'], row['usage']['input_tokens']), (None, 'speed', 100))
    row['speed'] = 'future-speed'
    expect('unknown speed unpriced', pricing.price_row(row, _table())['unpriced_reason'], 'speed')


def test_web_search_error_no_invented_fee():
    row = _row(_setting(server_tool_use={'web_fetch_requests': 2}))
    row['web_search_failures'] = 1
    p = pricing.price_row(row, _table())
    # Error evidence is not a count; unrecorded billable calls0 ->fee0.
    expect('search errors do not invent fee', (p['web_search_usd'], p['web_search_calls']), (0, 0))
    row['web_search_requests'] = 1
    p = pricing.price_row(row, _table())
    expect('aggregate retained with error diagnostic', (p['web_search_usd'], p['counters'].get('price_search_failure_ambiguous')), (.01, 1))


def test_cost_oracle_not_substitution():
    case = _b()
    oracle = {'inputTokens': 999999, 'cacheCreationInputTokens': 0, 'cacheReadInputTokens': 0,
              'outputTokens': 999, 'costUSD': 1234, 'costBasis': 'actual'}
    _records(case).append({'type': 'cost-state', 'uuid': 'oracle', 'timestamp': '2026-09-10T00:00:22Z',
                           'modelUsage': {'claude-opus-5-5': oracle, 'claude-opus-5-5[1m]': oracle,
                                          'claude-sonnet-5-5': oracle}})
    _records(case).append({'type': 'system', 'subtype': 'local_command', 'uuid': 'usage-oracle',
                           'timestamp': '2026-09-10T00:00:23Z',
                           'usageReport': {'session': {'model_usage': {'claude-opus-5-5': oracle}}}})
    with corpus(case) as (result, _files, _root):
        checks = pricing.crosscheck(result['cost_checks'], result['rows'], _table())
        matched = next(c for c in checks['checks'] if c['source'] == 'cost_state' and c['model'] == 'claude-opus-5-5' and not c['context_1m'])
        # Captured base15/creation150/read90/output18; cost-state1234 is only a comparison.
        expect('cost oracle never substitutes', (pricing.summarize(result['rows'], _table())['usd'],
                                                matched['reported_cost_usd'], matched['captured_counts']['base_input'],
                                                matched['captured_usd']), (.011578, 1234, 15, .011578))
        expect('context-specific oracle matches', (len(checks['checks']), len(checks['unmatched_models'])), (4, 2))


def _bytes(result):
    counts = {}
    for f in result['content']:
        if f['image'] is None:
            counts[f['category']] = counts.get(f['category'], 0) + (f['utf8_bytes'] or 0)
    return counts


def test_inventory_B():
    with corpus(_b()) as (result, files, _root):
        # Userabcd4 + canonical {"x":"y"}9 + toolxyz3 + assistantok2 =18.
        expect('B inventory categories', _bytes(result), {'user_message': 4, 'tool_call_input': 9, 'tool_output': 3, 'assistant_message': 2})
        expect('B inventory bytes versus recorded input', (sum(_bytes(result).values()), sum(r['usage']['input_tokens'] for r in result['rows'])), (18, 255))
        expect('facts contain no bodies', all(set(f) == {'item_key', 'family_id', 'category', 'ts', 'utf8_bytes', 'body_digest', 'snapshot', 'image'}
                                              for f in result['content']), True)
        # Missing block identity withholds composition only, not usage.
    case = _b()
    _records(case)[2].pop('apiBlockIndex')
    with corpus(case) as (result, _files, _root):
        expect('unkeyed argument excluded', (result['counters'].get('composition_unkeyed_items'), _bytes(result).get('tool_call_input')), (1, None))


def test_snapshot_dedup():
    snapshot = {'type': 'attachment', 'uuid': 'snapshot', 'timestamp': '2026-09-10T00:00:00Z',
                'attachment': {'type': 'prompt_snapshot', 'systemPrompt': ['saved'],
                               'tools': [{'name': 't', 'description': 'd', 'schema': {}, 'path': 'excluded'}]}}
    case = _b()
    for i in range(3):
        item = copy.deepcopy(snapshot)
        item['uuid'] += str(i)
        _records(case).append(item)
    with corpus(case) as (result, _files, _root):
        # Three copies: saved5 bytes once; canonical definition is42 bytes once.
        expect('snapshots deduplicate within family', (_bytes(result)['system_prompt'], _bytes(result)['tool_schema'],
                                                      result['counters'].get('composition_snapshot_copies')), (5, 42, 4))
    # A second unrelated family retains its own saved state.
    case['files']['synthetic-project/C.jsonl'] = [dict(snapshot, uuid='second-family')]
    with corpus(case) as (result, _files, _root):
        expect('snapshots are family scoped', _bytes(result)['system_prompt'], 10)


def test_state_copies_not_arguments():
    case = _b()
    _records(case).append({'type': 'attachment', 'uuid': 'copies', 'timestamp': '2026-09-10T00:00:22Z',
                           'attachment': {'type': 'deferred_tools_record', 'toolInputCopies': [{'id': 't', 'copy': {'x': 'y'}}]}})
    with corpus(case) as (result, _files, _root):
        # One actual call has9 bytes; saved copy adds0.
        expect('saved copies add no arguments', _bytes(result)['tool_call_input'], 9)


def test_unknown_attachment_no_recursive_scan():
    case = _b()
    _records(case).append({'type': 'attachment', 'uuid': 'future', 'timestamp': '2026-09-10T00:00:22Z',
                           'attachment': {'type': 'future', 'hidden': {'content': 'hidden invented sentinel'}}})
    with corpus(case) as (result, _files, _root):
        expect('unknown attachment omitted', (sum(_bytes(result).values()), result['counters'].get('composition_unknown_attachments')), (18, 1))


def test_no_external_file_or_image_reads():
    record = cases._user('external', '2026-09-10T00:00:00Z', [
        {'type': 'tool_result', 'tool_use_id': 'external', 'content': {'type': 'file', 'file_id': 'file sentinel'}},
        {'type': 'image', 'source': {'type': 'url', 'url': 'https://invented.invalid/image'}},
        {'type': 'image', 'source': {'type': 'file', 'file_id': 'invented-id'}}])
    with mock.patch('builtins.open', side_effect=AssertionError('external open attempted')), \
            mock.patch.object(Path, 'open', side_effect=AssertionError('external path open attempted')), offline():
        facts, counters = composition.measure_record(record, {'source_id': 'invented'}, 1)
    expect('external sources unavailable', ([f['image']['estimated_visual_tokens'] for f in facts],
                                           counters.get('composition_external_tool_results_unread'),
                                           counters.get('images_unsupported_source')), ([None, None], 1, 2))


def test_image_standard_1920():
    # 1456/28=52 and ceil819/28=30 ->1560 patches, padding fits1568.
    expect('standard image resize', composition.resize_image(1920, 1080, max_edge=1568, max_tokens=1568), (1456, 819))
    expect('standard image patches', composition.claude_image_tokens(1920, 1080, 'claude-opus-4-6'), 1560)


def test_image_high_1920():
    # ceil1920/28=69; ceil1080/28=39 ->2691 patches without resize.
    expect('high image original', composition.resize_image(1920, 1080, max_edge=2576, max_tokens=4784), (1920, 1080))
    expect('high image patches', composition.claude_image_tokens(1920, 1080, 'claude-opus-4-7'), 2691)


def test_image_high_4k():
    # 2576/28=92; ceil1449/28=52 ->4784 patches.
    expect('4k image resize', composition.resize_image(3840, 2160, max_edge=2576, max_tokens=4784), (2576, 1449))
    expect('4k image patches', composition.claude_image_tokens(3840, 2160, 'claude-opus-5-5'), 4784)
    expect('portrait preserves aspect', composition.resize_image(2160, 3840, max_edge=2576, max_tokens=4784), (1449, 2576))
    expect('unknown model and transforms unavailable', (composition.claude_image_tokens(1920, 1080, 'claude-future-9'),
                                                        composition.claude_image_tokens(1920, 1080, 'claude-opus-5-5', transformations_known=False)), (None, None))


def _png(width, height):
    return base64.b64encode(b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', width, height)).decode('ascii')


def test_image_not_added_to_input():
    case = _b()
    _records(case)[3]['message']['content'][0]['content'] = [
        {'type': 'text', 'text': 'xyz'}, {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': _png(1920, 1080)}}]
    with corpus(case) as (result, _files, _root):
        # Known image2691 patches stays outside recorded255 input and inventory18 bytes.
        image = next(f['image'] for f in result['content'] if f['image'] is not None)
        expect('image measured without input inflation', (sum(r['usage']['input_tokens'] for r in result['rows']),
                                                         sum(_bytes(result).values()), image['estimated_visual_tokens']), (255, 18, 2691))
    # A first prompt image is associated with its own served4.6 response, not a default.
    case = _case([_records(_b())[0], _r1()])
    _records(case)[0]['message']['content'].append({'type': 'image', 'source': {
        'type': 'base64', 'media_type': 'image/png', 'data': _png(1920, 1080)}})
    _records(case)[1]['message']['model'] = 'claude-opus-4-6'
    with corpus(case) as (result, _files, _root):
        image = next(f['image'] for f in result['content'] if f['image'] is not None)
        expect('first prompt image uses consuming response model', (image['model'], image['estimated_visual_tokens'],
                                                                   result['counters'].get('images_unknown_model', 0)), ('claude-opus-4-6', 1560, 0))
    # Change the model for R2: the image in its tool-result prompt uses high resolution.
    case = _b()
    _records(case)[1]['message']['model'] = _records(case)[2]['message']['model'] = 'claude-opus-4-6'
    _records(case)[3]['message']['content'][0]['content'] = [
        {'type': 'text', 'text': 'xyz'}, {'type': 'image', 'source': {
            'type': 'base64', 'media_type': 'image/png', 'data': _png(1920, 1080)}}]
    with corpus(case) as (result, _files, _root):
        image = next(f['image'] for f in result['content'] if f['image'] is not None)
        expect('image never borrows previous response model', (image['model'], image['estimated_visual_tokens']), ('claude-opus-5-5', 2691))


def test_known_attachment_sources_and_surrogates():
    records = [
        {'type': 'instructions', 'files': [{'path': 'excluded', 'content': 'abc'}]},
        {'type': 'skill_listing', 'content': 'de'},
        {'type': 'file', 'content': {'file': {'content': 'fg', 'filename': 'excluded'}}},
        {'type': 'edited_text_file', 'snippet': 'h', 'filename': 'excluded'},
        {'type': 'nested_memory', 'content': {'content': 'ij', 'path': 'excluded'}},
        {'type': 'environment', 'text': 'k', 'content': 'l', 'snippet': 'm'},
    ]
    facts = []
    for i, attachment in enumerate(records):
        record = {'type': 'attachment', 'uuid': 'known%d' % i, 'timestamp': '2026-09-10T00:00:00Z', 'attachment': attachment}
        measured, _ = composition.measure_record(record, {'source_id': 'known'}, i + 1)
        facts.extend(measured)
    expect('exact known attachment sources', [(f['category'], f['utf8_bytes']) for f in facts],
           [('instructions', 3), ('skill_listing', 2), ('attachment', 2), ('attachment', 1),
            ('attachment', 2), ('attachment', 1), ('attachment', 1), ('attachment', 1)])
    with tempfile.TemporaryDirectory() as temp:
        record = _r1()
        record['message']['content'] = [{'type': 'text', 'text': '\ud800'}]
        path = Path(temp) / 'project/A.jsonl'
        path.parent.mkdir()
        path.write_text(json.dumps(record, ensure_ascii=True) + '\n', encoding='utf-8')
        result = ledger.build([worker.extract(path)])
        expect('surrogate inventory unavailable usage retained', (len(result['rows']), result['content'][0]['utf8_bytes'],
                                                                 result['content'][0]['body_digest'], result['counters'].get('composition_invalid_unicode')), (1, None, None, 1))


def test_content_replaced_without_revision():
    with corpus(_b()) as (_result, files, root), history.History(root / 'history.db') as store:
        before = store.capture(files)
        expect('precommit coverage unchanged', before['coverage']['history_committed'], False)
        store.commit()
        content = copy.deepcopy(files)
        fact = content[0]['responses'][0]['blocks'][1]['content'][0]
        fact['utf8_bytes'] = 10
        fact['body_digest'] = '0' * 64
        after = store.capture(content)
        expect('derived content is replaced without conflict', (_bytes(after)['tool_call_input'], after['counters'].get('history_conflicting_revisions', 0)), (10, 0))
        content[0]['content'][0].update(utf8_bytes=5, body_digest='1' * 64)
        after = store.capture(content)
        expect('derived source content adds a measurement without conflict', (_bytes(after)['user_message'],
                                                                             after['counters'].get('history_conflicting_revisions', 0)), (5, 0))
        store.commit()
        loaded = ledger.build([], history=store.load())
        expect('content preserved after history-only rebuild', _bytes(loaded), _bytes(after))
        expect('committed coverage supplied by history', store.coverage(loaded, endpoint='invented')['history_committed'], True)


def test_calendar_helpers_match_codex():
    codex_lib = REPO / 'plugins/token-counter/skills/token-report/scripts/tokencounter'
    if 'calendar_codex' not in sys.modules:
        _load('calendar_codex', codex_lib / '__init__.py', package=True)
    from calendar_codex import analyze as codex

    def outcome(function, args):
        try:
            return 'result', function(*args)
        except Exception as exc:
            return 'exception', type(exc).__name__

    vectors = {
        '_local_day': [('2026-09-10T00:00', x) for x in (None, '+00:00', '+08:00', '-04:30', '+0530', '+05:45', '+14:00', '-12:00', 'bad')]
                      + [('invalid', '+00:00'), (None, None)],
        '_day': [(v, 'fallback') for v in ('2026-09-10T00:00:00Z', '2026-09-10T00:00:00+08:00',
                                         '2026-09-10T00:00:00-04:30', '2026-09-10T00:00:00+0530',
                                         '2026-09-10T00:00:00', 'invalid', None, 1)],
        '_day_span': [(v,) for v in ('2026-09-10', '2024-02-29', '2025-02-29', 'invalid', None, 1)]
                     + [('2026-09-10', datetime.timezone(datetime.timedelta(hours=8)))],
        '_iso': [(v,) for v in (0, -1, 1789000000.125, None, float('inf'), 10**20, 'invalid')],
    }
    # Custom stdlib zone makes the spring day23h and fall day25h on Python3.8 too.
    class TestZone(datetime.tzinfo):
        def utcoffset(self, dt):
            return datetime.timedelta(hours=-4 if datetime.datetime(2026, 3, 8, 2) <= dt.replace(tzinfo=None)
                                      < datetime.datetime(2026, 11, 1, 2) else -5)
        def dst(self, dt):
            return self.utcoffset(dt) - datetime.timedelta(hours=-5)
    zone = TestZone()
    vectors['_day_span'].extend([(d, zone) for d in ('2026-03-08', '2026-11-01')])
    for name, inputs in vectors.items():
        for i, args in enumerate(inputs):
            expect('calendar parity %s vector%d' % (name, i), outcome(getattr(analyze, name), args), outcome(getattr(codex, name), args))
    expect('calendar spring and fall spans', [analyze._day_span(d, zone)[1] - analyze._day_span(d, zone)[0]
                                            for d in ('2026-03-08', '2026-11-01')], [23 * 3600, 25 * 3600])


def main():
    RESULTS.clear()
    tests = sorted(name for name in globals() if name.startswith('test_'))
    with offline():
        for name in tests:
            try:
                globals()[name]()
            except Exception as exc:
                RESULTS.append((name + ' invalid execution', False, type(exc).__name__))
    for name, ok, detail in RESULTS:
        # Only invented fixtures and numeric measurements appear in failures.
        print('[%s] %s%s' % ('PASS' if ok else 'FAIL', name, '' if ok else ': ' + detail))
    passed = sum(ok for _name, ok, _detail in RESULTS)
    print('\n%d/%d assertions passed (%d tests; network blocked)' % (passed, len(RESULTS), len(tests)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
