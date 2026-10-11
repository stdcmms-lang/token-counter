#!/usr/bin/env python3
"""Keep the modules both plugins share byte-identical.

The Codex plugin's copy is canonical; the Claude plugin's copy is written from it. Claude
Code copies an installed plugin's directory and nothing outside it, so a shared module has
to exist physically in each plugin: no symlink, no import across the two.

    python scripts/sync_shared.py          # copy the canonical files into the Claude plugin
    python scripts/sync_shared.py --check  # exit 1 and name any copy that differs or is missing
"""
import argparse
import os
import sys

SHARED = ('render.py', 'latency.py', 'index.py', 'images.py')
CANONICAL = os.path.join('plugins', 'token-counter', 'skills', 'token-report', 'scripts',
                         'tokencounter')
COPY = os.path.join('plugins', 'token-counter-claude', 'skills', 'token-report', 'scripts',
                    'tokencounter')


def shared_pairs(repo) -> list:
    """``[(canonical_path, copy_path), ...]`` for every shared module."""
    return [(os.path.join(repo, CANONICAL, name), os.path.join(repo, COPY, name))
            for name in SHARED]


def _read(path):
    if os.path.islink(path):
        raise OSError('shared module must be a real file, not a symlink: ' + path)
    with open(path, 'rb') as fh:
        return fh.read()


def synchronize(repo, *, check=False) -> int:
    """Copy, or with `check` only compare.  Returns the number of copies that differed or
    were missing; prints their relative paths, never their contents."""
    differing = 0
    for source, dest in shared_pairs(repo):
        data = _read(source)
        try:
            same = os.path.exists(dest) and _read(dest) == data
        except OSError:
            same = False
        if same:
            continue
        differing += 1
        print(os.path.relpath(dest, repo))
        if not check:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as fh:
                fh.write(data)
    return differing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--check', action='store_true',
                    help='compare only; exit 1 if any copy differs or is missing')
    a = ap.parse_args(argv)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    n = synchronize(repo, check=a.check)
    if a.check:
        print(f'{n} shared module(s) out of sync' if n else 'shared modules identical')
        return 1 if n else 0
    print(f'{n} shared module(s) written')
    return 0


if __name__ == '__main__':
    sys.exit(main())
