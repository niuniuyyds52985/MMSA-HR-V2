"""Deterministic text preparation and a separately labelled lexical baseline.

No network, model, database, media access or score/follow-up mutation occurs here.
All offsets use Unicode code points in the exact immutable transcript version.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from interview_timeline import _id, _text_segments
from interview_text_normalization import normalize_for_match, normalization_metadata

CONFIG_PATH = Path(__file__).with_name('config') / 'text-baseline.v1.json'
BASELINE_SCHEMA = 'text-baseline-result.v1'
CRITERIA_VERSION = 'question-criteria.zh.v1'

_CRITERIA = {
    'leadership_F3_Q1': [
        ('initiative', '主动承担与动机', '是否说明主动承担的具体情境及团队目标'),
        ('coordination', '本人协调行动', '是否说明本人分工、沟通、跟进或兜底的具体动作'),
        ('outcome_cost', '结果与承担代价', '是否交代结果、取舍、困难及承担责任的代价'),
        ('reflection', '复盘与调整', '是否说明经历后的复盘或调整')],
    'results_K6': [
        ('goal_constraint', '目标与资源约束', '是否结合剩余目标、两周期限和预算不增加的约束'),
        ('bottleneck_options', '瓶颈与替代路径', '是否分析瓶颈并提出资源内的具体替代行动'),
        ('plan_checkpoints', '安排与检查点', '是否说明优先级、行动顺序和进度检查点'),
        ('adjustment_risk', '验证与调整条件', '是否说明路径验证、调整条件、风险及备用路径')],
    'learning_F10_Q2': [
        ('experience', '具体失败经历', '是否说明具体经历、原有做法和实际结果'),
        ('method', '提炼的方法', '是否从经历提炼可解释的方法或规律'),
        ('transfer', '不同情境的迁移', '是否说明在另一任务中的适配与本人行动'),
        ('effect_boundary', '效果与适用条件', '是否说明迁移效果、适用条件或不适用情境')]
}


def criteria_for_question(question):
    """Fixed draft criteria grounded in the existing question/anchor wording."""
    if question.get('criteria'):
        import copy
        return copy.deepcopy(question['criteria'])
    qid = question['question_id']
    values = _CRITERIA.get(qid)
    if values is None:
        # Tests and future questions keep their actual configured anchors; never
        # import a different factor's criteria merely because the IDs are new.
        values = [('anchor_' + str(n), str(n) + '分锚点', text)
                  for n, text in sorted(question.get('anchors', {}).items())]
    return [{'criterion_id': qid + ':' + suffix, 'name': name,
             'description': description, 'version': CRITERIA_VERSION,
             'status': 'proposed', 'source': 'configured_question_and_anchors'}
            for suffix, name, description in values]


def question_context(question):
    """Attach source-backed factor definitions without adding score dimensions."""
    bank = json.loads(CONFIG_PATH.with_name('question-bank.v1.json').read_text(encoding='utf-8'))
    factor = next((f for f in bank['factors'] if f['factor_id'] == question.get('factor_id')), None)
    source_cell = question.get('source_cell', '')
    question_type = 'situational' if source_cell.startswith('K') else 'behavioral' if source_cell.startswith('F') else 'unspecified'
    result = {**question, 'question_type': question_type}
    if factor:
        result['factor_context'] = {k: factor[k] for k in
            ('factor_id', 'name', 'definition', 'assessment_content', 'source_sheet',
             'definition_source_cell', 'assessment_content_source_cell')}
        result['factor_context']['bank_version'] = bank['bank_version']
        result['factor_context']['status'] = 'source_reference_not_validated_measurement'
    return result


def prepare_text_answers(answers):
    """Preserve server timeline segments; otherwise use its text-only fallback.

    Returns only content input fields. Timestamps, biometrics, permissions and
    arbitrary caller metadata are deliberately absent from cloud inputs.
    """
    prepared, ids = [], set()
    for answer in answers:
        aid, revision, text = (answer.get('answer_id'), answer.get('transcript_revision'),
                               answer.get('transcript'))
        if not isinstance(aid, str) or not aid or aid in ids or type(revision) is not int or revision < 1 or not isinstance(text, str):
            raise ValueError('回答标识、版本或原文无效。')
        ids.add(aid)
        source = answer.get('segments')
        if source is None:
            source = _text_segments(aid, revision, text)
        if not isinstance(source, list):
            raise ValueError('原话句段无效。')
        segments, cursor = [], 0
        for segment in source:
            if not isinstance(segment, dict):
                raise ValueError('原话句段无效。')
            start, end = segment.get('char_start'), segment.get('char_end')
            if type(start) is not int or type(end) is not int or start != cursor or not start < end <= len(text):
                raise ValueError('句段字符区间必须连续覆盖原文。')
            part = text[start:end]
            expected_id = _id('segment', aid, revision, start, end, part)
            if segment.get('text') != part or segment.get('segment_id') != expected_id:
                raise ValueError('句段不属于所指定的原文版本。')
            segments.append({'segment_id': expected_id, 'text': part, 'char_start': start, 'char_end': end})
            cursor = end
        if cursor != len(text):
            raise ValueError('句段没有覆盖完整原文。')
        prepared.append({'answer_id': aid, 'transcript_revision': revision,
                         'transcript': text, 'parent_question_id': answer.get('parent_question_id'),
                         'segments': segments})
    return prepared


def _load_config(config):
    if config is None:
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    if not isinstance(config, dict) or config.get('schema_version') != 'text-baseline-config.v1':
        raise ValueError('词频配置版本不受支持。')
    return config


def _occurrences(text, phrase):
    cursor = 0
    while True:
        start = text.find(phrase, cursor)
        if start < 0:
            return
        end = start + len(phrase)
        yield start, end
        cursor = end


def _context(text, start, end, rules):
    delimiters = rules['clause_delimiters']
    left = max((text.rfind(d, 0, start) for d in delimiters), default=-1) + 1
    stops = [text.find(d, end) for d in delimiters]
    right = min((p for p in stops if p >= 0), default=len(text))
    prefix = text[max(left, start - rules['negation_prefix_codepoints']):start]
    negators = [term for term in rules['negators'] if term in prefix]
    clause = text[left:right]
    reporters = [term for term in rules['reporters'] if term in text[left:start]]
    quoted = False
    for opening, closing in rules['quoted_pairs']:
        if opening == closing:
            quoted = quoted or text[:start].count(opening) % 2 == 1 and closing in text[end:]
        else:
            begin = text.rfind(opening, 0, start + 1)
            close_before = text.rfind(closing, 0, start)
            quoted = quoted or begin >= 0 and begin > close_before and text.find(closing, end) >= 0
    # Time is a discourse marker, not a claim about when an event truly happened.
    # Sentence punctuation resets it; commas retain an earlier marker until a
    # later marker explicitly replaces it.
    sentence_start = max((text.rfind(d, 0, start) for d in '。.!！?？;；\n'), default=-1) + 1
    before = text[sentence_start:start]
    markers = [(before.rfind(term), label, term) for label, terms in
               [('past', rules['past_markers']), ('later', rules['later_markers'])] for term in terms]
    position, temporal, marker = max(markers, default=(-1, 'unspecified', None))
    if position < 0:
        temporal, marker = 'unspecified', None
    flags = []
    if negators:
        flags.append('negation_cue')
    if reporters:
        flags.append('reported_speech_cue')
    if quoted:
        flags.append('quoted_text')
    if temporal != 'unspecified':
        flags.append(temporal + '_cue')
    # This is deliberately conservative lexical filtering, never a positive or
    # negative capability judgment. We retain all raw hits alongside it.
    retained = not (negators or reporters or quoted or temporal == 'past')
    return {'context': clause, 'context_char_start': left, 'context_char_end': right,
            'flags': flags, 'negation_cues': negators, 'reporting_cues': reporters,
            'temporal_context': temporal, 'temporal_marker': marker,
            'retained_by_context_rule': retained}


def run_text_baseline(answers, question, config=None):
    """Run the fixed lexical comparator for one parent question and its answers.

    `answers` needs answer_id/transcript_revision/transcript and may include the
    parent's follow-up; `question` needs question_id and factor_id. The returned
    JSON is standalone, versioned and suitable for an immutable research run.
    It intentionally has no score, recommendation or follow-up decision field.
    """
    config = _load_config(config)
    prepared = prepare_text_answers(answers)
    factor_id = question.get('factor_id')
    factor = next((f for f in config['factors'] if f['factor_id'] == factor_id), None)
    if factor is None:
        raise ValueError('题目因子没有已配置的来源词表。')
    config_digest = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True,
                                             separators=(',', ':')).encode('utf-8')).hexdigest()
    # Simplified shadows retain exact original Unicode-codepoint offsets.
    shadows = {a['answer_id']: normalize_for_match(a['transcript']) for a in prepared}
    text_length = sum(len(a['transcript']) for a in prepared)
    denominator = sum(sum(c.isalnum() for c in a['transcript']) for a in prepared)
    matches, terms, totals = [], [], {'positive_prompt': 0, 'risk_prompt': 0}
    for term in factor['terms']:
        phrase = term['phrase']
        if not isinstance(phrase, str) or not phrase:
            raise ValueError('词表包含空词语。')
        raw, retained, present_answers = 0, 0, set()
        for answer in prepared:
            shadow = shadows[answer['answer_id']]
            for start, end in _occurrences(shadow, normalize_for_match(phrase)):
                raw += 1
                present_answers.add(answer['answer_id'])
                context = _context(shadow, start, end, config['context_rules'])
                context['context'] = answer['transcript'][context['context_char_start']:context['context_char_end']]
                retained += int(context['retained_by_context_rule'])
                overlapping = [s['segment_id'] for s in answer['segments']
                               if start < s['char_end'] and end > s['char_start']]
                matches.append({'term_id': term['term_id'], 'phrase': phrase,
                    'source_label': term['source_label'], 'source_cell': term['source_cell'],
                    'answer_id': answer['answer_id'], 'transcript_revision': answer['transcript_revision'],
                    'char_start': start, 'char_end': end, 'segment_ids': overlapping, **context})
        totals[term['source_label']] += raw
        terms.append({**term, 'raw_count': raw, 'context_retained_count': retained,
                      'answer_presence_count': len(present_answers),
                      'per_100_alphanumeric_codepoints': 100 * raw / denominator if denominator else None})
    return {'schema_version': BASELINE_SCHEMA, 'algorithm_version': config['algorithm_version'] + '+script-t2s.v1',
            'text_normalization': normalization_metadata(),
            'config_sha256': config_digest, 'context_rules_version': config['context_rules']['version'],
            'question_id': question['question_id'], 'factor_id': factor_id,
            'input_versions': {a['answer_id']: a['transcript_revision'] for a in prepared},
            'usage': 'research_comparator_only', 'source': config['source'],
            'matching': {**config['matching'], 'normalization': normalization_metadata()['version'],
                         'method': 'case-sensitive exact phrase in simplified shadow; non-overlapping per term; original codepoint offsets'}, 'denominator': {
                'kind': 'unicode_alphanumeric_codepoints', 'value': denominator,
                'all_codepoints': text_length, 'normalization_scale': 100},
            'raw_counts_by_source_label': totals,
            'context_retained_total': sum(t['context_retained_count'] for t in terms),
            'answer_presence_total': sum(t['answer_presence_count'] for t in terms),
            'terms': terms, 'matches': matches, 'limitations': config['limitations']}
