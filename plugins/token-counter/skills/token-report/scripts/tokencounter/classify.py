"""Content classification.

Turns a rollout payload into ``(category, text)`` segments plus image references.  Reported
usage gives totals; it never says what filled the window -- this is what supplies that
(ARCHITECTURE.md section 5.1).

``event_msg``/``item_completed`` mirrors ``response_item`` content and is deliberately
**not** classified, or every item would be counted twice.
"""
import json

# Ordered for display; also the canonical category vocabulary.
CATEGORIES = (
    'system_prompt',
    'environment',
    'tool_schema',
    'developer_instructions',
    'agents_md',
    'user_message',
    'assistant_message',
    'agent_message',
    'tool_call_input',
    'tool_output',
    'reasoning_summary',
    'reasoning_blob',
    'image',
    'other',
)

# Categories whose bytes are opaque: counted as characters, never as tokens.
OPAQUE = frozenset({'reasoning_blob', 'image'})

OUTPUT_ITEMS = frozenset({'reasoning', 'function_call', 'custom_tool_call',
                          'local_shell_call', 'compaction'})


def item_role(payload):
    """``'input'`` or ``'output'`` -- which side of the model call produced this item.

    Anything the model emits as a ``*_call`` is output, as `worker._timing_side` reads it:
    a ``web_search_call`` between a response's reasoning and its message is part of that
    response, and calling it input split the response's own output into its prompt.
    """
    t = payload.get('type')
    if t == 'message':
        return 'output' if payload.get('role') == 'assistant' else 'input'
    if t in OUTPUT_ITEMS or (isinstance(t, str) and t.endswith('_call')):
        return 'output'
    return 'input'


def _text_elements(container, seg, images, default_cat):
    """Walk a ``content``/``output`` value, which may be a str, a list, or absent."""
    if container is None:
        return
    if isinstance(container, str):
        if container:
            seg.append((default_cat, container))
        return
    if not isinstance(container, list):
        seg.append((default_cat, json.dumps(container, ensure_ascii=False)))
        return
    for el in container:
        if isinstance(el, str):
            if el:
                seg.append((default_cat, el))
            continue
        if not isinstance(el, dict):
            continue
        et = el.get('type')
        if et in ('input_text', 'output_text', 'text', 'summary_text'):
            t = el.get('text')
            if t:
                seg.append((default_cat, t))
        elif et == 'input_image':
            images.append(el.get('image_url'))
        elif et == 'encrypted_content':
            blob = el.get('encrypted_content')
            if blob:
                seg.append(('reasoning_blob', blob))
        elif et == 'refusal':
            t = el.get('refusal')
            if t:
                seg.append((default_cat, t))
        else:
            t = el.get('text')
            if t:
                seg.append((default_cat, t))


def response_item(payload):
    """``(segments, images)`` for one ``response_item`` payload."""
    seg, images = [], []
    t = payload.get('type')

    if t == 'message':
        role = payload.get('role')
        cat = {'user': 'user_message',
               'assistant': 'assistant_message',
               'developer': 'developer_instructions',
               'system': 'system_prompt'}.get(role, 'other')
        _text_elements(payload.get('content'), seg, images, cat)

    elif t == 'agent_message':
        _text_elements(payload.get('content'), seg, images, 'agent_message')

    elif t in ('function_call', 'custom_tool_call', 'local_shell_call'):
        name = payload.get('name')
        if name:
            seg.append(('tool_call_input', name))
        body = payload.get('arguments')
        if body is None:
            body = payload.get('input')
        if body is None:
            body = payload.get('action')
        if isinstance(body, str):
            if body:
                seg.append(('tool_call_input', body))
        elif body is not None:
            seg.append(('tool_call_input', json.dumps(body, ensure_ascii=False)))

    elif t in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output'):
        _text_elements(payload.get('output'), seg, images, 'tool_output')

    elif t == 'reasoning':
        for s in (payload.get('summary') or []):
            if isinstance(s, dict):
                txt = s.get('text')
                if txt:
                    seg.append(('reasoning_summary', txt))
            elif isinstance(s, str) and s:
                seg.append(('reasoning_summary', s))
        blob = payload.get('encrypted_content')
        if blob:
            seg.append(('reasoning_blob', blob))
        _text_elements(payload.get('content'), seg, images, 'reasoning_summary')

    elif t == 'compaction':
        blob = payload.get('encrypted_content')
        if blob:
            seg.append(('reasoning_blob', blob))

    else:
        for key in ('text', 'content', 'output'):
            if key in payload:
                _text_elements(payload.get(key), seg, images, 'other')
                break

    return seg, images


def session_meta(payload):
    """``(segments, images)`` for the system prompt and any persisted tool schemas."""
    seg, images = [], []
    bi = payload.get('base_instructions')
    if isinstance(bi, dict):
        txt = bi.get('text')
    else:
        txt = bi if isinstance(bi, str) else None
    if txt:
        seg.append(('system_prompt', txt))
    dt = payload.get('dynamic_tools')
    if dt:
        seg.append(('tool_schema', json.dumps(dt, ensure_ascii=False)))
    return seg, images


def world_state(payload):
    """``(segments, images)`` for the environment block and AGENTS.md content."""
    seg, images = [], []
    state = payload.get('state')
    if not isinstance(state, dict):
        if state:
            seg.append(('environment', json.dumps(state, ensure_ascii=False)))
        return seg, images
    rest = {}
    for k, v in state.items():
        if k == 'agents_md' and v:
            seg.append(('agents_md', json.dumps(v, ensure_ascii=False)))
        else:
            rest[k] = v
    if rest:
        seg.append(('environment', json.dumps(rest, ensure_ascii=False)))
    return seg, images
