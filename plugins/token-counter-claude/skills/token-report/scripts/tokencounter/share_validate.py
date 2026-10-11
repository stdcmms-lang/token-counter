"""Content-free local checks for the pinned schema-1 server contract.

Issues are literal catalogue entries, never values, paths or exception messages.
The Claude client additionally fixes its identity, session hash length and weekly
duration. Gzip/doctype/body checks belong to the HTTP API, not Zod safeParse.
"""
import base64
import datetime
import gzip
import io
import json
import math
import re
import zlib
from typing import List

CLIENT = {'name': 'claude-usage', 'version': '0.1.0'}
MAX_BODY_BYTES = 2000000
MAX_DAYS = 4000
MAX_SESSIONS = 3000
MAX_WINDOWS = 1000
MAX_SPLIT_ROWS = 50
MAX_SPLIT_TOTAL = 1000
MAX_GROUPS = 50
MAX_REPORT_BYTES = 8000000
MAX_REPORT_GZ_BYTES = 900000
MAX_ACTIVE_S = 30 * 86400
MAX_SPAN_S = 400 * 86400
EARLIEST_DAY = '2025-04-01'
COUNT_FIELDS = ('responses', 'input', 'cached', 'output', 'reasoning')
WINDOW_COUNTS = COUNT_FIELDS[:4]
TIERS = ('standard', 'fast', 'ultrafast')
HANDLE_RE = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}\Z', re.ASCII)
INSTANT_RE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})\Z', re.ASCII)

# Shape catalogue plus arithmetic catalogue. Prefixes describe fixed contract
# locations; they never contain the model, date, ID or index of a rejected row.
ISSUES = frozenset('''payload_object payload_schema client_shape client_name client_version
handle_invalid generated_at_invalid days_shape days_budget day_shape day_date day_counts
day_tiers_shape tier_counts sessions_shape sessions_budget session_shape session_id
session_day session_instant session_counts session_model windows_shape windows_budget
window_shape window_start window_minutes window_plan window_percent window_counts
split_shape split_budget split_total_budget split_row_shape split_model split_tier
split_counts latency_shape latency_dates timing_shape timing_count timing_seconds
groups_shape groups_budget group_shape group_model group_effort group_fit group_share
tier_groups_shape tier_groups_budget group_tier clock_shape clock_budget clock_key
clock_count clock_seconds clock_above payload_body_budget payload_json_invalid
day_duplicate day_before day_future day_no_responses day_cached day_reasoning
day_input_bound day_sessions tier_cached tier_reasoning tier_no_responses
tier_responses_total tier_input_total tier_cached_total tier_output_total tier_reasoning_total
session_duplicate session_reversed session_span session_future session_active_span
session_active_bound session_day_range session_day_missing session_no_responses
session_cached session_reasoning session_input_total window_duplicate window_before
window_future window_first_peak window_cached window_no_responses window_input_total
split_duplicate split_cached split_cache_write split_no_responses split_responses_total
split_input_total split_cached_total split_output_total latency_reversed latency_before
latency_future latency_span response_median response_p90 response_total turn_median
turn_p90 turn_total group_duplicate group_median group_p90 group_total tier_group_duplicate
tier_group_median tier_group_p90 tier_group_total clock_duplicate clock_median clock_total
report_object report_schema report_base64 report_gzip report_gzip_budget report_size
report_doctype report_body_budget'''.split())


def payload_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def valid_handle(value):
    return (isinstance(value, str) and 3 <= len(value) <= 24
            and HANDLE_RE.fullmatch(value) is not None
            and value not in ('api', 'admin', 'share'))


def _number(value, low=0, high=None, integer=False):
    # JSON has one number type on the server: 1.0 is an integer value, True is not.
    return (type(value) in (int, float)
            and abs(value) <= 1.7976931348623157e308
            and math.isfinite(value) and value >= low
            and (not integer or value <= 9007199254740991 and value % 1 == 0)
            and (high is None or value <= high))


def _day(value):
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        return False
    try:
        datetime.date.fromisoformat('2000' + value[4:] if value.startswith('0000') else value)
        return True
    except ValueError:
        return False


def _instant(value):
    if not isinstance(value, str) or INSTANT_RE.fullmatch(value) is None:
        return None
    try:
        zero_year = value.startswith('0000')
        parsed = datetime.datetime.fromisoformat(('2000' + value[4:] if zero_year else value).replace('Z', '+00:00'))
        # ISO offset hours must be valid independently of datetime's normalization.
        if value[-6:-5] in ('+', '-') and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            return None
        # Date.parse truncates fractional seconds to milliseconds. Compute the
        # offset without normalizing into a year outside datetime's range.
        delta = parsed.replace(tzinfo=datetime.timezone.utc) - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
        milliseconds = (delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
                        - int(parsed.utcoffset().total_seconds()) * 1000)
        if zero_year:
            milliseconds -= 5 * 146097 * 86400000  # five 400-year Gregorian cycles
        return milliseconds / 1000
    except (ValueError, OverflowError, OSError):
        return None


def _client(body, add):
    client = body.get('client')
    if not isinstance(client, dict):
        add('client_shape')
        return
    if client.get('name') != CLIENT['name']:
        add('client_name')
    if client.get('version') != CLIENT['version']:
        add('client_version')


def _text(value, limit, nullable=False):
    # Zod counts UTF-16 code units. Claude model IDs themselves are ASCII.
    return (nullable and value is None or isinstance(value, str)
            and 1 <= len(value.encode('utf-16-le', errors='surrogatepass')) // 2 <= limit)


def _counts(row, fields):
    return all(_number(row.get(k), integer=True) for k in fields)


def _shape(p, add):
    if not _number(p.get('schema'), integer=True) or p['schema'] != 1:
        add('payload_schema')
    _client(p, add)
    if p.get('handle') is not None and not valid_handle(p['handle']):
        add('handle_invalid')
    if _instant(p.get('generated_at')) is None:
        add('generated_at_invalid')
    for key, cap in (('days', MAX_DAYS), ('sessions', MAX_SESSIONS), ('windows', MAX_WINDOWS)):
        if key == 'windows' and key not in p:
            continue
        rows = p.get(key)
        if not isinstance(rows, list):
            add(key + '_shape')
            continue
        if len(rows) > cap:
            add(key + '_budget')
        for row in rows:
            singular = {'days': 'day', 'sessions': 'session', 'windows': 'window'}[key]
            if not isinstance(row, dict):
                add(singular + '_shape')
                continue
            fields = WINDOW_COUNTS if key == 'windows' else COUNT_FIELDS + (('sessions',) if key == 'days' else ('active_s',))
            if not _counts(row, fields):
                add(singular + '_counts')
            if key == 'days':
                if not _day(row.get('date')):
                    add('day_date')
                if 'tiers' in row:
                    if not isinstance(row['tiers'], dict):
                        add('day_tiers_shape')
                    else:
                        for tier in TIERS:
                            if tier in row['tiers'] and (not isinstance(row['tiers'][tier], dict)
                                    or not _counts(row['tiers'][tier], COUNT_FIELDS)):
                                add('tier_counts')
            elif key == 'sessions':
                if not isinstance(row.get('id'), str) or re.fullmatch('[0-9a-f]{16}', row['id']) is None:
                    add('session_id')
                if not _day(row.get('day')):
                    add('session_day')
                if any(_instant(row.get(k)) is None for k in ('start', 'end')):
                    add('session_instant')
                if not _text(row.get('model'), 80, nullable=True):
                    add('session_model')
            else:
                if _instant(row.get('start')) is None:
                    add('window_start')
                if not _number(row.get('window_minutes'), integer=True) or row['window_minutes'] != 10080:
                    add('window_minutes')
                if not _text(row.get('plan'), 40, nullable=True):
                    add('window_plan')
                if any(not _number(row.get(k), high=100) for k in ('first_pct', 'peak_pct')):
                    add('window_percent')
                if 'split' in row:
                    split = row['split']
                    if not isinstance(split, list):
                        add('split_shape')
                        continue
                    if len(split) > MAX_SPLIT_ROWS:
                        add('split_budget')
                    for s in split:
                        if not isinstance(s, dict):
                            add('split_row_shape')
                            continue
                        if not _text(s.get('model'), 80):
                            add('split_model')
                        if s.get('tier') not in TIERS:
                            add('split_tier')
                        if not _counts(s, WINDOW_COUNTS) or any(k in s and not _number(s[k], integer=True)
                                for k in ('cache_write_5m', 'cache_write_1h')):
                            add('split_counts')
    if isinstance(p.get('windows'), list) and sum(len(w.get('split', [])) for w in p['windows']
            if isinstance(w, dict) and isinstance(w.get('split', []), list)) > MAX_SPLIT_TOTAL:
        add('split_total_budget')
    if 'latency' in p:
        _latency_shape(p['latency'], add)


def _timing_shape(t, add):
    if not isinstance(t, dict):
        add('timing_shape')
        return
    if not _number(t.get('n'), low=1, integer=True):
        add('timing_count')
    if any(not _number(t.get(k)) for k in ('median_s', 'p90_s')):
        add('timing_seconds')


def _latency_shape(l, add):
    if not isinstance(l, dict):
        add('latency_shape')
        return
    if any(not _day(l.get(k)) for k in ('from', 'to')):
        add('latency_dates')
    _timing_shape(l.get('responses'), add)
    t = l.get('turns')
    if t is not None and not (isinstance(t, dict) and _number(t.get('n')) and t['n'] == 0):
        _timing_shape(t, add)
        if isinstance(t, dict) and t.get('model_share') is not None and not _number(t['model_share'], high=1):
            add('group_share')
    if not _text(l.get('plan'), 40, nullable=True):
        add('window_plan')
    for key in ('groups', 'tier_groups'):
        if key == 'tier_groups' and key not in l:
            continue
        groups = l.get(key)
        if not isinstance(groups, list):
            add(key + '_shape')
            continue
        if len(groups) > MAX_GROUPS:
            add(key + '_budget')
        for g in groups:
            if not isinstance(g, dict):
                add('group_shape')
                continue
            _timing_shape(g, add)
            if not _text(g.get('model'), 80):
                add('group_model')
            if not _text(g.get('effort'), 40):
                add('group_effort')
            if key == 'tier_groups' and g.get('tier') not in TIERS:
                add('group_tier')
            if any(g.get(k) is not None and not _number(g[k]) for k in ('overhead_s', 'output_tps')):
                add('group_fit')
            if g.get('above_share') is not None and not _number(g['above_share'], high=1):
                add('group_share')
    for key, field, low, cap in (('utc_hours', 'hour', 0, 24), ('utc_weekdays', 'day', 1, 7)):
        if key not in l:
            continue
        buckets = l[key]
        if not isinstance(buckets, list):
            add('clock_shape')
            continue
        if len(buckets) > cap:
            add('clock_budget')
        for b in buckets:
            if not isinstance(b, dict):
                add('clock_shape')
                continue
            if not _number(b.get(field), low=low, high=cap - (1 if low == 0 else 0), integer=True):
                add('clock_key')
            if not _number(b.get('n'), low=1, integer=True):
                add('clock_count')
            if not _number(b.get('median_s')):
                add('clock_seconds')
            if b.get('median_above_s') is not None and not _number(b['median_above_s']):
                add('clock_above')


def validate_payload(payload, *, now) -> List[str]:
    issues = []
    def add(code):
        assert code in ISSUES
        if code not in issues:
            issues.append(code)
    if not isinstance(payload, dict):
        return ['payload_object']
    _shape(payload, add)
    try:
        if len(payload_bytes(payload)) > MAX_BODY_BYTES:
            add('payload_body_budget')
    except (ValueError, TypeError, UnicodeError, OverflowError):
        add('payload_json_invalid')
    if issues:
        return issues
    current = now.timestamp() if isinstance(now, datetime.datetime) else float(now)
    current = math.floor(current * 1000) / 1000
    latest = datetime.datetime.fromtimestamp(current + 86400, datetime.timezone.utc).date().isoformat()
    limit = current + 600
    seen, day_input, day_responses = set(), 0, 0
    for d in payload['days']:
        if d['date'] in seen: add('day_duplicate')
        seen.add(d['date'])
        if d['date'] < EARLIEST_DAY: add('day_before')
        if d['date'] > latest: add('day_future')
        if d['responses'] < 1: add('day_no_responses')
        if d['cached'] > d['input']: add('day_cached')
        if d['reasoning'] > d['output']: add('day_reasoning')
        if d['input'] > 20000000000: add('day_input_bound')
        if d['sessions'] > d['responses']: add('day_sessions')
        tiers = [d.get('tiers', {})[k] for k in TIERS if k in d.get('tiers', {})]
        for t in tiers:
            if t['cached'] > t['input']: add('tier_cached')
            if t['reasoning'] > t['output']: add('tier_reasoning')
            if t['input'] + t['output'] and not t['responses']: add('tier_no_responses')
        for k in COUNT_FIELDS:
            if sum(t[k] for t in tiers) > d[k]: add('tier_' + k + '_total')
        day_input += d['input']
        day_responses += d['responses']
    ids, session_input = set(), 0
    for s in payload['sessions']:
        if s['id'] in ids: add('session_duplicate')
        ids.add(s['id'])
        start, end = _instant(s['start']), _instant(s['end'])
        span = end - start
        if span < 0: add('session_reversed')
        if span > MAX_SPAN_S: add('session_span')
        if end > limit: add('session_future')
        if s['active_s'] > span + 1: add('session_active_span')
        if s['active_s'] > MAX_ACTIVE_S: add('session_active_bound')
        if not EARLIEST_DAY <= s['day'] <= latest: add('session_day_range')
        if s['day'] not in seen: add('session_day_missing')
        if not s['responses']: add('session_no_responses')
        if s['cached'] > s['input']: add('session_cached')
        if s['reasoning'] > s['output']: add('session_reasoning')
        session_input += s['input']
    if session_input > day_input: add('session_input_total')
    starts, window_input = set(), 0
    earliest = _instant(EARLIEST_DAY + 'T00:00:00Z')
    for w in payload.get('windows', []):
        start = _instant(w['start'])
        if start in starts: add('window_duplicate')
        starts.add(start)
        if start < earliest: add('window_before')
        if start > limit: add('window_future')
        if w['first_pct'] > w['peak_pct']: add('window_first_peak')
        if w['cached'] > w['input']: add('window_cached')
        if w['input'] + w['output'] and not w['responses']: add('window_no_responses')
        pairs = set()
        for s in w.get('split', []):
            pair = s['model'], s['tier']
            if pair in pairs: add('split_duplicate')
            pairs.add(pair)
            if s['cached'] > s['input']: add('split_cached')
            if s.get('cache_write_5m', 0) + s.get('cache_write_1h', 0) > max(0, s['input'] - s['cached']): add('split_cache_write')
            if s['input'] + s['output'] and not s['responses']: add('split_no_responses')
        for k in WINDOW_COUNTS:
            if sum(s[k] for s in w.get('split', [])) > w[k]: add('split_' + k + '_total')
        window_input += w['input']
    if window_input > day_input: add('window_input_total')
    if 'latency' in payload:
        _latency_issues(payload['latency'], day_responses, latest, add)
    return issues


def _latency_issues(l, day_responses, latest, add):
    if l['from'] > l['to']: add('latency_reversed')
    if l['from'] < EARLIEST_DAY: add('latency_before')
    if l['to'] > latest: add('latency_future')
    if (_instant(l['to'] + 'T00:00:00Z') - _instant(l['from'] + 'T00:00:00Z')) / 86400 + 1 > 92: add('latency_span')
    def timing(t, prefix, cap):
        if t['median_s'] > t['p90_s']: add(prefix + '_median')
        if t['p90_s'] > cap: add(prefix + '_p90')
    timing(l['responses'], 'response', 3600)
    if l['responses']['n'] > day_responses: add('response_total')
    if l.get('turns') and l['turns'].get('n') != 0:
        timing(l['turns'], 'turn', 7200)
        if l['turns']['n'] > l['responses']['n']: add('turn_total')
    for key, prefix in (('groups', 'group'), ('tier_groups', 'tier_group')):
        pairs = set()
        for g in l.get(key, []):
            pair = (g['model'], g['effort']) + ((g['tier'],) if key == 'tier_groups' else ())
            if pair in pairs: add(prefix + '_duplicate')
            pairs.add(pair)
            timing(g, prefix, 3600)
        if sum(g['n'] for g in l.get(key, [])) > l['responses']['n']: add(prefix + '_total')
    for key, field in (('utc_hours', 'hour'), ('utc_weekdays', 'day')):
        seen = set()
        for b in l.get(key, []):
            if b[field] in seen: add('clock_duplicate')
            seen.add(b[field])
            if b['median_s'] > 3600: add('clock_median')
        if sum(b['n'] for b in l.get(key, [])) > l['responses']['n']: add('clock_total')


def validate_report(body) -> List[str]:
    issues = []
    def add(code):
        assert code in ISSUES
        if code not in issues:
            issues.append(code)
    if not isinstance(body, dict):
        return ['report_object']
    if not _number(body.get('schema'), integer=True) or body['schema'] != 1: add('report_schema')
    _client(body, add)
    encoded = body.get('html_gz')
    if (not isinstance(encoded, str) or not encoded or len(encoded) > 1200000
            or re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', encoded) is None):
        add('report_base64')
    try:
        if len(payload_bytes(body)) > MAX_BODY_BYTES: add('report_body_budget')
    except (ValueError, TypeError, UnicodeError, OverflowError):
        add('payload_json_invalid')
    if issues:
        return issues
    try:
        gz = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return ['report_base64']
    if len(gz) > MAX_REPORT_GZ_BYTES: add('report_gzip_budget')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(gz)) as stream:
            page = stream.read(MAX_REPORT_BYTES + 1)
        if len(page) > MAX_REPORT_BYTES: add('report_size')
        if not page.lower().startswith(b'<!doctype html>'): add('report_doctype')
    except (OSError, EOFError, ValueError, zlib.error):
        add('report_gzip')
    return issues
