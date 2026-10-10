"""Visible UTF-8 inventory and local Claude image estimates, never token attribution.

Dispatch is by known field shape. Text, canonical argument JSON and embedded image data
are transient: only lengths, digests and image header measurements leave this module.
"""
import hashlib
import json
import re

from . import images
from .models import bump
from .worker import PROMPT_ATTACHMENTS, _digest, _model_name, _response_identity, epoch

CATEGORIES = (
    'system_prompt', 'tool_schema', 'instructions', 'skill_listing', 'user_message',
    'tool_call_input', 'tool_output', 'assistant_message', 'reasoning_summary',
    'compaction_summary', 'attachment',
)
LABELS = dict(zip(CATEGORIES, (
    'Saved system prompt', 'Saved tool definitions', 'Instructions', 'Skill listings',
    'User text', 'Tool arguments', 'Tool results', 'Assistant text', 'Readable thinking',
    'Compaction summaries', 'Other attachment text',
)))
KNOWN_MODELS = frozenset(
    ['claude-' + family + '-' + version for family in ('fable', 'mythos')
     for version in ('5-1', '5')]
    + ['claude-opus-' + v for v in ('5-5', '5', '4-8', '4-7', '4-6', '4-5', '4-1', '4')]
    + ['claude-sonnet-' + v for v in ('5-5', '5', '4-6', '4-5', '4')]
    + ['claude-haiku-' + v for v in ('5-5', '4-5', '3-5')])


def resize_image(width, height, *, max_edge, max_tokens) -> tuple:
    """Largest aspect-preserving size whose padded dimensions and patches fit."""
    if any(type(v) is not int or v <= 0 for v in (width, height, max_edge, max_tokens)):
        raise ValueError('invalid image limits')
    landscape = width >= height
    long_edge, short_edge = (width, height) if landscape else (height, width)

    def size(edge):
        short = max(1, round(short_edge * edge / long_edge))
        return (edge, short) if landscape else (short, edge)

    def fits(edge):
        w, h = size(edge)
        pw, ph = (w + 27) // 28, (h + 27) // 28
        return pw * 28 <= max_edge and ph * 28 <= max_edge and pw * ph <= max_tokens

    lo, hi = 0, min(long_edge, max_edge)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    if lo == 0:
        raise ValueError('image limits cannot fit one patch')
    return size(lo)


def _image_limits(model):
    canonical = _model_name(model)[0]
    if canonical not in KNOWN_MODELS:
        return None
    # Match the version after the family, including the two-component minor version.
    parts = canonical.split('-')[2:]
    high = tuple(int(p) for p in parts) >= (4, 7)
    return (2576, 4784) if high else (1568, 1568)


def claude_image_tokens(width, height, model, *, transformations_known=True):
    limits = _image_limits(model)
    if not transformations_known or limits is None or any(
            type(v) is not int or v <= 0 for v in (width, height)):
        return None
    w, h = resize_image(width, height, max_edge=limits[0], max_tokens=limits[1])
    return ((w + 27) // 28) * ((h + 27) // 28)


def assign_image_model(image, model):
    canonical = _model_name(model)[0]
    image['model'] = canonical if canonical in KNOWN_MODELS else None
    tokens = claude_image_tokens(image['width'], image['height'], image['model'],
                                 transformations_known=image['transformations_known']
                                 and image['source_kind'] == 'base64')
    image['estimated_visual_tokens'] = tokens
    image['method'] = 'claude-28px-patches' if tokens is not None else None


def _image(block, model, counters):
    source = block.get('source')
    source = source if isinstance(source, dict) else {}
    kind = source.get('type')
    kind = kind if kind in ('base64', 'url', 'file') else 'unknown'
    transformed = block.get('transformations_known', True) is True
    dims = None
    if kind == 'base64' and isinstance(source.get('data'), str):
        mime = source.get('media_type')
        if isinstance(mime, str) and re.fullmatch(r'image/[a-zA-Z0-9.+-]+', mime):
            dims = images.dimensions('data:' + mime + ';base64,' + source['data'])
    if dims is None:
        width = height = None
        bump(counters, 'images_unknown_dimensions')
    else:
        _format, width, height = dims
        bump(counters, 'images_known_dimensions')
    if kind != 'base64':
        bump(counters, 'images_unsupported_source')
    if not transformed:
        bump(counters, 'images_transformations_unknown')
    canonical = _model_name(model)[0]
    if canonical not in KNOWN_MODELS:
        canonical = None
        bump(counters, 'images_unknown_model')
    tokens = claude_image_tokens(width, height, canonical,
                                 transformations_known=transformed and kind == 'base64')
    return {'width': width, 'height': height, 'source_kind': kind,
            'transformations_known': transformed, 'model': canonical,
            'estimated_visual_tokens': tokens, 'method': 'claude-28px-patches' if tokens is not None else None}


def measure_record(record, source, physical_line) -> tuple:
    facts, counters = [], {}
    kind = record.get('type')
    ts = record.get('timestamp') if epoch(record.get('timestamp')) is not None else None
    uuid = record.get('uuid')
    identity = ('record', uuid) if isinstance(uuid, str) and uuid else None
    model = source.get('model')
    if kind == 'assistant':
        message = record.get('message')
        message = message if isinstance(message, dict) else {}
        if message.get('model') == '<synthetic>' or record.get('isApiErrorMessage') is True:
            return facts, counters
        model = message.get('model')
        key, index = _response_identity(record), record.get('apiBlockIndex')
        identity = ('block', key, index) if key and type(index) is int and index >= 0 else None

    def add(body, category, position, snapshot=False, image=None):
        if identity is None:
            bump(counters, 'composition_unkeyed_items')
            return
        length = digest = None
        if body is not None:
            try:
                raw = body.encode('utf-8')
                length, digest = len(raw), hashlib.sha256(raw).hexdigest()
            except UnicodeError:
                bump(counters, 'composition_invalid_unicode')
        facts.append({'item_key': _digest([identity, category, position]), 'family_id': None,
                      'category': category, 'ts': ts, 'utf8_bytes': length,
                      'body_digest': digest, 'snapshot': snapshot, 'image': image})

    def text(body, category, position, snapshot=False):
        if isinstance(body, str):
            add(body, category, position, snapshot)

    def canonical(value, category, position, snapshot=False):
        try:
            body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'), allow_nan=False)
        except (ValueError, TypeError):
            bump(counters, 'composition_unknown_blocks')
            return
        add(body, category, position, snapshot)

    def content(value, category, position, tool_result=False):
        if isinstance(value, str):
            text(value, category, position)
        elif isinstance(value, list):
            for i, block in enumerate(value):
                pos = position + (i,)
                if not isinstance(block, dict):
                    bump(counters, 'composition_unknown_blocks')
                    continue
                typ = block.get('type')
                if typ == 'text':
                    text(block.get('text'), category, pos)
                elif typ == 'thinking':
                    text(block.get('thinking'), 'reasoning_summary', pos)
                elif typ == 'redacted_thinking':
                    # Opaque encrypted data is neither readable text nor an unknown shape.
                    pass
                elif typ == 'tool_use' and kind == 'assistant':
                    if 'input' in block:
                        canonical(block['input'], 'tool_call_input', pos)
                elif typ == 'tool_result' and kind == 'user' and not tool_result:
                    content(block.get('content'), 'tool_output', pos, tool_result=True)
                    if block.get('file_id') or (isinstance(block.get('content'), dict)):
                        bump(counters, 'composition_external_tool_results_unread')
                elif typ == 'image':
                    add(None, category, pos, image=_image(block, model, counters))
                elif typ in ('server_tool_use', 'web_search_tool_result', 'web_fetch_tool_result'):
                    # Structured server results do not establish billable search counts.
                    pass
                elif typ == 'tool_reference':
                    # A tool named by reference inside a result carries no visible text.
                    pass
                elif typ in ('file', 'document'):
                    if tool_result:
                        bump(counters, 'composition_external_tool_results_unread')
                    else:
                        bump(counters, 'composition_unknown_blocks')
                else:
                    bump(counters, 'composition_unknown_blocks')

    def definitions(value, position):
        if not isinstance(value, list):
            return
        for i, entry in enumerate(value):
            if isinstance(entry, dict):
                body = {k: entry[k] for k in ('name', 'description', 'schema', 'input_schema') if k in entry}
                if body:
                    canonical(body, 'tool_schema', position + (i,), True)

    if kind in ('user', 'assistant'):
        message = record.get('message')
        if isinstance(message, dict):
            category = ('compaction_summary' if record.get('isCompactSummary') is True else
                        'user_message' if kind == 'user' else 'assistant_message')
            content(message.get('content'), category, ('message',))
        # Claude Code's own toolUseResult metadata names the file a tool read; its text
        # is already in the tool_result block above, so that path is not unread content.
    elif kind == 'attachment':
        attachment = record.get('attachment')
        if not isinstance(attachment, dict):
            bump(counters, 'composition_unknown_attachments')
            return facts, counters
        typ = attachment.get('type')
        if typ == 'prompt_snapshot':
            prompts = attachment.get('systemPrompt')
            if isinstance(prompts, list):
                for i, prompt in enumerate(prompts):
                    text(prompt, 'system_prompt', ('system', i), True)
            else:
                text(prompts, 'system_prompt', ('system',), True)
            definitions(attachment.get('tools'), ('tools',))
        elif typ == 'deferred_tools_record':
            definitions(attachment.get('entries'), ('deferred',))
            # toolInputCopies are saved state, never another tool-call argument item.
        elif typ == 'instructions':
            files = attachment.get('files')
            if isinstance(files, list):
                for i, file in enumerate(files):
                    if isinstance(file, dict):
                        text(file.get('content'), 'instructions', ('files', i))
        elif typ == 'skill_listing':
            text(attachment.get('content'), 'skill_listing', ('content',))
        elif typ == 'file':
            box = attachment.get('content')
            file = box.get('file') if isinstance(box, dict) else None
            if isinstance(file, dict):
                text(file.get('content'), 'attachment', ('file', 'content'))
        elif typ == 'edited_text_file':
            text(attachment.get('snippet'), 'attachment', ('snippet',))
        elif typ == 'nested_memory':
            box = attachment.get('content')
            if isinstance(box, dict):
                text(box.get('content'), 'attachment', ('content', 'content'))
        elif typ in PROMPT_ATTACHMENTS:
            for field in ('text', 'content', 'snippet'):
                text(attachment.get(field), 'attachment', (field,))
        else:
            bump(counters, 'composition_unknown_attachments')
    return facts, counters


def combine(facts, families) -> tuple:
    """Deduplicate canonical block items and saved state within an attributed family."""
    out, seen, counters = [], set(), {}
    for fact in facts:
        family = families.get(fact['family_id'], fact['family_id']) if families else fact['family_id']
        key = (('snapshot', family, fact['category'], fact['body_digest'])
               if fact['snapshot'] and fact['body_digest'] is not None and family is not None
               else ('item', fact['item_key']))
        if key in seen:
            if fact['snapshot']:
                bump(counters, 'composition_snapshot_copies')
            continue
        seen.add(key)
        out.append(dict(fact, family_id=family))
    return out, counters


def latest_items(facts):
    """Immutable history batches may contain newer derived facts for a record item."""
    latest = {}
    for fact in facts:
        latest[fact['item_key']] = fact
    return list(latest.values())


def prompt_growth(rows, compactions):
    counters = {}
    boundaries = sorted(t for t in (epoch(f['ts']) for f in compactions) if t is not None)
    stamped = ((epoch(r['ts']), r) for r in rows)
    ordered = sorted((item for item in stamped if item[0] is not None), key=lambda item: item[0])
    for (start, a), (end, b) in zip(ordered, ordered[1:]):
        if ('prompt_growth_compaction_skipped' in b.get('quality_flags', ())
                or any(start < t <= end for t in boundaries)):
            bump(counters, 'prompt_growth_compaction_skipped')
        elif _model_name(a['model'])[0] != _model_name(b['model'])[0]:
            bump(counters, 'prompt_growth_model_change_skipped')
        elif b['usage']['input_tokens'] < a['usage']['input_tokens']:
            bump(counters, 'prompt_growth_negative')
    return counters
