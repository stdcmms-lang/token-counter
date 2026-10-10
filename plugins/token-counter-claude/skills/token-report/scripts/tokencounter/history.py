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
import functools
import gc
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid
import zlib

from .models import COUNTER_NAMES, FACT_CODEC, CoverageResult, LedgerResult, bump
from .worker import _digest, _model_name as _raw_model_name, epoch

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
# Row granularity is independent of schema/codec 1. Old single-fact rows remain readable.
FACT_LIST_KEY = 'source-list'
USAGE_FIELDS = ('base_input', 'creation', 'reads', 'output', 'thinking', 'write_5m',
                'write_1h', 'searches', 'fetches', 'speed', 'service_tier', 'inference_geo',
                'iterations_count', 'iterations_agree', 'valid_ttl', 'invalid_optional_fields')
METADATA_FIELDS = ('raw_model', 'requested_model', 'advisor_model', 'effort', 'per_turn_effort',
                   'aborted', 'truncated', 'stop_reason')
COPY_FIELDS = ('response_key', 'source_id', 'effort', 'per_turn_effort', 'aborted', 'truncated',
               'stop_reason', 'terminal_record_key', 'partial', 'timestamp_quality', 'raw_model',
               'requested_model', 'advisor_model', 'quality_flags', '_history_codec',
               'history_merged', 'conflicting')
BLOCK_FIELDS = ('record_key', 'physical_line', 'api_block_index', 'ts', 'req_ts', 'turn', 'uuid', 'parent_uuid')
COPY_DATA_FIELDS = tuple(k for k in COPY_FIELDS if k not in (
    'response_key', 'source_id', '_history_codec'))
COPY_BOOL_FIELDS = ('aborted', 'truncated', 'partial', 'history_merged', 'conflicting')
COPY_VALUE_FIELDS = tuple(k for k in COPY_DATA_FIELDS if k not in COPY_BOOL_FIELDS)
COPY_LOCAL_FIELDS = frozenset(('session_id', 'agent_id', '_history_sha256'))
COPY_STORED_FIELDS = frozenset(COPY_FIELDS) | {'blocks', 'revisions', 'metadata_revisions'}
# The one positional response layout ever written. A plain dictionary blob is the
# round-4 layout and is read as it is.
COPY_COLUMNS = 5
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
    blob = zlib.compress(raw, 6)
    if len(raw) < 4096:
        # Short independent streams sometimes compress better with fewer lazy
        # matches. Both encode the identical canonical bytes and checked digest.
        compact = zlib.compress(raw, 4)
        if len(compact) < len(blob):
            blob = compact
    return blob, hashlib.sha256(raw).hexdigest()


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
    # The allow-lists select scalars. Callers rebuild the few nested measurements
    # explicitly, rather than recursively copying every scalar in every block.
    return {name: value.get(name) for name in names}


@functools.lru_cache(maxsize=1 << 18)
def _identifier(value, kind):
    return _digest([kind, value]) if value is not None else None


_cached_model_name = functools.lru_cache(maxsize=1024)(_raw_model_name)


def _model_name(value):
    return _cached_model_name(value) if isinstance(value, str) else _raw_model_name(value)


def _safe_usage(usage):
    if usage is None:
        return None
    out = _fields(usage, USAGE_FIELDS)
    if out['invalid_optional_fields'] is not None:
        out['invalid_optional_fields'] = list(out['invalid_optional_fields'])
    return out


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


def _tool_matches(fact, previous, *, stored=False):
    """Compare the scalar tool allow-list without allocating a sanitized fact."""
    named, recorded = 'name' in fact, 'record_key' in fact
    if (len(previous) != 4 + int(named) + 2 * int(recorded) or
            named != ('name' in previous) or recorded != ('record_key' in previous)):
        return False
    for field in ('tool_key', 'stream_id', 'ts', 'call_id'):
        if field not in previous:
            return False
        value = fact.get(field)
        if field == 'call_id' and not stored:
            value = _identifier(value, 'call')
        if type(value) is not type(previous[field]) or value != previous[field]:
            return False
    if named and previous['name'] is not None:
        return False
    if recorded:
        for field in ('record_key', 'parent_uuid'):
            if field not in previous:
                return False
            value = fact.get(field)
            if field == 'parent_uuid' and not stored:
                value = _identifier(value, 'uuid')
            if type(value) is not type(previous[field]) or value != previous[field]:
                return False
    return True


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
    return dict(value, session_id=source['session_id'],
                agent_id=source['thread_id'] if source['kind'] == 'subagent' else None)


@functools.lru_cache(maxsize=1 << 18)
def _wire_hash(value):
    if isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value):
        return '~' + base64.b64encode(bytes.fromhex(value)).decode('ascii')
    return value


@functools.lru_cache(maxsize=1 << 19)
def _read_hash(value):
    if isinstance(value, str) and len(value) == 45 and value.startswith('~'):
        raw = base64.b64decode(value[1:], validate=True)
        if len(raw) != 32:
            raise ValueError('invalid stored identifier')
        return raw.hex()
    return value


def _copy_payload(value, calls=None):
    """Lossless positional JSON for the same allow-listed fields, still schema 1.

    Separate response blobs cannot share zlib's dictionary. Fixed columns avoid
    repeating that dictionary in every response; old dictionary blobs still decode.
    """
    if value['_history_codec'] != FACT_CODEC:
        raise ValueError('uncaptured response encoding')

    def tool(f):
        retained = calls is not None and f['call_id'] is not None and calls.get((value['source_id'], f['tool_key'])) == (
            f['call_id'], f['stream_id'])
        return [_wire_hash(f['tool_key']), [] if retained else _wire_hash(f['call_id']), f['ts'],
                [] if retained else _wire_hash(f['stream_id']),
                'name' in f, [_wire_hash(f['record_key']), _wire_hash(f['parent_uuid'])] if 'record_key' in f else None]

    def block(b):
        metadata = b['response_metadata']
        delta = (None if metadata is None else [[i, metadata.get(k)] for i, k in enumerate(METADATA_FIELDS)
                                               if not _same_data(metadata.get(k), value.get(k))])
        return [[_wire_hash(b.get(k)) if k in ('record_key', 'uuid', 'parent_uuid') else b.get(k) for k in BLOCK_FIELDS],
                None if b['usage'] is None else [b['usage'].get(k) for k in USAGE_FIELDS],
                b['content'], [tool(f) for f in b['tool_starts']],
                delta]
    if any(type(value[k]) is not bool for k in COPY_BOOL_FIELDS):
        raise ValueError('invalid captured response flags')
    flags = sum(1 << i for i, k in enumerate(COPY_BOOL_FIELDS) if value[k])
    return [COPY_COLUMNS,
            [flags] + [_wire_hash(value.get(k)) if k == 'terminal_record_key' else value.get(k) for k in COPY_VALUE_FIELDS],
            [block(b) for b in value['blocks']], [block(b) for b in value['revisions']],
            [[r.get(k) for k in METADATA_FIELDS] for r in value['metadata_revisions']]]


def _expand_copy(value, response_key=None, source_id=None, calls=None):
    """The inverse of _copy_payload; a round-4 dictionary blob is returned as it is."""
    if not isinstance(value, list):
        return value
    if len(value) != 5 or value[0] != COPY_COLUMNS:
        raise ValueError('unsupported response columns')
    _layout, header, blocks, revisions, metadata_revisions = value

    def fields(names, values):
        if not isinstance(values, list) or len(names) != len(values):
            raise ValueError('invalid response columns')
        return dict(zip(names, values))

    def tool(f):
        if len(f) != 6:
            raise ValueError('invalid tool columns')
        key = _read_hash(f[0])
        if f[1] == [] and f[3] == []:
            call, stream = calls[(source_id, key)]
        else:
            call, stream = _read_hash(f[1]), _read_hash(f[3])
        out = dict(tool_key=key, call_id=call, ts=f[2], stream_id=stream)
        if f[4]:
            out['name'] = None
        if f[5] is not None:
            if len(f[5]) != 2:
                raise ValueError('invalid tool columns')
            out.update(record_key=_read_hash(f[5][0]), parent_uuid=_read_hash(f[5][1]))
        return out

    def block(b):
        if len(b) != 5:
            raise ValueError('invalid block columns')
        scalars = fields(BLOCK_FIELDS, b[0])
        for name in ('record_key', 'uuid', 'parent_uuid'):
            scalars[name] = _read_hash(scalars[name])
        metadata = None
        if b[4] is not None:
            metadata = dict(envelope)
            for index, item in b[4]:
                if type(index) is not int or not 0 <= index < len(METADATA_FIELDS):
                    raise ValueError('invalid envelope columns')
                metadata[METADATA_FIELDS[index]] = item
        return dict(scalars, usage=None if b[1] is None else fields(USAGE_FIELDS, b[1]),
                    content=b[2], tool_starts=[tool(f) for f in b[3]],
                    response_metadata=metadata)

    if not isinstance(header, list) or len(header) != len(COPY_VALUE_FIELDS) + 1:
        raise ValueError('invalid response columns')
    flags = header[0]
    if type(flags) is not int or not 0 <= flags < (1 << len(COPY_BOOL_FIELDS)):
        raise ValueError('invalid response flags')
    scalars = fields(COPY_VALUE_FIELDS, header[1:])
    scalars.update({k: bool(flags & (1 << i)) for i, k in enumerate(COPY_BOOL_FIELDS)})
    scalars.update(response_key=response_key, source_id=source_id, _history_codec=FACT_CODEC)
    scalars['terminal_record_key'] = _read_hash(scalars['terminal_record_key'])
    envelope = {k: scalars.get(k) for k in METADATA_FIELDS}
    return dict(scalars, blocks=[block(b) for b in blocks],
                revisions=[block(b) for b in revisions],
                metadata_revisions=[fields(METADATA_FIELDS, r) for r in metadata_revisions])


def _same_copy(safe, previous):
    keys = safe.keys() - COPY_LOCAL_FIELDS
    return (previous.keys() - COPY_LOCAL_FIELDS == keys and
            all(_same_data(previous[k], safe[k]) for k in keys))


def _same_data(a, b):
    """JSON identity includes scalar types (1 and 1.0 have different digests)."""
    if a is b:
        return True
    kind = type(a)
    if kind is not type(b) or a != b:
        return False
    if kind is dict:
        return all(_same_data(value, b[key]) for key, value in a.items())
    if kind is list:
        return all(_same_data(x, y) for x, y in zip(a, b))
    return True


def _copy_matches(value, previous):
    """Compare the safe projection without allocating another tree or digesting it."""
    if previous.keys() - COPY_LOCAL_FIELDS != COPY_STORED_FIELDS or not previous['history_merged']:
        return False
    if value.get('revisions') or previous['revisions'] or value.get('metadata_revisions') or previous['metadata_revisions']:
        return False
    for field in COPY_FIELDS:
        if field in ('raw_model', 'requested_model', 'advisor_model'):
            candidate = _model_name(value.get(field))[1]
        elif field == 'quality_flags':
            candidate = sorted(set(value[field]) & COUNTER_NAMES) if value.get(field) else []
        elif field in ('_history_codec', 'history_merged'):
            candidate = FACT_CODEC if field == '_history_codec' else True
        else:
            candidate = value.get(field, False if field == 'conflicting' else None)
        if field == 'quality_flags':
            if not _same_data(candidate, previous[field]):
                return False
        elif type(candidate) is not type(previous[field]) or candidate != previous[field]:
            return False
    incoming = value.get('blocks', ())
    if len(incoming) != len(previous['blocks']):
        return False
    stored = value.get('_history_codec') == FACT_CODEC
    for block, old in zip(incoming, previous['blocks']):
        for field in BLOCK_FIELDS:
            candidate = block.get(field)
            if field in ('uuid', 'parent_uuid') and not stored:
                candidate = _identifier(candidate, 'uuid')
            prior_value = old.get(field)
            if type(candidate) is not type(prior_value) or candidate != prior_value:
                return False
        usage, prior = block.get('usage'), old['usage']
        if (usage is None) != (prior is None):
            return False
        if usage is not None:
            for k in USAGE_FIELDS:
                a, b = usage.get(k), prior.get(k)
                if k == 'invalid_optional_fields':
                    if not _same_data(a, b):
                        return False
                elif type(a) is not type(b) or a != b:
                    return False
        metadata, prior = block.get('response_metadata'), old['response_metadata']
        if (metadata is None) != (prior is None):
            return False
        if metadata is not None and any(type(metadata.get(k)) is not type(prior.get(k)) or
                                        metadata.get(k) != prior.get(k) for k in METADATA_FIELDS):
            return False
        if (block.get('content') or old['content']) and not _same_data(
                [_safe_content(f) for f in block.get('content', ())], old['content']):
            return False
        tools, prior_tools = block.get('tool_starts', ()), old['tool_starts']
        if len(tools) != len(prior_tools) or any(
                not _tool_matches(f, prior, stored=stored) for f, prior in zip(tools, prior_tools)):
            return False
    return True


def _incoming_copy(value, previous=None):
    """``(safe copy, stored digest or None)``: the digest only when nothing changed."""
    if previous is not None and _copy_matches(value, previous):
        return previous, previous['_history_sha256']
    safe = _safe_copy(value)
    # This flag describes retained ordering, rather than recorded evidence. Give the
    # fast-path candidate the same flag as a captured copy; the merge still collapses
    # first captures and preserves every revision when any other field differs.
    if previous is not None and _same_copy(dict(safe, history_merged=True), previous):
        return safe, previous['_history_sha256']
    return safe, None


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
        return dict(previous) if previous is not None else None, counters
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
            # A stable extraction may revise derived inventory rules. Content is not
            # captured revision evidence, and replacing it must not quarantine usage.
            old['content'] = copy.deepcopy(block.get('content', []))
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
        return _tool_fact_key(item['tool_key'], item.get('record_key'))
    field = {'content': 'item_key', 'limits': 'reading_key', 'events': 'event_key',
             'calendar': 'response_key'}.get(kind, 'record_key')
    return item[field]


@functools.lru_cache(maxsize=1 << 18)
def _tool_fact_key(tool_key, record_key):
    return _digest([tool_key, record_key])


def _incoming_facts(kind, items, retained):
    variants = {}
    for entry in retained:
        variants.setdefault(entry['fact_key'].split(':', 1)[0], []).append(entry)
    for item in items:
        key = _fact_key(kind, item)
        if kind in ('tool_starts', 'tool_results'):
            same = next((entry for entry in variants.get(key, ())
                         if _tool_matches(item, entry['value'])), None)
            if same is not None:
                yield key, same['value'], same['sha256']
                continue
        safe = _safe_fact(kind, item)
        same = next((entry for entry in variants.get(key, ())
                     if _same_data(entry.get('_safe_value', entry['value']), safe)), None)
        # A matched value is exactly the checked retained fact: carry its stored
        # blob digest, including when that blob is an immutable list of facts.
        yield key, safe, same['sha256'] if same is not None else _digest(safe)


def _tool_calls(facts):
    calls = {}
    for entry in facts:
        _add_tool_call(calls, entry['source_id'], entry['value'])
    return calls


def _add_tool_call(calls, source_id, tool):
    key = (source_id, tool['tool_key'])
    call = (tool['call_id'], tool['stream_id'])
    if key in calls and calls[key] != call:
        raise ValueError('ambiguous retained tool identity')
    calls[key] = call


def _fact_payload(kind, value, source_id, calls):
    # tool_key already binds the response identity and original call ID. The
    # hashed call ID/stream are retained in immutable tool-start facts; timestamps
    # and every other fact field stay in this blob. Unmatched facts stay complete.
    if kind == 'tool_results' and calls.get((source_id, value['tool_key'])) == (
            value['call_id'], value['stream_id']):
        out = {k: v for k, v in value.items() if k not in ('tool_key', 'call_id', 'stream_id')}
        out['tool_ref'] = _wire_hash(value['tool_key'])
        return out
    return value


def _expand_fact(kind, value, source_id, calls):
    if kind == 'tool_results' and 'tool_ref' in value:
        out = dict(value)
        key = _read_hash(out.pop('tool_ref'))
        call, stream = calls[(source_id, key)]
        out.update(tool_key=key, call_id=call, stream_id=stream)
        return out
    return value


def _stored_facts(key, value):
    if key == FACT_LIST_KEY or key.startswith(FACT_LIST_KEY + ':'):
        for item in value:
            if not isinstance(item, dict):
                raise ValueError('invalid fact list')
            # A conflicting revision is wrapped with its suffixed key. Every safe fact
            # has three or more fields, so the exact two-key shape is unambiguous.
            yield (item['fact_key'], item['value']) if item.keys() == {'fact_key', 'value'} else (None, item)
    else:
        yield key, value


def _normalize_turns(result, retained):
    """Keep source-local references stable when re-extraction renumbers openers."""
    openers = {f['record_key']: f['turn'] for f in retained if f.get('record_key') is not None}
    next_turn = max(openers.values(), default=-1) + 1
    mapping = {}
    for fact in result['turns']:
        key = fact.get('record_key')
        if key is not None:
            if key not in openers:
                openers[key] = next_turn
                next_turn += 1
            mapping[fact['turn']] = openers[key]
    if all(a == b for a, b in mapping.items()):
        return result
    out = dict(result)
    out['turns'] = [dict(f, turn=mapping.get(f['turn'], f['turn'])) for f in result['turns']]
    out['responses'] = [dict(r, blocks=[dict(b, turn=mapping.get(b['turn'], b['turn']))
                                      for b in r['blocks']]) for r in result['responses']]
    return out


def _restore_fact(kind, item, sources):
    if kind != 'links':
        # Each load decodes fresh JSON; these facts already own their plain values.
        return item
    out = dict(item)
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
    # The ledger hands over a newly constructed row, before any stream index or
    # family view refers to it. Only captured scalar calendar fields are replaced.
    out, counters = row, {}
    if captured is not None:
        if _fields(row, CALENDAR_FIELDS) != captured:
            bump(counters, 'calendar_context_changed')
        out.update(captured)
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
                if snapshot.get('counts'):
                    current = _row_snapshot(row)['counts']
                    if any(current[field] < count for field, count in snapshot['counts'].items()):
                        reasons.add('months_withheld_decreased_contributions')
                calendar = _fields(row, CALENDAR_FIELDS)
                if calendar != snapshot.get('calendar', calendar):
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
        self._verified_blobs = set()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self.path), timeout=timeout)
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                # A new capture store, not a migration/reset of an existing store.
                self.db.execute('PRAGMA page_size=16384')
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
        self._verified_blobs.clear()

    def _decode(self, blob, digest):
        # Verification is tied to the exact compressed bytes as well as the digest.
        # A changed SQL blob/checksum cannot reuse an earlier integrity decision.
        if (blob, digest) not in self._verified_blobs:
            value = _unpack(blob, digest)
            self._verified_blobs.add((blob, digest))
            return value
        return json.loads(zlib.decompress(blob).decode('utf-8'))

    def _encode(self, value):
        blob, digest = _pack(value)
        # _pack constructs these exact bytes from checked canonical JSON. Subsequent
        # reads can reuse that decision, just as they reuse the full verification.
        self._verified_blobs.add((blob, digest))
        return blob, digest

    def check_integrity(self) -> tuple:
        counters = {}
        if not self.history_available or self.db is None:
            return False, dict(self.counters)
        self._verified_blobs.clear()
        try:
            meta = dict(self.db.execute('SELECT k,v FROM meta'))
            if meta.get('schema') != str(SCHEMA_VERSION):
                bump(counters, 'history_schema_unsupported')
            elif str(uuid.UUID(meta.get('history_uuid', ''))) != self.history_uuid:
                bump(counters, 'history_integrity_failed')
            elif self.db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                bump(counters, 'history_integrity_failed')
            else:
                self._load_reset_clusters()
                calls, results = {}, []
                for table in ('sources', 'facts', 'response_copies', 'submissions'):
                    columns = ('kind,fact_key,source_id,' if table == 'facts' else
                               'response_key,source_id,' if table == 'response_copies' else
                               'status,' if table == 'submissions' else '')
                    for row in self.db.execute('SELECT ' + columns + 'payload,payload_sha256 FROM ' + table):
                        blob, digest = row[-2:]
                        value = _unpack(blob, digest)
                        self._verified_blobs.add((blob, digest))
                        if table == 'facts':
                            kind, key, sid = row[:3]
                            if kind == 'account_snapshot':
                                if set(value) != set(SNAPSHOT_FIELDS) or key != _digest(value):
                                    raise ValueError('snapshot identity mismatch')
                            else:
                                for _key, item in _stored_facts(key, value):
                                    if kind == 'tool_starts':
                                        _add_tool_call(calls, sid, item)
                                    elif kind == 'tool_results':
                                        results.append((sid, item))
                        elif table == 'submissions':
                            if row[0] not in ACTIVE_STATUSES + ('retired',):
                                raise ValueError('invalid receipt status')
                            base64.b64decode(value['payload_base64'], validate=True)
                        elif table == 'response_copies':
                            _expand_copy(value, row[0], row[1], calls)
                    if table == 'facts':
                        for sid, item in results:
                            _expand_fact('tool_results', item, sid, calls)
                for blob, digest in self.db.execute('SELECT contributors,contributors_sha256 FROM submissions'):
                    _unpack(blob, digest)
                    self._verified_blobs.add((blob, digest))
                if self.db.execute('SELECT 1 FROM response_copies r LEFT JOIN sources s '
                                    'ON r.source_id=s.source_id WHERE s.source_id IS NULL LIMIT 1').fetchone():
                    raise ValueError('orphan response evidence')
                if self.db.execute('SELECT 1 FROM facts f LEFT JOIN sources s '
                                    "ON f.source_id=s.source_id WHERE s.source_id IS NULL AND f.source_id != 'account' LIMIT 1").fetchone():
                    raise ValueError('orphan fact evidence')
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
        blob, digest = self._encode(value)
        old = self.db.execute('SELECT payload_sha256 FROM facts WHERE kind=? AND fact_key=? AND source_id=?',
                              (kind, key, source_id)).fetchone()
        conflicting = old is not None and old[0] != digest
        if conflicting:
            # Preserve both versions. The original stable identity is in the value.
            key = key + ':' + digest
        inserted = self.db.execute('INSERT OR IGNORE INTO facts VALUES(?,?,?,?,?)', (kind, key, source_id, blob, digest)).rowcount
        if conflicting and inserted and kind != 'content':
            bump(self.counters, 'history_conflicting_revisions')

    def _append_facts(self, kind, source_id, values, loaded, retained, calls):
        """Append one immutable list of newly seen safe facts for this source/kind.

        Legacy schema-1 single-fact rows stay in place. Only new facts enter the list,
        so existing receipt fact keys/digests and every incompatible version survive.
        """
        facts, state = loaded[2], loaded[2]['state']
        variants = {}
        for entry in retained:
            variants.setdefault(entry['fact_key'].split(':', 1)[0], []).append(entry)
        additions = []
        for key, safe, digest in values:
            previous = variants.get(key, ())
            if any(_same_data(entry.get('_safe_value', entry['value']), safe) for entry in previous):
                continue
            if previous:
                key += ':' + digest
                if kind != 'content':
                    bump(self.counters, 'history_conflicting_revisions')
            entry = {'source_id': source_id, 'fact_key': key, 'value': safe}
            if kind == 'links':
                entry['_safe_value'] = safe
            additions.append(entry)
            variants.setdefault(key.split(':', 1)[0], []).append(entry)
        if additions:
            payload = []
            for entry in additions:
                safe = entry.get('_safe_value', entry['value'])
                encoded = _fact_payload(kind, safe, source_id, calls)
                payload.append(encoded if entry['fact_key'] == _fact_key(kind, safe) else
                               {'fact_key': entry['fact_key'], 'value': encoded})
            blob, digest = self._encode(payload)
            groups = {entry['row_key'] for entry in retained
                      if entry['row_key'] == FACT_LIST_KEY or entry['row_key'].startswith(FACT_LIST_KEY + ':')}
            row_key = FACT_LIST_KEY if not groups else '%s:%08d:%s' % (FACT_LIST_KEY, len(groups), digest)
            self.db.execute('INSERT OR IGNORE INTO facts VALUES(?,?,?,?,?)', (kind, row_key, source_id, blob, digest))
            for entry in additions:
                entry.update(sha256=digest, row_key=row_key)
            facts[kind].extend(additions)
            retained.extend(additions)
            state['fact_digests'].append({'kind': kind, 'fact_key': row_key,
                                          'source_id': source_id, 'sha256': digest})

    def capture(self, results, *, account=None, now=None) -> LedgerResult:
        if not self.history_available:
            return
        results = list(results.values()) if isinstance(results, dict) else list(results)
        instant = _now(now)
        loaded = self.load()
        if not self.history_available:
            return
        sources, copies, facts, snapshots = loaded
        by_source = {s['source_id']: s for s in sources}
        by_copy = {(c['response_key'], c['source_id']): c for c in copies}
        calls = _tool_calls(facts['tool_starts'])
        fact_index = {}
        for kind in FACT_KINDS + ('calendar',):
            for entry in facts[kind]:
                fact_index.setdefault((kind, entry['source_id']), []).append(entry)
        turns = {}
        for fact in facts['turns']:
            turns.setdefault(fact['source_id'], []).append(fact['value'])
        self.history_committed = False
        present = {r['source_id'] for r in results}
        for source_id, source in by_source.items():
            if source_id not in present and source['live']:
                self.db.execute('UPDATE sources SET live=0 WHERE source_id=?', (source_id,))
                source['live'] = False
        for result in results:
            source_id = result['source_id']
            if not result['stable_read']:
                self._read_counters[source_id] = dict(result['counters'])
                for name, count in result['counters'].items():
                    bump(self.counters, name, count)
                continue
            result = _normalize_turns(result, turns.get(source_id, ()))
            for tool in result['tool_starts']:
                _add_tool_call(calls, source_id, _safe_fact('tool_starts', tool))
            old = by_source.get(source_id)
            source = _safe_source(result, old)
            digest = _digest(source)
            if old is not None and digest == old['_history_sha256'] and all(
                    result[k] == old[k] for k in ('session_id', 'thread_id', 'kind', 'fact_codec')):
                self.db.execute('UPDATE sources SET last_seen=?,live=1 WHERE source_id=?', (instant, source_id))
            else:
                blob, digest = self._encode(source)
                self.db.execute(
                    'INSERT INTO sources VALUES(?,?,?,?,?,?,1,?,?,?) ON CONFLICT(source_id) DO UPDATE SET '
                    'session_id=excluded.session_id,thread_id=excluded.thread_id,kind=excluded.kind,'
                    'last_seen=excluded.last_seen,live=1,fact_codec=excluded.fact_codec,'
                    'payload=excluded.payload,payload_sha256=excluded.payload_sha256',
                    (source_id, result['session_id'], result['thread_id'], result['kind'], instant, instant,
                     result['fact_codec'], blob, digest))
            source.update(source_id=source_id, session_id=result['session_id'], thread_id=result['thread_id'],
                          kind=result['kind'], fact_codec=result['fact_codec'], path='', live=True,
                          first_seen=old['first_seen'] if old else instant, last_seen=instant,
                          _history_sha256=digest)
            by_source[source_id] = source
            for incoming in result['responses']:
                key = (incoming['response_key'], source_id)
                prior = by_copy.get(key)
                safe, digest = _incoming_copy(incoming, prior) if prior is not None else (_safe_copy(incoming), None)
                if prior is not None and digest == prior['_history_sha256']:
                    continue
                merged, counters = merge_response_copy(prior, safe)
                for name, count in counters.items():
                    if name != 'history_conflicting_revisions' or not (prior and prior.get('conflicting')):
                        bump(self.counters, name, count)
                safe = _safe_copy(merged)
                if prior is not None and _same_copy(safe, prior):
                    continue
                blob, digest = self._encode(_copy_payload(safe, calls))
                self.db.execute(
                    'INSERT INTO response_copies VALUES(?,?,?,?,?,?,?) '
                    'ON CONFLICT(response_key,source_id) DO UPDATE SET last_seen=excluded.last_seen,'
                    'payload=excluded.payload,payload_sha256=excluded.payload_sha256,conflicting=excluded.conflicting',
                    (incoming['response_key'], source_id, instant, instant, blob, digest,
                     int(merged.get('conflicting', False))))
                by_copy[key] = dict(_restore_copy(safe, source), _history_sha256=digest)
            for kind in FACT_KINDS:
                retained = fact_index.setdefault((kind, source_id), [])
                self._append_facts(kind, source_id, _incoming_facts(kind, result[kind], retained), loaded, retained, calls)
        for snapshot in _account_snapshots(account):
            digest = _digest(snapshot)
            self._put_fact('account_snapshot', digest, 'account', snapshot)
            if snapshot not in snapshots:
                snapshots.append(snapshot)
                facts['state']['fact_digests'].append({'kind': 'account_snapshot', 'fact_key': digest,
                                                      'source_id': 'account', 'sha256': digest})
        sources[:] = sorted(by_source.values(), key=lambda s: s['source_id'])
        copies[:] = [by_copy[key] for key in sorted(by_copy, key=lambda k: (k[1], k[0]))]
        for entry in facts['links']:
            entry['value'] = _restore_fact('links', entry['_safe_value'], sources)
        facts['state'].update(history_committed=False, counters=dict(self.counters),
                              read_counters=copy.deepcopy(self._read_counters))
        # Capture canonical calendar context once, after union and global ownership.
        from .ledger import build
        # These exact live copies/facts have already been reconciled above. Avoid
        # sanitizing them a second time during this one build. Absent and unstable
        # copies still take the ordinary retention decisions in the ledger.
        facts['state']['_captured_live_copies'] = [(c['response_key'], r['source_id'])
            for r in results if r['stable_read'] for c in r['responses']]
        try:
            canonical = build(results, history=loaded)
        finally:
            facts['state'].pop('_captured_live_copies', None)
        # Captured reset anchors and the account's observed grid phase are durable
        # metadata. Re-clustering later facts may widen quotes, but cannot move them.
        from . import windows
        clusters, _ = windows.cluster_resets(canonical['limits'], established=facts['state'].get('reset_clusters', ()))
        anchors = windows._anchors(clusters)
        if anchors != facts['state'].get('reset_clusters', []):
            raw = _json_bytes(anchors).decode('utf-8')
            self.db.executemany('INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                                [('reset_clusters', raw), ('reset_clusters_sha256', _digest(anchors))])
        facts['state']['reset_clusters'] = anchors
        canonical['_reset_clusters'] = copy.deepcopy(anchors)
        calendars = {f['value']['response_key'] for f in facts['calendar']}
        additions = {}
        for row in canonical['rows']:
            if row['response_key'] not in calendars:
                safe = dict(response_key=row['response_key'], **_row_snapshot(row))
                additions.setdefault(row['source_id'], []).append((row['response_key'], safe, _digest(safe)))
                canonical['_retention']['captured'][row['response_key']] = safe
                calendars.add(row['response_key'])
        for source_id, values in additions.items():
            self._append_facts('calendar', source_id, values, loaded,
                               fact_index.setdefault(('calendar', source_id), []), calls)
        # The canonical ledger over live and retained evidence, so a caller that goes on
        # to report or share does not build it a second time.
        return canonical

    def load(self) -> tuple:
        facts = {kind: [] for kind in FACT_KINDS + ('calendar', 'submissions')}
        state = {'history_available': self.history_available, 'history_committed': self.history_committed,
                 'history_uuid': self.history_uuid, 'counters': dict(self.counters),
                 'read_counters': copy.deepcopy(self._read_counters), 'fact_digests': []}
        facts['state'] = state
        if not self.history_available:
            return [], [], facts, []
        collecting = gc.isenabled()
        if collecting:
            # Decoded JSON is acyclic. Avoid rescanning the resident live corpus
            # for cycles while allocating these independent plain-data snapshots.
            gc.disable()
        try:
            return self._load(facts, state)
        except (sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError, zlib.error):
            bump(self.counters, 'history_integrity_failed')
            self.history_available = self.history_committed = False
            for kind in facts:
                if kind != 'state':
                    facts[kind] = []
            state.update(history_available=False, history_committed=False, counters=dict(self.counters), fact_digests=[])
            return [], [], facts, []
        finally:
            if collecting:
                gc.enable()

    def _load(self, facts, state):
        state['reset_clusters'] = self._load_reset_clusters()
        sources, copies, snapshots = [], [], []
        for row in self.db.execute('SELECT source_id,session_id,thread_id,kind,first_seen,last_seen,live,'
                                   'fact_codec,payload,payload_sha256 FROM sources ORDER BY source_id'):
            sid, session, thread, kind, first, last, live, codec, blob, digest = row
            sources.append(dict(self._decode(blob, digest), source_id=sid, session_id=session, thread_id=thread,
                                kind=kind, first_seen=first, last_seen=last, live=bool(live),
                                fact_codec=codec, path='', _history_sha256=digest))
        by_source = {s['source_id']: s for s in sources}
        fact_rows, calls = [], {}
        for kind, key, sid, blob, digest in self.db.execute('SELECT * FROM facts ORDER BY kind,fact_key,source_id'):
            value = self._decode(blob, digest)
            fact_rows.append((kind, key, sid, value, digest))
            if kind == 'tool_starts':
                for _key, item in _stored_facts(key, value):
                    _add_tool_call(calls, sid, item)
        for key, sid, blob, digest, conflicting in self.db.execute(
                'SELECT response_key,source_id,payload,payload_sha256,conflicting FROM response_copies ORDER BY source_id,response_key'):
            payload = self._decode(blob, digest)
            value = _restore_copy(_expand_copy(payload, key, sid, calls), by_source[sid])
            if value['response_key'] != key or value['source_id'] != sid:
                raise ValueError('response identity mismatch')
            value['conflicting'] = bool(conflicting)
            value['_history_sha256'] = digest
            copies.append(value)
        for kind, key, sid, value, digest in fact_rows:
            state['fact_digests'].append({'kind': kind, 'fact_key': key, 'source_id': sid, 'sha256': digest})
            if kind == 'account_snapshot':
                snapshots.append(value)
            else:
                for item_key, item in _stored_facts(key, value):
                    safe = _expand_fact(kind, item, sid, calls)
                    if kind in facts and kind != 'submissions':
                        entry = dict(fact_key=_fact_key(kind, safe) if item_key is None else item_key,
                                     source_id=sid, row_key=key, sha256=digest,
                                     value=_restore_fact(kind, safe, sources))
                        if kind == 'links':
                            entry['_safe_value'] = safe
                        facts[kind].append(entry)
        facts['submissions'] = self._load_submissions()
        return sources, copies, facts, sorted(snapshots, key=lambda s: (s['observed_at'], _digest(s)))

    def _load_reset_clusters(self):
        from . import windows
        meta = dict(self.db.execute("SELECT k,v FROM meta WHERE k IN ('reset_clusters','reset_clusters_sha256')"))
        if not meta:
            return []  # Round-4/5 histories learn anchors on their next capture.
        anchors = json.loads(meta['reset_clusters'])
        if not isinstance(anchors, list) or _digest(anchors) != meta.get('reset_clusters_sha256'):
            raise ValueError('reset anchor metadata checksum mismatch')
        return windows._checked_anchors(anchors)

    def _load_submissions(self):
        receipts = []
        for row in self.db.execute('SELECT submission_id,endpoint,token_binding,status,created_at,confirmed_at,'
                                   'payload,payload_sha256,contributors,contributors_sha256 FROM submissions ORDER BY created_at,submission_id'):
            ident, endpoint, binding, status, created, confirmed, blob, digest, evidence, evidence_digest = row
            payload = base64.b64decode(self._decode(blob, digest)['payload_base64'], validate=True)
            receipts.append({'submission_id': ident, 'endpoint': endpoint, 'token_binding': binding,
                                         'status': status, 'created_at': created, 'confirmed_at': confirmed,
                                         'payload': payload, 'contributors': self._decode(evidence, evidence_digest)})
        return receipts

    def coverage(self, ledger, *, endpoint, token_binding=None) -> CoverageResult:
        try:
            receipts = self._load_submissions() if self.history_available else []
        except (sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError, zlib.error):
            bump(self.counters, 'history_integrity_failed')
            self.history_available = self.history_committed = False
            receipts = []
        receipts = [r for r in receipts if r['endpoint'] == endpoint
                    and r['token_binding'] == token_binding and r['status'] in ACTIVE_STATUSES]
        bound = token_binding is None or bool(receipts)
        if not bound:
            self.counters['history_token_binding_mismatch'] = 1
        view = dict(ledger, counters=dict(ledger['counters']), coverage=dict(ledger['coverage']))
        view['coverage'].update(history_available=self.history_available and ledger['coverage']['history_available'],
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
        if not self.history_available or not self.history_committed:
            raise ValueError('history is not committed and intact')
        if not isinstance(endpoint, str) or not endpoint or (token_binding is not None and not isinstance(token_binding, str)):
            raise ValueError('invalid receipt binding')
        raw = payload if isinstance(payload, bytes) else _json_bytes(payload)
        # Payload bytes are already validated wire data by the sharing layer. Encoding
        # them inside canonical JSON preserves even insignificant whitespace exactly.
        blob, digest = self._encode({'payload_base64': base64.b64encode(raw).decode('ascii')})
        evidence, evidence_digest = self._encode(contributors)
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
