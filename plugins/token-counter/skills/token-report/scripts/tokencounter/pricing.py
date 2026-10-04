"""What the recorded usage would cost at OpenAI's API list prices -- not what anyone paid.

A ChatGPT plan is not billed per token, so this is a counterfactual: each charged response
priced as the API would price that same request, from the usage Codex recorded for it.  It
is computed per response, never from totals, because the rate depends on the response:

- **Model.**  The model of the turn that sent it, looked up in the vendored price table
  (``assets/vendor/openai_prices.json``, written by ``scripts/fetch_prices.py``).  Nothing
  is downloaded: a model the table does not list is unpriced and counted, never guessed.
- **Processing tier.**  The thread's tier setting, from the `thread_settings_applied`
  snapshots Codex writes into the rollout: ``priority`` (Fast mode) and ``flex`` have their
  own rates.  Codex never persists the snapshot for a thread's first turn, so that turn
  takes the file's first snapshot (`tier_inferred`, see `worker._backfill_tier`).  A file
  with no snapshot at all -- a one-turn `codex exec` run, or an older Codex -- is priced at
  standard and counted as `tier_unrecorded`, with an upper bound priced at Fast beside it.
  A per-turn tier override is not logged, nor is the tier the server actually served.
- **Prompt size.**  Above the long-context threshold (272K input tokens) some models charge
  long-context rates for the whole request.  A tier with no published long-context rate on
  such a model leaves the response unpriced.
- **Input split.**  ``input_tokens`` holds the whole prompt; cached reads and cache writes
  are parts of it, disjoint from each other, as OpenAI's own cost formula takes them
  (ordinary = input - cached - cache writes).  A model with no cached rate bills cached
  tokens as ordinary input; one with no cache-write rate bills writes as ordinary input.
  Reasoning tokens are part of output and are not added again.

Web searches carry a per-call fee on top of their tokens, which ride in input already.
"""
import json
import math
import os
import re

_PKG = os.path.dirname(os.path.abspath(__file__))
# scripts/tokencounter -> scripts -> token-report -> skills -> <plugin root>
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_PKG))))
DEFAULT_PRICES = os.path.join(_PLUGIN_ROOT, 'assets', 'vendor', 'openai_prices.json')

# The `service_tier` Codex writes, to the price table's tier.  ``default`` is Codex's own
# sentinel for "no tier requested"; ``priority`` is what Fast mode sends.
TIER_OF = {'default': 'standard', 'auto': 'standard', 'standard': 'standard',
           'priority': 'fast', 'fast': 'fast', 'flex': 'flex', 'ultrafast': 'ultrafast'}

RATE_KEYS = ('input', 'cached_input', 'cache_write', 'output')
_DATED = re.compile(r'-\d{4}-\d{2}-\d{2}$')


def _n(v):
    """A token count from a usage record, 0 when it is not one: a damaged record must cost
    its own price, not the run."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return max(v, 0)
    if isinstance(v, float) and v == v and v not in (float('inf'), float('-inf')):
        return max(int(v), 0)
    return 0


def prices_path(override=None):
    return override or os.environ.get('TOKEN_COUNTER_PRICES') or DEFAULT_PRICES


def _rate_ok(v, required):
    """A rate is a finite, non-negative number; None only where a rate may be absent.
    `Infinity` and `NaN` parse from JSON and would price a response at inf or nan."""
    if v is None:
        return not required
    return (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            and v >= 0)


def _rates_ok(r):
    if not isinstance(r, dict):
        return False
    return (_rate_ok(r.get('input'), True) and _rate_ok(r.get('output'), True)
            and _rate_ok(r.get('cached_input'), False)
            and _rate_ok(r.get('cache_write'), False))


def load(path=None):
    """``(table, None)`` or ``(None, reason)``.

    Validated whole before any of it is used: a table with one malformed rate would price
    some responses and silently skip others, and a total built that way is wrong without
    looking wrong.
    """
    p = prices_path(path)
    try:
        with open(p, encoding='utf-8') as fh:
            t = json.load(fh)
    except OSError as exc:
        return None, f'price table unreadable ({exc.__class__.__name__}: {p})'
    except ValueError as exc:
        return None, f'price table is not JSON ({exc.__class__.__name__}: {p})'
    if not isinstance(t, dict) or not isinstance(t.get('models'), dict) or not t['models']:
        return None, f'price table has no models ({p})'
    th = t.get('long_context_threshold')
    if not isinstance(th, int) or isinstance(th, bool) or th <= 0:
        return None, f'price table has no long-context threshold ({p})'
    ws = t.get('web_search_per_call')
    if not _rate_ok(ws, False):
        return None, f'price table has a malformed web search fee ({p})'
    # Every price divides by it: zero, a string or null would raise mid-run.
    ut = t.setdefault('unit_tokens', 1_000_000)
    if not isinstance(ut, int) or isinstance(ut, bool) or ut <= 0:
        return None, f'price table has a malformed unit_tokens ({p})'
    for name, tiers in t['models'].items():
        if not isinstance(tiers, dict) or 'standard' not in tiers:
            return None, f'price table: {name} has no standard rates ({p})'
        for tier in set(TIER_OF.values()):
            r = tiers.get(tier)
            if r is None:
                continue
            if not _rates_ok(r) or (r.get('long') is not None and not _rates_ok(r['long'])):
                return None, f'price table: {name} has malformed {tier} rates ({p})'
    t['path'] = p
    return t, None


def lookup(table, model):
    """The table's name for `model`, or None.  A dated snapshot falls back to its alias."""
    if not isinstance(model, str) or not model.strip():
        return None
    m = model.strip().lower()
    models = table['models']
    for cand in (m, _DATED.sub('', m)):
        if cand in models:
            return cand
    return None


def tier_name(service_tier):
    """The price table's tier for a recorded `service_tier`; None for a value it does not
    know.  An unrecorded tier (None) is standard, and the caller counts it as unrecorded."""
    if service_tier is None:
        return 'standard'
    return TIER_OF.get(str(service_tier).strip().lower())


def price(table, row):
    """``(usd, why)`` for one charged ledger row: the price, or None and the reason.

    `why` is None when priced, else ``'model'`` (not in the table), ``'tier'`` (a tier the
    table has no rates for on that model) or ``'long_context'`` (a long prompt in a tier
    with no long-context rate).  Web search fees are not included; see :func:`web_search`.
    """
    model = lookup(table, row.get('model'))
    if model is None:
        return None, 'model'
    tiers = table['models'][model]
    tier = tier_name(row.get('tier'))
    rates = tiers.get(tier) if tier else None
    if rates is None:
        return None, 'tier'
    u = row.get('usage') or {}
    inp = _n(u.get('input_tokens'))
    cached = min(_n(u.get('cached_input_tokens')), inp)
    writes = min(_n(u.get('cache_write_input_tokens')), inp - cached)
    out = _n(u.get('output_tokens'))
    if inp > table['long_context_threshold'] and tiers.get('long_context'):
        rates = rates.get('long')
        if rates is None:
            return None, 'long_context'
    r_in = rates['input']
    r_cached = rates['cached_input'] if rates.get('cached_input') is not None else r_in
    r_write = rates['cache_write'] if rates.get('cache_write') is not None else r_in
    usd = ((inp - cached - writes) * r_in + cached * r_cached + writes * r_write
           + out * rates['output']) / table['unit_tokens']
    return usd, None


def is_long(table, row):
    """Whether a row was priced at long-context rates."""
    model = lookup(table, row.get('model'))
    if model is None or not table['models'][model].get('long_context'):
        return False
    return _n((row.get('usage') or {}).get('input_tokens')) > table['long_context_threshold']


def web_search(table, row):
    """The per-call web search fee for the searches made in producing `row`."""
    return _n(row.get('web_search')) * (table.get('web_search_per_call') or 0.0)


def describe(table):
    """One line naming the table: where it came from, when, and how many models."""
    return (f"OpenAI API list prices as of {table.get('as_of') or 'an unknown date'}, "
            f"{len(table['models'])} models")
