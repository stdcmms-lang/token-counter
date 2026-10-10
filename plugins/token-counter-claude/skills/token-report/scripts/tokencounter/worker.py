"""Per-file strict extraction of Claude's recorded facts.

One transcript is one unit of work. Dispatch is by shape, never CLI version, and required
counts are JSON integers without coercion. The result retains identifiers and measurements,
not transcript bodies. Optional response diagnostics travel with the terminal usage so the
ledger counts them once after global deduplication.
"""
import datetime
import hashlib
import json
import math
import re
from pathlib import Path

from . import rollout
from .models import FACT_CODEC, FileResult, bump

REQUIRED = ('input_tokens', 'cache_creation_input_tokens',
            'cache_read_input_tokens', 'output_tokens')
MODEL_RE = re.compile(r'^claude-[a-z0-9]+(?:-[a-z0-9]+)*$')
MARKER_RE = re.compile(r'^[a-z0-9_-]{1,80}$')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'none')
ALIASES = {'claude-haiku-4-5-20251001': 'claude-haiku-4-5'}
METADATA_RECORDS = frozenset((
    'mode', 'atis-latch', 'last-prompt', 'bridge-session', 'ai-title', 'queue-operation',
    'permission-mode', 'custom-title', 'agent-name', 'file-history-delta', 'worktree-state',
    'relocated', 'file-history-snapshot', 'frame-link', 'pr-link',
    'artifact-autoreact-ledger', 'artifact-comment-monitor',
))
PROMPT_ATTACHMENTS = frozenset((
    'total_tokens_reminder', 'batching_reminder_sent', 'bash_output_audience_note',
    'environment', 'queued_command', 'silent_turn_reminder', 'file', 'edited_text_file',
    'date', 'instructions', 'skill_listing', 'nested_memory',
))
FINGERPRINT_COUNTERS = {}


def epoch(value):
    """Epoch seconds for an offset ISO timestamp, including milliseconds/microseconds."""
    if not isinstance(value, str) or len(value) < 20:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value[:-1] + '+00:00'
                                             if value.endswith('Z') else value)
        return dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _string(value):
    return value if isinstance(value, str) and value else None


def _integer(value):
    return type(value) is int and value >= 0


def _required_usage(value):
    if not isinstance(value, dict) or any(field not in value for field in REQUIRED):
        return None, 'usage_required_missing'
    if any(type(value[field]) is not int for field in REQUIRED):
        return None, 'usage_required_type_invalid'
    if any(value[field] < 0 for field in REQUIRED):
        return None, 'usage_required_negative'
    return tuple(value[field] for field in REQUIRED), None


def _response_identity(record):
    message = record.get('message')
    mid = _string(message.get('id')) if isinstance(message, dict) else None
    rid = _string(record.get('requestId'))
    return _digest([mid, rid]) if mid is not None and rid is not None else None


def _record_kind(record):
    return record.get('type')


def _served_model(record):
    message = record.get('message')
    return message.get('model') if isinstance(message, dict) else None


def _chargeable(record):
    message = record.get('message')
    if isinstance(message, dict) and message.get('model') == '<synthetic>':
        return 'synthetic_records'
    if record.get('isApiErrorMessage') is True:
        return 'api_error_records'
    return None


def _model_name(raw):
    if not isinstance(raw, str):
        return 'unknown', None, False
    raw = raw.strip().lower()
    context = raw.endswith('[1m]')
    model = raw[:-4] if context else raw
    if not MODEL_RE.fullmatch(model):
        return 'unknown', None, False
    return ALIASES.get(model, model), raw, context


def _optional_count(box, field, missing, invalid, ceiling=None):
    if box is not None and not isinstance(box, dict):
        return None, invalid
    if not isinstance(box, dict) or field not in box or box[field] is None:
        return None, missing
    value = box[field]
    if not _integer(value) or (ceiling is not None and value > ceiling):
        return None, invalid
    return value, None


def _ttl(value, creation):
    box = value.get('cache_creation')
    if box is None:
        return (0, 0, True, None) if creation == 0 else (None, None, False, 'cache_ttl_missing')
    if not isinstance(box, dict):
        return None, None, False, 'cache_ttl_invalid'
    a, b = box.get('ephemeral_5m_input_tokens'), box.get('ephemeral_1h_input_tokens')
    if a is None or b is None:
        return (0, 0, True, None) if creation == 0 else (None, None, False, 'cache_ttl_missing')
    if not _integer(a) or not _integer(b) or a + b != creation:
        return None, None, False, 'cache_ttl_invalid'
    return a, b, True, None


def _iterations(value, required):
    iterations = value.get('iterations')
    if iterations is None:
        return None, None, []
    if not isinstance(iterations, list):
        return None, None, ['iterations_disagree']
    tuples = [_required_usage(item)[0] for item in iterations]
    # Top-level counts are authoritative even when several iterations disagree.
    agree = len(tuples) == 1 and tuples[0] == required
    flags = ['iterations_multiple'] if len(tuples) > 1 else []
    if not agree:
        flags.append('iterations_disagree')
    return len(iterations), agree, flags


def _usage_fact(value):
    required, reason = _required_usage(value)
    if reason:
        return None, reason
    base, creation, reads, output = required
    flags = []
    thinking, reason = _optional_count(value.get('output_tokens_details'), 'thinking_tokens',
                                       'reasoning_missing', 'reasoning_invalid', output)
    if reason:
        flags.append(reason)
    w5, w1, valid_ttl, ttl_reason = _ttl(value, creation)
    if ttl_reason:
        flags.append(ttl_reason)
    tools = value.get('server_tool_use')
    searches, search_reason = _optional_count(tools, 'web_search_requests', 'web_search_count_missing',
                                              'web_search_count_invalid')
    fetches, fetch_reason = _optional_count(tools, 'web_fetch_requests', 'web_fetch_count_missing',
                                           'web_fetch_count_invalid')
    flags.extend(reason for reason in (search_reason, fetch_reason) if reason)
    speed = _string(value.get('speed'))
    if speed is None:
        flags.append('speed_missing' if value.get('speed') is None else 'speed_unknown')
        speed = None if value.get('speed') is None else 'unknown'
    elif speed not in ('standard', 'fast'):
        speed = 'unknown'
        flags.append('speed_unknown')
    geo = _string(value.get('inference_geo'))
    if geo is None:
        flags.append('inference_geo_missing' if value.get('inference_geo') is None
                     else 'inference_geo_unknown')
        geo = None if value.get('inference_geo') is None else 'unknown'
    elif geo not in ('global', 'us', 'not_available'):
        geo = 'unknown'
        flags.append('inference_geo_unknown')
    service = _string(value.get('service_tier'))
    if value.get('service_tier') is not None and (service is None or not MARKER_RE.fullmatch(service)):
        service = None
        flags.append('optional_metadata_invalid')
    count, agree, iteration_flags = _iterations(value, required)
    flags.extend(iteration_flags)
    if value.get('fallback_credit') is not None:
        flags.append('fallback_credit_nonnull')
    return {
        'base_input': base, 'creation': creation, 'reads': reads, 'output': output,
        'thinking': thinking, 'write_5m': w5, 'write_1h': w1, 'searches': searches,
        'fetches': fetches, 'speed': speed, 'service_tier': service, 'inference_geo': geo,
        'iterations_count': count, 'iterations_agree': agree, 'valid_ttl': valid_ttl,
        'invalid_optional_fields': sorted(set(flags)),
    }, None


def _partial(aborted, truncated):
    return aborted or truncated


def _effort(effort, per_turn_effort):
    a = effort if effort in EFFORTS else None
    b = per_turn_effort if per_turn_effort in EFFORTS else None
    if effort is not None and a is None:
        return None, 'effort_missing'
    if a is not None and b is not None and a != b:
        return None, 'effort_conflict'
    return (a or b), None if (a or b) is not None else 'effort_missing'


def _stamp(value, counters, *, required=False):
    if epoch(value) is not None:
        return value
    if value is not None or required:
        bump(counters, 'record_timestamp_invalid')
    return None


def _reset(value):
    if isinstance(value, str):
        return epoch(value)
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _event_key(record):
    uuid = _string(record.get('uuid'))
    if uuid:
        return _digest(['event', uuid])
    rid, ts = _string(record.get('requestId')), _string(record.get('timestamp'))
    quota = record.get('quotaLimits')
    quota = quota if isinstance(quota, dict) else {}
    if rid and ts:
        return _digest(['event', rid, ts, 429, quota.get('rateLimitType'),
                        _reset(quota.get('resetsAt'))])
    return None


def _full_reading(record):
    quota = record.get('quotaLimits')
    return (record.get('isApiErrorMessage') is True
            and type(record.get('apiErrorStatus')) is int and record['apiErrorStatus'] == 429
            and isinstance(quota, dict) and quota.get('status') == 'rejected'
            and quota.get('rateLimitType') == 'seven_day'
            and _reset(quota.get('resetsAt')) is not None)


def _rate_facts(record, source_id, ts, counters):
    events, limits = [], []
    if record.get('isApiErrorMessage') is not True or type(record.get('apiErrorStatus')) is not int:
        return events, limits
    if record['apiErrorStatus'] != 429:
        return events, limits
    key = _event_key(record)
    if key is None:
        bump(counters, 'limit_event_missing_identity')
        return events, limits
    quota = record.get('quotaLimits')
    quota = quota if isinstance(quota, dict) else {}
    kind = quota.get('rateLimitType')
    kind = kind if kind in ('seven_day', 'five_hour', 'seven_day_sonnet') else None
    if kind is None:
        bump(counters, 'limit_event_unknown_kind')
    reset = _reset(quota.get('resetsAt'))
    events.append({'event_key': key, 'source_id': source_id, 'ts': ts,
                   'kind': kind, 'resets_at': reset})
    bump(counters, 'limit_events')
    if ts is None:
        bump(counters, 'limit_event_invalid_timestamp')
        bump(counters, 'limit_events_undated')
    if _full_reading(record) and ts is not None:
        limits.append({'reading_key': _digest(['quota_429', key]), 'source_id': source_id,
                       'ts': ts, 'kind': 'weekly_all', 'scope_key': None, 'percent': 100,
                       'resets_at': reset, 'source': 'quota_429'})
        bump(counters, 'limit_readings')
        bump(counters, 'limit_429_assumed_all_models')
    elif quota.get('status') == 'rejected' and quota.get('rateLimitType') == 'seven_day':
        bump(counters, 'limit_reset_invalid' if reset is None else 'limit_reading_invalid')
    return events, limits


def _report_readings(record, source_id, record_key, ts, counters):
    report = record.get('usageReport')
    if not isinstance(report, dict):
        bump(counters, 'limit_reading_invalid')
        return []
    bump(counters, 'limit_reports')
    rate = report.get('rate_limits')
    items = rate.get('limits') if isinstance(rate, dict) else None
    if not isinstance(items, list):
        bump(counters, 'limit_reading_invalid')
        return []
    out = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            bump(counters, 'limit_reading_invalid')
            continue
        kind = item.get('kind')
        if kind not in ('session', 'weekly_all', 'weekly_scoped'):
            bump(counters, 'limit_unknown_kind')
            continue
        reset = _reset(item.get('resets_at'))
        if reset is None:
            bump(counters, 'limit_reset_invalid')
            continue
        pct = item.get('percent')
        if (ts is None or type(pct) not in (int, float) or not math.isfinite(pct)
                or not 0 <= pct <= 100):
            bump(counters, 'limit_reading_invalid')
            continue
        scope = item.get('scope')
        if kind == 'weekly_scoped' and not isinstance(scope, dict):
            bump(counters, 'limit_unknown_scope')
            continue
        # Scope is an identity digest only; display names and future nested fields are
        # never retained as text or aliased to an all-model reading.
        scope_key = _digest(scope) if kind == 'weekly_scoped' else None
        out.append({'reading_key': _digest(['usage_report', record_key, index]),
                    'source_id': source_id, 'ts': ts, 'kind': kind, 'scope_key': scope_key,
                    'percent': pct, 'resets_at': reset, 'source': 'usage_report'})
        bump(counters, 'limit_readings')
    return out


def _cost_checks(record, source_id, record_key, ts, counters):
    if record.get('type') == 'cost-state':
        box, source = record.get('modelUsage'), 'cost_state'
        bump(counters, 'cost_state_records')
    else:
        report = record.get('usageReport')
        session = report.get('session') if isinstance(report, dict) else None
        box = session.get('model_usage') if isinstance(session, dict) else None
        source = 'usage_report'
    if box is None:
        return []
    if not isinstance(box, dict):
        bump(counters, 'cost_check_invalid')
        return []
    out = []
    fields = {'base_input': 'inputTokens', 'creation': 'cacheCreationInputTokens',
              'reads': 'cacheReadInputTokens', 'output': 'outputTokens',
              'thinking': 'thinkingTokens', 'searches': 'webSearchRequests'}
    for raw_model, item in sorted(box.items()):
        model = _model_name(raw_model)[0]
        if model == 'unknown' or not isinstance(item, dict):
            bump(counters, 'cost_check_invalid')
            continue
        counts, invalid = {}, False
        for name, field in fields.items():
            value = item.get(field)
            counts[name] = value if _integer(value) else None
            invalid = invalid or (value is not None and not _integer(value))
        cost = item.get('costUSD')
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            invalid = invalid or cost is not None
            cost = None
        basis = item.get('costBasis')
        basis = basis if basis in ('list', 'actual') else None
        if invalid:
            bump(counters, 'cost_check_invalid')
        out.append({'record_key': _digest([record_key, model]), 'source_id': source_id,
                    'ts': ts, 'source': source, 'model': model, 'cost_usd': cost,
                    'cost_basis': basis, 'counts': counts})
        if source == 'usage_report':
            bump(counters, 'usage_report_cost_crosschecks')
    return out


def _prompt_side(record):
    if record.get('type') == 'user':
        if record.get('isCompactSummary') is True:
            return 'summary'
        message = record.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, str):
            return 'turn'
        if isinstance(content, list):
            if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
                return 'input'
            if any(isinstance(b, dict) and b.get('type') == 'text' for b in content):
                return 'turn'
    if record.get('type') == 'attachment':
        attachment = record.get('attachment')
        if isinstance(attachment, dict) and attachment.get('type') in PROMPT_ATTACHMENTS:
            # Recognise prompt-bearing fields only. No text is copied into a fact.
            if any(field in attachment for field in ('text', 'content', 'snippet', 'files')):
                return 'input'
    return None


def _source(path):
    path = Path(path).absolute()
    child = path.parent.name == 'subagents'
    session = path.parent.parent.name if child else path.stem
    thread = path.stem if child else 'main'
    if child and thread.startswith('agent-'):
        thread = thread[6:]
    return {'source_id': _digest([session, thread]), 'session_id': session,
            'thread_id': thread, 'path': str(path), 'kind': 'subagent' if child else 'main',
            'first_record_ts': None, 'size': 0, 'mtime_ns': 0, 'prefix_hash': '',
            'line_count': 0, 'stable_read': False, 'last_complete_line': 0}


def _blank(path, metrics_only):
    result = _source(path)
    result.update({name: [] for name in ('responses', 'links', 'limits', 'events', 'content',
                                       'tool_starts', 'tool_results', 'turns', 'compactions',
                                       'cost_checks')})
    result.update(counters={}, metrics_only=metrics_only, fact_codec=FACT_CODEC)
    return result


def _extract_once(path, metrics_only):
    result, copies, nodes = _blank(path, metrics_only), {}, {}
    counters = result['counters']
    before = rollout.stat_key(path)
    result['size'], result['mtime_ns'] = before
    result['prefix_hash'] = rollout.prefix_hash(path)
    source_id = result['source_id']
    first, inherited, parent_last = True, False, None
    turn_number = -1
    for line, record in rollout.read_records(path, counters):
        try:
            kind = _record_kind(record)
            ts = _stamp(record.get('timestamp'), counters,
                        required=kind in ('assistant', 'user', 'attachment', 'system'))
            if first and ts is not None:
                # The first *stamped* record. Most files open with unstamped metadata
                # (mode, custom-title, fork-context-ref): 175 of 281 on the development
                # corpus, and the first stamped record is usually within five lines.
                result['first_record_ts'], first = ts, False
            uuid, parent = _string(record.get('uuid')), _string(record.get('parentUuid'))
            record_key = _digest(['uuid', uuid]) if uuid else _digest([source_id, line])
            state = dict(nodes.get(parent) or {'anchor': None, 'compact': None, 'turn': None})
            side = _prompt_side(record)
            if side == 'summary':
                state['compact'] = ts
            elif not inherited and side in ('turn', 'input'):
                state['anchor'] = ts
                if side == 'turn':
                    turn_number += 1
                    state['turn'] = turn_number
                    result['turns'].append({'stream_id': source_id, 'turn': turn_number,
                                            'start': ts, 'logged_duration_ms': None,
                                            'logged_record_key': None, 'record_key': record_key})

            if kind in ('continued-in', 'fork-context-ref'):
                fork = kind == 'fork-context-ref'
                a = _string(record.get('parentSessionId' if fork else 'sessionId'))
                b = result['session_id'] if fork else _string(record.get('continuedInSessionId'))
                agent = _string(record.get('agentId')) if fork else None
                if a is None or b is None or (fork and agent is None):
                    bump(counters, 'lineage_invalid')
                else:
                    parent_last = _string(record.get('parentLastUuid')) if fork else None
                    result['links'].append({'kind': 'fork' if fork else 'continued',
                                            'from_session': a, 'to_session': b,
                                            'agent_id': agent, 'parent_last_uuid': parent_last,
                                            'source_id': source_id, 'record_key': record_key})
                    if fork:
                        inherited = True
            elif kind == 'assistant':
                bump(counters, 'assistant_records')
                events, limits = _rate_facts(record, source_id, ts, counters)
                result['events'].extend(events)
                result['limits'].extend(limits)
                key, excluded = _response_identity(record), _chargeable(record)
                if excluded:
                    bump(counters, excluded)
                    if record.get('isApiErrorMessage') is True and excluded != 'api_error_records':
                        bump(counters, 'api_error_records')
                if key is None:
                    # A synthetic or API-error record is already counted by its own
                    # reason; it carries no requestId, and it is not a lost response.
                    if not excluded:
                        bump(counters, 'response_missing_identity')
                else:
                    message = record['message']
                    model, raw_model, _context = _model_name(_served_model(record))
                    copy = copies.get(key)
                    if copy is None:
                        copy = copies[key] = {
                            'response_key': key, 'source_id': source_id,
                            'session_id': result['session_id'],
                            'agent_id': result['thread_id'] if result['kind'] == 'subagent' else None,
                            'blocks': [], 'raw_model': raw_model,
                            'requested_model': _model_name(record.get('requestedModel'))[1],
                            'advisor_model': None,
                            'effort': None, 'per_turn_effort': None, 'aborted': False,
                            'truncated': False, 'stop_reason': None, 'terminal_record_key': None,
                            'partial': False, 'timestamp_quality': 'unknown', 'quality_flags': [],
                        }
                    flags = copy['quality_flags']
                    requested = record.get('requestedModel')
                    if requested is not None and _model_name(requested)[1] is None:
                        flags.append('optional_metadata_invalid')
                    if excluded:
                        if excluded not in flags:
                            flags.append(excluded)
                    else:
                        if _model_name(copy['raw_model'])[0] != model:
                            flags.append('response_model_conflict')
                        if model == 'unknown':
                            flags.append('model_name_invalid')
                        usage_value = message.get('usage')
                        if usage_value is None:
                            bump(counters, 'assistant_missing_usage')
                        usage, reason = _usage_fact(usage_value)
                        if reason:
                            bump(counters, reason)
                        index = record.get('apiBlockIndex')
                        if index is not None and not _integer(index):
                            bump(counters, 'block_index_invalid')
                            index = None
                        starts = []
                        content = message.get('content')
                        if isinstance(content, list):
                            for block in content:
                                if not isinstance(block, dict) or block.get('type') != 'tool_use':
                                    continue
                                call, name = _string(block.get('id')), _string(block.get('name'))
                                if call is None or name is None:
                                    bump(counters, 'tool_identity_conflict')
                                    continue
                                starts.append({'tool_key': _digest([key, call]), 'call_id': call,
                                               'stream_id': source_id, 'name': name, 'ts': ts})
                        anchor = state['anchor']
                        if anchor is not None and state['compact'] is not None:
                            anchor = max((anchor, state['compact']), key=lambda value: epoch(value)
                                         if epoch(value) is not None else float('-inf'))
                        if copy['blocks']:
                            anchor, block_turn = copy['blocks'][0]['req_ts'], copy['blocks'][0]['turn']
                        else:
                            block_turn = state['turn']
                        copy['blocks'].append({'record_key': record_key, 'uuid': uuid,
                                               'parent_uuid': parent, 'physical_line': line,
                                               'api_block_index': index, 'ts': ts, 'usage': usage,
                                               'content': [], 'tool_starts': starts,
                                               'req_ts': anchor, 'turn': block_turn})
                        result['tool_starts'].extend(starts)
                        state['calls'] = dict(state.get('calls') or {})
                        for start in starts:
                            state['calls'][start['call_id']] = start['tool_key']
                        copy['aborted'] = copy['aborted'] or record.get('isAbortedMidStream') is True
                        copy['truncated'] = copy['truncated'] or record.get('truncatedAfterOutput') is True
                        copy['partial'] = _partial(copy['aborted'], copy['truncated'])
                        if usage is not None:
                            copy['terminal_record_key'] = record_key
                            stop = message.get('stop_reason')
                            copy['stop_reason'] = stop if stop in ('tool_use', 'end_turn', 'max_tokens',
                                                                  'refusal', 'stop_sequence') else None
                            a, b = record.get('effort'), record.get('perTurnEffort')
                            copy['effort'] = (a if isinstance(a, str) and a in EFFORTS else
                                              None if a is None else 'unknown')
                            copy['per_turn_effort'] = (b if isinstance(b, str) and b in EFFORTS else
                                                       None if b is None else 'unknown')
                            if copy['effort'] == 'unknown' or copy['per_turn_effort'] == 'unknown':
                                flags.append('optional_metadata_invalid')
                        advisor = record.get('advisorModel')
                        if advisor is not None:
                            # Routine metadata: a model id on 77% of assistant records in
                            # the development corpus. Retained as a name, counted once per
                            # charged response, never a quality loss.
                            advisor_model, safe, _advisor_context = _model_name(advisor)
                            if safe is None:
                                flags.append('optional_metadata_invalid')
                            elif copy['advisor_model'] is None:
                                copy['advisor_model'] = advisor_model
            elif kind == 'user':
                bump(counters, 'nonusage_records')
                message = record.get('message')
                content = message.get('content') if isinstance(message, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get('type') != 'tool_result':
                            continue
                        call = _string(block.get('tool_use_id'))
                        if call is None:
                            bump(counters, 'tool_identity_conflict')
                            continue
                        tool_key = (state.get('calls') or {}).get(call)
                        result['tool_results'].append({'tool_key': tool_key or _digest(['result', record_key, call]),
                                                       'call_id': call, 'stream_id': source_id,
                                                       'ts': ts, 'record_key': record_key,
                                                       'parent_uuid': parent})
            elif kind == 'system':
                bump(counters, 'nonusage_records')
                subtype = record.get('subtype')
                if subtype == 'compact_boundary':
                    box = record.get('compactMetadata')
                    duration = box.get('durationMs') if isinstance(box, dict) else None
                    if duration is not None and not _integer(duration):
                        bump(counters, 'malformed_records')
                        duration = None
                    result['compactions'].append({'record_key': record_key, 'ts': ts,
                                                  'duration_ms': duration})
                    bump(counters, 'compaction_boundaries')
                    bump(counters, 'compaction_usage_unavailable')
                elif subtype == 'turn_duration':
                    duration = record.get('durationMs')
                    if not _integer(duration):
                        bump(counters, 'logged_turn_invalid')
                    elif state['turn'] is None:
                        bump(counters, 'logged_turn_unmatched')
                    else:
                        result['turns'].append({'stream_id': source_id, 'turn': state['turn'],
                                                'start': None, 'logged_duration_ms': duration,
                                                'logged_record_key': record_key, 'record_key': None})
                elif subtype == 'local_command' and 'usageReport' in record:
                    result['limits'].extend(_report_readings(record, source_id, record_key, ts, counters))
                    result['cost_checks'].extend(_cost_checks(record, source_id, record_key, ts, counters))
            elif kind == 'cost-state':
                bump(counters, 'nonusage_records')
                result['cost_checks'].extend(_cost_checks(record, source_id, record_key, ts, counters))
            elif kind == 'attachment' or kind in METADATA_RECORDS:
                bump(counters, 'nonusage_records')
            else:
                bump(counters, 'unknown_record_types')
            if uuid is not None:
                nodes[uuid] = state
            if inherited and uuid == parent_last and parent_last is not None:
                inherited = False
                # Inherited context cannot anchor the subagent's first own response.
                nodes[uuid] = {'anchor': None, 'compact': None, 'turn': None}
        except (TypeError, ValueError, UnicodeError, OverflowError):
            bump(counters, 'malformed_records')
    result['responses'] = list(copies.values())
    result['last_complete_line'] = counters.get('complete_lines', 0)
    result['line_count'] = result['last_complete_line'] + counters.get('trailing_partial_lines', 0)
    after = rollout.stat_key(path)
    result['stable_read'] = before == after and result['prefix_hash'] == rollout.prefix_hash(path)
    if not result['stable_read']:
        bump(counters, 'file_changed_during_read')
    # Content measurement is Round 5. Both modes leave content empty and count nothing
    # for composition; metrics_only already preserves every non-content fact.
    return result


def extract(path, *, metrics_only=False) -> FileResult:
    changed = 0
    for _attempt in range(2):
        try:
            result = _extract_once(path, metrics_only)
        except (OSError, RuntimeError):
            result = _blank(path, metrics_only)
            bump(result['counters'], 'extraction_errors')
            return result
        if result['stable_read']:
            bump(result['counters'], 'file_changed_during_read', changed)
            return result
        changed += 1
    # An unstable snapshot cannot replace a previous capture. Round 4's collector keeps
    # that capture; until then this result supplies damage and source metadata only.
    blank = _blank(path, metrics_only)
    for field in ('size', 'mtime_ns', 'prefix_hash', 'line_count', 'last_complete_line', 'first_record_ts'):
        blank[field] = result[field]
    blank['counters'] = result['counters']
    blank['counters']['file_changed_during_read'] = changed
    return blank


def extractor_fingerprint(*, metrics_only=False) -> int:
    """Six-byte key over named sources, fact codec, mode and strict extraction probes."""
    try:
        h = hashlib.blake2b(digest_size=6)
        directory = Path(__file__).parent
        for name in ('models.py', 'rollout.py', 'worker.py', 'ledger.py', 'composition.py',
                     'windows.py', 'images.py', 'analyze.py'):
            path = directory / name
            h.update(name.encode('ascii'))
            # The later-round sources are explicitly absent today. Their appearance
            # moves the key; an unreadable source that exists still disables reuse.
            if name in ('composition.py', 'windows.py', 'analyze.py') and not path.exists():
                h.update(b'absent')
            else:
                h.update(path.read_bytes())
        if any(_integer(value) for value in (True, -1, 1.0, '1')) or not _integer(0):
            raise ValueError('strict integer probe failed')
        probe = epoch('2026-09-10T08:00:00.123456+08:00')
        expected = datetime.datetime(2026, 9, 10, 0, 0, 0, 123456,
                                     tzinfo=datetime.timezone.utc).timestamp()
        if probe != expected:
            raise ValueError('timestamp probe failed')
        h.update(_digest(['\u00e9', {'b': 2, 'a': 1}]).encode('ascii'))
        h.update(json.dumps([FACT_CODEC, metrics_only, probe], separators=(',', ':')).encode('ascii'))
        return int.from_bytes(h.digest(), 'big')
    except (OSError, ValueError, UnicodeError):
        bump(FINGERPRINT_COUNTERS, 'extractor_fingerprint_unavailable')
        return 0
