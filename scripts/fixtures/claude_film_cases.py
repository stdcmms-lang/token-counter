"""Invented film transcripts. Expected numbers come from the agreed arithmetic.

No real corpus, account or reference step array is read by this module.
"""
import copy
import datetime
from pathlib import Path
from claude_cases import assistant_record, _usage, _user, write_corpus

PROJECT = 'PRIVATE_PATH_FILM'
SESSION = 'PRIVATE_SESSION_FILM_A'
SENTINELS = ('PRIVATE_', '</script>', 'https://private.invalid', 'SECRET_')


def stamp(seconds):
    dt = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return dt.isoformat().replace('+00:00', 'Z')


def text(kind='text', suffix='TEXT'):
    return {'type': kind, 'thinking' if kind == 'thinking' else 'text':
            'PRIVATE_' + suffix + ' </script> https://private.invalid SECRET_' + suffix}


def tool(name, suffix):
    return {'type': 'tool_use', 'id': 'PRIVATE_CALL_' + suffix, 'name': name,
            'input': {'PRIVATE_KEY': 'PRIVATE_INPUT_' + suffix + ' </script> https://private.invalid'}}


def response(n, seconds, values, blocks=None, index=0, model='claude-opus-5-5'):
    r = assistant_record('PRIVATE_MESSAGE_%d' % n, 'PRIVATE_REQUEST_%d' % n,
                         stamp(seconds), _usage(*values), uuid='PRIVATE_R%d_%d' % (n, index),
                         parent_uuid='PRIVATE_UNRELATED', block_index=index, model=model,
                         content=blocks or [])
    return r


def boundary(name, seconds, trigger=None, pre=None, post=None):
    box = {}
    if trigger is not None:
        box['trigger'] = trigger
    if pre is not None:
        box['preTokens'] = pre
    if post is not None:
        box['postTokens'] = post
    return {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'PRIVATE_BOUNDARY_' + name,
            'timestamp': stamp(seconds), 'compactMetadata': box}


def summary(name, seconds):
    r = _user('PRIVATE_SUMMARY_' + name, stamp(seconds), [text(suffix='SUMMARY_' + name)])
    r['isCompactSummary'] = True
    return r


def primary():
    records = [
        _user('PRIVATE_U0', stamp(0), [text(suffix='PROMPT0')]),
        response(1, 5, (10, 20, 30, 40, 2, 1, 1), [text('thinking', 'THINK1')]),
        response(1, 10, (10, 20, 30, 40, 8, 3, 1), [text(suffix='OUTPUT1'), tool('Read', 'READ')], index=1),
        response(90, 11, (1, 2, 3, 4, 5, 1), [text(suffix='SYNTHETIC')], model='<synthetic>'),
        response(91, 12, (1, 2, 3, 4, 5, 1), [text(suffix='ERROR')]),
        response(0, 13, (0, 0, 0, 0, 0, 0), [text(suffix='ZERO')]),
        _user('PRIVATE_TOOLRESULT', stamp(14), [{'type': 'tool_result', 'tool_use_id': 'PRIVATE_CALL_READ',
                                              'content': 'PRIVATE_RESULT </script> https://private.invalid'}]),
        response(2, 20, (5, 0, 100, 50, 10, 4), [tool('mcp__PRIVATE_TOOL_MCP', 'MCP'), tool('PRIVATE_TOOL_CUSTOM', 'CUSTOM')]),
        boundary('MANUAL', 21, 'manual', 155, 10), summary('MANUAL', 22),
        _user('PRIVATE_U1', stamp(23), [text(suffix='PROMPT1')]),
        response(3, 30, (2, 80000, 0, 50, 20, 5), [text('thinking', 'THINK3'), tool('Bash', 'BASH'), text(suffix='OUTPUT3')]),
        boundary('AUTO', 31, 'auto', 80052, 20), summary('AUTO', 32),
        response(4, 40, (3, 0, 0, 60, 6, 2, 2), [text(suffix='OUTPUT4')], model='claude-unknown-film'),
        boundary('UNKNOWN', 41), summary('UNKNOWN', 42),
        response(5, 50, (1, 0, 5, 70, 4, 0), [text(suffix='OUTPUT5')]),
        response(5, 51, (1, 0, 5, 70, 7, 0), [text(suffix='INVALID5')], index=1),
        response(6, 65, (1, 0, 0, 100, 2, 1), [text('thinking', 'THINK6'), text(suffix='OUTPUT6')]),
    ]
    records[4]['isApiErrorMessage'] = True
    records[18]['message']['usage']['input_tokens'] = True
    child = [_user('PRIVATE_CHILD_U', stamp(0), [text(suffix='CHILD_PROMPT')]),
             response(100, 5, (2, 0, 10, 20, 5, 1), [text(suffix='CHILD')])]
    for r in records + child:
        r.update(sessionId=SESSION, cwd='PRIVATE_CWD', title='PRIVATE_TITLE', project='PRIVATE_PROJECT',
                 privateUrl='https://private.invalid', privateMarkup='</script>')
    return {'files': {PROJECT + '/' + SESSION + '.jsonl': records,
                      PROJECT + '/' + SESSION + '/subagents/agent-child.jsonl': child}}


def variant(name):
    case = primary()
    records = case['files'][PROJECT + '/' + SESSION + '.jsonl']
    if name in ('missing_thinking', 'under_threshold'):
        del records[19]['message']['usage']['output_tokens_details']
        if name == 'under_threshold':
            records[11]['message']['usage']['output_tokens'] = 220
    elif name == 'zero_input':
        for r in records:
            if r.get('type') == 'assistant' and r is not records[18]:
                r['message']['usage']['input_tokens'] = 0
    elif name == 'duplicate':
        duplicate = copy.deepcopy(records[2])
        duplicate['uuid'] += '_DUPLICATE'
        records.insert(3, duplicate)
    elif name == 'physical_prompt':
        records.insert(5, _user('PRIVATE_UNRELATED_PROMPT', stamp(12.5), [text(suffix='INSERTED')], 'PRIVATE_ABSENT_PARENT'))
    elif name == 'unknown_trigger':
        records[15]['compactMetadata']['trigger'] = 'PRIVATE_TRIGGER </script>'
    elif name == 'zero_only':
        records[:] = [records[5]]
    elif name == 'error_only':
        records[:] = [records[3], records[4]]
    elif name == 'safe_overflow':
        records[19]['message']['usage']['cache_read_input_tokens'] = 9007199254740992
    elif name == 'cumulative_overflow':
        records[19]['message']['usage']['cache_read_input_tokens'] = 9007199254740991
    elif name != 'primary':
        raise ValueError('unknown invented variant')
    return case


def small(name):
    records = []
    n = 2 if name == 'edges' else 21
    if name == 'edges':
        records.append(boundary('ZERO', 0))
    for i in range(n):
        records.append(response(i + 1, i + 1, (0, 0, 0, 1, 0, 0)))
        if name == 'edges' and i == 0:
            records.extend([boundary('ONE_A', 1), boundary('ONE_B', 1)])
        if name == 'dense' and i == 19:
            records.extend(boundary('DENSE_%d' % k, 20) for k in range(100))
    if name == 'edges':
        records.append(boundary('END', 3))
    return {'files': {PROJECT + '/' + SESSION + '.jsonl': records}}


def write(root, case=None):
    case = primary() if case is None else case
    write_corpus(root, case)
    return Path(root) / PROJECT / (SESSION + '.jsonl')


def r1(speed='standard', geo='global'):
    case = primary()
    records = case['files'][PROJECT + '/' + SESSION + '.jsonl']
    records[:] = records[:3]
    for r in records[1:]:
        r['message']['usage'].update(speed=speed, inference_geo=geo)
    return case
