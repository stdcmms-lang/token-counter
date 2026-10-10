"""Invented Claude ledger, extraction and path regressions; stdlib only.

    python -I -S -B scripts/test_claude_ledger.py

Expected counts are the plan's hand calculations. Each test edits a fresh copy of a builder
case. All writes are inside TemporaryDirectory; neither live account state nor transcripts
are test inputs. Named assertions also identify the mutation that must turn them red.
"""
import ast
import builtins
import contextlib
import copy
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
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


_load('claude_counter', LIB / '__init__.py', package=True)
cases = _load('claude_cases', HERE / 'fixtures/claude_cases.py')
from claude_counter import account, ledger, models, paths, rollout, worker  # noqa: E402

RESULTS = []
LEDGER_TESTS = (
    'test_last_block_whole_usage', 'test_last_not_componentwise_max',
    'test_equal_usage_distinct_ids', 'test_missing_identity', 'test_zero_real_response',
    'test_zero_placeholder_copy', 'test_conflicting_nonzero_copy', 'test_continuation_owner',
    'test_fork_context_owner', 'test_subagent_continuation_pair',
    'test_unrelated_copy_charged_once', 'test_unrelated_copy_owner_tie',
    'test_continuation_cycle', 'test_orphan_subagent', 'test_synthetic_positive_usage',
    'test_api_error_positive_usage', 'test_aborted_positive_usage', 'test_truncated_real_usage',
    'test_null_stop_is_not_abort', 'test_max_tokens_and_refusal', 'test_compaction_no_charge',
    'test_cost_state_no_charge', 'test_iterations_not_added', 'test_iterations_disagree',
    'test_required_counts_strict', 'test_optional_reasoning_invalid', 'test_ttl_mismatch',
    'test_missing_optional_counts', 'test_actual_model_authoritative', 'test_dated_alias_closed',
    'test_context_suffix', 'test_version_irrelevant', 'test_partial_line',
    'test_unknown_shape_counted', 'test_filter_after_dedup',
)


def expect(name, got, expected):
    RESULTS.append((name, got == expected, 'expected %r, got %r' % (expected, got)))


def _b():
    return copy.deepcopy(cases.baseline_B())


def _records(case):
    return next(iter(case['files'].values()))


def _r1():
    row = copy.deepcopy(_records(_b())[2])
    # Single-terminal variants link directly to U0; full B keeps its two-block chain.
    row['parentUuid'] = 'B-U0'
    return row


def _case(records, session='A'):
    case = _b()
    case['files'] = {'synthetic-project/' + session + '.jsonl': copy.deepcopy(records)}
    return case


@contextlib.contextmanager
def corpus(case, metrics_only=False):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'projects'
        written = cases.write_corpus(root, copy.deepcopy(case))
        results = [worker.extract(path, metrics_only=metrics_only) for path in written]
        yield ledger.build(results), results, root


def totals(result):
    rows = result['rows']
    return (len(rows), sum(r['usage']['input_tokens'] for r in rows),
            sum(r['usage']['output_tokens'] for r in rows))


def timed(result):
    return [worker.epoch(r['ts']) - worker.epoch(r['req_ts']) for r in result['rows']
            if r['req_ts'] is not None and r['ts'] is not None]


def count(result, name):
    return result['counters'].get(name, 0)


def _continuation(case, a='A', b='B', shift=False, cycle=False):
    records = copy.deepcopy(_records(case))
    case = copy.deepcopy(case)
    case['files'] = {'synthetic-project/' + a + '.jsonl': records}
    replay = copy.deepcopy(records)
    if shift:
        for record in replay:
            if 'timestamp' in record:
                record['timestamp'] = record['timestamp'].replace('2026-09-10', '2026-10-01')
    records.append({'type': 'continued-in', 'sessionId': a, 'continuedInSessionId': b})
    if cycle:
        replay.append({'type': 'continued-in', 'sessionId': b, 'continuedInSessionId': a})
    case['files']['synthetic-project/' + b + '.jsonl'] = replay
    return case


def _child():
    row = _r1()
    row.update(uuid='child-R1', requestId='child-request', parentUuid='child-U0', apiBlockIndex=0)
    row['message']['id'] = 'child-message'
    row['message']['usage'] = cases._usage(20, 0, 0, 0, 2)
    row['message']['content'] = []
    row['timestamp'] = '2026-09-10T00:00:16Z'
    user = copy.deepcopy(_records(_b())[0])
    user.update(uuid='child-U0', parentUuid='B-R1-1', timestamp='2026-09-10T00:00:14Z')
    return [user, row]


def _fork_case():
    case = _case([_records(_b())[0], _r1()])
    fork = {'type': 'fork-context-ref', 'agentId': 'child', 'parentSessionId': 'A',
            'parentLastUuid': 'B-R1-1'}
    case['files']['synthetic-project/A/subagents/agent-child.jsonl'] = [
        fork, copy.deepcopy(_records(case)[0]), _r1()] + _child()
    return case


def test_last_block_whole_usage() -> None:
    with corpus(_b()) as (result, _files, _root):
        expect('last block counts', totals(result), (2, 255, 18))
        expect('collapsed blocks', count(result, 'blocks_collapsed'), 1)
        expect('B cached/reasoning', (sum(r['usage']['cached_input_tokens'] for r in result['rows']),
                                    sum(r['usage']['reasoning_output_tokens'] for r in result['rows'])), (90, 7))
        expect('B response intervals', timed(result), [10, 8])
        expect('B active seconds', next(iter(result['families'].values()))['active_s'], 10)
        expect('B tool interval', [t['seconds'] for t in result['tools']], [2])
        expect('B logged duration', [t['logged_duration_ms'] for t in result['turns']], [19000])


def test_last_not_componentwise_max() -> None:
    case = _b()
    records = _records(case)[:3]
    records[2]['message']['usage']['input_tokens'] = 9
    with corpus(_case(records)) as (result, _files, _root):
        expect('input change quarantined', (totals(result), count(result, 'response_input_conflict')),
               ((0, 0, 0), 1))


def test_equal_usage_distinct_ids() -> None:
    first, second = _r1(), _r1()
    second.update(uuid='other-R1', requestId='other-request')
    second['message']['id'] = 'other-message'
    with corpus(_case([first, second])) as (result, _files, _root):
        expect('equal tuples distinct identities', totals(result), (2, 200, 16))


def test_missing_identity() -> None:
    for component in ('requestId', 'id'):
        row = _r1()
        del (row if component == 'requestId' else row['message'])[component]
        with corpus(_case([row])) as (result, _files, _root):
            expect('missing identity ' + component,
                   (totals(result), count(result, 'response_missing_identity')), ((0, 0, 0), 1))


def test_zero_real_response() -> None:
    row = _r1()
    row['message']['usage'] = cases._usage(0, 0, 0, 0, 0)
    with corpus(_case([row])) as (result, _files, _root):
        expect('real zero retained', (totals(result), count(result, 'zero_usage_responses')), ((1, 0, 0), 1))


def test_zero_placeholder_copy() -> None:
    zero = _r1()
    zero['message']['usage'] = cases._usage(0, 0, 0, 0, 0)
    case = _case([_r1()])
    case['files']['synthetic-project/C.jsonl'] = [zero]
    with corpus(case) as (result, _files, _root):
        expect('placeholder uses whole nonzero tuple',
               (totals(result), count(result, 'zero_usage_placeholder_copies')), ((1, 100, 8), 1))


def test_conflicting_nonzero_copy() -> None:
    other = _r1()
    other['message']['usage']['output_tokens'] = 9
    case = _case([_r1()])
    case['files']['synthetic-project/C.jsonl'] = [other]
    with corpus(case) as (result, _files, _root):
        expect('nonzero conflict quarantined',
               (totals(result), count(result, 'response_usage_conflict')), ((0, 0, 0), 1))
        expect('excluded identity once', count(result, 'responses_excluded'), 1)


def test_continuation_owner() -> None:
    with corpus(_continuation(_b(), shift=True)) as (result, files, _root):
        expect('continuation B counts', totals(result), (2, 255, 18))
        expect('continuation copies', count(result, 'cross_file_response_copies'), 2)
        original = next(f for f in files if f['session_id'] == 'A')['source_id']
        expect('ancestor main owns', {r['source_id'] for r in result['rows']}, {original})
        expect('continuation family', set(result['families']), {'A'})


def test_fork_context_owner() -> None:
    with corpus(_fork_case()) as (result, files, _root):
        expect('fork counts', totals(result), (2, 120, 10))
        expect('fork family and streams', (len(result['families']), len(result['by_stream'])), (1, 2))
        parent = next(f for f in files if f['kind'] == 'main')['source_id']
        row = next(r for r in result['rows'] if r['usage']['input_tokens'] == 100)
        expect('parent owns fork context', row['source_id'], parent)
        expect('child uses own task', [v for v in timed(result) if v == 2], [2])  # 16 - 14 = 2.


def test_subagent_continuation_pair() -> None:
    case = _case([])
    case['files']['synthetic-project/A.jsonl'] = [
        {'type': 'continued-in', 'sessionId': 'A', 'continuedInSessionId': 'B'}]
    case['files']['synthetic-project/A/subagents/agent-child.jsonl'] = _child()
    case['files']['synthetic-project/B/subagents/agent-child.jsonl'] = _child()
    with corpus(case) as (result, files, _root):
        expect('child continuation counts', totals(result), (1, 20, 2))
        attribution, _counters = ledger.resolve_families([f for r in files for f in r['links']], files)
        children = [attribution[f['source_id']] for f in files if f['kind'] == 'subagent']
        expect('child continuation stream', len({c['stream_id'] for c in children}), 1)


def test_unrelated_copy_charged_once() -> None:
    user, row = copy.deepcopy(_records(_b())[0]), _r1()
    other_user, other_row = copy.deepcopy(user), copy.deepcopy(row)
    other_user['timestamp'] = '2026-10-01T00:00:00Z'
    other_row['timestamp'] = '2026-10-01T00:00:10Z'
    case = _case([user, row])
    case['files']['synthetic-project/C.jsonl'] = [other_user, other_row]
    with corpus(case) as (result, files, _root):
        expect('unrelated one charge', (totals(result), count(result, 'unrelated_response_copies')),
               ((1, 100, 8), 1))
        original = next(f for f in files if f['session_id'] == 'A')['source_id']
        expect('earlier unrelated source owns', result['rows'][0]['source_id'], original)
        attribution, _ = ledger.resolve_families([], files)
        copies = [ledger.collapse_blocks(f['responses'][0])[0] for f in files]
        owner, classified, _ = ledger.choose_owner(copies, attribution)
        expect('other unrelated timestamp copied',
               [c['timestamp_quality'] for c in classified if c['source_id'] != owner['source_id']], ['copied'])
        expect('unrelated restamps untimed', timed(result), [])


def test_unrelated_copy_owner_tie() -> None:
    case = _case([_r1()])
    case['files']['synthetic-project/C.jsonl'] = [_r1()]
    with corpus(case) as (result, files, _root):
        expect('unrelated tie lexical owner', result['rows'][0]['source_id'], min(f['source_id'] for f in files))
        expect('unrelated tie one charge', totals(result), (1, 100, 8))


def test_continuation_cycle() -> None:
    with corpus(_continuation(_b(), cycle=True)) as (result, _files, _root):
        expect('cycle usage retained', totals(result), (2, 255, 18))
        expect('cycle session unavailable', (len(result['families']), count(result, 'continuation_cycles')), (0, 1))
        expect('cycle row families unavailable', {r['family_id'] for r in result['rows']}, {None})


def test_orphan_subagent() -> None:
    case = _b()
    case['files'] = {'synthetic-project/A/subagents/agent-child.jsonl': _child()}
    with corpus(case) as (result, _files, _root):
        expect('orphan usage retained', totals(result), (1, 20, 2))
        expect('orphan family labelled', (count(result, 'orphan_subagent_families'),
                                        result['families']['A']['orphan_main']), (1, True))


def test_synthetic_positive_usage() -> None:
    row = _r1()
    row['message']['model'] = '<synthetic>'
    with corpus(_case([row])) as (result, _files, _root):
        expect('synthetic never charged', (totals(result), count(result, 'synthetic_records')), ((0, 0, 0), 1))
    # Real synthetic records carry a message id but no requestId (10 of 18 on the
    # development corpus); they are counted by their own reason, not as lost identities.
    del row['requestId']
    with corpus(_case([row])) as (result, _files, _root):
        expect('synthetic without identity', (totals(result), count(result, 'synthetic_records'),
                                              count(result, 'response_missing_identity')), ((0, 0, 0), 1, 0))


def test_api_error_positive_usage() -> None:
    row = _r1()
    row['isApiErrorMessage'] = True
    with corpus(_case([row])) as (result, _files, _root):
        expect('API error never charged', (totals(result), count(result, 'api_error_records')), ((0, 0, 0), 1))


def _partial_case(flag):
    user, row = copy.deepcopy(_records(_b())[0]), _r1()
    row[flag] = True
    row['message']['usage'] = cases._usage(7, 1, 0, 2, 3)
    with corpus(_case([user, row])) as (result, _files, _root):
        expect(flag + ' lower bound', (totals(result), count(result, 'partial_responses'), len(timed(result))),
               ((1, 10, 3), 1, 0))


def test_aborted_positive_usage() -> None:
    _partial_case('isAbortedMidStream')


def test_truncated_real_usage() -> None:
    _partial_case('truncatedAfterOutput')


def test_null_stop_is_not_abort() -> None:
    case = _b()
    for row in _records(case):
        if row['type'] == 'assistant':
            row['message']['stop_reason'] = None
    with corpus(case) as (result, _files, _root):
        expect('null stop counts', (totals(result), count(result, 'response_stop_reason_missing')), ((2, 255, 18), 2))
        expect('null stop completed timing', timed(result), [10, 8])


def test_max_tokens_and_refusal() -> None:
    first, second = _r1(), _r1()
    first['message']['stop_reason'] = 'max_tokens'
    second['message'].update(id='refusal-message', stop_reason='refusal')
    second.update(uuid='refusal-R', requestId='refusal-request', parentUuid='B-R1-1',
                  timestamp='2026-09-10T00:00:20Z')
    with corpus(_case([_records(_b())[0], first, second])) as (result, _files, _root):
        expect('max tokens and refusal counts',
               (totals(result), count(result, 'max_tokens_responses'), count(result, 'refusal_responses')),
               ((2, 200, 16), 1, 1))
        expect('max tokens and refusal timed', len(timed(result)), 2)


def test_compaction_no_charge() -> None:
    case = _b()
    boundary, summary = _r1(), _r1()
    # Stray assistant-shaped fields do not turn a boundary/summary into charged usage.
    boundary.update(type='system', subtype='compact_boundary', uuid='compact-boundary',
                    parentUuid='B-U1', requestId='compact-request', timestamp='2026-09-10T00:00:17Z',
                    compactMetadata={'durationMs': 5000})
    boundary['message']['id'] = 'compact-message'
    summary.update(type='user', uuid='compact-summary', parentUuid='compact-boundary',
                   requestId='summary-request', isCompactSummary=True, timestamp='2026-09-10T00:00:18Z')
    summary['message'].update(id='summary-message', role='user', content='invented summary')
    _records(case)[4]['parentUuid'] = 'compact-summary'
    _records(case)[4:4] = [boundary, summary]
    with corpus(case) as (result, _files, _root):
        expect('compaction no charge', (totals(result), count(result, 'compaction_usage_unavailable')), ((2, 255, 18), 1))
        expect('compaction completion floor', timed(result), [10, 2])  # 20 - summary18 = 2.


def test_cost_state_no_charge() -> None:
    case = _b()
    _records(case).append({'type': 'cost-state', 'sessionId': 'A', 'totalCostUSD': 9000,
                          'modelUsage': {'claude-opus-5-5': {
                              'inputTokens': 9999, 'cacheCreationInputTokens': 8888,
                              'cacheReadInputTokens': 7777, 'outputTokens': 6666,
                              'thinkingTokens': 0, 'webSearchRequests': 0, 'costUSD': 9000}}})
    with corpus(case) as (result, _files, _root):
        expect('cost state no charge', totals(result), (2, 255, 18))
        expect('cost state separate diagnostic', (len(result['cost_checks']), count(result, 'cost_state_count_mismatches')), (1, 1))
    # A map routinely holds a [1m] entry beside the plain one (12 records on the
    # development corpus). Both are kept as distinct checks; B's rows have no [1m]
    # context, so the [1m] entry matches nothing and the plain one mismatches.
    _records(case)[-1]['modelUsage']['claude-opus-5-5[1m]'] = {
        'inputTokens': 1, 'cacheCreationInputTokens': 1, 'cacheReadInputTokens': 1,
        'outputTokens': 1, 'thinkingTokens': 0, 'webSearchRequests': 0, 'costUSD': 1}
    with corpus(case) as (result, _files, _root):
        expect('cost state context entries kept apart',
               (sorted((c['raw_model'], c['context_1m']) for c in result['cost_checks']),
                count(result, 'cost_state_count_mismatches'), count(result, 'cost_state_unmatched_models')),
               ([('claude-opus-5-5', False), ('claude-opus-5-5[1m]', True)], 1, 1))


def test_iterations_not_added() -> None:
    row = _r1()
    usage = row['message']['usage']
    usage['iterations'] = [dict({k: usage[k] for k in worker.REQUIRED}, type='message')]
    with corpus(_case([row])) as (result, _files, _root):
        expect('iterations counted once', totals(result), (1, 100, 8))


def test_iterations_disagree() -> None:
    row = _r1()
    usage = row['message']['usage']
    usage['iterations'] = [dict({k: usage[k] for k in worker.REQUIRED}, type='message')]
    usage['iterations'][0]['output_tokens'] = 9
    with corpus(_case([row])) as (result, _files, _root):
        expect('iteration disagreement diagnostic', (totals(result), count(result, 'iterations_disagree')), ((1, 100, 8), 1))


def test_required_counts_strict() -> None:
    for field in worker.REQUIRED:
        for label, value in (('bool', True), ('string', '2'), ('float', 2.0), ('negative', -1), ('missing', None)):
            row = _r1()
            if label == 'missing':
                del row['message']['usage'][field]
            else:
                row['message']['usage'][field] = value
            reason = ('usage_required_missing' if label == 'missing' else
                      'usage_required_negative' if label == 'negative' else 'usage_required_type_invalid')
            with corpus(_case([row])) as (result, _files, _root):
                expect('strict ' + field + ' ' + label, (totals(result), count(result, reason)), ((0, 0, 0), 1))


def test_optional_reasoning_invalid() -> None:
    row = _r1()
    row['message']['usage']['output_tokens_details']['thinking_tokens'] = 9
    with corpus(_case([row])) as (result, _files, _root):
        expect('invalid reasoning unavailable',
               (totals(result), result['rows'][0]['usage']['reasoning_output_tokens'], count(result, 'reasoning_invalid')),
               ((1, 100, 8), None, 1))


def test_ttl_mismatch() -> None:
    row = _r1()
    row['message']['usage']['cache_creation']['ephemeral_1h_input_tokens'] = 20
    with corpus(_case([row])) as (result, _files, _root):
        expect('TTL mismatch unavailable',
               (totals(result), result['rows'][0]['cache_write_5m'], result['rows'][0]['cache_write_1h'],
                result['rows'][0]['cache_ttl_complete'], count(result, 'cache_ttl_invalid')),
               ((1, 100, 8), None, None, False, 1))


def test_missing_optional_counts() -> None:
    row = _r1()
    for field in ('output_tokens_details', 'speed', 'server_tool_use'):
        del row['message']['usage'][field]
    with corpus(_case([row])) as (result, _files, _root):
        names = ('reasoning_missing', 'speed_missing', 'web_search_count_missing', 'web_fetch_count_missing')
        expect('missing optional counters', [count(result, name) for name in names], [1, 1, 1, 1])
        expect('missing optional core retained', totals(result), (1, 100, 8))
        expect('missing speed never Standard', result['rows'][0]['tier'], None)


def test_actual_model_authoritative() -> None:
    row = _r1()
    row['message']['model'] = 'claude-haiku-4-5-20251001'
    row['requestedModel'] = 'claude-opus-5-5'
    with corpus(_case([row])) as (result, _files, _root):
        actual = result['rows'][0]
        expect('served Haiku authoritative', (actual['model'], actual['requested_model']),
               ('claude-haiku-4-5', 'claude-opus-5-5'))
        # Haiku4.5: (10*1 + 20*1.25 + 30*2 + 40*.1 + 8*5)/1e6 + .01 = .010139.
        # Round 5 prices this retained served model and these exact components.
        expect('served model price inputs',
               (actual['base_input_tokens'], actual['cache_write_5m'], actual['cache_write_1h'],
                actual['usage']['cached_input_tokens'], actual['usage']['output_tokens']), (10, 20, 30, 40, 8))


def test_dated_alias_closed() -> None:
    expect('closed dated alias', [worker._model_name(raw)[0] for raw in
                                ('claude-haiku-4-5-20251001', 'claude-haiku-4-5-20251002')],
           ['claude-haiku-4-5', 'claude-haiku-4-5-20251002'])


def test_context_suffix() -> None:
    row = _r1()
    row['message']['model'] = ' CLAUDE-OPUS-5-5[1m] '
    with corpus(_case([row])) as (result, _files, _root):
        actual = result['rows'][0]
        expect('exact context modifier', (actual['model'], actual['context_1m']), ('claude-opus-5-5', True))
        expect('context same price inputs', totals(result), (1, 100, 8))
    expect('other suffix invalid', worker._model_name('claude-opus-5-5[2m]')[0], 'unknown')


def test_version_irrelevant() -> None:
    case = _b()
    for record in _records(case):
        record['version'] = '0.0.0'
    with corpus(case) as (result, _files, _root):
        expect('version never gates shape', (totals(result), timed(result)), ((2, 255, 18), [10, 8]))


def test_partial_line() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'projects'
        path = cases.write_corpus(root, _case([_r1()]))[0]
        data = path.read_bytes()
        path.write_bytes(data[:-1])
        result = ledger.build([worker.extract(path)])
        expect('partial line withheld', (totals(result), count(result, 'trailing_partial_lines')), ((0, 0, 0), 1))
        path.write_bytes(data)
        result = ledger.build([worker.extract(path)])
        expect('newline includes response', totals(result), (1, 100, 8))


def test_unknown_shape_counted() -> None:
    case = _b()
    _records(case).append({'type': 'future-record', 'hidden': 'invented text'})
    _records(case)[2]['message']['content'].append({'type': 'future-block', 'hidden': 'invented text'})
    _records(case).append({'type': 'attachment', 'attachment': {'type': 'future-attachment', 'hidden': 'invented text'},
                          'timestamp': '2026-09-10T00:00:22Z'})
    with corpus(case) as (result, files, _root):
        expect('unknown record counted', (totals(result), count(result, 'unknown_record_types')), ((2, 255, 18), 1))
    with corpus(case, metrics_only=True) as (result, files, _root):
        expect('metrics-only content empty', (result['content'], files[0]['content'],
                                             any(k.startswith('composition_') for k in result['counters'])), ([], [], False))


def test_filter_after_dedup() -> None:
    with corpus(_continuation(_case([_records(_b())[0], _r1()]), shift=True)) as (result, files, root):
        original = next(f for f in files if f['session_id'] == 'A')
        with open(original['path'], 'ab') as fh:
            fh.write(b'{damaged}\n')
        results = [worker.extract(path) for path in rollout.discover(root)[0]]
        result = ledger.build(results)
        # The original ends in September. The October replay contributes no new row,
        # and the September damage remains available to the later report filter.
        selected = [r for r in result['rows'] if r['local_day'] >= '2026-10-01']
        expect('filter follows global ownership', (len(selected), count(result, 'unparseable_records')), (0, 1))


def test_paths_default() -> None:
    with tempfile.TemporaryDirectory() as temp:
        home = Path(temp).resolve()
        resolved = paths.resolve_paths(env={}, home=home)
        expect('default paths', (resolved['sessions_root'], resolved['account_path'], resolved['state_dir']),
               (home / '.claude/projects', home / '.claude.json', home / '.claude/token-counter'))
        expect('default state files', [resolved[field].name for field in
                                     ('index_path', 'history_path', 'report_path', 'shared_report_path', 'share_state_path')],
               ['index-cache.db', 'history.db', 'report.html', 'report-shared.html', 'claude-share.json'])
        expect('empty config same default', paths.resolve_paths(env={'CLAUDE_CONFIG_DIR': ''}, home=home), resolved)


def test_paths_config_dir() -> None:
    with tempfile.TemporaryDirectory() as temp:
        home, config = Path(temp) / 'home', Path(temp) / 'config'
        resolved = paths.resolve_paths(env={'CLAUDE_CONFIG_DIR': str(config)}, home=home)
        expect('configured paths', (resolved['sessions_root'], resolved['account_path'], resolved['state_dir']),
               (config / 'projects', config / '.claude.json', config / 'token-counter'))
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(config)}):
            expect('compatibility home is Claude', rollout.codex_home(), str(config))


def test_paths_explicit_namespace() -> None:
    with tempfile.TemporaryDirectory() as temp:
        home, root = Path(temp) / 'home', Path(temp) / 'copy/projects'
        resolved = paths.resolve_paths(root, env={'CLAUDE_CONFIG_DIR': str(home)}, home=home)
        # Q10: SHA256 of normcase(resolved root), first 12 hex characters.
        namespace = hashlib.sha256(os.path.normcase(str(root.resolve())).encode('utf-8')).hexdigest()[:12]
        expect('explicit paths', (resolved['sessions_root'], resolved['account_path'], resolved['state_dir']),
               (root, root.parent / '.claude.json', root.parent / 'token-counter' / namespace))
        expect('explicit namespace normalizes dots', paths.state_namespace(root / '..' / 'projects'), namespace)
        expect('different roots separate state', paths.state_namespace(root) != paths.state_namespace(root.parent / 'other'), True)
        expect('within root boundary', (paths.is_within(root / 'child', root), paths.is_within(root.parent / 'projects-other', root)), (True, False))


def test_account_allow_list() -> None:
    with tempfile.TemporaryDirectory() as temp:
        resolved = paths.resolve_paths(Path(temp) / 'projects', env={}, home=temp)
        document = {'oauthAccount': {
            'organizationType': 'claude_max', 'organizationRateLimitTier': 'default_claude_max_5x',
            'subscriptionCreatedAt': '2026-09-01T00:00:00Z', 'emailAddress': 'invented@example.invalid',
            'organizationName': 'Invented organization', 'displayName': 'FORBIDDEN-NAME',
            'accountUuid': 'FORBIDDEN-ID', 'billingType': {'hidden': 'FORBIDDEN-BILLING'},
            'access_token': 'FORBIDDEN-TOKEN'}}
        resolved['account_path'].write_text(json.dumps(document), encoding='utf-8')
        opened, original = [], builtins.open

        def spy(path, *args, **kwargs):
            observed = Path(path).resolve()
            if observed.name == '.credentials.json' or observed != resolved['account_path']:
                raise AssertionError('unexpected account open')
            opened.append(observed)
            return original(path, *args, **kwargs)

        with mock.patch.object(builtins, 'open', spy):
            result = account.read_account(resolved, now=123.0)
            skipped = account.read_account(resolved, no_account=True, now=123.0)
        expect('only resolved account opened', opened, [resolved['account_path']])
        expect('allow-listed account scalars', result, {
            'snapshot': {'observed_at': 123.0, 'organization_type': 'claude_max',
                         'rate_limit_tier': 'default_claude_max_5x', 'current_plan': 'claude:max-5x',
                         'subscription_created_at': 1788220800.0},
            'email': 'invented@example.invalid', 'organization_name': 'Invented organization',
            'skipped': False, 'counters': {}})  # Sep1 2026 UTC, 20697 days * 86400.
        expect('no account skips open', skipped, {'snapshot': None, 'email': None,
                                                 'organization_name': None, 'skipped': True, 'counters': {}})
        document['oauthAccount'].update(organizationType=[], organizationRateLimitTier={},
                                         subscriptionCreatedAt=True, emailAddress=2,
                                         organizationName={'hidden': 'FORBIDDEN-NAME'})
        resolved['account_path'].write_text(json.dumps(document), encoding='utf-8')
        with mock.patch.object(builtins, 'open', spy):
            invalid = account.read_account(resolved, now=123.0)
        # All five allow-listed fields now have a wrong type; none is retained.
        expect('nested and wrong-type account fields rejected',
               (invalid['email'], invalid['organization_name'], invalid['snapshot']['current_plan'],
                invalid['snapshot']['subscription_created_at'], invalid['counters']['account_field_invalid']),
               (None, None, None, None, 5))


def test_account_plan_mapping() -> None:
    samples = [('claude_max', 'default_claude_max_5x', 'claude:max-5x'),
               ('claude_max', 'default_claude_max_20x', 'claude:max-20x'),
               ('claude_pro', None, 'claude:pro'), ('claude_pro', 'future', 'claude:pro'),
               ('claude_pro', 'default_claude_max_5x', None),
               ('claude_pro', 'default_claude_max_20x', None), ('claude_max', None, None),
               ('claude_max', '', None), ('claude_max', 'future', None),
               ('claude_team', 'default_claude_max_5x', None),
               ('claude_enterprise', 'default_claude_max_20x', None), (None, 'default_claude_max_5x', None)]
    expect('plan mapping table', [account.map_plan(a, b)[0] for a, b, _expected in samples],
           [expected for _a, _b, expected in samples])
    expect('unknown Pro tier diagnostic', account.map_plan('claude_pro', 'future')[1], {'account_tier_unrecognized': 1})
    expect('contradictory fields diagnostic', account.map_plan('claude_pro', 'default_claude_max_5x')[1],
           {'account_fields_contradictory': 1, 'account_plan_unknown': 1})
    with tempfile.TemporaryDirectory() as temp:
        resolved = paths.resolve_paths(Path(temp) / 'projects', env={}, home=temp)
        resolved['account_path'].write_text('{"numStartups": 1}', encoding='utf-8')
        expect('no oauth account is unavailable, not invalid',
               account.read_account(resolved, now=1.0)['counters'], {'account_unavailable': 1})


def test_discover_layouts() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'projects'
        expected = cases.write_corpus(root, _fork_case())
        for relative in ('stray.jsonl', 'synthetic-project/extra/deep.jsonl',
                         'synthetic-project/A/other/stray.jsonl'):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'UNEXPECTED-MUST-NOT-BE-READ\n')
        tool_result = root / 'synthetic-project/A/tool-results/private.jsonl'
        tool_result.parent.mkdir(parents=True)
        tool_result.write_bytes(b'TOOL-RESULT-MUST-NOT-BE-READ\n')
        opened, original = [], builtins.open

        def spy(path, *args, **kwargs):
            observed = Path(path).resolve()
            if observed not in expected:
                raise AssertionError('unexpected transcript read')
            opened.append(observed)
            return original(path, *args, **kwargs)

        with mock.patch.object(builtins, 'open', spy):
            discovered, counters = rollout.discover(root)
            results = [worker.extract(path) for path in discovered]
        expect('only transcript layouts discovered', discovered, sorted(expected))
        # Three unexpected JSONL locations; tool-results is never traversed.
        expect('unexpected JSONL counted without reads', (counters, set(opened)), ({'unexpected_jsonl_paths': 3}, set(expected)))
        # Parent R1 (100/8) plus child's own response (20/2), inherited R1 charged once.
        expect('discovered facts charged', totals(ledger.build(results)), (2, 120, 10))


def _429():
    row = _r1()
    row.update(uuid='quota-429', timestamp='2026-09-10T19:30:00Z', isApiErrorMessage=True,
               apiErrorStatus=429, quotaLimits={'status': 'rejected', 'rateLimitType': 'seven_day',
                                               'resetsAt': '2026-09-17T16:00:00Z'})
    row['message']['model'] = '<synthetic>'
    return row


def test_429_is_full_reading() -> None:
    for reset in ('2026-09-17T16:00:00Z', 1789660800):
        row = _429()
        row['quotaLimits']['resetsAt'] = reset
        with corpus(_case([row])) as (result, files, _root):
            fact = files[0]
            expect('429 full fact reading', [(r['kind'], r['percent'], r['source'], r['resets_at']) for r in fact['limits']],
                   [('weekly_all', 100, 'quota_429', 1789660800.0)])  # Sep17 UTC16 = Sep1 +16d +16h.
            expect('429 fact event and assumption',
                   (len(fact['events']), fact['counters'].get('limit_429_assumed_all_models'), totals(result)), (1, 1, (0, 0, 0)))


def test_429_without_weekly_rejection_has_no_percent() -> None:
    for field, value in (('rateLimitType', 'five_hour'), ('status', 'allowed'), ('resetsAt', 'invalid')):
        row = _429()
        row['quotaLimits'][field] = value
        with corpus(_case([row])) as (_result, files, _root):
            expect('429 without weekly rejection ' + field, (len(files[0]['events']), len(files[0]['limits'])), (1, 0))


def test_429_copies_and_server_error() -> None:
    case = _continuation(_case([_429()]))
    case['files']['synthetic-project/C.jsonl'] = [_429()]
    with corpus(case) as (result, _files, _root):
        expect('429 copies one event reading assumption',
               (len(result['events']), len(result['limits']), count(result, 'limit_429_assumed_all_models')), (1, 1, 1))
        # Three UUID copies discard two events and two readings, in separate scopes.
        expect('event and reading copy counters separate',
               (count(result, 'limit_event_copies'), count(result, 'limit_reading_copies')), (2, 2))
    row = _429()
    row['apiErrorStatus'] = 500
    with corpus(_case([row])) as (result, _files, _root):
        expect('500 has no rate facts', (len(result['events']), len(result['limits'])), (0, 0))


def test_usage_report_readings() -> None:
    case = copy.deepcopy(cases.quota_W())
    reading = next(r for r in _records(case) if 'usageReport' in r)
    items = reading['usageReport']['rate_limits']['limits']
    items.extend([dict(items[0], kind='session'), dict(items[0], kind='weekly_scoped',
                                                    scope={'model': {'display_name': 'Invented'}, 'surface': 'code'})])
    with corpus(case) as (result, _files, _root):
        # W has three weekly readings; add one session and one scoped reading. The
        # scope is SHA-256, represented by 32 bytes / 64 hex characters.
        expect('usage report kinds kept separate', sorted(r['kind'] for r in result['limits']),
               ['session', 'weekly_all', 'weekly_all', 'weekly_all', 'weekly_scoped'])
        expect('scope is digest only', [len(r['scope_key']) for r in result['limits'] if r['kind'] == 'weekly_scoped'], [64])


def test_terminal_invalid_and_output_decrease() -> None:
    case = _case(_records(_b())[:3])
    _records(case)[2]['message']['usage']['output_tokens'] = '8'
    with corpus(case) as (result, _files, _root):
        # R1's preceding block: input10+20+30+40=100, output2, thinking1.
        expect('malformed terminal lower bound',
               (totals(result), count(result, 'response_terminal_usage_invalid'), count(result, 'partial_responses')),
               ((1, 100, 2), 1, 1))
    case = _case(_records(_b())[:3])
    _records(case)[2]['message']['usage'].update(output_tokens=1, output_tokens_details={'thinking_tokens': 1})
    with corpus(case) as (result, _files, _root):
        # The terminal is whole, even when output falls 2 -> 1.
        expect('decreasing output uses last tuple', (totals(result), count(result, 'response_output_decreased')), ((1, 100, 1), 1))


def test_timestamps_damage_and_modes() -> None:
    # UTC+8 cancels the eight hours; the Unix origin leaves only the fractional second.
    expect('offset microseconds', worker.epoch('1970-01-01T08:00:00.123456+08:00'), .123456)
    expect('Z milliseconds', worker.epoch('1970-01-01T00:00:00.123Z'), .123)
    expect('invalid and naive stamps', [worker.epoch(v) for v in ('invalid', '2026-09-10T00:00:00', True)], [None, None, None])
    case = _case([{'type': 'mode', 'mode': 'invented'}, _r1()])
    with corpus(case) as (_result, files, _root):
        # Files usually open with unstamped metadata; the source timestamp is the
        # first stamped record's, here R1's end.
        expect('first stamped record timestamp', files[0]['first_record_ts'], '2026-09-10T00:00:10Z')
    case = _case([_r1()])
    _records(case)[0]['timestamp'] = 'invalid'
    with corpus(case) as (result, _files, _root):
        expect('invalid completion retained undated',
               (totals(result), result['rows'][0]['ts'], result['rows'][0]['local_day'],
                count(result, 'record_timestamp_invalid'), count(result, 'undated_responses')),
               ((1, 100, 8), None, None, 1, 1))
    with corpus(_b()) as (full, files, _root):
        metrics_files = [worker.extract(f['path'], metrics_only=True) for f in files]
        metrics = ledger.build(metrics_files)
        def noncontent(value):
            value = copy.deepcopy(value)
            value['content'] = []
            value.pop('_live_content_keys', None)
            value.pop('_live_snapshot_keys', None)
            value['counters'] = {k: v for k, v in value['counters'].items()
                                 if not k.startswith(('composition_', 'images_'))}
            return value
        expect('metrics preserves all noncontent facts', noncontent(metrics), noncontent(full))
        path = Path(files[0]['path'])
        with path.open('ab') as fh:
            fh.write(b'[]\n{invalid}\n')
        damaged = ledger.build([worker.extract(path)])
        # Two appended complete lines are one non-object and one parse failure;
        # neither changes B's two responses or 255/18 recorded counts.
        expect('all complete damage counted', (totals(damaged), count(damaged, 'non_object_records'),
                                               count(damaged, 'unparseable_records')), ((2, 255, 18), 1, 1))
        expect('counter values strict integers', all(type(v) is int and v >= 0 for v in damaged['counters'].values()), True)


def test_optional_dedup_and_model_conflict() -> None:
    case = _case([_r1()])
    del _records(case)[0]['message']['usage']['speed']
    with corpus(_continuation(case)) as (result, _files, _root):
        expect('optional counts once globally', count(result, 'speed_missing'), 1)
    case = _case([_r1()])
    other = _r1()
    other['message']['model'] = 'claude-sonnet-5-5'
    case['files']['synthetic-project/C.jsonl'] = [other]
    with corpus(case) as (result, _files, _root):
        expect('served model conflict quarantined', (totals(result), count(result, 'response_model_conflict')), ((0, 0, 0), 1))


def test_calendar_bodies_and_fingerprint() -> None:
    reference = REPO / 'plugins/token-counter/skills/token-report/scripts/tokencounter/analyze.py'
    current_ast, reference_ast = ast.parse((LIB / 'ledger.py').read_text(encoding='utf-8')), ast.parse(reference.read_text(encoding='utf-8'))
    for name in ('_local_day', '_day_span', '_day', '_iso'):
        a = next(n for n in current_ast.body if isinstance(n, ast.FunctionDef) and n.name == name)
        b = next(n for n in reference_ast.body if isinstance(n, ast.FunctionDef) and n.name == name)
        expect('calendar body ' + name, ast.dump(a), ast.dump(b))
    expect('fingerprint is available', worker.extractor_fingerprint() != 0, True)
    expect('fingerprint modes separate', worker.extractor_fingerprint() != worker.extractor_fingerprint(metrics_only=True), True)
    with mock.patch.object(Path, 'read_bytes', side_effect=OSError):
        expect('unreadable fingerprint disables reuse', worker.extractor_fingerprint(), 0)


def test_shortened_continuation_turns() -> None:
    case = _case(_records(_b()))
    _records(case).append({'type': 'continued-in', 'sessionId': 'A', 'continuedInSessionId': 'B'})
    user, row = copy.deepcopy(_records(_b())[0]), _r1()
    user.update(uuid='next-turn-U', timestamp='2026-09-10T00:01:00Z', parentUuid=None)
    row.update(uuid='next-turn-R', parentUuid='next-turn-U', requestId='next-turn-request',
               timestamp='2026-09-10T00:01:10Z')
    row['message']['id'] = 'next-turn-message'
    case['files']['synthetic-project/B.jsonl'] = [user, row]
    with corpus(case) as (result, _files, _root):
        # B contributes one additional R1 tuple, 100 input/8 output. Its source-local
        # turn zero follows A's turn zero in their one canonical stream.
        expect('short continuation totals', totals(result), (3, 355, 26))
        expect('short continuation turns distinct', [r['turn'] for r in result['rows']], [0, 0, 1])
        expect('short continuation opener facts', [(t['turn'], t['start']) for t in result['turns']],
               [(0, '2026-09-10T00:00:00Z'), (1, '2026-09-10T00:01:00Z')])


def test_conflicting_reading_copies_retained() -> None:
    case = copy.deepcopy(cases.quota_W())
    records = [r for r in _records(case) if 'usageReport' in r][:1]
    case = _case(records)
    conflicting = copy.deepcopy(records[0])
    conflicting['usageReport']['rate_limits']['limits'][0]['percent'] = 21
    case['files']['synthetic-project/C.jsonl'] = [conflicting]
    with corpus(case) as (result, _files, _root):
        # Same UUID and time quote 20% versus 21%; neither can silently replace the other.
        expect('conflicting readings retained', sorted(r['percent'] for r in result['limits']), [20, 21])
        expect('reading revision conflict counted', count(result, 'limit_quote_conflict'), 1)


def test_block_duplicates_missing_index_and_roots() -> None:
    case = _b()
    _records(case).insert(3, copy.deepcopy(_records(case)[2]))
    with corpus(case) as (result, _files, _root):
        expect('duplicate block counted once', (totals(result), count(result, 'in_file_duplicate_blocks'),
                                                count(result, 'blocks_collapsed')), ((2, 255, 18), 1, 1))
    case = _b()
    for record in _records(case):
        record.pop('apiBlockIndex', None)
    with corpus(case) as (result, _files, _root):
        expect('missing block index still collapses usage', totals(result), (2, 255, 18))
    case = _case([_r1(), {'type': 'continued-in', 'sessionId': 'A', 'continuedInSessionId': 'B'}])
    other = _r1()
    other.update(uuid='other-root-R', requestId='other-root-request')
    other['message']['id'] = 'other-root-message'
    case['files']['synthetic-project/C.jsonl'] = [other, {'type': 'continued-in', 'sessionId': 'C', 'continuedInSessionId': 'B'}]
    with corpus(case) as (result, _files, _root):
        # Two distinct R1 responses remain dated; A/C are incompatible roots of B.
        expect('multiple roots retain usage without session',
               (totals(result), len(result['families']), count(result, 'continuation_multiple_roots')), ((2, 200, 16), 0, 1))


def test_effort_and_optional_types() -> None:
    for effort, fallback, expected, reason in ((None, 'xhigh', 'xhigh', None),
                                             ('high', 'max', None, 'effort_conflict'),
                                             ({'hidden': 'invented'}, 'max', None, 'effort_missing')):
        row = _r1()
        row.update(effort=effort, perTurnEffort=fallback)
        with corpus(_case([row])) as (result, _files, _root):
            expect('effort fallback ' + str(expected) + str(reason), result['rows'][0]['effort'], expected)
            if reason:
                expect('effort diagnostic ' + reason, count(result, reason), 1)
    row = _r1()
    row['message']['usage'].update(output_tokens_details=[], speed={'hidden': 'invented'},
                                   inference_geo=['invented'], service_tier=['invented'],
                                   server_tool_use={'web_search_requests': True, 'web_fetch_requests': -1})
    with corpus(_case([row])) as (result, _files, _root):
        names = ('reasoning_invalid', 'speed_unknown', 'inference_geo_unknown', 'optional_metadata_invalid',
                 'web_search_count_invalid', 'web_fetch_count_invalid')
        # Six malformed optional components each contribute one diagnostic. Required
        # R1 is still 10+20+30+40=100 input / 8 output, with speed/geo unknown.
        expect('invalid optional types counted', [count(result, name) for name in names], [1, 1, 1, 1, 1, 1])
        expect('invalid optional types do not alter core', totals(result), (1, 100, 8))
        expect('invalid optional metadata has fixed markers',
               (result['rows'][0]['speed'], result['rows'][0]['inference_geo'], result['rows'][0]['tier']), ('unknown', 'unknown', None))
    row, other = _r1(), _r1()
    row['advisorModel'] = ' CLAUDE-OPUS-5-5 '
    other.update(uuid='advisor-R', requestId='advisor-request', advisorModel={'hidden': 'invented'})
    other['message']['id'] = 'advisor-message'
    with corpus(_case([row, other])) as (result, _files, _root):
        # advisorModel is routine metadata (a model id on 77% of corpus records): kept as a
        # canonical name and counted once per charged response, never as a quality loss.
        expect('advisor model retained and counted',
               (sorted(str(r['advisor_model']) for r in result['rows']), count(result, 'advisor_model_records'),
                count(result, 'optional_metadata_invalid'), totals(result)),
               (['None', 'claude-opus-5-5'], 1, 1, (2, 200, 16)))


def test_changed_file_retries_once() -> None:
    with corpus(_b()) as (_result, files, _root):
        path = files[0]['path']
        size, mtime = rollout.stat_key(path)
        key, changed = (size, mtime), (size + 1, mtime)
        with mock.patch.object(rollout, 'stat_key', side_effect=[key, changed, key, key]) as spy:
            stable = worker.extract(path)
        # Two before/after pairs: the first changes and the second is stable, so four
        # stats, one damaged read and B's unchanged counts. Two changes retain no facts.
        expect('changed read retried once', (spy.call_count, stable['stable_read'],
                                             stable['counters']['file_changed_during_read'],
                                             totals(ledger.build([stable]))), (4, True, 1, (2, 255, 18)))
        with mock.patch.object(rollout, 'stat_key', side_effect=[key, changed, key, changed]) as spy:
            unstable = worker.extract(path)
        expect('unstable read supplies damage only', (spy.call_count, unstable['stable_read'],
                                                     unstable['counters']['file_changed_during_read'],
                                                     unstable['responses']), (4, False, 2, []))


def test_facts_contain_no_content() -> None:
    case = _b()
    _records(case)[0]['message']['content'][0]['text'] = 'FORBIDDEN-PROMPT'
    _records(case)[2]['message']['content'][0]['input'] = {'hidden': 'FORBIDDEN-INPUT'}
    _records(case)[3]['message']['content'][0]['content'] = 'FORBIDDEN-RESULT'
    _records(case)[4]['message']['content'][0]['text'] = 'FORBIDDEN-OUTPUT'
    _records(case)[4]['message']['content'].append({'type': 'thinking', 'thinking': 'FORBIDDEN-THINKING'})
    with corpus(case) as (result, files, _root):
        expect('facts contain no transcript bodies', 'FORBIDDEN-' in json.dumps([result, files]), False)


def test_discover_outside_root_links() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'projects'
        written = cases.write_corpus(root, _case([_r1()]))
        link = root / 'synthetic-project/outside.jsonl'
        link.write_bytes(b'NEVER-READ\n')
        outside = Path(temp) / 'outside.jsonl'
        outside.write_bytes(b'NEVER-READ\n')
        original = Path.resolve

        def resolved(path, *args, **kwargs):
            # Simulate an outside-root file link without needing Windows symlink privileges.
            return original(outside) if path == link else original(path, *args, **kwargs)

        with mock.patch.object(Path, 'resolve', resolved), mock.patch.object(builtins, 'open', side_effect=AssertionError('discovery opened a file')):
            found, counters = rollout.discover(root)
        expect('outside-root links excluded without opening', (found, counters), (written, {'outside_root_links': 1}))


def main() -> int:
    RESULTS.clear()
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith('test_') and callable(fn)]
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            RESULTS.append((name + ' invalid execution', False, exc.__class__.__name__))
    for name, ok, detail in RESULTS:
        print('[%s] %s%s' % ('PASS' if ok else 'FAIL', name, '' if ok else ': ' + detail))
    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    print('\n%d/%d assertions passed (%d tests)' % (passed, len(RESULTS), len(tests)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
