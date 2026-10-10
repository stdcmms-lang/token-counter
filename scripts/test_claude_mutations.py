"""Paired ledger mutations: baseline, execution, and a designated failed assertion.

Each manifest entry names the pure decision it protects. A crash (including one contained
by extraction), import error, baseline failure or unrelated assertion is invalid evidence.
The patch is in memory and restored after each target; no source files are rewritten.

    python -B scripts/test_claude_mutations.py --section ledger
"""
import argparse
import copy
import importlib.util
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--section', choices=('ledger', 'retention'), default='ledger')
    args = parser.parse_args(argv)
    if args.section == 'retention':
        return _retention_main()
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
