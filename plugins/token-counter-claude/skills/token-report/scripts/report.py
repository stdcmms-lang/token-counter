#!/usr/bin/env python3
"""Claude Code Token Report: captured local usage, timing and offline API list value."""
import argparse
import concurrent.futures as cf
import datetime
import functools
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

if sys.version_info < (3, 8):
    sys.stderr.write('token-counter-claude needs Python 3.8 or newer.\n')
    raise SystemExit(2)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tokencounter import __version__, account, analyze, history, index, ledger, paths, pricing, render, rollout, worker  # noqa: E402
from tokencounter.models import LedgerResult, bump  # noqa: E402

DEFAULT_ENDPOINT = 'https://tokenusage.dev/api'
HISTORY_WARNING = 'History unavailable: showing live captured usage only; sharing is disabled.'


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog='report.py', description=__doc__)
    ap.add_argument('--sessions-root', metavar='PATH')
    ap.add_argument('--since', metavar='YYYY-MM-DD')
    ap.add_argument('--until', metavar='YYYY-MM-DD')
    ap.add_argument('--session', metavar='PREFIX')
    ap.add_argument('--out', metavar='PATH')
    ap.add_argument('--json', metavar='PATH')
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('--style', choices=[s for s, _ in render.STYLES], default='clinical')
    ap.add_argument('--public', action='store_true')
    ap.add_argument('--metrics-only', action='store_true')
    ap.add_argument('--no-account', action='store_true')
    ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--rebuild', action='store_true')
    view = ap.add_mutually_exclusive_group()
    view.add_argument('--include-archived', action='store_true', default=True)
    view.add_argument('--live-only', action='store_true')
    ap.add_argument('--prices', metavar='PATH')
    pool = ap.add_mutually_exclusive_group()
    pool.add_argument('--procs', type=int, metavar='N')
    pool.add_argument('--fast', action='store_true')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--doctor', action='store_true')
    ap.add_argument('--version', action='version', version='token-counter-claude ' + __version__)
    a = ap.parse_args(argv)
    for key in ('since', 'until'):
        value = getattr(a, key)
        if value is not None:
            try:
                if datetime.date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                ap.error('--' + key + ' must be YYYY-MM-DD')
    if a.since and a.until and a.since > a.until:
        ap.error('--since must not be later than --until')
    if a.procs is not None and a.procs < 1:
        ap.error('--procs must be at least one')
    cores = os.cpu_count() or 1
    a.procs = a.procs if a.procs is not None else (cores if a.fast else max(1, cores // 2))
    # CPython's Windows process pool has a hard limit of 61 workers.
    if sys.platform.startswith('win'):
        a.procs = min(a.procs, 61)
    a.include_archived = not a.live_only
    return a


def _progress(options, message):
    if not options.quiet:
        print(message, file=sys.stderr)


def _extract(files, options):
    fn = functools.partial(worker.extract, metrics_only=options.metrics_only)
    results, started = {}, time.perf_counter()
    pooled = options.procs > 1 and len(files) > 1
    if pooled:
        try:
            with cf.ProcessPoolExecutor(max_workers=options.procs) as pool:
                for n, result in enumerate(pool.map(fn, files, chunksize=4), 1):
                    results[result['path']] = result
                    if n % 400 == 0:
                        _progress(options, '  parsed: {:,}/{:,} ({:.0f}s)'.format(
                            n, len(files), time.perf_counter() - started))
        except (OSError, ValueError, ImportError, RuntimeError):
            print('  process pool unavailable; falling back to a single process', file=sys.stderr)
            results.clear()
            pooled = False
    if not pooled:
        for n, path in enumerate(files, 1):
            result = fn(path)
            results[result['path']] = result
            if n % 400 == 0:
                _progress(options, '  parsed: {:,}/{:,} ({:.0f}s)'.format(
                    n, len(files), time.perf_counter() - started))
    if files:
        _progress(options, '  parsed: {:,} files in {:.1f}s'.format(len(files), time.perf_counter() - started))
    return results


def _receipt_binding(resolved):
    """Only endpoint and opaque receipt binding are needed; tokens never enter a model."""
    try:
        with open(resolved['share_state_path'], 'rb') as fh:
            state = json.load(fh)
        endpoint = state.get('endpoint') or state.get('api') or DEFAULT_ENDPOINT
        binding = state.get('token_binding')
        entry = (state.get('endpoints') or {}).get(endpoint) or {}
        binding = entry.get('token_binding', binding)
        return (endpoint if isinstance(endpoint, str) and endpoint else DEFAULT_ENDPOINT,
                binding if isinstance(binding, str) else None)
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_ENDPOINT, None


def collect(options, *, now=None) -> LedgerResult:
    instant = time.time() if now is None else analyze._now(now)
    resolved = paths.resolve_paths(options.sessions_root)
    options._paths, options._timings = resolved, {}
    started = time.perf_counter()
    files, discovery_q = rollout.discover(resolved['sessions_root'])
    options._timings['discover'] = time.perf_counter() - started
    started = time.perf_counter()
    cache, fingerprint, results, pending = None, 0, {}, []
    try:
        if not options.no_cache:
            fingerprint = worker.extractor_fingerprint(metrics_only=options.metrics_only)
            if fingerprint:
                cache, _reason = index.try_open(str(resolved['index_path']))
                if cache is None:
                    print('Index unavailable: extracting live transcripts without it.', file=sys.stderr)
            else:
                print('Extractor fingerprint unavailable: running without the index.', file=sys.stderr)
        if cache is not None and options.rebuild:
            try:
                cache.clear()
            except (sqlite3.Error, OSError):
                cache.close()
                cache = None
                print('Index rebuild unavailable: extracting live transcripts without it.', file=sys.stderr)
        for file in files:
            path = str(file)
            got = None
            if cache is not None:
                try:
                    size, mtime = rollout.stat_key(path)
                    got = cache.fresh(path, size, mtime, fingerprint)
                    if got is not None:
                        cache.touch(path, instant)
                except (sqlite3.Error, OSError):
                    got = None
            if got is None:
                pending.append(path)
            else:
                results[path] = got
        _progress(options, '{:,} transcript files | {:,} cached | {:,} to parse'.format(
            len(files), len(results), len(pending)))
        options._index_hits = len(results)
        results.update(_extract(pending, options))
        if cache is not None:
            try:
                for path in pending:
                    result = results[path]
                    if result['stable_read']:
                        cache.put(result, fingerprint, instant)
                cache.commit()
            except (sqlite3.Error, OSError):
                print('Index update unavailable: the report still uses the extracted facts.', file=sys.stderr)
    finally:
        if cache is not None:
            cache.close()
    options._timings['extract or index load'] = time.perf_counter() - started
    acct = account.read_account(resolved, no_account=options.no_account, now=instant)
    started, store, captured = time.perf_counter(), None, None
    history_q = {}
    try:
        store = history.History(resolved['history_path'])
        captured = store.capture(results, account=acct['snapshot'], now=instant)
        store.commit()
        if captured is not None and store.history_available and store.history_committed:
            # capture() intentionally returns a pre-commit ledger.
            captured['coverage']['history_committed'] = True
            endpoint, binding = _receipt_binding(resolved)
            store.coverage(captured, endpoint=endpoint, token_binding=binding)
            if not store.history_available:
                captured = None
        else:
            captured = None
        history_q.update(store.counters)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RuntimeError):
        captured = None
        bump(history_q, 'history_unavailable')
    finally:
        if store is not None:
            history_q.update(store.counters)
            store.close()
    options._timings['capture + commit'] = time.perf_counter() - started
    if captured is None:
        print(HISTORY_WARNING, file=sys.stderr)
        started = time.perf_counter()
        captured = ledger.build(results, account_snapshots=([acct['snapshot']] if acct['snapshot'] else []), now=instant)
        options._timings['build'] = time.perf_counter() - started
        options._history_failed = True
    else:
        options._timings['build'] = 0.0  # canonical ledger construction is inside capture()
        options._history_failed = False
    for counters in (discovery_q, acct['counters'], history_q, worker.FINGERPRINT_COUNTERS):
        for name, count in counters.items():
            # History's ledger already contains its diagnostics; do not double them.
            captured['counters'][name] = max(captured['counters'].get(name, 0), count)
    captured['account'] = {'available': acct['snapshot'] is not None, 'email': acct['email']}
    return captured


def _session(built, options):
    prefix = options.session
    if not prefix:
        return None
    candidates = set()
    for info in built.get('_source_families', {}).values():
        family = info['family_id']
        if family is not None and any((info.get(k) or '').startswith(prefix)
                                     for k in ('family_id', 'session_id', 'stream_id')):
            candidates.add(family)
    eligible = {r['family_id'] for r in built['rows'] if analyze._selected(r, options.since, options.until)
                and (not options.live_only or not r.get('archived'))}
    # A quota-only or refusal-only family is still a reportable family.
    for kind, live_key, fact_key in (('limits', '_live_limit_keys', 'reading_key'),
                                     ('events', '_live_event_keys', 'event_key')):
        for fact in built[kind]:
            if (analyze._selected(fact, options.since, options.until)
                    and (not options.live_only or fact[fact_key] in built.get(live_key, {}))):
                info = built.get('_source_families', {}).get(fact['source_id'], {})
                eligible.add(info.get('family_id'))
    candidates &= eligible
    if len(candidates) != 1:
        message = ('Ambiguous session prefix: ' + ', '.join(sorted(candidates)) if candidates
                   else 'No session family matches the requested prefix.')
        raise ValueError(message)
    return next(iter(candidates))


def build_report(options, *, now=None) -> tuple:
    instant = time.time() if now is None else analyze._now(now)
    built = collect(options, now=instant)
    session = _session(built, options)
    table = pricing.load(options.prices)
    if table[0] is None:
        print('API list value unavailable: ' + (table[1] or 'price_table_unavailable'), file=sys.stderr)
    started = time.perf_counter()
    model = analyze.analyze(built, scope={'label': 'captured local usage'}, since=options.since,
        until=options.until, session=session, live_only=options.live_only or options._history_failed,
        metrics_only=options.metrics_only, prices=table, now=instant)
    options._timings['analyze'] = time.perf_counter() - started
    if options.public:
        model = analyze.public_model(model)
    started = time.perf_counter()
    page = render.render(model, options.public, options.style, profile=analyze.claude_profile(model))
    options._timings['render'] = time.perf_counter() - started
    _progress(options, '  ledger + analysis in {:.1f}s'.format(
        sum(options._timings[k] for k in ('capture + commit', 'build', 'analyze'))))
    return model, page


def terminal_summary(model) -> str:
    t, lines = model['totals'], []
    hit = '--' if t['cache_hit'] is None else '{:.1f}%'.format(100*t['cache_hit'])
    lines.append("{responses:,} responses | {input:.3f}B recorded input | {uncached:.0f}M uncached | {hit} cached | {output:.0f}M output".format(
        responses=t['responses'], input=t['input']/1e9, uncached=t['uncached']/1e6, hit=hit, output=t['output']/1e6))
    lines.append('  recorded input: {base_input:,} base input + {cache_creation:,} cache writes + {cached:,} cache reads'.format(**t))
    lines.append('  output: {:,} recorded thinking tokens · unavailable for {:,} responses'.format(t['reasoning'], t['thinking_unavailable']))
    lines.append('  cache: {cached:,} read · {cache_write_5m:,} 5m writes · {cache_write_1h:,} 1h writes · {cache_write_unknown:,} writes with unknown TTL'.format(**t))
    av = model['api_value']
    if av['available']:
        lines.append('API list value ${:,.6f} at Anthropic list prices of {} -- recorded usage priced, not a bill'.format(av['usd'], av.get('as_of') or '?'))
        lines.append('  {:,} of {:,} responses priced | {:,} unpriced | {:,} web searches ${:,.6f}'.format(
            av['priced'], av['responses'], av['unpriced'], av['web_search_calls'], av['web_search_usd']))
    else:
        lines.append('API list value not available -- ' + (av.get('reason') or 'no price table'))
    for name in sorted(pricing.ASSUMPTIONS + pricing.EXCLUSIONS):
        if av.get(name):
            lines.append('  {}: {:,}'.format(name, av[name]))
    for why, count in sorted(av.get('unpriced_by_reason', {}).items()):
        lines.append('  unpriced {}: {:,}'.format(why, count))
    lat = model['latency']
    if lat['available']:
        r = lat['responses']
        sec = lambda v: render.secs(v).replace('&mdash;', '--')
        lines.append('response time {} median, {} p90 over {:,} responses'.format(sec(r['median_s']), sec(r['p90_s']), r['n']))
    ev = model['limit_events']
    if ev['total']:
        lines.append('rate-limit events {:,} on {:,} days'.format(ev['total'], len(ev['daily'])))
    who = model.get('account', {}).get('email')
    if who:
        lines.append(who)
    rl = model['rate_limits']
    cur = rl.get('current')
    if cur:
        if cur['expired']:
            lines.append('Reset since the last weekly reading; no current percentage recorded.')
        else:
            value = '--' if cur['last_pct'] is None else '{:g}%'.format(cur['last_pct'])
            lines.append('weekly all-model limit {} used | last recorded weekly reading | next {}'.format(value, cur['resets_at_iso']))
    else:
        lines.append('No weekly all-model readings in range.' if rl.get('available') else 'No structured limit readings in range.')
    if lat.get('plan') or rl.get('plans'):
        lines.append('captured plans: ' + ', '.join(sorted(set(rl.get('plans', {})) | ({lat['plan']} if lat.get('plan') else set()))))
    for name, count in sorted(model['quality'].items()):
        lines.append('{}: {:,}'.format(name, count))
    archived = model['coverage'].get('archived_responses', 0)
    if archived:
        lines.append('captured history: {} responses from transcripts no longer on disk'.format(archived))
    return '\n'.join(lines)


def doctor(options) -> int:
    resolved = paths.resolve_paths(options.sessions_root)
    okay = resolved['sessions_root'].is_dir()
    print('token-counter-claude ' + __version__)
    for key, path in resolved.items():
        if key != 'explicit_sessions_root':
            print('{}: {}'.format(key, path))
    files, counters = rollout.discover(resolved['sessions_root'])
    print('corpus: {:,} transcript files'.format(len(files)))
    for name, value in sorted(counters.items()):
        print('{}: {:,}'.format(name, value))
    for key, table, schema in (('index_path', 'files', index.SCHEMA_VERSION),
                               ('history_path', 'sources', history.SCHEMA_VERSION)):
        path = resolved[key]
        if not path.exists():
            print('{}: absent'.format(key.replace('_path', ' state')))
            continue
        db = None
        try:
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            valid = db.execute('PRAGMA quick_check').fetchone() == ('ok',)
            meta = dict(db.execute('SELECT k,v FROM meta'))
            valid = valid and meta.get('schema') == str(schema)
            count = db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            print('{}: {} · {:,} {}'.format(key.replace('_path', ' state'), 'okay' if valid else 'unavailable', count, table))
            okay = okay and valid
        except (sqlite3.Error, OSError, ValueError):
            print('{}: unavailable'.format(key.replace('_path', ' state')))
            okay = False
        finally:
            if db is not None:
                db.close()
    table, why = pricing.load(options.prices)
    print('price table: ' + ('okay · ' + table['as_of'] if table else (why or 'unavailable')))
    okay = okay and table is not None
    acct = account.read_account(resolved, no_account=options.no_account)
    print('account: ' + ('skipped' if options.no_account else 'available' if acct['snapshot'] else 'unavailable'))
    return 0 if okay else 1


def _open(path):
    try:
        if sys.platform.startswith('win'):
            os.startfile(str(path))
        else:
            subprocess.run(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)], check=False)
    except OSError:
        print('Could not open the report automatically.', file=sys.stderr)


def main(argv=None) -> int:
    options = parse_args(argv)
    if options.doctor:
        return doctor(options)
    try:
        model, page = build_report(options)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out = Path(options.out) if options.out else options._paths['report_path']
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding='utf-8')
        if options.json:
            if options.json == '-':
                json.dump(model, sys.stdout, ensure_ascii=False, indent=1)
                print()
            else:
                target = Path(options.json)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(model, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    except (OSError, UnicodeError):
        print('Could not write the requested report output.', file=sys.stderr)
        return 5
    if options.json != '-':
        print(terminal_summary(model))
        if not options.public:
            print(out)
    if not options.no_open:
        _open(out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
