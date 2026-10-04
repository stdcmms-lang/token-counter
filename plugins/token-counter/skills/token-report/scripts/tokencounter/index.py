"""SQLite index of per-file extraction results.

Keyed on ``(session_id, thread_id)`` with `path` as a mutable attribute, because archiving
**moves** rollout files and path is therefore not a stable identity (ARCHITECTURE.md 3.2).
Entries whose file has vanished are retained and marked archived rather than dropped.

Reuse is guarded, never assumed: a cached payload is used only when size, mtime and the
leading-bytes hash all still match, and when it was produced by this version of the
extractor.  Any mismatch re-parses the file whole.
"""
import json
import os
import sqlite3
import zlib

from . import rollout

SCHEMA_VERSION = 3

DDL = """
CREATE TABLE IF NOT EXISTS files (
    session_id  TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    path        TEXT NOT NULL,
    size        INTEGER,
    mtime_ns    INTEGER,
    prefix_hash TEXT,
    extractor   INTEGER,
    archived    INTEGER NOT NULL DEFAULT 0,
    seen_at     REAL,
    payload     BLOB,
    PRIMARY KEY (session_id, thread_id)
);
CREATE INDEX IF NOT EXISTS files_path ON files(path);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def default_path():
    return os.path.join(rollout.codex_home(), 'token-counter', 'index.db')


def try_open(path=None):
    """``(Index, None)`` or ``(None, reason)``.

    The index is an optimisation, never a source of truth: a corrupt file, a read-only
    directory, a database locked by a second run, or a filesystem that cannot do SQLite
    locking at all (some network mounts) must cost the run its speed, not its output.
    """
    try:
        return Index(path), None
    except (sqlite3.Error, OSError, ValueError) as exc:
        return None, f'{exc.__class__.__name__}: {exc}'


class Index:
    # A second report over the same corpus is a normal thing to do; wait for it rather than
    # failing, and give up long before a human would.
    TIMEOUT_S = 20

    def __init__(self, path=None):
        self.path = path or default_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=self.TIMEOUT_S)
        self.db.execute(f'PRAGMA busy_timeout={int(self.TIMEOUT_S * 1000)}')
        self.db.executescript(DDL)
        cur = self.db.execute("SELECT v FROM meta WHERE k='schema'")
        row = cur.fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta(k,v) VALUES('schema',?)", (str(SCHEMA_VERSION),))
            self.db.commit()
        elif row[0] != str(SCHEMA_VERSION):
            self.db.execute('DELETE FROM files')
            self.db.execute("UPDATE meta SET v=? WHERE k='schema'", (str(SCHEMA_VERSION),))
            self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- lookup ---------------------------------------------------------------

    def fresh(self, path, size, mtime_ns, extractor):
        """Cached result for `path`, or ``None`` if absent or stale."""
        cur = self.db.execute(
            'SELECT prefix_hash, payload FROM files '
            'WHERE path=? AND size=? AND mtime_ns=? AND extractor=?',
            (path, size, mtime_ns, extractor))
        row = cur.fetchone()
        if not row or not row[1]:
            return None
        from . import rollout
        try:
            if rollout.prefix_hash(path) != row[0]:
                return None                     # rewritten, not appended: re-parse whole
        except OSError:
            return None
        try:
            return json.loads(zlib.decompress(row[1]))
        except Exception:
            return None

    # -- write ----------------------------------------------------------------

    def put(self, result, extractor, seen_at):
        sid = result.get('session_id') or result['path']
        tid = result.get('thread_id') or result['path']
        blob = zlib.compress(json.dumps(result, separators=(',', ':')).encode(), 6)
        self.db.execute(
            'INSERT INTO files(session_id,thread_id,path,size,mtime_ns,prefix_hash,'
            'extractor,archived,seen_at,payload) VALUES(?,?,?,?,?,?,?,0,?,?) '
            'ON CONFLICT(session_id,thread_id) DO UPDATE SET '
            'path=excluded.path, size=excluded.size, mtime_ns=excluded.mtime_ns, '
            'prefix_hash=excluded.prefix_hash, extractor=excluded.extractor, '
            'archived=0, seen_at=excluded.seen_at, payload=excluded.payload',
            (sid, tid, result['path'], result.get('size'), result.get('mtime_ns'),
             result.get('prefix_hash'), extractor, seen_at, blob))

    def touch(self, path, seen_at):
        self.db.execute('UPDATE files SET seen_at=?, archived=0 WHERE path=?',
                        (seen_at, path))

    def clear(self):
        """Drop every cached result, keeping the schema.

        ``--rebuild`` does this instead of deleting the file: a second run holding the
        database open makes the file undeletable on Windows, and a rebuild that then quietly
        reuses the cache is worse than no rebuild.
        """
        self.db.execute('DELETE FROM files')
        self.db.commit()

    def reconcile_archived(self):
        """Re-check every entry against the filesystem.

        Archiving is decided by whether the recorded file still exists, **not** by whether
        this run happened to scan it -- a windowed run (``--since``) legitimately skips most
        of the corpus, and an earlier version wrongly archived all of it.  A file moved
        within the tree updates its own row, because the key is (session_id, thread_id) and
        path is only an attribute.
        """
        gone, back = [], []
        for rowid, path, archived in self.db.execute(
                'SELECT rowid, path, archived FROM files').fetchall():
            exists = os.path.isfile(path)
            if exists and archived:
                back.append(rowid)
            elif not exists and not archived:
                gone.append(rowid)
        if gone:
            self.db.executemany('UPDATE files SET archived=1 WHERE rowid=?',
                                [(r,) for r in gone])
        if back:
            self.db.executemany('UPDATE files SET archived=0 WHERE rowid=?',
                                [(r,) for r in back])
        return len(gone)

    def archived(self):
        """Cached results for files no longer on disk.  Retained, never silently dropped."""
        cur = self.db.execute(
            'SELECT payload FROM files WHERE archived=1 AND payload IS NOT NULL')
        for (blob,) in cur:
            try:
                yield json.loads(zlib.decompress(blob))
            except Exception:
                continue

    def commit(self):
        self.db.commit()

    def stats(self):
        cur = self.db.execute(
            'SELECT COUNT(*), SUM(archived), SUM(LENGTH(payload)) FROM files')
        n, arch, size = cur.fetchone()
        return {'entries': n or 0, 'archived': arch or 0, 'bytes': size or 0}
