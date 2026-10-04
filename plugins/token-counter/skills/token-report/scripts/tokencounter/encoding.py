"""Offline ``o200k_base`` construction from a vendored BPE blob.

Deliberately does **not** rely on ``TIKTOKEN_CACHE_DIR``: ``tiktoken.load.read_file_cached``
derives its cache filename from ``sha1(vocab_url)``, so a human-readable asset name is never
found and tiktoken silently falls through to a network fetch that fails under a restricted
sandbox. We read the vendored file directly and construct the ``Encoding`` ourselves.

"Directly" means without ``tiktoken.load`` at all (``read_ranks``): its loader copies every
file it reads into ``$TMPDIR/data-gym-cache`` under ``sha1(path)`` and serves that copy from
then on, and tiktoken 0.7.0 -- the last release for Python 3.8 -- cannot read a local path
without the ``blobfile`` package.

See ARCHITECTURE.md section 4.
"""
import base64
import functools
import os

O200K_PAT = (
    r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?"""
    r"""|[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?"""
    r"""|\p{N}{1,3}"""
    r"""| ?[^\s\p{L}\p{N}]+[\r\n/]*"""
    r"""|\s*[\r\n]+"""
    r"""|\s+(?!\S)"""
    r"""|\s+"""
)

O200K_SPECIAL = {"<|endoftext|>": 199999, "<|endofprompt|>": 200018}

ENCODING_NAME = 'o200k_base_vendored'

_PKG = os.path.dirname(os.path.abspath(__file__))
# scripts/tokencounter -> scripts -> token-report -> skills -> <plugin root>
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_PKG))))
DEFAULT_VENDOR = os.path.join(_PLUGIN_ROOT, 'assets', 'vendor', 'o200k_base.tiktoken')


def vendor_path(override=None):
    return override or os.environ.get('TOKEN_COUNTER_VOCAB') or DEFAULT_VENDOR


MIN_RANKS = 190_000          # o200k_base has 199,998 mergeable ranks; anything far below
#                              this means a truncated or corrupt vendored blob.


def read_ranks(path):
    """The mergeable ranks in a ``.tiktoken`` file: one ``base64(token) rank`` pair per line.

    Parsed the way ``tiktoken.load.load_tiktoken_bpe`` parses it, from the file itself on
    every call.  That loader is not used: it reads through a cache that is keyed by path and
    never checked against the file, so a vocabulary changed in place keeps loading as it was,
    and under tiktoken 0.7.0 it raises for any local path unless ``blobfile`` is installed.
    """
    with open(path, 'rb') as fh:
        contents = fh.read()
    ranks = {}
    seen = set()
    for n, line in enumerate(contents.splitlines(), 1):
        if not line:
            continue
        try:
            token, rank = line.split()
            token, rank = base64.b64decode(token), int(rank)
        except Exception as exc:
            raise ValueError(f'line {n} is not "<base64 token> <rank>": {line[:60]!r}') from exc
        # A file cut off inside a rank ("... 19") repeats an earlier rank.  tiktoken panics
        # on that with a pyo3 PanicException, which `except Exception` does not catch.
        if token in ranks or rank in seen:
            raise ValueError(f'line {n} repeats a token or rank given earlier: {line[:60]!r}')
        ranks[token] = rank
        seen.add(rank)
    return ranks


def load(path=None):
    """Build the encoding. Cached per process; costs ~0.3s on first call.

    Cached by the file `path` resolves to, not by the argument: ``load()`` and
    ``load(vendor_path())`` are one vocabulary, and keying on the raw argument built it twice.
    """
    return _load(vendor_path(path))


@functools.lru_cache(maxsize=2)
def _load(p):
    try:
        from tiktoken import Encoding
    except ImportError as exc:                      # pragma: no cover - environment issue
        raise ImportError(
            'tiktoken is required but not importable. Install it with:\n'
            '    python -m pip install tiktoken\n'
            f'(original error: {exc})'
        ) from exc

    if not os.path.isfile(p):
        raise FileNotFoundError(
            f'vendored BPE not found at {p}.\n'
            'Vendor it once with:  python scripts/fetch_vocab.py\n'
            'The plugin never downloads at runtime.'
        )
    try:
        ranks = read_ranks(p)
    except Exception as exc:
        raise ValueError(
            f'vendored BPE at {p} could not be parsed ({exc.__class__.__name__}: {exc}).\n'
            'Re-vendor it with:  python scripts/fetch_vocab.py'
        ) from exc
    # A truncated blob would otherwise build an Encoding that silently produces wrong counts.
    if len(ranks) < MIN_RANKS:
        raise ValueError(
            f'vendored BPE at {p} holds only {len(ranks):,} ranks, expected ~200,000. '
            'The file is truncated or corrupt.\n'
            'Re-vendor it with:  python scripts/fetch_vocab.py'
        )
    return Encoding(
        name=ENCODING_NAME,
        pat_str=O200K_PAT,
        mergeable_ranks=ranks,
        special_tokens=O200K_SPECIAL,
    )


# The tests stand a cleared cache in for a new process, through the public name.
load.cache_clear = _load.cache_clear
