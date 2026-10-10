"""The canonical Claude usage ledger.

Response identity is the server's (message.id, requestId), not usage equality. Collapse
blocks within each source first, compare whole terminal tuples, then charge one owner.
Continuation and fork evidence settles attribution; identical unrelated copies still
represent one response. Conflicting nonzero counts or served models are quarantined.
"""
import collections
import copy as copylib
import datetime
import functools
import re

from .models import LedgerResult, bump
from .worker import _digest, _effort, _model_name, epoch as _raw_epoch

_OFFSET_RE = re.compile(r'(Z|[+-]\d{2}:?\d{2})$')
_cached_epoch = functools.lru_cache(maxsize=1 << 18)(_raw_epoch)


def epoch(value):
    # Offset-bearing record timestamps are immutable measurements, independent of
    # host calendar context. Invalid/non-string inputs keep the extractor's result.
    return _cached_epoch(value) if isinstance(value, str) else _raw_epoch(value)


@functools.lru_cache(maxsize=1 << 18)
def _local_day(minute_prefix, offset):
    """Local calendar day for an ISO-8601 minute plus its UTC offset.

    Rollout record timestamps are UTC (``...Z``) but the directory layout and file names use
    **local** dates, and `--since`/`--until` filter on those.  Bucketing on the raw prefix
    would put a 00:24 local session in the previous day for anyone east of Greenwich.
    Any explicit offset is honoured, not just ``Z`` -- treating ``-04:00`` as wall-clock put
    it on the wrong day.  Resolution is the **minute**: truncating to the hour first was also
    wrong, because a half-hour offset such as ``-04:30`` then lands a day early.  Memoised, so
    136k responses cost far fewer conversions.
    """
    if offset is None:
        return minute_prefix[:10]        # no offset at all: take the date as written
    try:
        dt = datetime.datetime.fromisoformat(minute_prefix + ':00' + offset)
    except ValueError:
        return None
    return dt.astimezone().date().isoformat()


def _day_span(date_str, tz=None):
    """Local midnight to local midnight for a ``YYYY-MM-DD`` bucket, as unix seconds.

    Daily buckets are *local* calendar days (:func:`_local_day`), so both ends are taken from
    the calendar -- the naive date, and the naive date plus one -- and localised only then.
    Adding a day to the *localised* start was the first version, and on the fixed offset
    ``astimezone()`` attaches that is +86,400 s: an hour late on the day the clock springs
    forward and an hour early on the day it falls back, so the bar overlapped or gapped its
    neighbour twice a year.  `tz` pins the zone for tests; ``None`` is the machine's.
    """
    try:
        d = datetime.date.fromisoformat(str(date_str))
    except (ValueError, TypeError):
        return None, None               # 'unknown', or a date the filename never carried

    def local(naive):
        return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()

    try:
        start = datetime.datetime(d.year, d.month, d.day)
        return (local(start).timestamp(),
                local(start + datetime.timedelta(days=1)).timestamp())
    except (OverflowError, OSError, ValueError):
        return None, None


def _day(ts, fallback=None):
    if isinstance(ts, str) and len(ts) >= 16:
        m = _OFFSET_RE.search(ts)
        if m:
            off = '+00:00' if m.group(1) == 'Z' else m.group(1)
            if len(off) == 5:            # +0800 -> +08:00
                off = off[:3] + ':' + off[3:]
        else:
            off = None
        got = _local_day(ts[:16], off)
        if got:
            return got
    return fallback


def _iso(epoch_s):
    """Local-time ISO string for a unix timestamp.  Windows are read by a human, in their
    own zone; the raw epoch is kept beside it for anything that needs to compute."""
    if epoch_s is None:
        return None
    try:
        return (datetime.datetime.fromtimestamp(epoch_s)
                .astimezone().isoformat(timespec='minutes'))
    except (OverflowError, OSError, ValueError):
        return None


def _usage_tuple(usage):
    return tuple(usage[field] for field in ('base_input', 'creation', 'reads', 'output'))


def _terminal(copy):
    key = copy.get('terminal_record_key')
    return next((b for b in reversed(copy['blocks']) if b['record_key'] == key), None)


def collapse_blocks(copy) -> tuple:
    """Return a collapsed copy and counters, without changing the input facts.

    The last valid record supplies the whole tuple. Earlier valid usage can survive a
    malformed terminal as a partial lower bound, but changing prompt counts is a conflict.
    """
    out, counters, blocks, seen = dict(copy, quality_flags=list(copy['quality_flags'])), {}, [], {}
    inputs, previous_output = set(), None
    retained_terminal = out.get('terminal_record_key') if out.get('history_merged') else None
    ordered = out['blocks'] if out.get('history_merged') else sorted(out['blocks'], key=lambda b: b['physical_line'])
    for block in ordered:
        index = block['api_block_index']
        key = ('index', index) if index is not None else ('record', block['record_key'])
        old = seen.get(key)
        if old is not None:
            a, b = old['usage'], block['usage']
            if a == b:
                bump(counters, 'in_file_duplicate_blocks')
                continue
            out['quality_flags'].append('response_usage_conflict')
        seen[key] = block
        blocks.append(block)
        usage = block['usage']
        if usage is not None:
            inputs.add(_usage_tuple(usage)[:3])
            if previous_output is not None and usage['output'] < previous_output:
                bump(counters, 'response_output_decreased')
            previous_output = usage['output']
    bump(counters, 'blocks_collapsed', max(0, len(blocks) - 1))
    out['blocks'] = blocks
    valid = [b for b in blocks if b['usage'] is not None]
    out['terminal_record_key'] = (retained_terminal if any(b['record_key'] == retained_terminal for b in valid)
                                  else valid[-1]['record_key'] if valid else None)
    if len(inputs) > 1:
        out['quality_flags'].append('response_input_conflict')
        bump(counters, 'response_input_conflict')
    if blocks and blocks[-1]['usage'] is None:
        out['partial'] = bool(valid) or out['partial']
        out['quality_flags'].append('response_terminal_usage_invalid')
        bump(counters, 'response_terminal_usage_invalid')
    out['quality_flags'] = sorted(set(out['quality_flags']))
    return out, counters


def resolve_families(links, sources) -> tuple:
    """Source attribution and counters for the directed continuation graph.

    A component needs one root and no cycle. Forked streams inherit their parent family;
    a subagent without a main transcript still has its containing orphan family.
    """
    counters, children, parents, neighbors = {}, collections.defaultdict(set), collections.defaultdict(set), collections.defaultdict(set)
    sessions = {source['session_id'] for source in sources}
    forks, seen = [], set()
    for link in links:
        key = (link['kind'], link['from_session'], link['to_session'],
               link['agent_id'], link['parent_last_uuid'])
        if key in seen:
            bump(counters, 'lineage_link_copies')
            continue
        seen.add(key)
        if link['kind'] == 'fork':
            forks.append(link)
            bump(counters, 'fork_context_links')
            sessions.add(link['from_session'])
        else:
            a, b = link['from_session'], link['to_session']
            sessions.update((a, b))
            children[a].add(b)
            parents[b].add(a)
            neighbors[a].add(b)
            neighbors[b].add(a)
            bump(counters, 'continuation_links')
    by_session, visited = {}, set()
    for session in sorted(sessions):
        if session in visited:
            continue
        component, pending = set(), [session]
        while pending:
            current = pending.pop()
            if current not in component:
                component.add(current)
                pending.extend(neighbors[current] - component)
        visited.update(component)
        roots = sorted(s for s in component if not parents[s])
        degree = {s: len(parents[s]) for s in component}
        queue, consumed = list(roots), 0
        while queue:
            current = queue.pop()
            consumed += 1
            for child in children[current]:
                degree[child] -= 1
                if degree[child] == 0:
                    queue.append(child)
        cyclic = consumed != len(component)
        if cyclic:
            bump(counters, 'continuation_cycles')
        if len(roots) > 1:
            bump(counters, 'continuation_multiple_roots')
        root = roots[0] if len(roots) == 1 and not cyclic else None
        distances = {root: 0} if root else {}
        pending = [root] if root else []
        while pending:
            current = pending.pop(0)
            for child in sorted(children[current]):
                distance = distances[current] + 1
                if child not in distances or distance < distances[child]:
                    distances[child] = distance
                    pending.append(child)
        for current in component:
            by_session[current] = {'family_id': root, 'component': min(component),
                                   'distance': distances.get(current, 0)}
    out = {}
    for source in sources:
        info = dict(by_session[source['session_id']])
        matches = [link for link in forks if link['to_session'] == source['session_id']
                   and link['agent_id'] == source['thread_id'] and source['kind'] == 'subagent']
        parent_families = {by_session[link['from_session']]['family_id'] for link in matches}
        if matches:
            if len(parent_families) == 1:
                parent_info = by_session[matches[0]['from_session']]
                info.update(parent_info)
            else:
                info['family_id'] = None
                bump(counters, 'continuation_multiple_roots')
        info.update({field: source[field] for field in ('source_id', 'session_id', 'thread_id',
                                                       'kind', 'first_record_ts')})
        info['stream_id'] = _digest([info['family_id'] or info['component'], source['thread_id']])
        out[source['source_id']] = info
    main_families = {info['family_id'] for info in out.values() if info['kind'] == 'main'}
    orphans = {info['family_id'] for info in out.values()
               if info['kind'] == 'subagent' and info['family_id'] is not None
               and info['family_id'] not in main_families}
    bump(counters, 'orphan_subagent_families', len(orphans))
    for info in out.values():
        info['orphan_main'] = info['family_id'] in orphans
    return out, counters


def _source_rank(info):
    ts = epoch(info['first_record_ts'])
    return (ts is None, ts if ts is not None else 0, info['source_id'])


def _owner_rank(copy, families):
    info = families[copy['source_id']]
    original_main = info['kind'] == 'main' and info['session_id'] == info['family_id']
    return (0 if original_main else 1, info['distance'],
            0 if info['kind'] == 'main' else 1, _source_rank(info))


def choose_owner(copies, families) -> tuple:
    """Return ``(owner, classified_copies, counters)``; input facts stay unchanged."""
    # Ownership only annotates the envelopes/flag lists. Blocks are read-only here;
    # copying their usage and tool trees again costs more than canonicalization.
    copies, counters = [dict(c, quality_flags=list(c['quality_flags'])) for c in copies], {}
    bump(counters, 'cross_file_response_copies', max(0, len(copies) - 1))
    all_blocks = [(c['response_key'], 'index', b['api_block_index'])
                  if b['api_block_index'] is not None else (c['response_key'], 'record', b['record_key'])
                  for c in copies for b in c['blocks']]
    bump(counters, 'cross_file_block_copies', len(all_blocks) - len(set(all_blocks)))
    for reason in ('history_conflicting_revisions', 'response_input_conflict',
                   'response_usage_conflict', 'response_model_conflict'):
        if any(reason in c['quality_flags'] for c in copies):
            bump(counters, reason)
            return None, copies, counters
    valid = [c for c in copies if _terminal(c) is not None
             and not any(flag in c['quality_flags'] for flag in ('synthetic_records', 'api_error_records'))]
    if not valid:
        return None, copies, counters
    models = {_model_name(c['raw_model'])[0] for c in valid} - {'unknown'}
    if len(models) > 1:
        bump(counters, 'response_model_conflict')
        return None, copies, counters
    nonzero = [c for c in valid if any(_usage_tuple(_terminal(c)['usage']))]
    tuples = {_usage_tuple(_terminal(c)['usage']) for c in nonzero}
    if len(tuples) > 1:
        bump(counters, 'response_usage_conflict')
        return None, copies, counters
    if nonzero:
        bump(counters, 'zero_usage_placeholder_copies', len(valid) - len(nonzero))
        valid = nonzero
    components = {families[c['source_id']]['component'] for c in valid}
    unrelated = len(components) > 1
    rank = (lambda c: _source_rank(families[c['source_id']])) if unrelated else (lambda c: _owner_rank(c, families))
    owner = min(valid, key=rank)
    if unrelated:
        owner_component = families[owner['source_id']]['component']
        bump(counters, 'unrelated_response_copies', sum(
            1 for c in copies if families[c['source_id']]['component'] != owner_component))
    owner['timestamp_quality'] = 'original'
    stamps = {epoch(_terminal(c)['ts']) for c in valid}
    # A server identity proves one charge. It does not prove original timing between
    # unrelated, restamped copies; the deterministic owner still supplies the local day.
    original_source = families[owner['source_id']]['session_id'] == families[owner['source_id']]['family_id']
    if len(stamps) > 1 and (unrelated or not original_source):
        owner['timestamp_quality'] = 'unknown'
        owner['quality_flags'].append('response_replay_timestamp_unverified')
        bump(counters, 'response_replay_timestamp_unverified')
    for c in copies:
        if c is not owner:
            c['timestamp_quality'] = 'copied'
    return owner, copies, counters


def _response_row(owner, attribution, counters, day_spans=None):
    terminal = _terminal(owner)
    usage = terminal['usage']
    model, raw_model, context = _model_name(owner['raw_model'])
    flags = set(owner['quality_flags']) | set(usage['invalid_optional_fields'])
    if not usage['searches']:
        flags.discard('price_search_failure_ambiguous')
    effort, effort_reason = _effort(owner['effort'], owner['per_turn_effort'])
    if effort_reason:
        flags.add(effort_reason)
    for name in sorted(flags):
        if name not in ('response_replay_timestamp_unverified', 'prompt_growth_compaction_skipped'):
            bump(counters, name)
    if owner['partial']:
        bump(counters, 'partial_responses')
    if not any(_usage_tuple(usage)):
        bump(counters, 'zero_usage_responses')
    if owner['advisor_model'] is not None:
        bump(counters, 'advisor_model_records')
    stop = owner['stop_reason']
    if stop in ('max_tokens', 'refusal'):
        bump(counters, 'max_tokens_responses' if stop == 'max_tokens' else 'refusal_responses')
    elif stop is None:
        bump(counters, 'response_stop_reason_missing')
    ts = terminal['ts']
    day = _day(ts) if epoch(ts) is not None else None
    if day_spans is None:
        start, end = _day_span(day)
    else:
        if day not in day_spans:
            day_spans[day] = _day_span(day)
        start, end = day_spans[day]
    if day is None or start is None or end is None:
        day, start, end = None, None, None
        bump(counters, 'undated_responses')
    req_ts = _timing_anchor(owner)
    if (owner['agent_id'] is not None and req_ts is None and not owner['partial']
            and owner['timestamp_quality'] == 'original'):
        flags.add('latency_subagent_prompt_missing')
        bump(counters, 'latency_subagent_prompt_missing')
    whole_input = usage['base_input'] + usage['creation'] + usage['reads']
    return {
        'response_key': owner['response_key'], 'source_id': owner['source_id'],
        'family_id': attribution['family_id'], 'stream_id': attribution['stream_id'],
        'stream': 'claude', 'index': 0, 'ts': ts, 'req_ts': req_ts,
        'first_output_ts': owner['blocks'][0]['ts'], 'timestamp_quality': owner['timestamp_quality'],
        'local_day': day, 'day_start': start, 'day_end': end,
        'calendar_signature': _digest([day, start, end]), 'turn': owner['blocks'][0]['turn'],
        'model': model, 'raw_model': raw_model, 'requested_model': owner['requested_model'],
        'advisor_model': owner['advisor_model'], 'context_1m': context, 'effort': effort,
        'tier': usage['speed'] if usage['speed'] in ('standard', 'fast') else None,
        'speed': usage['speed'], 'service_tier': usage['service_tier'],
        'inference_geo': usage['inference_geo'], 'base_input_tokens': usage['base_input'],
        'cache_creation_input_tokens': usage['creation'], 'cache_write_5m': usage['write_5m'],
        'cache_write_1h': usage['write_1h'], 'cache_ttl_complete': usage['valid_ttl'],
        'web_search_requests': usage['searches'], 'web_fetch_requests': usage['fetches'],
        'usage': {'input_tokens': whole_input, 'cached_input_tokens': usage['reads'],
                  'output_tokens': usage['output'], 'reasoning_output_tokens': usage['thinking'],
                  'total_tokens': whole_input + usage['output']},
        'partial': owner['partial'], 'replayed': False, 'plan': None, 'plan_source': None,
        'quality_flags': sorted(flags),
    }


def _timing_anchor(copy):
    return (copy['blocks'][0]['req_ts']
            if not copy['partial'] and copy['timestamp_quality'] == 'original' else None)


def _response_copies(result):
    """Only assistant response facts are usage; cost snapshots never fill gaps."""
    return result['responses']


def _group_copies(copies):
    out = collections.defaultdict(list)
    for copy in copies:
        out[copy['response_key']].append(copy)
    return out


def _time_order(row):
    ts = epoch(row['ts'])
    return ts is None, ts if ts is not None else 0, row['source_id'], row['response_key']


def _floor_start(row, previous):
    anchor = epoch(row['req_ts'])
    end = epoch(previous['ts']) if previous else None
    return previous['ts'] if anchor is not None and end is not None and end > anchor else row['req_ts']


def _family_summary(family, rows, attribution):
    dated = sorted({epoch(row['ts']) for row in rows if epoch(row['ts']) is not None})
    active = sum(b - a for a, b in zip(dated, dated[1:]) if 0 <= b - a <= 1800)
    ordered = sorted(rows, key=_time_order)
    models = collections.Counter()
    for row in rows:
        models[row['model']] += row['usage']['input_tokens']
    model = min(models, key=lambda name: (-models[name], name))
    return {'family_id': family, 'streams': sorted({r['stream_id'] for r in rows}),
            'start': ordered[0]['ts'], 'end': ordered[-1]['ts'],
            'local_day': ordered[0]['local_day'], 'active_s': round(active),
            'responses': len(rows), 'input': sum(r['usage']['input_tokens'] for r in rows),
            'cached': sum(r['usage']['cached_input_tokens'] for r in rows),
            'output': sum(r['usage']['output_tokens'] for r in rows),
            'reasoning': sum(r['usage']['reasoning_output_tokens'] or 0 for r in rows),
            'model': model, 'orphan_main': any(attribution[r['source_id']]['orphan_main'] for r in rows)}


def _fact_identity(item, key):
    if key == 'reading_key':
        # Different sourced measurements are not copies merely because they share a
        # record UUID. Keep both so Round 6 can diagnose a same-time quote conflict.
        return item[key] + ':' + _digest([item['kind'], item['scope_key'],
                                          item['percent'], item['resets_at']])
    return item[key]


def _unique_facts(items, key, attribution, counters, duplicate_counter='auxiliary_fact_copies'):
    out, revisions = {}, collections.defaultdict(set)
    for item in sorted(items, key=lambda item: _owner_rank(item, attribution)):
        identity = _fact_identity(item, key)
        if identity in out:
            bump(counters, duplicate_counter)
        else:
            out[identity] = copylib.deepcopy(item)
            revisions[item[key]].add(identity)
    if key == 'reading_key':
        bump(counters, 'limit_quote_conflict', sum(1 for variants in revisions.values() if len(variants) > 1))
    return [out[identity] for identity in sorted(out)]


def _turn_facts(results, attribution):
    """Assign stream turns by opener identity, not source-local ordinal.

    A shortened continuation can begin at its own turn zero while the original stream
    already has turns. The opener UUID preserves that distinction without user content.
    """
    counters, openers, references, logged = {}, {}, {}, []
    for result in sorted(results, key=lambda r: _owner_rank(r, attribution)):
        source_id = result['source_id']
        stream = attribution[source_id]['stream_id']
        for fact in result['turns']:
            if fact['record_key'] is not None:
                key = (stream, fact['record_key'])
                references[(source_id, fact['turn'])] = key
                if key in openers:
                    bump(counters, 'auxiliary_fact_copies')
                else:
                    openers[key] = dict(fact, stream_id=stream)
            if fact['logged_record_key'] is not None:
                logged.append((source_id, fact))
    turn_refs, result, next_turn = {}, [], collections.Counter()
    for key, fact in sorted(openers.items(), key=lambda pair: (
            pair[0][0], epoch(pair[1]['start']) is None,
            epoch(pair[1]['start']) or 0, pair[0][1])):
        fact['turn'] = next_turn[fact['stream_id']]
        next_turn[fact['stream_id']] += 1
        result.append(fact)
        turn_refs[key] = fact
    seen = set()
    for source_id, fact in logged:
        key = references.get((source_id, fact['turn']))
        opener = turn_refs.get(key)
        identity = (attribution[source_id]['stream_id'], fact['logged_record_key'])
        if identity in seen:
            bump(counters, 'auxiliary_fact_copies')
        elif opener is None:
            bump(counters, 'logged_turn_unmatched')
        else:
            seen.add(identity)
            if opener['logged_record_key'] is None:
                opener.update(logged_record_key=fact['logged_record_key'],
                              logged_duration_ms=fact['logged_duration_ms'])
            else:
                result.append(dict(fact, stream_id=opener['stream_id'], turn=opener['turn']))
    mapping = {reference: turn_refs[key]['turn'] for reference, key in references.items()}
    return result, mapping, counters


def _tools(results, owners, attribution, counters):
    starts, seen = {}, set()
    for owner in owners:
        stream = attribution[owner['source_id']]['stream_id']
        for block in owner['blocks']:
            for fact in block['tool_starts']:
                if owner['timestamp_quality'] != 'original':
                    bump(counters, 'tool_replayed')
                    continue
                key = (stream, fact['tool_key'])
                if key in starts:
                    bump(counters, 'auxiliary_fact_copies')
                else:
                    starts[key] = dict(fact, stream_id=stream)
    matches = collections.defaultdict(list)
    for result in sorted(results, key=lambda r: _source_rank(attribution[r['source_id']])):
        stream = attribution[result['source_id']]['stream_id']
        for fact in result['tool_results']:
            key = (stream, fact['tool_key'])
            identity = (stream, fact['record_key'], fact['call_id'])
            if identity in seen:
                bump(counters, 'auxiliary_fact_copies')
                continue
            seen.add(identity)
            if key in starts:
                matches[key].append(fact)
            else:
                bump(counters, 'tool_unmatched_result')
    out = []
    for key, start in sorted(starts.items()):
        found = matches[key]
        if len(found) != 1:
            bump(counters, 'tool_identity_conflict' if found else 'tool_unmatched_call')
            continue
        end = found[0]
        a, b = epoch(start['ts']), epoch(end['ts'])
        out.append({'tool_key': start['tool_key'], 'stream_id': start['stream_id'],
                    'name': start['name'], 'ts': start['ts'], 'end_ts': end['ts'],
                    'seconds': None if a is None or b is None else b - a})
    return out


def _cost_diagnostics(checks, rows, counters):
    by_model = collections.defaultdict(list)
    for row in rows:
        by_model[(row['source_id'], row['model'], row['context_1m'])].append(row)
    for check in checks:
        if check['source'] != 'cost_state':
            continue
        matching = [row for row in by_model[(check['source_id'], check['model'], check['context_1m'])]
                    if check['ts'] is None or (epoch(row['ts']) is not None
                                               and epoch(row['ts']) <= epoch(check['ts']))]
        if not matching:
            bump(counters, 'cost_state_unmatched_models')
            continue
        captured = {'base_input': sum(r['base_input_tokens'] for r in matching),
                    'creation': sum(r['cache_creation_input_tokens'] for r in matching),
                    'reads': sum(r['usage']['cached_input_tokens'] for r in matching),
                    'output': sum(r['usage']['output_tokens'] for r in matching)}
        if any(check['counts'][name] is not None and check['counts'][name] != value
               for name, value in captured.items()):
            bump(counters, 'cost_state_count_mismatches')


def _historical_results(live, history, counters):
    """Reconcile a checked plain-data capture before lineage and ownership."""
    from .history import (FACT_KINDS, _incoming_copy, _incoming_facts, _normalize_turns, _restore_copy,
                          _restore_fact, _same_copy, _safe_source,
                          merge_response_copy)
    sources, retained, facts, snapshots = history
    state = facts['state']
    for name, count in state['counters'].items():
        if name.startswith(('history_', 'share_')) and name != 'history_conflicting_revisions':
            bump(counters, name, count)
    previous = {s['source_id']: s for s in sources}
    current = {r['source_id']: r for r in live}
    combined = {}
    uncaptured = False
    for sid in sorted(set(previous) | set(current)):
        old, incoming = previous.get(sid), current.get(sid)
        if incoming is not None and incoming['stable_read']:
            safe = _safe_source(incoming, old)
            source = dict(incoming, **safe)
            if old is None or _digest(safe) != old['_history_sha256']:
                uncaptured = True
        elif old is not None:
            source = dict(old)
        else:
            source = dict(incoming)
        source['counters'] = dict(source['counters'])
        source['responses'] = []
        for kind in FACT_KINDS:
            source[kind] = []
        # Unstable results contribute diagnostics only, never source attributes/facts.
        if incoming is not None and not incoming['stable_read']:
            for name, count in incoming['counters'].items():
                source['counters'][name] = max(count, source['counters'].get(name, 0))
        for name, count in state.get('read_counters', {}).get(sid, {}).items():
            source['counters'][name] = max(count, source['counters'].get(name, 0))
        combined[sid] = source
    fact_entries = collections.defaultdict(list)
    for kind in FACT_KINDS:
        for entry in facts[kind]:
            combined[entry['source_id']][kind].append(entry['value'])
            fact_entries[(kind, entry['source_id'])].append(entry)
    source_list = list(combined.values())
    old_copies = {(c['response_key'], c['source_id']): c for c in retained}
    captured_keys = set(state.get('_captured_live_copies', ()))
    capture_complete = '_captured_live_copies' in state
    incoming_copies = {}
    for sid, incoming in sorted(current.items()):
        if not incoming['stable_read']:
            continue
        if capture_complete:
            continue
        incoming = _normalize_turns(incoming, combined[sid]['turns'])
        incoming_copies.update({(c['response_key'], sid): _incoming_copy(c, old_copies.get((c['response_key'], sid)))
                                for c in incoming['responses']})
        for kind in FACT_KINDS:
            entries = fact_entries[(kind, sid)]
            digests = {entry['sha256'] for entry in entries}
            keyed = kind in ('links', 'tool_starts', 'tool_results')
            seen = {entry['fact_key'].split(':', 1)[0] for entry in entries} if keyed else set(digests)
            for fact_key, safe, digest in _incoming_facts(kind, incoming[kind], entries):
                key = fact_key if keyed else digest
                if digest not in digests:
                    uncaptured = True
                if key not in seen:
                    combined[sid][kind].append(_restore_fact(kind, safe, source_list))
                    seen.add(key)
    for key in sorted(set(old_copies) | set(incoming_copies)):
        candidate = incoming_copies.get(key)
        incoming = candidate[0] if candidate is not None else None
        old = old_copies.get(key)
        if key[1] in current and not current[key[1]]['stable_read']:
            incoming = {'stable_read': False}
        if old is not None and (key in captured_keys or
                               (candidate is not None and candidate[1] == old['_history_sha256'])):
            merged = old
        else:
            merged, _ = merge_response_copy(old, incoming)
            if merged is not None and (old is None or not _same_copy(merged, old)):
                uncaptured = True
        if merged is not None:
            combined[key[1]]['responses'].append(_restore_copy(merged, combined[key[1]]))
    live_keys = {c['response_key'] for r in live if r['stable_read'] for c in r['responses']}
    live_blocks = {(c['response_key'], b['api_block_index'] if b['api_block_index'] is not None else b['record_key'])
                   for r in live if r['stable_read'] for c in r['responses'] for b in c['blocks']}
    retained_blocks = {(c['response_key'], b['api_block_index'] if b['api_block_index'] is not None else b['record_key'])
                       for c in retained for b in c['blocks']}
    removed = {c['response_key'] for c in retained} - live_keys
    bump(counters, 'archived_sources', len(set(previous) - set(current)))
    bump(counters, 'retained_removed_responses', len(removed))
    bump(counters, 'retained_removed_blocks', len(retained_blocks - live_blocks))
    calendars = {entry['value']['response_key']: entry['value'] for entry in facts['calendar']}
    state = dict(state)
    if uncaptured:
        state['history_committed'] = False
    return list(combined.values()), snapshots, state, calendars, live_keys


def build(results, *, history=None, account_snapshots=(), now=None) -> LedgerResult:
    """Canonicalize every source before any caller applies date or session filters."""
    results = list(results.values()) if isinstance(results, dict) else list(results)
    live_content = [(r['source_id'], f) for r in results if r['stable_read']
                    for f in r['content'] + [f for c in r['responses'] for b in c['blocks'] for f in b['content']]]
    live_events = {f['event_key']: True for r in results if r['stable_read'] for f in r['events']}
    live_starts = {f['tool_key'] for r in results if r['stable_read'] for f in r['tool_starts']}
    live_tool_results = {f['tool_key'] for r in results if r['stable_read'] for f in r['tool_results']}
    live_logged = {f['logged_record_key']: True for r in results if r['stable_read']
                   for f in r['turns'] if f['logged_record_key'] is not None}
    counters = {}
    state, calendars, live_keys = None, {}, set()
    if history is not None:
        results, snapshots, state, calendars, live_keys = _historical_results(results, history, counters)
        account_snapshots = list(account_snapshots) + snapshots
        account_snapshots = list({_digest(s): s for s in account_snapshots}.values())
    recomputed = {'limit_events', 'limit_readings', 'limit_429_assumed_all_models',
                  'limit_events_undated', 'limit_event_invalid_timestamp',
                  'compaction_boundaries', 'compaction_usage_unavailable',
                  'prompt_growth_negative', 'prompt_growth_compaction_skipped',
                  'prompt_growth_model_change_skipped'}
    for result in results:
        for name, count in result['counters'].items():
            if name not in recomputed:
                bump(counters, name, count)
    links = [link for result in results for link in result['links']]
    attribution, family_counters = resolve_families(links, results)
    for name, count in family_counters.items():
        bump(counters, name, count)
    collapsed_copies = []
    for result in results:
        for original in _response_copies(result):
            collapsed, block_counters = collapse_blocks(original)
            for name, count in block_counters.items():
                if name not in ('response_input_conflict', 'response_terminal_usage_invalid'):
                    bump(counters, name, count)
            collapsed_copies.append(collapsed)
    copies = _group_copies(collapsed_copies)
    rows, owners, day_spans = [], [], {}
    for key in sorted(copies):
        owner, _classified, owner_counters = choose_owner(copies[key], attribution)
        for name, count in owner_counters.items():
            bump(counters, name, count)
        if owner is None:
            bump(counters, 'responses_excluded')
            continue
        if attribution[owner['source_id']]['family_id'] is None:
            bump(counters, 'response_owner_unavailable')
        rows.append(_response_row(owner, attribution[owner['source_id']], counters, day_spans))
        owners.append(owner)
    if history is not None:
        from .history import _freeze_calendar
        for i, row in enumerate(rows):
            captured = calendars.get(row['response_key'])
            rows[i], calendar_counters = _freeze_calendar(row, captured['calendar'] if captured else None)
            rows[i]['archived'] = row['response_key'] not in live_keys
            for name, count in calendar_counters.items():
                bump(counters, name, count)
        bump(counters, 'archived_responses', sum(r['archived'] for r in rows))
    turns, turn_mapping, turn_counters = _turn_facts(results, attribution)
    for name, value in turn_counters.items():
        bump(counters, name, value)
    for row in rows:
        row['turn'] = turn_mapping.get((row['source_id'], row['turn']))
    by_stream, families = collections.defaultdict(list), collections.defaultdict(list)
    for row in rows:
        by_stream[row['stream_id']].append(row)
        if row['family_id'] is not None:
            families[row['family_id']].append(row)
    for stream_rows in by_stream.values():
        stream_rows.sort(key=_time_order)
        previous = None
        for index, row in enumerate(stream_rows):
            row['index'] = index
            row['req_ts'] = _floor_start(row, previous)
            previous = row
    rows.sort(key=_time_order)
    limits = _unique_facts([f for r in results for f in r['limits']], 'reading_key', attribution,
                           counters, 'limit_reading_copies')
    events = _unique_facts([f for r in results for f in r['events']], 'event_key', attribution,
                           counters, 'limit_event_copies')
    for reading in limits:
        bump(counters, 'limit_readings')
        if reading['source'] == 'quota_429':
            bump(counters, 'limit_429_assumed_all_models')
    for event in events:
        bump(counters, 'limit_events')
        if event['ts'] is None:
            bump(counters, 'limit_events_undated')
            bump(counters, 'limit_event_invalid_timestamp')
    compactions = []
    seen = set()
    for result in sorted(results, key=lambda r: _source_rank(attribution[r['source_id']])):
        for fact in result['compactions']:
            if fact['record_key'] in seen:
                bump(counters, 'auxiliary_fact_copies')
            else:
                seen.add(fact['record_key'])
                compactions.append(copylib.deepcopy(fact))
                bump(counters, 'compaction_boundaries')
                bump(counters, 'compaction_usage_unavailable')
    checks = _unique_facts([f for r in results for f in r['cost_checks']], 'record_key', attribution, counters)
    _cost_diagnostics(checks, rows, counters)
    from . import composition
    content = []
    for source in sorted(results, key=lambda r: _source_rank(attribution[r['source_id']])):
        family = attribution[source['source_id']]['family_id']
        content.extend(dict(f, family_id=family) for f in composition.latest_items(source['content']))
    for owner in owners:
        family = attribution[owner['source_id']]['family_id']
        content.extend(dict(f, family_id=family) for b in owner['blocks'] for f in b['content'])
    content, content_counters = composition.combine(content, {})
    for name, count in content_counters.items():
        bump(counters, name, count)
    for stream, stream_rows in by_stream.items():
        boundaries = [f for r in results if attribution[r['source_id']]['stream_id'] == stream
                      for f in r['compactions']]
        for name, count in composition.prompt_growth(stream_rows, boundaries).items():
            bump(counters, name, count)
    result = {
        'rows': rows, 'by_stream': dict(by_stream),
        'families': {family: _family_summary(family, family_rows, attribution)
                     for family, family_rows in sorted(families.items())},
        'limits': limits, 'events': events, 'content': content,
        'tools': _tools(results, owners, attribution, counters),
        'turns': turns, 'compactions': compactions,
        'cost_checks': checks, 'account_snapshots': copylib.deepcopy(list(account_snapshots)),
        'counters': counters,
        '_live_content_keys': {f['item_key']: True for _sid, f in live_content},
        '_live_snapshot_keys': [[attribution[sid]['family_id'], f['category'], f['body_digest']]
                                for sid, f in live_content if f['snapshot'] and f['body_digest'] is not None],
        '_live_event_keys': live_events,
        '_live_tool_keys': {key: True for key in live_starts & live_tool_results},
        '_live_logged_keys': live_logged,
        '_source_families': {sid: {k: info[k] for k in ('session_id', 'family_id', 'stream_id')}
                             for sid, info in attribution.items()},
        'coverage': {'history_available': False, 'history_committed': False, 'token_bound': False,
                     'calendar_completeness': 'unknown', 'captured_responses': len(rows),
                     'archived_responses': 0, 'safe_months': [], 'withheld_months': {},
                     'windows_replace_safe': False, 'window_reasons': []},
    }
    if state is not None:
        result['coverage'].update(history_available=state['history_available'],
                                  history_committed=state['history_committed'], token_bound=True,
                                  archived_responses=counters.get('archived_responses', 0))
        result['_retention'] = {'captured': calendars, 'fact_digests': state['fact_digests']}
        from .history import month_coverage
        result['coverage'] = month_coverage(result, ())
    return result
