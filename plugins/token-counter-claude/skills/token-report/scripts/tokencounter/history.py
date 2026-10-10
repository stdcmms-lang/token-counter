"""Durable, content-free capture evidence, independent of the disposable index.

``load`` returns (sources, response_copies, facts, account_snapshots), all plain data.
The facts dictionary also carries checked receipts and collection state. The ledger
reconciles this data before ownership; it never opens the database.

Receipt contributors use ``months[month].responses[response_key]`` snapshots, with
``counts`` and ``calendar`` dictionaries. Optional selected ``sessions`` retain their
response keys and fact digests. Window evidence uses the same response snapshots plus
sourced ``readings``. A token_binding is an opaque receipt-generation ID, allocated
before the first preparation and saved alongside the endpoint/token/history UUID by
the sharing layer. An existing binding must have a receipt in this history.
"""
import base64
import copy
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid
import zlib

from .models import COUNTER_NAMES, FACT_CODEC, CoverageResult, bump
from .worker import _digest, _model_name, epoch

SCHEMA_VERSION = 1
ACTIVE_STATUSES = ('prepared', 'confirmed', 'unknown')
MONTH_REASON = 'Withheld month {month}: previously submitted captured usage could not be reproduced safely.'
WINDOW_REASON = 'Windows withheld: replacing the stored list would lose previously submitted evidence.'
UNAVAILABLE_REASON = 'History unavailable: showing live captured usage only; sharing is disabled.'
TOKEN_REASON = 'Share token has no matching capture history; replacement is disabled.'
CALENDAR_FIELDS = ('local_day', 'day_start', 'day_end', 'calendar_signature')
SNAPSHOT_FIELDS = ('observed_at', 'organization_type', 'rate_limit_tier', 'current_plan', 'subscription_created_at')
FACT_KINDS = ('links', 'limits', 'events', 'content', 'tool_starts', 'tool_results',
              'turns', 'compactions', 'cost_checks')
DDL = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE sources(
  source_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  live INTEGER NOT NULL,
  fact_codec INTEGER NOT NULL,
  payload BLOB NOT NULL,
  payload_sha256 TEXT NOT NULL
);
CREATE TABLE response_copies(
  response_key TEXT NOT NULL,
  source_id TEXT NOT NULL,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  payload BLOB NOT NULL,
  payload_sha256 TEXT NOT NULL,
  conflicting INTEGER NOT NULL,
  PRIMARY KEY(response_key, source_id)
);
CREATE TABLE facts(
  kind TEXT NOT NULL,
  fact_key TEXT NOT NULL,
  source_id TEXT NOT NULL,
  payload BLOB NOT NULL,
  payload_sha256 TEXT NOT NULL,
  PRIMARY KEY(kind, fact_key, source_id)
);
CREATE TABLE submissions(
  submission_id TEXT PRIMARY KEY,
  endpoint TEXT NOT NULL,
  token_binding TEXT,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  confirmed_at REAL,
  payload BLOB NOT NULL,
  payload_sha256 TEXT NOT NULL,
  contributors BLOB NOT NULL,
  contributors_sha256 TEXT NOT NULL
);
"""


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _pack(value):
    raw = _json_bytes(value)
    return zlib.compress(raw, 6), hashlib.sha256(raw).hexdigest()


def _checked_digest(raw, expected):
    """The integrity decision, separate so its mutation cannot hide behind a crash."""
    return hashlib.sha256(raw).hexdigest() == expected


def _unpack(blob, digest):
    raw = zlib.decompress(blob)
    if not _checked_digest(raw, digest):
        raise ValueError('history digest mismatch')
    value = json.loads(raw.decode('utf-8'))
    if _json_bytes(value) != raw:
        raise ValueError('history JSON is not canonical')
    return value


def _now(value):
    value = time.time() if value is None else value
    if isinstance(value, str):
        value = epoch(value)
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('invalid capture time')
    return float(value)


def _fields(value, names):
    return {name: copy.deepcopy(value.get(name)) for name in names}


def _identifier(value, kind):
    return _digest([kind, value]) if value is not None else None


def _safe_usage(usage):
    if usage is None:
        return None
    return _fields(usage, ('base_input', 'creation', 'reads', 'output', 'thinking',
                          'write_5m', 'write_1h', 'searches', 'fetches', 'speed',
                          'service_tier', 'inference_geo', 'iterations_count',
                          'iterations_agree', 'valid_ttl', 'invalid_optional_fields'))


def _safe_content(fact):
    result = _fields(fact, ('item_key', 'category', 'ts', 'utf8_bytes', 'body_digest', 'snapshot'))
    result['family_id'] = None  # ownership is rebuilt, never a raw session in a blob
    image = fact.get('image')
    result['image'] = None if image is None else _fields(image, (
        'width', 'height', 'source_kind', 'transformations_known', 'model',
        'estimated_visual_tokens', 'method'))
    return result


def _safe_tool(fact, *, stored=False):
    result = _fields(fact, ('tool_key', 'stream_id', 'ts'))
    result['call_id'] = fact.get('call_id') if stored else _identifier(fact.get('call_id'), 'call')
    if 'name' in fact:
        result['name'] = None  # tool display names are live-only
    if 'record_key' in fact:
        result['record_key'] = fact['record_key']
        result['parent_uuid'] = (fact.get('parent_uuid') if stored else
                                 _identifier(fact.get('parent_uuid'), 'uuid'))
    return result


def _safe_block(block, stored=False):
    out = _fields(block, ('record_key', 'physical_line', 'api_block_index', 'ts', 'req_ts', 'turn'))
    for field in ('uuid', 'parent_uuid'):
        out[field] = block.get(field) if stored else _identifier(block.get(field), 'uuid')
    out['usage'] = _safe_usage(block.get('usage'))
    out['content'] = [_safe_content(f) for f in block.get('content', ())]
    out['tool_starts'] = [_safe_tool(f, stored=stored) for f in block.get('tool_starts', ())]
    metadata = block.get('response_metadata')
    out['response_metadata'] = None if metadata is None else _fields(metadata, (
        'raw_model', 'requested_model', 'advisor_model', 'effort', 'per_turn_effort',
        'aborted', 'truncated', 'stop_reason'))
    return out


def _safe_copy(value):
    """Storage allow-list. Raw local identifiers live only in source SQL columns."""
    stored = value.get('_history_codec') == FACT_CODEC
    out = _fields(value, ('response_key', 'source_id', 'effort', 'per_turn_effort',
                         'aborted', 'truncated', 'stop_reason', 'terminal_record_key',
                         'partial', 'timestamp_quality'))
    for field in ('raw_model', 'requested_model', 'advisor_model'):
        out[field] = _model_name(value.get(field))[1]
    out['quality_flags'] = sorted(set(value.get('quality_flags', ())) & COUNTER_NAMES)
    out['blocks'] = [_safe_block(b, stored) for b in value.get('blocks', ())]
    out['_history_codec'] = FACT_CODEC
    out['history_merged'] = value.get('history_merged', False)
    out['conflicting'] = value.get('conflicting', False)
    out['revisions'] = [_safe_block(b, stored) for b in value.get('revisions', ())]
    out['metadata_revisions'] = [_fields(r, ('raw_model', 'requested_model', 'advisor_model',
                                           'effort', 'per_turn_effort', 'stop_reason',
                                           'aborted', 'truncated'))
                                 for r in value.get('metadata_revisions', ())]
    return out


def _restore_copy(value, source):
    return dict(copy.deepcopy(value), session_id=source['session_id'],
                agent_id=source['thread_id'] if source['kind'] == 'subagent' else None)


def _block_identity(block):
    index = block.get('api_block_index')
    return ('index', index) if index is not None else ('record', block['record_key'])


EVIDENCE_FIELDS = ('record_key', 'uuid', 'parent_uuid', 'api_block_index', 'ts', 'usage',
                   'response_metadata')


def _block_evidence(block):
    """The captured record, its measurement, its parent chain and its envelope metadata.

    An allow-list, not "everything else": physical positions and source-local timing/turn
    references change when a file is shortened, and derived content facts (round 5) change
    when extraction rules do. Neither is a revision of what the server recorded.
    """
    return {k: block.get(k) for k in EVIDENCE_FIELDS}


def _later_block(block, terminal, incoming):
    if terminal is None:
        return True
    a, b = block.get('api_block_index'), terminal.get('api_block_index')
    if a is not None and b is not None:
        return a > b
    keys = [v['record_key'] for v in incoming['blocks']]
    if terminal['record_key'] in keys and block['record_key'] in keys:
        return keys.index(block['record_key']) > keys.index(terminal['record_key'])
    a, b = epoch(block.get('ts')), epoch(terminal.get('ts'))
    return a is not None and b is not None and a > b


def _block_position(blocks, new, incoming):
    """Use a common record to anchor physical order before timestamp fallbacks."""
    incoming_keys = [b['record_key'] for b in incoming['blocks']]
    own = incoming_keys.index(new['record_key'])
    positions = {b['record_key']: i for i, b in enumerate(blocks)}
    following = [key for key in incoming_keys[own + 1:] if key in positions]
    if following:
        return positions[following[0]]
    preceding = [key for key in incoming_keys[:own] if key in positions]
    if preceding:
        return positions[preceding[-1]] + 1
    index = new.get('api_block_index')
    for i, retained in enumerate(blocks):
        retained_index = retained.get('api_block_index')
        if index is not None and retained_index is not None and index < retained_index:
            return i
        a, b = epoch(new.get('ts')), epoch(retained.get('ts'))
        if (index is None or retained_index is None) and a is not None and b is not None and a < b:
            return i
    return len(blocks)


def merge_response_copy(previous, incoming) -> tuple:
    """Pure evidence union for one response in one source: (copy, counters).

    None denotes an absent incoming identity. An unstable source supplies a sentinel
    with stable_read=False. Revisions remain alongside the original blocks forever.
    Newly seen later records, rather than independent maxima, advance the terminal.
    """
    from .ledger import collapse_blocks
    counters = {}
    if incoming is None or incoming.get('stable_read') is False:
        return copy.deepcopy(previous), counters
    if previous is None:
        out, _ = collapse_blocks(incoming)
        out['history_merged'] = True
        return out, counters
    if (previous['response_key'], previous['source_id']) != (incoming['response_key'], incoming['source_id']):
        raise ValueError('different response copy identities')
    out = copy.deepcopy(previous)
    out['history_merged'] = True
    blocks = out['blocks']
    by_identity = {_block_identity(b): b for b in blocks}
    by_record = {b['record_key']: b for b in blocks}
    terminal = next((b for b in blocks if b['record_key'] == out.get('terminal_record_key')), None)
    revisions = out.setdefault('revisions', [])
    metadata = out.setdefault('metadata_revisions', [])
    changed = False
    advance = False
    for block in incoming['blocks']:
        old = by_identity.get(_block_identity(block)) or by_record.get(block['record_key'])
        if old is not None:
            if _block_evidence(old) != _block_evidence(block):
                if not any(_block_evidence(b) == _block_evidence(block) for b in revisions):
                    revisions.append(copy.deepcopy(block))
                changed = True
            continue
        new = copy.deepcopy(block)
        # Keep retained physical evidence intact. The list is the durable order used
        # by collapse_blocks, even if incoming line numbers restarted at one.
        position = _block_position(blocks, new, incoming)
        blocks.insert(position, new)
        by_identity[_block_identity(new)] = new
        by_record[new['record_key']] = new
        if new.get('usage') is not None and _later_block(new, terminal, incoming):
            terminal = new
            out['terminal_record_key'] = new['record_key']
            advance = True
    if _model_name(previous.get('raw_model'))[0] != _model_name(incoming.get('raw_model'))[0]:
        changed = True
        revision = _fields(incoming, ('raw_model', 'requested_model', 'advisor_model', 'effort',
                                      'per_turn_effort', 'stop_reason', 'aborted', 'truncated'))
        if revision not in metadata:
            metadata.append(revision)
    if advance:
        for field in ('raw_model', 'requested_model', 'advisor_model', 'effort',
                      'per_turn_effort', 'stop_reason', 'partial'):
            out[field] = copy.deepcopy(incoming.get(field))
    for field in ('aborted', 'truncated'):
        out[field] = bool(previous.get(field) or incoming.get(field))
    out['partial'] = bool(out['partial'] or out['aborted'] or out['truncated'])
    out['quality_flags'] = sorted(set(out.get('quality_flags', ())) | set(incoming.get('quality_flags', ())))
    if changed or previous.get('conflicting'):
        out['conflicting'] = True
        out['quality_flags'] = sorted(set(out['quality_flags']) | {'history_conflicting_revisions'})
        bump(counters, 'history_conflicting_revisions')
    return out, counters


def _safe_source(result, previous=None):
    out = _fields(result, ('first_record_ts', 'size', 'mtime_ns', 'prefix_hash', 'line_count',
                          'stable_read', 'last_complete_line', 'metrics_only'))
    out['counters'] = {name: value for name, value in result['counters'].items() if name in COUNTER_NAMES}
    if previous:
        a, b = epoch(previous.get('first_record_ts')), epoch(out.get('first_record_ts'))
        if a is not None and (b is None or a <= b):
            out['first_record_ts'] = previous['first_record_ts']
        for name, count in previous['counters'].items():
            out['counters'][name] = max(count, out['counters'].get(name, 0))
    return out


def _safe_fact(kind, item):
    if kind == 'links':
        out = _fields(item, ('kind', 'source_id', 'record_key'))
        out.update(from_session=_identifier(item['from_session'], 'session'),
                   to_session=_identifier(item['to_session'], 'session'),
                   agent_id=_identifier(item.get('agent_id'), 'thread'),
                   parent_last_uuid=_identifier(item.get('parent_last_uuid'), 'uuid'))
        return out
    if kind == 'content':
        return _safe_content(item)
    if kind in ('tool_starts', 'tool_results'):
        return _safe_tool(item)
    fields = {
        'limits': ('reading_key', 'source_id', 'ts', 'kind', 'scope_key', 'percent', 'resets_at', 'source'),
        'events': ('event_key', 'source_id', 'ts', 'kind', 'resets_at'),
        'turns': ('stream_id', 'turn', 'start', 'logged_duration_ms', 'logged_record_key', 'record_key'),
        'compactions': ('record_key', 'ts', 'duration_ms'),
        'cost_checks': ('record_key', 'source_id', 'ts', 'source', 'model', 'raw_model', 'context_1m',
                        'cost_usd', 'cost_basis', 'counts'),
    }
    out = _fields(item, fields[kind])
    if kind == 'cost_checks':
        out['counts'] = _fields(item['counts'], ('base_input', 'creation', 'reads', 'output', 'thinking', 'searches'))
    return out


def _fact_key(kind, item):
    if kind == 'turns':
        return item.get('logged_record_key') or item['record_key']
    if kind in ('tool_starts', 'tool_results'):
        return _digest([item['tool_key'], item.get('record_key')])
    field = {'content': 'item_key', 'limits': 'reading_key', 'events': 'event_key'}.get(kind, 'record_key')
    return item[field]


def _normalize_turns(result, retained):
    """Keep source-local references stable when re-extraction renumbers openers."""
    out = copy.deepcopy(result)
    openers = {f['record_key']: f['turn'] for f in retained if f.get('record_key') is not None}
    next_turn = max(openers.values(), default=-1) + 1
    mapping = {}
    for fact in out['turns']:
        key = fact.get('record_key')
        if key is not None:
            if key not in openers:
                openers[key] = next_turn
                next_turn += 1
            mapping[fact['turn']] = openers[key]
    for fact in out['turns']:
        fact['turn'] = mapping.get(fact['turn'], fact['turn'])
    for response in out['responses']:
        for block in response['blocks']:
            block['turn'] = mapping.get(block['turn'], block['turn'])
    return out


def _restore_fact(kind, item, sources):
    out = copy.deepcopy(item)
    if kind == 'links':
        sessions = {_identifier(s['session_id'], 'session'): s['session_id'] for s in sources}
        threads = {_identifier(s['thread_id'], 'thread'): s['thread_id'] for s in sources}
        for field in ('from_session', 'to_session'):
            out[field] = sessions.get(out[field], out[field])
        out['agent_id'] = threads.get(out['agent_id'], out['agent_id'])
    return out


def _snapshot(account):
    fields = SNAPSHOT_FIELDS
    if set(account) != set(fields):
        raise ValueError('account snapshot must contain exactly five fields')
    out = _fields(account, fields)
    for field in ('observed_at', 'subscription_created_at'):
        value = out[field]
        if value is None and field == 'subscription_created_at':
            continue
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('invalid snapshot timestamp')
    for field in ('organization_type', 'rate_limit_tier', 'current_plan'):
        if out[field] is not None and not isinstance(out[field], str):
            raise ValueError('invalid snapshot plan field')
    return out


def _account_snapshots(account):
    return [] if account is None else [_snapshot(account)]


def _row_snapshot(row):
    usage = row['usage']
    return {'counts': {'input': usage['input_tokens'], 'cached': usage['cached_input_tokens'],
                       'output': usage['output_tokens'], 'reasoning': usage['reasoning_output_tokens'] or 0},
            'calendar': _fields(row, CALENDAR_FIELDS)}


def _freeze_calendar(row, captured):
    """First captured assignment wins, including an initially undated assignment."""
    out, counters = copy.deepcopy(row), {}
    if captured is not None:
        if _fields(row, CALENDAR_FIELDS) != captured:
            bump(counters, 'calendar_context_changed')
        out.update(copy.deepcopy(captured))
    return out, counters


def _month_failures(ledger, submissions):
    rows = {r['response_key']: r for r in ledger['rows']}
    months = {r['local_day'][:7] for r in rows.values() if r['local_day'] is not None}
    required = {}
    for receipt in submissions:
        if receipt['status'] in ACTIVE_STATUSES:
            for month, evidence in receipt['contributors'].get('months', {}).items():
                months.add(month)
                required.setdefault(month, []).append(evidence)
    # The full captured set is supplied before filters. This also identifies the
    # affected month of a quarantined identity even on its first share.
    captured = ledger.get('_retention', {}).get('captured', {})
    for key, snapshot in captured.items():
        day = snapshot['calendar']['local_day']
        if day is not None:
            month = day[:7]
            months.add(month)
            required.setdefault(month, []).append({'responses': {key: {'calendar': snapshot['calendar']}}})
    flags = ledger['coverage']
    trusted = all(flags[name] for name in ('history_available', 'history_committed', 'token_bound'))
    trusted = trusted and not any(ledger['counters'].get(k) for k in (
        'history_integrity_failed', 'history_schema_unsupported', 'history_commit_failed'))
    failures = {}
    for month in sorted(months):
        reasons = set()
        if not trusted:
            reasons.add('history_unavailable')
        for evidence in required.get(month, ()):
            for key, snapshot in evidence.get('responses', {}).items():
                row = rows.get(key)
                if row is None:
                    reasons.add('months_withheld_missing_contributors')
                    continue
                current = _row_snapshot(row)
                if any(current['counts'][field] < count for field, count in snapshot.get('counts', {}).items()):
                    reasons.add('months_withheld_decreased_contributions')
                if current['calendar'] != snapshot.get('calendar', current['calendar']):
                    reasons.add('months_withheld_calendar_change')
            for session in evidence.get('sessions', ()):
                if any(key not in rows for key in session.get('responses', ())):
                    reasons.add('months_withheld_missing_contributors')
                for fact in session.get('facts', ()):
                    if fact not in ledger.get('_retention', {}).get('fact_digests', ()):
                        reasons.add('months_withheld_missing_contributors')
        failures[month] = reasons
    return failures


def month_coverage(ledger, submissions) -> CoverageResult:
    """Pure whole-month replacement decision over checked, retained evidence."""
    out = copy.deepcopy(ledger['coverage'])
    out['calendar_completeness'] = 'unknown'
    failures = _month_failures(ledger, submissions)
    out['safe_months'] = sorted(month for month, reasons in failures.items() if not reasons)
    out['withheld_months'] = {month: [MONTH_REASON.format(month=month)]
                              for month, reasons in sorted(failures.items()) if reasons}
    return out


def _window_evidence_safe(ledger, submissions):
    rows = {r['response_key']: r for r in ledger['rows']}
    readings = {_digest(_safe_fact('limits', r)) for r in ledger['limits']}
    for receipt in submissions:
        if receipt['status'] not in ACTIVE_STATUSES:
            continue
        for window in receipt['contributors'].get('windows', ()):
            for key, snapshot in window.get('responses', {}).items():
                if key not in rows:
                    return False
                current = _row_snapshot(rows[key])
                if current['calendar'] != snapshot['calendar'] or any(
                        current['counts'][k] < v for k, v in snapshot['counts'].items()):
                    return False
            if any(key not in rows for key in window.get('rows', ())):
                return False
            if any(_digest(_safe_fact('limits', r)) not in readings for r in window.get('readings', ())):
                return False
    return True


class History:
    def __init__(self, path, *, timeout=20.0):
        self.path = Path(path)
        self.db = None
        self.counters = {}
        self.history_uuid = None
        self.history_available = False
        self.history_committed = False
        self._read_counters = {}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self.path), timeout=timeout)
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                # A new capture store, not a migration/reset of an existing store.
                self.db.executescript('BEGIN;\n' + DDL)
                self.history_uuid = str(uuid.uuid4())
                self.db.executemany('INSERT INTO meta(k,v) VALUES(?,?)',
                                    [('schema', str(SCHEMA_VERSION)), ('history_uuid', self.history_uuid)])
                self.db.commit()
            else:
                meta = dict(self.db.execute('SELECT k,v FROM meta'))
                if meta.get('schema') != str(SCHEMA_VERSION):
                    bump(self.counters, 'history_schema_unsupported')
                    return
                self.history_uuid = meta.get('history_uuid')
            self.history_available = True
            self.history_committed = True
            self.check_integrity()
        except (sqlite3.Error, OSError, ValueError):
            bump(self.counters, 'history_unavailable')
            self.history_available = False
            self.history_committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        self.history_available = False
        self.history_committed = False

    def check_integrity(self) -> tuple:
        counters = {}
        if not self.history_available or self.db is None:
            return False, dict(self.counters)
        try:
            meta = dict(self.db.execute('SELECT k,v FROM meta'))
            if meta.get('schema') != str(SCHEMA_VERSION):
                bump(counters, 'history_schema_unsupported')
            elif str(uuid.UUID(meta.get('history_uuid', ''))) != self.history_uuid:
                bump(counters, 'history_integrity_failed')
            elif self.db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                bump(counters, 'history_integrity_failed')
            else:
                for table in ('sources', 'response_copies', 'facts', 'submissions'):
                    for blob, digest in self.db.execute('SELECT payload,payload_sha256 FROM ' + table):
                        _unpack(blob, digest)
                for blob, digest in self.db.execute('SELECT contributors,contributors_sha256 FROM submissions'):
                    _unpack(blob, digest)
                if self.db.execute('SELECT 1 FROM response_copies r LEFT JOIN sources s '
                                    'ON r.source_id=s.source_id WHERE s.source_id IS NULL LIMIT 1').fetchone():
                    raise ValueError('orphan response evidence')
                if self.db.execute('SELECT 1 FROM facts f LEFT JOIN sources s '
                                    "ON f.source_id=s.source_id WHERE s.source_id IS NULL AND f.source_id != 'account' LIMIT 1").fetchone():
                    raise ValueError('orphan fact evidence')
                for key, blob, digest in self.db.execute(
                        "SELECT fact_key,payload,payload_sha256 FROM facts WHERE kind='account_snapshot'"):
                    snapshot = _unpack(blob, digest)
                    if set(snapshot) != set(SNAPSHOT_FIELDS) or key != _digest(snapshot):
                        raise ValueError('snapshot identity mismatch')
                for status, blob, digest in self.db.execute('SELECT status,payload,payload_sha256 FROM submissions'):
                    if status not in ACTIVE_STATUSES + ('retired',):
                        raise ValueError('invalid receipt status')
                    base64.b64decode(_unpack(blob, digest)['payload_base64'], validate=True)
                if self.db.execute('SELECT 1 FROM sources WHERE fact_codec != ? LIMIT 1', (FACT_CODEC,)).fetchone():
                    bump(counters, 'history_schema_unsupported')
        except (sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError, zlib.error):
            bump(counters, 'history_integrity_failed')
        self.counters.update(counters)
        if counters:
            self.history_available = False
            self.history_committed = False
        return not counters, counters

    def _put_fact(self, kind, key, source_id, value):
        blob, digest = _pack(value)
        old = self.db.execute('SELECT payload_sha256 FROM facts WHERE kind=? AND fact_key=? AND source_id=?',
                              (kind, key, source_id)).fetchone()
        conflicting = old is not None and old[0] != digest
        if conflicting:
            # Preserve both versions. The original stable identity is in the value.
            key = key + ':' + digest
        inserted = self.db.execute('INSERT OR IGNORE INTO facts VALUES(?,?,?,?,?)', (kind, key, source_id, blob, digest)).rowcount
        if conflicting and inserted:
            bump(self.counters, 'history_conflicting_revisions')

    def capture(self, results, *, account=None, now=None) -> None:
        if not self.history_available or not self.check_integrity()[0]:
            return
        results = list(results.values()) if isinstance(results, dict) else list(results)
        instant = _now(now)
        self.history_committed = False
        present = {r['source_id'] for r in results}
        for source_id, in self.db.execute('SELECT source_id FROM sources').fetchall():
            if source_id not in present:
                self.db.execute('UPDATE sources SET live=0 WHERE source_id=?', (source_id,))
        for result in results:
            source_id = result['source_id']
            if not result['stable_read']:
                self._read_counters[source_id] = dict(result['counters'])
                for name, count in result['counters'].items():
                    bump(self.counters, name, count)
                continue
            retained_turns = [_unpack(blob, digest) for blob, digest in self.db.execute(
                "SELECT payload,payload_sha256 FROM facts WHERE kind='turns' AND source_id=?", (source_id,))]
            result = _normalize_turns(result, retained_turns)
            old = self.db.execute('SELECT payload,payload_sha256 FROM sources WHERE source_id=?', (source_id,)).fetchone()
            source = _safe_source(result, _unpack(*old) if old else None)
            blob, digest = _pack(source)
            self.db.execute(
                'INSERT INTO sources VALUES(?,?,?,?,?,?,1,?,?,?) ON CONFLICT(source_id) DO UPDATE SET '
                'session_id=excluded.session_id,thread_id=excluded.thread_id,kind=excluded.kind,'
                'last_seen=excluded.last_seen,live=1,fact_codec=excluded.fact_codec,'
                'payload=excluded.payload,payload_sha256=excluded.payload_sha256',
                (source_id, result['session_id'], result['thread_id'], result['kind'], instant, instant,
                 result['fact_codec'], blob, digest))
            for incoming in result['responses']:
                previous = self.db.execute('SELECT payload,payload_sha256 FROM response_copies '
                                            'WHERE response_key=? AND source_id=?',
                                            (incoming['response_key'], source_id)).fetchone()
                prior = _unpack(*previous) if previous else None
                merged, counters = merge_response_copy(prior, _safe_copy(incoming))
                for name, count in counters.items():
                    if name != 'history_conflicting_revisions' or not (prior and prior.get('conflicting')):
                        bump(self.counters, name, count)
                blob, digest = _pack(_safe_copy(merged))
                self.db.execute(
                    'INSERT INTO response_copies VALUES(?,?,?,?,?,?,?) '
                    'ON CONFLICT(response_key,source_id) DO UPDATE SET last_seen=excluded.last_seen,'
                    'payload=excluded.payload,payload_sha256=excluded.payload_sha256,conflicting=excluded.conflicting',
                    (incoming['response_key'], source_id, instant, instant, blob, digest,
                     int(merged.get('conflicting', False))))
            for kind in FACT_KINDS:
                for item in result[kind]:
                    safe = _safe_fact(kind, item)
                    self._put_fact(kind, _fact_key(kind, safe), source_id, safe)
        for snapshot in _account_snapshots(account):
            self._put_fact('account_snapshot', _digest(snapshot), 'account', snapshot)
        # Capture canonical calendar context once, after union and global ownership.
        from .ledger import build
        canonical = build(results, history=self.load())
        calendars = {f['value']['response_key'] for f in self.load()[2]['calendar']}
        for row in canonical['rows']:
            if row['response_key'] not in calendars:
                self._put_fact('calendar', row['response_key'], row['source_id'],
                               dict(response_key=row['response_key'], **_row_snapshot(row)))
                calendars.add(row['response_key'])
        # The canonical ledger over live and retained evidence, so a caller that goes on
        # to report or share does not build it a second time.
        return canonical

    def load(self) -> tuple:
        facts = {kind: [] for kind in FACT_KINDS + ('calendar', 'submissions')}
        state = {'history_available': self.history_available, 'history_committed': self.history_committed,
                 'history_uuid': self.history_uuid, 'counters': dict(self.counters),
                 'read_counters': copy.deepcopy(self._read_counters), 'fact_digests': []}
        facts['state'] = state
        if not self.history_available or not self.check_integrity()[0]:
            state.update(history_available=False, history_committed=False, counters=dict(self.counters))
            return [], [], facts, []
        sources, copies, snapshots = [], [], []
        for row in self.db.execute('SELECT source_id,session_id,thread_id,kind,first_seen,last_seen,live,'
                                   'fact_codec,payload,payload_sha256 FROM sources ORDER BY source_id'):
            sid, session, thread, kind, first, last, live, codec, blob, digest = row
            sources.append(dict(_unpack(blob, digest), source_id=sid, session_id=session, thread_id=thread,
                                kind=kind, first_seen=first, last_seen=last, live=bool(live),
                                fact_codec=codec, path=''))
        by_source = {s['source_id']: s for s in sources}
        for key, sid, blob, digest, conflicting in self.db.execute(
                'SELECT response_key,source_id,payload,payload_sha256,conflicting FROM response_copies ORDER BY source_id,response_key'):
            value = _restore_copy(_unpack(blob, digest), by_source[sid])
            value['conflicting'] = bool(conflicting)
            copies.append(value)
        for kind, key, sid, blob, digest in self.db.execute('SELECT * FROM facts ORDER BY kind,fact_key,source_id'):
            value = _unpack(blob, digest)
            state['fact_digests'].append({'kind': kind, 'fact_key': key, 'source_id': sid, 'sha256': digest})
            if kind == 'account_snapshot':
                snapshots.append(value)
            elif kind in facts and kind != 'submissions':
                facts[kind].append({'source_id': sid, 'fact_key': key,
                                    'value': _restore_fact(kind, value, sources)})
        for row in self.db.execute('SELECT submission_id,endpoint,token_binding,status,created_at,confirmed_at,'
                                   'payload,payload_sha256,contributors,contributors_sha256 FROM submissions ORDER BY created_at,submission_id'):
            ident, endpoint, binding, status, created, confirmed, blob, digest, evidence, evidence_digest = row
            payload = base64.b64decode(_unpack(blob, digest)['payload_base64'], validate=True)
            facts['submissions'].append({'submission_id': ident, 'endpoint': endpoint, 'token_binding': binding,
                                         'status': status, 'created_at': created, 'confirmed_at': confirmed,
                                         'payload': payload, 'contributors': _unpack(evidence, evidence_digest)})
        return sources, copies, facts, sorted(snapshots, key=lambda s: (s['observed_at'], _digest(s)))

    def coverage(self, ledger, *, endpoint, token_binding=None) -> CoverageResult:
        integrity, _ = self.check_integrity()
        loaded = self.load()
        receipts = [r for r in loaded[2]['submissions'] if r['endpoint'] == endpoint
                    and r['token_binding'] == token_binding and r['status'] in ACTIVE_STATUSES]
        bound = token_binding is None or bool(receipts)
        if not bound:
            self.counters['history_token_binding_mismatch'] = 1
        view = copy.deepcopy(ledger)
        view['coverage'].update(history_available=integrity and ledger['coverage']['history_available'],
                                history_committed=self.history_committed and ledger['coverage']['history_committed'],
                                token_bound=bound)
        for name, count in self.counters.items():
            if name.startswith('history_') and name != 'history_conflicting_revisions':
                view['counters'][name] = count
                ledger['counters'][name] = count
        result = month_coverage(view, receipts)
        trusted = all(result[k] for k in ('history_available', 'history_committed', 'token_bound'))
        result['windows_replace_safe'] = trusted and _window_evidence_safe(view, receipts)
        result['window_reasons'] = ([] if result['windows_replace_safe'] else
                                   [TOKEN_REASON if not bound else WINDOW_REASON if receipts else UNAVAILABLE_REASON])
        failures = _month_failures(view, receipts)
        ledger['counters']['months_withheld'] = len(result['withheld_months'])
        for name in ('months_withheld_missing_contributors', 'months_withheld_decreased_contributions',
                     'months_withheld_calendar_change'):
            ledger['counters'][name] = sum(name in reasons for reasons in failures.values())
        ledger['counters']['windows_withheld_retention'] = int(bool(receipts) and not result['windows_replace_safe'])
        ledger['coverage'] = copy.deepcopy(result)
        return result

    def prepare(self, payload, contributors, *, endpoint, token_binding) -> str:
        if not self.history_available or not self.history_committed or not self.check_integrity()[0]:
            raise ValueError('history is not committed and intact')
        if not isinstance(endpoint, str) or not endpoint or (token_binding is not None and not isinstance(token_binding, str)):
            raise ValueError('invalid receipt binding')
        raw = payload if isinstance(payload, bytes) else _json_bytes(payload)
        # Payload bytes are already validated wire data by the sharing layer. Encoding
        # them inside canonical JSON preserves even insignificant whitespace exactly.
        blob, digest = _pack({'payload_base64': base64.b64encode(raw).decode('ascii')})
        evidence, evidence_digest = _pack(contributors)
        ident = str(uuid.uuid4())
        self.history_committed = False
        self.db.execute('INSERT INTO submissions VALUES(?,?,?,?,?,NULL,?,?,?,?)',
                        (ident, endpoint, token_binding, 'prepared', _now(None), blob, digest, evidence, evidence_digest))
        bump(self.counters, 'share_prepared')
        self.commit()
        if not self.history_committed:
            raise ValueError('prepared receipt was not committed')
        return ident

    def _status(self, submission_id, status, confirmed_at=None):
        if not self.history_available:
            raise ValueError('history unavailable')
        self.history_committed = False
        changed = self.db.execute('UPDATE submissions SET status=?, confirmed_at=COALESCE(?, confirmed_at) '
                                  "WHERE submission_id=? AND status != ? AND status != 'retired'",
                                  (status, confirmed_at, submission_id, status)).rowcount
        if changed:
            bump(self.counters, 'share_confirmed' if status == 'confirmed' else 'share_outcome_unknown')

    def confirm(self, submission_id, *, handle, now=None) -> None:
        # The handle is the server's public name for the share; it is never stored here.
        self._status(submission_id, 'confirmed', _now(now))

    def mark_unknown(self, submission_id, *, now=None) -> None:
        self._status(submission_id, 'unknown')

    def retire_token(self, token_binding, *, now=None) -> None:
        if not self.history_available:
            raise ValueError('history unavailable')
        self.history_committed = False
        self.db.execute("UPDATE submissions SET status='retired' WHERE token_binding IS ?", (token_binding,))

    def commit(self) -> None:
        self.history_committed = False
        if not self.history_available or self.db is None:
            return
        try:
            self.db.commit()
        except sqlite3.Error:
            bump(self.counters, 'history_commit_failed')
            return
        self.history_committed = True
        self.counters.pop('history_commit_failed', None)
