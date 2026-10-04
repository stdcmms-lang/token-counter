"""Confirm the installed plugin is byte-identical to this repository.

`codex plugin add` copies the plugin into `~/.codex/plugins/cache/`. Editing the repository
afterwards leaves the installed copy stale, and a claim that the installed plugin was
"verified" then refers to different code. That happened once; this makes it checkable.

    python scripts/verify_install.py
"""
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, 'plugins', 'token-counter')
SKIP = {'__pycache__'}


def market():
    """The marketplace id, read from the manifest so these hints cannot go stale."""
    manifest = os.path.join(REPO, '.agents', 'plugins', 'marketplace.json')
    try:
        with open(manifest, encoding='utf-8') as fh:
            name = json.load(fh).get('name')
    except (OSError, ValueError):
        return '<marketplace>'
    return name if isinstance(name, str) and name else '<marketplace>'


MARKET = market()


def installed_root():
    """The installed copy belonging to this repository's marketplace.

    Scanning every marketplace and taking the last hit, as an earlier version did, verifies
    an arbitrary copy: renaming the marketplace orphans the old cache directory, and
    `local-dev` sorts after `jack-beanstalk-2022`, so the abandoned copy became the one
    checked -- reporting 9 differing files against a fresh install. The manifest names the
    marketplace this repository publishes, so that is the copy compared; any other is
    reported and ignored.
    """
    home = os.environ.get('CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex')
    base = os.path.join(home, 'plugins', 'cache')
    found = {}
    for market in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        d = os.path.join(base, market, 'token-counter')
        if not os.path.isdir(d):
            continue
        # By version number, not by string: '1.10.0' is newer than '1.9.0'.
        vers = sorted((v for v in os.listdir(d) if os.path.isdir(os.path.join(d, v))),
                      key=_version_key)
        if vers:
            found[market] = os.path.join(d, vers[-1])
    orphans = sorted(m for m in found if m != MARKET)
    if orphans:
        print(f'note: not checked, cached under another marketplace: {", ".join(orphans)}',
              file=sys.stderr)
    return found.get(MARKET)


def _version_key(v):
    """A sort key under which numeric parts compare as numbers, so '1.10.0' > '1.9.0', and
    any other part sorts after them, as text."""
    return tuple((0, int(p), '') if p.isdigit() else (1, 0, p) for p in v.split('.'))


def digests(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for fn in filenames:
            if fn.endswith('.pyc'):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace('\\', '/')
            with open(p, 'rb') as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def main():
    root = installed_root()
    if root is None:
        print('token-counter is not installed. Run:\n'
              f'  codex plugin marketplace add {REPO}\n'
              f'  codex plugin add token-counter@{MARKET}', file=sys.stderr)
        return 2
    print(f'repo     : {SRC}')
    print(f'installed: {root}')
    a, b = digests(SRC), digests(root)
    missing = sorted(set(a) - set(b))
    extra = sorted(set(b) - set(a))
    differ = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    for k in missing:
        print(f'  MISSING from install : {k}')
    for k in extra:
        print(f'  EXTRA in install     : {k}')
    for k in differ:
        print(f'  STALE                : {k}')
    # `extra` counts too: a file the repository does not have is still code that ships, and
    # an earlier version of this script printed extras and then exited 0.
    if missing or differ or extra:
        print(f'\n{len(missing) + len(differ) + len(extra)} file(s) differ '
              f'({len(missing)} missing, {len(differ)} stale, {len(extra)} extra). '
              'Reinstall with:\n'
              f'  codex plugin remove token-counter@{MARKET}\n'
              f'  codex plugin add token-counter@{MARKET}', file=sys.stderr)
        return 1
    print(f'\n{len(a)} files identical. The installed plugin is this code.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
