"""Immutable manual time mappings for one exact transcript revision.

This module does not mutate text, ratings, decisions, or its input objects.
Each review is a full explicit mapping snapshot; omitted segments are untimed.
"""
from __future__ import annotations
import copy
import hashlib
import json

VERSION = 'manual-alignment.v1'


class AlignmentError(ValueError):
    pass


def text_digest(timeline):
    return hashlib.sha256(timeline['text'].encode('utf-8')).hexdigest()


def segment_digest(timeline):
    value = [{k: item[k] for k in ('segment_id', 'char_start', 'char_end', 'text')}
             for item in timeline['segments']]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def validate_mapping(timeline, mappings):
    media = timeline.get('media') or {}
    duration = media.get('duration_ms')
    if not media.get('available') or type(duration) is not int or not 0 < duration <= 180000 or not media.get('media_sha256'):
        raise AlignmentError('此回答没有可校准的原始媒体或有效时长。')
    if not isinstance(mappings, list) or not 1 <= len(mappings) <= 2000:
        raise AlignmentError('请提供至少一个句段的时间映射。')
    indexed = {item['segment_id']: item for item in timeline['segments'] if item['text'].strip()}
    if len(indexed) != sum(bool(item['text'].strip()) for item in timeline['segments']):
        raise AlignmentError('时间线句段标识存在冲突。')
    normalized, seen = [], set()
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise AlignmentError('时间映射格式不合法。')
        sid = mapping.get('segment_id')
        if not isinstance(sid, str) or sid not in indexed or sid in seen:
            raise AlignmentError('句段须属于指定转写版本，且不能重复或只含空白。')
        start, end = mapping.get('start_ms'), mapping.get('end_ms')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= duration:
            raise AlignmentError('句段时间须为原片时长内的整数毫秒，且结束晚于开始。')
        source = indexed[sid]
        normalized.append({'segment_id': sid, 'start_ms': start, 'end_ms': end,
                           'char_start': source['char_start'], 'char_end': source['char_end']})
        seen.add(sid)
    normalized.sort(key=lambda item: (item['char_start'], item['char_end']))
    previous = -1
    for item in normalized:
        if item['start_ms'] < previous:
            raise AlignmentError('按原文顺序，句段时间不能倒退或重叠。')
        previous = item['end_ms']
    return normalized


def overlay(timeline, review):
    """Apply a stored payload (or {payload: payload}) to the same frozen input."""
    if review is None:
        return copy.deepcopy(timeline)
    payload = review.get('payload', review)
    if not isinstance(payload, dict) or payload.get('schema_version') != VERSION:
        raise AlignmentError('人工校准记录版本不支持。')
    for field in ('session_id', 'answer_id', 'transcript_revision'):
        if payload.get(field) != timeline.get(field):
            raise AlignmentError('人工校准记录不属于此回答和转写版本。')
    media = timeline.get('media') or {}
    if (payload.get('media_sha256') != media.get('media_sha256') or payload.get('duration_ms') != media.get('duration_ms')
            or payload.get('text_sha256') != text_digest(timeline) or payload.get('segments_sha256') != segment_digest(timeline)):
        raise AlignmentError('人工校准的原文、句段或原片依据已不匹配。')
    normalized = validate_mapping(timeline, payload.get('segments'))
    result = copy.deepcopy(timeline)
    times = {item['segment_id']: item for item in normalized}
    for segment in result['segments']:
        mapping = times.get(segment['segment_id'])
        segment.update(start_ms=mapping['start_ms'] if mapping else None,
                       end_ms=mapping['end_ms'] if mapping else None,
                       alignment_status='aligned' if mapping else 'unaligned',
                       timestamp_source='human_review' if mapping else None,
                       precision='segment' if mapping else None)
        if mapping:
            segment['alignment_review_id'] = payload['alignment_review_id']
    meaningful = [item for item in result['segments'] if item['text'].strip()]
    complete = len(times) == len(meaningful)
    result['alignment'].update(status='aligned' if complete else 'partial', method='human_review', version=VERSION,
        source_job_id=None, alignment_review_id=payload['alignment_review_id'], review_version=payload['version'],
        reviewer=payload['reviewer'], reviewed_at=payload['created_at'], review_reason=payload['reason'],
        reason='人工核准的句段时间，仅对应本回答本转写版本。' if complete else '部分句段有人工时间；未提交的句段保持未对齐。')
    result['alignment_review'] = copy.deepcopy(payload)
    from interview_timeline import _evidence_occurrences
    for reference in result['references']:
        occurrences = _evidence_occurrences(result['text'], reference, result['segments'])
        reference.update(occurrences=occurrences,
                         match_status='unique' if len(occurrences) == 1 else 'ambiguous' if occurrences else 'not_found')
    result['notices'].append('人工校准只修改回放定位，不修改原话、内容评分、面试版本或已封存决策；时间精度仍为句段。')
    return result
