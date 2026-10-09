"""Tests for the rest of the pipeline: tokenizer, classification, images, attribution.

Run with ``python scripts/test_pipeline.py``. No network, no corpus dependency.
"""
import base64
import contextlib
import datetime
import io
import json
import os
import pathlib
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import zlib

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'plugins', 'token-counter', 'skills', 'token-report', 'scripts')
sys.path.insert(0, LIB)

# No test may reach PyPI. The two that exercise installing lift this for their own runs.
os.environ['TOKEN_COUNTER_NO_INSTALL'] = '1'

from tokencounter import analyze, classify, deps, encoding, images, latency, ledger, pricing, render, rollout, worker  # noqa: E402

RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f'\n        {detail}' if not ok else ''))


# --------------------------------------------------------------------------- tokenizer

def test_offline_tokenizer():
    """The encoding must build with networking hard-blocked, and match stock o200k_base."""
    code = (
        'import socket, sys\n'
        'class Blocked(Exception): pass\n'
        'def deny(*a, **k): raise Blocked("network access attempted")\n'
        'socket.socket = deny; socket.create_connection = deny\n'
        f'sys.path.insert(0, {LIB!r})\n'
        'from tokencounter import encoding\n'
        'enc = encoding.load()\n'
        'sample = "The quick brown fox\\n\\tjumps 1234 \\u4e2d\\u6587 \\U0001f600 x**2 # note"\n'
        'print(enc.n_vocab, len(enc.encode_ordinary(sample)),'
        ' sum(enc.encode_ordinary(sample)))\n'
    )
    p = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if p.returncode != 0:
        check('tokenizer builds offline', False, p.stderr.strip()[-400:])
        return
    vocab, n, checksum = p.stdout.split()
    check('tokenizer builds offline (network blocked)', vocab == '200019',
          f'n_vocab={vocab}')
    try:
        import tiktoken
        ref = tiktoken.get_encoding('o200k_base')
        sample = 'The quick brown fox\n\tjumps 1234 中文 \U0001f600 x**2 # note'
        ids = ref.encode_ordinary(sample)
        check('offline output identical to stock o200k_base',
              int(n) == len(ids) and int(checksum) == sum(ids),
              f'ours=({n},{checksum}) stock=({len(ids)},{sum(ids)})')
    except Exception as exc:
        check('offline output identical to stock o200k_base', True,
              f'reference unavailable: {exc.__class__.__name__} (skipped)')


def test_encoding_cached():
    a, b = encoding.load(), encoding.load()
    check('encoding is cached per process', a is b)


def test_vocabulary_read_from_the_file():
    """The vocabulary is parsed from the file on every load, never through `tiktoken.load`.

    Under tiktoken 0.7.0, the last release for Python 3.8, `tiktoken.load.read_file` raises
    for any local path unless `blobfile` is installed, so loading through it left 3.8 with no
    tokenizer.  Newer releases read through `read_file_cached`, which keeps a copy in
    `$TMPDIR/data-gym-cache` keyed by path and serves it without looking at the file again.
    """
    import tiktoken.load
    d = tempfile.mkdtemp()
    with open(encoding.vendor_path(), 'rb') as fh:
        blob = fh.read()

    def no_blobfile(path):
        # What 0.7.0's `read_file` does with a local path when `blobfile` is not installed.
        raise ImportError('blobfile is not installed. Please install it by running '
                          '`pip install blobfile`.')

    first = os.path.join(d, 'first.tiktoken')
    with open(first, 'wb') as fh:
        fh.write(blob)
    orig = tiktoken.load.read_file
    tiktoken.load.read_file = no_blobfile
    try:
        got = encoding.load(first).n_vocab
    except Exception as exc:
        got = f'{exc.__class__.__name__}: {str(exc)[:200]}'
    finally:
        tiktoken.load.read_file = orig
        encoding.load.cache_clear()
    check('the vocabulary loads where tiktoken.load cannot read a local file (tiktoken 0.7.0)',
          got == 200019, str(got))

    # One run loads it, the file is then damaged in place, and the next run must see the
    # damage.  `cache_clear` stands in for the new process.
    second = os.path.join(d, 'second.tiktoken')
    with open(second, 'wb') as fh:
        fh.write(blob)
    try:
        encoding.load(second)
    except Exception:
        pass                    # the check below still has to see the damage
    encoding.load.cache_clear()
    with open(second, 'wb') as fh:
        fh.write(b''.join(blob.splitlines(keepends=True)[:1000]))
    try:
        encoding.load(second)
        check('a vocabulary damaged in place is read again, not served from a copy', False,
              'loaded without error: the ranks came from somewhere other than the file')
    except ValueError as exc:
        check('a vocabulary damaged in place is read again, not served from a copy',
              'ranks' in str(exc), str(exc)[:200])
    finally:
        encoding.load.cache_clear()

    bad = os.path.join(d, 'bad.tiktoken')
    lines = blob.splitlines(keepends=True)
    with open(bad, 'wb') as fh:
        fh.write(b''.join(lines[:5] + [b'not-a-rank-line\n'] + lines[5:]))
    try:
        encoding.load(bad)
        check('a malformed vocabulary line is rejected, with its line number', False,
              'no exception raised')
    except ValueError as exc:
        check('a malformed vocabulary line is rejected, with its line number',
              'could not be parsed' in str(exc) and 'line 6 ' in str(exc), str(exc)[:200])
    finally:
        encoding.load.cache_clear()

    # Cut off inside the last rank: "<token> 199997" becomes "<token> 19", a rank given
    # earlier.  tiktoken panics on a repeated rank with an exception that is not an
    # Exception, so it has to be refused before tiktoken sees it.  (A Windows checkout
    # turned `blob[:-40]` in the cache-key test into exactly this.)
    cut = os.path.join(d, 'cut.tiktoken')
    token, rank = lines[-1].split()
    with open(cut, 'wb') as fh:
        fh.write(b''.join(lines[:-1]) + token + b' ' + rank[:2])
    name = 'a vocabulary cut off inside a rank is rejected, not handed to tiktoken'
    try:
        encoding.load(cut)
        check(name, False, 'no exception raised')
    except ValueError as exc:
        check(name, 'repeats' in str(exc), str(exc)[:200])
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        check(name, False, f'{exc.__class__.__name__}: {str(exc)[:200]}')
    finally:
        encoding.load.cache_clear()

    # Same ranks as tiktoken's own loader, where that loader can read a local file at all.
    keep = os.environ.get('TIKTOKEN_CACHE_DIR')
    os.environ['TIKTOKEN_CACHE_DIR'] = ''              # tiktoken: '' turns its cache off
    try:
        ref = tiktoken.load.load_tiktoken_bpe(encoding.vendor_path())
    except Exception as exc:
        ref = exc
    finally:
        if keep is None:
            os.environ.pop('TIKTOKEN_CACHE_DIR')
        else:
            os.environ['TIKTOKEN_CACHE_DIR'] = keep
    if isinstance(ref, Exception):
        check('ranks identical to tiktoken.load.load_tiktoken_bpe', True,
              f'reference unavailable: {ref.__class__.__name__} (skipped)')
    else:
        check('ranks identical to tiktoken.load.load_tiktoken_bpe',
              encoding.read_ranks(encoding.vendor_path()) == ref, 'ranks differ')


# --------------------------------------------------------------------------- images

def _png(w, h):
    ihdr = b'IHDR' + struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
    raw = (b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + ihdr
           + struct.pack('>I', zlib.crc32(ihdr)))
    return 'data:image/png;base64,' + base64.b64encode(raw + b'\x00' * 900).decode()


def _jpeg(w, h, fill=0):
    """`fill` FF bytes are inserted before SOF0: the standard allows any run of them."""
    raw = (b'\xff\xd8'
           + b'\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00' + b'\x00' * 9
           + b'\xff' * fill
           + b'\xff\xc0' + struct.pack('>H', 17) + b'\x08' + struct.pack('>HH', h, w)
           + b'\x03' + b'\x00' * 9)
    return 'data:image/jpeg;base64,' + base64.b64encode(raw + b'\x00' * 900).decode()


def test_images():
    check('PNG dimensions from prefix', images.dimensions(_png(1920, 1080)) == ('png', 1920, 1080),
          str(images.dimensions(_png(1920, 1080))))
    check('JPEG dimensions from prefix', images.dimensions(_jpeg(800, 600)) == ('jpeg', 800, 600),
          str(images.dimensions(_jpeg(800, 600))))
    # FF FF before a marker was read as marker FF with a garbage length, and the sniffer
    # jumped past the frame header into nothing.
    check('JPEG fill bytes before a marker are stepped over',
          images.dimensions(_jpeg(800, 600, fill=3)) == ('jpeg', 800, 600),
          str(images.dimensions(_jpeg(800, 600, fill=3))))
    check('unknown format is ambiguous, not zero',
          images.estimate('data:image/heic;base64,AAAA') == {
              'format': None, 'width': None, 'height': None, 'lo': 0, 'hi': None})
    e = images.estimate(_png(1024, 1024))
    check('estimate reports a range across formula families', e['lo'] < e['hi'],
          f"lo={e['lo']} hi={e['hi']}")
    check('patch formula is capped', images.patch_tokens(20000, 20000) <= 1536,
          str(images.patch_tokens(20000, 20000)))
    check('4 KiB prefix suffices for a large payload',
          images.dimensions(_png(64, 64)[:6000] ) is not None)


# --------------------------------------------------------------------------- classify

def test_classify():
    segs, _ = classify.response_item(
        {'type': 'message', 'role': 'user',
         'content': [{'type': 'input_text', 'text': 'hello'}]})
    check('user message classified', segs == [('user_message', 'hello')], str(segs))

    segs, _ = classify.response_item(
        {'type': 'function_call_output', 'call_id': 'c', 'output': 'raw string output'})
    check('function_call_output accepts a bare string',
          segs == [('tool_output', 'raw string output')], str(segs))

    segs, _ = classify.response_item(
        {'type': 'custom_tool_call_output', 'call_id': 'c',
         'output': [{'type': 'input_text', 'text': 'a'}, {'type': 'input_text', 'text': 'b'}]})
    check('custom_tool_call_output accepts a list',
          segs == [('tool_output', 'a'), ('tool_output', 'b')], str(segs))

    segs, imgs = classify.response_item(
        {'type': 'custom_tool_call_output', 'call_id': 'c',
         'output': [{'type': 'input_image', 'image_url': 'data:image/png;base64,AA'}]})
    check('images are routed out of the text path', segs == [] and len(imgs) == 1, str(segs))

    segs, _ = classify.response_item(
        {'type': 'reasoning', 'encrypted_content': 'ZZZ',
         'summary': [{'type': 'summary_text', 'text': 'thinking about it'}]})
    check('reasoning splits summary from opaque blob',
          segs == [('reasoning_summary', 'thinking about it'), ('reasoning_blob', 'ZZZ')],
          str(segs))

    segs, _ = classify.session_meta({'base_instructions': {'text': 'SYS'}})
    check('base_instructions become the system prompt',
          segs == [('system_prompt', 'SYS')], str(segs))

    segs, _ = classify.world_state({'state': {'agents_md': {'a': 'b'}, 'cwd': '/x'}})
    check('agents_md is split from the environment block',
          sorted(c for c, _ in segs) == ['agents_md', 'environment'], str(segs))

    check('assistant messages are output-side',
          classify.item_role({'type': 'message', 'role': 'assistant'}) == 'output')
    check('user messages are input-side',
          classify.item_role({'type': 'message', 'role': 'user'}) == 'input')


# --------------------------------------------------------------------------- worker

def _rec(t, payload, ts):
    return {'timestamp': f'2026-01-01T00:00:{ts:02d}.000Z', 'type': t, 'payload': payload}


def _write(d, name, recs):
    (pathlib.Path(d) / name).write_text(
        ''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in recs), encoding='utf-8')


def test_worker_attribution():
    d = tempfile.mkdtemp()
    sysmsg = 'SYSTEM ' * 200
    tool_out = 'TOOLOUTPUT ' * 500
    recs = [
        _rec('session_meta', {'session_id': 'S', 'id': 'S',
                              'base_instructions': {'text': sysmsg},
                              'cli_version': '0.155.1'}, 0),
        _rec('turn_context', {'model': 'm1', 'effort': 'max', 'turn_id': 't1'}, 1),
        _rec('response_item', {'type': 'message', 'role': 'user',
                               'content': [{'type': 'input_text', 'text': 'question one'}]}, 2),
        _rec('response_item', {'type': 'custom_tool_call', 'call_id': 'c1', 'name': 'exec',
                               'input': 'ls'}, 3),
        _rec('token_usage_record', {'response_id': 'r1', 'usage': {
            'input_tokens': 900, 'cached_input_tokens': 0, 'output_tokens': 10,
            'reasoning_output_tokens': 0, 'total_tokens': 910}}, 4),
        _rec('response_item', {'type': 'custom_tool_call_output', 'call_id': 'c1',
                               'output': [{'type': 'input_text', 'text': tool_out}]}, 5),
        _rec('response_item', {'type': 'message', 'role': 'assistant',
                               'content': [{'type': 'output_text', 'text': 'answer'}]}, 6),
        _rec('token_usage_record', {'response_id': 'r2', 'usage': {
            'input_tokens': 2400, 'cached_input_tokens': 896, 'output_tokens': 12,
            'reasoning_output_tokens': 0, 'total_tokens': 2412}}, 7),
        _rec('response_item', {'type': 'message', 'role': 'user',
                               'content': [{'type': 'input_text', 'text': 'question two'}]}, 8),
        _rec('token_usage_record', {'response_id': 'r3', 'usage': {
            'input_tokens': 2500, 'cached_input_tokens': 2432, 'output_tokens': 9,
            'reasoning_output_tokens': 0, 'total_tokens': 2509}}, 9),
    ]
    _write(d, 'rollout-a.jsonl', recs)
    r = worker.process(os.path.join(d, 'rollout-a.jsonl'))

    ct = r['cat_tokens']
    check('every expected category is attributed',
          {'system_prompt', 'user_message', 'tool_call_input',
           'tool_output', 'assistant_message'} <= set(ct), str(sorted(ct)))
    check('system prompt counted exactly once',
          r['cat_items'].get('system_prompt') == 1, str(r['cat_items']))
    check('three responses extracted', len(r['responses']) == 3, str(len(r['responses'])))

    resp = r['responses']
    check('prompt grows monotonically between responses',
          resp[0]['recon_input'] < resp[1]['recon_input'] < resp[2]['recon_input'],
          str([x['recon_input'] for x in resp]))
    check('first response has no stable prefix', resp[0]['stable_prefix'] == 0,
          str(resp[0]))
    check('later responses carry a stable prefix',
          resp[1]['stable_prefix'] > 0 and resp[2]['stable_prefix'] > 0,
          str([x['stable_prefix'] for x in resp]))
    check("a response's own output is not in its own prompt",
          resp[1]['recon_input'] >= resp[1]['stable_prefix'],
          str(resp[1]))

    hot = {h['tool']: h for h in r['hot_items'] if h['tool']}
    check('tool output is attributed to its call by call_id', 'exec' in hot, str(hot.keys()))
    check('resent items are charged per resend',
          any(h['resends'] >= 2 and h['cost'] == h['tokens'] * h['resends']
              for h in r['hot_items']), str(r['hot_items'][:2]))

    check('metrics-only path agrees on usage records',
          len(worker.metrics_only(os.path.join(d, 'rollout-a.jsonl'))['explicit']) == 3)


def test_resend_identity():
    """resend_cost must equal the summed reconstructed prompts of the charged stream.

    Each item contributes tokens x (prompts containing it); summed over items that is the
    same double sum as summing recon_input over responses.  A both-stream file records every
    response twice, which doubled resend costs for 602 of 1,952 files until this was pinned.
    """
    d = tempfile.mkdtemp()
    usage = {'input_tokens': 900, 'cached_input_tokens': 0, 'output_tokens': 10,
             'reasoning_output_tokens': 0, 'total_tokens': 910}
    recs = [_rec('session_meta', {'session_id': 'S', 'id': 'S',
                                  'base_instructions': {'text': 'SYS ' * 100}}, 0)]
    for i in range(4):
        recs.append(_rec('response_item', {'type': 'message', 'role': 'user',
                                           'content': [{'type': 'input_text',
                                                        'text': f'turn {i} ' * 60}]}, i))
        recs.append(_rec('token_usage_record', {'response_id': f'r{i}', 'usage': usage}, i))
        recs.append(_rec('event_msg', {'type': 'token_count', 'info': {
            'last_token_usage': usage,
            'total_token_usage': dict(usage, input_tokens=900 * (i + 1))}}, i))
        recs.append(_rec('response_item', {'type': 'message', 'role': 'assistant',
                                           'content': [{'type': 'output_text',
                                                        'text': 'ok ' * 20}]}, i))
    _write(d, 'rollout-both2.jsonl', recs)
    r = worker.process(os.path.join(d, 'rollout-both2.jsonl'))
    pref = 'explicit' if r['explicit'] else 'legacy'
    want = sum(x['recon_input'] for x in r['responses'] if x['stream'] == pref)
    check('resend cost equals summed reconstructed prompts (both-stream)',
          r['resend_cost'] == want, f"resend_cost={r['resend_cost']} recon={want}")
    check('a both-stream file does not double its resend counts',
          all(h['resends'] <= 4 for h in r['hot_items']),
          str([h['resends'] for h in r['hot_items']]))


def test_stable_prefix_baseline():
    """The baseline is the previous REQUEST's prompt, not the prompt plus its own output.

    Round-6 counterexample: a 20-token request, a 100-token assistant reply, then a 10-token
    input reported a 120-token stable prefix. The provider never saw a 120-token prompt.

    The reply is written *before* the response's usage record, as Codex writes it.  The first
    version of this test wrote it after, so the reply was never a trailing output run, the
    fold this is about never happened, and the test passed with its fix reverted.  The
    both-stream variant pins the second defect: the legacy mirror that follows each explicit
    record moved the baseline *after* the fold, so every both-stream file carried the round-6
    figure while this explicit-only case stayed green.
    """
    u = {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1,
         'reasoning_output_tokens': 0, 'total_tokens': 2}

    def msg(role, text, ts):
        kind = 'output_text' if role == 'assistant' else 'input_text'
        return _rec('response_item', {'type': 'message', 'role': role,
                                      'content': [{'type': kind, 'text': text}]}, ts)

    def mirror(ts, cum):
        return _rec('event_msg', {'type': 'token_count', 'info': {
            'last_token_usage': u, 'total_token_usage': dict(u, input_tokens=cum)}}, ts)

    for both in (False, True):
        d = tempfile.mkdtemp()
        recs = [_rec('session_meta', {'session_id': 'S', 'id': 'S'}, 0),
                msg('user', 'aa ' * 200, 1), msg('assistant', 'bb ' * 800, 2),
                _rec('token_usage_record', {'response_id': 'r1', 'usage': u}, 3)]
        if both:
            recs.append(mirror(3, 1))
        recs += [msg('user', 'cc ' * 40, 4), msg('assistant', 'dd ' * 5, 5),
                 _rec('token_usage_record', {'response_id': 'r2', 'usage': u}, 6)]
        if both:
            recs.append(mirror(6, 2))
        _write(d, 'rollout-sp.jsonl', recs)
        r = worker.process(os.path.join(d, 'rollout-sp.jsonl'))
        first, second = [x for x in r['responses'] if x['stream'] == 'explicit'][:2]
        tag = 'both-stream' if both else 'explicit-only'
        check(f'stable prefix equals the previous request, not request + its output ({tag})',
              second['stable_prefix'] == first['recon_input'],
              f"stable={second['stable_prefix']} prev_request={first['recon_input']} "
              f"next_prompt={second['recon_input']}")
        check(f'the assistant reply is still part of the next prompt ({tag})',
              second['recon_input'] > first['recon_input'] + 20,
              f"{first['recon_input']} -> {second['recon_input']}")


def test_compaction_segments():
    """Identical content either side of a compaction must not look like a stable prefix."""
    d = tempfile.mkdtemp()
    u = {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1,
         'reasoning_output_tokens': 0, 'total_tokens': 2}
    msg = {'type': 'message', 'role': 'user',
           'content': [{'type': 'input_text', 'text': 'same text ' * 50}]}
    recs = [
        _rec('session_meta', {'session_id': 'S', 'id': 'S'}, 0),
        _rec('response_item', msg, 1),
        _rec('token_usage_record', {'response_id': 'r1', 'usage': u}, 2),
        _rec('compacted', {'replacement_history': [msg]}, 3),
        _rec('token_usage_record', {'response_id': 'r2', 'usage': u}, 4),
    ]
    _write(d, 'rollout-cp.jsonl', recs)
    r = worker.process(os.path.join(d, 'rollout-cp.jsonl'))
    after = r['responses'][1]
    check('a compacted prompt reports no stable prefix',
          after['stable_prefix'] == 0, str(after))


def test_windowed_ledger_scope():
    """A window must not hide the ancestors a fork child replays."""
    d = tempfile.mkdtemp()

    def snap(ts, inp, cum, day):
        return {'timestamp': f'2026-0{day}-01T00:00:{ts:02d}.000Z', 'type': 'event_msg',
                'payload': {'type': 'token_count', 'info': {
                    'last_token_usage': {'input_tokens': inp, 'cached_input_tokens': 0,
                                         'output_tokens': 1, 'reasoning_output_tokens': 0,
                                         'total_tokens': inp + 1},
                    'total_token_usage': {'input_tokens': cum, 'cached_input_tokens': 0,
                                          'output_tokens': 1, 'reasoning_output_tokens': 0,
                                          'total_tokens': cum + 1}}}}

    def smeta(tid, day, parent=None):
        pl = {'session_id': 'S', 'id': tid,
              'timestamp': f'2026-0{day}-01T00:00:00.000Z'}
        if parent:
            pl['parent_thread_id'] = parent
        return {'timestamp': f'2026-0{day}-01T00:00:00.000Z',
                'type': 'session_meta', 'payload': pl}

    os.makedirs(os.path.join(d, '2026', '01', '01'), exist_ok=True)
    os.makedirs(os.path.join(d, '2026', '02', '01'), exist_ok=True)
    _write(os.path.join(d, '2026', '01', '01'), 'rollout-2026-01-01T00-00-00-p.jsonl',
           [smeta('p', 1), snap(1, 100, 100, 1), snap(2, 200, 300, 1)])
    _write(os.path.join(d, '2026', '02', '01'), 'rollout-2026-02-01T00-00-00-c.jsonl',
           [smeta('c', 2, parent='p'), snap(1, 100, 100, 2), snap(2, 200, 300, 2),
            snap(3, 300, 600, 2)])

    all_paths = rollout.discover(d)
    window = set(rollout.discover(d, since='2026-02-01'))
    data = {p: worker.metrics_only(p) for p in all_paths}

    _, full = ledger.build(data, scope=window)
    naive_data = {p: data[p] for p in window}
    _, naive = ledger.build(naive_data)
    check('a windowed report excludes inherited history the window cannot see',
          full['responses'] == 1 and full['inherited'] == 2,
          f"responses={full['responses']} inherited={full['inherited']}")
    check('filtering before the ledger would have overcharged it',
          naive['responses'] == 3 and naive['inherited'] == 0,
          f"naive responses={naive['responses']}")


def test_corrupt_record_counted():
    """A complete but unparseable record is a lost record, and must be counted as one."""
    d = tempfile.mkdtemp()
    good = _rec('session_meta', {'session_id': 'S', 'id': 'S'}, 0)
    usage = _rec('token_usage_record', {'response_id': 'r1', 'usage': {
        'input_tokens': 200, 'cached_input_tokens': 0, 'output_tokens': 1,
        'reasoning_output_tokens': 0, 'total_tokens': 201}}, 2)
    p = pathlib.Path(d) / 'rollout-corrupt.jsonl'
    p.write_text(
        json.dumps(good, separators=(',', ':')) + '\n'
        + '{"type":"token_usage_record","payload":{"usage":{"input_tokens":100,\n'
        + json.dumps(usage, separators=(',', ':')) + '\n', encoding='utf-8')
    r = worker.metrics_only(str(p))
    check('a corrupt usage record is counted, not silently lost',
          r['counters'].get('unparseable_records') == 1
          and r['counters'].get('unparseable_usage_records') == 1,
          str(r['counters']))
    check('surviving records are still charged', len(r['explicit']) == 1, str(r['explicit']))


def test_extractor_version_tracks_source():
    """The cache key must move when anything that shapes a payload moves."""
    sys.path.insert(0, os.path.join(LIB))
    import importlib
    rp = importlib.import_module('report')
    v1 = rp._extractor_version()
    src = os.path.join(LIB, 'tokencounter', 'worker.py')
    original = open(src, 'rb').read()
    try:
        open(src, 'ab').write(b'\n# cache-key probe\n')
        v2 = rp._extractor_version()
    finally:
        open(src, 'wb').write(original)
    check('the cache key is derived from the extraction source', v1 != v2, f'{v1} vs {v2}')
    check('reverting the source restores the key', rp._extractor_version() == v1)

    # The vocabulary decides the token counts, so it is part of the payload's shape.
    d = tempfile.mkdtemp()
    alt = os.path.join(d, 'alt.tiktoken')
    real = encoding.vendor_path()
    with open(real, 'rb') as fh:
        blob = fh.read()
    with open(alt, 'wb') as fh:
        fh.write(blob)
    check('the same vocabulary at a different path keeps the cache key',
          rp._extractor_version(alt) == v1, 'identical contents should not force a rebuild')
    alt2 = os.path.join(d, 'alt2.tiktoken')
    with open(alt2, 'wb') as fh:
        fh.write(blob[:-40])
    try:
        v3 = rp._extractor_version(alt2)
    except Exception:
        v3 = 0
    check('a damaged vocabulary never reuses the cache',
          v3 != rp._extractor_version(alt), 'same key for different vocab contents')

    # The tokenizer implementation matters as much as the vocabulary: a same-version wheel
    # swap changes counts while __version__ and __file__ stay identical.  The key is a
    # behavioural fingerprint, so it moves when the tokenizer's output moves.
    import tiktoken
    real = tiktoken.Encoding.encode_ordinary
    encoding.load.cache_clear()
    try:
        tiktoken.Encoding.encode_ordinary = lambda self, t: [999] * (len(t) // 7 + 1)
        v_patched = rp._extractor_version()
    finally:
        tiktoken.Encoding.encode_ordinary = real
        encoding.load.cache_clear()
    check('a tokenizer that returns different ids changes the cache key',
          v_patched != v1, f'{v1} vs {v_patched}')
    check('restoring the tokenizer restores the key', rp._extractor_version() == v1)


def _snap_line(ts, inp, cum, day):
    return json.dumps({
        'timestamp': f'2026-0{day}-01T00:00:{ts:02d}.000Z', 'type': 'event_msg',
        'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': inp, 'cached_input_tokens': 0,
                                 'output_tokens': 1, 'reasoning_output_tokens': 0,
                                 'total_tokens': inp + 1},
            'total_token_usage': {'input_tokens': cum, 'cached_input_tokens': 0,
                                  'output_tokens': 1, 'reasoning_output_tokens': 0,
                                  'total_tokens': cum + 1}}}}, separators=(',', ':'))


def _meta_line(tid, day, parent=None):
    pl = {'session_id': 'S', 'id': tid, 'timestamp': f'2026-0{day}-01T00:00:00.000Z'}
    if parent:
        pl['parent_thread_id'] = parent
    return json.dumps({'timestamp': f'2026-0{day}-01T00:00:00.000Z',
                       'type': 'session_meta', 'payload': pl}, separators=(',', ':'))


def _corpus_with_corrupt_ancestor(corrupt):
    """Parent (out of window) with A, B, C; child (in window) replaying all three."""
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, '2026', '01', '01'), exist_ok=True)
    os.makedirs(os.path.join(d, '2026', '02', '01'), exist_ok=True)
    b = _snap_line(2, 200, 300, 1)
    if corrupt:
        # Object-shaped, complete, and damaged: the kind a byte prefilter cannot see.
        b = ('{"timestamp":"2026-01-01T00:00:02.000Z","typo":"event_msg","payload":'
             '{"typo":"t0ken_count","info":{"last_token_usage":{"input_tokens":200}}},BROKEN}')
    (pathlib.Path(d) / '2026' / '01' / '01' / 'rollout-2026-01-01T00-00-00-p.jsonl').write_text(
        '\n'.join([_meta_line('p', 1), _snap_line(1, 100, 100, 1), b,
                   _snap_line(3, 300, 600, 1)]) + '\n', encoding='utf-8')
    (pathlib.Path(d) / '2026' / '02' / '01' / 'rollout-2026-02-01T00-00-00-c.jsonl').write_text(
        '\n'.join([_meta_line('c', 2, parent='p'), _snap_line(1, 100, 100, 2),
                   _snap_line(2, 200, 300, 2), _snap_line(3, 300, 600, 2)]) + '\n',
        encoding='utf-8')
    return d


def _run_main(argv):
    """Drive report.main() the way the CLI does, and return (model, html)."""
    sys.path.insert(0, LIB)
    import importlib
    rp = importlib.import_module('report')
    out = tempfile.mkdtemp()
    jpath = os.path.join(out, 'm.json')
    hpath = os.path.join(out, 'r.html')
    rc = rp.main(list(argv) + ['--no-cache', '--no-open', '--quiet', '--procs', '1',
                               '--json', jpath, '--out', hpath])
    if rc != 0:
        return rc, None, None
    with open(jpath, encoding='utf-8') as fh:
        model = json.load(fh)
    with open(hpath, encoding='utf-8') as fh:
        return rc, model, fh.read()


def test_damage_outside_window_reaches_the_report():
    """Drives the real `report.main()`, not the helper underneath it.

    The previous version of this test called `report.damage_outside` directly. That passed
    even with the production call site left *below* the window filter, where its own guard is
    unreachable -- which is precisely the bug it was named for. Reverting the call site must
    make this fail, so the test has to go through `main`.
    """
    for corrupt in (False, True):
        d = _corpus_with_corrupt_ancestor(corrupt)
        rc, model, html = _run_main(['--sessions-root', d, '--since', '2026-02-01'])
        check(f'report.main succeeds (corrupt={corrupt})', rc == 0, f'exit {rc}')
        if model is None:
            return
        q = model['quality']
        if corrupt:
            check('a corrupt out-of-window file reaches the window report through main()',
                  q.get('damage_outside_window') == 1
                  and q.get('unparseable_records') == 1, str(dict(q)))
            # `unparseable_usage_records` is a subset of `unparseable_records`; summing both
            # into the headline counted one damaged line as two. This fixture's type strings
            # are deliberately mangled, so it is not usage-recognisable and the detail
            # counter stays 0 -- the headline must still read 1.
            check('one damaged record counts once, not once per counter',
                  q.get('damage_outside_window') == 1
                  and q.get('unparseable_records') == 1
                  and not q.get('unparseable_usage_records'), str(dict(q)))
            check('losing the ancestor record makes the window charge the replay',
                  model['totals']['responses'] == 3, str(model['totals']['responses']))
            check('the damage counter is carried out of the analysis',
                  model['quality']['damage_outside_window'] == 1 and '<html' in html,
                  str(model['quality'].get('damage_outside_window')))
        else:
            check('a clean ancestor reports no damage and the replay is excluded',
                  not q.get('damage_outside_window')
                  and model['totals']['responses'] == 0,
                  f"damage={q.get('damage_outside_window')} "
                  f"responses={model['totals']['responses']}")
            # Zero damage and damage-not-looked-for are different statements, and the
            # counter has to distinguish them whether or not a page displays it.
            check('the damage counter is carried even at zero',
                  model['quality']['damage_outside_window'] == 0 and '<html' in html,
                  str(model['quality'].get('damage_outside_window')))


def test_object_shaped_corruption_is_visible():
    """A complete, object-shaped line with damaged type strings must not vanish."""
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / 'rollout-s.jsonl'
    p.write_text(
        _meta_line('S', 1) + '\n'
        '{"timestamp":"x","typo":"event_msg","payload":{"typo":"t0ken_count"},BROKEN}\n',
        encoding='utf-8')
    m = worker.metrics_only(str(p))
    f = worker.process(str(p))
    check('the ledger-only path sees object-shaped corruption',
          m['counters'].get('unparseable_records') == 1, str(m['counters']))
    check('both paths agree on the damage count',
          m['counters'].get('unparseable_records')
          == f['counters'].get('unparseable_records'),
          f"metrics={m['counters']} full={f['counters']}")


def test_replay_mode_is_labelled():
    """With the exclusion off, the report must not say records were dropped."""
    d = _corpus_with_corrupt_ancestor(corrupt=False)
    paths = rollout.discover(d)
    results = {p: worker.process(p) for p in paths}
    out = {}
    for excl in (True, False):
        charged, counters = ledger.build(results, exclude_replay=excl)
        model = analyze.analyze(results, charged, counters,
                                scope={'label': 't', 'replay_excluded': excl},
                                extra_quality={'replay_exclusion_applied': 1 if excl else 0})
        out[excl] = (model, render.render(model))
    mex, hex_ = out[True]
    mch, hch = out[False]
    check('disabling the exclusion charges the replayed history',
          mch['totals']['responses'] > mex['totals']['responses'],
          f"{mex['totals']['responses']} -> {mch['totals']['responses']}")
    check('the charged mode does not claim records were dropped',
          mch['counters'].get('inherited', 0) == 0
          and mch['counters'].get('inherited_charged', 0) > 0,
          str({k: v for k, v in mch['counters'].items() if 'inherit' in k}))
    # The mode used to be printed on the page; the page is a dashboard now, so the flag
    # rides the model and the stdout summary.  It still has to be unambiguous there: which
    # mode produced a report conditions every count in it.
    check('the model states which replay mode produced it',
          mch['scope']['replay_excluded'] is False
          and mex['scope']['replay_excluded'] is True,
          f"charged={mch['scope']} excluded={mex['scope']}")
    check('the mode counter is carried even at zero',
          mch['quality']['replay_exclusion_applied'] == 0
          and mex['quality']['replay_exclusion_applied'] == 1,
          f"{mch['quality'].get('replay_exclusion_applied')} / "
          f"{mex['quality'].get('replay_exclusion_applied')}")
    check('both modes still render', '<html' in hch and '<html' in hex_)


def test_worker_double_count():
    """A file carrying both usage streams must charge exactly one of them."""
    d = tempfile.mkdtemp()
    usage = {'input_tokens': 1000, 'cached_input_tokens': 0, 'output_tokens': 10,
             'reasoning_output_tokens': 0, 'total_tokens': 1010}
    recs = [_rec('session_meta', {'session_id': 'S', 'id': 'S'}, 0)]
    for i in range(3):
        recs.append(_rec('token_usage_record', {'response_id': f'r{i}', 'usage': usage}, i + 1))
        recs.append(_rec('event_msg', {'type': 'token_count', 'info': {
            'last_token_usage': usage,
            'total_token_usage': {'input_tokens': 1000 * (i + 1), 'cached_input_tokens': 0,
                                  'output_tokens': 10, 'reasoning_output_tokens': 0,
                                  'total_tokens': 1000 * (i + 1) + 10}}}, i + 1))
    _write(d, 'rollout-both.jsonl', recs)
    paths = rollout.discover(d)
    data = {p: worker.metrics_only(p) for p in paths}
    _, c = ledger.build(data)
    check('both-stream file is charged once, not summed',
          c['responses'] == 3 and c['input'] == 3000,
          f"responses={c['responses']} input={c['input']}")


def test_truncated_line():
    """A rollout read mid-write must not yield a partial record."""
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / 'rollout-t.jsonl'
    good = json.dumps(_rec('session_meta', {'session_id': 'S', 'id': 'S'}, 0))
    p.write_text(good + '\n' + '{"type":"token_usage_record","payl', encoding='utf-8')
    r = worker.metrics_only(str(p))
    check('a trailing partial line is withheld',
          r['session_id'] == 'S' and r['explicit'] == [], str(r['explicit']))


# --------------------------------------------------------------------------- report

def test_render():
    d = tempfile.mkdtemp()
    recs = [
        _rec('session_meta', {'session_id': 'S', 'id': 'S', 'cwd': '/w',
                              'base_instructions': {'text': 'SYS ' * 50}}, 0),
        _rec('turn_context', {'model': 'm1', 'effort': 'max'}, 1),
        _rec('response_item', {'type': 'message', 'role': 'user',
                               'content': [{'type': 'input_text', 'text': 'hi ' * 40}]}, 2),
        _rec('token_usage_record', {'response_id': 'r1', 'usage': {
            'input_tokens': 500, 'cached_input_tokens': 128, 'output_tokens': 10,
            'reasoning_output_tokens': 4, 'total_tokens': 510}}, 3),
    ]
    _write(d, 'rollout-r.jsonl', recs)
    paths = rollout.discover(d)
    data = {p: worker.process(p) for p in paths}
    charged, counters = ledger.build(data)
    model = analyze.analyze(data, charged, counters, scope={'label': 'test'})
    html = render.render(model)
    check('report renders', '<html' in html and '</html>' in html, f'{len(html)} bytes')

    # Rollout content still reaches the page: a model name comes from `turn_context`
    # and is drawn in the daily chart's legend and tooltips.
    hostile = '</script><img src=x onerror=alert(1)> &"<'
    model2 = dict(model)
    model2['models'] = [dict(model['models'][0], model=hostile)]
    # Both splits: the chart draws the tiktoken one when the run counted input (§5.7).
    model2['daily'] = [dict(d, models={hostile: d['input']},
                            tiktoken_models={hostile: d['tiktoken_input']})
                       for d in model['daily']]
    h2 = render.render(model2)
    check('hostile rollout content cannot close the script block',
          '</script><img' not in h2 and h2.count('</script>') == 2,
          f"closers={h2.count('</script>')}")
    check('hostile rollout content is escaped in the chart legend',
          'onerror=alert' not in h2 or '&lt;img' in h2)
    # The brand in the top bar links to the site; a link the reader follows is not a fetch.
    offline = html.replace('<a href="https://tokenusage.dev">', '', 1)
    check('report is self-contained (no external fetches)',
          'http://' not in offline and 'https://' not in offline
          and 'src="//' not in offline)
    check('report embeds the limit series it draws', '__TC__' in html)
    check('the page opens in Clinical and carries a rule for every other style',
          render.STYLES[0][0] == 'clinical' and 'data-style="clinical"' in html
          and all(f'[data-style="{s}"]' in html for s, _ in render.STYLES[1:]))
    # The collage is drawn from a fixed seed: two renders of one model are the same page.
    # Clinical's palette is the original chart palette, and the WebGL layer reads it from these
    # variables rather than carrying colours of its own: pin them, light and dark.
    light = ('--c0:#2563eb;--c1:#7c3aed;--c2:#db2777;--c3:#ea580c;--c4:#ca8a04;--c5:#16a34a;'
             '--c6:#0891b2;--c7:#4f46e5;--c8:#9333ea;--c9:#e11d48;--c10:#65a30d;--c11:#0d9488;'
             '--c12:#a16207;--c13:#475569;')
    dark = ('--c0:#60a5fa;--c1:#a78bfa;--c2:#f472b6;--c3:#fb923c;--c4:#fbbf24;--c5:#4ade80;'
            '--c6:#22d3ee;--c7:#818cf8;--c8:#c084fc;--c9:#fb7185;--c10:#a3e635;--c11:#2dd4bf;'
            '--c12:#d6b45b;--c13:#94a3b8;')
    flat = html.replace(' ', '').replace('\n', '')
    check('Clinical keeps the original chart palette, light and dark',
          light in flat and dark in flat
          and '--cached:#93b4f5;--uncached:#2563eb;--out:#10b981;' in flat
          and '--cached:#2b4270;--uncached:#60a5fa;--out:#34d399;' in flat)
    check('the Matisse collage is the same on every render',
          '<div class="mz">' in html and render.render(model) == html)
    check('Clinical paints its marks in WebGL',
          'clinical' in render.GL_STYLES
          and '"gl_styles":["clinical"]' in html and 'class="mk"' in html)
    check('Nocturne is a style, and the one that is a 3D scene',
          [s for s, _ in render.STYLES] == ['clinical', 'matisse', 'nocturne']
          and render.SCENE_STYLES == ['nocturne'] and '"scene_styles":["nocturne"]' in html
          and 'const S3D' in html and 'const N3' in html)
    # Nocturne's categorical palette was validated as a set on its own plinth (render.py,
    # STYLE_CSS): a slot changed alone can undo that without any other test noticing.
    noct = ('--c0:#c1821f;--c1:#4174c7;--c2:#b04466;--c3:#14a685;--c4:#a55cc0;--c5:#4f9a5c;'
            '--c6:#8f76cc;--c7:#5f9234;--c8:#6a78d6;--c9:#878c22;--c10:#7a86e0;--c11:#c96a22;'
            '--c12:#3f86c8;--c13:#6f8290;')
    check('Nocturne keeps the palette it was validated with', noct in flat)
    check('there is no effect switcher: the marks are always drawn plain',
          not hasattr(render, 'FX') and 'fxbtn' not in html and 'tc-fx' not in html
          and '"fx":' not in html)
    check('a page with no limit snapshots says so rather than drawing an empty chart',
          'No rate-limit snapshots in range' in html)
    check('totals reach the page', '500' in html or '510' in html)


# ------------------------------------------------------------------------- environment

def test_environment_degrades():
    """Everything this tool needs from the machine can be missing or broken.

    None of it may take the run with it: the index is an optimisation, the vocabulary is
    only needed for tokenization, and an empty corpus is a setup problem the user has to be
    told how to fix, not a traceback.
    """
    import contextlib
    import io

    import report as cli
    from tokencounter import index as idx

    d = tempfile.mkdtemp()

    bad = os.path.join(d, 'corrupt.db')
    with open(bad, 'wb') as fh:
        fh.write(b'this is not a database' * 200)
    cache, why = idx.try_open(bad)
    check('a corrupt index degrades to no index', cache is None and bool(why), str(why))

    with open(os.path.join(d, 'afile'), 'w') as fh:
        fh.write('x')
    cache, why = idx.try_open(os.path.join(d, 'afile', 'index.db'))
    check('an unusable index location degrades to no index',
          cache is None and bool(why), str(why))

    cache, why = idx.try_open(os.path.join(d, 'fresh.db'))
    check('a usable index still opens', cache is not None and why is None, str(why))
    if cache:
        cache.close()

    # An empty corpus: the message has to name where it looked and how to look elsewhere,
    # because the usual cause is a relocated CODEX_HOME.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = cli.main(['--sessions-root', os.path.join(d, 'nothing-here'),
                       '--no-open', '--quiet'])
    msg = err.getvalue()
    check('an empty corpus exits cleanly and says where it looked',
          rc == 2 and 'CODEX_HOME' in msg and '--sessions-root' in msg,
          f'rc={rc} msg={msg!r}')

    # Tokenization is the only thing that needs the vocabulary, so the ledger-only path has
    # to survive its absence -- that is the fallback when a plugin copy ships without it.
    corpus, *_ = _rl_corpus()
    out = os.path.join(d, 'r.html')
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        rc = cli.main(['--sessions-root', corpus, '--metrics-only', '--no-open',
                       '--quiet', '--no-account', '--out', out,
                       '--vocab', os.path.join(d, 'no-such-vocab.tiktoken')])
    check('the ledger-only path runs with no vocabulary at all',
          rc == 0 and os.path.exists(out) and os.path.getsize(out) > 2000,
          f'rc={rc} err={err.getvalue()!r}')

    # Without --metrics-only and without a tokenizer, the run must still produce a page,
    # and the page must say why one panel is empty: an empty pie and an uncountable pie
    # look identical otherwise.
    soft = os.path.join(d, 'soft.html')
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        rc = cli.main(['--sessions-root', corpus, '--no-open', '--quiet', '--no-account',
                       '--out', soft, '--vocab', os.path.join(d, 'no-such-vocab.tiktoken')])
    page = open(soft, encoding='utf-8').read() if os.path.exists(soft) else ''
    check('a missing tokenizer costs one panel, not the run',
          rc == 0 and page.count('class="tile"') == 6 and 'Not counted:' in page
          and 'rlchart' in page,
          f'rc={rc} tiles={page.count(chr(34).join(["class=", "tile", ""]))}')

    # --doctor reports the setup instead of assuming it.
    home = os.path.join(d, 'home')
    os.makedirs(home, exist_ok=True)
    keep, cached = os.environ.get('CODEX_HOME'), list(cli._OUT_DIR)
    os.environ['CODEX_HOME'] = home
    cli._OUT_DIR.clear()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli.main(['--doctor', '--sessions-root', os.path.join(d, 'nothing-here')])
    finally:
        cli._OUT_DIR[:] = cached
        if keep is None:
            os.environ.pop('CODEX_HOME', None)
        else:
            os.environ['CODEX_HOME'] = keep
    text = buf.getvalue()
    check('--doctor names the resolved paths and fails loudly on an empty corpus',
          rc == 1 and 'sessions root' in text and 'rollout files' in text and '!' in text,
          f'rc={rc}\n{text}')


# --------------------------------------------------------------------------- installing tiktoken

def test_report_installs_missing_tiktoken():
    """A run that tokenizes asks for tiktoken to be installed beside its index; a run that
    cannot use it, or was told not to install, never asks; a failed install costs the panel.

    `deps.ensure` is replaced with a recorder, so nothing is installed and nothing leaves
    the machine. The install itself is `test_tiktoken_installs_itself`.
    """
    root, home, _ = _indexed_corpus([('019bbbbb-0000-7000-8000-000000000001', 3)])
    calls = []
    keep_ensure, keep_load = deps.ensure, encoding.load
    keep_env = os.environ.pop(deps.ENV_OFF, None)

    def run(cli, *extra):
        calls.clear()
        out = os.path.join(home, 'm.json')
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(['--sessions-root', root, '--no-open', '--no-account', '--quiet',
                           '--no-cache', '--json', out, *extra])
        with open(out, encoding='utf-8') as fh:
            return rc, list(calls), json.load(fh), err.getvalue()

    try:
        deps.ensure = lambda r: calls.append(r)
        with _codex_home(home) as cli:
            rc, got, _, _ = run(cli)
            check('a run that tokenizes installs tiktoken, beside the index',
                  rc == 0 and got == [cli.out_dir()], f'rc={rc} calls={got}')
            for extra, why in ((['--no-install'], '--no-install'),
                               (['--metrics-only'], '--metrics-only, which never tokenizes'),
                               (['--vocab', os.path.join(home, 'none.tiktoken')],
                                'no vocabulary, which no install fixes')):
                rc, got, _, _ = run(cli, *extra)
                check(f'no install is attempted with {why}', rc == 0 and got == [],
                      f'rc={rc} calls={got}')
            os.environ[deps.ENV_OFF] = '1'
            rc, got, _, _ = run(cli)
            os.environ.pop(deps.ENV_OFF)
            check('no install is attempted with TOKEN_COUNTER_NO_INSTALL=1',
                  rc == 0 and got == [], f'rc={rc} calls={got}')

            reason = 'tiktoken could not be installed: PyPI is unreachable (no network access?)'
            deps.ensure = lambda r: reason

            def missing(path=None):
                raise ImportError('tiktoken is required but not importable. Install it with:\n'
                                  '    python -m pip install tiktoken')
            encoding.load = missing
            rc, _, model, err = run(cli)
            sc = model['scope']
            check('a failed install costs one panel, and the page says why',
                  rc == 0 and sc['metrics_only'] and sc['tokenizer_note'] == reason + '.',
                  f'rc={rc} scope={sc}')
            check('a failed install says the next run tries again',
                  'the next run tries again' in err, err)

            rc, _, model, _ = run(cli, '--no-install')
            note = model['scope']['tokenizer_note'] or ''
            check('without an install, the note is a sentence that says how to get it',
                  rc == 0 and note.endswith('installs it.') and 'with:' not in note, note)
    finally:
        deps.ensure, encoding.load = keep_ensure, keep_load
        os.environ[deps.ENV_OFF] = keep_env if keep_env is not None else '1'


def test_install_edge_cases():
    """The corners of `deps` a real install rarely reaches."""
    import report as cli

    # pip writes in the console code page, uv in UTF-8: neither may raise while decoding.
    p, why = deps._call([sys.executable, '-c',
                         'import sys; sys.stdout.buffer.write(b"ok \\xff\\xfe\\x81 done")'],
                        dict(os.environ))
    check('installer output that is not valid text is decoded, not raised',
          why is None and p.stdout.startswith('ok ') and '\ufffd' in p.stdout, repr(why))

    real = deps.sysconfig.get_config_var
    deps.sysconfig.get_config_var = lambda k: 1 if k == 'Py_GIL_DISABLED' else real(k)
    try:
        ft = deps.tag()
    finally:
        deps.sysconfig.get_config_var = real
    check('a free-threaded build gets an install directory of its own',
          ft.split('-')[1].endswith('t'), ft)

    # A broken install this process cannot move (Windows, an extension it loaded) is
    # reported before anything is downloaded, so the next run does not download again.
    target = deps.lib_dir(tempfile.mkdtemp())
    os.makedirs(os.path.join(target, 'tiktoken'))
    calls, rename, installer = [], os.rename, deps._run_installer

    def held(a, b):
        if os.path.normpath(a) == os.path.normpath(target):
            raise PermissionError('in use')
        return rename(a, b)
    os.rename, deps._run_installer = held, lambda stage: calls.append(stage)
    try:
        why = deps._install(target)
    finally:
        os.rename, deps._run_installer = rename, installer
    check('an install that cannot be moved aside is reported before any download',
          calls == [] and 'delete that directory' in (why or '')
          and os.path.isdir(os.path.join(target, 'tiktoken')), repr(why))

    # The report writes where deps looks: one list, not two copies of it.
    d = tempfile.mkdtemp()
    ours = [os.path.join(d, 'home'), os.path.join(d, 'tmp')]
    keep_roots, keep_out = deps.roots, list(cli._OUT_DIR)
    deps.roots = lambda: list(ours)
    cli._OUT_DIR.clear()
    try:
        got = cli.out_dir()
    finally:
        deps.roots = keep_roots
        cli._OUT_DIR[:] = keep_out
    check('the report writes to the directory the install is looked for in',
          got == ours[0], got)


def _fake_tiktoken_wheel(d, version='99.0.0'):
    """A pure-Python wheel named tiktoken, for pip to install with no network."""
    import zipfile
    info = f'tiktoken-{version}.dist-info'
    files = {
        'tiktoken/__init__.py': f'__version__ = {version!r}\n',
        f'{info}/METADATA': f'Metadata-Version: 2.1\nName: tiktoken\nVersion: {version}\n',
        f'{info}/WHEEL': ('Wheel-Version: 1.0\nGenerator: test_pipeline\n'
                          'Root-Is-Purelib: true\nTag: py3-none-any\n'),
    }
    files[f'{info}/RECORD'] = ''.join(f'{k},,\n' for k in files) + f'{info}/RECORD,,\n'
    os.makedirs(d, exist_ok=True)
    with zipfile.ZipFile(os.path.join(d, f'tiktoken-{version}-py3-none-any.whl'), 'w') as z:
        for k, v in files.items():
            z.writestr(k, v)


def test_tiktoken_installs_itself():
    """`deps.ensure` really installs, once, into CODEX_HOME, and never into the interpreter.

    Each run is a fresh interpreter started with ``-I -S``, so a tiktoken already installed
    on this machine is invisible to it, and pip is pointed at a local wheel instead of PyPI.
    """
    d = tempfile.mkdtemp()
    wheels, empty = os.path.join(d, 'wheels'), os.path.join(d, 'empty')
    _fake_tiktoken_wheel(wheels)
    os.makedirs(empty)
    code = (
        'import json, sys\n'
        f'sys.path.insert(0, {LIB!r})\n'
        'from tokencounter import deps\n'
        'why = deps.ensure(deps.roots()[0])\n'
        'try:\n'
        '    import tiktoken\n'
        '    got = [tiktoken.__version__, tiktoken.__file__]\n'
        'except ImportError:\n'
        '    got = None\n'
        'print(json.dumps({"why": why, "got": got}))\n'
    )

    def run(home, links, **env):
        e = dict(os.environ, CODEX_HOME=home, PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX='1',
                 PIP_FIND_LINKS=links, PIP_NO_CACHE_DIR='1')
        e.pop(deps.ENV_OFF, None)
        e.update(env)
        p = subprocess.run([sys.executable, '-I', '-S', '-c', code], env=e,
                           capture_output=True, text=True, timeout=300)
        try:
            return json.loads(p.stdout.strip().splitlines()[-1]), p.stderr
        except (ValueError, IndexError):
            return {'why': f'rc={p.returncode} {p.stderr[-300:]}', 'got': None}, p.stderr

    home = os.path.join(d, 'home')
    lib = deps.lib_dir(os.path.join(home, 'token-counter'))
    r, err = run(home, wheels)
    check('a missing tiktoken is installed into CODEX_HOME/token-counter/lib/<interpreter>',
          r['why'] is None and r['got'] and r['got'][0] == '99.0.0'
          and os.path.dirname(os.path.dirname(r['got'][1])) == lib
          and 'installing it' in err, f'{r} {err[-300:]}')

    # With nothing for pip to find, a second install could only fail: the run must use the
    # first one without starting pip at all.
    r, err = run(home, empty)
    check('a later run reuses the install without running pip',
          r['why'] is None and r['got'] and r['got'][0] == '99.0.0'
          and 'installing' not in err, f'{r} {err[-300:]}')

    # OSError, not ImportError: what an extension built for another interpreter raises.
    with open(os.path.join(lib, 'tiktoken', '__init__.py'), 'w') as fh:
        fh.write('raise OSError("an extension built for another interpreter")\n')
    doc = subprocess.run([sys.executable, '-I', '-S', os.path.join(LIB, 'report.py'), '--doctor',
                          '--sessions-root', empty],
                         env=dict(os.environ, CODEX_HOME=home), capture_output=True, text=True,
                         timeout=120)
    check('--doctor reports a tiktoken that raises on import, instead of crashing on it',
          doc.returncode == 1 and '! tiktoken' in doc.stdout and 'Traceback' not in doc.stderr,
          f'rc={doc.returncode}\n{doc.stdout[-600:]}\n{doc.stderr[-600:]}')
    r, err = run(home, wheels)
    check('an install that no longer imports is replaced',
          r['why'] is None and r['got'] and r['got'][0] == '99.0.0', f'{r} {err[-300:]}')

    fresh = os.path.join(d, 'fresh')
    r, err = run(fresh, empty)
    left = os.listdir(os.path.join(fresh, 'token-counter', 'lib'))
    check('a failed install says why, raises nothing, and leaves nothing half-installed',
          r['got'] is None and isinstance(r['why'], str) and 'could not be installed' in r['why']
          and left == [], f'{r} left={left}')

    off = os.path.join(d, 'off')
    r, err = run(off, wheels, TOKEN_COUNTER_NO_INSTALL='1')
    check('TOKEN_COUNTER_NO_INSTALL=1 installs nothing and says so',
          r['got'] is None and deps.ENV_OFF in (r['why'] or '')
          and not os.path.exists(os.path.join(off, 'token-counter')), f'{r} {err[-300:]}')


# --------------------------------------------------------------------------- the index, end to end

def _explicit_rollout(sid, n, day='2026-09-20'):
    """`n` explicit-stream responses of 1,000 input / 500 cached each, in session `sid`."""
    recs = [{'timestamp': f'{day}T10:00:00.000Z', 'type': 'session_meta',
             'payload': {'session_id': sid, 'id': sid}}]
    for i in range(n):
        ts = f'{day}T10:00:{i:02d}.000Z'
        recs.append({'timestamp': ts, 'type': 'response_item',
                     'payload': {'type': 'message', 'role': 'user',
                                 'content': [{'type': 'input_text', 'text': f'q{i}'}]}})
        recs.append({'timestamp': ts, 'type': 'token_usage_record',
                     'payload': {'response_id': f'{sid}-r{i}', 'usage': {
                         'input_tokens': 1000, 'cached_input_tokens': 500,
                         'output_tokens': 10, 'reasoning_output_tokens': 0,
                         'total_tokens': 1010}}})
    return recs


def _indexed_corpus(sessions):
    """A dated corpus and an isolated CODEX_HOME beside it: ``(root, home, paths)``."""
    d = tempfile.mkdtemp()
    root = os.path.join(d, 'sessions')
    day = os.path.join(root, '2026', '09', '20')
    os.makedirs(day)
    paths = []
    for sid, n in sessions:
        name = f'rollout-2026-09-20T10-00-00-{sid}.jsonl'
        _write(day, name, _explicit_rollout(sid, n))
        paths.append(os.path.join(day, name))
    return root, os.path.join(d, 'home'), paths


@contextlib.contextmanager
def _codex_home(home):
    """Point CODEX_HOME -- and so index.db -- at `home` for the duration."""
    import report as cli
    os.makedirs(home, exist_ok=True)
    keep, cached = os.environ.get('CODEX_HOME'), list(cli._OUT_DIR)
    os.environ['CODEX_HOME'] = home
    cli._OUT_DIR.clear()
    try:
        yield cli
    finally:
        cli._OUT_DIR[:] = cached
        if keep is None:
            os.environ.pop('CODEX_HOME', None)
        else:
            os.environ['CODEX_HOME'] = keep


def _run_indexed(root, home, *extra):
    """report.main() with the index live under `home`: ``(rc, model, stderr)``.

    Not `--quiet`: the tests below read the "N cached | M to parse" line.
    """
    out = tempfile.mkdtemp()
    jpath = os.path.join(out, 'm.json')
    err = io.StringIO()
    with _codex_home(home) as cli, contextlib.redirect_stderr(err), \
            contextlib.redirect_stdout(io.StringIO()):
        rc = cli.main(['--sessions-root', root, '--no-open', '--no-account', '--procs', '1',
                       '--json', jpath, '--out', os.path.join(out, 'r.html'), *extra])
    model = None
    if rc == 0:
        with open(jpath, encoding='utf-8') as fh:
            model = json.load(fh)
    return rc, model, err.getvalue()


def _cache_line(stderr):
    m = re.search(r'(\d+) cached \| (\d+) to parse', stderr)
    return m.groups() if m else None


def test_include_archived_counts_archived_sessions():
    """`--include-archived` must put an archived session into the report, not just say so.

    The archived payload was added to `results` but never to `window`, so the ledger charged
    it out of scope and the report filter dropped it: the run printed "1 archived sessions
    included" over a report that included none.  Driven through `main`, where the two sets
    meet.
    """
    root, home, paths = _indexed_corpus([('aaaa', 3), ('bbbb', 5)])
    rc, model, _ = _run_indexed(root, home)                       # populates the index
    check('two sessions on disk are both reported',
          rc == 0 and model['totals']['responses'] == 8, f'rc={rc}')
    if rc != 0:
        return
    os.remove(paths[1])
    rc, model, err = _run_indexed(root, home)
    check('a vanished file leaves a plain run, and is announced as archived',
          rc == 0 and model['totals']['responses'] == 3 and 'archived' in err,
          f"rc={rc} responses={model and model['totals']['responses']}")
    rc, model, err = _run_indexed(root, home, '--include-archived')
    check('--include-archived counts the archived session',
          rc == 0 and model['totals']['responses'] == 8
          and model['totals']['sessions'] == 2 and model['totals']['files'] == 2,
          f"rc={rc} totals={model and model['totals']}")
    rc, model, err = _run_indexed(root, home, '--include-archived', '--session', 'aaaa')
    check('--session still narrows an archived-inclusive window',
          rc == 0 and model['totals']['responses'] == 3 and model['totals']['sessions'] == 1,
          f"rc={rc} totals={model and model['totals']}")


def test_rebuild_discards_a_held_index():
    """`--rebuild` must re-parse even when the index file cannot be deleted.

    Deleting the file was the whole mechanism, and the fallback comment claimed the schema
    check would re-parse anyway -- it clears the table only on a version change.  On Windows
    a second run holding index.db open made the delete fail, and the run then served every
    cached payload under a message saying it could not delete the index.
    """
    root, home, _ = _indexed_corpus([('aaaa', 2), ('bbbb', 2), ('cccc', 2)])
    _run_indexed(root, home)
    rc, _, err = _run_indexed(root, home)
    check('a warm run reuses the index', rc == 0 and _cache_line(err) == ('3', '0'),
          f'rc={rc} {err[-200:]!r}')
    holder = sqlite3.connect(os.path.join(home, 'token-counter', 'index.db'))
    try:
        rc, _, err = _run_indexed(root, home, '--rebuild')
    finally:
        holder.close()
    check('--rebuild re-parses every file while another connection holds the index',
          rc == 0 and _cache_line(err) == ('0', '3'), f'rc={rc} {err[-300:]!r}')
    rc, _, err = _run_indexed(root, home)
    check('the rebuilt index is warm again', rc == 0 and _cache_line(err) == ('3', '0'),
          f'rc={rc} {err[-200:]!r}')


def test_zero_extractor_key_bypasses_index():
    """`_extractor_version` returns 0 for "cannot fingerprint the extractor; never reuse".

    `collect` used 0 as an ordinary key, so a row written under it was served back under it.
    """
    import report as cli
    from tokencounter import index as idx
    d = tempfile.mkdtemp()
    _write(d, 'rollout-z.jsonl', _explicit_rollout('zzzz', 1))
    p = os.path.join(d, 'rollout-z.jsonl')
    cache = idx.Index(os.path.join(d, 'index.db'))
    size, mtime = rollout.stat_key(p)
    cache.put({'path': p, 'session_id': 'zzzz', 'thread_id': 'zzzz', 'size': size,
               'mtime_ns': mtime, 'prefix_hash': rollout.prefix_hash(p),
               'poisoned': True}, 0, 0.0)
    cache.commit()
    with contextlib.redirect_stderr(io.StringIO()):
        results, _ = cli.collect([p], 1, 1, None, cache, True, True, 0)
    cache.close()
    check('a zero extractor key never reads the index',
          not results[p].get('poisoned') and len(results[p].get('explicit') or []) == 1,
          str(results[p])[:160])


def test_session_prefix_must_name_one_session():
    """`--session` takes "one session id or prefix"; a prefix matching two is refused rather
    than resolved to whichever came out of a set first."""
    root, home, _ = _indexed_corpus([('abc1', 1), ('abc2', 1)])
    rc, model, err = _run_indexed(root, home, '--no-cache', '--session', 'abc')
    check('a prefix matching two sessions is refused, naming them',
          rc == 3 and 'abc1' in err and 'abc2' in err, f'rc={rc} {err[-200:]!r}')
    rc, model, err = _run_indexed(root, home, '--no-cache', '--session', 'abc2')
    check('a prefix matching one session is accepted and focused',
          rc == 0 and model['totals']['sessions'] == 1 and model['deep_dive'] == ['abc2'],
          f"rc={rc} {model and (model['totals']['sessions'], model['deep_dive'])}")


# --------------------------------------------------------------------------- failure modes

def test_failure_modes():
    d = tempfile.mkdtemp()

    (pathlib.Path(d) / 'rollout-empty.jsonl').write_text('', encoding='utf-8')
    r = worker.process(os.path.join(d, 'rollout-empty.jsonl'))
    check('empty rollout yields no usage, no crash',
          r['explicit'] == [] and r['legacy'] == []
          and r['counters'].get('no_usage_data') == 1, str(r['counters']))

    (pathlib.Path(d) / 'rollout-junk.jsonl').write_text(
        'not json at all\n[]\nnull\n{"type":"x"}\n{"type":"session_meta","payload":3}\n',
        encoding='utf-8')
    r = worker.process(os.path.join(d, 'rollout-junk.jsonl'))
    check('malformed lines are skipped, not fatal', r['session_id'] is None)

    p = pathlib.Path(d) / 'rollout-noturn.jsonl'
    p.write_text(''.join(json.dumps(x) + '\n' for x in [
        _rec('session_meta', {'session_id': 'S', 'id': 'S'}, 0),
        _rec('token_usage_record', {'response_id': 'r', 'usage': {
            'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 1,
            'reasoning_output_tokens': 0, 'total_tokens': 11}}, 1)]), encoding='utf-8')
    data = {str(p): worker.metrics_only(str(p))}
    charged, counters = ledger.build(data)
    m = analyze.analyze(data, charged, counters)
    check('a session with no turn_context buckets as unknown, not as a crash',
          m['models'] and m['models'][0]['model'] == 'unknown', str(m['models']))

    check('a missing rollout is an error, not an exception',
          bool(worker.process(os.path.join(d, 'rollout-nope.jsonl'))['error']))

    bad = pathlib.Path(d) / 'empty.tiktoken'
    bad.write_bytes(b'')
    try:
        encoding.load(str(bad))
        check('a truncated vocab is rejected', False, 'no exception raised')
    except ValueError as exc:
        check('a truncated vocab is rejected', 'truncated' in str(exc) or 'ranks' in str(exc),
              str(exc)[:120])
    except Exception as exc:
        check('a truncated vocab is rejected', False,
              f'{exc.__class__.__name__}: {exc}')


def test_local_day_bucketing():
    """Record timestamps are UTC; the directory layout and --since filter use local dates."""
    import datetime
    off = datetime.datetime.now().astimezone().utcoffset()
    day = analyze._day('2026-09-20T03:15:00.000Z')
    expect = (datetime.datetime(2026, 9, 20, 3, 15,
                                tzinfo=datetime.timezone.utc)
              .astimezone().date().isoformat())
    check('UTC timestamps bucket into local days', day == expect,
          f'got {day}, expected {expect} (offset {off})')
    check('a bad timestamp falls back rather than inventing a day',
          analyze._day('garbage', 'fallback') == 'fallback')



class _USEastern(datetime.tzinfo):
    """UTC-5 with the 2026 US clock changes, so the test does not depend on the machine."""
    _ON, _OFF = datetime.datetime(2026, 3, 8, 2), datetime.datetime(2026, 11, 1, 2)

    def utcoffset(self, dt):
        return datetime.timedelta(hours=-5) + self.dst(dt)

    def dst(self, dt):
        if dt is None:
            return datetime.timedelta(0)
        naive = dt.replace(tzinfo=None)
        return datetime.timedelta(hours=1 if self._ON <= naive < self._OFF else 0)

    def tzname(self, dt):
        return 'TEST'


def test_day_span_across_clock_changes():
    """A daily bar spans local midnight to local midnight: 23 or 25 hours twice a year.

    The first version localised the start and added a day to *that*.  On the fixed offset
    `astimezone()` attaches, a day is +86,400 s, so the bar ended at 01:00 on the day the
    clock sprang forward and at 23:00 on the day it fell back -- overlapping or gapping its
    neighbour while the docstring said the opposite.
    """
    tz = _USEastern()
    for day, hours in (('2026-03-07', 24), ('2026-03-08', 23),
                       ('2026-11-01', 25), ('2026-11-02', 24)):
        a, b = analyze._day_span(day, tz)
        check(f'{day} spans {hours}h', a is not None and round((b - a) / 3600) == hours,
              f'{(b - a) / 3600 if a is not None else None}h')
    _, b = analyze._day_span('2026-03-08', tz)
    c, _ = analyze._day_span('2026-03-09', tz)
    check('consecutive days meet exactly across the clock change', b == c, f'{b} vs {c}')
    check('the machine zone is still the default',
          analyze._day_span('2026-09-20')[0]
          == datetime.datetime(2026, 9, 20).astimezone().timestamp())


def test_daily_model_split():
    """The daily chart stacks by model, so each day's split must add up to that day."""
    d = tempfile.mkdtemp()

    def turn(ts, model, rid, inp):
        return [
            {'timestamp': ts, 'type': 'turn_context',
             'payload': {'model': model, 'effort': 'max'}},
            {'timestamp': ts, 'type': 'token_usage_record',
             'payload': {'response_id': rid, 'usage': {
                 'input_tokens': inp, 'cached_input_tokens': 0, 'output_tokens': 10,
                 'reasoning_output_tokens': 0, 'total_tokens': inp + 10}}},
        ]

    recs = [_rec('session_meta', {'session_id': 'S', 'id': 'S'}, 0)]
    recs += turn('2026-01-01T12:00:00.000Z', 'm1', 'r1', 500)
    recs += turn('2026-01-01T12:05:00.000Z', 'm2', 'r2', 300)
    recs += turn('2026-01-02T12:00:00.000Z', 'm2', 'r3', 700)
    _write(d, 'rollout-dm.jsonl', recs)

    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    m = analyze.analyze(data, charged, counters, scope={'label': 'test'})
    daily = m['daily']

    check('the daily split accounts for every recorded token',
          all(sum((x.get('models') or {}).values()) == x['input'] for x in daily),
          str([(x['date'], x['input'], x.get('models')) for x in daily]))
    check('a day is split by the model that was charged',
          len(daily) == 2 and (daily[0].get('models') or {}) == {'m1': 500, 'm2': 300}
          and (daily[1].get('models') or {}) == {'m2': 700},
          str([(x['date'], x.get('models')) for x in daily]))

    # The chart legend must name them, or the stack is unreadable -- and a cap that folded
    # a real model into `other` would show up here rather than silently.
    html = render.render(m)
    chart = html[html.index('id="dailychart"'):]
    chart = chart[:chart.index('id="catpie"')]
    check('the daily chart names the models it stacks',
          'm1' in chart and 'm2' in chart and 'other' not in chart,
          chart[-400:])


def _counted_corpus():
    """Two sessions on two days with real prompt text, cached tokens, two models and a weekly
    limit window: everything the input switch touches (§5.7)."""
    d = tempfile.mkdtemp()
    t0 = 1789000000
    reset = t0 + WEEK
    iso = lambda t: (datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
                     .isoformat().replace('+00:00', 'Z'))

    def session(sid, model, start, n, words):
        recs = [_rec('session_meta', {'session_id': sid, 'id': sid}, 0),
                {'timestamp': iso(start), 'type': 'turn_context',
                 'payload': {'model': model, 'effort': 'high'}}]
        cum = 0
        for i in range(n):
            recs.append({'timestamp': iso(start + i * 600), 'type': 'response_item',
                         'payload': {'type': 'message', 'role': 'user', 'content': [
                             {'type': 'input_text',
                              'text': f'question {i}: ' + 'lorem ipsum dolor ' * words}]}})
            cum += 5000
            tc = _tc(start + i * 600 + 1, 10.0 * (i + 1), reset, cum, 5000)
            tc['payload']['info']['last_token_usage']['cached_input_tokens'] = 2000
            recs.append(tc)
        return recs

    _write(d, 'rollout-2026-09-10T00-00-00-a.jsonl', session('A', 'm1', t0, 3, 40))
    _write(d, 'rollout-2026-09-11T00-00-00-b.jsonl', session('B', 'm2', t0 + 36 * 3600, 2, 400))
    return d


def test_input_counted_with_tiktoken():
    """Input on the page is tiktoken's count of each response's reconstructed prompt; output,
    cached and the cache hit stay Codex's; `--metrics-only` shows Codex's input as before."""
    import report as cli
    corpus = _counted_corpus()

    def run(*extra):
        out = tempfile.mkdtemp()
        j, h = os.path.join(out, 'm.json'), os.path.join(out, 'r.html')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(['--sessions-root', corpus, '--no-open', '--no-account', '--quiet',
                           '--no-cache', '--procs', '1', '--json', j, '--out', h, *extra])
        with open(j, encoding='utf-8') as fh:
            model = json.load(fh)
        with open(h, encoding='utf-8') as fh:
            return rc, model, fh.read(), buf.getvalue()

    rc, m, page, out = run()
    _, mo, page_mo, out_mo = run('--metrics-only')
    t, to = m['totals'], mo['totals']
    check('input is counted with tiktoken: the sum of the reconstructed prompts',
          rc == 0 and t['input_source'] == 'tiktoken' and t['tiktoken_input'] > 0
          and t['tiktoken_input'] == m['residual']['reconstructed']
          and t['tiktoken_input'] != t['input'], str(t))
    if t['input_source'] != 'tiktoken':
        return                          # failed assertion above; absent counted fields cannot be tested
    check('output, cached and the cache hit are still what Codex recorded',
          (t['input'], t['cached'], t['output'], t['reasoning'], t['cache_hit'])
          == (to['input'], to['cached'], to['output'], to['reasoning'], to['cache_hit'])
          == (25000, 10000, 50, 0, 0.4), f'{t}\n{to}')
    check('every response had a prompt to count, so none fell back to Codex\'s figure',
          m['quality'].get('tiktoken_input_fallback') == 0, str(m['quality']))

    days = m['daily']
    check('each day splits its tiktoken input by model, and keeps Codex\'s split beside it',
          len(days) == 2
          and all(sum(d['tiktoken_models'].values()) == d['tiktoken_input'] for d in days)
          and all(sum(d['models'].values()) == d['input'] for d in days)
          and sum(d['tiktoken_input'] for d in days) == t['tiktoken_input']
          and [(d['input'], d['cached']) for d in days]
          == [(d['input'], d['cached']) for d in mo['daily']],
          str(days))
    w = m['rate_limits']['windows']
    check('the limit curve runs on tiktoken input; the window keeps Codex\'s figures too',
          len(w) == 1 and w[0]['tokens']['tiktoken_input'] == t['tiktoken_input']
          and w[0]['cum_points'][-1][4] == t['tiktoken_input']
          and w[0]['tokens']['input'] == t['input'], json.dumps(w)[:400])
    s = m['sessions']
    check('sessions rank by tiktoken input, which puts the other session on top here',
          [x['session_id'] for x in s] == ['B', 'A'] and s[0]['input'] < s[1]['input'],
          str([(x['session_id'], x['input'], x['tiktoken_input']) for x in s]))

    tiles = page[page.index('class="tiles"'):page.index('id="rlchart"')]
    check('the page shows tiktoken input, and says output and caching are Codex\'s',
          f'<div class="k">Input</div><div class="v">{render.big(t["tiktoken_input"])}<' in tiles
          and 'counted with tiktoken' in tiles
          and f'{render.big(t["output"])}</div>' in tiles
          and f'{render.big(t["cached"])} of {render.big(t["input"])} recorded by Codex' in tiles
          and f'<div class="v">{render.big(s[0]["tiktoken_input"])}</div>' in tiles
          and '"input_source":"tiktoken"' in page, tiles)
    check('the terminal line names tiktoken input and Codex\'s figures apart',
          'input (tiktoken)' in out and 'recorded by Codex: 0.000B input' in out
          and '40.0% cached' in out, out)

    tiles_mo = page_mo[page_mo.index('class="tiles"'):page_mo.index('id="rlchart"')]
    wo = mo['rate_limits']['windows'][0]
    check('without the tokenizer every input figure is Codex\'s, and labelled as recorded',
          to['input_source'] == 'recorded' and to['tiktoken_input'] is None
          and 'Recorded input' in tiles_mo and 'tiktoken' not in tiles_mo
          and 'recorded input' in out_mo and 'tiktoken_input' not in wo['tokens']
          and all(len(p) == 4 for p in wo['cum_points'])
          and all(x['tiktoken_input'] is None for x in mo['sessions']), str(to))

    # A file extracted without its reconstruction keeps Codex's figure for its responses,
    # counted, rather than contributing nothing.
    data = {p: worker.process(p) for p in rollout.discover(corpus)}
    gone = next(p for p, r in data.items() if r['session_id'] == 'A')
    data[gone]['responses'] = []
    charged, counters = ledger.build(data)
    mf = analyze.analyze(data, charged, counters, scope={'label': 'test'})
    q = mf['quality']
    check('a response with no reconstructed prompt keeps Codex\'s input, and is counted',
          mf['totals']['input_source'] == 'tiktoken'
          and q['tiktoken_input_fallback'] == 3 and q['tiktoken_input_fallback_tokens'] == 15000
          and mf['totals']['tiktoken_input'] == 15000 + next(
              x['tiktoken_input'] for x in m['sessions'] if x['session_id'] == 'B'),
          f"{mf['totals']} {q}")

    # A prompt that reconstructs to zero tokens is a reconstruction that found nothing, and
    # falls back the same way rather than adding nothing.
    data = {p: worker.process(p) for p in rollout.discover(corpus)}
    for resp in data[gone]['responses']:
        resp['recon_input'] = 0
    charged, counters = ledger.build(data)
    mz = analyze.analyze(data, charged, counters, scope={'label': 'test'})
    check('a prompt that reconstructs to nothing keeps Codex\'s input, and is counted',
          mz['quality']['tiktoken_input_fallback'] == 3
          and mz['totals']['tiktoken_input'] == mf['totals']['tiktoken_input'],
          f"{mz['totals']} {mz['quality']}")


# --------------------------------------------------------------------------- latency

LAT_T0 = 1_790_000_000               # 2026-09-21, a fixed instant for every timing fixture


def _usage(rid, inp, cached, out):
    return {'response_id': rid, 'usage': {
        'input_tokens': inp, 'cached_input_tokens': cached, 'output_tokens': out,
        'reasoning_output_tokens': 0, 'total_tokens': inp + out}}


def _user(t, text='q'):
    return _at(t, 'response_item', {'type': 'message', 'role': 'user',
                                    'content': [{'type': 'input_text', 'text': text}]})


def _said(t, text='a'):
    return _at(t, 'response_item', {'type': 'message', 'role': 'assistant',
                                    'content': [{'type': 'output_text', 'text': text}]})


def _call(t, cid, name='exec'):
    return _at(t, 'response_item', {'type': 'function_call', 'call_id': cid, 'name': name,
                                    'arguments': '{}'})


def _out(t, cid, text='ok'):
    return _at(t, 'response_item', {'type': 'function_call_output', 'call_id': cid,
                                    'output': text})


def _timed_session(t):
    """Two turns laid out the way Codex writes them, with every gap known in advance.

    Turn 0: a tool call answered, then a final message.  Turn 1: a tool whose output is
    written while its response is still streaming, then a compaction call with no input
    of its own, then one more response straight after it.
    """
    return [
        _at(t, 'session_meta', {'session_id': 'L', 'id': 'L'}),
        _at(t, 'turn_context', {'model': 'm1', 'effort': 'high'}),
        _user(t + 1),
        _at(t + 4, 'response_item', {'type': 'reasoning', 'summary': []}),
        _call(t + 5, 'c1'),
        _at(t + 6, 'token_usage_record', _usage('r1', 1000, 0, 50)),         # 6 - 1 = 5
        _out(t + 9, 'c1'),                                                  # tool: 4s
        _said(t + 14),
        _at(t + 15, 'token_usage_record', _usage('r2', 1200, 1000, 40)),     # 15 - 9 = 6
        _at(t + 100, 'turn_context', {'model': 'm1', 'effort': 'high'}),
        _user(t + 101),
        _call(t + 103, 'c2', 'apply_patch'),
        _out(t + 104, 'c2'),                       # in flight: before its response's usage
        _said(t + 106),
        _at(t + 107, 'token_usage_record', _usage('r3', 1400, 1200, 30)),    # 107 - 101 = 6
        _at(t + 120, 'token_usage_record', _usage('r4', 1500, 0, 200)),      # 120 - 107 = 13
        _said(t + 124),
        _at(t + 125, 'token_usage_record', _usage('r5', 300, 0, 20)),        # 125 - 120 = 5
    ]


def _latency_of(d, full=True):
    fn = worker.process if full else worker.metrics_only
    data = {p: fn(p) for p in rollout.discover(d)}
    charged, _ = ledger.build(data)
    return latency.build(data, charged, tz=datetime.timezone.utc)


def test_response_time_from_records():
    """A response runs from the prompt being complete to its usage record, and no further."""
    d = tempfile.mkdtemp()
    _write(d, 'rollout-lat.jsonl', _timed_session(LAT_T0))
    for full in (True, False):
        tag = 'full pass' if full else 'ledger-only pass'
        lat, q = _latency_of(d, full)
        g = lat['groups'][0] if lat.get('groups') else {}
        check(f'five responses are timed ({tag})', lat['responses']['n'] == 5,
              str(lat['responses']))
        check(f'response times are the gaps the records leave ({tag})',
              lat['responses']['total_s'] == 35 and g.get('median_s') == 6,
              f"{lat['responses']} {g}")
        tools = {t['tool']: t for t in lat['tools']}
        check(f'tool time runs from the call to its output ({tag})',
              tools.get('exec', {}).get('total_s') == 4
              and tools.get('apply_patch', {}).get('total_s') == 1, str(lat['tools']))
    r = worker.process(os.path.join(d, 'rollout-lat.jsonl'))
    starts = [worker.epoch(x['req_ts']) - LAT_T0 for x in r['explicit']]
    check('a tool output written mid-stream does not become its own request start',
          starts[2] == 101, str(starts))
    lat, _ = _latency_of(d)
    check('a call with no input of its own starts when the previous response ended',
          _times(d) == [5, 6, 6, 13, 5], str(_times(d)))
    t = lat['turns']
    check('a turn runs from its opening to its last response',
          t['n'] == 2 and t['total_s'] == 15 + 25, str(t))
    check("the model's share of a turn is its responses' time",
          t['model_s'] == 11 + 24, str(t))


def _times(d, only=''):
    """Every timed response's seconds, in file order, for files whose path has `only`."""
    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, _ = ledger.build(data)
    out = []
    for p, rows in sorted(charged.items()):
        if only not in os.path.basename(p):
            continue
        prev = None
        for r in rows:
            start = latency._response_start(worker.epoch(r['req_ts']), prev)
            end = worker.epoch(r['ts'])
            prev = end if prev is None else max(prev, end)
            out.append(round(end - start, 3))
    return out


def _legacy_tc(t, last, cum, out=10):
    u = {'input_tokens': last, 'cached_input_tokens': 0, 'output_tokens': out,
         'reasoning_output_tokens': 0, 'total_tokens': last + out}
    c = {'input_tokens': cum, 'cached_input_tokens': 0, 'output_tokens': out,
         'reasoning_output_tokens': 0, 'total_tokens': cum + out}
    return _at(t, 'event_msg', {'type': 'token_count',
                                'info': {'last_token_usage': u, 'total_token_usage': c}})


def _fork_corpus(d, turns):
    """A parent of `turns` legacy turns (a 3 s call, a 17 s tool, an 11 s answer each) and a
    fork child that replays all of it, then does 5 s + 5 s of work of its own.  With
    `turns` 0 the parent has one usage record, so the replay is a one-record match.

    The replay is stamped with the child's creation time a millisecond apart, record by
    record, as a real one is: a replayed tool call then reads as a 1 ms call, not as a zero
    that any duration filter would drop anyway.
    """
    t = LAT_T0
    parent_work, cum = [], 0
    for i in range(turns):
        cum += 1000 * (i + 1)
        b = t + 100 * i
        parent_work += [_user(b + 1), _call(b + 3, f'p{i}'), _legacy_tc(b + 4, 1000 * (i + 1), cum),
                        _out(b + 20, f'p{i}'), _said(b + 30),
                        _legacy_tc(b + 31, 1000 * (i + 1) + 5, cum + 1000 * (i + 1) + 5)]
        cum += 1000 * (i + 1) + 5
    if turns == 0:
        parent_work = [_user(t + 1), _call(t + 3, 'p0'), _legacy_tc(t + 4, 1000, 1000),
                       _out(t + 20, 'p0')]
        cum = 1000
    parent = ([_at(t, 'session_meta', {'session_id': 'F', 'id': 'P'}),
               _at(t, 'turn_context', {'model': 'm1', 'effort': 'low'})] + parent_work)
    _write(d, 'rollout-1-parent.jsonl', parent)
    tc = t + 1000                                       # the child's creation time
    replay = [dict(r, timestamp=_at(tc + k / 1000, 'x', {})['timestamp'])
              for k, r in enumerate(parent_work)]
    child = ([_at(tc, 'session_meta', {'session_id': 'F', 'id': 'C', 'parent_thread_id': 'P'})]
             + replay
             + [_at(tc + 50, 'turn_context', {'model': 'm1', 'effort': 'low'}),
                _user(tc + 51), _call(tc + 55, 'k1'),
                _legacy_tc(tc + 56, 9000, cum + 9000),
                _out(tc + 60, 'k1'), _said(tc + 64),
                _legacy_tc(tc + 65, 9100, cum + 18100)])
    _write(d, 'rollout-2-child.jsonl', child)


def test_replayed_history_is_not_timed():
    """A fork child's copy of its parent's history is stamped with the child's creation
    time; none of it may pass for work the child did -- whether the ledger drops it, or
    charges it anyway."""
    d = tempfile.mkdtemp()
    _fork_corpus(d, 3)
    lat, q = _latency_of(d)
    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    kid = [p for p in charged if 'child' in p][0]
    check('the replayed usage never reaches the ledger', counters.get('inherited') == 6
          and len(charged[kid]) == 2, f"{dict(counters)} {len(charged[kid])}")
    check('the child is timed on its own work only',
          _times(d, 'child') == [5, 5] and _times(d, 'parent') == [3, 11] * 3
          and lat['responses']['n'] == 8, f"{_times(d)} {lat['responses']}")
    ex = {x['tool']: x for x in lat['tools']}.get('exec', {})
    check('replayed tool calls are counted and left out',
          q['tool_replayed'] == 3 and ex.get('n') == 4 and ex.get('total_s') == 3 * 17 + 5,
          f"{dict(q)} {lat['tools']}")
    check('no replayed tool call passes for a millisecond one',
          all(v['median_s'] >= 5 for v in lat['tools']) and ex.get('median_s') == 17,
          str(lat['tools']))

    # The other bound: with the exclusion off the ledger charges the replay, and it must
    # still not be timed -- six 1 ms responses would drag the pace line to the floor.
    charged2, _ = ledger.build(data, exclude_replay=False)
    lat2, q2 = latency.build(data, charged2, tz=datetime.timezone.utc)
    ex2 = {x['tool']: x for x in lat2['tools']}.get('exec', {})
    check('a replay charged with the exclusion off is not timed',
          len(charged2[kid]) == 8 and q2['latency_replayed'] == 6
          and lat2['responses']['n'] == 8 and lat2['responses']['median_s'] == 5,
          f"{dict(q2)} {lat2['responses']}")
    check('... nor are its tool calls', ex2.get('n') == 4 and q2['tool_replayed'] == 3,
          f"{dict(q2)} {lat2['tools']}")

    # A one-record match is ambiguous: charged and disclosed, and never timed.
    d3 = tempfile.mkdtemp()
    _fork_corpus(d3, 0)
    data3 = {p: worker.process(p) for p in rollout.discover(d3)}
    charged3, counters3 = ledger.build(data3)
    lat3, q3 = latency.build(data3, charged3, tz=datetime.timezone.utc)
    ex3 = {x['tool']: x for x in lat3['tools']}.get('exec', {})
    check('an ambiguous one-record replay is charged but not timed',
          counters3.get('ambiguous') == 1 and q3['latency_replayed'] == 1
          and lat3['responses']['n'] == 3 and lat3['responses']['median_s'] == 5,
          f"{dict(counters3)} {dict(q3)} {lat3['responses']}")
    check('... nor is its tool call', ex3.get('n') == 2 and q3['tool_replayed'] == 1,
          f"{dict(q3)} {lat3['tools']}")
    for label, scoped, rows, old, old_q in [('excluded', data, charged, lat, q),
                                          ('charged', data, charged2, lat2, q2),
                                          ('ambiguous', data3, charged3, lat3, q3)]:
        extra, extra_q = latency.build(scoped, rows, tz=datetime.timezone.utc, tier_groups=True)
        check(f'{label} replay: additional tier grouping preserves samples and quality counters',
              {k: v for k, v in extra.items() if k != 'tier_groups'} == old and extra_q == old_q
              and sum(g['n'] for g in extra['tier_groups']) == old['responses']['n'], str(extra))


def _timed(recs):
    """Every timed response's seconds for one rollout, through the whole pipeline."""
    d = tempfile.mkdtemp()
    _write(d, 'rollout-x.jsonl', recs)
    return _times(d)


def test_request_start_edge_cases():
    """Where a request starts, in the orderings a real rollout produces."""
    t = LAT_T0

    def tc(x):
        return _at(x, 'turn_context', {'model': 'm1', 'effort': 'high'})

    meta = _at(t, 'session_meta', {'session_id': 'E', 'id': 'E'})

    # An interrupted response writes output and no usage record.  Its start must not
    # outlive it: the next turn is timed from its own message, not the interrupted one.
    got = _timed([meta, tc(t), _user(t + 1), _said(t + 5),
                  _at(t + 6, 'token_usage_record', _usage('r1', 100, 0, 5)),
                  tc(t + 100), _user(t + 101),
                  _at(t + 105, 'response_item', {'type': 'reasoning', 'summary': []}),
                  tc(t + 400), _user(t + 401), _said(t + 405),
                  _at(t + 406, 'token_usage_record', _usage('r2', 200, 0, 5))])
    check("an interrupted response does not hold the next turn's start", got == [5, 5],
          str(got))
    got = _timed([meta, tc(t), _user(t + 1), _said(t + 5),
                  _at(t + 6, 'token_usage_record', _usage('r1', 100, 0, 5)),
                  _user(t + 101),
                  _at(t + 105, 'response_item', {'type': 'reasoning', 'summary': []}),
                  _at(t + 106, 'event_msg', {'type': 'turn_aborted', 'reason': 'interrupted'}),
                  _user(t + 401), _said(t + 405),
                  _at(t + 406, 'token_usage_record', _usage('r2', 200, 0, 5))])
    check('... and an aborted turn ends it even with no turn opening after it',
          got == [5, 5], str(got))

    # A legacy compaction has no charged usage of its own; the next response starts when
    # the compaction is written, not at the last tool output before it.
    got = _timed([meta, tc(t), _user(t + 1), _call(t + 3, 'c1'), _legacy_tc(t + 4, 1000, 1000),
                  _out(t + 6, 'c1'),
                  _at(t + 66, 'compacted', {'message': '', 'replacement_history': []}),
                  _at(t + 66, 'event_msg', {'type': 'token_count', 'info': {   # its snapshot
                      'last_token_usage': {'input_tokens': 0, 'output_tokens': 0,
                                           'total_tokens': 1010},
                      'total_token_usage': {'input_tokens': 1000, 'output_tokens': 10,
                                            'total_tokens': 1010}}}),
                  _said(t + 67), _legacy_tc(t + 68, 300, 1300)])
    check('a legacy compaction is not charged to the response after it', got == [3, 2],
          str(got))

    # A legacy repeat (a rate-limit refresh re-sending the last usage) can land mid-stream;
    # it is not a response, so it must not end the one in flight.
    got = _timed([meta, tc(t), _user(t + 1), _said(t + 4), _legacy_tc(t + 5, 1000, 1000),
                  tc(t + 9), _user(t + 10), _call(t + 12, 'c2'), _out(t + 13, 'c2'),
                  _legacy_tc(t + 14, 1000, 1000),
                  _said(t + 18), _legacy_tc(t + 20, 1500, 2500)])
    check('a repeated legacy record does not end the response in flight', got == [4, 10],
          str(got))

    # Local bookkeeping the classifier does not know at all, written before the response's
    # first output, and a web search the model ran must not pass for the request's start.
    got = _timed([meta, tc(t), _user(t + 1),
                  _at(t + 2, 'response_item', {'type': 'ghost_snapshot'}),
                  _at(t + 3, 'response_item', {'type': 'web_search_call', 'status': 'completed'}),
                  _at(t + 4, 'response_item', {'type': 'ghost_snapshot'}),
                  _at(t + 6, 'response_item', {'type': 'reasoning', 'summary': []}),
                  _said(t + 8), _at(t + 9, 'token_usage_record', _usage('r1', 100, 0, 5))])
    check("only known inputs move a request's start", got == [8], str(got))

    # Rollout content is not trusted to be well-formed: a tool name or call id of the
    # wrong type is not a reason for the ledger-only pass, or the report, to fail.
    d = tempfile.mkdtemp()
    _write(d, 'rollout-odd.jsonl', [
        meta, tc(t), _user(t + 1),
        _at(t + 2, 'response_item', {'type': 'function_call', 'call_id': 'c1',
                                     'name': ['exec'], 'arguments': '{}'}),
        _at(t + 3, 'response_item', {'type': 'function_call', 'call_id': ['c2'],
                                     'name': 'exec', 'arguments': '{}'}),
        _at(t + 4, 'token_usage_record', _usage('r1', 100, 0, 5)),
        _out(t + 8, 'c1'),
        _at(t + 9, 'response_item', {'type': 'function_call_output', 'call_id': ['c2'],
                                     'output': 'x'}),
        _said(t + 10), _at(t + 11, 'token_usage_record', _usage('r2', 200, 0, 5))])
    data = {p: worker.metrics_only(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    try:
        model = analyze.analyze(data, charged, counters, scope={'label': 't'})
        tools = {x['tool']: x['n'] for x in model['latency']['tools']}
        ok = tools == {'function_call': 1}
    except Exception as exc:                          # the failure is the finding
        ok, tools = False, repr(exc)
    check('a malformed tool name or call id is survived, not raised', ok, str(tools))


def _pace_corpus(n=600, busy_hour=15, seed=11):
    """`n` responses of one model at a known pace, with extra wait added at one hour of day.

    Pace: 1.5 s fixed, 50 output tokens/s, 5,000 uncached input tokens/s.  A third of the
    responses wait nothing beyond that; the rest wait a little, and far longer in `busy_hour`.
    """
    import random
    rng = random.Random(seed)
    rows, t = [], LAT_T0 - LAT_T0 % 86400        # midnight UTC
    for i in range(n):
        start = t + i * 3600 // 24 * 2 + rng.randint(0, 60)
        out = rng.randint(20, 3000)
        unc = rng.choice([0, rng.randint(500, 40000)])
        hour = datetime.datetime.fromtimestamp(start, datetime.timezone.utc).hour
        wait = 0.0 if rng.random() < 1 / 3 else rng.expovariate(1 / (30 if hour == busy_hour else 2))
        d = 1.5 + out / 50 + unc / 5000 + wait + rng.uniform(0, .02)
        iso = lambda x: (datetime.datetime.fromtimestamp(x, datetime.timezone.utc)
                         .isoformat().replace('+00:00', 'Z'))
        rows.append({'ts': iso(start + d), 'req_ts': iso(start), 'model': 'm1', 'effort': 'high',
                     'turn': i, 'stream': 'explicit', 'index': i,
                     'usage': {'input_tokens': unc + 1000, 'cached_input_tokens': 1000,
                               'output_tokens': out}})
    return rows


def test_pace_split():
    """The pace line is found under the fastest responses, and the rest is time above it."""
    rows = _pace_corpus()
    files = {'a': {'turn_starts': [r['req_ts'] for r in rows], 'tool_times': []}}
    lat, q = latency.build(files, {'a': rows}, tz=datetime.timezone.utc)
    f = lat['groups'][0]['fit']
    check('the fit finds the fixed overhead', abs(f['overhead_s'] - 1.5) < .1, str(f))
    check('the fit finds the output pace', abs(f['output_tps'] - 50) < 1.5, str(f))
    check('the fit finds the uncached input pace',
          abs(f['uncached_input_tps'] - 5000) < 400, str(f))
    r = lat['responses']
    check('response time splits into work and time above the pace, and nothing else',
          abs(r['work_s'] + r['above_s'] - r['total_s']) < .01 and r['unfit_s'] == 0, str(r))
    hours = {h['hour']: h for h in lat['hours']}
    calm = [h['median_above_s'] for k, h in hours.items() if k != 15]
    check('the busy hour stands out in time above the pace',
          hours[15]['median_above_s'] > 5 * max(calm), f"{hours[15]} vs {max(calm)}")
    check('every response lands in one hour of the day',
          sum(h['n'] for h in lat['hours']) == r['n'] and len(lat['hours']) == 24,
          str(len(lat['hours'])))

    # A model with too few responses gets no line, and its time is kept apart, not guessed.
    few = [dict(x, model='rare') for x in _pace_corpus(n=latency.FIT_MIN - 1, seed=3)]
    lat2, _ = latency.build({'a': files['a'], 'b': {'turn_starts': [], 'tool_times': []}},
                            {'a': rows, 'b': few}, tz=datetime.timezone.utc)
    rare = [g for g in lat2['groups'] if g['model'] == 'rare'][0]
    check('too few responses: no pace line, and the time is reported as not split',
          rare['fit'] is None and rare['above_s'] is None and rare['above_share'] is None
          and abs(lat2['responses']['unfit_s'] - rare['total_s']) < .01, str(rare))
    cov = lat2['responses']['fitted_share']
    check('the headline share says how much of the time it covers',
          0 < cov < 1 and abs(cov - (1 - rare['total_s'] / lat2['responses']['total_s'])) < 1e-3,
          str(lat2['responses']))

    # Two columns that move together, or one that barely moves, make the design singular
    # or let a single response set a rate: the fit drops the column instead of failing.
    import random
    rng = random.Random(4)
    together = [(1 + o / 50 + rng.random() * .1, o, 10 * o) for o in range(10, 2000, 20)]
    f = latency.floor_fit(together)
    check('collinear token columns still give a line', f is not None and f['b'] > 0, str(f))
    sparse = [(1 + o / 50 + rng.random() * .1, o, 0) for o in range(10, 2000, 20)]
    sparse[5] = (sparse[5][0] + 30, sparse[5][1], 90000)
    f = latency.floor_fit(sparse)
    check('one response with uncached input does not set the input rate', f is not None
          and f['c'] == 0 and abs(1 / f['b'] - 50) < 2, str(f))


def test_latency_tier_fits_are_independent():
    def row(i, tier, duration):
        req = LAT_T0 + 60 * i
        iso = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()
        return {'model': 'm', 'effort': 'high', 'tier': tier, 'req_ts': iso(req),
                'ts': iso(req + duration), 'usage': {'input_tokens': 100,
                                                   'cached_input_tokens': 20, 'output_tokens': 10}}
    files = {'a': {'turn_starts': [], 'tool_times': []}}
    rows = [row(i, 'default', 10) for i in range(40)]
    rows += [row(i, 'priority', 2) for i in range(40, 80)]
    default, q = latency.build(files, {'a': rows}, tz=datetime.timezone.utc)
    extra, eq = latency.build(files, {'a': rows}, tz=datetime.timezone.utc, tier_groups=True,
                              plan_windows=[(LAT_T0, LAT_T0 + 86400, 'prolite')])
    tiers = {g['tier']: g for g in extra['tier_groups']}
    check('tier fits have Standard/Fast overheads 10/2',
          tiers.get('standard', {}).get('fit', {}).get('overhead_s') == 10
          and tiers.get('fast', {}).get('fit', {}).get('overhead_s') == 2, str(tiers))
    check('mixed timing remains median 6 and p90 10',
          extra['responses']['median_s'] == 6 and extra['responses']['p90_s'] == 10
          and extra['groups'][0]['median_s'] == 6 and extra['groups'][0]['p90_s'] == 10)
    check('each independent tier fit has 40 samples, zero token slopes and no above-line time',
          all(g['fit'] == {'overhead_s': duration, 'output_tps': None,
                           'uncached_input_tps': None, 'samples': 40}
              and g['n'] == 40 and g['median_s'] == g['p90_s'] == duration
              and g['above_s'] == g['above_share'] == g['median_above_s'] == 0
              for tier, duration in (('standard', 10), ('fast', 2)) for g in [tiers[tier]]))
    check('tier groups and plan context change no existing report fields or quality counters',
          {k: v for k, v in extra.items() if k not in ('tier_groups', 'plan')} == default
          and eq == q and 'tier_groups' not in default and 'plan' not in default
          and extra['plan'] == 'prolite')
    ultra = [row(i, 'ultrafast', 1) for i in range(40)]
    u, _ = latency.build(files, {'a': ultra}, tz=datetime.timezone.utc, tier_groups=True)
    check('a separate 40-response Ultrafast group fits its own one-second overhead',
          u['tier_groups'][0]['tier'] == 'ultrafast'
          and u['tier_groups'][0]['fit'] == {'overhead_s': 1, 'output_tps': None,
                                           'uncached_input_tps': None, 'samples': 40}
          and u['tier_groups'][0]['above_share'] == 0)
    few = rows[:39] + rows[40:79]
    small, _ = latency.build(files, {'a': few}, tier_groups=True)
    check('tier fits require their own 40 samples even when the mixed fit has enough',
          small['groups'][0]['fit'] is not None
          and all(g['fit'] is None and g['above_share'] is None for g in small['tier_groups']))
    many = [row(i, 'default', 10) for i in range(latency.FIT_MAX + 1)]
    capped, _ = latency.build(files, {'a': many}, tier_groups=True)
    check('tier fits keep all accepted responses but fit at most 4,000 samples',
          capped['tier_groups'][0]['n'] == latency.FIT_MAX + 1
          and capped['tier_groups'][0]['fit']['samples'] == latency.FIT_MAX)


def test_latency_caps_and_counters():
    """A sample that cannot be a response time is counted where the report shows it."""
    t = LAT_T0
    iso = lambda x: (datetime.datetime.fromtimestamp(x, datetime.timezone.utc)
                     .isoformat().replace('+00:00', 'Z'))
    base = {'model': 'm', 'effort': 'e', 'turn': 0, 'usage': {'output_tokens': 1}}
    rows = [dict(base, req_ts=iso(t), ts=iso(t + 10)),
            dict(base, req_ts=iso(t + 11), ts=iso(t + 11)),                 # zero
            dict(base, req_ts=None, ts=iso(t + 20)),        # no request stamp: not timed
            dict(base, req_ts=iso(t + 30), ts=iso(t + 30 + latency.RESPONSE_CAP_S + 1)),
            dict(base, req_ts=None, ts=None)]
    tools = [['exec', t + 1, 3.0], ['exec', t + 1, 0.0], ['exec', t + 1, latency.TOOL_CAP_S + 1],
             ['exec', t - 5, 2.0], ['exec', None, None], [['exec'], t + 1, 1.0]]
    # Both are fork children: only a file that declares a parent has history replayed into
    # it, and so a replay boundary for its tool calls.
    files = {'a': {'turn_starts': [iso(t)], 'tool_times': tools, 'parent_thread_id': 'p'},
             'b': {'turn_starts': [], 'tool_times': [['exec', t, 1.0]],
                   'parent_thread_id': 'p'}}
    lat, q = latency.build(files, {'a': rows, 'b': []})
    check('each rejected sample is counted under its own reason',
          (q['latency_samples'], q['latency_nonpositive'], q['latency_over_cap'],
           q['latency_no_start'], q['latency_no_end']) == (1, 1, 1, 1, 1), str(dict(q)))
    # A payload from before `req_ts` existed (an archived one, say) has no request stamps at
    # all; timing it from each previous response would time the gaps between responses.
    old = [{k: v for k, v in r.items() if k != 'req_ts'} for r in _pace_corpus(n=50)]
    lat_old, q_old = latency.build({'a': {}}, {'a': old})
    check('rows with no request stamp are never timed from the previous response',
          lat_old['available'] is False and q_old['latency_no_start'] == 50,
          f"{lat_old} {dict(q_old)}")
    check('each rejected tool call is counted under its own reason',
          (q['tool_calls_timed'], q['tool_nonpositive'], q['tool_over_cap'],
           q['tool_replayed'], q['tool_no_time'], q['tool_without_response'])
          == (2, 1, 1, 1, 1, 1)
          and {x['tool'] for x in lat['tools']} == {'exec', 'unknown'}, str(dict(q)))
    # The same calls in files that declare no parent: nothing there is replayed, so a call
    # before the first timed response -- in a turn that was interrupted -- is still timed.
    own = {k: {kk: vv for kk, vv in v.items() if kk != 'parent_thread_id'}
           for k, v in files.items()}
    _, q_own = latency.build(own, {'a': rows, 'b': []})
    check("a file that declares no parent has none of its tool calls taken for a replay",
          (q_own['tool_calls_timed'], q_own['tool_replayed'], q_own['tool_without_response'])
          == (4, 0, 0), str(dict(q_own)))
    none, q2 = latency.build({'a': {}}, {'a': []})
    check('no responses: the section says why instead of showing zeros',
          none == {'available': False, 'reason': 'no charged responses in range'}, str(none))
    check('the model carries the latency section and its counters',
          'latency' in axis_model() and 'latency_samples' in axis_model()['quality'])


def test_latency_render():
    """The page shows response time as a tile every style draws and as a chart on the time
    axis -- and nothing else: the per-model, hourly, turn and tool breakdowns are in the
    JSON, and no tool name reaches the page, local or public."""
    d = tempfile.mkdtemp()
    _write(d, 'rollout-lat.jsonl', _timed_session(LAT_T0))
    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    model = analyze.analyze(data, charged, counters, scope={'label': 'test'})
    html = render.render(model)
    check('a response-time tile sits among the headline numbers',
          '<div class="k">Response time</div><div class="v">6.0s</div>' in html, '')
    check('there is no response-time panel below the charts',
          'class="panel lat"' not in html and 'Fixed overhead' not in html
          and 'by hour of the day' not in html, '')
    check('the breakdowns the page leaves out are still in the model',
          model['latency']['groups'] and model['latency']['hours']
          and model['latency']['turns']['n'] == 2 and model['latency']['tools'], '')
    pub = render.render(model, public=True)
    check('no tool name reaches the page, local or public',
          all('apply_patch' not in h and '>exec<' not in h for h in (html, pub))
          and 'id="latchart"' in pub, '')
    none = dict(model, latency={'available': False, 'reason': 'none here'})
    check('with no responses timed, the chart says why and the page draws no tile',
          'Response time not available &mdash; none here.' in render.render(none)
          and 'Response time</div><div class="v">' not in render.render(none), '')
    hostile = '<img src=x onerror=alert(1)>'
    check('a hostile reason is escaped',
          hostile not in render.render(dict(model, latency={'available': False,
                                                             'reason': hostile})), '')
    check('durations read as a person would write them, rounding into the next unit',
          [render.secs(x) for x in (0.084, 0.996, 8.44, 9.96, 34.2, 59.6, 125, 3599.6, 3900,
                                    None)]
          == ['0.08s', '1.0s', '8.4s', '10s', '34s', '1m 00s', '2m 05s', '1h 00m', '1h 05m',
              '&mdash;'])


def test_latency_chart():
    """Response time by day is a third chart on the shared time axis: two lines, the median and
    the p90, drawn by the page from per-day rows that carry the daily chart's own local-day
    spans.  No estimate is on it; nothing timed says so."""
    d = tempfile.mkdtemp()
    _write(d, 'rollout-lat.jsonl', _timed_session(LAT_T0))
    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    model = analyze.analyze(data, charged, counters, scope={'label': 'test'})
    days = model['latency']['daily']
    check('each response-time day carries the local-day span the daily bars use',
          days and all(dd['start'] == analyze._day_span(dd['date'])[0]
                       and dd['end'] == analyze._day_span(dd['date'])[1] for dd in days),
          str(days))
    html = render.render(model)
    check('the chart sits right after the daily chart, before the pies',
          html.index('id="dailychart"') < html.index('id="latchart"') < html.index('id="catpie"'))
    panel = html.split('id="latchart"')[1].split('id="catpie"')[0]
    check('the chart is left to the page to draw, with its two series named',
          '<div class="chart" id="latchart"></div>' in html
          and 'median response time</span>' in panel and '>p90</span>' in panel, '')
    check('the chart carries its legend and no description',
          panel.count('<span><i style=') == 2 and '<p class="sub">' not in panel, '')
    check('no estimate is on the page', 'fastest pace' not in html and 'estimated' not in panel,
          '')
    payload = json.loads(re.search(r'window.__TC__ = (\{.*?\});</script>', html).group(1)
                         .replace('\\u003c', '<').replace('\\u003e', '>').replace('\\u0026', '&'))
    check('the payload carries each day as [start, end, timed responses, median, p90]',
          payload['latency']['days'] == [[dd['start'], dd['end'], dd['n'], dd['median_s'],
                                          dd['p90_s']] for dd in days]
          and payload['latency']['min'] == render.HOUR_MIN
          and payload['geo']['lat_h'] == render.LAT_H, str(payload['latency']))
    dm = render._domain(model)
    check('one domain covers the response-time days too',
          dm[0] <= days[0]['start'] and dm[1] >= days[-1]['end'], str(dm))
    none = render.render(dict(model, latency={'available': False, 'reason': 'none here'}))
    lc = none.split('id="latchart"')[1].split('id="catpie"')[0]
    check('nothing timed: the chart keeps its place, says why, and names no series',
          'Response time not available &mdash; none here.' in lc and '>p90<' not in lc
          and '"latency":{"days":[]' in none, '')


# --------------------------------------------------------------- account and rate limits

def _jwt(claims):
    """A syntactically valid JWT whose payload is `claims`.  The signature is never read."""
    def seg(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b'=').decode()
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.{'s' * 40}"


NS = 'https://api.openai.com/auth'
SECRETS = {'access_token': 'ACCESS-TOKEN-SHOULD-NEVER-APPEAR',
           'refresh_token': 'REFRESH-TOKEN-SHOULD-NEVER-APPEAR'}


def _auth_file(home, claims):
    """Write an auth.json beside a sessions root, as Codex lays it out."""
    os.makedirs(os.path.join(home, 'sessions'), exist_ok=True)
    doc = {'auth_mode': 'chatgpt', 'last_refresh': '2026-09-20T00:00:00Z',
           'tokens': dict(SECRETS, id_token=_jwt(claims), account_id='acct-fallback')}
    with open(os.path.join(home, 'auth.json'), 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)
    return os.path.join(home, 'sessions')


def test_account_claims():
    from tokencounter import account

    # Nested namespace object -- the shape the live tokens on this machine carry.
    root = _auth_file(tempfile.mkdtemp(), {
        'email': 'a@example.com', 'name': 'A Person',
        NS: {'chatgpt_account_id': 'acct-nested', 'chatgpt_plan_type': 'pro'}})
    a = account.read(root)
    check('nested namespaced claims are read',
          a['email'] == 'a@example.com' and a['plan'] == 'pro'
          and a['account_id'] == 'acct-nested', str(a))

    # Flattened "<namespace>/<claim>" -- the conventional JWT spelling.
    root2 = _auth_file(tempfile.mkdtemp(), {
        'email': 'b@example.com',
        f'{NS}/chatgpt_account_id': 'acct-flat', f'{NS}/chatgpt_plan_type': 'plus'})
    b = account.read(root2)
    check('flattened namespaced claims are read',
          b['plan'] == 'plus' and b['account_id'] == 'acct-flat', str(b))

    # A token carrying neither shape still identifies the account, via tokens.account_id.
    root3 = _auth_file(tempfile.mkdtemp(), {'email': 'c@example.com'})
    c = account.read(root3)
    check('a token without namespaced claims still identifies the account',
          c['available'] and c['account_id'] == 'acct-fallback' and c['plan'] is None,
          str(c))

    # The whole point of the allow-list: no bearer material can leave this module.
    check('no access or refresh token reaches the account record',
          not any(v in json.dumps([a, b, c]) for v in SECRETS.values()))

    check('--no-account reads nothing',
          account.read(root, enabled=False)['available'] is False)
    check('a missing auth.json degrades, not raises',
          account.read(os.path.join(tempfile.mkdtemp(), 'sessions'))['available'] is False)

    bad = tempfile.mkdtemp()
    os.makedirs(os.path.join(bad, 'sessions'), exist_ok=True)
    with open(os.path.join(bad, 'auth.json'), 'w', encoding='utf-8') as fh:
        fh.write('{not json')
    r = account.read(os.path.join(bad, 'sessions'))
    check('a corrupt auth.json degrades with a reason',
          r['available'] is False and 'JSON' in (r['reason'] or ''), str(r))

    # --sessions-root must not reach into the real ~/.codex: a report over a copied corpus
    # would otherwise be stamped with the live account's email address.
    lonely = os.path.join(tempfile.mkdtemp(), 'elsewhere')
    os.makedirs(lonely, exist_ok=True)
    check('an overridden sessions root looks for auth.json beside it',
          account.auth_path(lonely) == os.path.join(os.path.dirname(lonely), 'auth.json'))


def _rl(pct, resets_at, window=10080, slot='primary', plan='pro'):
    other = 'secondary' if slot == 'primary' else 'primary'
    return {'limit_id': 'codex', 'plan_type': plan, 'rate_limit_reached_type': None,
            slot: {'used_percent': pct, 'window_minutes': window, 'resets_at': resets_at},
            other: None}


def _tc(ts_epoch, pct, resets_at, cum, last, **kw):
    """A `token_count` event carrying both a usage snapshot and a rate-limit snapshot."""
    iso = (datetime.datetime.fromtimestamp(ts_epoch, datetime.timezone.utc)
           .isoformat().replace('+00:00', 'Z'))
    usage = {'input_tokens': last, 'cached_input_tokens': 0, 'output_tokens': 10,
             'reasoning_output_tokens': 0, 'total_tokens': last + 10}
    total = {'input_tokens': cum, 'cached_input_tokens': 0, 'output_tokens': 10,
             'reasoning_output_tokens': 0, 'total_tokens': cum + 10}
    return {'timestamp': iso, 'type': 'event_msg',
            'payload': {'type': 'token_count',
                        'info': {'last_token_usage': usage, 'total_token_usage': total},
                        'rate_limits': _rl(pct, resets_at, **kw)}}


WEEK = 7 * 86400


def _rl_corpus(slot='primary', window=10080):
    """One consumed window, an early reset into a second, and an idle sliding thread."""
    d = tempfile.mkdtemp()
    t0 = 1789000000
    a_reset = t0 + WEEK
    recs = [_rec('session_meta', {'session_id': 'S', 'id': 'S'}, 0)]
    cum = 0
    for i, pct in enumerate((0.0, 20.0, 55.0, 90.0)):
        cum += 1000
        recs.append(_tc(t0 + i * 3600, pct, a_reset, cum, 1000, slot=slot, window=window))
    # The reset: a new window reported at 0%, resetting seven days from *that* instant --
    # sooner than the window it replaces would have expired.
    t1 = t0 + 4 * 3600
    b_reset = t1 + WEEK
    for i, pct in enumerate((0.0, 12.0)):
        cum += 1000
        recs.append(_tc(t1 + i * 3600, pct, b_reset, cum, 1000, slot=slot, window=window))
    _write(d, 'rollout-a.jsonl', recs)

    # The server does not always stop reporting a window when it replaces it: a session
    # served the previous week's window after the new one opened repeats its last reading.
    _write(d, 'rollout-late.jsonl',
           [_rec('session_meta', {'session_id': 'L', 'id': 'L'}, 0),
            _tc(t1 + 2 * 3600, 90.0, a_reset, 0, 0, slot=slot, window=window)])

    # An idle thread: 0% throughout, re-quoting its reset as now+7d on every call.
    t2 = t1 + 3 * 3600
    idle = [_rec('session_meta', {'session_id': 'T', 'id': 'T'}, 0)]
    for i in range(5):
        idle.append(_tc(t2 + i * 60, 0.0, t2 + i * 60 + WEEK, 0, 0, slot=slot, window=window))
    _write(d, 'rollout-b.jsonl', idle)
    return d, t0, t1, a_reset, b_reset


def test_rate_limit_windows():
    d, t0, t1, a_reset, b_reset = _rl_corpus()
    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    rl = analyze.analyze(data, charged, counters, scope={'label': 'test'})['rate_limits']

    check('rate-limit snapshots are extracted',
          rl['available'] and rl['weekly'], str(rl.get('reason')))
    check('one sliding idle window does not become five',
          rl['windows_total'] == 2 and rl['idle_windows'] == 1,
          f"windows={rl['windows_total']} idle={rl['idle_windows']} quotes={rl['quotes']}")

    w0, w1 = rl['windows']
    check('the reset boundary is where the reported percentage drops',
          abs(w1['reset_at'] - t1) < 90, f"boundary={w1['reset_at']} expected~{t1}")
    check('an early reset is not forced onto a seven-day grid',
          w1['resets_at'] == b_reset and b_reset - a_reset < WEEK,
          f"{w1['resets_at']} vs {b_reset}")
    check('peak reported percentage is carried per window',
          w0['peak_pct'] == 90.0 and w1['peak_pct'] == 12.0)

    # Tokens are measured locally and must split at the boundary, not pool into one window.
    check('cumulative tokens are attributed to the window they were spent in',
          w0['tokens']['input'] == 4000 and w1['tokens']['input'] == 2000,
          f"{w0['tokens']['input']} / {w1['tokens']['input']}")
    check('the cumulative curve restarts at each reset',
          bool(w1['cum_points']) and w1['cum_points'][0][1] <= 2000
          and w0['cum_points'][-1][1] == 4000,
          f"{w0['cum_points'][-1]} then {w1['cum_points'][0]}")
    check('a window that was never consumed is not drawn',
          all(w['peak_pct'] for w in rl['windows']))

    # A reading for a window that arrives after its successor opened is not drawn: two
    # limit curves live at the same moment is a chart of a contradiction.
    check('a window reported after its successor opened is not drawn past the boundary',
          rl['late_readings'] == 1 and w0['late_points'] == 1
          and all(p[0] <= w1['reset_at'] for p in w0['pct_points']),
          f"late={rl['late_readings']} w0={w0['late_points']} "
          f"last={w0['pct_points'][-1] if w0['pct_points'] else None} "
          f"boundary={w1['reset_at']}")
    check('the clipped reading still counts toward the reported peak',
          w0['peak_pct'] == 90.0 and w0['late_peak'] == 90.0,
          f"peak={w0['peak_pct']} late_peak={w0['late_peak']}")

    # The chart draws each window's API value.  An unpriced response adds nothing to a
    # window that has a price, and a window with no priced response has no dollars at all,
    # never a $0 that reads as free.
    priced = analyze.rate_limit_windows(
        data, [], now=t1 + 3600,
        usd=[(w0['reset_at'] + 60, 1.25), (w0['reset_at'] + 120, None),
             (w0['reset_at'] + 180, 0.5), (w1['reset_at'] + 60, None)])['windows']
    check('API value is accumulated per window, and an unpriced window has none',
          priced[0]['usd'] == 1.75 and priced[0]['usd_points'][-1][1] == 1.75
          and priced[1]['usd'] is None and priced[1]['usd_points'] == [],
          f"{priced[0]['usd']} {priced[0]['usd_points']} / "
          f"{priced[1]['usd']} {priced[1]['usd_points']}")

    # The weekly window sat in `secondary` behind a 5-hour `primary` in older CLI builds.
    d2, *_ = _rl_corpus(slot='secondary')
    data2 = {p: worker.process(p) for p in rollout.discover(d2)}
    ch2, ct2 = ledger.build(data2)
    rl2 = analyze.analyze(data2, ch2, ct2)['rate_limits']
    check('the weekly window is found by length, not by slot name',
          rl2['available'] and rl2['weekly'] and rl2['windows_total'] == 2,
          f"available={rl2['available']} total={rl2.get('windows_total')}")

    # A `token_count` with `info: null` carries no usage but still carries a window.
    d3 = tempfile.mkdtemp()
    rec = _tc(t0, 33.0, a_reset, 0, 0)
    rec['payload']['info'] = None
    _write(d3, 'rollout-c.jsonl',
           [_rec('session_meta', {'session_id': 'U', 'id': 'U'}, 0), rec])
    r3 = worker.process(os.path.join(d3, 'rollout-c.jsonl'))
    check('a usage-less token_count still yields its rate-limit window',
          len(r3['rate_limits']) == 1 and r3['rate_limits'][0]['max_pct'] == 33.0,
          str(r3['rate_limits']))


def test_cumulative_curve_is_monotonic():
    """The cumulative curve must only ever climb, whatever order the files are read in.

    Responses reach the analyzer one file at a time, in path order, while sessions overlap in
    time: a file read second routinely holds responses older than the one read first.  A
    running total accumulated in that order and then plotted against time steps backwards.
    The corpus below makes the two orders opposites -- the file that sorts first holds the
    *later* half of the week -- so any regression shows up as a curve that falls.
    """
    d = tempfile.mkdtemp()
    t0 = 1789000000
    reset = t0 + WEEK

    def thread(name, sid, start, per, pcts):
        recs = [_rec('session_meta', {'session_id': sid, 'id': sid}, 0)]
        cum = 0
        for i, pct in enumerate(pcts):
            cum += per
            recs.append(_tc(start + i * 1800, pct, reset, cum, per))
        _write(d, name, recs)

    # Sorted by path, 'rollout-a-late' is read before 'rollout-z-early'; sorted by time it
    # comes second.  Unequal per-response sizes keep the two halves distinguishable.
    thread('rollout-z-early.jsonl', 'E', t0, 1000, (0.0, 5.0, 10.0, 15.0))
    thread('rollout-a-late.jsonl', 'L', t0 + 4 * 1800, 5000, (20.0, 25.0, 30.0, 35.0))

    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    rl = analyze.analyze(data, charged, counters, scope={'label': 'test'})['rate_limits']
    pts = rl['windows'][-1]['cum_points'] if rl['windows'] else []

    falls = [(a, b) for a, b in zip(pts, pts[1:])
             if b[1] < a[1] or b[2] < a[2] or b[3] < a[3]]
    check('the cumulative curve never steps backwards',
          bool(pts) and not falls,
          f"{len(falls)} drop(s), first {falls[0] if falls else None} in {pts}")
    check('cumulative points are drawn in time order',
          [p[0] for p in pts] == sorted(p[0] for p in pts), str([p[0] for p in pts]))
    # The curve must end on the true total, not merely climb to some arbitrary value.
    check('the curve ends at the total spent in the window',
          bool(pts) and pts[-1][1] == 4 * 1000 + 4 * 5000,
          f"end={pts[-1][1] if pts else None} expected={4 * 1000 + 4 * 5000}")


def test_account_and_limits_render():
    from tokencounter import account
    d, t0, t1, a_reset, b_reset = _rl_corpus()
    _auth_file(d, {'email': 'who@example.com', NS: {'chatgpt_plan_type': 'pro'}})
    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    acct = account.read(os.path.join(d, 'sessions'))
    model = analyze.analyze(data, charged, counters, scope={'label': 'test'}, account=acct)
    html = render.render(model)

    # The page is a dashboard now: five headline numbers and three charts.  The account
    # is reported through the model and the stdout summary instead, so that is where it is
    # asserted -- the page-level guarantee that survives is the one about secrets.
    check('the account is identified in the model',
          model['account']['available'] and model['account']['email'] == 'who@example.com',
          str(model['account']))
    check('the reset time reaches the model',
          bool(model['rate_limits']['current']['resets_at_iso']),
          str(model['rate_limits'].get('current')))
    check('no bearer material reaches the HTML',
          not any(v in html for v in SECRETS.values()))
    check('the limit series are embedded for the chart',
          '"cum_points"' in html and '"pct_points"' in html)
    check('the weekly limit is a reported percentage, never a token conversion',
          'Weekly limit used' in html and '%' in html
          and 'tokens per percent' not in html.lower())

    # Without an account the page must still render, and must not imply one.
    bare = analyze.analyze(data, charged, counters, scope={'label': 'test'},
                           account=account.blank('disabled with --no-account'))
    h2 = render.render(bare)
    check('a report with no account renders and names none',
          '<html' in h2 and 'who@example.com' not in h2
          and bare['account']['reason'] == 'disabled with --no-account',
          str(bare['account']))


def _reached(t, info=None, five_hour=False):
    """A `token_count` whose rate-limit snapshot says a limit was reached."""
    rl = _rl(100.0, LAT_T0 + WEEK)
    if five_hour:
        rl['secondary'] = rl['primary']
        rl['primary'] = {'used_percent': 100.0, 'window_minutes': 300, 'resets_at': LAT_T0 + 3600}
    rl['rate_limit_reached_type'] = 'primary'
    return _at(t, 'event_msg', {'type': 'token_count', 'info': info, 'rate_limits': rl})


def _limit_corpus():
    """A parent refused on two days, a fork child that replays all of it at its creation and
    is then refused once on its own, and an unrelated session refused in its opening burst.

    Days, in UTC: the parent's two refusals on day 0 and one on day 1 (that one beside the
    usage of a response, with both windows full), the child's own on day 2, the other
    session's on day 3.  One of the parent's is stamped with no readable time.
    """
    d = tempfile.mkdtemp()
    t = LAT_T0
    work = [_user(t + 1), _reached(t + 2), _user(t + 100), _reached(t + 101),
            _tc(t + 200, 40.0, t + WEEK, 1000, 1000),               # a snapshot, not reached
            _reached(t + 86400, info=_tc(0, 0, 0, 2000, 1000)['payload']['info'], five_hour=True),
            dict(_reached(t + 86500), timestamp='not a time')]
    _write(d, 'rollout-1-parent.jsonl',
           [_at(t, 'session_meta', {'session_id': 'F', 'id': 'P'}),
            _at(t, 'turn_context', {'model': 'm1', 'effort': 'low'})] + work)
    tc = t + 2 * 86400 + 500                            # the child's creation time
    replay = [dict(r, timestamp=_at(tc + k / 1000, 'x', {})['timestamp'])
              for k, r in enumerate(work)]
    _write(d, 'rollout-2-child.jsonl',
           [_at(tc, 'session_meta', {'session_id': 'F', 'id': 'C', 'parent_thread_id': 'P'})]
           + replay
           + [_at(tc + 3, 'turn_context', {'model': 'm1', 'effort': 'low'}),
              _user(tc + 5), _reached(tc + 5.4)])
    ts = t + 3 * 86400
    _write(d, 'rollout-3-solo.jsonl',
           [_at(ts, 'session_meta', {'session_id': 'S', 'id': 'S'}), _reached(ts + .001)])
    return d


def test_limit_events():
    """A rate-limit event is a snapshot in which Codex logged a limit as reached: one a
    snapshot, counted per local day, with a fork child's replayed copies left out."""
    d = _limit_corpus()
    data = {p: worker.process(p) for p in rollout.discover(d)}
    by = {os.path.basename(p).split('-')[2].split('.')[0]: r for p, r in data.items()}
    par = by['parent']
    check('the worker keeps one event a reached snapshot, whichever windows it names',
          par['limit_events'] == [LAT_T0 + 2, LAT_T0 + 101, LAT_T0 + 86400]
          and par['counters'].get('limit_event_no_time') == 1, str(par['limit_events']))
    check('and the metrics-only pass keeps the same events',
          worker.metrics_only([p for p in data if 'parent' in p][0])['limit_events']
          == par['limit_events'], '')
    kid = by['child']
    check('a fork child\'s opening burst ends where its own work begins',
          abs(kid['opening_burst_end'] - (LAT_T0 + 2 * 86400 + 500 + .006)) < 1e-6
          and len(kid['limit_events']) == 5, str(kid['opening_burst_end']))
    ev, q = analyze.limit_events(data, tz=datetime.timezone.utc)
    day = lambda k: (datetime.datetime.fromtimestamp(LAT_T0, datetime.timezone.utc).date()
                     + datetime.timedelta(days=k)).isoformat()
    check('events are counted per day; the child\'s replayed copies are not, its own refusal is',
          [(x['date'], x['n']) for x in ev['daily']]
          == [(day(0), 2), (day(1), 1), (day(2), 1), (day(3), 1)]
          and ev['total'] == 5 and q['limit_events'] == 5 and q['limit_events_replayed'] == 4,
          str(ev) + str(q))
    check('each day carries the local-day span the other charts use',
          all((x['start'], x['end']) == analyze._day_span(x['date'], datetime.timezone.utc)
              for x in ev['daily']), str(ev['daily']))
    orphan = {p: dict(r, parent_thread_id=None) for p, r in data.items()}
    check('without a declared parent nothing is taken for a replay',
          analyze.limit_events(orphan)[0]['total'] == 9, '')
    charged, counters = ledger.build(data)
    model = analyze.analyze(data, charged, counters, scope={'label': 'test'})
    check('the model carries the events, and the quality counters say what was left out',
          model['limit_events']['total'] == 5 and model['quality']['limit_events_replayed'] == 4
          and model['quality']['limit_event_no_time'] == 1, str(model['quality']))
    none = analyze.limit_events({p: dict(r, limit_events=[]) for p, r in data.items()})
    check('no events is an empty list, not a missing key', none[0] == {'total': 0, 'daily': []},
          str(none))


def test_limit_events_chart():
    """The events are bars on the response-time chart, on an axis of their own; a report with
    events and nothing timed still draws the chart, and one with neither says why."""
    d = _limit_corpus()
    _write(d, 'rollout-4-lat.jsonl', _timed_session(LAT_T0))
    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    model = analyze.analyze(data, charged, counters, scope={'label': 'test'})
    html = render.render(model)
    panel = html.split('id="latchart"')[1].split('id="catpie"')[0]
    payload = lambda h: json.loads(re.search(r'window.__TC__ = (\{.*?\});</script>', h).group(1)
                                   .replace('\\u003c', '<').replace('\\u003e', '>')
                                   .replace('\\u0026', '&'))
    evd = model['limit_events']['daily']
    check('the payload carries each event day as [start, end, events]',
          payload(html)['latency']['events'] == [[x['start'], x['end'], x['n']] for x in evd]
          and len(evd) == 4, str(payload(html)['latency']))
    check('the legend names the bars and their axis beside the two lines',
          'median response time</span>' in panel and '>p90</span>' in panel
          and 'rate-limit events (right axis)</span>' in panel
          and 'no rate-limit events' not in panel, panel[:400])
    dm = render._domain(model)
    check('one domain covers the event days too',
          dm[0] <= evd[0]['start'] and dm[1] >= evd[-1]['end'], str(dm))
    check('the public page carries the counts too',
          payload(render.render(model, public=True))['latency']['events']
          == payload(html)['latency']['events'], '')
    quiet = dict(model, limit_events={'total': 0, 'daily': []})
    qp = render.render(quiet).split('id="latchart"')[1].split('id="catpie"')[0]
    check('with no events the legend says none were logged, and no bar is sent',
          'no rate-limit events logged</span>' in qp and '"events":[]' in render.render(quiet),
          qp[:400])
    blocked = render.render(dict(model, latency={'available': False, 'reason': 'none here'}))
    bp = blocked.split('id="latchart"')[1].split('id="catpie"')[0]
    check('events and nothing timed: the page draws the bars, and the legend says why no lines',
          '<div class="chart" id="latchart"></div>' in blocked
          and '<span>Response time not available &mdash; none here</span>' in bp
          and 'rate-limit events (right axis)' in bp and '>p90<' not in bp
          and payload(blocked)['latency']['events'], bp[:400])
    neither = render.render(dict(quiet, latency={'available': False, 'reason': 'none here'}))
    check('neither: the chart keeps its place and says why, as before',
          'Response time not available &mdash; none here.</p>' in neither, '')
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc, _m, _h = _run_main(['--sessions-root', d, '--no-account'])
    check('the terminal summary counts the events and the days they fell on',
          rc == 0 and 'rate-limit events 5 on 4 days' in buf.getvalue(), buf.getvalue())


# ------------------------------------------------------------------ one shared time axis

def _at(t, kind, payload):
    """A record stamped at an absolute instant, not at second `n` of one fixed day."""
    iso = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
           .isoformat().replace('+00:00', 'Z'))
    return {'timestamp': iso, 'type': kind, 'payload': payload}


def axis_model():
    """A corpus with limit windows, several days of usage, and tokenized content.

    Also the fixture `scripts/test_page.js` renders, so the page's own JavaScript is
    exercised against the same data these assertions are written against.
    """
    d, t0, _t1, _a, _b = _rl_corpus()
    recs = [_at(t0, 'session_meta', {'session_id': 'C', 'id': 'C', 'cwd': '/w',
                                     'base_instructions': {'text': 'SYS ' * 60}}),
            _at(t0, 'turn_context', {'model': 'm1', 'effort': 'max'})]
    for i in range(3):                         # one turn a day, three days running
        ts = t0 + i * 86400
        recs.append(_at(ts, 'response_item', {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': f'ask {i} ' * 60}]}))
        recs.append(_at(ts + 60, 'token_usage_record', {'response_id': f'r{i}', 'usage': {
            'input_tokens': 5000 + i, 'cached_input_tokens': 1000, 'output_tokens': 20,
            'reasoning_output_tokens': 5, 'total_tokens': 5020 + i}}))
    _write(d, 'rollout-content.jsonl', recs)

    # A second session, days later and with content of its own: with only one file, every
    # tokenized byte in the corpus would share a single bucket, and a chart that never
    # recomposed would pass a test written against it.
    t2 = t0 + 5 * 86400
    later = [_at(t2, 'session_meta', {'session_id': 'E', 'id': 'E', 'cwd': '/w',
                                      'base_instructions': {'text': 'OTHER SYS ' * 40}}),
             _at(t2, 'turn_context', {'model': 'm2', 'effort': 'low'}),
             _at(t2 + 30, 'response_item', {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': 'later question ' * 50}]}),
             _at(t2 + 60, 'token_usage_record', {'response_id': 'r9', 'usage': {
                 'input_tokens': 7000, 'cached_input_tokens': 2000, 'output_tokens': 30,
                 'reasoning_output_tokens': 7, 'total_tokens': 7030}})]
    _write(d, 'rollout-later.jsonl', later)

    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    return analyze.analyze(data, charged, counters, scope={'label': 'test'})


def page_fixture():
    model = axis_model()
    # The corpus's models are not in any price table; the page test should still draw the API
    # value tile among the others, in every style.
    model['api_value'] = {'available': True, 'usd': 1234.5, 'unpriced': 2,
                          'prices': {'as_of': '2026-10-04'}}
    # The corpus times one response a day, and a day needs HOUR_MIN to be a point on the
    # response-time chart; the page test needs points to move.  Same days, same spans, more
    # responses.
    # The first day is kept thin, so the page test sees a day that is not drawn and a line
    # that breaks around it; the corpus's own gap (no sessions for two days) breaks it too.
    model['latency']['daily'] = [
        dict(d, n=(render.HOUR_MIN - 2 if i == 0 else render.HOUR_MIN + 7),
             median_s=20.0 + i, p90_s=45.0 + 2 * i)
        for i, d in enumerate(model['latency']['daily'])]
    # Rate-limit events on the thin day, on a drawn day, and on the day after it, inside the
    # corpus's gap: a day the limit blocked outright, with bars and no timed response.
    lat_days = model['latency']['daily']
    gap = (datetime.date.fromisoformat(lat_days[2]['date'])
           + datetime.timedelta(days=1)).isoformat()
    model['limit_events'] = {'total': 11, 'daily': [
        {'date': d, 'n': n, 'start': analyze._day_span(d)[0], 'end': analyze._day_span(d)[1]}
        for d, n in ((lat_days[0]['date'], 3), (lat_days[2]['date'], 1), (gap, 7))]}
    return render.render(model)


def test_shared_time_axis():
    """The three charts are read against each other, so they must share one axis.

    A moment has to land on the same x in the limit chart and in the daily chart, and the
    composition pie has to be recomposable over the same range.  None of that holds unless
    the model carries a real span for every bucket it publishes, and the page draws both
    charts over one domain on one geometry.
    """
    import re
    model = axis_model()
    daily = model['daily']
    check('every daily bucket carries its own local-day span',
          len(daily) >= 3 and all(x['start'] is not None
                                  and 23 * 3600 <= x['end'] - x['start'] <= 25 * 3600
                                  for x in daily),
          str([(x['date'], x['start'], x['end']) for x in daily]))
    check('the daily buckets run in order, without overlapping',
          all(a['end'] <= b['start'] for a, b in zip(daily, daily[1:])),
          str([(x['date'], x['start'], x['end']) for x in daily]))

    # The pie is filtered by time, so the timeline has to account for exactly the tokens the
    # corpus-wide figure does: a bucket lost here is content that disappears from the chart
    # at full zoom, with nothing to show that it did.
    series = model['cat_series']
    rolled = {}
    for t, cats in series:
        for k, v in cats.items():
            rolled[k] = rolled.get(k, 0) + v
    corpus = {c['category']: c['tokens'] for c in model['categories'] if c['tokens']}
    check('the content timeline accounts for exactly the corpus categories',
          rolled == corpus and sum(rolled.values()) > 0,
          f'{sorted(rolled.items())} vs {sorted(corpus.items())}')
    bucket = model['cat_bucket_s']
    check('content buckets are aligned to the bucket length the model publishes',
          bucket > 0 and all(t % bucket == 0 for t, _ in series),
          str([t for t, _ in series][:5]))

    dom = render._domain(model)
    pts = [p[0] for w in model['rate_limits']['windows'] for p in w['cum_points']]
    check('one domain covers every series the page can draw',
          bool(dom) and all(dom[0] <= t <= dom[1] for t in pts)
          and all(dom[0] <= x['start'] and x['end'] <= dom[1] for x in daily)
          and all(dom[0] <= t and t + bucket <= dom[1] for t, _ in series),
          f'domain={dom} points={len(pts)}')

    page = render.render(model)
    check('the page carries the one domain and the one geometry both charts read',
          f'"domain":[{dom[0]},{dom[1]}]' in page
          and f'"w":{render.CHART_W},"l":{render.CHART_L},"r":{render.CHART_R}' in page,
          page[page.find('"geo"'):page.find('"geo"') + 90])

    vb = re.search(r'<svg viewBox="0 0 (\d+) (\d+)" data-h', page)
    clip = re.search(r'<rect class="clip" x="(\d+)" y="0" width="(\d+)"', page)
    check('the daily chart is drawn on the geometry the limit chart is given',
          bool(vb) and bool(clip) and int(vb.group(1)) == render.CHART_W
          and int(clip.group(1)) == render.CHART_L
          and int(clip.group(2)) == render.CHART_W - render.CHART_L - render.CHART_R,
          f'{vb and vb.groups()} {clip and clip.groups()}')

    # The bars are placed by the domain, not by their position in the list.  This is the
    # cross-reference itself: a day must start where the limit chart puts that instant.
    bar = re.search(r'<g class="bar" data-a="(\d+)" data-b="(\d+)" '
                    r'transform="translate\(([\d.]+),0\) scale\(([\d.]+),1\)"', page)
    plot = render.CHART_W - render.CHART_L - render.CHART_R
    ok = False
    if bar:
        a, b = int(bar.group(1)), int(bar.group(2))
        x, sx = float(bar.group(3)), float(bar.group(4))
        want_x = render.CHART_L + (a - dom[0]) / (dom[1] - dom[0]) * plot
        want_sx = (b - a) / (dom[1] - dom[0]) * plot
        ok = abs(x - want_x) < 0.05 and abs(sx - want_sx) < 0.05
    check('a day bar is placed by the shared domain, in unit x',
          ok, bar.group(0) if bar else 'no bar drawn')

    check('all three charts are on the page, with no toolbar above them',
          'id="tcbar"' not in page and 'id="catpie"' in page
          and '"cats"' in page and 'id="dailychart"' in page and 'id="rlchart"' in page)

    # A report with no rate limits has only the daily axis, and must still be drawable.
    bare = dict(model, rate_limits={'available': False, 'reason': 'none recorded'})
    dom2 = render._domain(bare)
    h2 = render.render(bare)
    check('the axis survives a report with no limit snapshots',
          bool(dom2) and dom2[0] <= daily[0]['start'] and 'id="dailychart"' in h2
          and 'No rate-limit snapshots' in h2, str(dom2))


# --------------------------------------------------------------------------- API value

# A price table of its own, so these tests do not move when scripts/fetch_prices.py
# re-vendors the real one.  `big` has long-context, cache-write and Fast rates, `flat` has
# none of those, and `pro` has no cached rate.
PRICE_TABLE = {
    'as_of': '2026-01-02', 'source': 'test', 'unit_tokens': 1_000_000,
    'long_context_threshold': 272_000, 'web_search_per_call': 0.01,
    'models': {
        'big': {'standard': {'input': 2.0, 'cached_input': 0.2, 'cache_write': 2.5,
                             'output': 10.0,
                             'long': {'input': 4.0, 'cached_input': 0.4, 'cache_write': 5.0,
                                      'output': 15.0}},
                'fast': {'input': 4.0, 'cached_input': 0.4, 'cache_write': 5.0,
                         'output': 20.0, 'long': None},
                'long_context': True},
        'flat': {'standard': {'input': 1.0, 'cached_input': 0.1, 'cache_write': None,
                              'output': 8.0, 'long': None},
                 'long_context': False},
        'pro': {'standard': {'input': 30.0, 'cached_input': None, 'cache_write': None,
                             'output': 180.0, 'long': None},
                'long_context': False},
    },
}


def _price_file(table=None):
    path = os.path.join(tempfile.mkdtemp(), 'prices.json')
    with open(path, 'w', encoding='utf-8') as fh:
        if isinstance(table, str):
            fh.write(table)
        else:
            json.dump(PRICE_TABLE if table is None else table, fh)
    return path


def _row(model, inp, cached=0, out=0, writes=None, tier=None, reasoning=0, ws=0):
    u = {'input_tokens': inp, 'cached_input_tokens': cached, 'output_tokens': out,
         'reasoning_output_tokens': reasoning, 'total_tokens': inp + out}
    if writes is not None:
        u['cache_write_input_tokens'] = writes
    return {'model': model, 'tier': tier, 'usage': u, 'web_search': ws}


def test_price_table():
    """The vendored table loads with the network blocked; a malformed one is refused whole."""
    code = (
        'import socket, sys\n'
        'def deny(*a, **k): raise RuntimeError("network access attempted")\n'
        'socket.socket = deny; socket.create_connection = deny\n'
        f'sys.path.insert(0, {LIB!r})\n'
        'from tokencounter import pricing\n'
        'import json\n'
        't, why = pricing.load()\n'
        'print(json.dumps([why, len(t["models"]), t["long_context_threshold"],'
        ' "gpt-5.5" in t["models"] and "gpt-5.3-codex" in t["models"]] if t else [why]))\n'
    )
    p = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    try:
        got = json.loads(p.stdout)
    except ValueError:
        got = None
    check('the vendored price table loads offline (network blocked)',
          p.returncode == 0 and got and got[0] is None and got[1] > 20
          and got[2] == 272_000 and got[3] is True, (p.stdout + p.stderr)[-400:])
    t, why = pricing.load(_price_file())
    check('a well-formed table loads', why is None and set(t['models']) == {'big', 'flat', 'pro'},
          str(why))
    bad = {
        'not JSON': '{"models": ',
        'no models': dict(PRICE_TABLE, models={}),
        'no standard rates': dict(PRICE_TABLE, models={'x': {'fast': PRICE_TABLE['models']
                                                              ['big']['fast']}}),
        'a negative rate': dict(PRICE_TABLE, models={'x': {'standard': dict(
            PRICE_TABLE['models']['flat']['standard'], output=-1)}}),
        'a string rate': dict(PRICE_TABLE, models={'x': {'standard': dict(
            PRICE_TABLE['models']['flat']['standard'], input='1.0')}}),
        'no threshold': {k: v for k, v in PRICE_TABLE.items() if k != 'long_context_threshold'},
        'a zero unit_tokens': dict(PRICE_TABLE, unit_tokens=0),
        'a string unit_tokens': dict(PRICE_TABLE, unit_tokens='1000000'),
        'an infinite rate': dict(PRICE_TABLE, models={'x': {'standard': dict(
            PRICE_TABLE['models']['flat']['standard'], cached_input=float('inf'))}}),
        'a NaN rate': dict(PRICE_TABLE, models={'x': {'standard': dict(
            PRICE_TABLE['models']['flat']['standard'], output=float('nan'))}}),
    }
    for label, table in bad.items():
        got, why = pricing.load(_price_file(table))
        check(f'a table with {label} is refused, with a reason', got is None and bool(why), why)
    got, why = pricing.load(os.path.join(tempfile.mkdtemp(), 'missing.json'))
    check('a missing table is refused, with a reason', got is None and 'unreadable' in why, why)


def test_pricing_math():
    """Each response priced at its own model, tier and prompt size, with OpenAI's input split."""
    t, _ = pricing.load(_price_file())
    near = lambda a, b: a is not None and abs(a - b) < 1e-12

    usd, why = pricing.price(t, _row('flat', 1000, 400, 50, reasoning=25))
    check('ordinary, cached and output tokens at their own rates',
          why is None and near(usd, (600 * 1.0 + 400 * 0.1 + 50 * 8.0) / 1e6), f'{usd} {why}')
    check('reasoning is part of output, never added again',
          near(pricing.price(t, _row('flat', 1000, 400, 50))[0], usd))
    usd, _ = pricing.price(t, _row('big', 10_000, 4_000, 100, writes=5_000))
    check('cache writes are part of input, priced at the write rate, apart from cached reads',
          near(usd, (1_000 * 2.0 + 4_000 * 0.2 + 5_000 * 2.5 + 100 * 10.0) / 1e6), str(usd))
    check('a model with no cache-write rate bills writes as ordinary input',
          near(pricing.price(t, _row('flat', 1000, 400, 50, writes=300))[0],
               (600 * 1.0 + 400 * 0.1 + 50 * 8.0) / 1e6))
    check('a model with no cached rate bills cached input as ordinary input',
          near(pricing.price(t, _row('pro', 2000, 1000))[0], 2000 * 30.0 / 1e6))

    check('a prompt of exactly the threshold is short context',
          near(pricing.price(t, _row('big', 272_000))[0], 272_000 * 2.0 / 1e6))
    check('one token over prices the whole request at long-context rates',
          near(pricing.price(t, _row('big', 272_001, 1_000, 10))[0],
               (271_001 * 4.0 + 1_000 * 0.4 + 10 * 15.0) / 1e6))
    check('a model with no long-context rates keeps its rates at any size',
          near(pricing.price(t, _row('flat', 400_000))[0], 400_000 * 1.0 / 1e6))
    check('a long prompt in a tier with no long-context rate is unpriced, not guessed',
          pricing.price(t, _row('big', 300_000, tier='priority')) == (None, 'long_context'))

    check('Fast mode (priority) is priced at Fast rates',
          near(pricing.price(t, _row('big', 1000, tier='priority'))[0], 1000 * 4.0 / 1e6))
    check("Codex's default sentinel is standard",
          near(pricing.price(t, _row('big', 1000, tier='default'))[0], 1000 * 2.0 / 1e6))
    check('a tier the model has no rates for is unpriced',
          pricing.price(t, _row('flat', 1000, tier='flex')) == (None, 'tier'))
    check('an unknown tier is unpriced',
          pricing.price(t, _row('big', 1000, tier='scale')) == (None, 'tier'))
    check('a dated snapshot is priced as its alias',
          near(pricing.price(t, _row('flat-2026-01-01', 1000))[0], 1000 * 1.0 / 1e6))
    check('an unlisted model is unpriced', pricing.price(t, _row('nope', 1000)) == (None, 'model')
          and pricing.price(t, _row(None, 1000)) == (None, 'model'))
    damaged = {'model': 'flat', 'usage': {'input_tokens': 'x', 'cached_input_tokens': None,
                                          'output_tokens': 1000}}
    check('a damaged count prices as zero instead of raising',
          near(pricing.price(t, damaged)[0], 1000 * 8.0 / 1e6))
    check('web searches carry the per-call fee',
          near(pricing.web_search(t, _row('flat', 0, ws=3)), 0.03))


def _token_count(t, last, total):
    return _at(t, 'event_msg', {'type': 'token_count',
                                'info': {'last_token_usage': last, 'total_token_usage': total}})


def _searches_corpus(d):
    """Three files: an explicit stream with a context snapshot and two tier changes, a file
    whose legacy record is written before its explicit one, and a legacy-only file."""
    t = 1_790_000_000
    _write(d, 'rollout-a.jsonl', [
        _at(t, 'session_meta', {'session_id': 'A', 'id': 'A'}),
        _at(t, 'turn_context', {'model': 'big', 'effort': 'high'}),
        _user(t + 1),
        _at(t + 2, 'response_item', {'type': 'web_search_call', 'status': 'completed'}),
        _at(t + 3, 'response_item', {'type': 'web_search_call', 'status': 'completed'}),
        _at(t + 4, 'token_usage_record', {'response_id': 'a0', 'usage': {
            'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 50,
            'cached_input_tokens': 0, 'reasoning_output_tokens': 0}}),
        _at(t + 5, 'token_usage_record', _usage('a1', 1000, 0, 10)),
        _at(t + 6, 'event_msg', {'type': 'thread_settings_applied', 'thread_settings': {
            'model': 'big', 'model_provider_id': 'openai', 'service_tier': 'priority'}}),
        _user(t + 7),
        _at(t + 8, 'token_usage_record', _usage('a2', 2000, 1000, 20)),
        _at(t + 9, 'event_msg', {'type': 'thread_settings_applied', 'thread_settings': {
            'model': 'big', 'model_provider_id': 'openai'}}),
        _user(t + 10),
        _at(t + 12, 'token_usage_record', _usage('a3', 3000, 0, 5)),
    ])
    _write(d, 'rollout-b.jsonl', [
        _at(t, 'session_meta', {'session_id': 'B', 'id': 'B'}),
        _at(t, 'turn_context', {'model': 'big', 'effort': 'high'}),
        _user(t + 1),
        _at(t + 2, 'response_item', {'type': 'web_search_call', 'status': 'completed'}),
        _token_count(t + 3, {'input_tokens': 500, 'cached_input_tokens': 0,
                             'output_tokens': 5, 'reasoning_output_tokens': 0,
                             'total_tokens': 505},
                     {'input_tokens': 500, 'output_tokens': 5, 'total_tokens': 505}),
        _at(t + 3, 'token_usage_record', _usage('b1', 500, 0, 5)),
    ])
    snap = {'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0,
            'reasoning_output_tokens': 0, 'total_tokens': 40}
    real = {'input_tokens': 700, 'cached_input_tokens': 0, 'output_tokens': 7,
            'reasoning_output_tokens': 0, 'total_tokens': 707}
    _write(d, 'rollout-c.jsonl', [
        _at(t, 'session_meta', {'session_id': 'C', 'id': 'C'}),
        _at(t, 'turn_context', {'model': 'flat', 'effort': 'low'}),
        _user(t + 1),
        _at(t + 2, 'response_item', {'type': 'web_search_call', 'status': 'completed'}),
        _token_count(t + 3, snap, dict(snap)),
        _token_count(t + 4, real, dict(real, total_tokens=747)),
        _token_count(t + 5, real, dict(real, total_tokens=747)),   # a repeat: never charged
    ])


def test_tier_and_searches_extracted():
    """Each charged row carries the tier requested before it and the searches made for it."""
    d = tempfile.mkdtemp()
    _searches_corpus(d)
    for label, fn in (('full', worker.process), ('metrics-only', worker.metrics_only)):
        data = {os.path.basename(p): fn(p) for p in rollout.discover(d)}
        charged, _ = ledger.build(data)
        got = {f: [(r['tier'], r['web_search']) for r in rows] for f, rows in charged.items()}
        inferred = [r['tier_inferred'] for r in charged.get('rollout-a.jsonl', [])]
        check(f'{label}: the tier follows each settings snapshot',
              [x[0] for x in got.get('rollout-a.jsonl', [])][1:] == ['priority', 'default'],
              str(got))
        check(f"{label}: the first turn, whose snapshot Codex never persists, takes the "
              f"file's first snapshot and says so",
              got.get('rollout-a.jsonl', [(None,)])[0][0] == 'priority'
              and inferred == [True, False, False], f'{got} {inferred}')
        check(f'{label}: a context snapshot does not take the searches before it',
              [x[1] for x in got.get('rollout-a.jsonl', [])] == [2, 0, 0], str(got))
        check(f'{label}: a search lands on the stream the ledger charges, whichever writes first',
              got.get('rollout-b.jsonl') == [(None, 1)], str(got))
        check(f'{label}: on the legacy stream, searches land on the charged record',
              got.get('rollout-c.jsonl') == [(None, 1)], str(got))
        classes = {f: [pricing.tier_class(r.get('tier')) for r in rows]
                   for f, rows in charged.items()}
        check(f'{label}: sharing classifies A as Fast, Fast, Standard and B/C as Standard',
              classes == {'rollout-a.jsonl': ['fast', 'fast', 'standard'],
                          'rollout-b.jsonl': ['standard'], 'rollout-c.jsonl': ['standard']},
              str(classes))


def test_sharing_tier_classes():
    check('sharing has exactly the three canonical classes in order',
          pricing.TIER_CLASSES == ('standard', 'fast', 'ultrafast'))
    for raw, want in [(None, 'standard'), ('', 'standard'), ('  \t', 'standard'),
                      ('default', 'standard'), ('auto', 'standard'), ('standard', 'standard'),
                      (' DEFAULT ', 'standard'), (' Auto ', 'standard'), (' StAnDaRd ', 'standard'),
                      ('priority', 'fast'), ('fast', 'fast'), (' PrIoRiTy\t', 'fast'),
                      (' FAST ', 'fast'), ('ultrafast', 'ultrafast'), (' UlTrAfAsT ', 'ultrafast'),
                      ('flex', 'standard'), (' FLEX ', 'standard'), ('scale', 'standard'),
                      ('unrecorded', 'standard'), ('other', 'standard'), (1, 'standard'),
                      (False, 'standard'), ({}, 'standard')]:
        check(f'sharing tier {raw!r} classifies as {want}', pricing.tier_class(raw) == want)
    check('API pricing still names an absent raw tier Standard', pricing.tier_name(None) == 'standard')


def test_replayed_search_not_charged_twice():
    """A search the parent made after its last usage record, replayed into a fork child, is
    not charged to the child's first response."""
    d = tempfile.mkdtemp()
    t = 1_790_000_000
    parent = [
        _at(t, 'session_meta', {'session_id': 'P', 'id': 'P'}),
        _at(t, 'turn_context', {'model': 'big', 'effort': 'high'}),
        _user(t + 1),
        _at(t + 2, 'response_item', {'type': 'web_search_call', 'status': 'completed'}),
        _at(t + 3, 'token_usage_record', _usage('p1', 100, 0, 1)),
        _user(t + 10),
        _at(t + 11, 'response_item', {'type': 'web_search_call', 'status': 'completed'}),
        _at(t + 12, 'event_msg', {'type': 'turn_aborted', 'reason': 'interrupted'}),
    ]
    _write(d, 'rollout-parent.jsonl', parent)
    c = t + 3600
    # The child replays the parent's records stamped a millisecond apart, then works.
    child = [_at(c, 'session_meta', {'session_id': 'P', 'id': 'K', 'parent_thread_id': 'P'})]
    for i, r in enumerate(parent[1:], 1):
        child.append(dict(r, timestamp=_at(c + i / 1000, 'x', {})['timestamp']))
    child += [_user(c + 5), _at(c + 6, 'token_usage_record', _usage('k1', 200, 0, 2))]
    _write(d, 'rollout-child.jsonl', child)
    data = {os.path.basename(p): worker.metrics_only(p) for p in rollout.discover(d)}
    charged, _ = ledger.build(data)
    got = {f: [(r['response_id'], r['web_search']) for r in rows] for f, rows in charged.items()}
    check("a fork child's replayed, unanswered search is not charged to its first response",
          got == {'rollout-parent.jsonl': [('p1', 1)], 'rollout-child.jsonl': [('k1', 0)]},
          str(got))


def test_fetch_prices_parser():
    """scripts/fetch_prices.py, offline, against the pricing page the vendored table came from."""
    import fetch_prices as fp
    with open(fp.FIXTURE, encoding='utf-8') as fh:
        md = fh.read()
    models, threshold, ws = fp.parse_pricing(md)
    check('the threshold and the web search fee are read off the page',
          threshold == 272_000 and ws == 0.01, f'{threshold} {ws}')
    m = models.get('gpt-5.5') or {}
    check('a flagship model has a rate per tier, its name without the context note',
          set(m) == {'standard', 'flex', 'fast'} and m['standard']['input'] == 5.0
          and m['standard']['long']['output'] == 45.0 and m['fast']['long'] is None
          and not any('(' in k for k in models), str(m)[:300])
    check('Batch is never read as a tier',
          not any('batch' in t for t in models.values()), '')
    check('the Codex table is read per tier',
          (models.get('gpt-5.3-codex') or {}).get('fast', {}).get('input') == 3.5, '')
    cy = (models.get('gpt-5.6-cyber') or {}).get('standard') or {}
    check('the Cyber table is read as standard rates, and repeats keep the flagship rates',
          (cy.get('input'), cy.get('cache_write'), cy.get('output')) == (12.5, 15.625, 75.0)
          and set(models['gpt-5.6-sol']) == {'standard', 'flex', 'fast'}, str(cy))
    check('image, audio and embedding models are not taken for text models',
          not any(k.startswith(('gpt-image', 'gpt-realtime', 'text-embedding', 'tts'))
                  for k in models), '')
    page = fp.parse_model_page('| Metric | Price | Unit |\n| --- | ---: | --- |\n'
                               '| Input | $1.25 | 1M tokens |\n| Cached input | $0.125 | 1M tokens |\n'
                               '| Output | $10 | 1M tokens |\n')
    check('a model page gives standard rates',
          (page['input'], page['cached_input'], page['output']) == (1.25, 0.125, 10.0), str(page))
    with open(pricing.DEFAULT_PRICES, encoding='utf-8') as fh:
        vendored = json.load(fh)['models']
    stale = [k for k, v in models.items()
             if {t: r for t, r in (vendored.get(k) or {}).items() if t in v} != v]
    check('the vendored table is what the parser makes of the page kept beside it',
          not stale, str(stale[:5]))


def test_interrupted_turns_counted():
    """Aborted turns are counted, a fork child's replayed copy of its parent's left out."""
    d = tempfile.mkdtemp()
    t = 1_790_000_000
    _write(d, 'rollout-parent.jsonl', [
        _at(t, 'session_meta', {'session_id': 'P', 'id': 'P'}),
        _user(t + 1),
        _at(t + 50, 'event_msg', {'type': 'turn_aborted', 'reason': 'interrupted'}),
    ])
    c = t + 3600
    # The replay is stamped with the child's creation time a millisecond apart, record by
    # record, as Codex writes it; the child's own work starts seconds later.
    _write(d, 'rollout-child.jsonl', [
        _at(c, 'session_meta', {'session_id': 'P', 'id': 'K', 'parent_thread_id': 'P'}),
        _user(c + .001),
        _at(c + .002, 'event_msg', {'type': 'turn_aborted', 'reason': 'interrupted'}),
        _user(c + 5),
        _at(c + 30, 'event_msg', {'type': 'turn_aborted', 'reason': 'interrupted'}),
    ])
    data = {p: worker.metrics_only(p) for p in rollout.discover(d)}
    check('aborted turns are counted, a replayed copy left out',
          analyze.turn_aborts(data) == (2, 1), str(analyze.turn_aborts(data)))


def _priced_corpus(d):
    """One session over two days: `big` at standard and Fast, a long `big` prompt, `flat` with a
    web search, and a model no table lists; then an interrupted turn."""
    t = 1_790_000_000
    recs = [
        _at(t, 'session_meta', {'session_id': 'V', 'id': 'V', 'cwd': '/w'}),
        _at(t, 'turn_context', {'model': 'big', 'effort': 'high'}),
        _user(t + 1),
        _at(t + 2, 'token_usage_record', _usage('v1', 10_000, 4_000, 100)),     # tier unrecorded
        _at(t + 3, 'event_msg', {'type': 'thread_settings_applied', 'thread_settings': {
            'model': 'big', 'model_provider_id': 'openai', 'service_tier': 'priority'}}),
        _user(t + 4),
        _at(t + 5, 'token_usage_record', _usage('v2', 20_000, 10_000, 200)),     # Fast
        _at(t + 6, 'event_msg', {'type': 'thread_settings_applied', 'thread_settings': {
            'model': 'big', 'model_provider_id': 'openai'}}),
        _user(t + 7),
        _at(t + 8, 'token_usage_record', _usage('v3', 300_000, 0, 1_000)),       # long context
        _at(t + 86_400, 'turn_context', {'model': 'flat', 'effort': 'low'}),
        _user(t + 86_401),
        _at(t + 86_402, 'response_item', {'type': 'web_search_call', 'status': 'completed'}),
        _at(t + 86_403, 'token_usage_record', _usage('v4', 1_000, 0, 10)),
        _at(t + 86_410, 'turn_context', {'model': 'mystery', 'effort': 'low'}),
        _user(t + 86_411),
        _at(t + 86_412, 'token_usage_record', _usage('v5', 5_000, 0, 50)),
        _at(t + 86_420, 'event_msg', {'type': 'turn_aborted', 'reason': 'interrupted'}),
    ]
    _write(d, 'rollout-v.jsonl', recs)
    # A one-turn thread, as `codex exec` writes it: no settings snapshot at all.
    _write(d, 'rollout-w.jsonl', [
        _at(t + 200, 'session_meta', {'session_id': 'W', 'id': 'W', 'cwd': '/w'}),
        _at(t + 200, 'turn_context', {'model': 'big', 'effort': 'high'}),
        _user(t + 201),
        _at(t + 202, 'token_usage_record', _usage('w1', 200_000, 0, 20)),
    ])
    v1 = (6_000 * 4.0 + 4_000 * 0.4 + 100 * 20.0) / 1e6      # first turn: inferred Fast
    v2 = (10_000 * 4.0 + 10_000 * 0.4 + 200 * 20.0) / 1e6
    v3 = (300_000 * 4.0 + 1_000 * 15.0) / 1e6
    v4 = (1_000 * 1.0 + 10 * 8.0) / 1e6
    w1, w1_fast = (200_000 * 2.0 + 20 * 10.0) / 1e6, (200_000 * 4.0 + 20 * 20.0) / 1e6
    low = v1 + v2 + v3 + v4 + w1 + 0.01
    return low, low - w1 + w1_fast


def test_api_value_headline():
    """The report prices every response, says what it could not price, and shows the total
    as a headline tile worded as a counterfactual."""
    import report as cli
    d = tempfile.mkdtemp()
    want, want_high = _priced_corpus(d)
    data = {p: worker.process(p) for p in rollout.discover(d)}
    charged, counters = ledger.build(data)
    table = pricing.load(_price_file())
    model = analyze.analyze(data, charged, counters, scope={'label': 'test'}, prices=table)
    av = model['api_value']
    check('the API value is the sum of every priced response and the web search fee',
          av['available'] and abs(av['usd'] - want) < 1e-9, f"{av['usd']} want {want}")
    check('what it could not price is counted, with the model and the reason',
          av['priced'] == 5 and av['unpriced'] == 1
          and av['unpriced_models'] == [{'model': 'mystery', 'reason': 'model',
                                         'responses': 1, 'input': 5_000, 'output': 50}], str(av))
    check('tiers, inferred and unrecorded tiers, long prompts, searches and aborts are counted',
          av['tiers'] == {'standard': 3, 'fast': 2} and av['tier_inferred'] == 1
          and av['tier_unrecorded'] == 1 and av['long_context'] == 1
          and av['web_search_calls'] == 1 and av['aborted_turns'] == 1, str(av))
    check('the upper bound prices the unrecorded tier at Fast, and only that',
          abs(av['usd_high'] - want_high) < 1e-9, f"{av['usd_high']} want {want_high}")
    check('the days and the sessions add up to the total',
          abs(sum(x['api_usd'] for x in model['daily']) - av['usd']) < 1e-9
          and abs(sum(x['api_usd'] for x in model['sessions']) - av['usd']) < 1e-9,
          str([x['api_usd'] for x in model['daily']]))

    page = render.render(model)
    tiles = page[page.index('class="tiles"'):page.index('</div>\n\n')]
    check('the API value is a headline tile, worded as a counterfactual',
          f'<div class="k">API value</div><div class="v">{render.usd(av["usd"])}</div>'
          '<div class="n">if billed at API price</div>' in tiles, tiles[:700])
    check('dollars are formatted to fit a tile',
          [render.usd(x) for x in (0.4, 12.345, 1234.5, 123_456, 2_345_678)]
          == ['$0.40', '$12.35', '$1,234', '$123.5K', '$2.35M'])

    lines = cli.api_summary(av)
    check('stdout says the figure is not a bill, and what it left out',
          'not a bill' in lines[0] and any('mystery 1' in x for x in lines)
          and any('aborted turn' in x for x in lines)
          and any('in total if they ran in Fast mode' in x for x in lines)
          and any('1 web search $0.01' in x for x in lines), '\n'.join(lines))

    none = analyze.analyze(data, charged, counters, scope={'label': 'test'},
                           prices=(None, 'price table unreadable (test)'))
    nav = none['api_value']
    check('without a price table the report says why, and draws no tile',
          not nav['available'] and 'unreadable' in nav['reason']
          and '<div class="k">API value</div>' not in render.render(none)
          and all(x['api_usd'] is None for x in none['daily'])
          and none['sessions'][0]['api_usd'] is None
          and cli.api_summary(nav)[0].startswith('API value not available'), str(nav))
    unpriced = analyze.analyze(data, charged, counters, scope={'label': 'test'},
                               prices=pricing.load(_price_file(dict(PRICE_TABLE, models={
                                   'other': PRICE_TABLE['models']['flat']}))))
    check('with nothing priced the report names the models, and draws no tile',
          not unpriced['api_value']['available']
          and 'mystery' in unpriced['api_value']['reason']
          and '<div class="k">API value</div>' not in render.render(unpriced),
          str(unpriced['api_value'].get('reason')))


def test_prices_option():
    """--prices replaces the vendored table; an unreadable one costs the run its API value
    and nothing else."""
    d = tempfile.mkdtemp()
    root = os.path.join(d, 'sessions')
    day = os.path.join(root, '2026', '09', '20')
    os.makedirs(day)
    _priced_corpus(day)
    home = os.path.join(d, 'home')

    def run(*extra):
        out = tempfile.mkdtemp()
        jpath = os.path.join(out, 'm.json')
        err = io.StringIO()
        with _codex_home(home) as cli, contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(['--sessions-root', root, '--no-open', '--no-account', '--procs', '1',
                           '--metrics-only', '--json', jpath, *extra])
        model = json.load(open(jpath, encoding='utf-8')) if rc == 0 else None
        return rc, model, err.getvalue()

    rc, m, _ = run('--prices', _price_file())
    av = (m or {}).get('api_value') or {}
    check('--prices prices the usage with the table it names',
          rc == 0 and av.get('available') and av['prices']['default'] is False
          and av['prices']['as_of'] == '2026-01-02', str(av)[:300])
    rc, m, err = run('--prices', os.path.join(d, 'no-such-table.json'))
    av = (m or {}).get('api_value') or {}
    check('an unreadable --prices table costs the API value, not the run',
          rc == 0 and av.get('available') is False and 'API value not computed' in err
          and m['totals']['responses'] == 6, f'rc={rc} {av} {err[-300:]}')


# --------------------------------------------------------------------------- review findings

def stat_mode(p):
    import stat
    return stat.S_IMODE(os.stat(p).st_mode)


def test_temp_fallback_is_private():
    """The temp-directory fallback is this user's alone, or it is not used.

    On Linux the temp directory is /tmp, which every local user can write: a fixed name
    there is one anyone can make first, then fill with a `tiktoken` for `deps.activate` to
    import, or a symlink for the report, the index or the share token to be written through.
    """
    import report as cli
    posix = hasattr(os, 'getuid')
    tmp = deps.roots()[1]
    check('the temp fallback is named for the user',
          not posix or os.path.basename(tmp) == f'token-counter-{os.getuid()}', tmp)

    d = tempfile.mkdtemp()
    open_dir = os.path.join(d, 'shared')
    os.makedirs(open_dir)
    os.chmod(open_dir, 0o777)
    mine = os.path.join(d, 'mine')
    made = deps.private(mine, create=True)
    link = os.path.join(d, 'link')
    linked = True
    try:
        os.symlink(mine, link)
    except (OSError, NotImplementedError):
        linked = False
    check('a directory others can write is not trusted, nor a symlink to a trusted one',
          not posix or (deps.private(open_dir) is None
                        and (not linked or deps.private(link) is None)), open_dir)
    check('a directory made for the purpose is trusted, and closed to others',
          made == mine and (not posix or stat_mode(mine) & 0o077 == 0),
          f'{made} {oct(stat_mode(mine)) if os.path.isdir(mine) else "-"}')

    # A `tiktoken` planted in a temp root anyone can write is never put on the path.
    home = os.path.join(d, 'home')
    plant = os.path.join(open_dir, 'lib', deps.tag(), 'tiktoken')
    os.makedirs(plant)
    with open(os.path.join(plant, '__init__.py'), 'w') as fh:
        fh.write('raise SystemExit("planted tiktoken imported")\n')
    keep_roots, keep_path = deps.roots, list(sys.path)
    keep_mods = {k: v for k, v in sys.modules.items() if k.split('.')[0] in deps._PROVIDED}
    deps.roots = lambda: [home, open_dir]
    try:
        got = deps.activate()
        on_path = any(p.startswith(open_dir) for p in sys.path)
    finally:
        deps.roots = keep_roots
        sys.path[:] = keep_path
        for k in [k for k in sys.modules if k.split('.')[0] in deps._PROVIDED]:
            del sys.modules[k]
        sys.modules.update(keep_mods)
    check('an install planted in a temp root others can write is not activated',
          not posix or (got is None and not on_path), f'{got} {on_path}')

    # The report falls back only to a directory that is its own.
    blocked = os.path.join(d, 'not-a-dir')
    with open(blocked, 'w') as fh:
        fh.write('x')
    for fallback, why in ((open_dir, 'one someone else could have made'),
                          (os.path.join(d, 'fresh'), 'one that does not exist yet')):
        keep_out = list(cli._OUT_DIR)
        deps.roots = lambda: [os.path.join(blocked, 'home'), fallback]
        cli._OUT_DIR.clear()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                got = cli.out_dir()
        finally:
            deps.roots = keep_roots
            cli._OUT_DIR[:] = keep_out
        want_fallback = fallback != open_dir or not posix
        check(f'an unwritable CODEX_HOME falls back to a private directory ({why})',
              (got == fallback) == want_fallback and deps.private(got) == got,
              f'{got} for {fallback}')
        if got != fallback and os.path.basename(got).startswith('token-counter-'):
            shutil.rmtree(got, ignore_errors=True)       # this run's own mkdtemp


def test_report_write_fallback_is_a_fresh_file():
    """When the report cannot be written where it belongs, it goes to a new file of its own,
    never to a fixed name in the shared temp directory."""
    import report as cli
    root, home, _ = _indexed_corpus([('feed0001', 1)])
    gone = os.path.join(tempfile.mkdtemp(), 'missing', 'dir')
    keep = cli.out_dir
    cli.out_dir = lambda: gone
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(['--sessions-root', root, '--no-open', '--no-account', '--quiet',
                           '--no-cache', '--metrics-only'])
    finally:
        cli.out_dir = keep
    m = re.search(r'writing (.+) instead', err.getvalue())
    alt = m.group(1) if m else ''
    check('an unwritable report path falls back to a fresh file, not a fixed temp name',
          rc == 0 and os.path.isfile(alt)
          and alt != os.path.join(tempfile.gettempdir(), 'report-all.html'),
          f'rc={rc} {err.getvalue()[-300:]!r}')
    if os.path.isfile(alt):
        os.remove(alt)


def test_codex_home_is_resolved_once():
    """Every place that needs Codex's state directory asks the same function."""
    from tokencounter import account, index
    home = tempfile.mkdtemp()
    keep = os.environ.get('CODEX_HOME')
    os.environ['CODEX_HOME'] = home
    try:
        got = (rollout.codex_home(), rollout.sessions_root(), index.default_path(),
               account.codex_home(), deps.roots()[0])
    finally:
        if keep is None:
            os.environ.pop('CODEX_HOME', None)
        else:
            os.environ['CODEX_HOME'] = keep
    want = (home, os.path.join(home, 'sessions'),
            os.path.join(home, 'token-counter', 'index.db'), home,
            os.path.join(home, 'token-counter'))
    check('the corpus, the index, auth.json and the install all follow CODEX_HOME',
          got == want, str(got))
    readers = [n for n in sorted(os.listdir(os.path.join(LIB, 'tokencounter')))
               if n.endswith('.py') and n != 'rollout.py'
               and "environ.get('CODEX_HOME')"
               in pathlib.Path(LIB, 'tokencounter', n).read_text(encoding='utf-8')]
    check('only rollout.codex_home reads CODEX_HOME', readers == [], str(readers))


def test_glob_characters_in_the_sessions_root():
    """A `[` in the root is part of a directory name, not a pattern.  (`*` and `?` are too,
    but Windows allows neither in a name.)"""
    d = os.path.join(tempfile.mkdtemp(), 'br[x]')
    day = os.path.join(d, '2026', '09', '20')
    os.makedirs(day)
    _write(day, 'rollout-2026-09-20T10-00-00-g.jsonl', _explicit_rollout('g', 1))
    check('a sessions root with glob characters in its name is still read',
          len(rollout.discover(d)) == 1, str(rollout.discover(d)))


def test_session_prefix_within_the_range():
    """`--session` matches within the requested dates: a session outside them is neither the
    one asked for nor a rival that makes the prefix ambiguous."""
    d = tempfile.mkdtemp()
    root = os.path.join(d, 'sessions')
    for day, sid in (('2026-09-01', 'abc1'), ('2026-09-20', 'abc2')):
        sub = os.path.join(root, *day.split('-'))
        os.makedirs(sub)
        _write(sub, f'rollout-{day}T10-00-00-{sid}.jsonl', _explicit_rollout(sid, 1, day))
    home = os.path.join(d, 'home')
    rc, model, err = _run_indexed(root, home, '--no-cache', '--since', '2026-09-10',
                                  '--session', 'abc')
    check('a prefix is matched within the range, not across the corpus',
          rc == 0 and model['deep_dive'] == ['abc2'], f'rc={rc} {err[-200:]!r}')
    rc, model, err = _run_indexed(root, home, '--no-cache', '--since', '2026-09-10',
                                  '--session', 'abc1')
    check('a session wholly outside the range is reported, not an empty report',
          rc == 3 and 'in the requested range' in err, f'rc={rc} {err[-200:]!r}')


def test_index_key_only_when_the_index_is_used():
    """The extractor fingerprint loads the vocabulary and encodes: only a run that reads or
    writes the index pays for it."""
    import report as cli
    root, home, _ = _indexed_corpus([('feed0002', 1)])
    calls, keep = [], cli._extractor_version
    cli._extractor_version = lambda vocab=None: calls.append(vocab) or keep(vocab)
    try:
        for extra, want in ((['--no-cache'], 0), (['--metrics-only'], 0), ([], 1)):
            calls.clear()
            rc, _, _ = _run_indexed(root, home, *extra)
            check(f'the index key is computed {want} time(s) with {extra or "the index"}',
                  rc == 0 and len(calls) == want, f'rc={rc} calls={calls}')
    finally:
        cli._extractor_version = keep
    check('the vocabulary is built once for a path, however it is named',
          encoding.load() is encoding.load(encoding.vendor_path()))


def test_malformed_records_are_survived():
    """A record of an unexpected shape costs that record, never the file or the run."""
    t = LAT_T0
    d = tempfile.mkdtemp()
    inf_rl = {'plan_type': 'pro', 'primary': {'used_percent': 5.0, 'window_minutes': 10080,
                                              'resets_at': float('inf')}}
    _write(d, 'rollout-odd.jsonl', [
        _at(t, 'session_meta', {'session_id': 'O', 'id': 'O'}),
        _at(t, 'turn_context', {'model': 'm1', 'effort': 'high'}),
        _user(t + 1),
        _at(t + 2, 'response_item', {'type': 'function_call', 'call_id': ['c2'],
                                     'name': 'exec', 'arguments': '{}'}),
        _at(t + 3, 'response_item', {'type': 'function_call_output', 'call_id': ['c2'],
                                     'output': 'x'}),
        _at(t + 4, 'event_msg', {'type': 'token_count', 'info': ['x']}),
        _at(t + 5, 'token_usage_record', {'response_id': 'rs', 'usage': 'abc'}),
        _at(t + 6, 'turn_context', {'model': {'x': 1}, 'effort': {'y': 2}}),
        _at(t + 7, 'token_usage_record', dict(_usage('rl', 1, 0, 1), response_id=['r'])),
        _at(t + 8, 'event_msg', {'type': 'token_count', 'rate_limits': inf_rl, 'info': {
            'last_token_usage': {'input_tokens': 50, 'output_tokens': 5, 'total_tokens': 55},
            'total_token_usage': {'input_tokens': 50, 'output_tokens': 5,
                                  'total_tokens': 55}}}),
        _said(t + 9),
        _at(t + 10, 'token_usage_record', _usage('r2', 200, 0, 5))])
    try:
        got = {p: worker.process(p) for p in rollout.discover(d)}
        r = next(iter(got.values()))
        ok = (r['counters'].get('malformed_records', 0) >= 3
              and [x['response_id'] for x in r['explicit']][-1] == 'r2'
              and len(r['legacy']) == 1
              and r['counters'].get('rate_limit_no_reset') == 1)
        charged, counters = ledger.build(got)
        model = analyze.analyze(got, charged, counters, scope={'label': 't'})
        ok = ok and model['quality'].get('malformed_records', 0) >= 3
        why = str(r['counters'])
    except Exception as exc:                          # the failure is the finding
        ok, why = False, repr(exc)
    check('malformed records are counted and skipped on the full path, not raised', ok, why)


def test_effort_is_per_turn():
    """A turn whose context records no effort has none, not the previous turn's."""
    t = LAT_T0
    d = tempfile.mkdtemp()
    _write(d, 'rollout-e.jsonl', [
        _at(t, 'session_meta', {'session_id': 'F', 'id': 'F'}),
        _at(t, 'turn_context', {'model': 'm1', 'effort': 'high'}),
        _user(t + 1), _said(t + 2),
        _at(t + 3, 'token_usage_record', _usage('r1', 100, 0, 5)),
        _at(t + 10, 'turn_context', {'model': 'm2', 'effort': None}),
        _user(t + 11), _said(t + 12),
        _at(t + 13, 'token_usage_record', _usage('r2', 100, 0, 5))])
    r = worker.metrics_only(rollout.discover(d)[0])
    got = [(x['model'], x['effort']) for x in r['explicit']]
    check("a turn with no effort is not labelled with the previous turn's",
          got == [('m1', 'high'), ('m2', None)] and r['efforts'] == {'high': 1}, str(got))


def test_explicit_snapshot_keeps_the_frozen_start():
    """An uncharged context snapshot on the explicit stream does not end the response in
    flight, as on the legacy stream."""
    t = LAT_T0
    snap = {'usage': {'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0,
                      'reasoning_output_tokens': 0, 'total_tokens': 500}}
    got = _timed([_at(t, 'session_meta', {'session_id': 'Z', 'id': 'Z'}),
                  _at(t, 'turn_context', {'model': 'm1', 'effort': 'high'}),
                  _user(t + 1), _said(t + 2),
                  _at(t + 3, 'token_usage_record', snap),
                  _out(t + 4, 'cx'),                  # written mid-stream
                  _at(t + 10, 'token_usage_record', _usage('r1', 100, 0, 5))])
    check('an explicit context snapshot mid-response keeps its start', got == [9], str(got))


def test_model_calls_are_output():
    """A model-emitted `*_call` is output: a web search between a response's reasoning and
    its message keeps that reasoning out of the response's reconstructed prompt."""
    check('a web search call is output, for prompt content as for timing',
          classify.item_role({'type': 'web_search_call'}) == 'output'
          and classify.item_role({'type': 'image_generation_call'}) == 'output'
          and classify.item_role({'type': 'function_call_output'}) == 'input')
    t = LAT_T0
    recon = []
    for search in (False, True):
        d = tempfile.mkdtemp()
        recs = [_at(t, 'session_meta', {'session_id': 'W', 'id': 'W'}),
                _at(t, 'turn_context', {'model': 'm1', 'effort': 'high'}),
                _user(t + 1, 'question ' * 30),
                _at(t + 2, 'response_item', {'type': 'reasoning', 'summary': [
                    {'type': 'summary_text', 'text': 'thinking it over ' * 100}]})]
        if search:
            recs.append(_at(t + 3, 'response_item', {'type': 'web_search_call',
                                                     'status': 'completed'}))
        recs += [_said(t + 4), _at(t + 5, 'token_usage_record', _usage('r1', 100, 0, 5))]
        _write(d, 'rollout-w.jsonl', recs)
        recon.append(worker.process(rollout.discover(d)[0])['responses'][0]['recon_input'])
    check("a search does not pull the response's own reasoning into its prompt",
          recon[0] == recon[1] and recon[0] > 0, str(recon))


def test_fork_child_rate_limits_not_replayed():
    """A fork child's replayed rate-limit snapshots, restamped with its creation time, are
    the parent's: they neither redate the parent's windows nor open a window of their own."""
    t = 1_790_000_000
    a_reset, b_open = t + 86400, t + 2 * 86400
    b_reset = b_open + WEEK
    parent = [_rec('session_meta', {'session_id': 'P', 'id': 'P'}, 0),
              _tc(t + 3600, 50.0, a_reset, 1000, 1000),
              _tc(b_open, 5.0, b_reset, 2000, 1000),
              _tc(b_open + 3600, 10.0, b_reset, 3000, 1000)]
    parent[0]['timestamp'] = _at(t, 'x', {})['timestamp']
    c = t + 3 * 86400
    child = [_at(c, 'session_meta', {'session_id': 'P', 'id': 'K', 'parent_thread_id': 'P'})]
    for i, r in enumerate(parent[1:], 1):
        child.append(dict(r, timestamp=_at(c + i / 1000, 'x', {})['timestamp']))
    child.append(_tc(c + 60, 15.0, b_reset, 4000, 1000))
    d = tempfile.mkdtemp()
    _write(d, 'rollout-parent.jsonl', parent)
    _write(d, 'rollout-child.jsonl', child)
    data = {os.path.basename(p): worker.metrics_only(p) for p in rollout.discover(d)}
    kid = data['rollout-child.jsonl']
    check("a fork child keeps only its own rate-limit snapshots",
          kid['counters'].get('rate_limit_replayed') == 3
          and [(w['resets_at'], w['n']) for w in kid['rate_limits']] == [(b_reset, 1)],
          f"{kid['counters']} {[(w['resets_at'], w['n']) for w in kid['rate_limits']]}")
    both = analyze.rate_limit_windows(data, [], now=c + 3600)
    check('with the parent in range, the replay contradicts nothing',
          both['overlapping'] == 0 and both['late_readings'] == 0
          and [w['reset_at'] for w in both['windows']][-1] == b_open,
          f"{both['overlapping']} {both['late_readings']} "
          f"{[w['reset_at'] for w in both['windows']]}")
    alone = analyze.rate_limit_windows({'c': kid}, [], now=c + 3600)
    check('with the parent out of range, no old window appears and the reset stays put',
          [w['reset_at'] for w in alone['windows']] == [b_open],
          str([w['reset_at'] for w in alone['windows']]))


def test_limit_tile_and_axis():
    """The limit tile shows the current window, or says it has reset; the axis ends where
    the data does; a window that is not weekly is not called weekly."""
    model = axis_model()
    rl = model['rate_limits']
    dom = render._domain(model)
    later = dict(model, rate_limits=dict(rl, now=rl['now'] + 90 * 86400))
    check('the shared axis ends with the data, not at the wall clock',
          render._domain(later) == dom and dom[1] < rl['now'], f'{dom} now={rl["now"]}')

    cur = dict(rl['current'] or {}, last_pct=87.0, resets_at=rl['now'] - 3 * 86400,
               expired=True)
    page = render.render(dict(model, rate_limits=dict(rl, current=cur)))
    tile = page.split('Weekly limit used')[1][:300] if 'Weekly limit used' in page else ''
    check('a window that has reset is not shown as the current figure',
          '87%' not in tile and 'no reading since' in tile and '&mdash;' in tile, tile)
    cur = dict(cur, expired=False, resets_at=rl['now'] + 3 * 86400)
    page = render.render(dict(model, rate_limits=dict(rl, current=cur)))
    check('a live window shows its last reading',
          '87%' in page.split('Weekly limit used')[1][:300])

    five = dict(rl, weekly=False, window_minutes=300)
    page = render.render(dict(model, rate_limits=five))
    check('a 5-hour window is called a 5-hour limit, on the tile, the legend and the page',
          '5-hour limit used' in page and '5-hour limit</span>' in page
          and 'Weekly limit used' not in page and '"name":"5-hour"' in page, '')
    check('window lengths are named',
          [render.limit_name({'weekly': False, 'window_minutes': m})
           for m in (1440, 2880, 300, 90)] == ['daily', '2-day', '5-hour', '90-minute']
          and render.limit_name(rl) == 'weekly')


def test_public_page_carries_no_tokenizer_path():
    """The tokenizer note is an exception's first line, which names paths on the machine; the
    page token-share publishes says only what happened."""
    model = axis_model()
    secret = '/home/jane/secret-project/o200k.tiktoken'
    model['scope'] = dict(model.get('scope') or {}, metrics_only=True,
                          tokenizer_note=f'vendored BPE not found at {secret}.')
    check('the public page leaves the vocabulary path out',
          secret not in render.render(model, public=True)
          and 'Not counted:' in render.render(model, public=True))
    check('the local page keeps it', secret in render.render(model))


def test_page_source_compiles_cleanly():
    """The page's script is Python string data: an escape Python does not know is a
    SyntaxWarning on 3.12 and a SyntaxError later."""
    import warnings
    path = os.path.join(LIB, 'tokencounter', 'render.py')
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            compile(src, path, 'exec')
        ok, why = True, ''
    except (SyntaxError, SyntaxWarning, DeprecationWarning) as exc:
        ok, why = False, str(exc)
    check('render.py compiles with warnings as errors', ok, why)
    check("the page's regex keeps its escapes", r'/var\((--[\w-]+)\)/' in render.JS)


def test_windows_pool_cap():
    """Windows' process pool refuses more than 61 workers."""
    import report as cli
    check('the worker count is capped at what the platform allows',
          cli.max_procs('win32') == 61 and cli.max_procs('linux') == 64)


def test_installed_version_order():
    """`verify_install` compares against the newest installed version, by number."""
    import verify_install
    got = sorted(['1.9.0', '1.10.0', '1.8.0'], key=verify_install._version_key)
    check("versions sort by number: '1.10.0' is newer than '1.9.0'",
          got == ['1.8.0', '1.9.0', '1.10.0'], str(got))


def test_fetch_vocab_checks_before_replacing():
    """A cache blob or a download that is not the expected vocabulary never replaces the
    vendored file."""
    import fetch_vocab
    import urllib.request
    d = tempfile.mkdtemp()
    vendor = os.path.join(d, 'vendor', 'o200k_base.tiktoken')
    os.makedirs(os.path.dirname(vendor))
    with open(vendor, 'wb') as fh:
        fh.write(b'the good one\n')
    bad = os.path.join(d, 'planted')
    with open(bad, 'wb') as fh:
        fh.write(b'planted\n')
    keep = (fetch_vocab.VENDOR, fetch_vocab._cache_candidates, urllib.request.urlopen)
    fetch_vocab.VENDOR = vendor
    fetch_vocab._cache_candidates = lambda: iter([bad])
    urllib.request.urlopen = lambda url, timeout=None: io.BytesIO(b'cut short')
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ok = fetch_vocab.fetch()
    finally:
        fetch_vocab.VENDOR, fetch_vocab._cache_candidates, urllib.request.urlopen = keep
    with open(vendor, 'rb') as fh:
        kept = fh.read()
    check('a blob with the wrong checksum is rejected and the vendored file kept',
          ok is False and kept == b'the good one\n'
          and os.listdir(os.path.dirname(vendor)) == ['o200k_base.tiktoken'],
          f'{ok} {kept!r} {os.listdir(os.path.dirname(vendor))}')


def main():
    test_offline_tokenizer()
    test_encoding_cached()
    test_vocabulary_read_from_the_file()
    test_images()
    test_classify()
    test_worker_attribution()
    test_resend_identity()
    test_stable_prefix_baseline()
    test_compaction_segments()
    test_windowed_ledger_scope()
    test_corrupt_record_counted()
    test_extractor_version_tracks_source()
    test_include_archived_counts_archived_sessions()
    test_rebuild_discards_a_held_index()
    test_zero_extractor_key_bypasses_index()
    test_session_prefix_must_name_one_session()
    test_damage_outside_window_reaches_the_report()
    test_object_shaped_corruption_is_visible()
    test_replay_mode_is_labelled()
    test_worker_double_count()
    test_truncated_line()
    test_failure_modes()
    test_local_day_bucketing()
    test_day_span_across_clock_changes()
    test_daily_model_split()
    test_input_counted_with_tiktoken()
    test_environment_degrades()
    test_report_installs_missing_tiktoken()
    test_tiktoken_installs_itself()
    test_install_edge_cases()
    test_account_claims()
    test_response_time_from_records()
    test_replayed_history_is_not_timed()
    test_request_start_edge_cases()
    test_pace_split()
    test_latency_tier_fits_are_independent()
    test_latency_caps_and_counters()
    test_latency_render()
    test_latency_chart()
    test_rate_limit_windows()
    test_cumulative_curve_is_monotonic()
    test_account_and_limits_render()
    test_limit_events()
    test_limit_events_chart()
    test_shared_time_axis()
    test_price_table()
    test_pricing_math()
    test_tier_and_searches_extracted()
    test_sharing_tier_classes()
    test_replayed_search_not_charged_twice()
    test_fetch_prices_parser()
    test_interrupted_turns_counted()
    test_api_value_headline()
    test_prices_option()
    test_render()
    test_temp_fallback_is_private()
    test_report_write_fallback_is_a_fresh_file()
    test_codex_home_is_resolved_once()
    test_glob_characters_in_the_sessions_root()
    test_session_prefix_within_the_range()
    test_index_key_only_when_the_index_is_used()
    test_malformed_records_are_survived()
    test_effort_is_per_turn()
    test_explicit_snapshot_keeps_the_frozen_start()
    test_model_calls_are_output()
    test_fork_child_rate_limits_not_replayed()
    test_limit_tile_and_axis()
    test_public_page_carries_no_tokenizer_path()
    test_page_source_compiles_cleanly()
    test_windows_pool_cap()
    test_installed_version_order()
    test_fetch_vocab_checks_before_replacing()
    bad = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f'\n{len(RESULTS) - bad}/{len(RESULTS)} passed')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
