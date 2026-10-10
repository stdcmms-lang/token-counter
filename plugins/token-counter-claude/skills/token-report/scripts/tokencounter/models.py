"""Content-free Claude facts and the fixed diagnostic catalogue.

Facts are ordinary dictionaries so the disposable index can cache them verbatim. Local
identifiers establish ownership; account identity, message bodies and tool data do not
belong in captured facts. Counters have separate scopes and are not additive losses.
"""
from pathlib import Path
from typing import Dict, List, Literal, Optional, TypedDict

FACT_CODEC = 1
SourceKind = Literal['main', 'subagent']
TimestampQuality = Literal['original', 'copied', 'unknown']
LimitKind = Literal['session', 'weekly_all', 'weekly_scoped']
Effort = Literal['low', 'medium', 'high', 'xhigh', 'max', 'none']
Plan = Literal['claude:pro', 'claude:max-5x', 'claude:max-20x']

COUNTER_SCOPES = {
    'record': (
        'records_seen', 'complete_lines', 'unparseable_records', 'non_object_records',
        'malformed_records', 'trailing_partial_lines', 'unknown_record_types',
        'nonusage_records', 'extraction_errors', 'file_changed_during_read',
        'unexpected_jsonl_paths', 'outside_root_links', 'record_timestamp_invalid',
        'directory_links_skipped', 'extractor_fingerprint_unavailable',
    ),
    'response': (
        'assistant_records', 'blocks_collapsed', 'in_file_duplicate_blocks',
        'assistant_missing_usage', 'usage_required_missing', 'usage_required_type_invalid',
        'usage_required_negative', 'response_missing_identity', 'response_input_conflict',
        'response_output_decreased', 'response_terminal_usage_invalid',
        'response_usage_conflict', 'response_model_conflict', 'responses_excluded',
        'zero_usage_responses', 'synthetic_records', 'api_error_records',
        'partial_responses', 'max_tokens_responses', 'refusal_responses',
        'response_stop_reason_missing', 'model_name_invalid', 'block_index_invalid',
    ),
    'lineage': (
        'cross_file_response_copies', 'cross_file_block_copies',
        'zero_usage_placeholder_copies', 'continuation_links', 'continuation_cycles',
        'continuation_multiple_roots', 'fork_context_links', 'orphan_subagent_families',
        'unrelated_response_copies', 'response_owner_unavailable',
        'response_replay_timestamp_unverified', 'retained_removed_responses',
        'retained_removed_blocks', 'archived_sources', 'archived_responses', 'lineage_invalid',
        'lineage_link_copies', 'auxiliary_fact_copies',
    ),
    'measurement': (
        'reasoning_missing', 'reasoning_invalid', 'cache_ttl_missing', 'cache_ttl_invalid',
        'speed_missing', 'speed_unknown', 'inference_geo_missing', 'inference_geo_unknown',
        'web_search_count_missing', 'web_search_count_invalid',
        'web_fetch_count_missing', 'web_fetch_count_invalid', 'effort_missing',
        'effort_conflict', 'iterations_multiple', 'iterations_disagree',
        'fallback_credit_nonnull', 'advisor_model_records', 'compaction_boundaries',
        'compaction_usage_unavailable', 'cost_state_records', 'cost_state_unmatched_models',
        'cost_state_count_mismatches', 'usage_report_cost_crosschecks', 'cost_check_invalid',
        'optional_metadata_invalid', 'undated_responses', 'calendar_context_changed',
        'damage_outside_window',
        'composition_unknown_blocks', 'composition_unknown_attachments',
        'composition_unkeyed_items', 'composition_invalid_unicode',
        'composition_external_tool_results_unread', 'composition_snapshot_copies',
        'images_known_dimensions', 'images_unknown_dimensions', 'images_unknown_model',
        'images_unsupported_source', 'images_transformations_unknown',
        'prompt_growth_negative', 'prompt_growth_compaction_skipped',
        'prompt_growth_model_change_skipped',
    ),
    'pricing': (
        'unpriced_responses', 'unpriced_input', 'unpriced_output', 'unpriced_model',
        'unpriced_speed', 'unpriced_geography', 'unsupported_fast_setting',
        'speed_unrecorded', 'price_cache_ttl_assumed', 'price_geography_default_global',
        'search_count_unrecorded', 'price_search_failure_ambiguous',
        'price_partial_responses', 'price_table_unavailable', 'price_table_invalid',
    ),
    'timing': (
        'latency_replayed', 'latency_no_end', 'latency_no_start', 'latency_nonpositive',
        'latency_over_cap', 'latency_samples', 'tool_without_response', 'tool_no_time',
        'tool_replayed', 'tool_nonpositive', 'tool_over_cap', 'tool_calls_timed',
        'turn_no_start', 'turn_over_cap', 'latency_partial_response',
        'latency_copy_timestamp_unverified', 'latency_parent_chain_ambiguous',
        'latency_subagent_prompt_missing', 'latency_unknown_speed',
        'logged_turn_over_cap', 'latency_groups_omitted', 'latency_tier_groups_omitted',
    ),
    # These diagnostics are needed by extraction now. Attribution, clustering, timing
    # aggregation and state counters arrive with their implementing rounds.
    'observation': (
        'limit_reports', 'limit_readings', 'limit_reading_invalid', 'limit_reset_invalid',
        'limit_unknown_kind', 'limit_unknown_scope', 'limit_429_assumed_all_models',
        'limit_events', 'limit_event_copies', 'limit_event_missing_identity',
        'limit_reading_copies', 'limit_quote_conflict',
        'limit_event_invalid_timestamp', 'limit_event_unknown_kind', 'limit_events_undated',
        'account_tier_unrecognized', 'account_plan_unknown', 'account_plan_conflict',
        'account_fields_contradictory',
        'account_subscription_date_missing', 'account_unavailable', 'account_invalid',
        'account_field_invalid', 'logged_turn_invalid', 'logged_turn_unmatched',
        'tool_unmatched_call', 'tool_unmatched_result', 'tool_identity_conflict',
        'windows_unavailable',
    ),
    'history': (
        'history_unavailable', 'history_integrity_failed', 'history_commit_failed',
        'history_schema_unsupported', 'history_token_binding_mismatch',
        'history_conflicting_revisions', 'months_withheld',
        'months_withheld_missing_contributors', 'months_withheld_decreased_contributions',
        'months_withheld_calendar_change', 'months_withheld_schema_budget',
        'sessions_withheld_month', 'windows_withheld_retention',
        'windows_withheld_schema_budget', 'share_prepared', 'share_confirmed',
        'share_outcome_unknown',
    ),
}
COUNTER_NAMES = frozenset(name for names in COUNTER_SCOPES.values() for name in names)
REASONS = {name: name.replace('_', ' ') for name in COUNTER_NAMES}


def bump(counters, name, count=1):
    """A discarded component must have a fixed name, never exception text."""
    if name not in COUNTER_NAMES:
        raise ValueError('unknown counter name')
    if type(count) is not int or count < 0:
        raise ValueError('counter increment is not a nonnegative integer')
    if count:
        counters[name] = counters.get(name, 0) + count


class Usage(TypedDict):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: Optional[int]
    total_tokens: int


class Counts(TypedDict):
    responses: int
    input: int
    cached: int
    output: int
    reasoning: int


class ImageFact(TypedDict):
    width: Optional[int]
    height: Optional[int]
    source_kind: Literal['base64', 'url', 'file', 'unknown']
    transformations_known: bool
    model: Optional[str]
    estimated_visual_tokens: Optional[int]
    method: Optional[str]


class ContentFact(TypedDict):
    item_key: str
    family_id: Optional[str]
    category: str
    ts: Optional[str]
    utf8_bytes: Optional[int]
    body_digest: Optional[str]
    snapshot: bool
    image: Optional[ImageFact]


class RowPrice(TypedDict):
    tokens_usd: Optional[float]
    tokens_usd_high: Optional[float]
    web_search_usd: float
    web_search_calls: int
    unpriced_reason: Optional[str]
    counters: Dict[str, int]


class SourceInfo(TypedDict):
    source_id: str
    session_id: str                 # containing session, local only
    thread_id: str                  # main or agent identity, local only
    path: str                       # live cache attribute, local only
    kind: SourceKind
    first_record_ts: Optional[str]
    size: int
    mtime_ns: int
    prefix_hash: str
    line_count: int
    stable_read: bool
    last_complete_line: int


class ClaudeUsageFact(TypedDict):
    base_input: int
    creation: int
    reads: int
    output: int
    thinking: Optional[int]
    write_5m: Optional[int]
    write_1h: Optional[int]
    searches: Optional[int]
    fetches: Optional[int]
    speed: Optional[str]
    service_tier: Optional[str]
    inference_geo: Optional[str]
    iterations_count: Optional[int]
    iterations_agree: Optional[bool]
    valid_ttl: bool
    invalid_optional_fields: List[str]


class ToolStart(TypedDict):
    tool_key: str
    call_id: str
    stream_id: str
    name: str                      # local only
    ts: Optional[str]


class ToolResult(TypedDict):
    tool_key: str
    call_id: str
    stream_id: str
    ts: Optional[str]
    record_key: str
    parent_uuid: Optional[str]


class BlockFact(TypedDict):
    record_key: str
    uuid: Optional[str]
    parent_uuid: Optional[str]
    physical_line: int
    api_block_index: Optional[int]
    ts: Optional[str]
    usage: Optional[ClaudeUsageFact]
    content: List[ContentFact]
    tool_starts: List[ToolStart]
    # Freeze these content-free anchors at the first block, before a streaming tool
    # result can move them. They survive caching without retaining user records.
    req_ts: Optional[str]
    turn: Optional[int]
    response_metadata: Dict[str, object]  # safe per-record metadata for revision checks


class ResponseCopy(TypedDict):
    response_key: str
    source_id: str
    session_id: str
    agent_id: Optional[str]
    blocks: List[BlockFact]
    raw_model: Optional[str]
    requested_model: Optional[str]
    advisor_model: Optional[str]    # canonical id of a recorded advisorModel; metadata only
    effort: Optional[str]
    per_turn_effort: Optional[str]
    aborted: bool
    truncated: bool
    stop_reason: Optional[str]
    terminal_record_key: Optional[str]
    partial: bool
    timestamp_quality: TimestampQuality
    quality_flags: List[str]


class ResponseRow(TypedDict):
    response_key: str
    source_id: str
    family_id: Optional[str]
    stream_id: str
    stream: Literal['claude']
    index: int
    ts: Optional[str]
    req_ts: Optional[str]
    first_output_ts: Optional[str]
    timestamp_quality: TimestampQuality
    local_day: Optional[str]
    day_start: Optional[float]
    day_end: Optional[float]
    calendar_signature: str
    turn: Optional[int]
    model: str
    raw_model: Optional[str]
    requested_model: Optional[str]
    advisor_model: Optional[str]
    context_1m: bool
    effort: Optional[str]
    tier: Optional[str]
    speed: Optional[str]
    service_tier: Optional[str]
    inference_geo: Optional[str]
    base_input_tokens: int
    cache_creation_input_tokens: int
    cache_write_5m: Optional[int]
    cache_write_1h: Optional[int]
    cache_ttl_complete: bool
    web_search_requests: Optional[int]
    web_fetch_requests: Optional[int]
    usage: Usage
    partial: bool
    replayed: bool
    archived: bool                 # no live transcript copy; present when history is loaded
    plan: Optional[Plan]
    plan_source: Optional[Literal['account']]
    quality_flags: List[str]


class LineageLink(TypedDict):
    kind: Literal['continued', 'fork']
    from_session: str
    to_session: str
    agent_id: Optional[str]
    parent_last_uuid: Optional[str]
    source_id: str
    record_key: str


class LimitReading(TypedDict):
    reading_key: str
    source_id: str
    ts: str
    kind: LimitKind
    scope_key: Optional[str]
    percent: Optional[float]
    resets_at: float
    source: Literal['usage_report', 'quota_429']


class LimitEvent(TypedDict):
    event_key: str
    source_id: str
    ts: Optional[str]
    kind: Optional[str]
    resets_at: Optional[float]


class TurnFact(TypedDict):
    stream_id: str
    turn: int
    start: Optional[str]
    logged_duration_ms: Optional[int]
    logged_record_key: Optional[str]
    record_key: Optional[str]       # stable opener identity across shortened continuations


class CompactionFact(TypedDict):
    record_key: str
    ts: Optional[str]
    duration_ms: Optional[int]


class CostCheck(TypedDict):
    record_key: str
    source_id: str
    ts: Optional[str]
    source: Literal['cost_state', 'usage_report']
    model: str
    raw_model: str                 # safe normalized id as the map spelt it, e.g. with [1m]
    context_1m: bool
    cost_usd: Optional[float]
    cost_basis: Optional[str]
    counts: Dict[str, Optional[int]]


class AccountSnapshot(TypedDict):
    observed_at: float
    organization_type: Optional[str]
    rate_limit_tier: Optional[str]
    current_plan: Optional[Plan]
    subscription_created_at: Optional[float]


class AccountInfo(TypedDict):
    snapshot: Optional[AccountSnapshot]
    email: Optional[str]             # ephemeral, local only
    organization_name: Optional[str]  # ephemeral, local only
    skipped: bool
    counters: Dict[str, int]


class FileResult(SourceInfo):
    responses: List[ResponseCopy]
    links: List[LineageLink]
    limits: List[LimitReading]
    events: List[LimitEvent]
    content: List[ContentFact]
    tool_starts: List[ToolStart]
    tool_results: List[ToolResult]
    turns: List[TurnFact]
    compactions: List[CompactionFact]
    cost_checks: List[CostCheck]
    counters: Dict[str, int]
    metrics_only: bool
    fact_codec: int


class FamilySummary(TypedDict):
    family_id: str
    streams: List[str]
    start: Optional[str]
    end: Optional[str]
    local_day: Optional[str]
    active_s: int
    responses: int
    input: int
    cached: int
    output: int
    reasoning: int
    model: str
    orphan_main: bool


class ToolInterval(TypedDict):
    tool_key: str
    stream_id: str
    name: str                      # local only
    ts: Optional[str]
    end_ts: Optional[str]
    seconds: Optional[float]


class CoverageResult(TypedDict):
    history_available: bool
    history_committed: bool
    token_bound: bool
    calendar_completeness: Literal['unknown']
    captured_responses: int
    archived_responses: int
    safe_months: List[str]
    withheld_months: Dict[str, List[str]]
    windows_replace_safe: bool
    window_reasons: List[str]


class LedgerResult(TypedDict):
    rows: List[ResponseRow]
    by_stream: Dict[str, List[ResponseRow]]
    families: Dict[str, FamilySummary]
    limits: List[LimitReading]
    events: List[LimitEvent]
    content: List[ContentFact]
    tools: List[ToolInterval]
    turns: List[TurnFact]
    compactions: List[CompactionFact]
    cost_checks: List[CostCheck]
    account_snapshots: List[AccountSnapshot]
    counters: Dict[str, int]
    coverage: CoverageResult


class Paths(TypedDict):
    sessions_root: Path
    config_dir: Path
    account_path: Path
    state_dir: Path
    index_path: Path
    history_path: Path
    report_path: Path
    shared_report_path: Path
    share_state_path: Path
    explicit_sessions_root: bool


class ReportModel(TypedDict):
    schema: int
    client: str
    generated_at: str
    scope: dict
    totals: dict
    daily: List[dict]
    models: List[dict]
    sessions: List[dict]
    categories: List[dict]
    cat_series: List[list]
    cat_bucket_s: int
    rate_limits: dict
    limit_events: dict
    latency: dict
    api_value: dict
    quality: dict
    coverage: CoverageResult
    account: dict
    logged_turns: dict
    crosschecks: dict
    reconciliation: dict
    resend_cost: dict
    amplification: dict
    cache_leads: dict
