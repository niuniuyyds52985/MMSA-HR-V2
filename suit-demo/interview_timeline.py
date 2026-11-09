"""Read-only projections from frozen answer revisions, media jobs and citations.

This module never opens media, runs ASR/models, writes database rows, or invents
word timestamps. All text offsets are Unicode code points in [start, end).
"""
from __future__ import annotations

import hashlib
import json
import math
import re

SCHEMA_VERSION = 'interview-timeline.v1'
ALIGNMENT_VERSION = 'exact-asr.v1'


def _id(prefix, *parts):
    raw = json.dumps(parts, ensure_ascii=False, separators=(',', ':'))
    return prefix + ':' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _window(start, end, duration):
    return (_number(start) and _number(end) and type(duration) is int
            and 0 <= start < end <= duration)


def _segment(answer_id, revision, text, start, end, timing=None):
    return {'segment_id': _id('segment', answer_id, revision, start, end, text[start:end]),
            'index': 0, 'text': text[start:end], 'char_start': start, 'char_end': end,
            'start_ms': timing[0] if timing else None, 'end_ms': timing[1] if timing else None,
            'alignment_status': 'aligned' if timing else 'unaligned',
            'timestamp_source': 'asr_segment' if timing else None,
            'precision': 'segment' if timing else None}


def _text_segments(answer_id, revision, text):
    segments = []
    for match in re.finditer(r'.*?(?:[。！？!?；;\n]+|$)', text, flags=re.DOTALL):
        if match.end() > match.start():
            segments.append(_segment(answer_id, revision, text, match.start(), match.end()))
    return segments


def _asr_source_revision(job, revisions):
    """Identify an ASR-owned version, never a later human edit with equal text."""
    result = job.get('result') or {}
    transcription = result.get('transcription') or {}
    if job.get('status') != 'succeeded' or transcription.get('status') not in ('succeeded', 'success', 'completed', 'ok'):
        return None
    if result.get('input_current_at_completion') is not True:
        return None
    if result.get('job_id') != job.get('id') or result.get('answer_id') != job.get('answer_id'):
        return None
    input_revision = job.get('input_revision')
    if type(input_revision) is not int or result.get('input_transcript_revision') != input_revision:
        return None
    if 'output_transcript_revision' in result:
        output_revision = result['output_transcript_revision']
        if type(output_revision) is not int or output_revision != input_revision + 1:
            return None
    else:
        output_revision = input_revision + 1
    original, produced = revisions.get(input_revision), revisions.get(output_revision)
    if not original or original['text'].strip() or not produced or produced.get('actor') != 'asr':
        return None
    if not isinstance(transcription.get('text'), str) or not transcription['text'].strip():
        return None
    if produced['text'] != transcription['text']:
        return None
    return output_revision


def _aligned_segments(answer_id, revision, text, asr_segments, duration):
    """Ordered ASR segments must exactly cover all non-whitespace source text.

    This disambiguates repeated sentences using the complete ordered ASR source;
    incomplete or mismatched segment text cannot borrow the first matching quote.
    """
    if not isinstance(asr_segments, list) or not asr_segments:
        return None
    segments, cursor, previous_end = [], 0, -1
    for item in asr_segments:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str) or not item['text']:
            return None
        while cursor < len(text) and text[cursor].isspace() and not text.startswith(item['text'], cursor):
            begin = cursor
            while cursor < len(text) and text[cursor].isspace() and not text.startswith(item['text'], cursor):
                cursor += 1
            segments.append(_segment(answer_id, revision, text, begin, cursor))
        if not text.startswith(item['text'], cursor):
            return None
        end = cursor + len(item['text'])
        start_ms, end_ms = item.get('start_ms'), item.get('end_ms')
        valid_time = _window(start_ms, end_ms, duration) and start_ms >= previous_end
        segments.append(_segment(answer_id, revision, text, cursor, end, (start_ms, end_ms) if valid_time else None))
        if valid_time:
            previous_end = end_ms
        cursor = end
    if text[cursor:].strip():
        return None
    if cursor < len(text):
        segments.append(_segment(answer_id, revision, text, cursor, len(text)))
    return segments


def _audio_observations(answer, jobs):
    empty = {'status': 'not_available', 'source_job_id': None, 'unit': 'dBFS', 'windows': [],
             'reason': '尚无可用的真实录音能量观测。', 'text_alignment': 'independent_of_transcript'}
    if not answer.get('path'):
        return {**empty, 'reason': '此回答没有提交原始媒体。'}
    # A media job belongs to exactly one immutable answer recording. Transcript
    # edits do not change its RMS, and must not relabel it as content evidence.
    succeeded = [j for j in jobs if j.get('status') == 'succeeded']
    if not succeeded:
        return empty
    job = succeeded[0]
    result = job.get('result') or {}
    audio = result.get('audio') or {}
    if audio.get('status') != 'ok':
        return {**empty, 'source_job_id': job['id'], 'reason': '原媒体任务没有可用的 RMS 观测；未生成替代数值。'}
    windows = []
    source_windows = audio.get('windows')
    invalid = not isinstance(source_windows, list)
    previous_end = -1
    for index, item in enumerate(source_windows if isinstance(source_windows, list) else []):
        if (not isinstance(item, dict) or not _window(item.get('start_ms'), item.get('end_ms'), answer.get('duration_ms'))
                or item['start_ms'] < previous_end or not _number(item.get('rms_dbfs'))):
            invalid = True
            continue
        previous_end = item['end_ms']
        windows.append({'observation_id': _id('audio', job['id'], index, item),
                        'start_ms': item['start_ms'], 'end_ms': item['end_ms'],
                        'rms_dbfs': item['rms_dbfs'], 'usage': 'observed_only'})
    if not windows or invalid:
        # A malformed measurement set is never silently advertised as complete.
        return {**empty, 'status': 'invalid', 'source_job_id': job['id'],
                'reason': 'RMS 观测存在空缺、非有限数、越界或重叠时间；未展示无法核验的观测集。'}
    return {**empty, 'status': 'available', 'source_job_id': job['id'], 'windows': windows,
            'reason': '原片录音能量观测，与转写文字版本独立；不作为内容评分依据。'}


def _occurrences(text, quote, segments):
    matches, cursor = [], 0
    while quote:
        begin = text.find(quote, cursor)
        if begin < 0:
            break
        end = begin + len(quote)
        touched = [s for s in segments if s['char_start'] < end and s['char_end'] > begin]
        meaningful = [s for s in touched if s['text'].strip()]
        aligned = bool(meaningful) and all(s['alignment_status'] == 'aligned' for s in meaningful)
        matches.append({'char_start': begin, 'char_end': end,
                        'segment_ids': [s['segment_id'] for s in touched],
                        # Quotes inside a sentence locate the whole measured ASR
                        # segment; they never gain fabricated word timestamps.
                        'start_ms': min(s['start_ms'] for s in meaningful) if aligned else None,
                        'end_ms': max(s['end_ms'] for s in meaningful) if aligned else None,
                        'alignment_status': 'aligned' if aligned else 'unaligned',
                        'precision': 'segment' if aligned else None})
        cursor = begin + 1
    return matches


def _evidence_occurrences(text, evidence, segments):
    """Respect v2 explicit positions instead of broadening repeated citations."""
    matches = _occurrences(text, evidence.get('quote', ''), segments)
    start, end, segment_id = evidence.get('char_start'), evidence.get('char_end'), evidence.get('segment_id')
    if start is not None or end is not None:
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            return []
        matches = [item for item in matches if item['char_start'] == start and item['char_end'] == end]
    if segment_id is not None:
        if not isinstance(segment_id, str):
            return []
        segment = next((item for item in segments if item['segment_id'] == segment_id), None)
        if segment is None:
            return []
        matches = [item for item in matches if segment['char_start'] <= item['char_start']
                   and item['char_end'] <= segment['char_end']]
    return matches


def build_timeline(snapshot):
    answer, selected = snapshot['answer'], snapshot['revision']
    revision, text = selected['version'], selected['text']
    revisions = {r['version']: r for r in snapshot['revisions']}
    jobs = snapshot['jobs']
    has_media = bool(answer.get('path'))
    alignment = {'status': 'unaligned' if has_media else 'no_media', 'method': None,
                 'version': ALIGNMENT_VERSION, 'source_job_id': None, 'asr_transcript_revision': None,
                 'reason': '该转写版本尚无可核验的 ASR 句段时间对应。' if has_media else '纯文字回答没有可播放的媒体时间。'}
    segments = _text_segments(answer['id'], revision, text)
    sources = [(j, _asr_source_revision(j, revisions)) for j in jobs]
    alignment['available_asr_revisions'] = sorted({r for _, r in sources if r is not None})
    candidates = [(j, source_revision) for j, source_revision in sources if source_revision == revision]
    if has_media and len(candidates) == 1:
        job, source_revision = candidates[0]
        projected = _aligned_segments(answer['id'], revision, text,
                                      (job.get('result') or {}).get('transcription', {}).get('segments'), answer.get('duration_ms'))
        alignment.update(source_job_id=job['id'], asr_transcript_revision=source_revision)
        if projected is not None:
            segments = projected
            meaningful = [s for s in segments if s['text'].strip()]
            count = sum(s['alignment_status'] == 'aligned' for s in meaningful)
            status = 'aligned' if count and count == len(meaningful) else 'partial' if count else 'unaligned'
            alignment.update(status=status, method='exact_asr_segments',
                             reason='仅使用该 ASR 源版本的真实句段时间；定位精度为句段。' if status == 'aligned'
                             else '部分 ASR 句段时间无效、越界或重叠；冲突句段未采用时间值。' if status == 'partial'
                             else 'ASR 句段时间无效或超出原片时长，未采用时间值。')
        else:
            alignment['reason'] = 'ASR 句段不能精确覆盖此版本原文；保留文字，未猜测媒体位置。'
    elif has_media and len(candidates) > 1:
        alignment['reason'] = '此版本存在多个 ASR 来源，未静默选择某次结果。'
    elif has_media and any(source_revision is not None for _, source_revision in sources):
        alignment.update(status='stale', reason='已有 ASR 时间属于其他转写版本；本版未继承旧时间，需人工核对或重新对齐。')
    for index, segment in enumerate(segments):
        segment['index'] = index
    references = []
    for source in snapshot['sources']:
        for index, evidence in enumerate(source.get('evidence') or []):
            if evidence.get('answer_id') != answer['id'] or evidence.get('transcript_revision') != revision:
                continue
            quote = evidence.get('quote')
            if not isinstance(quote, str) or not quote.strip():
                continue
            occurrences = _evidence_occurrences(text, evidence, segments)
            references.append({'reference_id': _id('reference', source['source'], source['source_id'], index, answer['id'], revision, quote),
                               'evidence_id': evidence.get('evidence_id'), 'source': source['source'],
                               'source_id': source['source_id'], 'question_id': answer.get('parent_question_id') or answer['question_id'],
                               'quote': quote, 'transcript_revision': revision, 'reason': source.get('reason'),
                               'match_status': 'unique' if len(occurrences) == 1 else 'ambiguous' if occurrences else 'not_found',
                               'occurrences': occurrences,
                               **{key: evidence[key] for key in ('segment_id', 'char_start', 'char_end') if evidence.get(key) is not None}})
    return {'schema_version': SCHEMA_VERSION, 'session_id': answer['session_id'], 'answer_id': answer['id'],
            'question_id': answer['question_id'], 'parent_question_id': answer.get('parent_question_id'),
            'transcript_revision': revision, 'current_transcript_revision': answer['revision'],
            'is_current_revision': revision == answer['revision'], 'answer_current': bool(answer['current']),
            'text': text, 'media': {'available': has_media, 'availability_basis': 'recorded_attachment',
                                  'media_sha256': answer.get('media_hash'), 'duration_ms': answer.get('duration_ms') if has_media else None,
                                  'url': f"/v1/interviews/{answer['session_id']}/media/{answer['id']}" if has_media else None},
            'alignment': alignment, 'segments': segments, 'observations': {'audio': _audio_observations(answer, jobs)},
            'reference_scope': 'all_records_for_requested_transcript_revision', 'references': references,
            'notices': ['字符位置采用 Unicode 码点半开区间；时间相对本回答原片起点。',
                        '重复引用保留所有匹配位置，不能自动把第一处当作唯一出处。',
                        '引用集合包含不同报告或决策时间的同版本记录；不代表某一次历史决策实际使用的全部依据。历史报告应按其已冻结引用定位。',
                        '句段时间不代表词级精确对齐；媒体质量和声音能量不决定评分。']}
