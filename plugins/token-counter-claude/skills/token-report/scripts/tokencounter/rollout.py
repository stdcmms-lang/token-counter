"""Transcript discovery and complete-record iteration.

Only main and subagent transcripts are usage sources. Every complete line is parsed;
damage has a counter, and a trailing fragment is withheld until its newline arrives.
"""
import hashlib
import json
import os
from pathlib import Path

from .models import bump
from .paths import is_within, resolve_paths


def codex_home() -> str:
    """Compatibility for the shared index: Claude's configuration directory only."""
    return str(resolve_paths()['config_dir'])


def _transcript_layout(parts):
    return (len(parts) == 2 or (len(parts) == 4 and parts[2] == 'subagents'))


def discover(root) -> tuple:
    root = Path(root).expanduser().resolve()
    out, counters = [], {}
    for directory, dirs, files in os.walk(str(root), followlinks=False):
        keep = []
        for name in sorted(dirs):
            path = Path(directory) / name
            if not is_within(path, root):
                bump(counters, 'outside_root_links')
            elif path.is_symlink():
                bump(counters, 'directory_links_skipped')
            elif name != 'tool-results':
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix != '.jsonl':
                continue
            if not is_within(path, root):
                bump(counters, 'outside_root_links')
                continue
            parts = path.relative_to(root).parts
            if not _transcript_layout(parts) or not path.stem:
                bump(counters, 'unexpected_jsonl_paths')
                continue
            out.append(path)
    return sorted(out), counters


def stat_key(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def prefix_hash(path, nbytes=65536):
    h = hashlib.blake2b(digest_size=16)
    with open(path, 'rb') as fh:
        h.update(fh.read(nbytes))
    return h.hexdigest()


def _complete_line(raw):
    return raw.endswith(b'\n')


def read_records(path, counters):
    """Yield ``(physical_line, object)`` without retaining or printing raw bytes."""
    with open(path, 'rb') as fh:
        for line, raw in enumerate(fh, 1):
            if not _complete_line(raw):
                bump(counters, 'trailing_partial_lines')
                return
            bump(counters, 'complete_lines')
            bump(counters, 'records_seen')
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeError):
                bump(counters, 'unparseable_records')
                continue
            if not isinstance(obj, dict):
                bump(counters, 'non_object_records')
                continue
            yield line, obj
