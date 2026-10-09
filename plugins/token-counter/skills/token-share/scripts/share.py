#!/usr/bin/env python3
"""Share Codex token usage with the tokenusage.dev leaderboard.

    share.py                          # dry run: show what would be sent, send nothing
    share.py --handle NAME --yes      # first share: claim NAME on the leaderboard
    share.py --yes                    # later shares: update the numbers
    share.py --out payload.json       # write the exact payload to a file, send nothing
    share.py --delete-report --yes    # take the report page down, keep the numbers
    share.py --delete --yes           # remove everything shared, and forget the token

The token-report skill sends nothing anywhere; its one network call installs tiktoken from
PyPI when it is missing. This script is the one thing that sends, and it sends only when
run with --yes. What it sends is daily token counts, a handful of
per-session summaries -- counts, times and a model name -- and, per weekly rate-limit window,
the plan and percentage the server reported beside the tokens counted in it, over the last
month the median and p90 response and turn time, and what the usage would cost at OpenAI's API
list prices, per day and per session. Beside the
numbers it publishes the token-report page itself, rendered for the public, so the link it
prints opens the same page the user has locally. Never prompts, file contents, paths, session
titles, or anything from auth.json. See the SKILL.md next to this file.
"""
import argparse
import base64
import collections
import contextlib
import datetime
import gzip
import hashlib
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.normpath(os.path.join(HERE, '..', '..', 'token-report', 'scripts'))
sys.path.insert(0, REPORT)

import report as reportcli  # noqa: E402  -- enforces the Python floor on import
from tokencounter import analyze, latency, ledger, pricing, render, rollout, worker  # noqa: E402

CLIENT = {'name': 'token-counter', 'version': '1.9.1'}
SCHEMA = 1
DEFAULT_API = 'https://tokenusage.dev/api'

# A gap between two responses longer than this is time away, not time in the session, and
# counts for nothing. Responses are stamped when they complete, so the gaps between them
# include the model working, tools running and the user reading and typing; half an hour
# covers a long build or test run without crediting a lunch break.
IDLE_CAP_S = 30 * 60

# Per month, the sessions sent are the top few by active time and by tokens: enough for the
# server to pick each month's record holders, without shipping every session ever run.
SESSIONS_PER_MONTH = 10

# Weekly rate-limit windows sent, newest kept: two years of weeks. The server's per-plan
# estimate only reads recent ones; older windows are history, not evidence of today's limits.
WINDOWS_MAX = 104

# The server refuses anything dated before Codex shipped. The oldest window's start can be
# inferred from its quoted reset, seven days before it, so it could land earlier than the
# logs do; such a window is dropped here rather than failing the whole share.
EARLIEST_START = '2025-04-01T00:00:00Z'

# Response times are sent for the last month of responses, with at most this many model and
# effort groups: the server refuses a span over 92 days and more than 50 groups.
LATENCY_DAYS = 30
LATENCY_GROUPS_MAX = 50


def _iso(epoch_s):
    return datetime.datetime.fromtimestamp(epoch_s, datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def session_hash(sid):
    """Stable, one-way id for a session: the leaderboard can tell sessions apart, and no one
    can map one back to a rollout file."""
    return hashlib.sha256(('tokenusage.dev:' + str(sid)).encode('utf-8')).hexdigest()[:16]


def _pct(v):
    """A reported percentage, as sent: within 0..100, two decimals, or None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return round(min(100.0, max(0.0, float(v))), 2)


def limit_windows(results, responses, now_s):
    """Each consumed weekly rate-limit window, oldest first: the plan and the percentages the
    server reported for it, beside the tokens measured here in the same span.

    The two series are sent side by side and never combined, as in the report (ARCHITECTURE
    §5.6): this script asserts no tokens-per-percent rate. What tokenusage.dev derives from
    many sharers' windows is its own, and it documents how.

    `responses` is ``[(epoch, input, cached, output), ...]``, the same charged, clamped rows
    the days are counted from, so a window can never hold more than the days do.
    """
    rl = analyze.rate_limit_windows(results, responses, now=now_s, newest=None)
    if not rl.get('available') or not rl.get('weekly'):
        return []
    out = []
    starts = set()
    for w in rl['windows']:
        first, peak = _pct(w.get('first_pct')), _pct(w.get('peak_pct'))
        if w.get('reset_at') is None or first is None or not peak:
            continue
        start = _iso(w['reset_at'])
        # Two windows first seen in the same second would be refused as duplicates.
        if start < EARLIEST_START or start in starts:
            continue
        starts.add(start)
        plan = w.get('plan_type')
        plan = plan.strip().lower()[:40] if isinstance(plan, str) else ''
        t = w['tokens']
        out.append({
            'start': start,
            'window_minutes': rl['window_minutes'],
            'plan': plan or None,
            'first_pct': first,
            'peak_pct': peak,
            'responses': t['responses'],
            'input': t['input'],
            'cached': t['cached'],
            'output': t['output'],
        })
    return out[-WINDOWS_MAX:]


def latency_summary(results, charged, now_s):
    """Response and turn times over the last LATENCY_DAYS days, in the shape the server's
    `latency` takes (docs/share-protocol.md in tokenusage.dev), or None when nothing was timed.

    Timed by token-report's own `latency.build`, so the figures are the report's. Only a
    day-level span and aggregates leave: no tool names, and no UTC hour or weekday buckets,
    which the server leaves optional because they say when a sharer works.
    """
    # Every row goes in, and `since` leaves the older ones out of the figures: the first
    # recent response in a file is still floored by the end of the one before it.
    lat, _ = latency.build(results, charged, since=now_s - LATENCY_DAYS * 86400)
    if not lat.get('available') or not lat.get('daily'):
        return None
    dates = [d['date'] for d in lat['daily']]
    turns = lat['turns']
    groups = []
    for g in lat['groups'][:LATENCY_GROUPS_MAX]:
        fit = g.get('fit') or {}
        groups.append({
            'model': (g['model'] or 'unknown')[:80],
            'effort': (g['effort'] or 'unknown')[:40],
            'n': g['n'],
            'median_s': g['median_s'],
            'p90_s': g['p90_s'],
            'overhead_s': fit.get('overhead_s'),
            'output_tps': fit.get('output_tps'),
            'above_share': g.get('above_share'),
        })
    r = lat['responses']
    return {
        'from': min(dates),
        'to': max(dates),
        'responses': {'n': r['n'], 'median_s': r['median_s'], 'p90_s': r['p90_s']},
        'turns': None if not turns['n'] else {
            'n': turns['n'], 'median_s': turns['median_s'], 'p90_s': turns['p90_s'],
            'model_share': turns['model_share']},
        'groups': groups,
    }


def api_value_summary(api, results):
    """The payload's `api_value`: the total the days add up to, and what it covers. None
    when there is no price table, so a share without one sends nothing under the name."""
    if not api.on:
        return None
    m = api.model(results)
    return {
        'usd': _usd(m['usd']),
        'usd_high': _usd(m['usd_high']),
        'tokens_usd': _usd(m['tokens_usd']),
        'web_search_usd': _usd(m['web_search_usd']),
        'web_search_calls': m['web_search_calls'],
        'prices_as_of': m['prices']['as_of'],
        'prices_source': m['prices']['source'],
        'prices_default': m['prices']['default'],
        'responses': m['responses'],
        'priced': m['priced'],
        'unpriced': m['unpriced'],
        'tier_unrecorded': m['tier_unrecorded'],
        'tier_inferred': m['tier_inferred'],
        'tiers': m['tiers'],
        'aborted_turns': m['aborted_turns'],
    }


def active_seconds(epochs):
    """Time between consecutive responses, skipping gaps longer than IDLE_CAP_S."""
    total = 0.0
    for a, b in zip(epochs, epochs[1:]):
        gap = b - a
        if 0 < gap <= IDLE_CAP_S:
            total += gap
    return int(round(total))


def _usd(x):
    return None if x is None else round(x, 4)


def build_payload(results, charged, handle=None, now=None, prices=None):
    """The share payload from extracted files and their charged ledger rows.

    Returns ``(payload, notes)``. Everything is counted from the canonical ledger, exactly as
    the report counts it, so the leaderboard and the local report agree on every day.
    Days are the sharer's **local** calendar days, as in the report.

    The API value -- `api_usd` on each day and session, and `api_value` beside them -- is
    priced exactly as the report prices it (`analyze.ApiValue`), from the vendored table
    unless `prices` (what `pricing.load` returns) says otherwise. tokenusage.dev does not
    read these fields yet; its schema strips keys it does not know, so they cost a share
    nothing until it does. A day or session with nothing valued -- no priced response and
    no search fee -- sends null, not a 0 that reads as free.
    """
    notes = collections.Counter()
    days = collections.defaultdict(collections.Counter)
    sessions = {}
    timeline = []
    api = analyze.ApiValue(pricing.load() if prices is None else prices)
    day_usd = collections.defaultdict(float)
    day_valued = collections.Counter()

    for path, fr in sorted(results.items()):
        rows = charged.get(path) or []
        sid = fr.get('session_id') or path
        s = sessions.setdefault(sid, {'rows': [], 'counts': collections.Counter(),
                                      'models': collections.Counter(), 'usd': 0.0,
                                      'valued': 0})
        for r in rows:
            u = r.get('usage') or {}
            inp = int(u.get('input_tokens') or 0)
            cch = int(u.get('cached_input_tokens') or 0)
            out = int(u.get('output_tokens') or 0)
            rsn = int(u.get('reasoning_output_tokens') or 0)
            # The server rejects arithmetic no log should produce. A damaged record is not a
            # reason to refuse the whole share, so it is clamped here and counted.
            if cch > inp:
                cch = inp
                notes['clamped_cached'] += 1
            if rsn > out:
                rsn = out
                notes['clamped_reasoning'] += 1
            ts = r.get('ts')
            d = analyze._day(ts, fr.get('date'))
            if not d:
                notes['undated_responses'] += 1
                continue
            e = worker.epoch(ts)
            timeline.append((e, inp, cch, out))
            before = api.priced
            usd = api.add(r)
            # Valued: priced, or carrying a search fee. A day with neither sends null.
            if usd is not None and (usd or api.priced > before):
                day_usd[d] += usd
                s['usd'] += usd
                day_valued[d] += 1
                s['valued'] += 1
            dd = days[d]
            dd['responses'] += 1
            dd['input'] += inp
            dd['cached'] += cch
            dd['output'] += out
            dd['reasoning'] += rsn
            c = s['counts']
            c['responses'] += 1
            c['input'] += inp
            c['cached'] += cch
            c['output'] += out
            c['reasoning'] += rsn
            s['models'][r.get('model') or 'unknown'] += 1
            s['rows'].append((e, d))

    summaries = []
    for sid, s in sessions.items():
        timed = sorted((e, d) for e, d in s['rows'] if e is not None)
        if not s['rows']:
            continue
        # The session's day is the local day of its first response, the same bucket that
        # response was counted in above.
        first_day = timed[0][1] if timed else min(d for _, d in s['rows'])
        days[first_day]['sessions'] += 1
        if not timed:
            notes['untimed_sessions'] += 1
            continue
        epochs = [e for e, _ in timed]
        c = s['counts']
        model = s['models'].most_common(1)[0][0] if s['models'] else None
        summaries.append({
            'id': session_hash(sid),
            'day': first_day,
            'start': _iso(epochs[0]),
            'end': _iso(epochs[-1]),
            'active_s': active_seconds(epochs),
            'responses': c['responses'],
            'input': c['input'],
            'cached': c['cached'],
            'output': c['output'],
            'reasoning': c['reasoning'],
            'model': None if model == 'unknown' else model[:80],
            'api_usd': _usd(s['usd']) if api.on and s['valued'] else None,
        })

    by_month = collections.defaultdict(list)
    for x in summaries:
        by_month[x['day'][:7]].append(x)
    keep = {}
    for group in by_month.values():
        for key in (lambda x: (x['active_s'], x['input'] + x['output']),
                    lambda x: (x['input'] + x['output'], x['active_s'])):
            for x in sorted(group, key=key, reverse=True)[:SESSIONS_PER_MONTH]:
                keep[x['id']] = x
    notes['sessions_total'] = len(summaries)

    now_s = (now or datetime.datetime.now(datetime.timezone.utc)).timestamp()
    payload = {
        'schema': SCHEMA,
        'client': dict(CLIENT),
        'generated_at': _iso(now_s),
        'days': [dict(date=d, responses=v['responses'], input=v['input'], cached=v['cached'],
                      output=v['output'], reasoning=v['reasoning'], sessions=v['sessions'],
                      api_usd=_usd(day_usd[d]) if api.on and day_valued[d] else None)
                 for d, v in sorted(days.items()) if v['responses']],
        'sessions': sorted(keep.values(), key=lambda x: (x['start'], x['id'])),
        'windows': limit_windows(results, timeline, now_s),
    }
    lat = latency_summary(results, charged, now_s)
    if lat:
        payload['latency'] = lat
    av = api_value_summary(api, results)
    if av:
        payload['api_value'] = av
    if handle:
        payload['handle'] = handle
    return payload, notes


# ------------------------------------------------------------------------ the report page

def report_path():
    return os.path.join(reportcli.out_dir(), 'report-shared.html')


def build_report(a):
    """Render the page that is published at /r/<handle>: token-report's own page over every
    session, with --public (no session id, no directory name, auth.json unread).

    It is written to disk first, dry run or not, so the user can open exactly what would go
    public. Returns ``(path, None)`` or ``(None, why)``.
    """
    out = report_path()
    # --no-install: the dry run usually has no network and `--yes` does, so letting the
    # report install tiktoken here would publish a page with a panel the dry run lacked.
    # A tiktoken that token-report already installed is still used.
    argv = ['--public', '--no-open', '--no-install', '--out', out]
    if a.style:
        argv += ['--style', a.style]
    if a.sessions_root:
        argv += ['--sessions-root', a.sessions_root]
    if a.fast:
        argv.append('--fast')
    if a.procs:
        argv += ['--procs', str(a.procs)]
    if a.quiet:
        argv.append('--quiet')
    try:
        # The report prints its summary and the path on stdout; this script's stdout is the
        # share summary, so that goes nowhere. Progress and warnings still reach stderr.
        with contextlib.redirect_stdout(io.StringIO()):
            code = reportcli.main(argv)
    except (FileNotFoundError, ValueError, ImportError, OSError) as exc:
        return None, str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
    if code != 0 or not os.path.exists(out):
        return None, f'token-report exited with {code}'
    return out, None


def report_body(path):
    with open(path, 'rb') as fh:
        page = fh.read()
    return {'schema': SCHEMA, 'client': dict(CLIENT),
            'html_gz': base64.b64encode(gzip.compress(page, 9)).decode('ascii')}


# ------------------------------------------------------------------------ local state

def state_path():
    return os.path.join(reportcli.out_dir(), 'share.json')


def load_state():
    try:
        with open(state_path(), encoding='utf-8') as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    """The token is a credential: written 0600, and never printed.

    Through a new file of its own (`mkstemp` creates it exclusively, 0600), never a fixed
    temporary name: opening one of those follows whatever symlink is already there.
    """
    p = state_path()
    fd, tmp = tempfile.mkstemp(prefix='.share-', suffix='.tmp', dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def endpoint_state(state, api):
    return (state.get('endpoints') or {}).get(api) or {}


def set_endpoint_state(state, api, entry):
    eps = state.setdefault('endpoints', {})
    if entry is None:
        eps.pop(api, None)
    else:
        eps[api] = entry


# ------------------------------------------------------------------------ transport

def request(method, url, body=None, token=None, timeout=60):
    """``(status, parsed_json_or_None)``. Network failures raise OSError."""
    data = None if body is None else json.dumps(body, separators=(',', ':')).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('User-Agent', f"{CLIENT['name']}/{CLIENT['version']}")
    req.add_header('Accept', 'application/json')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        return status, json.loads(raw.decode('utf-8')) if raw else None
    except ValueError:
        return status, None


def _explain(status, body):
    body = body if isinstance(body, dict) else {}
    msg = body.get('message') or f'HTTP {status}'
    lines = [f'the server refused it ({status}): {msg}']
    for issue in (body.get('issues') or [])[:10]:
        lines.append(f'  - {issue}')
    return '\n'.join(lines)


# ------------------------------------------------------------------------ output

def _n(v):
    for unit, size in (('B', 1e9), ('M', 1e6), ('K', 1e3)):
        if v >= size:
            return f'{v / size:.2f}{unit}'
    return str(v)


def _dur(s):
    h, m = divmod(int(s) // 60, 60)
    return f'{h}h {m:02d}m' if h else f'{m}m'


def _describe_windows(windows):
    if not windows:
        return 'limits       no weekly rate-limit windows in the logs'
    plans = collections.Counter(w['plan'] or 'unknown' for w in windows)
    latest = windows[-1]
    return (f"limits       {len(windows):,} weekly windows, "
            + ', '.join(f'{n} on {p}' for p, n in plans.most_common())
            + f"; latest {latest['start'][:10]} at {latest['peak_pct']:g}% used")


def _describe_latency(lat):
    if not lat:
        return 'latency      no response in the last month could be timed'
    r = lat['responses']
    return (f"latency      {r['n']:,} timed responses, {lat['from']} .. {lat['to']}: median "
            f"{r['median_s']:g}s, p90 {r['p90_s']:g}s; {len(lat['groups'])} model/effort groups")


def _describe_api_value(av):
    if not av:
        return 'api value    not computed (no price table)'
    return (f"api value    ${av['usd']:,.2f} at OpenAI API list prices of {av['prices_as_of']} "
            f"({av['priced']:,} of {av['responses']:,} responses priced); "
            f"tokenusage.dev does not show it yet")


def describe(payload, notes, handle, api):
    """What will be sent, in words: printed before anything leaves the machine."""
    days = payload['days']
    months = collections.OrderedDict()
    for d in days:
        m = months.setdefault(d['date'][:7], collections.Counter())
        for k in ('input', 'output', 'responses', 'sessions'):
            m[k] += d[k]
    tot_in = sum(d['input'] for d in days)
    tot_out = sum(d['output'] for d in days)
    lines = [
        f"sharing as   {handle or '(no handle yet)'} -> {api}",
        f"covers       {len(days):,} active days"
        + (f", {days[0]['date']} .. {days[-1]['date']}" if days else ''),
        f"tokens       {_n(tot_in + tot_out)} total ({_n(tot_in)} recorded input, "
        f"{_n(tot_out)} output)",
        f"sessions     {len(payload['sessions']):,} summarised of {notes['sessions_total']:,}"
        f" (per month, the top {SESSIONS_PER_MONTH} by active time and by tokens)",
        _describe_windows(payload.get('windows') or []),
        _describe_latency(payload.get('latency')),
        _describe_api_value(payload.get('api_value')),
        '',
        'month        tokens      sessions  longest session',
    ]
    longest = {}
    for s in payload['sessions']:
        k = s['day'][:7]
        if s['active_s'] > longest.get(k, -1):
            longest[k] = s['active_s']
    for k, m in reversed(months.items()):
        lines.append(f"{k}      {_n(m['input'] + m['output']):>9}  {m['sessions']:>8,}  "
                     f"{_dur(longest[k]) if k in longest else '--'}")
    lines += [
        '',
        'sent: per-day token counts; per-session start/end times, active time, token counts',
        'and model name, under a one-way hash of the session id; and per weekly limit window,',
        'its start, the plan and percentage used that Codex logged, and the tokens counted in it;',
        'and, over the last month, the median and p90 response and turn time, in total and per model;',
        'and the API list-price value of the usage, per day, per session and in total.',
        'never sent: prompts, outputs, file contents or paths, session titles, your',
        'account or email.',
    ]
    for k, label in (('clamped_cached', 'responses had cached > input (clamped)'),
                     ('clamped_reasoning', 'responses had reasoning > output (clamped)'),
                     ('undated_responses', 'responses had no usable timestamp (skipped)'),
                     ('untimed_sessions', 'sessions had no usable timestamps (not summarised)')):
        if notes.get(k):
            lines.append(f'note: {notes[k]:,} {label}')
    return '\n'.join(lines)


# ------------------------------------------------------------------------ main

def collect_payload(a, handle):
    files = rollout.discover(a.sessions_root)
    if not files:
        root = rollout.sessions_root(a.sessions_root)
        print(f'No rollout files found under {root}\n'
              f'  CODEX_HOME={os.environ.get("CODEX_HOME") or "(unset)"}\n'
              f'  Point at another location with --sessions-root PATH.', file=sys.stderr)
        return None
    cores = os.cpu_count() or 4
    procs = a.procs or (cores if a.fast else max(1, min(8, cores // 2)))
    # The usage ledger alone: nothing is tokenized, and the report's index is neither read
    # nor written, so this cannot disturb it.
    results, _ = reportcli.collect(files, procs, 1, None, None, a.quiet, True, 0)
    charged, _ = ledger.build(results)
    return build_payload(results, charged, handle=handle)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='share.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--handle', help='leaderboard name: 3-24 of a-z 0-9 -; required the first '
                                     'time, renames you if given later')
    ap.add_argument('--yes', action='store_true', help='actually send (default: dry run)')
    ap.add_argument('--out', metavar='PATH', help='write the payload as JSON, send nothing')
    ap.add_argument('--delete', action='store_true',
                    help='with --yes: delete everything shared from this machine\'s token')
    ap.add_argument('--no-report', action='store_true',
                    help='share the numbers only, not the report page')
    ap.add_argument('--style', choices=[sid for sid, _ in render.STYLES],
                    help='the style the shared report opens in (default: '
                         f'{render.STYLES[0][0]}); readers can still switch')
    ap.add_argument('--delete-report', action='store_true',
                    help='with --yes: take the report page down, keep the shared numbers')
    ap.add_argument('--forget', action='store_true',
                    help='drop the locally stored token without contacting the server')
    ap.add_argument('--api', default=os.environ.get('TOKENUSAGE_API') or DEFAULT_API,
                    help=f'API base URL (default {DEFAULT_API}, or $TOKENUSAGE_API)')
    ap.add_argument('--sessions-root', metavar='PATH', help='override ~/.codex/sessions')
    ap.add_argument('--fast', action='store_true', help='use every core instead of half')
    ap.add_argument('--procs', type=int, default=None, help='worker processes')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)
    api = a.api.rstrip('/')

    state = load_state()
    mine = endpoint_state(state, api)

    if a.forget:
        set_endpoint_state(state, api, None)
        save_state(state)
        print(f"forgot the token for {api}"
              + (f" (handle {mine['handle']})" if mine.get('handle') else ''))
        return 0

    if a.delete_report:
        if not mine.get('token'):
            print(f'nothing to delete: no share token for {api} on this machine', file=sys.stderr)
            return 2
        if not a.yes:
            print(f"would take down the report page of {mine.get('handle')!r} on {api}, and keep "
                  f"the shared numbers.\nre-run with --delete-report --yes to do it.")
            return 0
        try:
            status, body = request('DELETE', f'{api}/report', token=mine['token'])
        except OSError as exc:
            print(f'could not reach {api}: {exc}', file=sys.stderr)
            return 6
        if status != 200:
            print(_explain(status, body), file=sys.stderr)
            return 7
        mine.pop('report_url', None)
        set_endpoint_state(state, api, mine)
        save_state(state)
        print(f"took down the report page of {mine.get('handle')!r}"
              + ('' if isinstance(body, dict) and body.get('deleted') else ' (there was none)'))
        return 0

    if a.delete:
        if not mine.get('token'):
            print(f'nothing to delete: no share token for {api} on this machine', file=sys.stderr)
            return 2
        if not a.yes:
            print(f"would delete {mine.get('handle')!r} and everything it shared from {api}.\n"
                  f"re-run with --delete --yes to do it.")
            return 0
        try:
            status, body = request('DELETE', f'{api}/share', token=mine['token'])
        except OSError as exc:
            print(f'could not reach {api}: {exc}', file=sys.stderr)
            return 6
        if status != 200:
            print(_explain(status, body), file=sys.stderr)
            return 7
        set_endpoint_state(state, api, None)
        save_state(state)
        print(f"deleted {mine.get('handle')!r} from {api}")
        return 0

    handle = a.handle.strip().lower() if a.handle else None
    if not mine.get('token') and not handle and not a.out:
        # A dry run is still useful without one; only sending needs it.
        if a.yes:
            print('the first share needs a leaderboard name: add --handle NAME '
                  '(3-24 characters: a-z, 0-9, hyphens)', file=sys.stderr)
            return 2
    built = collect_payload(a, handle if handle != mine.get('handle') else None)
    if built is None:
        return 2
    payload, notes = built
    if not payload['days']:
        print('no recorded usage to share', file=sys.stderr)
        return 2

    shown = handle or mine.get('handle')
    print(describe(payload, notes, shown, api))

    page = None
    if not a.no_report and not a.out:
        page, why = build_report(a)
        if page:
            print(f'\nreport page  {page} ({os.path.getsize(page) / 1e3:,.0f} KB)\n'
                  f'             published as is: anyone with the link sees this page. Open it '
                  f'to check.\n             it holds the charts\' data: daily input by model, '
                  f'the weekly limit\n             curves over time, and content composition by '
                  f'category; and median and\n             p90 response time and '
                  f'rate-limit events by day. --no-report leaves it out.')
        else:
            print(f'\nreport page  could not be built ({why}); the numbers can still be shared',
                  file=sys.stderr)

    if a.out:
        with open(a.out, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=1)
        print(f'\nwrote the payload to {a.out}; nothing was sent')
        return 0
    if not a.yes:
        print('\ndry run: nothing was sent. re-run with --yes to share'
              + ('' if mine.get('token') or handle else ' (and --handle NAME the first time)')
              + '.')
        return 0

    try:
        status, body = request('POST', f'{api}/share', body=payload, token=mine.get('token'))
    except OSError as exc:
        print(f'\ncould not reach {api}: {exc}\n'
              f'  inside the Codex sandbox, the network is off unless you approve it for '
              f'this command.', file=sys.stderr)
        return 6
    if status not in (200, 201) or not isinstance(body, dict):
        print('\n' + _explain(status, body), file=sys.stderr)
        if status == 401:
            print('  the stored token is not accepted. `--forget` drops it; the next share '
                  'then registers a new handle.', file=sys.stderr)
        return 7

    entry = dict(mine)
    entry.update(handle=body.get('handle'), url=body.get('url'),
                 last_shared_at=payload['generated_at'])
    if body.get('token'):
        entry['token'] = body['token']
    set_endpoint_state(state, api, entry)
    save_state(state)
    verb = 'shared' if body.get('created') else 'updated'
    print(f"\n{verb}: {body.get('url')}")
    if not page:
        return 0

    # The numbers are in; the page goes up under the same token. A failure here leaves the
    # share as it is and says so, rather than pretending the link exists.
    try:
        status, rbody = request('PUT', f'{api}/report', body=report_body(page), token=entry['token'])
    except OSError as exc:
        print(f'report page not published: could not reach {api}: {exc}', file=sys.stderr)
        return 8
    if status != 200 or not isinstance(rbody, dict) or not rbody.get('url'):
        print('report page not published: ' + _explain(status, rbody), file=sys.stderr)
        return 8
    entry['report_url'] = rbody['url']
    set_endpoint_state(state, api, entry)
    save_state(state)
    print(f"report: {rbody['url']}")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\ninterrupted', file=sys.stderr)
        sys.exit(130)
