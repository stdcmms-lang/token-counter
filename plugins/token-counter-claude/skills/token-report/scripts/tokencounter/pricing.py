"""Offline Anthropic API list value for captured responses, with explicit defaults.

Counts remain the ledger's recorded measurements. The high scenario changes supported
unrecorded speed and cache TTL only; it cannot bound missing calls or search counts.
"""
import collections
import datetime
import json
import math
from pathlib import Path
import re

from .models import RowPrice, bump
from .worker import _model_name, epoch

RATE_KEYS = ('input', 'cache_write_5m', 'cache_write_1h', 'cached', 'output')
TIER_CLASSES = ('standard', 'fast')
MODEL_RE = re.compile(r'^claude-[a-z0-9]+(?:-[a-z0-9]+)*$')
ASSUMPTIONS = ('unsupported_fast_setting', 'speed_unrecorded', 'price_cache_ttl_assumed',
               'price_geography_default_global', 'search_count_unrecorded',
               'price_search_failure_ambiguous', 'price_partial_responses',
               'price_table_unavailable', 'price_table_invalid')
EXCLUSIONS = ('unpriced_responses', 'unpriced_input', 'unpriced_output', 'unpriced_model',
              'unpriced_speed', 'unpriced_geography')


def vendor_path() -> str:
    return str(Path(__file__).resolve().parents[4] / 'assets/vendor/anthropic_prices.json')


def _rate_ok(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _rates_ok(record):
    return isinstance(record, dict) and set(record) == set(RATE_KEYS) and all(
        _rate_ok(record[key]) for key in RATE_KEYS)


def _table_ok(table):
    if not isinstance(table, dict) or type(table.get('schema')) is not int or table['schema'] != 1:
        return False
    if table.get('vendor') != 'anthropic' or table.get('units') != 'USD per million tokens':
        return False
    if not isinstance(table.get('as_of'), str) or not isinstance(table.get('source_url'), str) or not table['source_url']:
        return False
    if datetime.date.fromisoformat(table['as_of']).isoformat() != table['as_of']:
        return False
    records, aliases, fees = table.get('models'), table.get('aliases'), table.get('fees')
    if not isinstance(records, dict) or not records or not isinstance(aliases, dict):
        return False
    if not isinstance(fees, dict) or set(fees) != {'web_search_request', 'web_fetch_request'} or not all(
            _rate_ok(fee) for fee in fees.values()) or fees['web_fetch_request'] != 0:
        return False
    fields = set(RATE_KEYS) | {'long_context_threshold', 'long_context', 'fast',
                              'us_residency_supported', 'image_tier'}
    for name, record in records.items():
        if not isinstance(name, str) or not MODEL_RE.fullmatch(name) or not isinstance(record, dict) or set(record) != fields:
            return False
        if not all(_rate_ok(record[k]) for k in RATE_KEYS):
            return False
        threshold, alternate = record['long_context_threshold'], record['long_context']
        if threshold is None:
            if alternate is not None:
                return False
        elif type(threshold) is not int or threshold <= 0 or not _rates_ok(alternate):
            return False
        if record['fast'] is not None and not _rates_ok(record['fast']):
            return False
        if type(record['us_residency_supported']) is not bool or record['image_tier'] not in ('standard', 'high'):
            return False
    return all(isinstance(alias, str) and MODEL_RE.fullmatch(alias) and alias not in records
               and isinstance(target, str) and target in records for alias, target in aliases.items())


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate price field')
        out[key] = value
    return out


def load(path=None) -> tuple:
    try:
        with open(vendor_path() if path is None else path, encoding='utf-8') as handle:
            table = json.load(handle, object_pairs_hook=_unique_object)
    except OSError:
        return None, 'price_table_unavailable'
    except (ValueError, UnicodeError):
        return None, 'price_table_invalid'
    try:
        if not _table_ok(table):
            return None, 'price_table_invalid'
    except (ValueError, TypeError, OverflowError):
        return None, 'price_table_invalid'
    table['path'] = str(vendor_path() if path is None else path)
    return table, None


def canonical_model(raw, prices=None) -> tuple:
    """(canonical id, safe raw id, context_1m); unknown dated IDs remain named."""
    model, safe, context = _model_name(raw)
    if prices is not None and safe is not None:
        name = safe[:-4] if context else safe
        model = prices['aliases'].get(name, name)
    return model, safe, context


def tier_class(raw) -> str:
    return raw if isinstance(raw, str) and raw in TIER_CLASSES else 'unknown'


def _excluded(row):
    if row.get('raw_model') == '<synthetic>' or row.get('model') == '<synthetic>' or 'synthetic_records' in row.get('quality_flags', ()):
        return 'synthetic'
    if row.get('isApiErrorMessage') is True or 'api_error_records' in row.get('quality_flags', ()):
        return 'api_error'
    return None


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError('noninteger recorded count')
    return value


def _token_value(row, rates, write_5m, write_1h, geo):
    usage = row['usage']
    return (row['base_input_tokens'] * rates['input'] + write_5m * rates['cache_write_5m']
            + write_1h * rates['cache_write_1h'] + usage['cached_input_tokens'] * rates['cached']
            + usage['output_tokens'] * rates['output']) * geo / 1_000_000


def _unpriced(result, row, reason):
    result['unpriced_reason'] = reason
    for name, count in (('unpriced_responses', 1), ('unpriced_input', row['usage']['input_tokens']),
                        ('unpriced_output', row['usage']['output_tokens'])):
        bump(result['counters'], name, count)
    if reason in ('model', 'speed', 'geography'):
        bump(result['counters'], 'unpriced_' + reason)
    return result


def price_row(row, prices) -> RowPrice:
    counters = {}
    result = {'tokens_usd': None, 'tokens_usd_high': None, 'web_search_usd': 0.0,
              'web_search_calls': 0, 'unpriced_reason': _excluded(row), 'counters': counters}
    if result['unpriced_reason'] is not None:
        return result
    if row.get('partial'):
        bump(counters, 'price_partial_responses')
    searches = row.get('web_search_requests')
    if type(searches) is not int or searches < 0:
        searches = 0
        bump(counters, 'search_count_unrecorded')
    result.update(web_search_calls=searches,
                  web_search_usd=searches * prices['fees']['web_search_request'] if prices is not None else 0.0)
    if searches and (row.get('web_search_failures') or 'price_search_failure_ambiguous' in row.get('quality_flags', ())):
        bump(counters, 'price_search_failure_ambiguous')
    if prices is None:
        return _unpriced(result, row, 'table_unavailable')
    model = canonical_model(row.get('raw_model') or row.get('model'), prices)[0]
    record = prices['models'].get(model)
    if record is None:
        return _unpriced(result, row, 'model')
    usage = row['usage']
    for value in (row['base_input_tokens'], row['cache_creation_input_tokens'],
                  usage['input_tokens'], usage['cached_input_tokens'], usage['output_tokens']):
        _count(value)
    rates = record
    threshold = record['long_context_threshold']
    if threshold is not None and usage['input_tokens'] > threshold:
        rates = record['long_context']
    geography = row.get('inference_geo')
    if geography in (None, 'not_available'):
        bump(counters, 'price_geography_default_global')
    geo = 1.0
    if geography == 'us' and record['us_residency_supported']:
        geo = 1.1
    elif geography not in (None, 'not_available', 'global'):
        return _unpriced(result, row, 'geography')
    speed = row.get('speed')
    high_rates = rates
    if speed is None:
        bump(counters, 'speed_unrecorded')
        high_rates = record['fast'] or rates
    elif speed == 'fast':
        if record['fast'] is not None:
            rates = high_rates = record['fast']
        else:
            bump(counters, 'unsupported_fast_setting')
            if model != 'claude-opus-4-6':
                return _unpriced(result, row, 'speed')
    elif speed != 'standard':
        return _unpriced(result, row, 'speed')
    creation = row['cache_creation_input_tokens']
    a, b = row.get('cache_write_5m'), row.get('cache_write_1h')
    exact = (row.get('cache_ttl_complete') is True and type(a) is int and type(b) is int
             and a >= 0 and b >= 0 and a + b == creation)
    if creation and not exact:
        a, b = creation, 0
        high_a, high_b = 0, creation
        bump(counters, 'price_cache_ttl_assumed')
    else:
        a, b = (a, b) if exact else (0, 0)
        high_a, high_b = a, b
    result.update(tokens_usd=_token_value(row, rates, a, b, geo),
                  tokens_usd_high=_token_value(row, high_rates, high_a, high_b, geo))
    return result


def summarize(rows, prices) -> dict:
    return _summarize(rows, prices, price_row)


def _summarize(rows, prices, quote_row):
    """Aggregate quotes; analysis supplies its call-local price cache."""
    table, reason = prices if isinstance(prices, tuple) else (prices, None)
    counters = collections.Counter()
    if table is None:
        reason = reason or 'price_table_unavailable'
        bump(counters, reason)
    token_usd = high = search_usd = 0.0
    calls = priced = responses = 0
    unpriced, missing = collections.Counter(), collections.defaultdict(collections.Counter)
    by_model = collections.defaultdict(collections.Counter)
    for row in rows:
        if _excluded(row):
            continue
        responses += 1
        value = quote_row(row, table)
        for name, count in value['counters'].items():
            bump(counters, name, count)
        calls += value['web_search_calls']
        search_usd += value['web_search_usd']
        model = canonical_model(row.get('raw_model') or row.get('model'), table)[0]
        if value['unpriced_reason']:
            why = value['unpriced_reason']
            unpriced[why] += 1
            counts = missing[(model, why)]
            counts.update(responses=1, input=row['usage']['input_tokens'], output=row['usage']['output_tokens'])
        else:
            priced += 1
            token_usd += value['tokens_usd']
            high += value['tokens_usd_high']
        by_model[model]['usd'] += (value['tokens_usd'] or 0.0) + value['web_search_usd']
        by_model[model]['responses'] += 1
    out = {'available': bool(priced or search_usd), 'reason': reason,
           'usd': round(token_usd + search_usd, 6), 'usd_high': round(high + search_usd, 6),
           'tokens_usd': round(token_usd, 6), 'web_search_usd': round(search_usd, 6),
           'web_search_calls': calls, 'as_of': table.get('as_of') if table else None,
           'responses': responses, 'priced': priced, 'unpriced': sum(unpriced.values()),
           'unpriced_by_reason': dict(sorted(unpriced.items())),
           'unpriced_models': [dict(c, model=m, reason=why) for (m, why), c in sorted(
               missing.items(), key=lambda item: (-item[1]['responses'], item[0]))],
           'by_model': [dict(model=m, usd=round(c['usd'], 6), responses=c['responses']) for m, c in sorted(
               by_model.items(), key=lambda item: (-item[1]['usd'], item[0]))],
           'prices': {'as_of': table.get('as_of') if table else None,
                      'source': table.get('source_url') if table else None,
                      'models': len(table['models']) if table else 0,
                      'default': bool(table and table.get('path') == vendor_path())}}
    out.update({name: counters[name] for name in ASSUMPTIONS + EXCLUSIONS})
    out.update(counters)
    if not out['available'] and reason is None:
        out['reason'] = 'no responses in range' if not responses else 'no response could be priced'
    return out


def crosscheck(cost_checks, rows, prices) -> dict:
    """Reported snapshots beside same-source/model/context captured counts and value."""
    return _crosscheck(cost_checks, rows, prices, price_row)


def _crosscheck(cost_checks, rows, prices, quote_row):
    table = prices[0] if isinstance(prices, tuple) else prices
    by_key = collections.defaultdict(list)
    # Price every row once; a source's cost snapshots each cover most of its rows.
    priced = {}
    for row in rows:
        by_key[(row['source_id'], canonical_model(row.get('raw_model') or row['model'], table)[0],
                row['context_1m'])].append(row)
        if not _excluded(row):
            priced[id(row)] = quote_row(row, table)
    checks, unmatched = [], []
    for fact in cost_checks:
        end = epoch(fact['ts'])
        model = canonical_model(fact.get('raw_model') or fact['model'], table)[0]
        matching = [r for r in by_key[(fact['source_id'], model, fact['context_1m'])]
                    if end is None or (epoch(r['ts']) is not None and epoch(r['ts']) <= end)]
        captured = {'responses': len(matching), 'base_input': sum(r['base_input_tokens'] for r in matching),
                    'creation': sum(r['cache_creation_input_tokens'] for r in matching),
                    'reads': sum(r['usage']['cached_input_tokens'] for r in matching),
                    'output': sum(r['usage']['output_tokens'] for r in matching),
                    'thinking': sum(r['usage']['reasoning_output_tokens'] or 0 for r in matching),
                    'searches': sum(r['web_search_requests'] or 0 for r in matching)}
        # The same accumulation and rounding as summarize(), from the cached row prices.
        values = [priced[id(r)] for r in matching if id(r) in priced]
        tokens = sum(v['tokens_usd'] for v in values if v['tokens_usd'] is not None)
        high = sum(v['tokens_usd_high'] for v in values if v['tokens_usd_high'] is not None)
        search = sum(v['web_search_usd'] for v in values)
        available = bool(any(v['tokens_usd'] is not None for v in values) or search)
        item = {'source': fact['source'], 'ts': fact['ts'], 'model': model,
                'raw_model': fact.get('raw_model'), 'context_1m': fact['context_1m'],
                'source_id': fact['source_id'], 'reported_counts': dict(fact['counts']),
                'captured_counts': captured, 'reported_cost_usd': fact['cost_usd'],
                'captured_usd': round(tokens + search, 6) if available else None,
                'captured_usd_high': round(high + search, 6) if available else None,
                'cost_basis': fact['cost_basis'], 'unmatched': not matching}
        checks.append(item)
        if not matching:
            unmatched.append({'model': model, 'context_1m': fact['context_1m'],
                              'reported_counts': dict(fact['counts'])})
    return {'checks': checks, 'unmatched_models': unmatched}
