"""Round 4 durable capture and replacement guards; invented fixtures, stdlib only.

Deferred to rounds 6-8: test_rebuild_reextracts_every_live_file, test_live_only_view,
test_token_without_history, test_lost_history_token_still_deletes, test_window_list_*,
test_window_budget_omits_key, test_confirmed_delete_retires_token. Rebuild/no-cache
here mean fresh extraction of every discovered live file followed by capture.

    python -I -S -B scripts/test_claude_retention.py
"""
import builtins
import contextlib
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest import mock
import zlib

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'plugins/token-counter-claude/skills/token-report/scripts/tokencounter'


def _load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(
        name, str(path), submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if 'claude_counter' not in sys.modules:
    _load('claude_counter', LIB / '__init__.py', package=True)
cases = _load('claude_retention_cases', HERE / 'fixtures/claude_cases.py')
from claude_counter import account, history, ledger, models, paths, rollout, worker  # noqa: E402

ENDPOINT = 'http://127.0.0.1:8000/api'
BINDING = 'invented-receipt-generation'
NOW = worker.epoch('2026-10-10T00:00:00Z')
RESULTS = []
RETENTION_TESTS = (
    'test_capture_then_prune', 'test_capture_then_shorten', 'test_removed_terminal_block',
    'test_advance_terminal', 'test_conflicting_revision_quarantined',
    'test_rebuild_preserves_history', 'test_rebuild_shortened_source_keeps_terminal',
    'test_no_cache_preserves_history', 'test_initial_calendar_unknown',
    'test_prepared_snapshot_guard', 'test_missing_contributor_withholds_month',
    'test_decreased_contribution_withholds_month', 'test_other_month_survives_withholding',
    'test_commit_failure_no_send', 'test_corrupt_blob_no_fallback_share',
    'test_calendar_assignment_frozen', 'test_account_snapshot_capture_no_identity',
    'test_no_account_no_snapshot', 'test_unstable_read_keeps_capture',
)


def expect(name, got, expected):
    RESULTS.append((name, got == expected, 'expected %r, got %r' % (expected, got)))


def _b():
    return copy.deepcopy(cases.baseline_B())


def _records(case):
    return next(iter(case['files'].values()))


def _extract(root):
    discovered, _ = rollout.discover(root)
    return [worker.extract(path) for path in discovered]


def totals(view):
    rows = view['rows']
    return (len(rows), sum(r['usage']['input_tokens'] for r in rows),
            sum(r['usage']['output_tokens'] for r in rows),
            sum(r['usage']['cached_input_tokens'] for r in rows),
            sum(r['usage']['reasoning_output_tokens'] or 0 for r in rows))


def count(view, name):
    return view['counters'].get(name, 0)


def _view(store, live=()):
    return ledger.build(live, history=store.load())


def _capture(store, root, case=None):
    if case is not None:
        cases.write_corpus(root, case)
    live = _extract(root)
    store.capture(live, now=NOW + 1)
    store.commit()
    return live, _view(store, live)


@contextlib.contextmanager
def captured(case=None):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'projects'
        case = _b() if case is None else case
        written = cases.write_corpus(root, case)
        live = _extract(root)
        with history.History(Path(temp) / 'state/history.db') as store:
            store.capture(live, now=NOW)
            store.commit()
            yield store, root, written, live, _view(store, live)


def _contributors(view):
    months = {}
    for row in view['rows']:
        if row['local_day'] is None:
            continue
        month = row['local_day'][:7]
        usage = row['usage']
        snapshot = {
            'counts': {'input': usage['input_tokens'], 'cached': usage['cached_input_tokens'],
                       'output': usage['output_tokens'], 'reasoning': usage['reasoning_output_tokens'] or 0},
            'calendar': {k: row[k] for k in history.CALENDAR_FIELDS},
        }
        months.setdefault(month, {'responses': {}, 'sessions': []})['responses'][row['response_key']] = snapshot
    # Selected-session evidence survives a later ranking change.
    for month, evidence in months.items():
        evidence['sessions'] = [{'responses': sorted(evidence['responses']), 'facts': []}]
    return {'months': months, 'windows': []}


def _prepare(store, view, *, confirmed=True):
    contributors = _contributors(view)
    payload = b'{ "schema": 1, "client": {"name":"claude-usage"}, "days": [] }\n'
    ident = store.prepare(payload, contributors, endpoint=ENDPOINT, token_binding=BINDING)
    if confirmed:
        store.confirm(ident, handle='invented-public-handle', now=NOW)
        store.commit()
    return ident, payload, contributors


def _coverage(store, view, binding=BINDING):
    return store.coverage(view, endpoint=ENDPOINT, token_binding=binding)


def _prune(written):
    for path in written:
        path.unlink()


def test_capture_then_prune() -> None:
    with captured() as (store, root, written, _live, _before):
        _prune(written)
        live, view = _capture(store, root)
        coverage = _coverage(store, view, None)
        expect('pruned B retained', (totals(view), coverage['archived_responses']), ((2, 255, 18, 90, 7), 2))
        expect('pruned counters', [count(view, k) for k in (
            'archived_sources', 'archived_responses', 'retained_removed_responses', 'retained_removed_blocks')], [1, 2, 2, 3])
        expect('pruned month shareable', coverage['safe_months'], ['2026-09'])
        expect('pruned tools and turns retained', ([t['seconds'] for t in view['tools']],
                                                  [t['logged_duration_ms'] for t in view['turns']]), ([2], [19000]))
        expect('pruned extract has no live file', live, [])


def test_capture_then_shorten() -> None:
    with captured() as (store, root, written, live, _before):
        case = _b()
        _records(case)[4:] = []  # Remove R2 and the logged turn.
        current, view = _capture(store, root, case)
        expect('shortened B retained', totals(view), (2, 255, 18, 90, 7))
        expect('shortened removed response', (count(view, 'retained_removed_responses'), count(view, 'archived_responses')), (1, 1))
        source = store.load()[0][0]
        expect('live source attributes refreshed', (source['size'], source['last_complete_line'], source['live']),
               (current[0]['size'], 4, True))
        expect('earliest source stamp kept', source['first_record_ts'], live[0]['first_record_ts'])
        expect('idempotent repeated capture', store.db.execute('SELECT COUNT(*) FROM response_copies').fetchone()[0], 2)
        # A shorter source's first visible record is later than an unrelated copy's
        # opener. Ownership still uses the earliest *captured* source stamp.
        owner = live[0]['source_id']
        other = copy.deepcopy(_records(_b())[:3])
        other[0]['timestamp'] = '2026-09-10T00:00:03Z'
        case['files']['synthetic-other/C.jsonl'] = other
        _capture(store, root, case)
        case['files'][next(iter(case['files']))] = [copy.deepcopy(_records(_b())[1])]
        _current, ranked = _capture(store, root, case)
        expect('shortening cannot change unrelated-copy ownership',
               next(r['source_id'] for r in ranked['rows'] if r['usage']['input_tokens'] == 100), owner)


def test_removed_terminal_block() -> None:
    case = _b()
    _records(case)[3:] = []
    with captured(case) as (store, root, _written, _live, _before):
        short = copy.deepcopy(case)
        _records(short)[2:] = []
        _current, view = _capture(store, root, short)
        expect('removed R1 terminal retained', totals(view), (1, 100, 8, 40, 3))
        expect('removed terminal evidence kept', (count(view, 'retained_removed_blocks'),
                                                 len(store.load()[1][0]['blocks'])), (1, 2))
        # A newly found earlier block cannot choose a smaller terminal, either.
        previous = store.load()[1][0]
        earlier = copy.deepcopy(previous)
        earlier['blocks'] = [dict(copy.deepcopy(previous['blocks'][0]),
                                  record_key=worker._digest(['invented-earlier']), api_block_index=None,
                                  ts='2026-09-10T00:00:04Z')]
        merged, _ = history.merge_response_copy(previous, earlier)
        expect('new earlier block cannot retreat terminal', merged['terminal_record_key'], previous['terminal_record_key'])


def test_advance_terminal() -> None:
    with captured() as (store, root, _written, _live, _before):
        case = _b()
        block = copy.deepcopy(_records(case)[2])
        block.update(uuid='B-R1-2', parentUuid='B-R1-1', apiBlockIndex=2,
                     timestamp='2026-09-10T00:00:11Z')
        block['message']['usage']['output_tokens'] = 10
        block['message']['usage']['output_tokens_details']['thinking_tokens'] = 4
        block['message']['usage']['server_tool_use']['web_search_requests'] = 0
        block['message']['content'] = []
        _records(case).insert(3, block)
        current, view = _capture(store, root, case)
        expect('new later terminal advances whole tuple', totals(view), (2, 255, 20, 90, 8))
        r1 = next(r for r in view['rows'] if r['usage']['input_tokens'] == 100)
        expect('new terminal optional fields also advance', r1['web_search_requests'], 0)
        # Re-reading just the old terminal must not roll the newly captured one back.
        _current, shorter = _capture(store, root, _b())
        expect('advanced terminal survives earlier reextraction', totals(shorter), (2, 255, 20, 90, 8))


def test_conflicting_revision_quarantined() -> None:
    with captured() as (store, root, _written, _live, before):
        _prepare(store, before)
        case = _b()
        _records(case)[2]['uuid'] = 'changed-captured-terminal-uuid'
        _current, view = _capture(store, root, case)
        coverage = _coverage(store, view)
        expect('changed captured block quarantines identity',
               (totals(view), count(view, 'history_conflicting_revisions'), coverage['safe_months']),
               ((1, 155, 10, 50, 4), 1, []))
        expect('revision withholds submitted month', coverage['withheld_months'],
               {'2026-09': [history.MONTH_REASON.format(month='2026-09')]})
        copy_fact = next(c for c in store.load()[1] if len(c['blocks']) == 2)
        expect('both revision and original survive', (len(copy_fact['revisions']), copy_fact['conflicting']), (1, True))
        _current, again = _capture(store, root, _b())
        expect('original replay cannot clear quarantine', (totals(again), count(again, 'history_conflicting_revisions')),
               ((1, 155, 10, 50, 4), 1))
        original, _ = history.merge_response_copy(None, _live[0]['responses'][0])
        edited = copy.deepcopy(original)
        edited['blocks'][-1]['response_metadata']['effort'] = 'max'
        merged, _ = history.merge_response_copy(original, edited)
        expect('metadata-only block revision also retained', (merged['conflicting'], len(merged['revisions'])), (True, 1))


def test_rebuild_preserves_history() -> None:
    with captured() as (store, root, written, _live, _before):
        _prune(written)
        _capture(store, root)  # Fresh discovery/extraction, no index reuse.
        with history.History(store.path) as reopened:
            live, view = _capture(reopened, root)
            expect('rebuild after prune retains B', (totals(view), count(view, 'archived_responses')),
                   ((2, 255, 18, 90, 7), 2))
            expect('rebuild keeps history UUID', reopened.history_uuid, store.history_uuid)
            expect('rebuild live files freshly extracted', live, [])


def test_rebuild_shortened_source_keeps_terminal() -> None:
    with captured() as (store, root, _written, live, _before):
        case = _b()
        case['files'] = {next(iter(case['files'])): [copy.deepcopy(_records(case)[1])]}
        current, view = _capture(store, root, case)
        expect('full reextract shortened source keeps terminal', totals(view), (2, 255, 18, 90, 7))
        expect('short rebuild keeps removed blocks and row',
               (count(view, 'retained_removed_blocks'), count(view, 'retained_removed_responses')), (2, 1))
        expect('short rebuild source rank remains earliest', store.load()[0][0]['first_record_ts'], live[0]['first_record_ts'])
        expect('short rebuild refreshed file really has block0 only', len(current[0]['responses'][0]['blocks']), 1)


def test_no_cache_preserves_history() -> None:
    with captured() as (store, root, written, _live, before):
        _prepare(store, before)
        _prune(written)
        _current, view = _capture(store, root)
        expect('no-cache after prune retains B', totals(view), (2, 255, 18, 90, 7))
        expect('no-cache keeps receipts', (len(store.load()[2]['submissions']), _coverage(store, view)['safe_months']),
               (1, ['2026-09']))
        expect('no disposable cache created', list(store.path.parent.glob('index*')), [])


def test_initial_calendar_unknown() -> None:
    with captured() as (store, root, _written, _live, view):
        coverage = _coverage(store, view, None)
        expect('initial calendar remains unknown', (coverage['calendar_completeness'], coverage['safe_months']),
               ('unknown', ['2026-09']))
        expect('initial capture committed and shareable',
               (coverage['history_available'], coverage['history_committed'], coverage['token_bound']), (True, True, True))
        expect('SQL tables are durable schema1', {r[0] for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}, {'meta', 'sources', 'response_copies', 'facts', 'submissions'})
        expect('round4 meta has only schema and UUID', set(dict(store.db.execute('SELECT k,v FROM meta'))), {'schema', 'history_uuid'})
        case = _b()
        _records(case).append(cases.assistant_record('uncaptured-message', 'uncaptured-request',
                                                    '2026-09-10T00:00:30Z', cases._usage(20, 0, 0, 0, 2),
                                                    uuid='uncaptured-R'))
        cases.write_corpus(root, case)
        uncaptured = _view(store, _extract(root))
        expect('new live evidence must be durably captured before sharing',
               _coverage(store, uncaptured, None)['safe_months'], [])
        original_uuid = store.history_uuid
        store.db.execute("UPDATE meta SET v='99' WHERE k='schema'")
        store.db.commit()
        with history.History(store.path) as unsupported:
            expect('unsupported history schema reported', unsupported.counters, {'history_schema_unsupported': 1})
            expect('unsupported schema never reset', (dict(unsupported.db.execute('SELECT k,v FROM meta')),
                                                     unsupported.db.execute('SELECT COUNT(*) FROM response_copies').fetchone()[0]),
                   ({'schema': '99', 'history_uuid': original_uuid}, 2))


def test_prepared_snapshot_guard() -> None:
    with captured() as (store, root, written, _live, before):
        ident, payload, contributors = _prepare(store, before, confirmed=False)
        receipt = store.load()[2]['submissions'][0]
        expect('preparation exact bytes and snapshot durable', (receipt['payload'], receipt['contributors'], receipt['status']),
               (payload, contributors, 'prepared'))
        damaged = copy.deepcopy(before)
        damaged['rows'] = damaged['rows'][:1]
        damaged['_retention']['captured'] = {}  # Isolate the prepared-receipt guard.
        decision = history.month_coverage(damaged, [receipt])
        expect('prepared receipt guards possible submission', decision['safe_months'], [])
        hashes = store.db.execute('SELECT payload_sha256,contributors_sha256 FROM submissions').fetchone()
        store.mark_unknown(ident, now=NOW)
        store.commit()
        expect('unknown changes status only', (store.db.execute('SELECT payload_sha256,contributors_sha256 FROM submissions').fetchone(),
                                              store.load()[2]['submissions'][0]['status']), (hashes, 'unknown'))
        _prune(written)
        _current, view = _capture(store, root)
        with history.History(store.path) as reopened:
            expect('timeout then prune replaces with retained B',
                   (totals(_view(reopened)), _coverage(reopened, _view(reopened))['safe_months']),
                   ((2, 255, 18, 90, 7), ['2026-09']))
            retained = reopened.load()[2]['submissions'][0]
            expect('receipt survived reopen byte for byte', retained['payload'], payload)
        wrong = store.coverage(view, endpoint=ENDPOINT + '/other', token_binding=BINDING)
        expect('endpoint-specific binding cannot be reused', (wrong['safe_months'], wrong['window_reasons']),
               ([], [history.TOKEN_REASON]))
        # Round 4 stores/reproduces evidence; it neither builds nor compares a window
        # list. Use W's sourced readings and row snapshots directly for this guard.
        _current, with_w = _capture(store, root, cases.quota_W())
        contributors = _contributors(with_w)
        span_rows = [r for r in with_w['rows'] if r['ts'] in ('2026-09-10T18:30:00Z', '2026-09-10T19:00:00Z')]
        contributions = contributors['months']['2026-09']['responses']
        window = {'window_key': worker._digest(['invented-window-W']),
                  'rows': [r['response_key'] for r in span_rows],
                  'responses': {r['response_key']: contributions[r['response_key']] for r in span_rows},
                  'readings': copy.deepcopy(with_w['limits'])}
        contributors['windows'] = [window]
        window_id = store.prepare(payload, contributors, endpoint=ENDPOINT, token_binding=BINDING)
        for result in _current:
            Path(result['path']).unlink()
        _current, retained = _capture(store, root)
        expect('retained window evidence reproduced after prune', _coverage(store, retained)['windows_replace_safe'], True)
        evidence = next(r for r in store.load()[2]['submissions'] if r['submission_id'] == window_id)['contributors']['windows']
        expect('retained window evidence is exact', evidence, [window])
        damaged = copy.deepcopy(retained)
        damaged['limits'] = []
        coverage = _coverage(store, damaged)
        expect('missing retained reading prevents window replacement',
               (coverage['windows_replace_safe'], coverage['window_reasons']), (False, [history.WINDOW_REASON]))


def test_missing_contributor_withholds_month() -> None:
    with captured() as (store, _root, _written, _live, view):
        _prepare(store, view)
        damaged = copy.deepcopy(view)
        damaged['rows'] = damaged['rows'][:1]  # R2 is unavailable.
        coverage = _coverage(store, damaged)
        expect('missing submitted contributor withholds whole month', coverage['safe_months'], [])
        expect('missing contributor counter and exact reason',
               (count(damaged, 'months_withheld'), count(damaged, 'months_withheld_missing_contributors'), coverage['withheld_months']),
               (1, 1, {'2026-09': [history.MONTH_REASON.format(month='2026-09')]}))
        # Selected-session evidence matters even when it is no longer selected.
        receipts = copy.deepcopy(store.load()[2]['submissions'])
        evidence = receipts[0]['contributors']['months']['2026-09']
        evidence['sessions'][0]['facts'] = [{'kind': 'invented', 'fact_key': 'unavailable',
                                           'source_id': 'unavailable', 'sha256': '0' * 64}]
        expect('previous selected-session evidence required', history.month_coverage(view, receipts)['safe_months'], [])


def test_decreased_contribution_withholds_month() -> None:
    with captured() as (store, _root, _written, _live, view):
        _prepare(store, view)
        for field in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens'):
            damaged = copy.deepcopy(view)
            damaged['rows'][0]['usage'][field] -= 1
            coverage = _coverage(store, damaged)
            expect('decreased ' + field + ' withholds month', coverage['safe_months'], [])
            expect('decreased ' + field + ' counted once', count(damaged, 'months_withheld_decreased_contributions'), 1)


def test_other_month_survives_withholding() -> None:
    with captured() as (store, root, _written, _live, before):
        _prepare(store, before)
        case = _b()
        october = cases.assistant_record('october-message', 'october-request', '2026-10-01T00:00:10Z',
                                         cases._usage(20, 0, 0, 0, 2), uuid='october-R')
        case['files']['synthetic-project-october/october-session.jsonl'] = [october]
        _current, view = _capture(store, root, case)
        view['rows'] = [r for r in view['rows'] if r['usage']['input_tokens'] != 155]
        coverage = _coverage(store, view)
        expect('other month survives withholding', coverage['safe_months'], ['2026-10'])
        expect('only damaged September withheld', (list(coverage['withheld_months']), count(view, 'months_withheld')), (['2026-09'], 1))
        expect('October captured counts', totals({'rows': [r for r in view['rows'] if r['local_day'].startswith('2026-10')]}),
               (1, 20, 2, 0, 0))


def test_commit_failure_no_send() -> None:
    with captured() as (store, root, _written, live, view):
        store.capture(live, now=NOW + 1)
        real_db = store.db

        class FailingCommit:
            def execute(self, *args):
                return real_db.execute(*args)

            def commit(self):
                raise sqlite3.OperationalError('invented commit failure')

        with mock.patch.object(store, 'db', FailingCommit()):
            store.commit()
            coverage = _coverage(store, _view(store, live), None)
        expect('commit failure prevents any safe month', coverage['safe_months'], [])
        expect('commit failure recorded and uncommitted', (store.history_committed, store.counters.get('history_commit_failed')), (False, 1))
        attempted = False
        try:
            store.prepare(b'{}', _contributors(view), endpoint=ENDPOINT, token_binding=BINDING)
            attempted = True
        except ValueError:
            pass
        expect('uncommitted history cannot prepare a send', attempted, False)
        real_db.rollback()


def test_corrupt_blob_no_fallback_share() -> None:
    with captured() as (store, _root, _written, live, before):
        _prepare(store, before)
        store.db.execute("UPDATE response_copies SET payload_sha256=? WHERE response_key=?",
                         ('0' * 64, before['rows'][1]['response_key']))
        store.db.commit()
        intact, counters = store.check_integrity()
        fallback = _view(store, live)
        coverage = _coverage(store, fallback)
        expect('corrupt digest disables fallback sharing', (intact, coverage['safe_months']), (False, []))
        expect('corrupt history warning counted', counters, {'history_integrity_failed': 1})
        expect('live lower bound still reportable', totals(fallback), (2, 255, 18, 90, 7))
        expect('corruption never deletes evidence', store.db.execute('SELECT COUNT(*) FROM response_copies').fetchone()[0], 2)


def _utc_span(day):
    start = worker.epoch(day + 'T00:00:00Z')
    return start, start + 86400


def test_calendar_assignment_frozen() -> None:
    case = _b()
    for record in _records(case):
        record['timestamp'] = record['timestamp'].replace('2026-09-10T00:00', '2026-09-30T23:59')
    with mock.patch.object(ledger, '_day', lambda ts, fallback=None: ts[:10]), mock.patch.object(ledger, '_day_span', _utc_span):
        with captured(case) as (store, root, _written, _live, before):
            _prepare(store, before)
            original = [{k: r[k] for k in history.CALENDAR_FIELDS} for r in before['rows']]
            with mock.patch.object(ledger, '_day', lambda ts, fallback=None: '2026-10-01'):
                _current, view = _capture(store, root, case)
                coverage = _coverage(store, view)
            expect('captured calendar frozen across host zone change',
                   ([{k: r[k] for k in history.CALENDAR_FIELDS} for r in view['rows']], coverage['safe_months']),
                   (original, ['2026-09']))
            expect('calendar change is diagnostic', count(view, 'calendar_context_changed'), 2)
            damaged = copy.deepcopy(view)
            damaged['rows'][0]['calendar_signature'] = 'changed-calendar-signature'
            expect('calendar guard rejects changed assignment', _coverage(store, damaged)['safe_months'], [])
            expect('calendar change withholding counted', count(damaged, 'months_withheld_calendar_change'), 1)


def test_account_snapshot_capture_no_identity() -> None:
    case = _b()
    records = _records(case)
    records[0]['message']['content'][0]['text'] = 'FORBIDDEN-PROMPT'
    records[2]['message']['content'][0].update(name='FORBIDDEN-TOOL-NAME', input={'x': 'FORBIDDEN-ARGUMENTS'})
    records[3]['message']['content'][0]['content'] = 'FORBIDDEN-TOOL-RESULT'
    records[4]['message']['content'] = [{'type': 'text', 'text': 'FORBIDDEN-OUTPUT'},
                                       {'type': 'thinking', 'thinking': 'FORBIDDEN-THINKING'}]
    case['files'] = {'FORBIDDEN-PATH/FORBIDDEN-SESSION.jsonl': records}
    with captured(case) as (store, root, _written, live, _before):
        resolved = paths.resolve_paths(root, env={}, home=root.parent)
        resolved['account_path'].write_text(json.dumps({'oauthAccount': {
            'emailAddress': 'FORBIDDEN-EMAIL', 'organizationName': 'FORBIDDEN-ACCOUNT-NAME',
            'organizationType': 'claude_max', 'organizationRateLimitTier': 'default_claude_max_5x',
            'subscriptionCreatedAt': '2026-01-01T00:00:00Z', 'extra': 'FORBIDDEN-ACCOUNT-EXTRA'}}), encoding='utf-8')
        original_open = builtins.open

        def permitted_open(path, *args, **kwargs):
            if str(path).endswith('.credentials.json'):
                raise AssertionError('credentials open attempted')
            return original_open(path, *args, **kwargs)

        with mock.patch.object(builtins, 'open', permitted_open):
            info = account.read_account(resolved, now=NOW)
            store.capture(live, account=info['snapshot'], now=NOW)
            store.commit()
        snapshots = store.load()[3]
        expect('account five-field observation captured', snapshots, [{
            'observed_at': NOW, 'organization_type': 'claude_max', 'rate_limit_tier': 'default_claude_max_5x',
            'current_plan': 'claude:max-5x', 'subscription_created_at': worker.epoch('2026-01-01T00:00:00Z')}])
        blobs = [zlib.decompress(row[0]) for table in ('sources', 'response_copies', 'facts')
                 for row in store.db.execute('SELECT payload FROM ' + table)]
        expect('capture blobs contain no identity or text sentinels', any(b'FORBIDDEN-' in blob for blob in blobs), False)
        record = store.db.execute("SELECT fact_key,source_id FROM facts WHERE kind='account_snapshot'").fetchone()
        expect('account snapshot canonical digest and reserved source', record, (worker._digest(info['snapshot']), 'account'))
        expect('account observations enter ledger', _view(store, live)['account_snapshots'], snapshots)


def test_no_account_no_snapshot() -> None:
    with captured() as (store, root, _written, live, _before):
        resolved = paths.resolve_paths(root, env={}, home=root.parent)
        with mock.patch.object(builtins, 'open', side_effect=AssertionError('account opened')) as spy:
            info = account.read_account(resolved, no_account=True, now=NOW)
            store.capture(live, account=info['snapshot'], now=NOW)
            store.commit()
        expect('no-account writes no snapshot', (spy.call_count, store.load()[3]), (0, []))
        saved = {'observed_at': NOW, 'organization_type': 'claude_pro', 'rate_limit_tier': None,
                 'current_plan': 'claude:pro', 'subscription_created_at': None}
        store.capture(live, account=saved, now=NOW)
        store.commit()
        store.capture(live, account=None, now=NOW)
        store.commit()
        expect('no-account retains earlier snapshots', store.load()[3], [saved])


def test_unstable_read_keeps_capture() -> None:
    with captured() as (store, _root, written, live, _before):
        before = store.db.execute('SELECT * FROM sources').fetchall()
        copies = store.db.execute('SELECT * FROM response_copies').fetchall()
        size, mtime = rollout.stat_key(written[0])
        key, changed = (size, mtime), (size + 1, mtime)
        with mock.patch.object(rollout, 'stat_key', side_effect=[key, changed, key, changed]):
            unstable = worker.extract(written[0])
        expect('unstable extractor provides no facts', (unstable['stable_read'], unstable['responses']), (False, []))
        store.capture([unstable], now=NOW + 1)
        store.commit()
        view = _view(store, [unstable])
        expect('unstable read preserves B and reports damage', (totals(view), count(view, 'file_changed_during_read')),
               ((2, 255, 18, 90, 7), 2))
        expect('unstable capture refreshes no source or copies',
               (store.db.execute('SELECT * FROM sources').fetchall(), store.db.execute('SELECT * FROM response_copies').fetchall()),
               (before, copies))
        expect('unstable counters survive plain load without live results', count(_view(store), 'file_changed_during_read'), 2)


def main() -> int:
    RESULTS.clear()
    for name in RETENTION_TESTS:
        try:
            globals()[name]()
        except Exception as exc:
            RESULTS.append((name + ' invalid execution', False, exc.__class__.__name__))
    for name, ok, detail in RESULTS:
        print('[%s] %s%s' % ('PASS' if ok else 'FAIL', name, '' if ok else ': ' + detail))
    passed = sum(ok for _name, ok, _detail in RESULTS)
    print('\n%d/%d assertions passed (%d tests)' % (passed, len(RESULTS), len(RETENTION_TESTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
