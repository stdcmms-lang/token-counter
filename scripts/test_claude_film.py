#!/usr/bin/env python3
"""Offline invented-film assertions; also produces temporary Node page fixtures."""
import argparse
import ast
import base64
import builtins
import copy
import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts/fixtures'))
import claude_film_cases as cases
SCRIPT = REPO / 'plugins/token-counter-claude/skills/token-film/scripts/film.py'
spec = importlib.util.spec_from_file_location('film', str(SCRIPT))
film = importlib.util.module_from_spec(spec)
spec.loader.exec_module(film)


def collect(root, case=None, table=None):
    path = cases.write(root, case)
    main, meta, result = film.read_main(path)
    table = film.pricing.load() if table is None else table
    data = film.film_data(main, meta, table)
    summary = film.pricing.summarize(main['rows'], table)
    meta['pricing_counters'] = {k: summary.get(k, 0) for k in film.COUNTER_SCOPES['pricing']}
    omitted = film.omitted_subagents(path, film.rollout.discover(root)[0], result, table)
    return data, film.diagnostic_json(data, main, meta, omitted), main, meta, result


def error(fn, code, message=None):
    try:
        fn()
    except film.FilmError as exc:
        assert exc.exit_code == code, (exc.exit_code, code)
        if message is not None:
            assert str(exc) == message, (str(exc), message)
    else:
        raise AssertionError('expected FilmError')


def test_primary():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'projects'
        data, diag, main, meta, result = collect(root)
        assert data['counts'] == {'responses': 7, 'steps': 6, 'zero_dropped': 1, 'partial': 1,
                                  'compactions': {'manual': 1, 'auto': 1, 'unknown': 1}}
        assert [s['prompt'] for s in data['steps']] == [True, False, True, False, False, False]
        assert [s['blocks'] for s in data['steps']] == [['k', 't', 'Read'], ['MCP', 'tool'], ['k', 'Bash', 't'], ['t'], ['t', 't'], ['k', 't']]
        assert [sum(s[k] for k in ('read', 'write', 'input')) for s in data['steps']] == [100, 155, 80052, 63, 76, 101]
        assert [s['elapsed_ms'] for s in data['steps']] == [0, 15000, 25000, 35000, 45000, 60000]
        assert data['span_ms'] == 60000 and film.span_text(data['span_ms']) == '2 minutes'
        assert data['output_breakdown'] == {'complete': True, 'thinking_present': True, 'known_thinking_tokens': 15,
                                           'missing_responses': 0, 'missing_output_tokens': 0}
        assert film.category_totals(data['steps'], True) == {'read': 370, 'write': 80155, 'thinking': 15, 'writing': 35, 'input': 22}
        assert sum(r['usage']['input_tokens'] for r in main['rows']) == 80547
        assert sum(r['cache_write_5m'] for r in main['rows']) == 80020
        assert sum(r['cache_write_1h'] for r in main['rows']) == 135
        assert data['display_evidence'] == {'cache_ttl': 'mixed', 'context_1m': False, 'read_discounted': False}
        assert data['compactions'] == [{'after': 2, 'trigger': 'manual', 'pre': 155, 'post': 10},
                                       {'after': 3, 'trigger': 'auto', 'pre': 80052, 'post': 20},
                                       {'after': 4, 'trigger': 'unknown', 'pre': None, 'post': None}]
        p = data['pricing']
        assert (p['priced'], p['unpriced'], p['search_calls']) == (6, 1, 3)
        assert p['priced_tokens'] == {'read': 310, 'write': 80155, 'thinking': 13, 'writing': 31, 'input': 19}
        assert p['usd_micro'] == {'read': 62, 'write': 401180, 'thinking': 260, 'writing': 620, 'input': 76}
        assert (p['search_usd_micro'], p['total_usd_micro'], p['total_cents']) == (30000, 432198, 43)
        assert p['cents'] == {'read': 0, 'write': 40, 'thinking': 0, 'writing': 0, 'input': 0, 'search': 3}
        child = diag['excluded_subagents']
        assert tuple(child[k] for k in ('responses', 'input', 'cached', 'output', 'known_thinking', 'total_tokens', 'usd_micro')) == (1, 32, 20, 5, 1, 37, 192)
        assert all(child['quality'].get(k, 0) == 0 for k in ('synthetic_records', 'api_error_records', 'partial_responses', 'response_terminal_usage_invalid'))
        assert diag['family_comparison']['main_rows_differ_from_main_only'] == 0
        expected = {'api_error_records': 1, 'partial_responses': 1, 'price_partial_responses': 1,
                    'response_terminal_usage_invalid': 1, 'responses_excluded': 2, 'synthetic_records': 1,
                    'unpriced_input': 63, 'unpriced_model': 1, 'unpriced_output': 6, 'unpriced_responses': 1,
                    'usage_required_type_invalid': 1}
        actual = film.material_counters(diag['diagnostics']['main_ledger_counters'], diag['diagnostics']['pricing_counters'])
        for key, value in expected.items():
            assert actual[key] == value, (key, actual.get(key), value)
        assert meta['counters'] == {'film_pre_fallback': 1, 'film_post_fallback': 1, 'film_unknown_trigger': 1}
        assert film.local_start(film.worker.epoch(result['first_record_ts']), datetime.timezone(datetime.timedelta(hours=8))) == '2026-01-01 08:00'
        assert not Path(result['path']).exists(), 'snapshot leaked'


def test_terminal_ascii_and_grouping():
    # Cover named error constants and inline message literals, including formats.
    messages = []
    for node in ast.walk(ast.parse(SCRIPT.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'FilmError':
            for part in ast.walk(node.args[1]):
                value = part.value if isinstance(part, ast.Constant) else getattr(film, part.id, None) if isinstance(part, ast.Name) else None
                if isinstance(value, str):
                    messages.append(value)
    assert messages
    for message in messages:
        message.encode('ascii')
    assert film.RANGE == "This session's counts exceed the film renderer's exact integer range."
    with tempfile.TemporaryDirectory() as directory:
        data, diag, _, _, _ = collect(Path(directory) / 'projects')
        summary = film.terminal_summary('invented', data, diag['diagnostics'], diag['excluded_subagents'], Path('invented.html'))
        summary.encode('ascii')
        assert summary.splitlines()[0] == 'Token Counter for Claude Code - token film 0.2.0'
        assert 'Tokens: Re-reading=370 | Caching=80,155 | Thinking=15 | Writing=35 | Fresh input=22 | Total=80,597' in summary.splitlines()
        assert 'Pricing: 6 priced responses; 1 unpriced response' in summary.splitlines()
        assert 'Omitted subagents: 1 response | 37 tokens | API list value $0.000192' in summary.splitlines()
        # Display-only counts: 999 elapsed days count as 1,000 days; compactions
        # total 1,000 + 2,000 + 3,000 = 6,000. All expectations are handwritten.
        data = copy.deepcopy(data)
        data['span_ms'] = 999 * 86400000
        data['counts'].update(steps=4321, zero_dropped=1234, compactions={'manual': 1000, 'auto': 2000, 'unknown': 3000})
        data['pricing'].update(priced=12345, unpriced=23456)
        data['output_breakdown'].update(missing_responses=1234, missing_output_tokens=5678)
        diagnostics = copy.deepcopy(diag['diagnostics'])
        diagnostics['main_ledger_counters']['partial_responses'] = 1234
        diagnostics['film_counters']['film_unknown_blocks'] = 2345
        omitted = dict(diag['excluded_subagents'], responses=34567, total_tokens=45678)
        summary = film.terminal_summary('invented', data, diagnostics, omitted, Path('invented.html'))
        summary.encode('ascii')
        for line in ('Span: 1,000 days', 'Steps: 4,321', 'Zero-usage responses dropped: 1,234',
                     'Compactions: 6,000 (manual 1,000, automatic 2,000, unknown 3,000)',
                     'Pricing: 12,345 priced responses; 23,456 unpriced responses',
                     'Thinking: unrecorded for 1,234 responses (5,678 output tokens counted as Writing).',
                     'Omitted subagents: 34,567 responses | 45,678 tokens | API list value $0.000192',
                     'partial_responses: 1,234', 'film_unknown_blocks: 2,345'):
            assert line in summary.splitlines(), line


def test_thinking_and_prices():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'projects'
        above, diag, _, _, _ = collect(root, cases.variant('missing_thinking'))
        assert above['output_breakdown'] == {'complete': False, 'thinking_present': False, 'known_thinking_tokens': 14,
                                            'missing_responses': 1, 'missing_output_tokens': 2}
        assert film.category_totals(above['steps'], False)['writing'] == 50
        assert 'thinking' not in above['pricing']['usd_micro']
        assert above['pricing']['usd_micro']['writing'] == 880
        assert above['pricing']['total_usd_micro'] == 432198
        assert diag['diagnostics']['main_ledger_counters']['reasoning_missing'] == 1
        summary = film.terminal_summary('invented', above, diag['diagnostics'], diag['excluded_subagents'], Path('invented.html'))
        disclosure = 'Thinking: unrecorded for 1 response (2 output tokens counted as Writing).'
        assert disclosure in summary
        under, diag, _, _, _ = collect(root, cases.variant('under_threshold'))
        assert under['output_breakdown']['thinking_present'] is True
        assert film.category_totals(under['steps'], True) == {'read': 370, 'write': 80155, 'thinking': 14, 'writing': 236, 'input': 22}
        assert under['pricing']['usd_micro'] == {'read': 62, 'write': 401180, 'thinking': 240, 'writing': 4640, 'input': 76}
        assert under['pricing']['priced_tokens']['writing'] == 232
        assert under['pricing']['total_usd_micro'] == 436198
        assert under['pricing']['cents'] == {'read': 0, 'write': 40, 'thinking': 0, 'writing': 1, 'input': 0, 'search': 3}
        assert disclosure in film.terminal_summary('invented', under, diag['diagnostics'], diag['excluded_subagents'], Path('invented.html'))
        for output, present in ((99, True), (98, False)):
            steps = [{'read': 0, 'write': 0, 'input': 0, 'output': output, 'thinking': 1},
                     {'read': 0, 'write': 0, 'input': 0, 'output': 1, 'thinking': None}]
            assert film.output_breakdown(steps)['thinking_present'] is present
            assert film.category_totals(steps, present)['writing'] == 99
        no_prices, _, _, _, _ = collect(root, table=(None, 'price_table_unavailable'))
        assert no_prices['pricing']['reason'] == 'table_unavailable'
        assert no_prices['pricing']['total_usd_micro'] is None
        assert all(v is None for v in no_prices['pricing']['usd_micro'].values())
        assert no_prices['pricing']['search_calls'] == 3
        no_input, _, _, _, _ = collect(root, cases.variant('zero_input'))
        assert film.category_totals(no_input['steps'], True)['input'] == 0
        assert no_input['pricing']['total_usd_micro'] == 432122
        assert sum(film.category_totals(no_input['steps'], True).values()) == 80575
        for speed, geo, expected, total in (
                ('standard', 'global', [8, 340, 60, 100, 40], 10548),
                ('fast', 'global', [16, 680, 120, 200, 80], 11096),
                ('standard', 'us', [9, 374, 66, 110, 44], 10603),
                ('fast', 'us', [18, 748, 132, 220, 88], 11206)):
            d, _, _, _, _ = collect(root, cases.r1(speed, geo))
            assert [d['pricing']['usd_micro'][k] for k in film.CATEGORIES] == expected
            assert d['pricing']['total_usd_micro'] == total
        assert list(film.allocate_cents(dict(zip(film.CATEGORIES, [5000, 5000, 5000, 0, 0])), 15000).values()) == [1, 1, 0, 0, 0]
        # A finite validated rate can overflow float pricing arithmetic. Ten
        # input tokens at 1e308 per million give 1e303 USD, far above the
        # renderer's microdollar integer range (and the intermediate 1e309
        # multiplication exceeds binary64). It must be a clean range error.
        enormous = copy.deepcopy(film.pricing.load()[0])
        enormous['models']['claude-opus-5-5']['input'] = 1e308
        error(lambda: collect(root, cases.r1(), (enormous, None)), 3, film.RANGE)


def test_join_and_physical_order():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'projects'
        primary, _, _, _, _ = collect(root)
        duplicate, _, _, _, _ = collect(root, cases.variant('duplicate'))
        assert primary == duplicate
        prompted, _, _, _, _ = collect(root, cases.variant('physical_prompt'))
        assert [s['prompt'] for s in prompted['steps']] == [True, True, True, False, False, False]
        # Ledger sorts by timestamp, but the film stays in physical order.
        case = cases.primary()
        records = case['files'][cases.PROJECT + '/' + cases.SESSION + '.jsonl']
        records[11]['timestamp'] = cases.stamp(1)
        d, _, _, meta, _ = collect(root, case)
        assert [s['read'] for s in d['steps']] == [40, 50, 50, 60, 70, 100]
        assert d['steps'][2]['elapsed_ms'] == 15000 and meta['counters']['film_time_regressions'] == 1
        with mock.patch.object(film, 'scan_metadata', return_value={'responses': {}, 'prompts': [], 'boundaries': []}):
            error(lambda: film.read_main(root / cases.PROJECT / (cases.SESSION + '.jsonl')), 3, film.JOIN)
        unknown, _, _, _, _ = collect(root, cases.variant('unknown_trigger'))
        assert unknown == primary
        for name in ('zero_only', 'error_only'):
            error(lambda: collect(root, cases.variant(name)), 2, film.EMPTY)
        for name in ('safe_overflow', 'cumulative_overflow'):
            error(lambda: collect(root, cases.variant(name)), 3, film.RANGE)
        enormous_count = cases.primary()
        enormous_count['files'][cases.PROJECT + '/' + cases.SESSION + '.jsonl'][-1]['message']['usage']['cache_read_input_tokens'] = 10**400
        error(lambda: collect(root, enormous_count), 3, film.RANGE)
        # Half-up must happen in decimal milliseconds, including negative ties
        # before the regression clamp: +500 us -> 1 ms; -500 us -> -1 -> 0 ms.
        for second, expected_span, regressions in ((5.0005, 1, 0), (4.9995, 0, 1)):
            small = {'files': {cases.PROJECT + '/' + cases.SESSION + '.jsonl': [
                cases.response(1, 5, (0, 0, 0, 1, 0, 0)), cases.response(2, second, (0, 0, 0, 1, 0, 0))]}}
            d, _, _, m, _ = collect(root, small)
            assert d['span_ms'] == expected_span
            assert [s['elapsed_ms'] for s in d['steps']] == [0, expected_span]
            assert m['counters'].get('film_time_regressions', 0) == regressions
        assert film.safe_tool_name('read') == 'tool'
        assert film.safe_tool_name(None) == 'tool'
        assert all(film.safe_tool_name(k) == k for k in film.TOOLS)
        assert film.is_typed_prompt({'type': 'user', 'message': {'content': ''}}) is False
        assert film.is_typed_prompt({'type': 'user', 'isMeta': True, 'message': {'content': 'PRIVATE'}}) is False
        case = cases.primary()
        records = case['files'][cases.PROJECT + '/' + cases.SESSION + '.jsonl']
        records[1]['message']['content'].append({'type': 'PRIVATE_UNKNOWN', 'text': 'PRIVATE_BLOCK'})
        _, _, _, meta, _ = collect(root, case)
        assert meta['counters']['film_unknown_blocks'] == 1


def test_snapshots():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'projects'
        expected, _, _, _, _ = collect(root)
        real_match = film.prefix_matches
        real_extract = film.worker.extract
        captured_paths = []
        def extract(path, **kwargs):
            captured_paths.append(Path(path))
            return real_extract(path, **kwargs)
        for change in ('append', 'fragment', 'rewrite', 'long_rewrite', 'twice'):
            path = cases.write(root)
            if change == 'fragment':
                with path.open('ab') as handle:
                    handle.write(b'{"type":"user","message":{"content":"PRIVATE_FRAGMENT"}}')
            if change == 'long_rewrite':
                body = path.read_bytes().replace(b'PRIVATE_PROMPT0', b'PRIVATE_PROMPT0' + b'X' * 70000)
                path.write_bytes(body)
            calls = [0]
            def match(live, size, digest):
                calls[0] += 1
                if calls[0] == 1 or (change == 'twice' and calls[0] == 2):
                    if change == 'append':
                        with path.open('ab') as handle:
                            handle.write(film.canonical_json(cases.response(7, 70, (1, 0, 0, 2, 3, 1))) + b'\n')
                    elif change == 'fragment':
                        with path.open('ab') as handle:
                            handle.write(b'\n')
                    elif change == 'long_rewrite':
                        body = path.read_bytes()
                        body = body[:66000] + b'Y' + body[66001:]
                        path.write_bytes(body)
                    else:
                        path.write_bytes(path.read_bytes().replace(b'PRIVATE_PROMPT0', b'PRIVATE_PROMPTZ') if calls[0] == 1 else path.read_bytes().replace(b'PRIVATE_PROMPTZ', b'PRIVATE_PROMPTQ'))
                return real_match(live, size, digest)
            with mock.patch.object(film, 'prefix_matches', side_effect=match), mock.patch.object(film.worker, 'extract', side_effect=extract):
                if change == 'twice':
                    error(lambda: film.read_main(path), 3, film.CHANGED)
                else:
                    main, meta, _ = film.read_main(path)
                    assert film.film_data(main, meta, film.pricing.load()) == expected
                    assert meta['counters'].get('film_snapshot_retries', 0) == (1 if 'rewrite' in change else 0)
            assert all(not p.exists() for p in captured_paths), 'temporary snapshot survived'
        path = cases.write(root)
        with mock.patch.object(film.worker, 'extract', side_effect=lambda p, **kw: dict(real_extract(p, **kw), stable_read=False)):
            error(lambda: film.read_main(path), 3)
        assert all(not p.exists() for p in captured_paths)
        # Physical cutoff includes malformed and non-object complete records.
        with path.open('ab') as handle:
            handle.write(b'INVALID\n[]\n{"type":"user","message":{"content":"PRIVATE_TRAILING"}}')
        main, meta, result = film.read_main(path)
        assert result['last_complete_line'] == 22
        assert film.film_data(main, meta, film.pricing.load()) == expected
        original_directory = tempfile.TemporaryDirectory
        snapshot_directories = []
        def directory_spy(*args, **kwargs):
            d = original_directory(*args, **kwargs)
            snapshot_directories.append(Path(d.name))
            return d
        with mock.patch.object(film.tempfile, 'TemporaryDirectory', side_effect=directory_spy):
            film.read_main(path)
            with mock.patch.object(film, 'prefix_matches', return_value=False):
                error(lambda: film.read_main(path), 3, film.CHANGED)
            with mock.patch.object(film, 'verify_join', side_effect=film.FilmError(3, film.JOIN)):
                error(lambda: film.read_main(path), 3, film.JOIN)
            with mock.patch.object(film.worker, 'extract', side_effect=OSError('PRIVATE_EXCEPTION')):
                error(lambda: film.read_main(path), 3)
        assert len(snapshot_directories) == 5 and all(not p.exists() for p in snapshot_directories)


def test_selection_and_destinations():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'projects'
        data, _, _, _, _ = collect(root)
        path = root / cases.PROJECT / (cases.SESSION + '.jsonl')
        assert film.select_session([path], cases.SESSION) == path
        error(lambda: film.current_prefix({}), 2, 'CLAUDE_CODE_SESSION_ID is unset. Use --session PREFIX or --list.')
        error(lambda: film.parse_args([]), 2, 'Choose a session: --session PREFIX, --current, or --list.')
        for args in (['--list', '--out', 'x.html'], ['--list', '--json', '-'], ['--list', '--prices', 'x']):
            error(lambda: film.parse_args(args), 2)
        twin = root / 'OTHER_PRIVATE_PROJECT' / path.name
        twin.parent.mkdir()
        shutil.copyfile(path, twin)
        error(lambda: film.select_session([path, twin], cases.SESSION), 2)
        try:
            film.select_session([path, twin], cases.SESSION)
        except film.FilmError as exc:
            assert 'OTHER_PRIVATE_PROJECT' not in str(exc) and str(root) not in str(exc)
        resolved = film.paths.resolve_paths(root)
        page, _ = film.output_paths(film.parse_args(['--current']), resolved, data)
        assert page.name == 'film-%s-v0.2.0.html' % data['artifact_tag']
        for bad in (film.PLUGIN_ROOT / 'bad.html', resolved['report_path'], root / 'bad.html'):
            error(lambda: film.validate_destinations(bad, None, [path], resolved, film.PLUGIN_ROOT), 2)
        error(lambda: film.validate_destinations(page, page, [path], resolved, film.PLUGIN_ROOT), 2)
        error(lambda: film.validate_destinations(page.with_suffix('.txt'), None, [path], resolved, film.PLUGIN_ROOT), 2)
        entries = film.list_sessions([path, twin])
        assert all(e['steps'] == 6 and e['tokens'] == 80597 and e['compactions'] == 3 for e in entries)
        assert '2 minutes' in film.format_listing(entries)
        assert not resolved['state_dir'].exists()
        # Listing must parse just the ten selected mains, in metadata order.
        paths = []
        for i in range(11):
            another = path.with_name('invented-list-%02d.jsonl' % i)
            shutil.copyfile(path, another)
            os.utime(str(another), (1000 + i, 1000 + i))
            paths.append(another)
        with mock.patch.object(film, 'read_main', wraps=film.read_main) as read:
            selected_entries = film.list_sessions(paths)
            assert len(selected_entries) == read.call_count == 10
            assert [call.args[0] for call in read.call_args_list] == list(reversed(paths[1:]))
        assert film.span_text(None) == 'unavailable'
        assert [film.span_text(ms) for ms in (0, 7200000, 172800000, 4 * 86400000)] == ['1 minute', '3 hours', '3 days', '5 days']


def test_unavailable_listing():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'projects'
        path = cases.write(root)
        unreadable = root / 'invented-unreadable' / path.name
        unreadable.parent.mkdir()
        shutil.copyfile(path, unreadable)
        real_matches = film.prefix_matches
        def matches(candidate, size, digest):
            return candidate != unreadable and real_matches(candidate, size, digest)
        # Simulate two failed snapshot checks on only the invented second main.
        # The real read_main raises FilmError(CHANGED); its valid neighbor parses.
        unavailable_row = cases.SESSION + '\tunavailable' * 5
        with mock.patch.object(film, 'prefix_matches', side_effect=matches):
            entries = film.list_sessions([path, unreadable])
            assert len(entries) == 2
            assert sorted(e['steps'] for e in entries if e['steps'] is not None) == [6]
            listing = film.format_listing(entries)
            assert unavailable_row in listing.splitlines()
            assert sum(line.endswith('\t2 minutes\t6\t80,597\t3') for line in listing.splitlines()) == 1
            with mock.patch.object(sys, 'stdout', new=io.StringIO()) as stdout, mock.patch.object(sys, 'stderr', new=io.StringIO()) as stderr:
                assert film.main(['--sessions-root', str(root), '--list']) == 0
                assert unavailable_row in stdout.getvalue().splitlines()
                assert stderr.getvalue() == ''
            try:
                film.select_session([path, unreadable], cases.SESSION)
            except film.FilmError as exc:
                assert exc.exit_code == 2 and str(exc).startswith('That session prefix is ambiguous.\n')
                assert unavailable_row in str(exc).splitlines()
                assert sum(line.endswith('\t2 minutes\t6\t80,597\t3') for line in str(exc).splitlines()) == 1
                assert film.CHANGED not in str(exc) and str(root) not in str(exc)
            else:
                raise AssertionError('expected ambiguous prefix')
        assert not film.paths.resolve_paths(root)['state_dir'].exists()


def page_bytes(data):
    return film.render_html(data, film.font_bundle(film.PLUGIN_ROOT), film.license_bundle(film.PLUGIN_ROOT), SCRIPT.parent.parent / 'assets/film.html')


def test_html_privacy_and_determinism():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'projects'
        data, diag, _, _, _ = collect(root)
        html = page_bytes(data)
        assert html == page_bytes(collect(root)[0])
        for sentinel in cases.SENTINELS:
            if sentinel == '</script>':
                assert html.count(b'</script>') == 1, 'only the shell may close the script'
            else:
                assert sentinel.encode() not in html, sentinel
        assert b'<a ' not in html and b'<a>' not in html
        assert html.lower().startswith(b'<!doctype html>')
        moved = Path(directory) / 'moved/projects'
        shutil.copytree(str(root), str(moved))
        for p in moved.rglob('*.jsonl'):
            os.utime(str(p), (1234, 1234))
        moved_main, moved_meta, _ = film.read_main(moved / cases.PROJECT / (cases.SESSION + '.jsonl'))
        assert page_bytes(film.film_data(moved_main, moved_meta, film.pricing.load())) == html
        case = cases.primary()
        child = next(records for path, records in case['files'].items() if 'subagents' in path)
        child[-1]['message']['usage']['output_tokens'] = 8
        d2, diag2, _, _, _ = collect(root, case)
        assert page_bytes(d2) == html and diag2 != diag
        # Inherited main copies must stay owned by main; a subagent conflict is
        # diagnostic only and cannot replace main-only film rows.
        case = cases.primary()
        inherited = copy.deepcopy(case['files'][cases.PROJECT + '/' + cases.SESSION + '.jsonl'][1:3])
        child = next(records for path, records in case['files'].items() if 'subagents' in path)
        child[:0] = inherited
        same, family_diag, _, _, _ = collect(root, case)
        assert page_bytes(same) == html
        assert family_diag['excluded_subagents']['responses'] == 1
        assert family_diag['family_comparison']['main_rows_differ_from_main_only'] == 0
        child[1]['message']['usage']['output_tokens'] = 9
        conflict, conflict_diag, _, _, _ = collect(root, case)
        assert page_bytes(conflict) == html
        assert conflict_diag['family_comparison']['main_rows_differ_from_main_only'] == 1
        # Diagnostic values larger than the renderer's range are not embedded.
        # A child-only pricing overflow must likewise leave the main page intact.
        large_table = copy.deepcopy(film.pricing.load()[0])
        large_table['models']['claude-opus-5-5']['input'] = 1e308
        child_case = cases.small('twentyone')
        child_path = cases.PROJECT + '/' + cases.SESSION + '/subagents/agent-child.jsonl'
        child_case['files'][child_path] = [cases.response(100, 5, (0, 0, 0, 1, 0, 0))]
        unaffected, _, _, _, _ = collect(root, child_case, (large_table, None))
        child_case['files'][child_path][0]['message']['usage']['input_tokens'] = 1
        huge_dollars, huge_diag, _, _, _ = collect(root, child_case, (large_table, None))
        assert page_bytes(huge_dollars) == page_bytes(unaffected)
        assert huge_diag['excluded_subagents']['usd_micro'] > film.MAX_SAFE
        child_case['files'][child_path][0]['message']['usage']['input_tokens'] = 2
        unrepresentable, numeric_diag, _, _, _ = collect(root, child_case, (large_table, None))
        assert page_bytes(unrepresentable) == page_bytes(unaffected)
        assert numeric_diag['excluded_subagents']['usd_micro'] is None
        assert numeric_diag['excluded_subagents']['quality']['film_subagent_pricing_unavailable'] == 1
        # Re-key every private identity consistently; physical prompt flags do not use parents.
        case = json.loads(json.dumps(cases.primary()).replace('PRIVATE_', 'SECRET_'))
        rekeyed = Path(directory) / 'rekeyed'
        cases.write(rekeyed, case)
        selected = film.discover_main(rekeyed)[0][0]
        m, meta, _ = film.read_main(selected)
        assert page_bytes(film.film_data(m, meta, film.pricing.load())) == html
        for key, filename in (('fredoka', 'Fredoka.ttf'), ('mono', 'SpaceMono-Bold.ttf')):
            value = json.loads(re.search(rb'const FONT_DATA = (.*?);', html).group(1))[key]
            assert base64.b64decode(value) == (film.PLUGIN_ROOT / 'assets/vendor/fonts' / filename).read_bytes()
        licenses = json.loads(re.search(rb'const LICENSE_DATA = (.*?);', html).group(1))
        for filename, encoded in licenses.items():
            assert base64.b64decode(encoded) == (film.PLUGIN_ROOT / 'assets/vendor/fonts' / filename).read_bytes()
        script = re.search(rb'<script>(.*?)</script>', html, re.S).group(1).decode()
        forbidden = r'\b(?:fetch|XMLHttpRequest|WebSocket|sendBeacon|MediaRecorder|AudioDecoder|VideoDecoder)\b|Date\.now|Math\.random|performance\.now|\bimport\s*\('
        assert not re.search(forbidden, script)
        markup = html.split(b'<script>', 1)[0]
        assert not re.search(rb'<(?:form|iframe|img|audio|video)\b|\b(?:src|href)\s*=', markup)
        assert script.count("createElement('a')") == 1


def test_cli_and_containment():
    with tempfile.TemporaryDirectory(prefix='film installed ') as directory:
        root = Path(directory)
        corpus = root / 'projects'
        cases.write(corpus)
        page, diagnostic = root / 'out/page.html', root / 'out/data.json'
        cmd = [sys.executable, '-I', '-S', '-B', str(SCRIPT), '--sessions-root', str(corpus), '--session', cases.SESSION,
               '--out', str(page), '--json', str(diagnostic), '--no-open']
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='ascii', timeout=30, env=env)
        assert result.returncode == 0, result.stderr
        assert json.loads(diagnostic.read_bytes())['film']['pricing']['total_usd_micro'] == 432198
        assert '2 minutes' in result.stdout
        assert sorted(p.name for p in page.parent.iterdir()) == ['data.json', 'page.html']
        cli_result = subprocess.run(cmd[:5] + ['--version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='ascii', env=env)
        assert cli_result.stdout.splitlines() == ['token-counter-claude 0.2.0'] and cli_result.returncode == 0
        result = subprocess.run(cmd[:-3] + ['--json', '-', '--no-open'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='ascii', timeout=30, env=env)
        assert result.returncode == 0 and json.loads(result.stdout)['film']['counts']['steps'] == 6
        assert 'Page:' in result.stderr
        def deny(*args, **kwargs):
            raise AssertionError('unexpected account/history/index/network access')
        original_open, original_io_open = builtins.open, io.open
        def open_spy(file, *args, **kwargs):
            if isinstance(file, (str, bytes, Path)) and Path(file).name in ('.claude.json', '.credentials.json', 'credentials.json'):
                deny()
            return original_open(file, *args, **kwargs)
        def io_spy(file, *args, **kwargs):
            if isinstance(file, (str, bytes, Path)) and Path(file).name in ('.claude.json', '.credentials.json', 'credentials.json'):
                deny()
            return original_io_open(file, *args, **kwargs)
        from tokencounter import account, history, index
        with mock.patch.object(account, 'read_account', side_effect=deny), mock.patch.object(history, 'History', side_effect=deny), mock.patch.object(index, 'Index', side_effect=deny), mock.patch.object(socket, 'socket', side_effect=deny), mock.patch.object(builtins, 'open', side_effect=open_spy), mock.patch.object(io, 'open', side_effect=io_spy), mock.patch.object(sqlite3, 'connect', side_effect=deny):
            with mock.patch.object(sys, 'stdout', new=io.StringIO()), mock.patch.object(sys, 'stderr', new=io.StringIO()):
                assert film.main(['--sessions-root', str(corpus), '--current', '--no-open']) == 2
                assert film.main(['--sessions-root', str(corpus), '--session', cases.SESSION, '--out', str(page), '--no-open']) == 0
        assert not list(root.rglob('history.db')) and not list(root.rglob('index-cache.db')) and not list(root.rglob('claude-share.json'))


def emit_fixtures(directory):
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'projects'
        for name in ('primary', 'missing_thinking', 'under_threshold', 'zero_input', 'physical_prompt', 'duplicate', 'unknown_trigger'):
            data = collect(root, cases.variant(name))[0]
            (target / (name + '.html')).write_bytes(page_bytes(data))
        for name in ('twentyone', 'dense', 'edges'):
            (target / (name + '.html')).write_bytes(page_bytes(collect(root, cases.small(name))[0]))
        for speed in ('standard', 'fast'):
            for geo in ('global', 'us'):
                (target / ('r1_%s_%s.html' % (speed, geo))).write_bytes(page_bytes(collect(root, cases.r1(speed, geo))[0]))
        (target / 'no_prices.html').write_bytes(page_bytes(collect(root, table=(None, 'price_table_unavailable'))[0]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--section', choices=('data', 'integration'))
    parser.add_argument('--emit-fixtures')
    args = parser.parse_args()
    if args.emit_fixtures:
        emit_fixtures(args.emit_fixtures)
        return 0
    tests = []
    if args.section in (None, 'data'):
        tests.extend((test_primary, test_terminal_ascii_and_grouping, test_thinking_and_prices, test_join_and_physical_order, test_snapshots, test_selection_and_destinations, test_unavailable_listing))
    if args.section in (None, 'integration'):
        tests.extend((test_html_privacy_and_determinism, test_cli_and_containment))
    failed = 0
    for test in tests:
        try:
            test()
        except Exception:
            failed += 1
            print('[FAIL] ' + test.__name__)
            traceback.print_exc(file=sys.stdout)
    print('[%s] Claude film %s: %d checks, %d failures' % ('FAIL' if failed else 'PASS', args.section or 'data/integration', len(tests), failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
