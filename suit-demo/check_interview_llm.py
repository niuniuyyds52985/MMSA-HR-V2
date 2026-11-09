"""Explicit live check using synthetic text only. Never prints credentials."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from interview_llm import evaluate_answer, ProviderError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True, help='Authorize one synthetic API evaluation (adapter may retry once)')
    parser.parse_args()
    root = Path(__file__).resolve().parent
    config = json.loads((root / 'config/interview.v1.json').read_text(encoding='utf-8-sig'))
    question = config['questions'][0]
    answers = [{'answer_id': 'synthetic-live-check-v1', 'transcript_revision': 1,
                'transcript': '这是一条用于接口验收的合成回答，不是真实候选人经历。我担任社团活动负责人时，两名成员对分工有分歧。我分别了解他们能投入的时间，把任务按经验拆开，明确每项负责人和截止时间，每晚检查进度。遇到物料延迟，我联系备用供应商。活动如期完成，复盘时我们补充了备用供应商清单。',
                'parent_question_id': None}]
    started = time.monotonic()
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'scope': 'live API with synthetic text only; no real candidate data', 'synthetic': True}
    try:
        report.update(status='succeeded', result=evaluate_answer(question, answers, config['branches']))
    except ProviderError as exc:
        report.update(status='failed', error_code=exc.code, message=str(exc))
    except Exception:
        report.update(status='failed', error_code='local_configuration_or_runtime', message='Local check failed; secret and raw exception withheld.')
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    destination = root / 'local-data/interview-validation/live-llm-check.json'
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('status', 'elapsed_seconds', 'error_code', 'message') if k in report}, ensure_ascii=False))
    if report['status'] == 'succeeded':
        print(json.dumps({'returned_model': report['result']['provider'].get('returned_model'), 'schema_and_quotes_validated': True}))
    return 0 if report['status'] == 'succeeded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
