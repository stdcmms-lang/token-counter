"""Allow-listed account metadata from the resolved .claude.json.

This opens one file, read-only. Identity is ephemeral and local; only plan fields and the
parsed subscription date enter a snapshot. Credentials, settings and caches are not account
sources. An explicit corpus never borrows the live account, and --no-account opens nothing.
"""
import json
import time

from .models import AccountInfo, bump
from .worker import epoch

ACCOUNT_FIELDS = ('organizationType', 'organizationRateLimitTier', 'subscriptionCreatedAt',
                  'emailAddress', 'organizationName')
MAX_TIERS = {'default_claude_max_5x': 'claude:max-5x',
             'default_claude_max_20x': 'claude:max-20x'}


def map_plan(organization_type, rate_limit_tier) -> tuple:
    counters, plan = {}, None
    if organization_type == 'claude_max':
        plan = MAX_TIERS.get(rate_limit_tier) if isinstance(rate_limit_tier, str) else None
        if rate_limit_tier and plan is None:
            bump(counters, 'account_tier_unrecognized')
    elif organization_type == 'claude_pro':
        if isinstance(rate_limit_tier, str) and rate_limit_tier in MAX_TIERS:
            # Pro type with a Max tier: contradictory fields, no plan. Distinct from
            # account_plan_conflict, which Q5 counts between two known plans.
            bump(counters, 'account_fields_contradictory')
        else:
            plan = 'claude:pro'
            if rate_limit_tier:
                bump(counters, 'account_tier_unrecognized')
    if plan is None:
        bump(counters, 'account_plan_unknown')
    return plan, counters


def read_account(paths, *, no_account=False, now=None) -> AccountInfo:
    out = {'snapshot': None, 'email': None, 'organization_name': None,
           'skipped': no_account, 'counters': {}}
    if no_account:
        return out
    counters = out['counters']
    try:
        with open(paths['account_path'], 'rb') as fh:
            doc = json.load(fh)
    except OSError:
        bump(counters, 'account_unavailable')
        return out
    except (ValueError, UnicodeError):
        bump(counters, 'account_invalid')
        return out
    if not isinstance(doc, dict):
        bump(counters, 'account_invalid')
        return out
    box, values = doc.get('oauthAccount'), {}
    if box is None:
        # A well-formed file with no signed-in account (API-key use, or signed out).
        bump(counters, 'account_unavailable')
        return out
    if not isinstance(box, dict):
        bump(counters, 'account_invalid')
        return out
    for field in ACCOUNT_FIELDS:
        value = box.get(field)
        if value is not None and not isinstance(value, str):
            bump(counters, 'account_field_invalid')
            value = None
        values[field] = value
    plan, mapping = map_plan(values['organizationType'], values['organizationRateLimitTier'])
    counters.update(mapping)
    created = epoch(values['subscriptionCreatedAt'])
    if created is None:
        bump(counters, 'account_subscription_date_missing')
    out['snapshot'] = {
        'observed_at': time.time() if now is None else now,
        'organization_type': values['organizationType'],
        'rate_limit_tier': values['organizationRateLimitTier'], 'current_plan': plan,
        'subscription_created_at': created,
    }
    out['organization_name'] = values['organizationName']
    out['email'] = values['emailAddress'] or values['organizationName']
    return out
