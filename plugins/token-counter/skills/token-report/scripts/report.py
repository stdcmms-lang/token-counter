#!/usr/bin/env python3
"""Codex token counter -- build and open the local HTML report.

    report.py                          # all sessions
    report.py --since 2026-09-01       # windowed
    report.py --session <id-or-prefix> # single-session deep dive
    report.py --fast                   # every core instead of half
    report.py --json out.json          # machine-readable model, no HTML
    report.py --prices table.json      # price the usage with another table

Reads ~/.codex/sessions/**/rollout-*.jsonl, and ~/.codex/auth.json for the account name.
The API value is priced from a vendored table of OpenAI's list prices, never fetched.
No daemon, no interception, and no network -- except, once, to install tiktoken from PyPI
when it is missing (--no-install turns that off).  See ARCHITECTURE.md.
"""
import argparse
import collections
import concurrent.futures as cf
import functools
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

MIN_PY = (3, 8)
if sys.version_info < MIN_PY:
    sys.stderr.write(
        'token-counter needs Python %d.%d or newer; this is %s.\n'
        % (MIN_PY[0], MIN_PY[1], '.'.join(str(n) for n in sys.version_info[:3])))
    raise SystemExit(2)
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tokencounter import account as accountlib  # noqa: E402
from tokencounter import analyze, deps, index, ledger, pricing, render, rollout, worker  # noqa: E402

# Fixed input for the tokenizer fingerprint in `_extractor_version`.  Exercises the parts of
# the split pattern most likely to differ between implementations: contractions, CJK, an
# astral-plane character, digit runs, punctuation runs and trailing whitespace.
TOKENIZER_PROBE = (
    "The quick brown fox doesn't jump\n\tover 1234567 lazy dogs.\r\n"
    "def f(x): return x ** 2  # éèê 中文测试 \U0001f600\n"
    "  trailing   spaces   \nhttps://example.com/p?q=1&r=2 'a' \"b\" `c`   "
)


def _extractor_version(vocab=None):
    """Cache key covering everything that shapes a stored payload.

    A hand-maintained integer is a footgun: extraction changed four times in one sitting
    while the constant stayed at 4, and the index kept serving payloads from before the
    changes.  Hashing the modules that shape a payload means any edit invalidates the cache
    automatically, and an unrelated edit to the renderer does not.

    The **vocabulary** is part of that shape too: token counts come from it, so a run with a
    different `--vocab` must not reuse payloads counted with another one.  Hashing 3.6 MB
    costs about 10ms once per run.
    """
    import hashlib
    h = hashlib.blake2b(digest_size=6)
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tokencounter')
    for name in ('worker.py', 'classify.py', 'images.py', 'encoding.py', 'rollout.py'):
        try:
            with open(os.path.join(here, name), 'rb') as fh:
                h.update(fh.read())
        except OSError:
            return 0                            # unreadable source: never reuse the cache
    from tokencounter import encoding as tcenc
    vp = tcenc.vendor_path(vocab)
    # Contents, not path: the same vocabulary at a different path produces identical token
    # counts, so keying on the path alone would force pointless rebuilds while catching
    # nothing the content hash misses.
    try:
        with open(vp, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b''):
                h.update(chunk)
    except OSError:
        return 0                                # unreadable vocabulary: never reuse

    # A *behavioural* fingerprint of the tokenizer, not a description of it.  Hashing
    # `tiktoken.__version__` and its module path was not enough: a same-version wheel
    # replacement, a swapped native `_tiktoken` extension or an editable install all change
    # token counts while leaving both strings identical.  Encoding a fixed probe and hashing
    # the ids captures any of those, because it measures the only thing that matters -- what
    # this tokenizer actually returns.  Costs one encode plus the 0.3s vocabulary load.
    #
    # It cannot catch a monkeypatch applied *after* the key is computed, in-process; no cache
    # key can, and that is not a between-run scenario.
    try:
        enc = tcenc.load(vp)
        ids = enc.encode_ordinary(TOKENIZER_PROBE)
        h.update(str(len(ids)).encode())
        h.update(b','.join(str(i).encode() for i in ids))
    except Exception:
        return 0                                # unusable tokenizer: never reuse
    return int.from_bytes(h.digest(), 'big')


# Counters carried out of the window.  Only the first group is summed into the headline
# total: `unparseable_usage_records` is a *subset* of `unparseable_records`, and adding both
# double-counted a single damaged line as two.
DAMAGE_PRIMARY = ('unparseable_records', 'non_object_records')
DAMAGE_DETAIL = ('unparseable_usage_records',)


def damage_outside(results, window):
    """Damage counters for files the ledger charged but the report does not cover.

    Separated from `main` so it can be tested directly: the bug this replaces was an
    aggregation placed *after* the window filter, which made its own guard unreachable.
    """
    extra = collections.Counter()
    for p, r in results.items():
        if p in window:
            continue
        c = r.get('counters') or {}
        for k in DAMAGE_PRIMARY:
            if c.get(k):
                extra[k] += c[k]
                extra['damage_outside_window'] += c[k]
        for k in DAMAGE_DETAIL:
            if c.get(k):
                extra[k] += c[k]
        if r.get('error'):
            extra['extraction_errors'] += 1
            extra['damage_outside_window'] += 1
    return extra


def codex_home():
    """Codex's state directory.  ``CODEX_HOME`` moves it, and users do move it."""
    return (os.environ.get('CODEX_HOME')
            or os.path.join(os.path.expanduser('~'), '.codex'))


_OUT_DIR = []


def out_dir():
    """Where the index and the report are written, falling back to a temp directory.

    A read-only or absent ``CODEX_HOME`` is a setup this tool should survive: the report is
    still worth producing, it just lands somewhere else, and the path it lands at is printed
    on stdout either way.
    """
    if _OUT_DIR:
        return _OUT_DIR[0]
    # The same two directories tiktoken's private install is looked for in (deps.roots).
    home, fallback = deps.roots()
    d = home
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, '.writable')
        with open(probe, 'w'):
            pass
        os.remove(probe)
    except OSError as exc:
        d = fallback
        os.makedirs(d, exist_ok=True)
        print(f'{home} is not writable ({exc.__class__.__name__}); using {d}',
              file=sys.stderr)
    _OUT_DIR.append(d)
    return d


def open_local(path):
    try:
        if sys.platform.startswith('win'):
            os.startfile(path)                             # noqa: S606
        elif sys.platform == 'darwin':
            subprocess.run(['open', path], check=False)
        else:
            subprocess.run(['xdg-open', path], check=False)
        return True
    except Exception as exc:
        print(f'could not open automatically ({exc.__class__.__name__}); '
              f'open it manually:\n  {path}', file=sys.stderr)
        return False


def _run(fn, paths, procs, label, quiet):
    out = {}
    if not paths:
        return out
    t0 = time.time()
    done = 0
    pooled = procs > 1 and len(paths) > 1
    if pooled:
        try:
            with cf.ProcessPoolExecutor(max_workers=procs) as ex:
                for r in ex.map(fn, paths, chunksize=4):
                    out[r['path']] = r
                    done += 1
                    if not quiet and done % 400 == 0:
                        print(f'  {label}: {done:,}/{len(paths):,} ({time.time()-t0:.0f}s)',
                              file=sys.stderr)
        except (cf.process.BrokenProcessPool, OSError, ValueError,
                ImportError, RuntimeError) as exc:
            # Not every machine can start worker processes: a sandbox with no fork or spawn,
            # a memory cap that kills a child, an embedding interpreter that cannot re-import
            # __main__.  The same work runs here instead -- slower, and identical.
            print(f'  process pool unavailable ({exc.__class__.__name__}); '
                  f'falling back to a single process', file=sys.stderr)
            out.clear()
            pooled = False
    if not pooled:
        for p in paths:
            r = fn(p)
            out[r['path']] = r
    if not quiet:
        print(f'  {label}: {len(paths):,} files in {time.time()-t0:.1f}s', file=sys.stderr)
    return out


def collect(files, procs, threads, vocab, cache, quiet, metrics_only, extractor,
            light=frozenset()):
    """Extract every file, reusing cached results where they are still valid.

    `light` names files needed only for the usage ledger -- out-of-window ancestors.  They
    get the metrics-only pass, which is ~7x cheaper, unless the index already holds a full
    result for them.
    """
    if cache is not None and not extractor:
        # `_extractor_version` returns 0 when it cannot fingerprint the extractor.  Zero is a
        # sentinel, not a key: matching rows against it would serve payloads of unknown
        # provenance, and writing under it would create some for the next run to find.
        if not quiet:
            print('extractor fingerprint unavailable; running without the index',
                  file=sys.stderr)
        cache = None
    results = {}
    todo_full, todo_light = [], []
    now = time.time()
    hits = 0
    for p in files:
        if cache is not None:
            try:
                size, mtime = rollout.stat_key(p)
            except OSError:
                # Deleted or unreadable between discovery and now.  Parsing it will fail
                # too, but the worker counts that failure where the report can see it,
                # which dropping the path here did not.
                todo_full.append(p)
                continue
            got = cache.fresh(p, size, mtime, extractor)
            if got is not None:
                results[p] = got
                cache.touch(p, now)
                hits += 1
                continue
        (todo_light if (p in light and not metrics_only) else todo_full).append(p)

    if not quiet:
        n = len(todo_full) + len(todo_light)
        extra = f' ({len(todo_light):,} ledger-only)' if todo_light else ''
        print(f'{len(files):,} rollout files | {hits:,} cached | {n:,} to parse{extra}',
              file=sys.stderr)

    fn = (worker.metrics_only if metrics_only
          else functools.partial(worker.process, vocab=vocab, num_threads=threads))
    results.update(_run(fn, todo_full, procs, 'parsed', quiet))
    results.update(_run(worker.metrics_only, todo_light, procs, 'ledger-only', quiet))

    if cache is not None and not metrics_only and todo_full:
        for p in todo_full:
            r = results.get(p)
            if r and not r.get('error'):
                cache.put(r, extractor, now)
        cache.commit()

    return results, now


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='report.py', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--since', metavar='YYYY-MM-DD', help='earliest rollout date (inclusive)')
    ap.add_argument('--until', metavar='YYYY-MM-DD', help='latest rollout date (inclusive)')
    ap.add_argument('--session', metavar='ID', help='restrict to one session id or prefix')
    ap.add_argument('--fast', action='store_true',
                    help='use every core instead of half (faster, ~19%% more CPU)')
    ap.add_argument('--procs', type=int, default=None,
                    help='worker processes (default: half the cores, capped at 8)')
    ap.add_argument('--threads', type=int, default=None,
                    help='tiktoken threads per process (default 1; measured to be a worse '
                         'trade than more processes on every axis)')
    ap.add_argument('--metrics-only', action='store_true',
                    help='usage ledger only: no tokenization, no attribution')
    ap.add_argument('--no-cache', action='store_true', help='ignore and do not write the index')
    ap.add_argument('--include-archived', action='store_true',
                    help='also count indexed sessions whose rollout file is gone')
    ap.add_argument('--rebuild', action='store_true', help='discard the index and re-parse')
    ap.add_argument('--no-replay-exclusion', action='store_true',
                    help='charge fork-inherited history instead of excluding it; the '
                         'exclusion is a heuristic match, and this shows the other bound')
    ap.add_argument('--out', metavar='PATH', help='HTML output path')
    ap.add_argument('--json', metavar='PATH', help='write the report model as JSON')
    ap.add_argument('--no-open', action='store_true', help='do not open the browser')
    ap.add_argument('--style', choices=[sid for sid, _ in render.STYLES],
                    help='the style the page opens in (default: the first, or the last one '
                         'picked in this browser)')
    ap.add_argument('--public', action='store_true',
                    help='the page token-share publishes: no session id or directory name, '
                         'and auth.json is not read')
    ap.add_argument('--no-account', action='store_true',
                    help='do not read ~/.codex/auth.json; the report then names no account')
    ap.add_argument('--no-install', action='store_true',
                    help='do not install tiktoken from PyPI when it is missing; the content '
                         'composition is then empty (also TOKEN_COUNTER_NO_INSTALL=1)')
    ap.add_argument('--vocab', metavar='PATH', help='override the vendored BPE path')
    ap.add_argument('--prices', metavar='PATH',
                    help='price table for the API value, in the vendored file\'s format '
                         '(default: the vendored OpenAI list prices; also TOKEN_COUNTER_PRICES)')
    ap.add_argument('--sessions-root', metavar='PATH', help='override ~/.codex/sessions')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--doctor', action='store_true',
                    help='report what this machine provides -- interpreter, Codex home, '
                         'corpus, credentials, tokenizer, index -- and exit')
    a = ap.parse_args(argv)

    if a.doctor:
        return doctor(a)
    if a.public:
        a.no_account = True

    # Measured (scripts/bench.py, 16 logical cores): processes scale cleanly; tokenizer
    # threads cost more CPU for less throughput at every point, so the thread axis defaults
    # to 1 and `--fast` buys cores, not threads.
    cores = os.cpu_count() or 4
    procs = a.procs if a.procs else (cores if a.fast else max(1, min(8, cores // 2)))
    threads = a.threads if a.threads else 1
    procs = max(1, min(procs, 64))
    threads = max(1, min(threads, 32))

    # The ledger always sees the whole corpus; only the *report* is windowed.  Filtering
    # files before charging hides the ancestors a fork child replays, and the child's
    # inherited history is then charged as new -- measured at 246 responses and 30.5M input
    # tokens overcharged on a single session with `--since 2026-07-26`.
    all_files = rollout.discover(a.sessions_root)
    window = set(rollout.discover(a.sessions_root, a.since, a.until))
    if not all_files:
        root = rollout.sessions_root(a.sessions_root)
        why = ('that directory does not exist' if not os.path.isdir(root)
               else 'no rollout-*.jsonl files under it')
        print(f'No rollout files found under {root}\n'
              f'  {why}.\n'
              f'  CODEX_HOME={os.environ.get("CODEX_HOME") or "(unset)"}\n'
              f'  Point at another location with --sessions-root PATH, or set CODEX_HOME.',
              file=sys.stderr)
        return 2
    if not window:
        print('No rollout files in the requested range', file=sys.stderr)
        return 2
    files = all_files

    # The tokenizer is a soft dependency: it feeds the content composition and nothing
    # else.  Without it the usage ledger, the daily chart and the limit chart are all still
    # exact, so degrade to that rather than refusing to run.  `--metrics-only` is the same
    # path, chosen deliberately instead of forced.
    tokenizer_note = None
    if not a.metrics_only:
        tokenizer_note = tokenizer_status(a)
        if tokenizer_note:
            a.metrics_only = True

    extractor = _extractor_version(a.vocab)
    db_path = os.path.join(out_dir(), 'index.db')
    if a.rebuild:
        why = _discard_index(db_path)
        if why:
            # Neither emptied nor deleted.  Reading it now would serve exactly the payloads
            # the user asked to throw away, so the run goes without the index instead.
            print(f'could not discard the index ({why}); running without it',
                  file=sys.stderr)
            a.no_cache = True
    # `--metrics-only` bypasses the index entirely, in both directions. Reading it would
    # silently mix full cached results with metrics-only ones and make the content sections
    # depend on cache state; writing it would poison the index with attribution-free rows.
    cache = None
    if not (a.no_cache or a.metrics_only):
        cache, why = index.try_open(db_path)
        if cache is None and not a.quiet:
            print(f'index unusable ({why}); running without it', file=sys.stderr)

    try:
        # Out-of-window files are needed only for their usage records, so they get the cheap
        # metrics-only pass unless the index already holds a full result for them.
        results, seen_at = collect(files, procs, threads, a.vocab, cache,
                                   a.quiet, a.metrics_only, extractor,
                                   light=set(files) - window)
        if cache is not None:
            newly_archived = cache.reconcile_archived()
            if a.include_archived:
                n = _include_archived(cache, results, window, a)
                if n and not a.quiet:
                    print(f'  {n:,} archived sessions included', file=sys.stderr)
            cache.commit()
            if newly_archived and not a.quiet:
                print(f'  {newly_archived:,} index entries archived '
                      f'(file no longer on disk; use --include-archived to keep counting '
                      f'them)', file=sys.stderr)

        focus = None
        if a.session:
            match = {p for p, r in results.items()
                     if (r.get('session_id') or '').startswith(a.session)
                     or (r.get('thread_id') or '').startswith(a.session)}
            if not match:
                print(f'No session matching {a.session!r}', file=sys.stderr)
                return 3
            sids = sorted({results[p].get('session_id') or p for p in match})
            if len(sids) > 1:
                # A prefix naming several sessions is not "one session": the label would
                # name one and the deep dive would focus on whichever came out of the set.
                shown = ', '.join(s[:12] for s in sids[:6]) + (' ...' if len(sids) > 6 else '')
                print(f'{a.session!r} matches {len(sids)} sessions ({shown}); '
                      f'give a longer prefix', file=sys.stderr)
                return 3
            focus = sids[0]
            window &= match

        t0 = time.time()
        # Charge over everything; report on the window.  See ledger.build(scope=...).
        charged, counters = ledger.build(results, scope=window,
                                         exclude_replay=not a.no_replay_exclusion)
        # Damage in an out-of-window ANCESTOR changes what the window's ledger charges, so
        # its counters must reach the report.  This has to run against the unfiltered
        # results: an earlier attempt aggregated after the window filter, where the
        # `p not in window` test can never be true, and the fix was a silent no-op.
        extra = damage_outside(results, window)
        extra['replay_exclusion_applied'] = 0 if a.no_replay_exclusion else 1
        if cache is not None:
            extra['archived_entries'] = cache.stats()['archived']
            extra['archived_included'] = 1 if a.include_archived else 0

        results = {p: r for p, r in results.items() if p in window}
        scope = {'label': _scope_label(a, len(files), len(results)),
                 'since': a.since, 'until': a.until, 'session': a.session,
                 'replay_excluded': not a.no_replay_exclusion,
                 'metrics_only': bool(a.metrics_only),
                 'tokenizer_note': tokenizer_note}
        # Identity is read once, after extraction, and never enters the index: it is not a
        # property of any rollout file and must not be cached against one.
        acct = accountlib.read(a.sessions_root, enabled=not a.no_account)
        # Read from disk, never fetched: the vendored table, or the one --prices names.
        prices = pricing.load(a.prices)
        if prices[0] is None and not a.quiet:
            print(f'API value not computed: {prices[1]}', file=sys.stderr)
        model = analyze.analyze(results, charged, counters, scope=scope, focus=focus,
                                extra_quality=extra, account=acct, prices=prices)
        if not a.quiet:
            print(f'  ledger + analysis in {time.time()-t0:.1f}s', file=sys.stderr)

        if a.json:
            with open(a.json, 'w', encoding='utf-8') as fh:
                json.dump(model, fh, ensure_ascii=False, indent=1)
            print(a.json)
            if not a.out:                      # --json alone: no HTML, no browser
                return 0

        suffix = (a.session[:8] if a.session else
                  ('-'.join(x for x in (a.since, a.until) if x) or 'all'))
        out = a.out or os.path.join(out_dir(), f'report-{suffix}.html')
        page = render.render(model, public=a.public, style=a.style)
        try:
            with open(out, 'w', encoding='utf-8') as fh:
                fh.write(page)
        except OSError as exc:
            if a.out:               # an explicit path the user chose: do not second-guess it
                print(f'could not write {out}: {exc}', file=sys.stderr)
                return 5
            alt = os.path.join(tempfile.gettempdir(), os.path.basename(out))
            print(f'could not write {out} ({exc.__class__.__name__}); '
                  f'writing {alt} instead', file=sys.stderr)
            with open(alt, 'w', encoding='utf-8') as fh:
                fh.write(page)
            out = alt

        t = model['totals']
        hit = t['cache_hit']
        hit = '--' if hit is None else f'{100*hit:.1f}%'
        if t.get('input_source') == 'tiktoken':
            # Caching and output are Codex's own figures, so they are named with the input
            # they are measured against rather than beside the tiktoken count.
            print(f"{t['responses']:,} responses | "
                  f"{t['tiktoken_input']/1e9:.3f}B input (tiktoken) | "
                  f"recorded by Codex: {t['input']/1e9:.3f}B input, "
                  f"{t['uncached']/1e6:.0f}M uncached, {hit} cached, "
                  f"{t['output']/1e6:.0f}M output")
        else:
            print(f"{t['responses']:,} responses | "
                  f"{t['input']/1e9:.3f}B recorded input | "
                  f"{t['uncached']/1e6:.0f}M uncached | "
                  f"{hit} cached | "
                  f"{t['output']/1e6:.0f}M output")
        for line in api_summary(model.get('api_value')):
            print(line)
        lt = model.get('latency') or {}
        if lt.get('available'):
            r = lt['responses']
            sec = lambda x: render.secs(x).replace('&mdash;', '--')
            line = (f"response time {sec(r['median_s'])} median, {sec(r['p90_s'])} p90 "
                    f"over {r['n']:,} responses")
            if r.get('above_share') is not None:
                # An estimate, worded as one: the rollout has no server timings (§5.8).
                line += f" | an estimated {100 * r['above_share']:.0f}% above the fastest pace"
                cov = r.get('fitted_share')
                if cov is not None and cov < 0.995:
                    line += (f" (of the {100 * cov:.0f}% of response time in models with "
                             f"enough responses)")
            print(line)
        ev = model.get('limit_events') or {}
        if ev.get('total'):
            nd = len(ev.get('daily') or [])
            print(f"rate-limit events {ev['total']:,} on {nd:,} day{'' if nd == 1 else 's'} "
                  f"(snapshots in which Codex logged a limit as reached)")
        rl = model.get('rate_limits') or {}
        cur = rl.get('current')
        if acct.get('available') or cur:
            who = acct.get('email') or acct.get('account_id') or 'account not identified'
            plan = acct.get('plan') or (cur or {}).get('plan_type')
            line = f"{who}{f' ({plan})' if plan else ''}"
            if cur:
                pct = cur.get('last_pct')
                line += (f" | weekly limit {'--' if pct is None else f'{pct:g}%'} used"
                         f" | reset {cur.get('reset_at_iso') or '--'}"
                         f" | next {cur.get('resets_at_iso') or '--'}")
            print(line)
        print(out)
        if not a.no_open:
            open_local(out)
        return 0
    finally:
        if cache is not None:
            cache.close()


_UNPRICED_WHY = {'model': 'model not in the price table',
                 'tier': 'no published rate for its tier',
                 'long_context': 'no long-context rate for its tier'}


def api_summary(av):
    """The API value as stdout lines: the figure, then what it covers and leaves out."""
    av = av or {}
    if not av.get('available'):
        return [f"API value not available -- {av.get('reason') or 'no price table'}"]
    p = av.get('prices') or {}
    lines = [f"API value ${av['usd']:,.2f} if billed at OpenAI API list prices of "
             f"{p.get('as_of') or '?'}{'' if p.get('default', True) else ' (from --prices)'}"
             f" -- recorded usage priced, not a bill"]
    parts = [f"{av['priced']:,} of {av['responses']:,} responses priced"]
    tiers = av.get('tiers') or {}
    for k, label in (('fast', 'Fast'), ('flex', 'Flex'), ('ultrafast', 'Ultrafast')):
        if tiers.get(k):
            parts.append(f"{tiers[k]:,} at {label} rates")
    if av.get('tier_inferred'):
        n = av['tier_inferred']
        parts.append(f"{n:,} first-turn response{'' if n == 1 else 's'} given the tier of the "
                     f"thread's first settings snapshot")
    if av.get('tier_unrecorded'):
        hi = av.get('usd_high')
        parts.append(f"{av['tier_unrecorded']:,} with no recorded tier, priced at standard"
                     + (f" (${hi:,.2f} in total if they ran in Fast mode)"
                        if hi is not None and hi > av['usd'] + 0.005 else ''))
    if av.get('long_context'):
        parts.append(f"{av['long_context']:,} at long-context rates")
    if av.get('web_search_calls'):
        n = av['web_search_calls']
        parts.append(f"{n:,} web search{'' if n == 1 else 'es'} ${av['web_search_usd']:,.2f}")
    lines.append('  ' + ' | '.join(parts))
    for why, n in sorted((av.get('unpriced_by_reason') or {}).items(), key=lambda kv: -kv[1]):
        models = [m for m in (av.get('unpriced_models') or []) if m['reason'] == why]
        names = ', '.join(f"{m['model']} {m['responses']:,}" for m in models[:4])
        lines.append(f"  unpriced: {n:,} response{'' if n == 1 else 's'}, "
                     f"{_UNPRICED_WHY.get(why, why)}" + (f" ({names})" if names else ''))
    if av.get('aborted_turns'):
        n = av['aborted_turns']
        lines.append(f"  {n:,} aborted turn{'' if n == 1 else 's'} (interrupted or replaced): a "
                     f"response cut off by one writes no usage record, so it is not in the figure")
    return lines


def tokenizer_status(a):
    """Build the tokenizer, installing tiktoken first when nothing provides it.

    Returns ``None`` when content can be tokenized, otherwise the one line the page shows in
    place of the content composition.  `codex plugin add` installs no Python packages, so
    without this a fresh install never counted content until someone ran pip by hand; the
    first run that needs tiktoken now installs it into a directory of its own
    (``tokencounter/deps.py``).  Not when the vocabulary is missing, which no install fixes.
    Separated from `main` so the mutation harness can put the old behaviour back.
    """
    from tokencounter import encoding as tcenc
    install_failed = None
    if (not a.no_install and not deps.disabled()
            and os.path.isfile(tcenc.vendor_path(a.vocab))):
        install_failed = deps.ensure(out_dir())
    elif not deps.importable():
        deps.activate()                         # an earlier run's install, if there is one
    try:
        tcenc.load(a.vocab)
        return None
    except (ImportError, FileNotFoundError, ValueError) as exc:
        retry = ''
        if isinstance(exc, ImportError) and install_failed:
            note = install_failed + '.'
            retry = '  the next run tries again; --no-install skips the attempt\n'
        elif isinstance(exc, ImportError):
            # The exception's first line ends "Install it with:", and the page shows only
            # that line.
            note = 'tiktoken is not importable; "python -m pip install tiktoken" installs it.'
        else:
            note = str(exc).strip().splitlines()[0]
    print(f'tokenizer unavailable: {note}\n'
          f'  continuing with the usage ledger only; content composition will be empty.\n'
          f'{retry}'
          f'  run --doctor for the full picture', file=sys.stderr)
    return note


def doctor(a):
    """Print every environment assumption this tool makes, and whether it holds.

    Setups differ: CODEX_HOME is moved, Codex is signed in with an API key instead of a
    ChatGPT account, the sessions tree is on a network mount, the vocabulary was never
    vendored, an older CLI wrote a different rate-limit shape.  Each of those produces a
    different empty or partial report, and the difference is not visible from the report.
    This says which one you have.
    """
    ok = True

    def line(label, value, good=True):
        nonlocal ok
        ok = ok and good
        print(f'{"  " if good else "! "}{label:<22} {value}')

    print('interpreter')
    line('version', '.'.join(str(n) for n in sys.version_info[:3]),
         sys.version_info >= MIN_PY)
    line('executable', sys.executable)
    line('platform', f'{sys.platform} | {os.cpu_count() or "?"} cores')

    print('\ncodex home')
    home = codex_home()
    line('CODEX_HOME', os.environ.get('CODEX_HOME') or '(unset, using ~/.codex)')
    line('resolved', home, os.path.isdir(home))
    root = rollout.sessions_root(a.sessions_root)
    line('sessions root', root, os.path.isdir(root))
    line('output dir', out_dir())

    print('\ncorpus')
    files = rollout.discover(a.sessions_root) if os.path.isdir(root) else []
    line('rollout files', f'{len(files):,}', bool(files))
    if files:
        dates = [d for d in (rollout.file_date(p) for p in files) if d]
        line('date range', f'{min(dates)} .. {max(dates)}' if dates else 'undatable paths',
             bool(dates))
        undated = len(files) - len(dates)
        if undated:
            line('undated paths', f'{undated:,} (not YYYY/MM/DD, not rollout-<date>T...)',
                 False)
        size = 0
        for p in files:
            try:
                size += os.path.getsize(p)
            except OSError:
                pass
        line('corpus size', f'{size/1e9:.2f} GB')
        probe = files[-1]
        try:
            kinds = collections.Counter()
            for outer, _payload, _ts, _raw in rollout.iter_records(probe):
                kinds[outer] += 1
            line('newest file reads', f'{sum(kinds.values()):,} records: '
                 + ', '.join(f'{k} {v:,}' for k, v in kinds.most_common(4)),
                 bool(kinds))
        except OSError as exc:
            line('newest file reads', f'{exc.__class__.__name__}: {exc}', False)

    print('\ncredentials (identity only, never tokens)')
    acct = accountlib.read(a.sessions_root, enabled=not a.no_account)
    line('auth.json', accountlib.auth_path(a.sessions_root),
         os.path.exists(accountlib.auth_path(a.sessions_root)))
    line('auth mode', acct.get('auth_mode') or '(none)')
    line('identity', (acct.get('email') or acct.get('account_id') or acct.get('reason')
                      or 'not available'), bool(acct.get('available')))

    print('\ntokenizer')
    if not deps.importable():
        deps.activate()
    got = deps.installed_version()
    if got:
        line('tiktoken', f'{got[0]} ({os.path.dirname(got[1])})')
    else:
        line('tiktoken', 'not installed -- ' + (
            'installing is turned off; python -m pip install tiktoken'
            if a.no_install or deps.disabled() else
            f'the next report run installs it into {deps.lib_dir(out_dir())}'), False)
    try:
        from tokencounter import encoding as enc
        vp = enc.vendor_path(a.vocab)
        line('vocabulary', vp, os.path.exists(vp))
        if os.path.exists(vp) and got:
            e = enc.load(a.vocab)
            line('probe', f'{len(e.encode(TOKENIZER_PROBE)):,} tokens from the '
                          f'{len(TOKENIZER_PROBE)}-char probe')
    except Exception as exc:                        # noqa: BLE001 - reporting, not handling
        line('vocabulary', f'{exc.__class__.__name__}: {exc}', False)

    print('\nprices (API value; read from disk, never fetched)')
    table, why = pricing.load(a.prices)
    if table is None:
        line('price table', why, False)
    else:
        line('price table', table['path'])
        line('as of', f"{table.get('as_of') or '(no date)'} | {len(table['models']):,} models | "
                      f"{table.get('source') or ''}")

    print('\nindex')
    db = os.path.join(out_dir(), 'index.db')
    cache, why = index.try_open(db)
    if cache is None:
        line('index.db', f'{db} -- unusable: {why}', False)
    else:
        st = cache.stats()
        line('index.db', f'{db} ({os.path.getsize(db)/1e6:.1f} MB)')
        line('entries', ', '.join(f'{k} {v:,}' for k, v in sorted(st.items())))
        cache.close()

    print('\n' + ('all checks passed' if ok else
                  'something above is marked "!" -- the report may be empty or partial'))
    return 0 if ok else 1


def _in_scope(result, a):
    """Date filtering for an archived entry, whose file can no longer be stat'ed."""
    d = result.get('date') or (result.get('started_at') or '')[:10]
    if a.since and (not d or d < a.since):
        return False
    if a.until and (not d or d > a.until):
        return False
    return True


def _include_archived(cache, results, window, a):
    """``--include-archived``: count indexed sessions whose rollout file is gone.

    An archived entry has to join `window` as well as `results`.  The window is the set of
    paths discovered on disk, which an archived path by definition is not in, so adding the
    payload to `results` alone left the ledger charging it out of scope and the report
    filter dropping it: the run printed "N archived sessions included" and included none.
    Returns how many were added.
    """
    n = 0
    for r in cache.archived():
        p = r.get('path')
        if p and p not in results and _in_scope(r, a):
            results[p] = r
            window.add(p)
            n += 1
    return n


def _discard_index(db_path):
    """``--rebuild``: empty the index, deleting the file only if it cannot be opened.

    Deleting first was wrong.  On Windows a second run holding the database open makes the
    file undeletable, and the fallback promised that the schema check would re-parse
    everything -- it clears the table only on a version change, so ``--rebuild`` reported
    the failure and then served every cached payload.  Truncating through a connection works
    while the file is held open; deletion remains for a file too damaged to open as a
    database.  Returns ``None`` on success, otherwise why not.
    """
    if not os.path.exists(db_path):
        return None
    idx, why = index.try_open(db_path)
    if idx is not None:
        try:
            idx.clear()
            return None
        except sqlite3.Error as exc:
            why = f'{exc.__class__.__name__}: {exc}'
        finally:
            idx.close()
    try:
        os.remove(db_path)
        return None
    except OSError as exc:
        return f'{why}; delete: {exc.__class__.__name__}'


def _scope_label(a, n_files, n_used):
    bits = []
    if a.session:
        bits.append(f'session {a.session}')
    if a.since and a.until:
        bits.append(f'{a.since} to {a.until}')
    elif a.since:
        bits.append(f'since {a.since}')
    elif a.until:
        bits.append(f'through {a.until}')
    if not bits:
        bits.append('all sessions')
    bits.append(f'{n_used:,} rollout files')
    return ' · '.join(bits)


def _cli():
    try:
        return main()
    except (FileNotFoundError, ValueError, ImportError) as exc:
        # These carry actionable guidance (missing or corrupt vocabulary, absent tiktoken).
        # A raw traceback from inside a pool worker buries it.
        print(f'\n{exc}', file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print('\ninterrupted', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(_cli())
