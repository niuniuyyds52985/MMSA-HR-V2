"""Durable local media worker with lease heartbeats and a process timeout.

Run alongside `uvicorn interview_entry:app`. Model calls require local credentials.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def _write_result(output, result):
    # Atomic visibility lets a warm child signal completion without printing text.
    destination = Path(output)
    temporary = destination.with_suffix(destination.suffix + '.writing')
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    temporary.replace(destination)


def analyze_child(path, language, output, asr_engine='openai-whisper-base.v1'):
    from interview_media import analyze_media, MediaError
    try:
        result = {'ok': True, 'result': analyze_media(path, language, asr_engine=asr_engine)}
    except Exception as exc:
        result = {'ok': False, 'error': str(exc) if isinstance(exc, MediaError)
                  else '媒体分析失败，请检查运行环境或人工核对转写。'}
    _write_result(output, result)


def media_service(warmup_engine):
    # Parent IPC is explicitly UTF-8 even on Windows with a legacy code page.
    sys.stdin.reconfigure(encoding='utf-8')
    from interview_asr import warmup
    try:
        warmup(warmup_engine)
    except Exception:
        # Individual tasks retain the existing useful-energy / ASR-failure path.
        pass
    for line in sys.stdin:
        request = json.loads(line)
        analyze_child(request['path'], request['language'], request['output'], request['asr_engine'])
        # The only persistent state is model weights. No result/audio history.
        del request


def stop_process(proc):
    if proc is None or proc.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill.exe', '/PID', str(proc.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


class PersistentMediaProcess:
    """One sequential local ASR process; crashes/timeouts discard it entirely."""
    def __init__(self):
        self.proc = None

    def start(self):
        if self.proc is None or self.proc.poll() is not None:
            self.close()
            self.proc = subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--media-service',
                '--asr-engine', 'faster-whisper-base-int8.v1'], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return self.proc

    def submit(self, job, output):
        proc = self.start()
        proc.stdin.write(json.dumps({'path': job['path'], 'language': job['language'],
            'output': str(output), 'asr_engine': job.get('asr_engine', 'openai-whisper-base.v1')}, ensure_ascii=False) + '\n')
        proc.stdin.flush()
        return proc

    def close(self):
        if self.proc:
            stop_process(self.proc)
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc = None


def content_child(input_path, output, routing=False):
    from interview_llm import evaluate_answer, ProviderError
    try:
        data = json.loads(Path(input_path).read_text(encoding='utf-8'))
        from interview_routing import evaluate_routing
        evaluate = evaluate_routing if routing else evaluate_answer
        result = {'ok': True, 'result': evaluate(data['question'], data['answers'], data['branches'], data.get('model_config'))}
    except Exception as exc:
        result = {'ok': False, 'error': str(exc) if isinstance(exc, ProviderError)
                  else '内容模型分析失败，待人工核对。',
                  'code': exc.code if isinstance(exc, ProviderError) else 'content_error',
                  'retryable': exc.retryable if isinstance(exc, ProviderError) else False,
                  'details': exc.details if isinstance(exc, ProviderError) else {}}
    Path(output).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


def process_job(core, job, timeout_seconds, media_process=None):
    runtime = Path(core.data_dir) / 'worker-tmp'
    runtime.mkdir(parents=True, exist_ok=True)
    output = runtime / f'{uuid.uuid4().hex}.json'
    input_path = None
    if job.get('job_type') in ('content','routing'):
        input_path = runtime / f'{uuid.uuid4().hex}.input.json'
        input_path.write_text(json.dumps({k: job.get(k) for k in ('question', 'answers', 'branches', 'model_config')},
                                        ensure_ascii=False), encoding='utf-8')
        arguments = ['--routing-input' if job.get('job_type')=='routing' else '--content-input', str(input_path)]
    else:
        arguments = ['--analyze', job['path'], '--language', job['language'],
                     '--asr-engine', job.get('asr_engine', 'openai-whisper-base.v1')]
    persistent = media_process is not None and job.get('job_type') == 'media'
    if persistent:
        try:
            proc = media_process.submit(job, output)
        except (OSError, BrokenPipeError):
            media_process.close()
            core.fail_job(job['job_id'], job['lease_token'], '本地转写进程连接中断，下次任务会重建进程。', retryable=True)
            return
    else:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
            *arguments, '--output', str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if job.get('job_type')=='routing': timeout_seconds=8
    begin = time.monotonic()
    heartbeat = begin
    try:
        while proc.poll() is None and not (persistent and output.exists()):
            now = time.monotonic()
            if now - begin > timeout_seconds:
                stop_process(proc)
                core.fail_job(job['job_id'], job['lease_token'],
                              '分析超过配置时限，已终止本次任务。', retryable=job.get('job_type')!='routing', code='worker_timeout')
                return
            if now - heartbeat >= 10:
                core.heartbeat_job(job['job_id'], job['lease_token'], lease_seconds=120)
                heartbeat = now
            time.sleep(0.2)
        if (proc.returncode is not None and proc.returncode != 0) or not output.exists():
            core.fail_job(job['job_id'], job['lease_token'],
                          '分析子进程退出且未返回结果。', retryable=True, code='worker_exit')
            return
        data = json.loads(output.read_text(encoding='utf-8'))
        if data['ok']:
            if job.get('job_type') == 'routing':
                core.complete_routing_job(job['job_id'], job['lease_token'], data['result'])
            elif job.get('job_type') == 'content':
                core.complete_content_job(job['job_id'], job['lease_token'], data['result'])
            else:
                core.complete_job(job['job_id'], job['lease_token'], data['result'])
        else:
            core.fail_job(job['job_id'], job['lease_token'], data['error'], retryable=data.get('retryable', False),
                          code=data.get('code'), details=data.get('details'))
    finally:
        # A leased writeback failure must not leave an unobserved media task alive.
        if not persistent or not output.exists():
            stop_process(proc)
        output.unlink(missing_ok=True)
        output.with_suffix(output.suffix + '.writing').unlink(missing_ok=True)
        if input_path:
            input_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir')
    parser.add_argument('--lane', choices=('all', 'media', 'content', 'routing'), default='all')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--drain', action='store_true')
    parser.add_argument('--resume-blocked-session', help='Explicitly retry one historical session after configuration')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--analyze')
    parser.add_argument('--media-service', action='store_true')
    parser.add_argument('--asr-engine', choices=('openai-whisper-base.v1', 'faster-whisper-base-int8.v1'),
                        default='openai-whisper-base.v1')
    parser.add_argument('--content-input')
    parser.add_argument('--routing-input')
    parser.add_argument('--language', default='zh')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.media_service:
        media_service(args.asr_engine)
        return
    if args.analyze:
        analyze_child(args.analyze, args.language, args.output, args.asr_engine)
        return
    if args.routing_input:
        content_child(args.routing_input,args.output,routing=True)
        return
    if args.content_input:
        content_child(args.content_input, args.output)
        return
    from interview_core import InterviewCore
    from interview_runtime import build_identity, maintenance_active
    kwargs = {'data_dir': args.data_dir} if args.data_dir else {}
    core = InterviewCore(**kwargs)
    worker_id = f'local-{os.getpid()}-{uuid.uuid4().hex[:8]}'
    print(json.dumps({'worker': worker_id, 'status': 'started', **build_identity()}), flush=True)
    from interview_llm import provider_status
    from interview_credentials import cloud_inputs_allowed
    if args.resume_blocked_session:
        core.resume_blocked_content_jobs(session_id=args.resume_blocked_session)
    media_process = PersistentMediaProcess() if args.lane in ('all','media') else None
    if media_process:
        # Load while the candidate checks devices/records, before an answer queues.
        if not args.once and not args.drain:
            media_process.start()
        import atexit
        atexit.register(media_process.close)
    while True:
        if maintenance_active(core.data_dir):
            if args.once or args.drain:
                return
            time.sleep(0.25)
            continue
        job = core.claim_job(worker_id=worker_id, lease_seconds=120, job_type='routing') if args.lane in ('all','routing') else None
        if not job and args.lane in ('all','content'):
            job=core.claim_job(worker_id=worker_id,lease_seconds=120,job_type='content')
        if (job and args.resume_blocked_session != job['session_id']
                and not core.cloud_analysis_authorized(job['session_id'])
                and not cloud_inputs_allowed(job['answers'])):
            core.block_job(job['job_id'], job['lease_token'], '包含启用云接口前的历史回答，未自动发送。需明确指定会话后才能重试。')
            if args.once:
                return
            continue
        if job and provider_status(job.get('model_config'))['status'] == 'not_configured':
            core.block_job(job['job_id'], job['lease_token'], '百炼大模型未配置；内容自动评定未运行。')
            if args.once:
                return
            continue
        job = job or (core.claim_job(worker_id=worker_id, lease_seconds=120, job_type='media') if args.lane in ('all','media') else None)
        if job:
            try:
                process_job(core, job, max(30, args.timeout), media_process=media_process)
                print(json.dumps({'job_id': job['job_id'], 'status': 'processed'}), flush=True)
            except Exception as exc:
                # Never log transcript, bearer tokens, model credentials or raw exception text.
                try:
                    core.fail_job(job['job_id'], job['lease_token'],
                                  '结果写回被拒绝或任务输入已变化；保留当前正式版本。', retryable=False)
                except Exception:
                    # Another worker owns an expired/reclaimed lease; never overwrite it.
                    pass
                print(json.dumps({'job_id': job['job_id'], 'status': 'worker_error',
                                  'error_type': type(exc).__name__}), flush=True)
            if args.once:
                return
        elif args.once or args.drain:
            return
        else:
            time.sleep(0.15 if args.lane=='routing' else 1)


if __name__ == '__main__':
    main()
