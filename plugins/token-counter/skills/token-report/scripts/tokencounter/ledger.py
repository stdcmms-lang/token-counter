"""The canonical usage ledger.

**This is the single most important correctness constraint in the system.**
See ARCHITECTURE.md sections 2.2 to 2.5.

Two usage streams coexist and overlap.  Summing them double-counts by ~30%.  Exactly one
stream is charged per file: the explicit ``token_usage_record`` when present (it carries
``response_id``), otherwise the legacy ``event_msg``/``token_count`` snapshots.

Response identity discriminators, strongest first:

  1. ``response_id``   -- explicit stream only; exact identity.
  2. full ``info`` state -- a legacy record is a repeat only if its ``last_token_usage`` *and*
                          ``total_token_usage`` both match the immediate predecessor.  A
                          repeated usage value with an advancing cumulative counter is a
                          genuine new response.
  3. ancestor replay   -- a fork replays ancestor history as a LEADING RUN of the child file
                          matching a contiguous run in a file on its **declared ancestor
                          chain** (`parent_thread_id`, followed transitively).  A run of
                          length 1 cannot be distinguished from coincidence and is reported
                          as `ambiguous` (charged, and disclosed) rather than silently
                          dropped.

Discriminator 3 used to search every earlier file in the session, which deletes real
responses when two unrelated sibling threads happen to share a run of states -- entirely
possible for sub-agents launched with identical prompts.  Restricting it to the declared
ancestor chain costs nothing measurable: across the development corpus all **39** files with
a detected replay declare a `parent_thread_id`, and in all 39 the matching file *is* that
declared ancestor.

Both sides of a replay match are reduced by the *same* ``normalize`` and compared on
complete state (usage + cumulative).  Asymmetric normalization is a silent correctness bug:
it halts matching at the first compaction and charges inherited history twice.
"""
import collections

MIN_REPLAY_RUN = 2          # a 1-record match cannot be distinguished from coincidence

USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens',
                'reasoning_output_tokens', 'total_tokens')


def usage_key(u):
    return tuple(u.get(f) for f in USAGE_FIELDS)


def info_state(last, total):
    """Full snapshot state -- per-response usage AND cumulative counters."""
    return (usage_key(last), usage_key(total))


def is_context_snapshot(u):
    """Zero in, zero out, positive total: context state, not a response."""
    return ((u.get('input_tokens') or 0) == 0
            and (u.get('output_tokens') or 0) == 0
            and (u.get('total_tokens') or 0) > 0)


def longest_leading_replay(child_keys, ancestor_keys):
    """Longest prefix of `child_keys` appearing as a contiguous run in `ancestor_keys`."""
    best = 0
    n, m = len(child_keys), len(ancestor_keys)
    for start in range(m):
        k = 0
        while k < n and start + k < m and ancestor_keys[start + k] == child_keys[k]:
            k += 1
        if k > best:
            best = k
            if best == n:
                break
    return best


def normalize(legacy):
    """Canonical comparison sequence for a legacy stream.

    Collapses immediate full-state repeats and removes context snapshots.  Returns
    ``(entries, n_repeat, repeat_input, n_context)`` where each entry is
    ``(state, record)``.
    """
    out, prev = [], None
    n_repeat = repeat_input = n_ctx = 0
    for rec in legacy:
        last, total = rec['last'], rec['total']
        state = info_state(last, total)
        if prev is not None and state == prev:
            n_repeat += 1
            repeat_input += last.get('input_tokens') or 0
            continue
        prev = state
        if is_context_snapshot(last):
            n_ctx += 1
            continue
        out.append((state, rec))
    return out, n_repeat, repeat_input, n_ctx


def _charged_record(rec, stream):
    r = dict(rec.get('last') or rec.get('usage') or {})
    return {
        'usage': r,
        'ts': rec.get('ts'),
        'model': rec.get('model'),
        'effort': rec.get('effort'),
        # The processing tier requested (None: none recorded in the file; `tier_inferred`:
        # taken from the file's first snapshot, for its first turn) and the web searches made
        # producing it, all for pricing (tokencounter.pricing).
        'tier': rec.get('tier'),
        'tier_inferred': bool(rec.get('tier_inferred')),
        'web_search': rec.get('web_search') or 0,
        'response_id': rec.get('response_id'),
        'stream': stream,
        'index': rec.get('i'),
        # When the request went out, and which turn it belongs to (tokencounter.latency).
        'req_ts': rec.get('req_ts'),
        'turn': rec.get('turn'),
    }


def _ancestor_paths(files, entries):
    """path -> ordered list of ancestor paths, from the declared `parent_thread_id` chain.

    Following the chain transitively matters: a grandchild replays a run that the child
    itself inherited.
    """
    by_thread = {}
    parent_of = {}
    for _, path in entries:
        fr = files[path]
        tid = fr.get('thread_id') or path
        by_thread.setdefault(tid, path)
        parent_of[tid] = fr.get('parent_thread_id')
    out = {}
    for _, path in entries:
        fr = files[path]
        tid = fr.get('thread_id') or path
        chain, seen, cur = [], {tid}, parent_of.get(tid)
        while cur and cur not in seen:
            seen.add(cur)
            p = by_thread.get(cur)
            if p is not None:
                chain.append(p)
            cur = parent_of.get(cur)
        out[path] = chain
    return out


def build(files, scope=None, legacy_ancestry=False, exclude_replay=True):
    """Charge the canonical ledger across a corpus.

    `files` maps path -> a dict carrying at least ``session_id``, ``started_at``,
    ``thread_id``, ``parent_thread_id``, ``explicit`` and ``legacy`` (as produced by
    :mod:`tokencounter.worker`).

    `scope` optionally restricts what is *reported* -- rows and counters -- while still
    charging over every file given.  A windowed report must do this: filtering files before
    the ledger runs hides the ancestors a fork child replays, and the child's inherited
    history is then charged as new.  On the development corpus that overcharged one session
    by 246 responses and 30.5M input tokens.

    `legacy_ancestry` lets an explicit-stream file contribute its *legacy sidecar* as
    ancestor history.  It defaults **off**: it changed nothing measurable on the corpus, and
    a parent whose sidecar holds records its explicit stream does not can delete genuine
    child responses.

    Returns ``(charged, counters)`` where `charged` maps path -> list of charged usage rows
    in file order, and `counters` is a :class:`collections.Counter` of data-quality figures.
    """
    counters = collections.Counter()
    charged = {}

    by_session = collections.defaultdict(list)
    for path, fr in files.items():
        by_session[fr.get('session_id') or path].append((fr.get('started_at') or '', path))
    for entries in by_session.values():
        entries.sort()

    for entries in by_session.values():
        history = {}            # path -> normalized sequence, INDEPENDENT of charging
        ancestors = _ancestor_paths(files, entries)
        seen_rid = set()        # response ids already charged ANYWHERE in this session
        for _, path in entries:
            fr = files[path]
            in_scope = scope is None or path in scope
            explicit, legacy = fr.get('explicit') or [], fr.get('legacy') or []

            if explicit:
                counters['files_explicit'] += in_scope
                rows = []
                for rec in explicit:
                    if is_context_snapshot(rec.get('usage') or {}):
                        counters['context_snapshots'] += in_scope
                        continue
                    # Discriminator 1 at session scope.  The extractor deduplicates by
                    # response_id within a file; a fork child replaying its parent's history
                    # on the explicit stream would repeat those ids across files, and the
                    # replay rule below only runs on the legacy branch.  response_id is exact
                    # identity, so this can never over-drop.  Zero occurrences on the
                    # development corpus (45 multi-file explicit sessions) -- this closes a
                    # structural gap, not an observed one.
                    rid = rec.get('response_id')
                    if rid is not None:
                        if rid in seen_rid:
                            counters['cross_file_response_id'] += in_scope
                            continue
                        seen_rid.add(rid)
                    rows.append(_charged_record(rec, 'explicit'))
                if legacy and legacy_ancestry:
                    history[path] = normalize(legacy)[0]
                else:
                    history[path] = [(info_state(r['usage'], {}), r) for r in rows]
            elif not legacy:
                counters['files_no_usage'] += in_scope
                rows = []
                history[path] = []
            else:
                counters['files_legacy'] += in_scope
                norm, n_rep, rep_in, n_ctx = normalize(legacy)
                counters['legacy_repeat'] += n_rep * in_scope
                counters['legacy_repeat_input'] += rep_in * in_scope
                counters['context_snapshots'] += n_ctx * in_scope

                keys = [k for k, _ in norm]
                best = 0
                # Discriminator 3 searches the DECLARED ancestor chain only.  Searching every
                # earlier file in the session deletes real responses whenever two unrelated
                # sibling threads share a run of states.
                for anc_path in ancestors.get(path) or ():
                    anc = history.get(anc_path)
                    if not anc:
                        continue
                    best = max(best, longest_leading_replay(keys, [k for k, _ in anc]))
                    if best == len(keys):
                        break
                if best >= MIN_REPLAY_RUN:
                    counters['files_with_inherited'] += in_scope
                    # Count matched records under a name that reflects what actually
                    # happened.  Incrementing `inherited` while charging them made the report
                    # say "records dropped: 3" about three records it had just charged.
                    key = 'inherited' if exclude_replay else 'inherited_charged'
                    for _, rec in norm[:best]:
                        counters[key] += in_scope
                        counters[key + '_input'] += (
                            (rec['last'].get('input_tokens') or 0) * in_scope)
                    # Discriminator 3 is a *heuristic* match, not proven identity: the legacy
                    # stream carries no response id, so a replayed run is inferred from
                    # matching state against a declared ancestor.  A false positive deletes
                    # real responses; a false negative double-charges.  The exclusion is the
                    # better estimate, but it is reported as an inference (see §8 and the
                    # report's data-quality panel), and `exclude_replay=False` exposes the
                    # other bound rather than hiding it behind a rebuild.
                    keep = norm[best:] if exclude_replay else norm
                    matched = 0 if exclude_replay else best
                else:
                    if best == 1:
                        counters['ambiguous'] += in_scope
                        counters['ambiguous_input'] += (
                            (norm[0][1]['last'].get('input_tokens') or 0) * in_scope)
                    keep = norm
                    matched = best
                rows = [_charged_record(rec, 'legacy') for _, rec in keep]
                # A matched record that is charged anyway -- ambiguous, or with the exclusion
                # off -- still carries the child's creation time, so it is flagged for
                # whatever reads time off these rows (tokencounter.latency).  Its tokens are
                # charged as before.
                for r in rows[:matched]:
                    r['replayed'] = True
                # Ancestor history is this file's OWN normalized sequence whether or not the
                # records were charged -- a grandchild may replay an inherited run.
                history[path] = norm

            if not in_scope:
                continue
            charged[path] = rows
            counters['responses'] += len(rows)
            for r in rows:
                u = r['usage']
                counters['input'] += u.get('input_tokens') or 0
                counters['cached'] += u.get('cached_input_tokens') or 0
                counters['output'] += u.get('output_tokens') or 0
                counters['reasoning'] += u.get('reasoning_output_tokens') or 0
                cw = u.get('cache_write_input_tokens')
                if cw is None:
                    counters['cache_write_absent'] += 1
                else:
                    counters['cache_write'] += cw

    return charged, counters
