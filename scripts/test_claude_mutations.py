"""Paired ledger mutations: baseline, execution, and a designated failed assertion.

Each manifest entry names the pure decision it protects. A crash (including one contained
by extraction), import error, baseline failure or unrelated assertion is invalid evidence.
The patch is in memory and restored after each target; no source files are rewritten.

    python -B scripts/test_claude_mutations.py --section ledger
"""
import argparse
import copy
import importlib.util
import inspect
from pathlib import Path
import sys

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('test_claude_ledger', str(Path(__file__).with_name('test_claude_ledger.py')))
tests = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tests
spec.loader.exec_module(tests)
ledger, worker, rollout = tests.ledger, tests.worker, tests.rollout

MANIFEST = {}


def paired(test, function, mutation, assertion):
    def decorate(factory):
        MANIFEST[test] = {'function': function, 'mutation': mutation,
                          'assertion': assertion, 'factory': factory}
        return factory
    return decorate


@paired('test_last_block_whole_usage', 'ledger.collapse_blocks', 'select first valid block', 'last block counts')
def _first_terminal(original):
    def changed(copy):
        result, counters = original(copy)
        valid = [b for b in result['blocks'] if b['usage'] is not None]
        if valid:
            result['terminal_record_key'] = valid[0]['record_key']
        return result, counters
    return changed


@paired('test_last_not_componentwise_max', 'ledger.collapse_blocks', 'accept changed prompt components', 'input change quarantined')
def _allow_input_change(original):
    def changed(copy):
        result, counters = original(copy)
        result['quality_flags'] = [f for f in result['quality_flags'] if f != 'response_input_conflict']
        counters.pop('response_input_conflict', None)
        return result, counters
    return changed


@paired('test_equal_usage_distinct_ids', 'worker._response_identity', 'deduplicate by usage equality', 'equal tuples distinct identities')
def _usage_identity(original):
    return lambda record: worker._digest(record['message']['usage'])


@paired('test_missing_identity', 'worker._response_identity', 'invent absent request identity', 'missing identity requestId')
def _missing_request(original):
    def changed(record):
        record = copy.deepcopy(record)
        record.setdefault('requestId', 'invented-request')
        return original(record)
    return changed


@paired('test_zero_real_response', 'ledger.choose_owner', 'discard every all-zero response', 'real zero retained')
def _drop_zero(original):
    def changed(copies, families):
        owner, classified, counters = original(copies, families)
        if owner is not None and not any(ledger._usage_tuple(ledger._terminal(owner)['usage'])):
            owner = None
        return owner, classified, counters
    return changed


@paired('test_zero_placeholder_copy', 'ledger.choose_owner', 'prefer the zero placeholder', 'placeholder uses whole nonzero tuple')
def _prefer_zero(original):
    def changed(copies, families):
        zero = [c for c in copies if ledger._terminal(c) is not None
                and not any(ledger._usage_tuple(ledger._terminal(c)['usage']))]
        return original(zero or copies, families)
    return changed


@paired('test_conflicting_nonzero_copy', 'ledger.choose_owner', 'force conflicting outputs to agree', 'nonzero conflict quarantined')
def _overwrite_conflict(original):
    def changed(copies, families):
        copies = copy.deepcopy(copies)
        output = ledger._terminal(copies[0])['usage']['output']
        for c in copies:
            ledger._terminal(c)['usage']['output'] = output
        return original(copies, families)
    return changed


def _prefer_continuation(original):
    def changed(copy, families):
        rank = original(copy, families)
        return (-rank[0], -rank[1], rank[2], rank[3])
    return changed


paired('test_continuation_owner', 'ledger._owner_rank', 'prefer continuation over ancestor',
       'ancestor main owns')(_prefer_continuation)


@paired('test_fork_context_owner', 'ledger._owner_rank', 'prefer inherited fork stream over parent', 'fork family and streams')
def _prefer_fork(original):
    def changed(copy, families):
        rank = original(copy, families)
        return (0 if families[copy['source_id']]['kind'] == 'subagent' else 1, rank[1], rank[2], rank[3])
    return changed


@paired('test_subagent_continuation_pair', 'ledger.resolve_families', 'ignore subagent continuation links', 'child continuation stream')
def _separate_children(original):
    return lambda links, sources: original([], sources)


@paired('test_unrelated_copy_charged_once', 'ledger._group_copies', 'charge each unrelated source separately', 'unrelated one charge')
def _source_identity(original):
    def changed(copies):
        out = {}
        for c in copies:
            out.setdefault(c['response_key'] + ':' + c['source_id'], []).append(c)
        return out
    return changed


@paired('test_unrelated_copy_owner_tie', 'ledger._source_rank', 'reverse lexical source tie', 'unrelated tie lexical owner')
def _reverse_source(original):
    def changed(info):
        rank = original(info)
        return rank[:2] + (tuple(-ord(c) for c in rank[2]),)
    return changed


@paired('test_continuation_cycle', 'ledger.resolve_families', 'erase the closing cycle edge', 'cycle session unavailable')
def _erase_cycle(original):
    return lambda links, sources: original([l for l in links if l['from_session'] != 'B'], sources)


@paired('test_orphan_subagent', 'ledger.resolve_families', 'invent main evidence for orphan', 'orphan family labelled')
def _hide_orphan(original):
    def changed(links, sources):
        result, counters = original(links, sources)
        counters.pop('orphan_subagent_families', None)
        for info in result.values():
            info['orphan_main'] = False
        return result, counters
    return changed


@paired('test_synthetic_positive_usage', 'worker._chargeable', 'charge synthetic positive usage', 'synthetic never charged')
def _charge_synthetic(original):
    return lambda record: None if record['message'].get('model') == '<synthetic>' else original(record)


@paired('test_api_error_positive_usage', 'worker._chargeable', 'charge API-error positive usage', 'API error never charged')
def _charge_error(original):
    return lambda record: None if record.get('isApiErrorMessage') is True else original(record)


@paired('test_aborted_positive_usage', 'worker._partial', 'treat abortion as complete', 'isAbortedMidStream lower bound')
def _complete_abort(original):
    return lambda aborted, truncated: truncated


@paired('test_truncated_real_usage', 'worker._partial', 'treat truncation as complete', 'truncatedAfterOutput lower bound')
def _complete_truncation(original):
    return lambda aborted, truncated: aborted


@paired('test_null_stop_is_not_abort', 'ledger._timing_anchor', 'exclude missing stop reason from timing', 'null stop completed timing')
def _null_stop_abort(original):
    return lambda copy: None if copy['stop_reason'] is None else original(copy)


@paired('test_max_tokens_and_refusal', 'ledger._timing_anchor', 'exclude completed output-limit and refusal responses', 'max tokens and refusal timed')
def _refusal_abort(original):
    return lambda copy: None if copy['stop_reason'] in ('max_tokens', 'refusal') else original(copy)


@paired('test_compaction_no_charge', 'worker._record_kind', 'charge compaction boundary and summary', 'compaction no charge')
def _compaction_response(original):
    return lambda record: ('assistant' if record.get('isCompactSummary') is True
                           or record.get('subtype') == 'compact_boundary' else original(record))


@paired('test_cost_state_no_charge', 'ledger._response_copies', 'add running cost-state counts as response usage', 'cost state no charge')
def _cost_response(original):
    def changed(result):
        out = copy.deepcopy(original(result))
        for check in result['cost_checks']:
            if check['source'] != 'cost_state' or not out:
                continue
            manufactured = copy.deepcopy(out[0])
            manufactured['response_key'] = worker._digest(['cost-state', check['record_key']])
            manufactured['raw_model'] = check['model']
            for block in manufactured['blocks']:
                block['usage'].update({field: check['counts'][field]
                                       for field in ('base_input', 'creation', 'reads', 'output')})
            out.append(manufactured)
        return out
    return changed


@paired('test_iterations_not_added', 'worker._required_usage', 'add iteration counts to top-level counts', 'iterations counted once')
def _add_iterations(original):
    def changed(value):
        required, reason = original(value)
        if reason is None and isinstance(value.get('iterations'), list):
            for item in value['iterations']:
                extra, error = original(item)
                if error is None:
                    required = tuple(a + b for a, b in zip(required, extra))
        return required, reason
    return changed


@paired('test_iterations_disagree', 'worker._iterations', 'claim all iterations agree', 'iteration disagreement diagnostic')
def _agree_iterations(original):
    def changed(value, required):
        count, agree, flags = original(value, required)
        return count, True, [f for f in flags if f != 'iterations_disagree']
    return changed


@paired('test_required_counts_strict', 'worker._required_usage', 'coerce required bool/string/float/negative counts', 'strict input_tokens bool')
def _coerce_counts(original):
    def changed(value):
        if isinstance(value, dict):
            value = dict(value)
            for field in worker.REQUIRED:
                value[field] = abs(int(value.get(field, 0)))
        return original(value)
    return changed


@paired('test_optional_reasoning_invalid', 'worker._optional_count', 'accept thinking greater than output', 'invalid reasoning unavailable')
def _thinking_ceiling(original):
    return lambda box, field, missing, invalid, ceiling=None: original(box, field, missing, invalid)


@paired('test_ttl_mismatch', 'worker._ttl', 'accept TTL split without checking creation total', 'TTL mismatch unavailable')
def _ttl_sum(original):
    def changed(value, creation):
        box = value.get('cache_creation')
        return ((box['ephemeral_5m_input_tokens'], box['ephemeral_1h_input_tokens'], True, None)
                if isinstance(box, dict) else original(value, creation))
    return changed


@paired('test_missing_optional_counts', 'worker._usage_fact', 'invent zero optional measurements and Standard speed', 'missing optional counters')
def _optional_defaults(original):
    def changed(value):
        value = copy.deepcopy(value)
        value.setdefault('output_tokens_details', {'thinking_tokens': 0})
        value.setdefault('server_tool_use', {'web_search_requests': 0, 'web_fetch_requests': 0})
        value.setdefault('speed', 'standard')
        return original(value)
    return changed


@paired('test_actual_model_authoritative', 'worker._served_model', 'use requested model instead of served model', 'served Haiku authoritative')
def _requested_model(original):
    return lambda record: record.get('requestedModel') or original(record)


@paired('test_dated_alias_closed', 'worker._model_name', 'strip arbitrary dated model suffixes', 'closed dated alias')
def _any_date(original):
    def changed(raw):
        model, safe, context = original(raw)
        if safe is not None:
            model = worker.re.sub(r'-\d{8}$', '', model)
        return model, safe, context
    return changed


@paired('test_context_suffix', 'worker._model_name', 'discard the context modifier', 'exact context modifier')
def _lose_context(original):
    return lambda raw: original(raw.replace('[1m]', '') if isinstance(raw, str) else raw)


@paired('test_version_irrelevant', 'worker._record_kind', 'gate assistant shapes on version', 'version never gates shape')
def _version_gate(original):
    return lambda record: 'mode' if record.get('version') == '0.0.0' and record.get('type') == 'assistant' else original(record)


@paired('test_partial_line', 'rollout._complete_line', 'parse a trailing fragment before its newline', 'partial line withheld')
def _accept_fragment(original):
    return lambda raw: True


@paired('test_unknown_shape_counted', 'worker._record_kind', 'hide unknown record as known metadata', 'unknown record counted')
def _hide_shape(original):
    return lambda record: 'mode' if record.get('type') == 'future-record' else original(record)


paired('test_filter_after_dedup', 'ledger._owner_rank', 'attribute replay to later in-range source',
       'filter follows global ownership')(_prefer_continuation)


@paired('test_429_is_full_reading', 'worker._full_reading', 'omit the rejected weekly full reading', '429 full fact reading')
def _omit_full(original):
    return lambda record: False


@paired('test_429_without_weekly_rejection_has_no_percent', 'worker._full_reading', 'treat nonweekly rejection as weekly all-model',
        '429 without weekly rejection rateLimitType')
def _nonweekly_full(original):
    def changed(record):
        quota = record.get('quotaLimits') or {}
        return quota.get('status') == 'rejected' and worker._reset(quota.get('resetsAt')) is not None
    return changed


@paired('test_429_copies_and_server_error', 'ledger._fact_identity', 'count each source copy of a refusal separately',
        '429 copies one event reading assumption')
def _per_source_event(original):
    return lambda item, key: original(item, key) + ':' + item['source_id']


def _run(target):
    tests.RESULTS.clear()
    try:
        target()
    except BaseException as exc:
        return list(tests.RESULTS), exc.__class__.__name__
    return list(tests.RESULTS), None


def _evaluate(entry, target, factory=None):
    before, error = _run(target)
    named = entry['assertion']
    if error or not before or any(not ok for _n, ok, _detail in before) or not any(n == named for n, _ok, _d in before):
        return False, 'invalid mutation: baseline did not pass its designated assertion'
    module_name, function_name = entry['function'].split('.')
    module = {'worker': worker, 'ledger': ledger, 'rollout': rollout}[module_name]
    original = getattr(module, function_name)
    state = {'calls': 0, 'error': None}
    try:
        mutation = (factory or entry['factory'])(original)
    except BaseException as exc:
        return False, 'invalid mutation: setup ' + exc.__class__.__name__

    def instrumented(*args, **kwargs):
        state['calls'] += 1
        try:
            return mutation(*args, **kwargs)
        except BaseException as exc:
            state['error'] = exc.__class__.__name__
            raise

    setattr(module, function_name, instrumented)
    try:
        results, error = _run(target)
    finally:
        setattr(module, function_name, original)
    if state['error'] or error:
        return False, 'invalid mutation: exception ' + (state['error'] or error)
    if not state['calls']:
        return False, 'invalid mutation: decision did not execute'
    if not any(n == named and not ok for n, ok, _detail in results):
        return False, 'invalid mutation: designated assertion did not fail'
    return True, 'caught by: ' + named


def _self_test():
    entry = MANIFEST['test_equal_usage_distinct_ids']
    target = tests.test_equal_usage_distinct_ids
    for exception in (RuntimeError, ImportError):
        def factory(original):
            def crash(record):
                raise exception('invented mutation failure')
            return crash
        ok, reason = _evaluate(entry, target, factory)
        if ok or 'invalid mutation: exception ' not in reason:
            return False
    # An executed no-op, and a mutation that breaks only an unrelated assertion,
    # must also fail the harness's sensitivity test.
    ok, _reason = _evaluate(entry, target, lambda original: original)
    if ok:
        return False
    unrelated = dict(MANIFEST['test_last_block_whole_usage'], assertion='B tool interval')
    ok, _reason = _evaluate(unrelated, tests.test_last_block_whole_usage)
    return not ok


RETENTION_MANIFEST = {}


def retention_pair(test, function, mutation, assertion):
    def decorate(factory):
        RETENTION_MANIFEST[test] = {'function': function, 'mutation': mutation,
                                    'assertion': assertion, 'factory': factory}
        return factory
    return decorate


def _discard_absent(original):
    def changed(previous, incoming):
        if previous is not None and incoming is None:
            return None, {}
        return original(previous, incoming)
    return changed


for _test, _assertion in (
        ('test_capture_then_prune', 'pruned B retained'),
        ('test_capture_then_shorten', 'shortened B retained'),
        ('test_rebuild_preserves_history', 'rebuild after prune retains B'),
        ('test_no_cache_preserves_history', 'no-cache after prune retains B')):
    retention_pair(_test, 'history.merge_response_copy', 'discard absent captured response', _assertion)(_discard_absent)


def _replace_with_short_copy(original):
    def changed(previous, incoming):
        if previous is not None and incoming is not None and incoming.get('stable_read') is not False:
            return copy.deepcopy(incoming), {}
        return original(previous, incoming)
    return changed


for _test, _assertion in (
        ('test_removed_terminal_block', 'removed R1 terminal retained'),
        ('test_rebuild_shortened_source_keeps_terminal', 'full reextract shortened source keeps terminal')):
    retention_pair(_test, 'history.merge_response_copy', 'replace retained terminal with earlier incoming blocks',
                   _assertion)(_replace_with_short_copy)


@retention_pair('test_advance_terminal', 'history.merge_response_copy', 'ignore a genuinely new later terminal',
                'new later terminal advances whole tuple')
def _never_advance(original):
    def changed(previous, incoming):
        return (copy.deepcopy(previous), {}) if previous is not None else original(previous, incoming)
    return changed


@retention_pair('test_conflicting_revision_quarantined', 'history.merge_response_copy', 'clear captured revision quarantine',
                'changed captured block quarantines identity')
def _permit_revision(original):
    def changed(previous, incoming):
        result, counters = original(previous, incoming)
        if result is not None:
            result['conflicting'] = False
            result['quality_flags'] = [f for f in result['quality_flags'] if f != 'history_conflicting_revisions']
        counters.pop('history_conflicting_revisions', None)
        return result, counters
    return changed


@retention_pair('test_initial_calendar_unknown', 'history.month_coverage', 'claim complete calendar coverage',
                'initial calendar remains unknown')
def _complete_calendar(original):
    def changed(view, submissions):
        result = original(view, submissions)
        result['calendar_completeness'] = 'complete'
        return result
    return changed


@retention_pair('test_prepared_snapshot_guard', 'history.month_coverage', 'ignore possibly submitted prepared evidence',
                'prepared receipt guards possible submission')
def _ignore_prepared(original):
    return lambda view, submissions: original(view, [r for r in submissions if r['status'] != 'prepared'])


def _allow_month_failure(reason):
    def factory(original):
        def changed(view, submissions):
            from claude_counter import history
            result = original(view, submissions)
            for month, reasons in history._month_failures(view, submissions).items():
                if reason in reasons:
                    result['withheld_months'].pop(month, None)
                    if month not in result['safe_months']:
                        result['safe_months'].append(month)
            result['safe_months'].sort()
            return result
        return changed
    return factory


retention_pair('test_missing_contributor_withholds_month', 'history.month_coverage', 'allow a missing previous contributor',
               'missing submitted contributor withholds whole month')(
                   _allow_month_failure('months_withheld_missing_contributors'))
retention_pair('test_decreased_contribution_withholds_month', 'history.month_coverage', 'allow decreased submitted counts',
               'decreased input_tokens withholds month')(
                   _allow_month_failure('months_withheld_decreased_contributions'))


@retention_pair('test_other_month_survives_withholding', 'history.month_coverage', 'withhold every month after one unsafe month',
                'other month survives withholding')
def _withhold_all_months(original):
    def changed(view, submissions):
        result = original(view, submissions)
        if result['withheld_months']:
            result['safe_months'] = []
        return result
    return changed


@retention_pair('test_commit_failure_no_send', 'history.month_coverage', 'allow sharing an uncommitted capture',
                'commit failure prevents any safe month')
def _ignore_commit_failure(original):
    def changed(view, submissions):
        view = copy.deepcopy(view)
        view['coverage']['history_committed'] = True
        view['counters'].pop('history_commit_failed', None)
        return original(view, submissions)
    return changed


@retention_pair('test_corrupt_blob_no_fallback_share', 'history._checked_digest', 'accept a wrong blob checksum',
                'corrupt digest disables fallback sharing')
def _ignore_blob_digest(original):
    return lambda raw, expected: True


@retention_pair('test_calendar_assignment_frozen', 'history._freeze_calendar', 'reassign captured rows in the current host zone',
                'captured calendar frozen across host zone change')
def _reassign_calendar(original):
    return lambda row, captured: (copy.deepcopy(row), {})


@retention_pair('test_account_snapshot_capture_no_identity', 'history._snapshot', 'add account identity to captured observation',
                'capture blobs contain no identity or text sentinels')
def _retain_account_identity(original):
    def changed(account):
        snapshot = original(account)
        snapshot['email'] = 'FORBIDDEN-EMAIL'
        return snapshot
    return changed


@retention_pair('test_no_account_no_snapshot', 'history._account_snapshots', 'invent an observation with no account',
                'no-account writes no snapshot')
def _invent_account_snapshot(original):
    def changed(account):
        return original(account) if account is not None else [{
            'observed_at': 1.0, 'organization_type': None, 'rate_limit_tier': None,
            'current_plan': None, 'subscription_created_at': None}]
    return changed


@retention_pair('test_unstable_read_keeps_capture', 'history.merge_response_copy', 'discard the capture after an unstable read',
                'unstable read preserves B and reports damage')
def _discard_unstable(original):
    def changed(previous, incoming):
        if incoming is not None and incoming.get('stable_read') is False:
            return None, {}
        return original(previous, incoming)
    return changed


def _evaluate_retention(suite, entry, factory=None):
    target = getattr(suite, entry['test'])

    def run():
        suite.RESULTS.clear()
        try:
            target()
        except BaseException as exc:
            return list(suite.RESULTS), exc.__class__.__name__
        return list(suite.RESULTS), None

    before, error = run()
    named = entry['assertion']
    if error or not before or any(not ok for _n, ok, _detail in before) or not any(n == named for n, _ok, _d in before):
        return False, 'invalid mutation: baseline did not pass its designated assertion'
    module_name, function_name = entry['function'].split('.')
    module = {'history': suite.history}[module_name]
    original = getattr(module, function_name)
    state = {'calls': 0, 'error': None}
    try:
        mutation = (factory or entry['factory'])(original)
    except BaseException as exc:
        return False, 'invalid mutation: setup ' + exc.__class__.__name__

    def instrumented(*args, **kwargs):
        state['calls'] += 1
        try:
            return mutation(*args, **kwargs)
        except BaseException as exc:
            state['error'] = exc.__class__.__name__
            raise

    setattr(module, function_name, instrumented)
    try:
        results, error = run()
    finally:
        setattr(module, function_name, original)
    if state['error'] or error:
        return False, 'invalid mutation: exception ' + (state['error'] or error)
    if not state['calls']:
        return False, 'invalid mutation: decision did not execute'
    if not any(n == named and not ok for n, ok, _detail in results):
        return False, 'invalid mutation: designated assertion did not fail'
    return True, 'caught by: ' + named


def _retention_main():
    spec = importlib.util.spec_from_file_location('test_claude_retention', str(Path(__file__).with_name('test_claude_retention.py')))
    suite = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = suite
    spec.loader.exec_module(suite)
    missing = set(suite.RETENTION_TESTS) - set(RETENTION_MANIFEST)
    if missing:
        print('[FAIL] missing retention mutation pairs: ' + ', '.join(sorted(missing)))
        return 1
    entries = {name: dict(entry, test=name) for name, entry in RETENTION_MANIFEST.items()}
    witness = entries['test_capture_then_prune']
    for exception in (RuntimeError, ImportError):
        def factory(original):
            def crash(*args, **kwargs):
                raise exception('invented mutation failure')
            return crash
        ok, reason = _evaluate_retention(suite, witness, factory)
        if ok or 'invalid mutation: exception ' not in reason:
            print('[FAIL] retention mutation exception self-test')
            return 1
    noop, _ = _evaluate_retention(suite, witness, lambda original: original)
    unrelated, _ = _evaluate_retention(suite, dict(witness, assertion='pruned extract has no live file'))
    if noop or unrelated:
        print('[FAIL] retention mutation sensitivity self-test')
        return 1
    print('[PASS] crashes, import errors, no-ops and unrelated failures are invalid mutations')
    bad = 0
    for name, entry in sorted(entries.items()):
        ok, reason = _evaluate_retention(suite, entry)
        if not ok:
            bad += 1
        print('[%s] %s -> %s -> %s\n        %s' %
              ('PASS' if ok else 'FAIL', name, entry['function'], entry['mutation'], reason))
    print('\n%d/%d retention mutations caught' % (len(entries) - bad, len(entries)))
    return 1 if bad else 0


PRICING_MANIFEST = {}


def pricing_pair(test, function, mutation, assertion):
    def decorate(factory):
        PRICING_MANIFEST[test] = {'function': function, 'mutation': mutation,
                                  'assertion': assertion, 'factory': factory}
        return factory
    return decorate


def _price_edit(original, edit):
    def changed(row, prices):
        row = copy.deepcopy(row)
        edit(row)
        return original(row, prices)
    return changed


@pricing_pair('test_price_table_finite_rates', 'pricing._table_ok', 'accept an invalid table whole', 'finite rates reject whole table')
def _prices_allow_invalid(original):
    return lambda table: True


@pricing_pair('test_unavailable_table_keeps_recorded_search_counts', 'pricing.price_row',
              'drop observed search count with an unavailable table',
              'missing table keeps observed calls without fallback rates')
def _prices_unavailable_drops_calls(original):
    return _price_edit(original, lambda row: row.update(web_search_requests=0))


@pricing_pair('test_fast_R1', 'pricing.price_row', 'ignore recorded Fast speed', 'Fast R1 tokens')
@pricing_pair('test_fast_us_R1', 'pricing.price_row', 'ignore Fast while retaining US', 'Fast US R1 tokens')
def _prices_fast_as_standard(original):
    return _price_edit(original, lambda row: row.update(speed='standard'))


@pricing_pair('test_us_R1', 'pricing.price_row', 'ignore explicit US multiplier', 'US R1 tokens')
def _prices_us_as_global(original):
    return _price_edit(original, lambda row: row.update(inference_geo='global'))


@pricing_pair('test_haiku_threshold', 'pricing.price_row', 'make low prompt threshold exclusive', 'Haiku threshold inclusive low band')
def _prices_threshold_exclusive(original):
    def edit(row):
        if row['usage']['input_tokens'] == 100000:
            row['usage']['input_tokens'] += 1
    return _price_edit(original, edit)


@pricing_pair('test_haiku_threshold_includes_reads', 'pricing.price_row', 'choose band from base input alone', 'Haiku threshold includes reads')
def _prices_threshold_base_only(original):
    return _price_edit(original, lambda row: row['usage'].update(input_tokens=row['base_input_tokens']))


@pricing_pair('test_thinking_not_added', 'pricing.price_row', 'bill thinking as extra output', 'thinking is already output')
def _prices_bill_thinking(original):
    return _price_edit(original, lambda row: row['usage'].update(
        output_tokens=row['usage']['output_tokens'] + (row['usage']['reasoning_output_tokens'] or 0)))


@pricing_pair('test_web_fetch_zero_fee', 'pricing.price_row', 'bill fetch like search', 'fetch fee zero')
def _prices_bill_fetch(original):
    def changed(row, prices):
        value = original(row, prices)
        value['web_search_usd'] += (row['web_fetch_requests'] or 0) * .01
        return value
    return changed


@pricing_pair('test_missing_search_default_zero', 'pricing.price_row', 'invent one unrecorded search', 'missing search zero fee')
def _prices_missing_search(original):
    return _price_edit(original, lambda row: row.update(web_search_requests=1))


@pricing_pair('test_unknown_ttl_assumed_5m', 'pricing.price_row', 'make unknown TTL high use five minutes', 'unknown TTL high all one hour')
def _prices_unknown_ttl_exact(original):
    return _price_edit(original, lambda row: row.update(cache_write_5m=row['cache_creation_input_tokens'],
                                                       cache_write_1h=0, cache_ttl_complete=True))


@pricing_pair('test_missing_speed_standard_price_only', 'pricing.price_row', 'default absent speed to Fast', 'missing speed Standard default')
def _prices_missing_speed_fast(original):
    return _price_edit(original, lambda row: row.update(speed='fast'))


@pricing_pair('test_missing_speed_and_ttl_high', 'pricing.price_row', 'omit Fast adjustment from joint high', 'missing speed TTL joint high')
def _prices_joint_high_standard(original):
    return _price_edit(original, lambda row: row.update(speed='standard'))


@pricing_pair('test_not_available_geo_global', 'pricing.price_row', 'default unavailable geography to US', 'unavailable geography global')
@pricing_pair('test_missing_geo_global', 'pricing.price_row', 'default missing geography to US', 'missing geography global')
def _prices_default_geo_us(original):
    return _price_edit(original, lambda row: row.update(inference_geo='us'))


@pricing_pair('test_unknown_geo_unpriced', 'pricing.price_row', 'price unknown geography globally', 'unknown geography unpriced')
def _prices_unknown_geo_global(original):
    return _price_edit(original, lambda row: row.update(inference_geo='global'))


@pricing_pair('test_opus46_fast_fallback', 'pricing.price_row', 'double the documented Standard fallback', 'Opus 46 Fast falls back to Standard')
def _prices_opus46_fast(original):
    def changed(row, prices):
        value = original(row, prices)
        if row['model'] == 'claude-opus-4-6' and row['speed'] == 'fast':
            value['tokens_usd'] *= 2
            value['tokens_usd_high'] *= 2
        return value
    return changed


@pricing_pair('test_opus47_fast_unpriced', 'pricing.price_row', 'accept unsupported Fast at Standard price', 'Opus 47 Fast unpriced')
def _prices_opus47_fast(original):
    return _price_edit(original, lambda row: row.update(speed='standard'))


@pricing_pair('test_web_search_error_no_invented_fee', 'pricing.price_row', 'bill failed search evidence without a count', 'search errors do not invent fee')
def _prices_bill_failed_search(original):
    return _price_edit(original, lambda row: row.update(web_search_requests=row.get('web_search_failures', 0)))


def _evaluate_pricing(suite, entry, factory=None):
    def run():
        suite.RESULTS.clear()
        try:
            with suite.offline():
                getattr(suite, entry['test'])()
        except BaseException as exc:
            return list(suite.RESULTS), exc.__class__.__name__
        return list(suite.RESULTS), None

    before, error = run()
    named = entry['assertion']
    if error or not before or any(not ok for _n, ok, _d in before) or not any(n == named for n, _ok, _d in before):
        return False, 'invalid mutation: baseline did not pass its designated assertion'
    module_name, function_name = entry['function'].split('.')
    module = {'pricing': suite.pricing}[module_name]
    original = getattr(module, function_name)
    state = {'calls': 0, 'error': None}
    try:
        mutation = (factory or entry['factory'])(original)
    except BaseException as exc:
        return False, 'invalid mutation: setup ' + exc.__class__.__name__

    def instrumented(*args, **kwargs):
        state['calls'] += 1
        try:
            return mutation(*args, **kwargs)
        except BaseException as exc:
            state['error'] = exc.__class__.__name__
            raise

    setattr(module, function_name, instrumented)
    try:
        after, error = run()
    finally:
        setattr(module, function_name, original)
    if state['error'] or error:
        return False, 'invalid mutation: exception ' + (state['error'] or error)
    if not state['calls']:
        return False, 'invalid mutation: decision did not execute'
    if not any(n == named and not ok for n, ok, _d in after):
        return False, 'invalid mutation: designated assertion did not fail'
    return True, 'caught by: ' + named


def _pricing_main():
    spec = importlib.util.spec_from_file_location('test_claude_pricing', str(Path(__file__).with_name('test_claude_pricing.py')))
    suite = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = suite
    spec.loader.exec_module(suite)
    missing = set(suite.PRICING_DECISIONS) - set(PRICING_MANIFEST)
    if missing:
        print('[FAIL] missing pricing mutation pairs: ' + ', '.join(sorted(missing)))
        return 1
    entries = {name: dict(entry, test=name) for name, entry in PRICING_MANIFEST.items()}
    witness = entries['test_fast_R1']
    for exception in (RuntimeError, ImportError):
        def factory(original):
            def crash(*args, **kwargs):
                raise exception('invented mutation failure')
            return crash
        ok, reason = _evaluate_pricing(suite, witness, factory)
        if ok or 'invalid mutation: exception ' not in reason:
            print('[FAIL] pricing mutation exception self-test')
            return 1
    noop, _ = _evaluate_pricing(suite, witness, lambda original: original)
    unrelated, _ = _evaluate_pricing(suite, witness,
                                    lambda original: lambda row, prices: dict(original(row, prices), web_search_usd=0))
    if noop or unrelated:
        print('[FAIL] pricing mutation sensitivity self-test')
        return 1
    print('[PASS] crashes, import errors, no-ops and unrelated failures are invalid mutations')
    bad = 0
    for name, entry in sorted(entries.items()):
        ok, reason = _evaluate_pricing(suite, entry)
        bad += int(not ok)
        print('[%s] %s -> %s -> %s\n        %s' %
              ('PASS' if ok else 'FAIL', name, entry['function'], entry['mutation'], reason))
    print('\n%d/%d pricing mutations caught' % (len(entries) - bad, len(entries)))
    return 1 if bad else 0


WINDOWS_MANIFEST = {}


def windows_pair(name, function, mutation, assertion, test=None):
    def decorate(factory):
        WINDOWS_MANIFEST[name] = dict(test=test or name, function=function, mutation=mutation,
                                      assertion=assertion, factory=factory)
        return factory
    return decorate


def _window_source(original, before, after):
    source = inspect.getsource(original)
    if source.count(before) != 1:
        raise ValueError('mutation source decision is not unique')
    namespace = dict(original.__globals__)
    exec(compile(source.replace(before, after), '<window decision mutation>', 'exec'), namespace)
    return namespace[original.__name__]


@windows_pair('test_reset_jitter_cluster', 'windows._canonical', 'use earliest reset floor instead of rounded median', 'jitter canonical reset')
def _window_earliest_reset(original):
    return lambda resets: float(int(min(resets)))


@windows_pair('test_cluster_not_neighbor_chained', 'windows._fits', 'chain by nearest neighbor instead of full span', 'whole cluster span not neighbor distance')
def _window_neighbor_chain(original):
    return lambda c, reset, tolerance: min(abs(reset - c['min_reset']), abs(reset - c['max_reset'])) <= tolerance


@windows_pair('test_cluster_tolerance_inclusive', 'windows._fits', 'exclude the two-second boundary', 'two second tolerance inclusive')
def _window_exclusive_tolerance(original):
    return lambda c, reset, tolerance: max(c['max_reset'], reset) - min(c['min_reset'], reset) < tolerance


@windows_pair('test_canonical_reset_rounding', 'windows._canonical', 'round median ties to even', 'canonical median tie upward')
def _window_bankers_rounding(original):
    return _window_source(original, 'math.floor(statistics.median(resets) + .5)', 'round(statistics.median(resets))')


@windows_pair('test_established_anchor_stable', 'windows.cluster_resets', 'recompute an established reset from later quotes', 'captured anchor and phase stable')
def _window_move_anchor(original):
    def changed(*args, **kwargs):
        clusters, q = original(*args, **kwargs)
        for c in clusters:
            if c['readings']:
                c['reset_at'] = c['readings'][-1]['resets_at']
        return clusters, q
    return changed


@windows_pair('test_ambiguous_cluster_withheld', 'windows.cluster_resets', 'attach an ambiguous quote to one captured cluster', 'ambiguous established anchors never merged')
def _window_allow_ambiguous(original):
    return _window_source(original, 'if len(fits) > 1:', 'if len(fits) > 100:')


@windows_pair('test_reset_grid_diagnostic', 'windows._grid_departure', 'ignore departures from the learned phase', 'grid diagnostics only offset604804')
def _window_ignore_grid(original):
    return lambda reset, phase: False


@windows_pair('test_grid_snapping_mutation', 'windows.cluster_resets', 'snap a departing reset to the weekly grid',
              'grid diagnostics only offset604804', test='test_reset_grid_diagnostic')
def _window_snap_grid(original):
    def changed(*args, **kwargs):
        clusters, q = original(*args, **kwargs)
        for c in clusters:
            if c['kind'] == 'weekly_all':
                phase = c['grid_phase']
                c['reset_at'] = phase + round((c['reset_at'] - phase) / 604800) * 604800
        return clusters, q
    return changed


@windows_pair('test_first_to_first_peak_span', 'windows.observation_span', 'use the last peak instead of first peak', 'first to earliest peak')
@windows_pair('test_plateau_does_not_extend_span', 'windows.observation_span', 'extend numerator through the plateau', 'plateau leaves first peak endpoint')
def _window_last_peak(original):
    return _window_source(original, 'next((t for t, p in points if p == peak), None)',
                          'next((t for t, p in reversed(points) if p == peak), None)')


@windows_pair('test_boundary_open_closed', 'windows._in_span', 'include first end and exclude peak end', 'observation boundaries open closed')
def _window_reverse_boundaries(original):
    return _window_source(original, 'start < stamp <= end', 'start <= stamp < end')


@windows_pair('test_single_reading_withheld', 'windows.observation_span', 'accept a single sourced percentage', 'single reading withheld')
@windows_pair('test_429_only_single_reading', 'windows.observation_span', 'accept a lone full refusal reading', '429 only is single reading')
def _window_single_accepted(original):
    return _window_source(original, "bump(counters, 'window_single_reading')", 'pass')


@windows_pair('test_zero_delta_withheld', 'windows.observation_span', 'accept a nonincreasing comparison', 'zero delta withheld')
def _window_zero_accepted(original):
    return _window_source(original, "bump(counters, 'window_zero_delta')", 'pass')


@windows_pair('test_percent_drop_withheld', 'windows.observation_span', 'ignore percentage declines', 'percent decrease withheld')
def _window_drop_accepted(original):
    return _window_source(original, "bump(counters, 'limit_percent_decreased')", 'pass')


@windows_pair('test_same_stamp_conflict', 'windows.observation_span', 'accept conflicting percentages at the same time', 'same timestamp conflict withheld')
def _window_conflict_accepted(original):
    return _window_source(original, "bump(counters, 'limit_quote_conflict')", 'pass')


@windows_pair('test_429_is_full_reading', 'worker._full_reading', 'drop rejected weekly full readings', 'weekly refusal sourced full reading')
def _window_no_weekly_refusal(original):
    return lambda record: False


@windows_pair('test_429_without_weekly_rejection_has_no_percent', 'worker._full_reading', 'promote nonweekly refusals to all-model100',
              "nonweekly or nonrejected refusal has no percent {'kind': 'five_hour'}")
def _window_every_refusal_full(original):
    return lambda record: original(record) or record.get('quotaLimits', {}).get('rateLimitType') == 'five_hour'


@windows_pair('test_weekly_all_only', 'windows.wire_windows', 'submit local five-hour windows as weekly', 'wire only weekly all')
def _window_wire_all_kinds(original):
    return _window_source(original, "if w['kind'] == 'weekly_all' and w['shareable']", "if w['kind'] == 'session' or w['shareable']")


@windows_pair('test_scoped_not_aliased_to_all', 'windows.cluster_resets', 'erase scoped identities during reset clustering', 'scoped identities remain separate')
def _window_erase_scope(original):
    return _window_source(original, "selected[(kind, scope if kind == 'weekly_scoped' else None)]", 'selected[(kind, None)]')


@windows_pair('test_five_hour_not_weekly', 'windows.build_windows', 'subtract a week for session quota', 'session nominal five hours')
def _window_session_week(original):
    return _window_source(original, "300 * 60 if cluster['kind'] == 'session' else WEEK_S", 'WEEK_S')


@windows_pair('test_unknown_plan_null', 'windows.plan_for_interval', 'invent a Max5 witness without any captured snapshot', 'unknown plan does not withhold')
def _window_invent_plan_witness(original):
    def changed(start, end, snapshots):
        snapshots = list(snapshots)
        if not snapshots:
            snapshots = [dict(observed_at=start, organization_type='claude_max',
                              rate_limit_tier='default_claude_max_5x', current_plan='claude:max-5x', subscription_created_at=start)]
        return original(start, end, snapshots)
    return changed


@windows_pair('test_plan_from_current_snapshot', 'account.map_plan', 'map known Max5 tier to Pro', 'current captured snapshot attributes plan')
def _window_max5_as_pro(original):
    def changed(org, tier):
        plan, q = original(org, tier)
        return ('claude:pro' if plan == 'claude:max-5x' else plan), q
    return changed


@windows_pair('test_plan_null_before_subscription', 'windows.plan_for_interval', 'ignore subscription creation after span start', 'subscription start guard')
def _window_ignore_created_after(original):
    return _window_source(original, 'or created is None or created > start', 'or created is None')


@windows_pair('test_plan_conflict_in_span', 'windows.plan_for_interval', 'ignore earlier post-start snapshots', 'in span plan conflict once')
def _window_latest_snapshot_only(original):
    return lambda start, end, snapshots: original(start, end, sorted(snapshots, key=lambda s: s['observed_at'])[-1:])


@windows_pair('test_plan_changed_after_span', 'windows.plan_for_interval', 'ignore account observations after interval end', 'after span tier change invalidates plan')
def _window_ignore_after_span(original):
    return lambda start, end, snapshots: original(start, end, [s for s in snapshots if s['observed_at'] <= end])


@windows_pair('test_latency_plan_same_rule', 'analyze._timing', 'test subscription date against latest sample end', 'latency captured snapshot rule1')
def _window_latency_latest_end(original):
    return _window_source(original, 'windows.plan_for_interval(min(ends), max(ends),', 'windows.plan_for_interval(max(ends), max(ends),')


@windows_pair('test_snapshot_null_blocks_plan', 'windows.plan_for_interval', 'discard applicable null plan evidence', 'null snapshot blocks without known disagreement')
@windows_pair('test_plan_predecessor_null_blocks', 'windows.plan_for_interval', 'discard null latest predecessor evidence', 'null latest predecessor blocks')
def _window_ignore_null_plans(original):
    return lambda start, end, snapshots: original(start, end, [s for s in snapshots if s['current_plan'] is not None])


@windows_pair('test_subscription_date_missing_null', 'windows.plan_for_interval', 'invent a missing latest subscription date', 'missing witness subscription date')
def _window_invent_subscription_date(original):
    def changed(start, end, snapshots):
        snapshots = copy.deepcopy(snapshots)
        for s in snapshots:
            if s['subscription_created_at'] is None:
                s['subscription_created_at'] = start
        return original(start, end, snapshots)
    return changed


@windows_pair('test_team_max_tier_not_personal_max', 'account.map_plan', 'infer a personal Max plan from a Team tier', 'team tier never personal Max')
@windows_pair('test_plan_mapping_table', 'account.map_plan', 'let known Max tiers override organization type', 'closed account mapping table')
def _window_tier_overrides_org(original):
    return lambda org, tier: original('claude_max' if tier in ('default_claude_max_5x', 'default_claude_max_20x') else org, tier)


@windows_pair('test_unknown_max_tier_null', 'account.map_plan', 'assume Max5 for unfamiliar nonempty Max tiers', 'unknown Max tier diagnostic')
def _window_unknown_max5(original):
    return lambda org, tier: original(org, 'default_claude_max_5x' if org == 'claude_max' and tier else tier)


@windows_pair('test_plan_latest_predecessor_only', 'windows.plan_for_interval', 'let all older snapshots veto a later interval', 'only latest predecessor vetoes')
def _window_all_predecessors(original):
    return _window_source(original, 'latest_before = predecessor[-1:]', 'latest_before = predecessor')


@windows_pair('test_plan_predecessor_only_witness', 'windows.plan_for_interval', 'require a post-start snapshot as the witness', 'predecessor only witness accepted')
def _window_require_later_witness(original):
    return _window_source(original, 'or created is None or created > start',
                          'or created is None or created > start or not any(t >= start for t, s, p, d in mapped)')


@windows_pair('test_plan_latest_witness_date', 'windows.plan_for_interval', 'borrow the older witness creation date', 'latest witness owns creation guard')
def _window_old_witness_date(original):
    return _window_source(original, 'witness = mapped[-1][1]', 'witness = mapped[0][1]')


@windows_pair('test_plan_at_start_inclusive', 'windows.plan_for_interval', 'ignore snapshots exactly at the attribution start', 'snapshot at start participates')
def _window_exclude_start_snapshot(original):
    return _window_source(original, 's[0] >= start]', 's[0] > start]')


@windows_pair('test_partial_window_withheld', 'windows.build_windows', 'treat explicit partial contributors as complete', 'partial contributing response withheld')
def _window_accept_partial(original):
    return _window_source(original, "if any(r.get('partial') for r in members):", 'if False:')


@windows_pair('test_unknown_speed_window_partial_split', 'windows._split', 'infer Standard for absent recorded speed', 'known speed split exact counts')
def _window_infer_standard(original):
    return lambda rows: original([dict(r, tier=r.get('tier') or 'standard') for r in rows])


@windows_pair('test_missing_speed_withholding_mutation', 'windows.build_windows', 'withhold missing speed rather than keep partial partition',
              'unknown speed accepted outside split', test='test_unknown_speed_window_partial_split')
def _window_withhold_speed(original):
    return _window_source(original, "shareable=cluster['kind'] == 'weekly_all' and not reasons,",
                          "shareable=cluster['kind'] == 'weekly_all' and not reasons and not speed_missing,")


@windows_pair('test_unknown_ttl_window_withheld', 'windows._ttl_complete', 'assume unknown creation TTL is exact', 'unknown nonzero TTL withheld')
def _window_assume_ttl(original):
    return lambda row: True


@windows_pair('test_unavailable_timestamps_withheld', 'windows.build_windows', 'accept unassignable and replayed response ends', 'required response timing withheld tsNone')
def _window_ignore_timestamps(original):
    return _window_source(original,
        "if any(ts is None or _in_span(ts, span['observation_start'], span['observation_end'])\n               and not _timing_safe(r, ts) for ts, r in timed):", 'if False:')


@windows_pair('test_overlapping_span_withheld', 'windows._overlaps', 'permit overlapping all-model observation spans', 'overlapping all model spans withheld')
def _window_allow_overlap(original):
    return lambda a, b: False


@windows_pair('test_server_pricing_diagnostics_only', 'windows.build_windows', 'withhold Fast rows for server pricing differences', 'server pricing diagnostic only window_rows_fast')
def _window_withhold_fast(original):
    return _window_source(original, "shareable=cluster['kind'] == 'weekly_all' and not reasons,",
                          "shareable=cluster['kind'] == 'weekly_all' and not reasons and not any(r['tier'] == 'fast' for r in members),")


@windows_pair('test_sparse_points_no_synthetic_endpoints', 'windows.observation_span', 'synthesize a nominal zero-percent opening', 'sparse sourced percentage points')
def _window_fake_opening(original):
    def changed(readings):
        span, q = original(readings)
        span['pct_points'].insert(0, [min(r['resets_at'] for r in readings) - 604800, 0])
        return span, q
    return changed


@windows_pair('test_nominal_chart_observation_share', 'windows._in_nominal', 'restrict nominal chart to the share observation span', 'nominal counts distinct from share')
def _window_chart_share_span(original):
    return lambda stamp, start, end: stamp is not None and start + 7200 < stamp <= start + 10800


@windows_pair('test_nominal_chart_boundaries', 'windows._in_nominal', 'exclude nominal start and include reset', 'nominal interval includes start excludes reset')
def _window_reverse_nominal_boundary(original):
    return _window_source(original, 'start <= stamp < end', 'start < stamp <= end')


@windows_pair('test_small_delta_shareable_ineligible', 'windows.build_windows', 'withhold a positive delta below five points', 'small positive delta accepted')
def _window_minimum_delta_for_share(original):
    return _window_source(original, "shareable=cluster['kind'] == 'weekly_all' and not reasons,",
                          "shareable=cluster['kind'] == 'weekly_all' and not reasons and span['peak_pct'] - span['first_pct'] >= 5,")


@windows_pair('test_wire_coverage_guard', 'windows.wire_windows', 'submit entries despite unsafe replacement coverage', 'coverage forbids window replacement False')
def _window_ignore_coverage(original):
    return _window_source(original, 'return None, reasons', "return [_wire_entry(w) for w in windows if w['shareable']], reasons")


def _evaluate_windows(suite, entry, factory=None):
    def run():
        suite.RESULTS.clear()
        try:
            with suite.offline():
                getattr(suite, entry['test'])()
        except BaseException as exc:
            return list(suite.RESULTS), exc.__class__.__name__
        return list(suite.RESULTS), None

    before, error = run()
    named = entry['assertion']
    if error or not before or any(not ok for _n, ok, _d in before) or not any(n == named for n, _ok, _d in before):
        return False, 'invalid mutation: baseline did not pass its designated assertion'
    module_name, function_name = entry['function'].split('.')
    module = {'windows': suite.windows, 'account': suite.account, 'worker': suite.worker, 'analyze': suite.analyze}[module_name]
    original = getattr(module, function_name)
    state = {'calls': 0, 'error': None}
    try:
        mutation = (factory or entry['factory'])(original)
    except BaseException as exc:
        return False, 'invalid mutation: setup ' + exc.__class__.__name__

    def instrumented(*args, **kwargs):
        state['calls'] += 1
        try:
            return mutation(*args, **kwargs)
        except BaseException as exc:
            state['error'] = exc.__class__.__name__
            raise

    setattr(module, function_name, instrumented)
    try:
        after, error = run()
    finally:
        setattr(module, function_name, original)
    if state['error'] or error:
        return False, 'invalid mutation: exception ' + (state['error'] or error)
    if not state['calls']:
        return False, 'invalid mutation: decision did not execute'
    if not any(n == named and not ok for n, ok, _d in after):
        return False, 'invalid mutation: designated assertion did not fail'
    return True, 'caught by: ' + named


def _windows_main():
    spec = importlib.util.spec_from_file_location('test_claude_windows', str(Path(__file__).with_name('test_claude_windows.py')))
    suite = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = suite
    spec.loader.exec_module(suite)
    missing = set(suite.WINDOWS_DECISIONS) - {entry['test'] for entry in WINDOWS_MANIFEST.values()}
    if missing:
        print('[FAIL] missing windows mutation pairs: ' + ', '.join(sorted(missing)))
        return 1
    witness = WINDOWS_MANIFEST['test_boundary_open_closed']
    for exception in (RuntimeError, ImportError):
        def factory(original):
            def crash(*args, **kwargs):
                raise exception('invented mutation failure')
            return crash
        ok, reason = _evaluate_windows(suite, witness, factory)
        if ok or 'invalid mutation: exception ' not in reason:
            print('[FAIL] windows mutation exception self-test')
            return 1
    noop, _ = _evaluate_windows(suite, witness, lambda original: original)
    unrelated_entry = dict(WINDOWS_MANIFEST['test_nominal_chart_observation_share'], function='windows._split')
    def unrelated_factory(original):
        def changed(rows):
            split = original(rows)
            for s in split:
                s['cache_write_5m'] = 0
            return split
        return changed
    unrelated, unrelated_reason = _evaluate_windows(suite, unrelated_entry, unrelated_factory)
    if noop or unrelated or unrelated_reason != 'invalid mutation: designated assertion did not fail':
        print('[FAIL] windows mutation sensitivity self-test')
        return 1
    print('[PASS] crashes, import errors, no-ops and unrelated failures are invalid mutations')
    bad = 0
    for name, entry in sorted(WINDOWS_MANIFEST.items()):
        ok, reason = _evaluate_windows(suite, entry)
        bad += int(not ok)
        print('[%s] %s -> %s -> %s\n        %s' %
              ('PASS' if ok else 'FAIL', name, entry['function'], entry['mutation'], reason))
    print('\n%d/%d windows mutations caught' % (len(WINDOWS_MANIFEST) - bad, len(WINDOWS_MANIFEST)))
    return 1 if bad else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--section', choices=('ledger', 'retention', 'pricing', 'windows'), default='ledger')
    args = parser.parse_args(argv)
    if args.section == 'retention':
        return _retention_main()
    if args.section == 'pricing':
        return _pricing_main()
    if args.section == 'windows':
        return _windows_main()
    missing = set(tests.LEDGER_TESTS) - set(MANIFEST)
    if missing:
        print('[FAIL] missing ledger mutation pairs: ' + ', '.join(sorted(missing)))
        return 1
    if not _self_test():
        print('[FAIL] mutation harness sensitivity self-test')
        return 1
    print('[PASS] crashes, import errors, no-ops and unrelated failures are invalid mutations')
    bad = 0
    for name, entry in sorted(MANIFEST.items()):
        ok, reason = _evaluate(entry, getattr(tests, name))
        if not ok:
            bad += 1
        print('[%s] %s -> %s -> %s\n        %s' %
              ('PASS' if ok else 'FAIL', name, entry['function'], entry['mutation'], reason))
    print('\n%d/%d ledger mutations caught' % (len(MANIFEST) - bad, len(MANIFEST)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
