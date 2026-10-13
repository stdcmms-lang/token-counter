#!/usr/bin/env python3
"""A content-free, deterministic film of one currently present main transcript.

Collection is Python 3.8 standard library only. The unchanged report worker and
ledger decide which usage exists; a bounded temporary snapshot supplies both
passes. No account, index, capture history or network operation is performed.
"""
import argparse
import base64
import collections
import contextlib
import copy
import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Dict, Iterator, List, Mapping, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPORT = HERE.parent.parent / 'token-report' / 'scripts'
sys.path.insert(0, str(REPORT))
import report as reportcli  # noqa: E402 -- platform opening and Python floor
from tokencounter import ledger, paths, pricing, rollout, worker  # noqa: E402
from tokencounter.models import COUNTER_NAMES, COUNTER_SCOPES, FileResult, ResponseCopy, ResponseRow, bump  # noqa: E402

VERSION = '0.2.0'
PLUGIN_ROOT = HERE.parents[2]
MAX_SAFE = 9007199254740991
CATEGORIES = ('read', 'write', 'thinking', 'writing', 'input')
LABELS = {'read': 'Re-reading', 'write': 'Caching', 'thinking': 'Thinking',
          'writing': 'Writing', 'input': 'Fresh input', 'search': 'Web search fees'}
TOOLS = frozenset(('Agent', 'AskUserQuestion', 'Bash', 'Edit', 'Glob', 'Grep', 'LS',
                   'MultiEdit', 'NotebookEdit', 'PowerShell', 'Read', 'Skill', 'Task',
                   'TodoWrite', 'ToolSearch', 'WebFetch', 'WebSearch', 'Write'))
FILM_COUNTERS = frozenset(('film_unknown_blocks', 'film_unknown_trigger',
                          'film_pre_fallback', 'film_post_fallback',
                          'film_compaction_clamped', 'film_response_crosses_compaction',
                          'film_time_regressions', 'film_snapshot_retries',
                          'film_subagent_pricing_unavailable'))
CHANGED = 'The transcript changed while it was being read. Retry after the session is idle.'
JOIN = 'Could not join the two transcript passes; no film was written.'
RANGE = "This session's counts exceed the film renderer's exact integer range."
EMPTY = 'No nonzero main-thread responses were found; a film needs at least one.'


class FilmError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    selectors = parser.add_mutually_exclusive_group()
    selectors.add_argument('--session', metavar='PREFIX')
    selectors.add_argument('--current', action='store_true')
    selectors.add_argument('--list', action='store_true')
    parser.add_argument('--sessions-root', metavar='PATH')
    parser.add_argument('--prices', metavar='PATH')
    parser.add_argument('--out', metavar='PATH')
    parser.add_argument('--json', metavar='PATH')
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--version', action='version', version='token-counter-claude ' + VERSION)
    options = parser.parse_args(argv)
    if not (options.session or options.current or options.list):
        raise FilmError(2, 'Choose a session: --session PREFIX, --current, or --list.')
    if options.list and any((options.out, options.json, options.prices)):
        raise FilmError(2, '--out, --json, and --prices are invalid with --list.')
    return options


def discover_main(root: Path) -> Tuple[List[Path], Dict[str, int]]:
    files, counters = rollout.discover(root)
    return [p for p in files if p.parent.name != 'subagents'], counters


def candidate_prefixes(files: List[Path]) -> Dict[Path, str]:
    out = {}
    for path in files:
        n = min(8, len(path.stem))
        while n < len(path.stem) and any(p != path and p.stem.startswith(path.stem[:n]) for p in files):
            n += 1
        out[path] = path.stem[:n]
    return out


def select_session(files: List[Path], prefix: str) -> Path:
    found = [p for p in files if p.stem.startswith(prefix)]
    if not found:
        raise FilmError(2, 'No main session matches that prefix. Use --list.')
    if len(found) != 1:
        raise FilmError(2, 'That session prefix is ambiguous.\n' + format_listing(list_sessions(found, len(found))))
    return found[0]


def current_prefix(env: Mapping[str, str]) -> str:
    value = env.get('CLAUDE_CODE_SESSION_ID')
    if not value:
        raise FilmError(2, 'CLAUDE_CODE_SESSION_ID is unset. Use --session PREFIX or --list.')
    return value


def hash_prefix(path: Path, byte_count: int) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with open(path, 'rb') as source:
        remaining = byte_count
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise FilmError(3, CHANGED)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def copy_prefix(path: Path, snapshot: Path, byte_count: int) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with open(path, 'rb') as source, open(snapshot, 'wb') as destination:
        remaining = byte_count
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise FilmError(3, CHANGED)
            destination.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def frozen_main(path: Path) -> Iterator[Tuple[Path, int, str]]:
    size = rollout.stat_key(path)[0]
    with tempfile.TemporaryDirectory(prefix='token-film-') as directory:
        snapshot = Path(directory) / path.name
        digest = copy_prefix(path, snapshot, size)
        yield snapshot, size, digest


def prefix_matches(path: Path, byte_count: int, digest: str) -> bool:
    try:
        return rollout.stat_key(path)[0] >= byte_count and hash_prefix(path, byte_count) == digest
    except (OSError, FilmError):
        return False


def collapsed_copies(result: FileResult) -> Dict[str, ResponseCopy]:
    return {c['response_key']: ledger.collapse_blocks(c)[0] for c in result['responses']}


def is_typed_prompt(record: dict) -> bool:
    if record.get('isMeta') is True or record.get('isCompactSummary') is True:
        return False
    if worker._prompt_side(record) != 'turn':
        return False
    content = record['message'].get('content')
    return (isinstance(content, str) and bool(content)) or (
        isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'text'
                                         and isinstance(b.get('text'), str) and bool(b['text']) for b in content))


def safe_tool_name(value: object) -> str:
    if isinstance(value, str) and value.startswith('mcp__'):
        return 'MCP'
    return value if isinstance(value, str) and value in TOOLS else 'tool'


def block_codes(record: dict) -> List[str]:
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    codes = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind == 'thinking':
                codes.append('k')
            elif kind == 'text':
                codes.append('t')
            elif kind == 'tool_use':
                codes.append(safe_tool_name(block.get('name')))
    return codes


def scan_metadata(path: Path, result: FileResult) -> dict:
    responses, prompts, boundaries, seen = {}, [], [], set()
    for line, record in rollout.read_records(path, {}):
        if line > result['last_complete_line']:
            break
        uuid = worker._string(record.get('uuid'))
        record_key = worker._digest(['uuid', uuid]) if uuid else worker._digest([result['source_id'], line])
        if is_typed_prompt(record):
            prompts.append(line)
        if record.get('type') == 'assistant' and not worker._chargeable(record):
            response_key = worker._response_identity(record)
            if response_key:
                codes = block_codes(record)
                content = record.get('message', {}).get('content')
                unknown = sum(not isinstance(b, dict) or b.get('type') not in ('thinking', 'text', 'tool_use')
                              for b in content) if isinstance(content, list) else 0
                responses[(response_key, line, record_key)] = {
                    'blocks': codes, 'usage': worker._usage_fact(record['message'].get('usage'))[0],
                    'unknown': unknown,
                }
        if record.get('type') == 'system' and record.get('subtype') == 'compact_boundary' and record_key not in seen:
            seen.add(record_key)
            box = record.get('compactMetadata')
            box = box if isinstance(box, dict) else {}
            trigger = box.get('trigger')
            boundaries.append({'line': line, 'trigger': trigger if trigger in ('manual', 'auto') else 'unknown',
                               'pre': box.get('preTokens') if worker._integer(box.get('preTokens')) else None,
                               'post': box.get('postTokens') if worker._integer(box.get('postTokens')) else None})
    return {'responses': responses, 'prompts': prompts, 'boundaries': boundaries}


def verify_join(rows: List[ResponseRow], copies: Dict[str, ResponseCopy], metadata: dict) -> None:
    for row in rows:
        c = copies.get(row['response_key'])
        if c is None or not c['blocks']:
            raise FilmError(3, JOIN)
        for block in c['blocks']:
            key = (row['response_key'], block['physical_line'], block['record_key'])
            meta = metadata['responses'].get(key)
            if meta is None or meta['usage'] != block['usage']:
                raise FilmError(3, JOIN)
        terminals = [b for b in c['blocks'] if b['record_key'] == c['terminal_record_key'] and b['usage'] is not None]
        if not terminals:
            raise FilmError(3, JOIN)
        u = terminals[-1]['usage']
        expected = (u['base_input'], u['creation'], u['reads'], u['output'], u['thinking'],
                    u['write_5m'], u['write_1h'], u['valid_ttl'], u['speed'], u['inference_geo'], u['searches'])
        actual = (row['base_input_tokens'], row['cache_creation_input_tokens'], row['usage']['cached_input_tokens'],
                  row['usage']['output_tokens'], row['usage']['reasoning_output_tokens'], row['cache_write_5m'],
                  row['cache_write_1h'], row['cache_ttl_complete'], row['speed'], row['inference_geo'], row['web_search_requests'])
        if actual != expected or row['first_output_ts'] != c['blocks'][0]['ts']:
            raise FilmError(3, JOIN)


def join_steps(rows: List[ResponseRow], copies: Dict[str, ResponseCopy], metadata: dict) -> Tuple[List[dict], List[dict], Dict[str, int]]:
    retained = sorted((r for r in rows if r['usage']['total_tokens'] != 0),
                      key=lambda r: copies[r['response_key']]['blocks'][0]['physical_line'])
    steps, counters, previous_line, previous_time = [], collections.Counter(), 0, 0
    base_time = worker.epoch(retained[0]['first_output_ts']) if retained else None
    for row in retained:
        c = copies[row['response_key']]
        first = c['blocks'][0]['physical_line']
        blocks = []
        for block in c['blocks']:
            m = metadata['responses'][(row['response_key'], block['physical_line'], block['record_key'])]
            blocks.extend(m['blocks'])
            counters['film_unknown_blocks'] += m['unknown']
        end_time = worker.epoch(row['first_output_ts'])
        elapsed = None
        if base_time is not None and end_time is not None:
            elapsed = int(((Decimal(str(end_time)) - Decimal(str(base_time))) * 1000).quantize(
                Decimal('1'), rounding=ROUND_HALF_UP))
            if elapsed < previous_time:
                counters['film_time_regressions'] += 1
                elapsed = previous_time
            previous_time = elapsed
        u = row['usage']
        steps.append({'read': u['cached_input_tokens'], 'write': row['cache_creation_input_tokens'],
                      'input': row['base_input_tokens'], 'output': u['output_tokens'],
                      'thinking': u['reasoning_output_tokens'], 'prompt': any(previous_line < p < first for p in metadata['prompts']),
                      'elapsed_ms': elapsed, 'blocks': blocks})
        previous_line = first
        if any(first < b['line'] < c['blocks'][-1]['physical_line'] for b in metadata['boundaries']):
            counters['film_response_crosses_compaction'] += 1
    compactions, previous_after, previous_post = [], None, None
    for boundary in metadata['boundaries']:
        after = sum(copies[r['response_key']]['blocks'][0]['physical_line'] < boundary['line'] for r in retained)
        c = {k: boundary[k] for k in ('trigger', 'pre', 'post')}
        c['after'] = after
        compactions.append(c)
        if c['trigger'] == 'unknown':
            counters['film_unknown_trigger'] += 1
        if c['pre'] is None:
            counters['film_pre_fallback'] += 1
        if c['post'] is None:
            counters['film_post_fallback'] += 1
        if steps:
            nxt = steps[after] if after < len(steps) else None
            pre = (previous_post if previous_after == after else
                   sum(steps[after - 1][k] for k in ('read', 'write', 'input')) if after else
                   c['pre'] if c['pre'] is not None else sum(steps[0][k] for k in ('read', 'write', 'input')))
            setup = min(nxt['read'], pre) if nxt else 0
            proposed = (setup + c['post'] if c['post'] is not None else
                        sum(nxt[k] for k in ('read', 'write', 'input')) if nxt else pre)
            post = min(pre, proposed)
            if c['post'] is not None and proposed > pre:
                counters['film_compaction_clamped'] += 1
            previous_after, previous_post = after, post
    return steps, compactions, {k: v for k, v in counters.items() if v}


def read_main(path: Path, attempts: int = 2) -> Tuple[dict, dict, dict]:
    for attempt in range(attempts):
        try:
            with frozen_main(path) as (snapshot, size, digest):
                if not prefix_matches(path, size, digest):
                    raise FilmError(3, CHANGED)
                result = worker.extract(snapshot, metrics_only=True)
                if not result['stable_read'] or result['counters'].get('extraction_errors'):
                    raise FilmError(3, 'Could not extract the selected transcript; no film was written.')
                main_ledger = ledger.build([result], history=None)
                copies = collapsed_copies(result)
                metadata = scan_metadata(snapshot, result)
                verify_join(main_ledger['rows'], copies, metadata)
                steps, compactions, counters = join_steps(main_ledger['rows'], copies, metadata)
                if not prefix_matches(path, size, digest):
                    raise FilmError(3, CHANGED)
                if attempt:
                    counters['film_snapshot_retries'] = attempt
                # Endpoint times are physical retained output order, not ledger sort.
                ordered = sorted((r for r in main_ledger['rows'] if r['usage']['total_tokens']),
                                 key=lambda r: copies[r['response_key']]['blocks'][0]['physical_line'])
                first = worker.epoch(ordered[0]['first_output_ts']) if ordered else None
                last = worker.epoch(ordered[-1]['first_output_ts']) if ordered else None
                span = max(0, int(((Decimal(str(last)) - Decimal(str(first))) * 1000).quantize(
                    Decimal('1'), rounding=ROUND_HALF_UP))) if first is not None and last is not None else None
                metadata.update(steps=steps, compactions=compactions, counters=counters, span_ms=span)
                return main_ledger, metadata, result
        except FilmError as exc:
            if str(exc) != CHANGED or attempt + 1 == attempts:
                raise
        except OSError:
            raise FilmError(3, 'Could not read the selected transcript; no film was written.')
    raise FilmError(3, CHANGED)


def output_breakdown(steps: List[dict]) -> dict:
    missing = [s for s in steps if s['output'] > 0 and s['thinking'] is None]
    total_output = sum(s['output'] for s in steps)
    missing_output = sum(s['output'] for s in missing)
    return {'complete': not missing, 'thinking_present': 100 * missing_output <= total_output,
            'known_thinking_tokens': sum(s['thinking'] or 0 for s in steps),
            'missing_responses': len(missing), 'missing_output_tokens': missing_output}


def category_totals(steps: List[dict], thinking_present: bool) -> Dict[str, int]:
    totals = {k: sum(s[k] for s in steps) for k in ('read', 'write', 'input')}
    known = sum(s['thinking'] or 0 for s in steps) if thinking_present else 0
    totals['writing'] = sum(s['output'] for s in steps) - known
    if thinking_present:
        totals['thinking'] = known
    return {k: totals[k] for k in CATEGORIES if k in totals}


def price_projection(row: ResponseRow, category: str, thinking_present: bool) -> ResponseRow:
    out = copy.deepcopy(row)
    u = out['usage']
    # Retain the original whole prompt solely for long-context band selection.
    out['base_input_tokens'] = row['base_input_tokens'] if category == 'input' else 0
    u['cached_input_tokens'] = row['usage']['cached_input_tokens'] if category == 'read' else 0
    if category != 'write':
        out.update(cache_creation_input_tokens=0, cache_write_5m=0, cache_write_1h=0, cache_ttl_complete=True)
    thinking = (row['usage']['reasoning_output_tokens'] or 0) if thinking_present else 0
    u['output_tokens'] = thinking if category == 'thinking' else row['usage']['output_tokens'] - thinking if category == 'writing' else 0
    u['reasoning_output_tokens'] = 0
    out['web_search_requests'] = 0
    return out


def rounded_micro(value: float) -> int:
    number = Decimal(str(value))
    if not number.is_finite():
        raise FilmError(3, RANGE)
    # Integral conversion also handles large diagnostic-only subagent values.
    # The public projection separately enforces the renderer's integer range.
    return int((number * 1000000).to_integral_value(rounding=ROUND_HALF_UP))


def allocate_cents(micro: Dict[str, int], total_micro: int) -> Dict[str, int]:
    keys = [k for k in CATEGORIES + ('search',) if k in micro]
    cents = {k: micro[k] // 10000 for k in keys}
    remaining = (total_micro + 5000) // 10000 - sum(cents.values())
    if remaining < 0 or remaining > len(keys):
        raise FilmError(3, 'Could not reconcile receipt cents; no film was written.')
    order = sorted(keys, key=lambda k: (-(micro[k] % 10000), keys.index(k)))
    for k in order[:remaining]:
        cents[k] += 1
    return cents


def table_digest(table: Optional[dict]) -> Optional[str]:
    return hashlib.sha256(canonical_json({k: v for k, v in table.items() if k != 'path'})).hexdigest() if table else None


def price_categories(rows: List[ResponseRow], table_result: tuple, thinking_present: bool) -> dict:
    table, table_reason = table_result
    summary = pricing.summarize(rows, table_result)
    cats = [k for k in CATEGORIES if thinking_present or k != 'thinking']
    components, priced_tokens = {k: 0.0 for k in cats}, {k: 0 for k in cats}
    for row in rows:
        quote = pricing.price_row(row, table)
        if quote['tokens_usd'] is None:
            continue
        for k in cats:
            projection = price_projection(row, k, thinking_present)
            components[k] += pricing.price_row(projection, table)['tokens_usd']
            priced_tokens[k] += (projection['base_input_tokens'] if k == 'input' else
                                 projection['cache_creation_input_tokens'] if k == 'write' else
                                 projection['usage']['cached_input_tokens'] if k == 'read' else
                                 projection['usage']['output_tokens'])
    micro = {k: rounded_micro(v) if table else None for k, v in components.items()}
    search = rounded_micro(summary['web_search_usd']) if table else None
    total = rounded_micro(summary['usd']) if table and summary['available'] else None
    cents = allocate_cents(dict(micro, search=search), total) if total is not None else {k: None for k in cats + ['search']}
    return {'as_of': summary['as_of'], 'table_sha256': table_digest(table),
            'available': summary['available'], 'reason': ('table_unavailable' if table_reason == 'price_table_unavailable' else
                                                        'table_invalid' if table_reason == 'price_table_invalid' else
                                                        'no_priced_usage' if not summary['available'] else None),
            'priced': summary['priced'], 'unpriced': summary['unpriced'], 'priced_tokens': priced_tokens,
            'usd_micro': micro, 'search_calls': summary['web_search_calls'], 'search_usd_micro': search,
            'total_usd_micro': total, 'cents': cents,
            'total_cents': (total + 5000) // 10000 if total is not None else None}


def film_data(main: dict, metadata: dict, table_result: tuple) -> dict:
    steps, boundaries, rows = metadata['steps'], metadata['compactions'], main['rows']
    if not steps:
        raise FilmError(2, EMPTY)
    validate_public_data({'steps': steps, 'compactions': boundaries,
                          'recorded_searches': [r['web_search_requests'] or 0 for r in rows]})
    breakdown = output_breakdown(steps)
    ttl = set()
    for row in rows:
        if row['cache_creation_input_tokens']:
            ttl.add('1h' if row['cache_ttl_complete'] and row['cache_write_1h'] == row['cache_creation_input_tokens'] else
                    '5m' if row['cache_ttl_complete'] and row['cache_write_5m'] == row['cache_creation_input_tokens'] else 'mixed')
    read_discounted = True
    table = table_result[0]
    for row in rows:
        if row['usage']['cached_input_tokens']:
            quote = pricing.price_row(row, table)
            if quote['tokens_usd'] is None:
                read_discounted = False
            else:
                read_quote = pricing.price_row(price_projection(row, 'read', True), table)['tokens_usd']
                base = price_projection(row, 'input', True)
                base['base_input_tokens'] = row['usage']['cached_input_tokens']
                read_discounted = read_discounted and read_quote < pricing.price_row(base, table)['tokens_usd']
    data = {'schema': 1, 'plugin_version': VERSION, 'width': 1080, 'height': 1920, 'fps': 30, 'sample_rate': 48000,
            'span_ms': metadata['span_ms'], 'steps': steps, 'compactions': boundaries, 'output_breakdown': breakdown,
            'counts': {'responses': len(rows), 'steps': len(steps), 'zero_dropped': len(rows) - len(steps),
                       'partial': sum(r['partial'] for r in rows),
                       'compactions': {k: sum(c['trigger'] == k for c in boundaries) for k in ('manual', 'auto', 'unknown')}},
            'pricing': price_categories(rows, table_result, breakdown['thinking_present']),
            # Closed display evidence needed by the specified scale/explainer rules.
            'display_evidence': {'context_1m': any(r['context_1m'] for r in rows),
                                 'cache_ttl': next(iter(ttl)) if len(ttl) == 1 else 'mixed',
                                 'read_discounted': read_discounted}}
    validate_public_data(data)
    data['artifact_tag'] = artifact_tag(data)
    return data


def validate_public_data(data: dict) -> None:
    # All numerical fields are integral public measurements. Booleans are distinct.
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif type(value) is int and not 0 <= value <= MAX_SAFE:
            raise FilmError(3, RANGE)
        elif type(value) is float:
            raise FilmError(3, RANGE)
    grand = 0
    for step in data['steps']:
        if any(type(step[k]) is not int for k in ('read', 'write', 'input', 'output')):
            raise FilmError(3, RANGE)
        if step['thinking'] is not None and (type(step['thinking']) is not int or step['thinking'] > step['output']):
            raise FilmError(3, RANGE)
        grand += sum(step[k] for k in ('read', 'write', 'input', 'output'))
        if grand > MAX_SAFE:
            raise FilmError(3, RANGE)
        if any(b not in TOOLS | {'k', 't', 'MCP', 'tool'} for b in step['blocks']):
            raise FilmError(3, JOIN)
    if any(c['after'] > len(data['steps']) or c['trigger'] not in ('manual', 'auto', 'unknown') for c in data['compactions']):
        raise FilmError(3, JOIN)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def script_json(value: object) -> str:
    return canonical_json(value).decode('utf-8').replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def artifact_tag(data_without_tag: dict) -> str:
    return hashlib.sha256(canonical_json(data_without_tag)).hexdigest()[:12]


def font_bundle(plugin_root: Path) -> dict:
    directory = plugin_root / 'assets/vendor/fonts'
    return {key: base64.b64encode((directory / name).read_bytes()).decode('ascii')
            for key, name in (('fredoka', 'Fredoka.ttf'), ('mono', 'SpaceMono-Bold.ttf'))}


def license_bundle(plugin_root: Path) -> dict:
    directory = plugin_root / 'assets/vendor/fonts'
    return {name: base64.b64encode((directory / name).read_bytes()).decode('ascii')
            for name in ('OFL-Fredoka.txt', 'OFL-SpaceMono.txt')}


def render_html(data: dict, fonts: dict, licenses: dict, template: Path) -> bytes:
    text = template.read_text(encoding='utf-8')
    for marker, value in (('@@FILM_DATA@@', data), ('@@FONT_DATA@@', fonts), ('@@LICENSE_DATA@@', licenses)):
        if text.count(marker) != 1:
            raise FilmError(3, 'The film template is invalid; no film was written.')
        text = text.replace(marker, script_json(value))
    return text.replace('\r\n', '\n').encode('utf-8')


def omitted_subagents(main_path: Path, discovered: List[Path], main_result: FileResult, table_result: tuple) -> dict:
    directory = main_path.parent / main_path.stem / 'subagents'
    children = [p for p in discovered if p.parent == directory]
    results = [worker.extract(p, metrics_only=True) for p in children]
    family = ledger.build([main_result] + results, history=None)
    sources = {r['source_id'] for r in results}
    rows = [r for r in family['rows'] if r['source_id'] in sources]
    pricing_unavailable = False
    try:
        priced = pricing.summarize(rows, table_result)
        usd_micro = rounded_micro(priced['usd']) if priced['available'] else None
    except (OverflowError, ValueError, FilmError):
        # Unchanged float pricing cannot represent arbitrarily large child facts.
        # Child diagnostics must never prevent a valid main film from being made.
        pricing_unavailable = True
        priced = {'available': False, 'priced': 0, 'unpriced': len(rows),
                  'unpriced_responses': len(rows),
                  'unpriced_input': sum(r['usage']['input_tokens'] for r in rows),
                  'unpriced_output': sum(r['usage']['output_tokens'] for r in rows)}
        usd_micro = None
    main_only = ledger.build([main_result], history=None)
    main_rows = {r['response_key']: r for r in main_only['rows']}
    family_main = {r['response_key']: r for r in family['rows'] if r['source_id'] == main_result['source_id']}
    # Attribution/timing annotations may differ; compare charge measurements only.
    compared = ('usage', 'base_input_tokens', 'cache_creation_input_tokens', 'partial', 'raw_model')
    differs = sum(key not in family_main or any(row[k] != family_main[key][k] for k in compared) for key, row in main_rows.items())
    differs += len(set(family_main) - set(main_rows))
    # Family diagnostics are aggregate-only. Report the additional nonnegative
    # counts introduced by child evidence, plus prices of child-owned rows;
    # do not attribute the main transcript's existing warnings to a child.
    quality = {k: v - main_only['counters'].get(k, 0) for k, v in family['counters'].items()
               if k in COUNTER_NAMES and v > main_only['counters'].get(k, 0)}
    for k in COUNTER_SCOPES['pricing']:
        if priced.get(k, 0):
            quality[k] = quality.get(k, 0) + priced[k]
    if pricing_unavailable:
        quality['film_subagent_pricing_unavailable'] = 1
    return {'responses': len(rows), 'zero_responses': sum(r['usage']['total_tokens'] == 0 for r in rows),
            'input': sum(r['usage']['input_tokens'] for r in rows), 'cached': sum(r['usage']['cached_input_tokens'] for r in rows),
            'output': sum(r['usage']['output_tokens'] for r in rows),
            'known_thinking': sum(r['usage']['reasoning_output_tokens'] or 0 for r in rows),
            'total_tokens': sum(r['usage']['total_tokens'] for r in rows),
            'usd_micro': usd_micro,
            'priced': priced['priced'], 'unpriced': priced['unpriced'], 'quality': quality,
            '_main_rows_differ': differs}


def diagnostic_json(data: dict, main: dict, metadata: dict, omitted: dict) -> dict:
    return {'film': data, 'diagnostics': {
        'main_ledger_counters': {k: v for k, v in main['counters'].items() if k in COUNTER_NAMES},
        'pricing_counters': dict(metadata.get('pricing_counters', {})),
        'film_counters': {k: v for k, v in metadata['counters'].items() if k in FILM_COUNTERS}},
        'excluded_subagents': {k: v for k, v in omitted.items() if not k.startswith('_')},
        'family_comparison': {'main_rows_differ_from_main_only': omitted['_main_rows_differ']}}


def local_start(epoch_s: Optional[float], tz: Optional[datetime.tzinfo] = None) -> str:
    if epoch_s is None:
        return 'unavailable'
    try:
        return datetime.datetime.fromtimestamp(epoch_s, tz=tz).strftime('%Y-%m-%d %H:%M')
    except (ValueError, OSError, OverflowError):
        return 'unavailable'


def span_text(span_ms: Optional[int]) -> str:
    if span_ms is None:
        return 'unavailable'
    unit, label = (86400000, 'day') if span_ms >= 172800000 else (3600000, 'hour') if span_ms >= 7200000 else (60000, 'minute')
    n = span_ms // unit + 1
    return '%s %s%s' % (grouped(n), label, '' if n == 1 else 's')


def grouped(value: int) -> str:
    return format(value, ',d')


def money(cents: Optional[int]) -> str:
    return 'unavailable' if cents is None else '$%d.%02d' % (cents // 100, cents % 100)


def material_counters(ledger_counters: dict, pricing_counters: dict) -> Dict[str, int]:
    names = set(COUNTER_SCOPES['record'] + COUNTER_SCOPES['response'] + COUNTER_SCOPES['lineage'] +
                COUNTER_SCOPES['measurement'] + COUNTER_SCOPES['pricing'])
    names.add('tool_identity_conflict')
    names -= {'records_seen', 'complete_lines', 'nonusage_records', 'assistant_records', 'blocks_collapsed',
              'in_file_duplicate_blocks', 'zero_usage_responses', 'advisor_model_records', 'compaction_boundaries',
              'compaction_usage_unavailable', 'cross_file_block_copies', 'cross_file_response_copies', 'auxiliary_fact_copies',
              'continuation_links', 'fork_context_links', 'lineage_link_copies', 'composition_snapshot_copies',
              'cost_state_records', 'usage_report_cost_crosschecks', 'images_known_dimensions'}
    counters = collections.Counter(ledger_counters)
    counters.update(pricing_counters)
    return {k: counters[k] for k in sorted(names) if counters[k]}


def terminal_summary(prefix: str, data: dict, diagnostics: dict, omitted: dict, page: Path) -> str:
    counts, prices = data['counts'], data['pricing']
    totals = category_totals(data['steps'], data['output_breakdown']['thinking_present'])
    cats = list(totals)
    comp = counts['compactions']
    lines = ['Token Counter for Claude Code - token film ' + VERSION,
             'Session: %s (main thread only)' % prefix, 'Span: ' + span_text(data['span_ms']),
             'Steps: ' + grouped(counts['steps']), 'Zero-usage responses dropped: ' + grouped(counts['zero_dropped']),
             'Compactions: %s (manual %s, automatic %s, unknown %s)' % tuple(grouped(n) for n in (sum(comp.values()), comp['manual'], comp['auto'], comp['unknown'])),
             'Tokens: ' + ' | '.join(LABELS[k] + '=' + grouped(totals[k]) for k in cats) + ' | Total=' + grouped(sum(totals.values()))]
    if prices['available']:
        lines.append('API list value (USD, %s): ' % prices['as_of'] + ' | '.join(
            LABELS[k] + '=' + ('unpriced' if k != 'search' and totals[k] > 0 and prices['priced_tokens'][k] == 0 else money(prices['cents'][k]))
            for k in cats + ['search']) + ' | Total=' + money(prices['total_cents']))
        lines.append('Unrounded API list value: $%d.%06d (priced tokens plus recorded search fees)' % (
            prices['total_usd_micro'] // 1000000, prices['total_usd_micro'] % 1000000))
    else:
        lines.append('API list value: unavailable (%s)' % {'table_unavailable': 'table unavailable', 'table_invalid': 'table invalid', 'no_priced_usage': 'no priced usage'}[prices['reason']])
    lines.append('Pricing: %s priced %s; %s unpriced %s' % (grouped(prices['priced']), 'response' if prices['priced'] == 1 else 'responses',
                                                        grouped(prices['unpriced']), 'response' if prices['unpriced'] == 1 else 'responses'))
    if data['output_breakdown']['missing_responses']:
        b = data['output_breakdown']
        lines.append('Thinking: unrecorded for %s %s (%s output tokens counted as Writing).' % (
            grouped(b['missing_responses']), 'response' if b['missing_responses'] == 1 else 'responses', grouped(b['missing_output_tokens'])))
    lines.append('Omitted subagents: %s %s | %s tokens | API list value %s' % (
        grouped(omitted['responses']), 'response' if omitted['responses'] == 1 else 'responses', grouped(omitted['total_tokens']),
        '$%d.%06d' % (omitted['usd_micro'] // 1000000, omitted['usd_micro'] % 1000000) if omitted['usd_micro'] is not None else 'unavailable'))
    lines.append('Quality (main ledger and pricing; sorted nonzero counters):')
    lines.extend('%s: %s' % (k, grouped(v)) for k, v in material_counters(diagnostics['main_ledger_counters'], diagnostics['pricing_counters']).items())
    lines.append('Quality (film metadata; sorted nonzero counters):')
    lines.extend('%s: %s' % (k, grouped(v)) for k, v in sorted(diagnostics['film_counters'].items()) if v)
    lines.extend(('Page: ' + str(page), 'Save MP4: open the page, then select Save MP4. Nothing is uploaded.'))
    return '\n'.join(lines) + '\n'


def list_sessions(files: List[Path], limit: int = 10) -> List[dict]:
    prefixes = candidate_prefixes(files)
    newest = sorted(files, key=lambda p: (-rollout.stat_key(p)[1], os.path.normcase(str(p.resolve()))))[:limit]
    entries = []
    for path in newest:
        try:
            main_ledger, metadata, result = read_main(path)
        except FilmError:
            entries.append({'prefix': prefixes[path], 'start': 'unavailable', 'span_ms': None,
                            'steps': None, 'tokens': None, 'compactions': None})
            continue
        entries.append({'prefix': prefixes[path], 'start': local_start(worker.epoch(result['first_record_ts'])),
                        'span_ms': metadata['span_ms'], 'steps': len(metadata['steps']),
                        'tokens': sum(r['usage']['total_tokens'] for r in main_ledger['rows']),
                        'compactions': len(metadata['compactions'])})
    return entries


def format_listing(entries: List[dict]) -> str:
    lines = ['Recent main sessions (up to 10, newest modified first):', 'SESSION\tSTART (LOCAL)\tSPAN\tSTEPS\tTOKENS\tCOMPACTIONS']
    lines.extend(e['prefix'] + '\tunavailable' * 5 if e['steps'] is None else
                 '%s\t%s\t%s\t%d\t%s\t%d' % (e['prefix'], e['start'], span_text(e['span_ms']), e['steps'], grouped(e['tokens']), e['compactions']) for e in entries)
    return '\n'.join(lines) + '\n'


def output_paths(options: argparse.Namespace, resolved: dict, data: dict) -> Tuple[Path, Optional[Path]]:
    page = Path(options.out).expanduser() if options.out else resolved['state_dir'] / 'films' / ('film-%s-v%s.html' % (data['artifact_tag'], VERSION))
    json_path = Path(options.json).expanduser().resolve() if options.json and options.json != '-' else None
    return page.resolve(), json_path


def validate_destinations(page: Path, json_path: Optional[Path], inputs: List[Path], resolved: dict, plugin_root: Path) -> None:
    reserved = [resolved[k] for k in ('index_path', 'history_path', 'report_path', 'shared_report_path', 'share_state_path', 'account_path')]
    forbidden = {os.path.normcase(str(p.resolve())) for p in inputs + reserved}
    forbidden.update(os.path.normcase(str(p.resolve())) + s for p in reserved for s in ('-wal', '-shm', '-journal'))
    if json_path == page:
        raise FilmError(2, 'HTML and JSON destinations must be different.')
    for path, suffix in ((page, '.html'), (json_path, '.json')):
        if path is None:
            continue
        if path.suffix != suffix:
            raise FilmError(2, 'The %s destination must end in %s.' % ('page' if suffix == '.html' else 'JSON', suffix))
        if (paths.is_within(path, plugin_root) or paths.is_within(path, resolved['sessions_root']) or
                os.path.normcase(str(path.resolve())) in forbidden):
            raise FilmError(2, 'The destination collides with plugin, transcript, price or report state.')


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix='.film-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(body)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: Optional[List[str]] = None) -> int:
    try:
        options = parse_args(argv)
        resolved = paths.resolve_paths(options.sessions_root)
        files, discovery_counters = rollout.discover(resolved['sessions_root'])
        mains = [p for p in files if p.parent.name != 'subagents']
        if options.list:
            print(format_listing(list_sessions(mains)), end='')
            return 0
        selected = select_session(mains, current_prefix(os.environ) if options.current else options.session)
        main_ledger, metadata, result = read_main(selected)
        table_result = pricing.load(options.prices)
        data = film_data(main_ledger, metadata, table_result)
        priced_summary = pricing.summarize(main_ledger['rows'], table_result)
        metadata['pricing_counters'] = {k: priced_summary.get(k, 0) for k in COUNTER_SCOPES['pricing']}
        for k, v in discovery_counters.items():
            bump(main_ledger['counters'], k, v)
        page, json_path = output_paths(options, resolved, data)
        inputs = files + [Path(options.prices or pricing.vendor_path())]
        validate_destinations(page, json_path, inputs, resolved, PLUGIN_ROOT)
        html = render_html(data, font_bundle(PLUGIN_ROOT), license_bundle(PLUGIN_ROOT), HERE.parent / 'assets/film.html')
        omitted = omitted_subagents(selected, files, result, table_result)
        diagnostic = diagnostic_json(data, main_ledger, metadata, omitted)
        atomic_write(page, html)
        if json_path is not None:
            atomic_write(json_path, canonical_json(diagnostic) + b'\n')
        stream = sys.stderr if options.json == '-' else sys.stdout
        stream.write(terminal_summary(candidate_prefixes(mains)[selected], data, diagnostic['diagnostics'], omitted, page))
        if options.json == '-':
            body = canonical_json(diagnostic) + b'\n'
            if hasattr(sys.stdout, 'buffer'):
                sys.stdout.buffer.write(body)
            else:
                sys.stdout.write(body.decode('utf-8'))
        if not options.no_open:
            reportcli._open(str(page))
        return 0
    except FilmError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except (OSError, ValueError, OverflowError) as exc:
        # Never print source exceptions: they can contain private paths/record text.
        print('Could not create the film artifacts.', file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
