"""Eight-factor evidence profile. Missing data never becomes a zero or decision."""
from __future__ import annotations
import json
from pathlib import Path

_BANK = json.loads((Path(__file__).parent / 'config/question-bank.v1.json').read_text(encoding='utf-8-sig'))
FACTORS = [{k: f[k] for k in ('factor_id', 'dimension_id', 'name', 'dimension_name', 'definition', 'theoretical_sources')} for f in _BANK['factors']]
DIMENSIONS = _BANK['dimensions']


def factor_profile(report, reference=None):
    items = []
    targets = (reference or {}).get('targets', {})
    for factor in FACTORS:
        questions = [q for q in report.get('questions', []) if q['factor_id'] == factor['factor_id']]
        # There is no approved multi-question aggregation. Keep all source items.
        question = questions[0] if len(questions) == 1 else None
        score = question.get('score') if question and not question.get('review_stale') else None
        if type(score) is not int or not 1 <= score <= 5:
            score = None
        answer_ids = {a['answer_id'] for a in question.get('answers', [])} if question else set()
        jobs = [j for j in report.get('jobs', []) if question and (
            j.get('question_id') == question['question_id'] and j.get('job_type') == 'content'
            or j.get('job_type') == 'media' and j.get('answer_id') in answer_ids)]
        waiting = any(j.get('status') in ('queued', 'running') for j in jobs)
        unavailable = any(j.get('status') in ('failed', 'blocked') for j in jobs)
        state = ('not_measured' if not questions else 'aggregation_not_configured' if len(questions) > 1
                 else 'stale' if question.get('review_stale') else 'scored' if score is not None
                 else 'awaiting_answer' if not question.get('answers') else 'processing' if waiting else 'analysis_unavailable' if unavailable else 'insufficient_evidence')
        target = targets.get(factor['factor_id'])
        comparison = ('reference_not_set' if target is None else 'not_measured' if not questions
                      else 'insufficient_evidence' if score is None else 'at_or_above_reference' if score >= target else 'below_reference')
        items.append({**factor, 'score': score, 'state': state, 'score_source': question.get('score_source', 'unavailable') if question else 'unavailable',
                      'question_ids': [q['question_id'] for q in questions], 'reason': '内容分析尚未完成，暂不能判断证据是否充分。' if state == 'processing' else '自动内容分析暂不可用，保留原话供老师核对。' if state == 'analysis_unavailable' else question.get('reason') if question else '本轮题组没有测量此因子。',
                      'evidence': question.get('evidence', []) if question else [], 'anchors': question.get('anchors', {}) if question else {},
                      'target': target, 'comparison': comparison})
    return {'schema_version': 'factor-profile.v1', 'scale': {'minimum': 1, 'maximum': 5, 'status': 'proposed'},
            'report_version': report['version'], 'dimensions': DIMENSIONS, 'factors': items,
            'planned_count': sum(x['state'] != 'not_measured' for x in items), 'scored_count': sum(x['score'] is not None for x in items),
            'reference_profile': reference, 'usage': '模拟测试／方法验证；回答证据轮廓，不作录用或教学评价决定。',
            'dimension_aggregation': 'not_configured', 'missing_policy': 'null_not_zero'}


def report_extensions(db, session_id, report):
    """Called only when freezing a new report, never updates a cached report."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    reference = None
    appendix = None
    if 'reference_profiles' in tables:
        row = db.execute('SELECT payload FROM reference_profiles WHERE session_id=? ORDER BY version DESC LIMIT 1', (session_id,)).fetchone()
        reference = json.loads(row[0]) if row else None
    if 'research_publications' in tables:
        row = db.execute('SELECT payload FROM research_publications WHERE session_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (session_id,)).fetchone()
        appendix = json.loads(row[0]) if row else None
    if appendix:
        current = {a['answer_id']: a['transcript_revision'] for a in report.get('answers', []) if a.get('current')}
        appendix['input_stale'] = any(current.get(aid) != rev for aid, rev in appendix.get('input_versions', {}).items())
    return {'potential_profile': factor_profile(report, reference), 'research_appendix': appendix}
