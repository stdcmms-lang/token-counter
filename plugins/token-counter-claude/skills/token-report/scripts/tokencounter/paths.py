"""Claude corpus, account and state locations.

An explicit corpus has its own account lookup and state namespace. A copied corpus must
never fall back to the live account, and installed plugin directories hold no state.
"""
import hashlib
import os
from pathlib import Path

from .models import Paths


def state_namespace(root) -> str:
    normalized = os.path.normcase(str(Path(root).expanduser().resolve()))
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:12]


def is_within(path, root) -> bool:
    try:
        normalized = os.path.normcase(str(Path(path).resolve()))
        boundary = os.path.normcase(str(Path(root).resolve()))
        return os.path.commonpath((normalized, boundary)) == boundary
    except (OSError, ValueError, RuntimeError):
        return False


def resolve_paths(sessions_root=None, *, env=None, home=None) -> Paths:
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home).expanduser()
    configured = env.get('CLAUDE_CONFIG_DIR')
    config = Path(configured).expanduser() if configured else home / '.claude'
    config = config.resolve()
    explicit = sessions_root is not None
    if explicit:
        root = Path(sessions_root).expanduser().resolve()
        config = root.parent
        account = config / '.claude.json'
        state = config / 'token-counter' / state_namespace(root)
    else:
        root = config / 'projects'
        account = config / '.claude.json' if configured else home.resolve() / '.claude.json'
        state = config / 'token-counter'
    return {
        'sessions_root': root, 'config_dir': config, 'account_path': account,
        'state_dir': state, 'index_path': state / 'index-cache.db',
        'history_path': state / 'history.db', 'report_path': state / 'report.html',
        'shared_report_path': state / 'report-shared.html',
        'share_state_path': state / 'claude-share.json', 'explicit_sessions_root': explicit,
    }
