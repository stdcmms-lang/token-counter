"""Vendor Anthropic list prices from Markdown, with an offline fixture/check lane.

Runtime never fetches this table. --from selects a local page; --check compares the
entire parsed table and writes nothing. Unknown page shapes fail before any write.
"""
import argparse
import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
import urllib.request

PRICING_URL = 'https://platform.claude.com/docs/en/about-claude/pricing.md'
REPO = Path(__file__).resolve().parent.parent
VENDOR = REPO / 'plugins/token-counter-claude/assets/vendor/anthropic_prices.json'
RATE_KEYS = ('input', 'cache_write_5m', 'cache_write_1h', 'cached', 'output')
ALIASES = {'claude-haiku-4-5-20251001': 'claude-haiku-4-5'}


def _section(markdown, heading):
    match = re.search(r'^#{2,4} ' + re.escape(heading) + r'\s*$', markdown, re.M)
    if match is None:
        raise ValueError('missing pricing section: ' + heading)
    rest = markdown[match.end():]
    stop = re.search(r'^#{2,4} ', rest, re.M)
    return rest[:stop.start()] if stop else rest


def _tables(markdown):
    lines = markdown.splitlines()
    i = 0
    while i + 1 < len(lines):
        if lines[i].strip().startswith('|') and re.match(r'^\|\s*:?-{3}', lines[i + 1].strip()):
            cells = lambda line: [s.strip() for s in line.strip().strip('|').split('|')]
            header, rows = cells(lines[i]), []
            i += 2
            while i < len(lines) and lines[i].strip().startswith('|'):
                row = cells(lines[i])
                if len(row) != len(header):
                    raise ValueError('malformed pricing table row')
                rows.append(row)
                i += 1
            yield header, rows
        else:
            i += 1


def _dollars(cell):
    cell = re.sub(r'<sup>.*?</sup>', '', cell)
    match = re.fullmatch(r'\$(\d+(?:\.\d+)?)\s*/\s*MTok', cell.strip())
    if match is None:
        raise ValueError('unrecognized token price')
    return float(Decimal(match.group(1)))


def _model(label):
    match = re.match(r'^Claude (Fable|Mythos|Opus|Sonnet|Haiku) (\d+(?:\.\d+)?)\b', label)
    if match is None:
        raise ValueError('unrecognized model label')
    version = tuple(int(part) for part in match.group(2).split('.'))
    return 'claude-' + match.group(1).lower() + '-' + match.group(2).replace('.', '-'), version


def parse_prices(markdown, *, as_of, source_url) -> dict:
    if not isinstance(as_of, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}', as_of) is None:
        raise ValueError('invalid price date')
    datetime.date.fromisoformat(as_of)
    if not isinstance(source_url, str) or not source_url:
        raise ValueError('missing price source')
    residency = _section(markdown, 'Data residency pricing')
    cutoff = re.search(r'For Claude (\d+)\.(\d+) and later models', residency)
    vision = re.search(r'Claude (\d+)\.(\d+) and later models.*newer tokenizer', markdown)
    if cutoff is None or vision is None or '1.1x multiplier' not in residency:
        raise ValueError('missing model modifier rules')
    us_since = tuple(map(int, cutoff.groups()))
    high_since = tuple(map(int, vision.groups()))
    tables = list(_tables(_section(markdown, 'Model pricing')))
    expected_header = ['Model', 'Base input tokens', '5m cache writes', '1h cache writes',
                       'Cache hits and refreshes', 'Output tokens']
    if len(tables) != 1 or tables[0][0] != expected_header:
        raise ValueError('unrecognized model pricing table')
    models, row_count = {}, 0
    for row in tables[0][1]:
        name, version = _model(row[0])
        rates = dict(zip(RATE_KEYS, map(_dollars, row[1:])))
        band = re.search(r'for prompts (up to|over) ([\d,]+) tokens', row[0])
        if band and band.group(1) == 'over':
            previous = models.get(name)
            if previous is None or previous['long_context_threshold'] != int(band.group(2).replace(',', '')):
                raise ValueError('unpaired long-prompt prices')
            previous['long_context'] = rates
        else:
            if name in models:
                raise ValueError('duplicate model price')
            models[name] = dict(rates, long_context_threshold=(int(band.group(2).replace(',', '')) if band else None),
                                long_context=None, fast=None,
                                us_residency_supported=version >= us_since,
                                image_tier='high' if version >= high_since else 'standard')
        row_count += 1
    if len(models) != 20 or row_count != 21 or any(
            (r['long_context_threshold'] is None) != (r['long_context'] is None) for r in models.values()):
        raise ValueError('incomplete model price table')
    fast_tables = list(_tables(_section(markdown, 'Fast mode pricing')))
    if len(fast_tables) != 1 or fast_tables[0][0] != ['Model', 'Input', 'Output']:
        raise ValueError('unrecognized Fast pricing table')
    for row in fast_tables[0][1]:
        inp, out = _dollars(row[1]), _dollars(row[2])
        for label in row[0].split(' / '):
            name, _ = _model(label)
            record = models[name]
            multiplier = Decimal(str(inp)) / Decimal(str(record['input']))
            record['fast'] = {k: float(Decimal(str(record[k])) * multiplier) for k in RATE_KEYS}
            record['fast']['output'] = out
    if sum(r['fast'] is not None for r in models.values()) != 3:
        raise ValueError('incomplete Fast price table')
    search = re.search(r'\*\*\$(\d+(?:\.\d+)?) per ([\d,]+) searches\*\*',
                       _section(markdown, 'Web search tool'))
    if search is None or 'no additional charges' not in _section(markdown, 'Web fetch tool'):
        raise ValueError('missing server-tool fees')
    return {'schema': 1, 'vendor': 'anthropic', 'as_of': as_of, 'source_url': source_url,
            'units': 'USD per million tokens', 'models': {k: models[k] for k in sorted(models)},
            'aliases': dict(ALIASES),
            'fees': {'web_search_request': float(Decimal(search.group(1)) / Decimal(search.group(2).replace(',', ''))),
                     'web_fetch_request': 0}}


def fetch_markdown(url, *, timeout=60) -> str:
    request = urllib.request.Request(url, headers={'User-Agent': 'token-counter-claude fetch_prices'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--from', dest='from_path', type=Path)
    parser.add_argument('--as-of', default=datetime.datetime.now(datetime.timezone.utc).date().isoformat())
    parser.add_argument('--out', type=Path, default=VENDOR)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    try:
        page = args.from_path.read_text(encoding='utf-8') if args.from_path else fetch_markdown(PRICING_URL)
        table = parse_prices(page, as_of=args.as_of, source_url=PRICING_URL)
        if args.check:
            try:
                old = json.loads(args.out.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                old = {}
            same = old == table
            print('vendored : %s  %s' % (old.get('as_of') or '(none)', args.out))
            print('parsed   : %d models, as of %s' % (len(table['models']), table['as_of']))
            print('prices   : ' + ('UNCHANGED' if same else 'DIFFERENT'))
            return 0 if same else 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes((json.dumps(table, indent=2, allow_nan=False) + '\n').encode('utf-8'))
        print('wrote %s: %d models, as of %s' % (args.out, len(table['models']), table['as_of']))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print('could not build Anthropic price table: ' + exc.__class__.__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
