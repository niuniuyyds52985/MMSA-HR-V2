"""Fast, auditable question routing. Lexical coverage is not capability scoring."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import time
from interview_text_research import run_text_baseline, prepare_text_answers, _context, _load_config
from interview_text_normalization import normalize_for_match, normalization_metadata

POLICY_PATH = Path(__file__).with_name('config') / 'quick-followup.v1.json'
EVIDENCE_POLICY_PATH = POLICY_PATH.with_name('followup-evidence-policy.v1.json')
GUARD_VERSION = 'coverage-guards.v2.script-t2s'
GUARD_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
ACTION_TERMS = ('分工', '分配', '沟通', '协调', '组织', '安排', '跟进', '召集', '负责', '核对', '汇总', '拆分', '制定')
CUE_TERMS = (*ACTION_TERMS, '最终', '最后', '结果', '因此', '使得', '完成', '达到', '提高', '提升', '降低',
    '减少', '通过', '交付', '失败', '延期', '成功', '获得', '增长', '下降', '困难', '难点', '冲突', '分歧',
    '争执', '阻力', '不同意', '意见不一致', '最难', '总结', '提炼', '学到', '学会', '方法', '规律', '经验', '教训')


def _concrete_residue(text):
    # An explicit anti-stuffing check, not a claim that the remainder is true.
    value = re.sub('|'.join(re.escape(x) for x in sorted(CUE_TERMS, key=len, reverse=True)), '', text)
    value = re.sub(r'本人|我|主动|积极|认真|有效|顺利|合理|及时|非常|特别|具体|主要|负责|以及|然后|或者|并且|的|了|是|出|把|将|与|跟|和|并|要|会|能|所', '', value)
    return sum(ch.isalnum() for ch in value)


def _guard_context(text, match, context, *, own_action=False, method=False):
    quote = match.group()
    flags = list(context['flags'])
    # Behavioural questions explicitly ask about past events. A past marker
    # alone must not discard a concrete action as the comparator does.
    retained = not any(flag in flags for flag in ('negation_cue', 'reported_speech_cue', 'quoted_text'))
    if own_action or method:
        action = re.search('|'.join(ACTION_TERMS if own_action else ('总结', '提炼', '学到', '学会', '方法', '规律')), quote)
        prefix = quote[:action.start()] if action else quote
        if re.match(r'(?:我(?:们|的)|本人(?:的))', quote):
            flags.append('not_explicit_self_actor')
            retained = False
        if re.search(r'看到|看见|听到|听见|听说|知道|认为|觉得|相信|表示|提到|(?:我|本人)说', prefix):
            flags.append('observed_or_reported_other_action')
            retained = False
        if re.search(r'没有|没能|没|不曾|未曾|未能|并非|不是|未|不(?!但|仅|只|光|止)', prefix):
            flags.append('negated_action_prefix')
            retained = False
        if re.match(r'(?:我|本人)(?:只是)?(?:会|要|应该|计划|打算|希望|愿意|可以|能够|应当)', prefix):
            flags.append('intended_action_not_event_cue')
            retained = False
    if method:
        # The bare "first ... then ..." method pattern can begin inside an
        # account of somebody else's method; inspect its preceding clause too.
        before = text[context['context_char_start']:match.start()] + quote
        if re.search(r'(?:我|本人)(?:看到|看见|听到|听见|听说|知道|认为|觉得|相信)', before) or re.search(
                r'(?:同学|别人|组长|同事|老师|朋友|他们|她们|他|她)[^。！？；，\n]{0,6}(?:总结|提炼|提出|建议|说)', before):
            flags.append('reported_or_other_method')
            retained = False
    distinct_cues = sum(term in quote for term in CUE_TERMS)
    if _concrete_residue(quote) < 3 and (own_action or method or distinct_cues >= 3):
        flags.append('keyword_stuffing_or_no_concrete_complement')
        retained = False
    return {**context, 'flags': flags, 'retained_by_context_rule': retained,
            'guard_version': GUARD_VERSION, 'concrete_residue_codepoints': _concrete_residue(quote)}


def _coverage_hits(prepared, patterns, context_rules, *, own_action=False, method=False):
    hits = []
    for answer in prepared:
        shadow = normalize_for_match(answer['transcript'])
        for pattern in patterns:
            for match in re.finditer(pattern, shadow):
                context = _guard_context(shadow, match,
                    _context(shadow, match.start(), match.end(), context_rules),
                    own_action=own_action, method=method)
                if method and re.search(r'没有|没能|不曾|未曾|未能|并非|不是', match.group().split('方法')[0]):
                    context = {**context, 'retained_by_context_rule': False,
                        'flags': context['flags'] + ['negated_method_prefix']}
                context['context'] = answer['transcript'][context['context_char_start']:context['context_char_end']]
                hits.append({'answer_id': answer['answer_id'], 'transcript_revision': answer['transcript_revision'],
                    'char_start': match.start(), 'char_end': match.end(), 'quote': answer['transcript'][match.start():match.end()], 'rule_pattern': pattern, **context})
    return hits


def select_followup(answers, question, branches):
    started = time.perf_counter()
    policy_bytes = POLICY_PATH.read_bytes()
    policy = json.loads(policy_bytes.decode('utf-8'))
    evidence_policy = json.loads(EVIDENCE_POLICY_PATH.read_text(encoding='utf-8'))
    prepared = prepare_text_answers(answers)
    baseline = run_text_baseline(prepared, question)
    context_rules = _load_config(None)['context_rules']
    lexical = []
    for match in baseline['matches']:
        a = next(a for a in prepared if a['answer_id'] == match['answer_id'])
        lexical.append({**match, 'term': match['phrase'],
                        'quote': a['transcript'][match['char_start']:match['char_end']],
                        'retained': match['retained_by_context_rule']})
    coverage = []
    for rule in policy['questions'].get(question['question_id'], []):
        constraint = evidence_policy['questions'].get(question['question_id'], {}).get('branches', {}).get(rule['branch_id'], {})
        hits = _coverage_hits(prepared, rule['patterns'], context_rules,
            own_action=constraint.get('gap_type') == 'own_coordination_action')
        prerequisite = None
        if constraint.get('prerequisite_criterion_id'):
            method_hits = _coverage_hits(prepared, rule.get('prerequisite_patterns', []), context_rules, method=True)
            prerequisite = {'criterion_id': constraint['prerequisite_criterion_id'],
                'category': constraint.get('prerequisite_category'), 'hits': method_hits,
                'status': 'lexical_cue_present' if any(h['retained_by_context_rule'] for h in method_hits) else 'needs_clarification'}
        coverage.append({'branch_id': rule['branch_id'], 'criterion_id': rule['criterion_id'],
                         'target_evidence': rule['target_evidence'],
                         'status': 'lexical_cue_present' if any(h['retained_by_context_rule'] for h in hits) else 'needs_clarification',
                         'hits': hits, 'prerequisite': prerequisite,
                         'eligible': prerequisite is None or prerequisite['status'] == 'lexical_cue_present'})
    allowed = {b['branch_id']: b for b in branches}
    selected = next((c for c in coverage if c['status'] == 'needs_clarification' and c['branch_id'] in allowed and c['eligible']), None)
    branch = allowed[selected['branch_id']] if selected else None
    # Cite actual clauses, not a fabricated quote that purports to prove absence.
    evidence = []
    for a in prepared:
        for seg in a['segments'][:2]:
            quote = a['transcript'][seg['char_start']:seg['char_end']]
            if quote.strip():
                evidence.append({'answer_id': a['answer_id'], 'transcript_revision': a['transcript_revision'],
                                 'quote': quote, 'segment_id': seg['segment_id'],
                                 'char_start': seg['char_start'], 'char_end': seg['char_end']})
    reason = (f"按本题的词典与证据覆盖规则，尚未定位到“{selected['target_evidence']}”的明确线索，选择题库原追问“{branch['text']}”核实。未命中不等于候选人没有该能力。"
              if selected else '已定位到本题已配置追问目标的文字线索，继续下一题；这仅表示词法覆盖，内容是否充分由后台模型与老师复核。')
    if not branches:
        reason = '本题没有已审核的题库追问分支，保存回答并继续。'
    elif not selected and any(not c['eligible'] for c in coverage):
        reason = '尚未定位到该追问所需的具体方法线索，不能直接询问方法失效后的修正；本题没有其他可用题库分支，继续流程并保留待核实项。这不等于内容充分或没有学习能力。'
    incomplete = not selected and any(not c['eligible'] for c in coverage)
    return {'branch_id': branch['branch_id'] if branch else None, 'reason': reason,
            'stop_reason': 'prerequisite_not_met' if incomplete else None, 'coverage_incomplete': incomplete,
            'evidence': evidence, 'summary': {
                'schema_version': 'followup-decision-summary.v1', 'decision_source': policy['version'],
                'brief_reason': reason, 'target_evidence': branch.get('target_evidence') if branch else None,
                'criterion_id': selected['criterion_id'] if selected else None,
                'selected_branch': branch, 'used_evidence': evidence,
                'dictionary_snapshot': {
                    'algorithm_version': baseline['algorithm_version'], 'config_sha256': baseline['config_sha256'],
                    'text_normalization': baseline['text_normalization'], 'matching': baseline['matching'],
                    'input_versions': baseline['input_versions'], 'source': baseline['source'],
                    'matches': lexical, 'usage': 'comparator_only',
                    'involved_in_selection': False,
                    'meaning': '原词表对照；本次选择由下方题库目标覆盖词典规则执行，命中次数不等于能力分。'},
                'coverage_rules': coverage, 'coverage_policy_version': policy['version'],
                'text_normalization': normalization_metadata(),
                'coverage_policy_sha256': hashlib.sha256(policy_bytes).hexdigest(),
                'coverage_guard_version': GUARD_VERSION,
                'coverage_guard_sha256': GUARD_SHA256,
                'branch_evidence_policy_version': evidence_policy['policy_version'],
                'coverage_policy_source': policy['source'],
                'model_role': 'asynchronous_scoring_and_comparison_only',
                'model_involved_in_selection': False, 'latency_ms': round((time.perf_counter()-started)*1000, 2),
                'validation_status': 'engineering_rules_pending_human_calibration'}}
