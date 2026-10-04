"""One-time vendoring of the o200k_base BPE vocabulary.

Run once at packaging time. The plugin itself never downloads anything.

Sources, in order of preference:
  1. an existing tiktoken cache blob (no network),
  2. the published URL (network, packaging-time only).

The file is written verbatim in tiktoken's own ``.tiktoken`` format (base64 token,
space, rank) and then verified: it must load, and it must tokenize a corpus sample
identically to stock ``o200k_base``.

Usage:
    python scripts/fetch_vocab.py            # vendor + verify
    python scripts/fetch_vocab.py --verify   # verify only
"""
import argparse
import hashlib
import os
import shutil
import sys
import tempfile

VOCAB_URL = 'https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken'
EXPECTED_SHA256 = '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d'

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VENDOR = os.path.join(REPO, 'plugins', 'token-counter', 'assets', 'vendor',
                      'o200k_base.tiktoken')
PLUGIN_LIB = os.path.join(REPO, 'plugins', 'token-counter', 'skills', 'token-report',
                          'scripts')

SAMPLE = (
    "The quick brown fox\n\tjumps over 1234 lazy dogs.\r\n"
    "def f(x): return x ** 2  # éèê 中文 \U0001f600\n"
    "  trailing   spaces   \n<|endoftext|>-ish but not special\n"
    "https://example.com/path?q=1&r=2 'quoted' \"double\" `tick`\n"
)


def _cache_candidates():
    """Directories tiktoken may already have cached the blob in."""
    env = os.environ.get('TIKTOKEN_CACHE_DIR') or os.environ.get('DATA_GYM_CACHE_DIR')
    dirs = [env] if env else []
    dirs.append(os.path.join(tempfile.gettempdir(), 'data-gym-cache'))
    key = hashlib.sha1(VOCAB_URL.encode()).hexdigest()
    for d in dirs:
        if d:
            p = os.path.join(d, key)
            if os.path.isfile(p):
                yield p


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _install(stage, origin):
    """Move `stage` over the vendored file if it is the expected blob; delete it if not.

    Checked before it replaces anything: a cache directory under a shared temp directory can
    hold whatever another user put there, and a download can be cut short.
    """
    digest = _sha256(stage)
    if digest != EXPECTED_SHA256:
        os.remove(stage)
        print(f'  rejected {origin}: sha256 {digest}, expected {EXPECTED_SHA256}',
              file=sys.stderr)
        return False
    os.chmod(stage, 0o644)                  # mkstemp's 0600 is for the staging, not the asset
    os.replace(stage, VENDOR)
    return True


def fetch():
    """Vendor the blob from a cache or the URL.  The existing file is replaced only by one
    whose sha256 matches; returns whether it was."""
    d = os.path.dirname(VENDOR)
    os.makedirs(d, exist_ok=True)

    def stage():
        fd, path = tempfile.mkstemp(prefix='.o200k-', suffix='.tmp', dir=d)
        return os.fdopen(fd, 'wb'), path

    for src in _cache_candidates():
        out, path = stage()
        try:
            with out, open(src, 'rb') as fh:
                shutil.copyfileobj(fh, out)
        except OSError:
            os.remove(path)
            continue
        if _install(path, src):
            print(f'vendored from cache: {src}')
            return True
    import urllib.request
    print(f'downloading {VOCAB_URL} ...')
    out, path = stage()
    try:
        with out, urllib.request.urlopen(VOCAB_URL, timeout=60) as r:
            shutil.copyfileobj(r, out)
    except BaseException:
        os.remove(path)
        raise
    if not _install(path, VOCAB_URL):
        return False
    print('downloaded')
    return True


def verify():
    if not os.path.isfile(VENDOR):
        print(f'MISSING: {VENDOR}', file=sys.stderr)
        return False
    ok = True
    digest = _sha256(VENDOR)
    size = os.path.getsize(VENDOR)
    print(f'vendor : {VENDOR}')
    print(f'size   : {size:,} bytes')
    print(f'sha256 : {digest}')
    if EXPECTED_SHA256 and digest != EXPECTED_SHA256:
        print(f'  WARNING: expected {EXPECTED_SHA256}', file=sys.stderr)
        ok = False

    sys.path.insert(0, PLUGIN_LIB)
    from tokencounter import encoding as tcenc
    enc = tcenc.load(VENDOR)
    print(f'ranks  : {enc.n_vocab:,} vocab')

    ours = enc.encode_ordinary(SAMPLE)
    try:
        import tiktoken
        ref = tiktoken.get_encoding('o200k_base').encode_ordinary(SAMPLE)
    except Exception as exc:                       # offline: no reference available
        print(f'reference unavailable ({exc.__class__.__name__}); skipped comparison')
        return ok
    if ours == ref:
        print(f'parity : OK ({len(ours)} tokens identical to stock o200k_base)')
    else:
        print(f'parity : MISMATCH ours={len(ours)} ref={len(ref)}', file=sys.stderr)
        ok = False
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--verify', action='store_true', help='verify only, do not fetch')
    a = ap.parse_args()
    if not a.verify and not fetch():
        print(f'nothing vendored; {VENDOR} is unchanged', file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if verify() else 1)


if __name__ == '__main__':
    main()
