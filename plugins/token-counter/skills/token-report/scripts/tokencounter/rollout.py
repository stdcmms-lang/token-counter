"""Rollout discovery and record iteration.

Rollouts live at ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` and are the only input.
Nothing is intercepted at runtime. See ARCHITECTURE.md sections 2 and 3.1.
"""
import glob
import json
import os
import re

DATE_RE = re.compile(r'(\d{4})[/\\-](\d{2})[/\\-](\d{2})[/\\]rollout-')

# Byte hints, used only to *classify* a line that failed to parse -- did the corrupt record
# look like it carried usage?  They are no longer used to skip lines.
#
# A candidate prefilter on the ledger-only path was removed after measurement: it saved 1.0s
# across a 7.8 GB corpus (3.6s against 4.6s, and it was *slower* than parsing outright once
# validation was added), and it could not see a record whose own `type` string was damaged.
# An object-shaped corrupt ancestor was therefore invisible to the ledger. One second is not
# worth a class of silent loss.
USAGE_HINTS = (b'token_usage_record', b'token_count')
META_HINT = b'session_meta'


def codex_home():
    """Codex's state directory.  ``CODEX_HOME`` moves it, and users do move it.

    The one place it is resolved: the corpus, ``auth.json``, the index and tiktoken's
    private install all live under it, and a copy of this rule that drifted would split them
    across two directories.
    """
    return os.environ.get('CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex')


def sessions_root(override=None):
    if override:
        return override
    return os.path.join(codex_home(), 'sessions')


def file_date(path):
    """Calendar date encoded in the rollout's directory layout, or None."""
    m = DATE_RE.search(path.replace('\\', '/') + '|')
    if m:
        return '-'.join(m.groups())
    m = re.search(r'rollout-(\d{4})-(\d{2})-(\d{2})T', os.path.basename(path))
    return '-'.join(m.groups()) if m else None


def discover(root=None, since=None, until=None):
    """Rollout paths, oldest first.  `since`/`until` are inclusive ``YYYY-MM-DD``."""
    # Escaped: a `[`, `*` or `?` in the root is part of a directory name, not a pattern.
    pat = os.path.join(glob.escape(sessions_root(root)), '**', 'rollout-*.jsonl')
    out = []
    for p in glob.iglob(pat, recursive=True):
        d = file_date(p)
        if since and (d is None or d < since):
            continue
        if until and (d is None or d > until):
            continue
        out.append(p)
    out.sort()
    return out


def stat_key(path):
    """Cheap identity for cache validation: (size, mtime_ns)."""
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def prefix_hash(path, nbytes=65536):
    """Hash of the file's leading bytes, to detect rewrite rather than append."""
    import hashlib
    h = hashlib.blake2b(digest_size=16)
    with open(path, 'rb') as fh:
        h.update(fh.read(nbytes))
    return h.hexdigest()


def iter_lines(path):
    """Yield complete raw JSONL lines.

    A rollout may be read while Codex is mid-write, so a trailing fragment without a
    newline is withheld: only whole records are ever surfaced (ARCHITECTURE.md 3.2).
    """
    with open(path, 'rb') as fh:
        pending = None
        for raw in fh:
            if pending is not None:
                yield pending
            pending = raw if raw.endswith(b'\n') else None
            if pending is None:
                return
        if pending is not None and pending.endswith(b'\n'):
            yield pending


def iter_records(path, hints=None, damage=None):
    """Yield ``(outer_type, payload, timestamp, raw_len)`` for parseable records.

    `hints` is an optional tuple of byte substrings used as a *candidate* filter before
    ``json.loads``.  It is never authoritative: the caller must still dispatch on the
    parsed outer type.

    `damage` is an optional counter.  A complete line that fails to parse is a **lost
    record**, not a non-event: if it carried usage, that usage silently vanishes from the
    ledger.  Swallowing these without counting them was a real defect -- a corrupt usage
    line produced no error and no counter, and the response simply disappeared.
    """
    for raw in iter_lines(path):
        if hints is not None and not any(h in raw for h in hints):
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            if damage is not None:
                damage['unparseable_records'] += 1
                if any(h in raw for h in USAGE_HINTS):
                    damage['unparseable_usage_records'] += 1
            continue
        if not isinstance(obj, dict):
            if damage is not None:
                damage['non_object_records'] += 1
            continue
        payload = obj.get('payload')
        if not isinstance(payload, dict):
            continue
        yield obj.get('type'), payload, obj.get('timestamp'), len(raw)
