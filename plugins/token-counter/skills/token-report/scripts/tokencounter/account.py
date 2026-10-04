"""Codex account identity, read from the local credential file.

This is the **only** part of the system that opens anything outside ``~/.codex/sessions``,
and the exception is deliberate: a rollout log records no account at all.  Two accounts'
logs in one corpus, or a report handed to someone else, are otherwise unattributable.
See ARCHITECTURE.md section 2.6.

What is read, and what is refused, is the whole of the design:

* ``~/.codex/auth.json`` is opened read-only and parsed as JSON.  Nothing is written back.
* Of the OAuth material inside, only ``tokens.id_token`` is touched, and only its middle
  segment -- the claims, which are an unsigned, unencrypted base64url JSON object.  The
  signature segment is never decoded.  ``access_token`` and ``refresh_token`` are never
  read out of the parsed object, never passed on and never logged.
* From the claims, only identity fields are lifted: email, display name, account id, plan,
  subscription window.  The extraction is an allow-list, not a filter, so no bearer
  material can reach the report model, the JSON export, the HTML or the on-disk index --
  including if OpenAI later adds a claim that happens to carry one.

The claims are **not verified**.  Checking a JWT signature requires the issuer's keys and
therefore a network call, and this tool makes none.  The file is read as a local statement
of which account the CLI is signed into -- which is the question being asked -- and never
as an authentication decision.  A stale or hand-edited file yields a wrong name, not a
wrong number: nothing here feeds any figure in the report.

``report.py --no-account`` skips the file entirely and every field below reports as absent.
"""
import base64
import json
import os

from . import rollout

# Claim names, as the id_token spells them.
PLAIN_CLAIMS = {'email': 'email', 'name': 'name'}

# OpenAI namespaces its own claims with a URL, and two spellings exist: a nested object under
# the namespace (what the tokens on this machine carry) and the flattened
# ``<namespace>/<claim>`` form that is the more usual JWT convention.  Both are read.
# Supporting only one loses the plan and the account id *silently*, because the plain claims
# beside them still populate and the record looks healthy.  The claim set also varies between
# issuances of the same account's token -- a refresh here replaced a token carrying these
# with one that did not -- so every field is optional by construction and the report falls
# back to the plan the rollout logs report.
NS = 'https://api.openai.com/auth'
NS_CLAIMS = {
    'account_id': 'chatgpt_account_id',
    'plan': 'chatgpt_plan_type',
    'subscription_start': 'chatgpt_subscription_active_start',
    'subscription_until': 'chatgpt_subscription_active_until',
}

# A signed-in id_token is a few kB.  Anything far larger is not one, and is not decoded.
MAX_TOKEN_BYTES = 1 << 20


def codex_home(sessions_root=None):
    """Directory holding ``auth.json``.

    When ``--sessions-root`` overrides the corpus location, the credential file is looked
    for beside it rather than in the real ``~/.codex``.  Without that, a test or a report
    over a copied corpus would silently reach into the live account -- and put a real email
    address into output that has nothing to do with it.
    """
    if sessions_root:
        return os.path.dirname(os.path.abspath(sessions_root))
    return rollout.codex_home()


def auth_path(sessions_root=None):
    return os.path.join(codex_home(sessions_root), 'auth.json')


def _claims(id_token):
    """Claims from a JWT's payload segment, or ``None``.

    Only segment 1 is decoded.  Segment 2 is the signature and is never touched; segment 0
    is the header and carries no identity.
    """
    if not isinstance(id_token, str) or id_token.count('.') != 2:
        return None
    if len(id_token) > MAX_TOKEN_BYTES:
        return None
    seg = id_token.split('.')[1]
    try:
        raw = base64.urlsafe_b64decode(seg + '=' * (-len(seg) % 4))
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def blank(reason=None):
    return {'available': False, 'reason': reason, 'source': None, 'auth_mode': None,
            'email': None, 'name': None, 'account_id': None, 'plan': None,
            'subscription_start': None, 'subscription_until': None, 'last_refresh': None}


def read(sessions_root=None, enabled=True):
    """Identity of the signed-in account, or a blank record explaining why not.

    Never raises: a missing, unreadable or unexpected credential file degrades the account
    panel to "unavailable" and leaves every other figure in the report untouched.
    """
    if not enabled:
        return blank('disabled with --no-account')
    path = auth_path(sessions_root)
    try:
        with open(path, 'rb') as fh:
            doc = json.loads(fh.read(8 << 20))
    except FileNotFoundError:
        return blank('no auth.json (not signed in, or a different CODEX_HOME)')
    except OSError as exc:
        return blank(f'auth.json unreadable: {exc.__class__.__name__}')
    except ValueError:
        return blank('auth.json is not valid JSON')
    if not isinstance(doc, dict):
        return blank('auth.json is not an object')

    out = blank()
    out['source'] = path
    out['auth_mode'] = doc.get('auth_mode') if isinstance(doc.get('auth_mode'), str) else None
    out['last_refresh'] = doc.get('last_refresh') if isinstance(doc.get('last_refresh'),
                                                                str) else None
    tokens = doc.get('tokens')
    tokens = tokens if isinstance(tokens, dict) else {}

    claims = _claims(tokens.get('id_token')) or {}

    def take(field, v):
        # Scalars only.  A claim that arrives as a dict or list is not an identity string,
        # and stringifying it would put an unexamined structure into the report.
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out[field] = str(v)

    for field, claim in PLAIN_CLAIMS.items():
        take(field, claims.get(claim))
    box = claims.get(NS)
    box = box if isinstance(box, dict) else {}
    for field, short in NS_CLAIMS.items():
        take(field, box.get(short, claims.get(f'{NS}/{short}')))

    # `tokens.account_id` is a plain identifier, not credential material, and is the only
    # identity available when the id_token is absent (API-key auth) or unparseable.
    if not out['account_id']:
        aid = tokens.get('account_id')
        if isinstance(aid, str):
            out['account_id'] = aid

    out['available'] = any(out[f] for f in ('email', 'name', 'account_id', 'plan'))
    if not out['available']:
        out['reason'] = ('API-key auth: auth.json carries no ChatGPT identity'
                         if out['auth_mode'] and 'key' in out['auth_mode'].lower()
                         else 'auth.json carried no identity claims')
    return out
