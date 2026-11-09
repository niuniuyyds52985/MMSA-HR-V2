"""Small, safe diagnostics shared by providers, workers and report projection."""
from __future__ import annotations

ERROR_CODES = frozenset({
    'transport_error', 'connect_timeout', 'read_timeout', 'connection_error',
    'tls_error', 'proxy_error', 'api_http_error', 'invalid_response',
    'oversized_response', 'incomplete_response', 'content_error',
    'worker_timeout', 'worker_exit', 'not_configured', 'credential_error',
    'invalid_configuration', 'endpoint_mismatch', 'no_transcript',
    'invalid_segments', 'missing_criteria', 'invalid_schema', 'invalid_reason',
    'invalid_evidence', 'stale_evidence', 'invented_quote', 'invalid_score',
    'invalid_followup', 'invalid_branch', 'invalid_segment', 'invalid_offsets',
    'ambiguous_quote', 'invalid_evidence_units', 'invalid_criterion',
    'invalid_gap', 'invalid_gap_branch', 'missing_prerequisite_evidence',
    'invalid_criterion_judgment', 'invalid_routing', 'routing_timeout', 'routing_http_error', 'routing_unavailable',
})


def safe_error_code(code):
    return code if isinstance(code, str) and code in ERROR_CODES else 'content_error'


def safe_diagnostics(value):
    """Never persist exception text, URLs, headers, request bodies or credentials."""
    if not isinstance(value, dict):
        return {}
    output = {}
    for key, maximum in (('elapsed_ms', 3600000), ('connect_timeout_seconds', 60),
                         ('read_timeout_seconds', 300), ('attempt_count', 10),
                         ('http_status', 599)):
        item = value.get(key)
        if type(item) in (int, float) and 0 <= item <= maximum:
            output[key] = item
    events = []
    for item in (value.get('attempt_events') or [])[:4] if isinstance(value.get('attempt_events'), list) else []:
        if not isinstance(item, dict):
            continue
        event = {'code': safe_error_code(item.get('code'))}
        for key in ('attempt', 'elapsed_ms'):
            number = item.get(key)
            if type(number) in (int, float) and 0 <= number <= (10 if key == 'attempt' else 3600000):
                event[key] = number
        events.append(event)
    if events:
        output['attempt_events'] = events
    return output
