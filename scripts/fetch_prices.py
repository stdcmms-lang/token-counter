"""Vendoring of OpenAI's API list prices, for token-report's API value.

Run at packaging time, like fetch_vocab.py. The plugin itself never downloads prices: the
report reads the vendored file and nothing else, so it works offline and two runs over the
same logs give the same figure until this script is run again.

Sources:
  1. https://developers.openai.com/api/docs/pricing.md -- the pricing page as Markdown: the
     flagship tables for each processing tier (Standard, Flex, Fast, Ultrafast; Batch is
     skipped, since Codex never sends a batch request), the Cyber models' table (standard
     rates; a model the flagship tables already price keeps those), the grouped table that
     lists the Codex models, the per-call web search fee, and the long-context threshold.
  2. https://developers.openai.com/api/docs/models/<slug>.md -- a model's own page, for the
     Codex models the pricing page no longer lists. Those pages publish standard rates only,
     so a Fast or Flex response on one of them stays unpriced rather than guessed.

Every figure is copied as the page prints it, in USD per 1M tokens. A "-" is kept as null:
no cached rate means cached input is billed as ordinary input, no cache-write rate means
cache writes are, and no long-context rates on a model that has them for another tier means
a long request in that tier has no published price.

The pricing page the table was built from is kept beside this script
(`fixtures/openai_pricing.md`), so the parser is tested offline against the page it ran on.

Usage:
    python scripts/fetch_prices.py            # fetch, then write assets/vendor/openai_prices.json
    python scripts/fetch_prices.py --check    # fetch, and say how the vendored file differs
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.request

PRICING_URL = 'https://developers.openai.com/api/docs/pricing.md'
MODEL_URL = 'https://developers.openai.com/api/docs/models/{}.md'

# Codex models that have dropped off the pricing page but still appear in older rollouts.
# Each is priced from its own model page, and only when the pricing page does not list it.
MODEL_PAGES = ('gpt-5-codex', 'gpt-5.1-codex', 'gpt-5.1-codex-max', 'gpt-5.1-codex-mini',
               'gpt-5.2-codex', 'codex-mini-latest')

# Models the parse must find, or the page has changed shape and the file is not written.
REQUIRED = ('gpt-5', 'gpt-5.5', 'gpt-5.3-codex', 'gpt-5-codex')

TIERS = ('Standard', 'Flex', 'Fast', 'Ultrafast')
LABELS = TIERS + ('Batch',)
# A grouped table takes the tier named by the nearest label line above it, if one is this
# close: further away, the label belongs to an earlier section.
LABEL_REACH = 12

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VENDOR = os.path.join(REPO, 'plugins', 'token-counter', 'assets', 'vendor',
                      'openai_prices.json')
FIXTURE = os.path.join(HERE, 'fixtures', 'openai_pricing.md')


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'token-counter fetch_prices'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode('utf-8')


def _cells(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def tables(md):
    """Every Markdown table: ``(heading, label, label_line, line, header, rows)``.

    `heading` is the last ``###`` heading above it, `label` the last tier-name line above it
    and `label_line` where that was.
    """
    lines = md.splitlines()
    heading, label, label_at = None, None, None
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith('### '):
            heading = s[4:].strip()
        elif s in LABELS:
            label, label_at = s, i
        if (s.startswith('|') and i + 1 < len(lines)
                and re.match(r'^\|\s*:?-{3}', lines[i + 1].strip())):
            header, rows, at = _cells(s), [], i
            i += 2
            while i < len(lines) and lines[i].strip().startswith('|'):
                rows.append(_cells(lines[i]))
                i += 1
            yield heading, label, label_at, at, header, rows
            continue
        i += 1


def dollars(cell):
    """``$1.25`` -> 1.25, ``-`` -> None.  Anything else is a page format this does not know."""
    cell = cell.strip()
    if cell in ('-', ''):
        return None
    m = re.fullmatch(r'\$(\d+(?:\.\d+)?)', cell.replace(',', ''))
    if not m:
        raise ValueError(f'unrecognised price cell {cell!r}')
    return float(m.group(1))


def _rates(inp, cached, write, out):
    return {'input': inp, 'cached_input': cached, 'cache_write': write, 'output': out}


def _flagship_row(row):
    """``(model, rates)`` from a row of a nine-column flagship table, or None when it has no
    text-token price.  "(<272K context length)" is a note on the name, not part of it."""
    name = re.sub(r'\s*\(.*\)\s*$', '', row[0]).strip()
    v = [dollars(c) for c in row[1:9]]
    if v[0] is None or v[3] is None:
        return None
    long = None if v[4] is None else _rates(v[4], v[5], v[6], v[7])
    return name, dict(_rates(*v[:4]), long=long)


def parse_pricing(md):
    """``(models, long_context_threshold, web_search_per_call)`` from the pricing page."""
    models = {}
    cyber = {}
    for heading, label, label_at, at, header, rows in tables(md):
        tier_heading = re.fullmatch(r'(\w+) pricing data', heading or '')
        flagship_shape = header[:2] == ['Model', 'Short context input'] and len(header) == 9
        if tier_heading and tier_heading.group(1) in TIERS and flagship_shape:
            tier = tier_heading.group(1).lower()
            for row in rows:
                got = _flagship_row(row)
                if got:
                    models.setdefault(got[0], {})[tier] = got[1]
        elif heading == 'Grouped Pricing Table data' and flagship_shape:
            # The Cyber models: the flagship shape under a grouped heading, with no tier tabs.
            # Read as standard rates; it also repeats a flagship model, which keeps its own.
            for row in rows:
                got = _flagship_row(row)
                if got:
                    cyber[got[0]] = {'standard': got[1]}
        elif header == ['Category', 'Model', 'Input', 'Cached input', 'Output']:
            if label not in TIERS or label_at is None or at - label_at > LABEL_REACH:
                continue
            for row in rows:
                if row[0] != 'Codex':
                    continue
                inp, cached, out = (dollars(c) for c in row[2:5])
                if inp is None or out is None:
                    continue
                models.setdefault(row[1], {})[label.lower()] = dict(
                    _rates(inp, cached, None, out), long=None)

    for name, tiers in cyber.items():
        models.setdefault(name, tiers)

    m = re.search(r'Long context:\s*>\s*(\d+)K input tokens', md)
    if not m:
        raise ValueError('long-context threshold not found on the pricing page')
    threshold = int(m.group(1)) * 1000

    ws = None
    for _h, _l, _la, _at, header, rows in tables(md):
        if header[:1] != ['Tool']:
            continue
        for row in rows:
            if row[:2] == ['Web search', 'Web search (all models)']:
                f = re.match(r'\$(\d+(?:\.\d+)?)\s*/\s*1k calls', row[2])
                if f:
                    ws = float(f.group(1)) / 1000
    if ws is None:
        raise ValueError('web search fee not found on the pricing page')
    return models, threshold, ws


def parse_model_page(md):
    """Standard rates from a model's own page: its Text tokens table."""
    got = {}
    for _h, _l, _la, _at, header, rows in tables(md):
        if header[:3] != ['Metric', 'Price', 'Unit']:
            continue
        for row in rows:
            if len(row) >= 3 and row[2] == '1M tokens':
                got[row[0]] = dollars(row[1])
        break
    if got.get('Input') is None or got.get('Output') is None:
        raise ValueError('no text-token prices on the model page')
    return dict(_rates(got['Input'], got.get('Cached input'), None, got['Output']), long=None)


def build(today=None):
    """``(table, pricing page)``: the table, and the page it was parsed from."""
    md = fetch(PRICING_URL)
    models, threshold, ws = parse_pricing(md)
    sources = {}
    for slug in MODEL_PAGES:
        if slug in models:
            continue
        url = MODEL_URL.format(slug)
        models[slug] = {'standard': parse_model_page(fetch(url))}
        sources[slug] = url.replace('.md', '')
    missing = [m for m in REQUIRED if m not in models]
    if missing:
        raise ValueError(f'the pricing page no longer lists {", ".join(missing)}')
    for name, tiers in models.items():
        if 'standard' not in tiers:
            raise ValueError(f'{name} has no standard rates')
        tiers['long_context'] = any(t['long'] for k, t in tiers.items() if isinstance(t, dict))
        if name in sources:
            tiers['source'] = sources[name]
    return md, {
        'source': PRICING_URL.replace('.md', ''),
        'as_of': today or datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
        'currency': 'USD',
        'unit_tokens': 1_000_000,
        'long_context_threshold': threshold,
        'web_search_per_call': ws,
        'notes': ('OpenAI API list prices, copied from the pricing page and model pages. '
                  'Batch rates and the 10% regional-processing uplift are not included.'),
        'models': {k: models[k] for k in sorted(models)},
    }


def diff(old, new):
    """Lines saying how `new` differs from `old`, the date it was fetched aside."""
    out = []
    for k in sorted(set(old) | set(new)):
        if k in ('as_of', 'models'):
            continue
        if old.get(k) != new.get(k):
            out.append(f'{k}: {old.get(k)!r} -> {new.get(k)!r}')
    om, nm = old.get('models') or {}, new.get('models') or {}
    for m in sorted(set(om) | set(nm)):
        if m not in nm:
            out.append(f'- {m}')
        elif m not in om:
            out.append(f'+ {m}')
        elif om[m] != nm[m]:
            out.append(f'~ {m}: {json.dumps(om[m], sort_keys=True)}\n'
                       f'      -> {json.dumps(nm[m], sort_keys=True)}')
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--check', action='store_true',
                    help='report how the vendored file differs from the live pages; write nothing')
    a = ap.parse_args(argv)
    try:
        page, new = build()
    except Exception as exc:
        print(f'could not build the price table: {exc.__class__.__name__}: {exc}',
              file=sys.stderr)
        return 2
    try:
        with open(VENDOR, encoding='utf-8') as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        old = {}
    changes = diff(old, new)
    if a.check:
        print(f'vendored : {old.get("as_of") or "(none)"}  {VENDOR}')
        print(f'live     : {len(new["models"])} models')
        for line in changes:
            print('  ' + line)
        print('prices   : ' + ('UNCHANGED' if not changes else f'{len(changes)} change(s)'))
        return 1 if changes else 0
    os.makedirs(os.path.dirname(VENDOR), exist_ok=True)
    with open(VENDOR, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(new, fh, indent=1, sort_keys=False)
        fh.write('\n')
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(page)
    print(f'wrote {VENDOR}: {len(new["models"])} models, as of {new["as_of"]}')
    for line in changes:
        print('  ' + line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
