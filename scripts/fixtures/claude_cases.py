"""Invented Claude JSONL cases for the later ledger, pricing and quota suites.

A case has ``files`` mapping relative POSIX paths to lists of records, plus
``expected`` hand calculations. Tests may edit either map before write_corpus.
No builder reads account state, transcripts, prices or any other input file.
"""
import copy
import datetime
import json
from pathlib import Path


def assistant_record(message_id, request_id, timestamp, usage, *,
                     uuid, parent_uuid=None, block_index=0,
                     model="claude-opus-5-5", effort="high",
                     content=None, flags=None) -> dict:
    """Build an assistant envelope; flags are optional envelope overrides.

    Usage and content are copied verbatim, including deliberately invalid values.
    An omitted content list is empty, so defaults add no inventory bytes. Tests
    set message.stop_reason themselves when exercising terminal-reason handling.
    """
    record = {
        "type": "assistant", "uuid": uuid, "parentUuid": parent_uuid,
        "timestamp": timestamp, "requestId": request_id,
        "apiBlockIndex": block_index, "effort": effort,
        "message": {
            "id": message_id, "type": "message", "role": "assistant",
            "model": model, "content": copy.deepcopy(content if content is not None else []),
            "usage": copy.deepcopy(usage), "stop_reason": "end_turn",
        },
    }
    if flags is not None:
        record.update(copy.deepcopy(flags))
    return record


def _usage(base, write_5m, write_1h, reads, output, thinking=0,
           searches=0, fetches=0):
    return {
        "input_tokens": base,
        "cache_creation_input_tokens": write_5m + write_1h,
        "cache_read_input_tokens": reads, "output_tokens": output,
        "output_tokens_details": {"thinking_tokens": thinking},
        "cache_creation": {
            "ephemeral_5m_input_tokens": write_5m,
            "ephemeral_1h_input_tokens": write_1h,
        },
        "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": fetches},
        "speed": "standard", "inference_geo": "global",
    }


def _user(uuid, timestamp, content, parent_uuid=None):
    return {
        "type": "user", "uuid": uuid, "parentUuid": parent_uuid,
        "timestamp": timestamp,
        "message": {"role": "user", "content": copy.deepcopy(content)},
    }


def _case(project, session, records, expected):
    for record in records:
        record["sessionId"] = session
    return {
        "project": project, "session_id": session,
        "files": {project + "/" + session + ".jsonl": records},
        "expected": expected,
    }


def baseline_B() -> dict:
    """Q12 B: two responses, three blocks, one tool, 18 visible UTF-8 bytes."""
    u0 = _user("B-U0", "2026-09-10T00:00:00Z", [{"type": "text", "text": "abcd"}])
    r10 = assistant_record(
        "B-message-1", "B-request-1", "2026-09-10T00:00:05Z",
        _usage(10, 20, 30, 40, 2, thinking=1, searches=1, fetches=2),
        uuid="B-R1-0", parent_uuid="B-U0")
    r11 = assistant_record(
        "B-message-1", "B-request-1", "2026-09-10T00:00:10Z",
        _usage(10, 20, 30, 40, 8, thinking=3, searches=1, fetches=2),
        uuid="B-R1-1", parent_uuid="B-R1-0", block_index=1,
        content=[{"type": "tool_use", "id": "B-tool-1", "name": "synthetic_tool",
                  "input": {"x": "y"}}])
    r11["message"]["stop_reason"] = "tool_use"
    u1 = _user("B-U1", "2026-09-10T00:00:12Z", [
        {"type": "tool_result", "tool_use_id": "B-tool-1", "content": "xyz"}], "B-R1-1")
    r2 = assistant_record(
        "B-message-2", "B-request-2", "2026-09-10T00:00:20Z",
        _usage(5, 0, 100, 50, 10, thinking=4, fetches=1),
        uuid="B-R2", parent_uuid="B-U1", content=[{"type": "text", "text": "ok"}])
    turn = {"type": "system", "subtype": "turn_duration", "uuid": "B-turn",
            "parentUuid": "B-R2", "timestamp": "2026-09-10T00:00:21Z", "durationMs": 19000}
    return _case("synthetic-project-B", "synthetic-session-B", [u0, r10, r11, u1, r2, turn], {
        "responses": 2, "input": 255, "cached": 90, "output": 18, "reasoning": 7,
        "base_input": 15, "cache_write_5m": 20, "cache_write_1h": 130,
        "total_tokens": 273, "families": 1, "streams": 1, "active_s": 10,
        "response_seconds": [10, 8], "median_s": 9, "p90_s": 9.8,
        "computed_turn_s": 20, "model_share": 0.9, "logged_turn_s": 19,
        "tool_seconds": [2], "inventory_bytes": 18,
        "r1_tokens_usd": 0.000548, "r1_usd": 0.010548,
        "r2_tokens_usd": 0.001030, "usd": 0.011578, "usd_high": 0.011578,
    })


def quota_W() -> dict:
    """Q12 W: jittered weekly resets, a 20->25 plateau, five responses."""
    records = []
    parent = None
    readings = {
        "18:00": (20, "2026-09-17T15:59:59.600000Z"),
        "19:00": (25, "2026-09-17T16:00:00.470000Z"),
        "20:00": (25, "2026-09-17T16:00:00.470000Z"),
    }
    # At 18:00 and 19:00 the response precedes the observation physically;
    # selection still follows the specified open/closed timestamp boundaries.
    for i, hhmm in enumerate(("17:00", "18:00", "18:30", "19:00", "19:30"), 1):
        inp, cached = i * 100, i * 40
        w5, w1 = (10, 20) if hhmm in ("18:30", "19:00") else (0, 0)
        row = assistant_record(
            "W-message-%d" % i, "W-request-%d" % i, "2026-09-10T" + hhmm + ":00Z",
            _usage(inp - cached - w5 - w1, w5, w1, cached, i * 10),
            uuid="W-R%d" % i, parent_uuid=parent)
        records.append(row)
        parent = row["uuid"]
        if hhmm in readings:
            pct, reset = readings[hhmm]
            record = _reading(hhmm, pct, reset, parent)
            records.append(record)
            parent = record["uuid"]
    pct, reset = readings["20:00"]
    records.append(_reading("20:00", pct, reset, parent))
    return _case("synthetic-project-W", "synthetic-session-W", records, {
        "reset": "2026-09-17T16:00:00Z", "nominal_start": "2026-09-10T16:00:00Z",
        "observation_start": "2026-09-10T18:00:00Z",
        "observation_end": "2026-09-10T19:00:00Z", "first_pct": 20, "peak_pct": 25,
        "counts": {"responses": 2, "input": 700, "cached": 280, "output": 70,
                   "reasoning": 0},
        "cache_write_5m": 20, "cache_write_1h": 40, "total_tokens": 770,
        "nominal_counts": {"responses": 5, "input": 1500, "cached": 600,
                           "output": 150, "reasoning": 0},
        "nominal_total_tokens": 1650, "cumulative_tokens": [110, 330, 660, 1100, 1650],
    })


def _reading(hhmm, percent, reset, parent):
    return {
        "type": "system", "subtype": "local_command", "uuid": "W-usage-" + hhmm.replace(":", ""),
        "parentUuid": parent, "timestamp": "2026-09-10T" + hhmm + ":00Z",
        "commandRun": {"command": "usage"},
        "usageReport": {"rate_limits": {"limits": [{
            "kind": "weekly_all", "group": "weekly", "percent": percent,
            "resets_at": reset, "scope": None, "severity": "normal", "is_active": True,
        }], "extra_usage": None}},
    }


def account_snapshot(observed_at, *, organization_type='claude_max',
                     rate_limit_tier='default_claude_max_5x',
                     subscription_created_at='2026-09-10T17:00:00Z') -> dict:
    """Invented five-field snapshot; account identity is never part of a fixture fact."""
    def stamp(value):
        if isinstance(value, str):
            return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        return value
    plan = {'default_claude_max_5x': 'claude:max-5x',
            'default_claude_max_20x': 'claude:max-20x'}.get(rate_limit_tier) if organization_type == 'claude_max' else None
    if organization_type == 'claude_pro' and rate_limit_tier not in ('default_claude_max_5x', 'default_claude_max_20x'):
        plan = 'claude:pro'
    return {'observed_at': stamp(observed_at), 'organization_type': organization_type,
            'rate_limit_tier': rate_limit_tier, 'current_plan': plan,
            'subscription_created_at': stamp(subscription_created_at)}


def quota_429(timestamp='2026-09-10T19:30:00Z', *, kind='seven_day', status='rejected',
              reset='2026-09-17T16:00:00Z', uuid='W-429') -> dict:
    return assistant_record('error-message', 'error-request', timestamp, {}, uuid=uuid,
        model='<synthetic>', flags={'isApiErrorMessage': True, 'apiErrorStatus': 429,
                                    'quotaLimits': {'status': status, 'rateLimitType': kind, 'resetsAt': reset}})


def quota_W_timed() -> dict:
    """W plus invented own prompts ten seconds before each response; counts unchanged."""
    case = quota_W()
    records, parent = [], None
    for record in next(iter(case['files'].values())):
        if record['type'] == 'assistant':
            end = datetime.datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
            start = (end - datetime.timedelta(seconds=10)).isoformat().replace('+00:00', 'Z')
            prompt = _user(record['uuid'] + '-prompt', start, 'invented task', parent)
            prompt['sessionId'] = case['session_id']
            records.append(prompt)
            record['parentUuid'] = prompt['uuid']
        records.append(record)
        parent = record['uuid']
    case['files'][next(iter(case['files']))] = records
    return case


def write_corpus(root, case) -> list:
    """Write complete LF-terminated JSONL, returning Paths in sorted order.

    Paths must be <project>/<session>.jsonl or
    <project>/<session>/subagents/agent-<id>.jsonl. Reject traversal and
    outside-root symlinks before opening anything; root belongs to the caller.
    """
    root = Path(root).resolve()
    pending = []
    for relative, records in sorted(case["files"].items()):
        path = Path(relative)
        parts = path.parts
        main = len(parts) == 2 and path.suffix == ".jsonl"
        child = (len(parts) == 4 and parts[2] == "subagents" and
                 parts[3].startswith("agent-") and len(path.stem) > len("agent-") and
                 path.suffix == ".jsonl")
        if (path.is_absolute() or ".." in parts or "\\" in relative or
                ":" in relative or not (main or child)):
            raise ValueError("unexpected synthetic corpus path")
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ValueError("synthetic corpus path escapes root")
        data = b"".join((json.dumps(record, ensure_ascii=False, allow_nan=False,
                                   separators=(",", ":")) + "\n").encode("utf-8")
                        for record in records)
        pending.append((target, data))
    # Validate every target before writing any file.
    for target, data in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return [target for target, _data in pending]
