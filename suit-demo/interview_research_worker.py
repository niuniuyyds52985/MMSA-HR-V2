"""Independent durable research worker; never occupies the interview queue."""
from __future__ import annotations
import argparse
import os
import json
from pathlib import Path
import subprocess
import sys
import time


def stop_child(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill.exe', '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
    else:
        proc.kill()
        proc.wait(timeout=10)


def process_research_job(core, store, job, timeout=900):
    folder = core.data_dir / 'research-work' / job['run_id']
    folder.mkdir(parents=True, exist_ok=True)
    input_path, output = folder / 'input.json', folder / 'result.json'
    input_path.write_text(json.dumps(job['input'], ensure_ascii=False), encoding='utf-8')
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--input', str(input_path), '--output', str(output)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            # Windows schedules background observations below the
                            # interview's ASR/content work; inference is unchanged.
                            creationflags=(getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                                           | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)))
    started = heartbeat = time.monotonic()
    try:
        while proc.poll() is None:
            now = time.monotonic()
            if now - started > timeout:
                stop_child(proc)
                return store.complete(job['run_id'], job['lease_token'], error='研究运行超时；重试须显式创建新运行。')
            if now - heartbeat > 10:
                store.heartbeat(job['run_id'], job['lease_token'])
                heartbeat = now
            time.sleep(.2)
        if proc.returncode or not output.is_file():
            return store.complete(job['run_id'], job['lease_token'], error='研究子进程未返回有效结果。')
        result = json.loads(output.read_text(encoding='utf-8'))
        return store.complete(job['run_id'], job['lease_token'], result=result.get('result'), error=result.get('error'))
    finally:
        if proc.poll() is None:
            stop_child(proc)
        input_path.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir')
    parser.add_argument('--input')
    parser.add_argument('--output')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--drain', action='store_true')
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--lane', choices=('all', 'core', 'extended'), default='all',
                        help='core: basic automatic observations; extended: extended models and independent research')
    args = parser.parse_args()
    if args.input:
        from interview_research import execute_run
        try:
            snapshot = json.loads(Path(args.input).read_text(encoding='utf-8'))
            result = {'result': execute_run(snapshot, Path(args.output).parent)}
        except Exception:
            result = {'error': '研究推理失败；请检查依赖与模型诊断。未生成替代结果。'}
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return
    from interview_core import InterviewCore
    from interview_research import ResearchStore
    from interview_runtime import build_identity, maintenance_active
    core = InterviewCore(args.data_dir) if args.data_dir else InterviewCore()
    store = ResearchStore(core)
    print(json.dumps({'worker': os.getpid(), 'status': 'started', 'lane': args.lane, **build_identity()}), flush=True)
    while True:
        if maintenance_active(core.data_dir):
            if args.once or args.drain:
                break
            time.sleep(0.25)
            continue
        job = store.claim(lane=args.lane)
        if job:
            try:
                process_research_job(core, store, job, timeout=max(30, args.timeout))
            except Exception:
                try:
                    store.complete(job['run_id'], job['lease_token'], error='研究结果写回被拒绝或输入失效。')
                except Exception:
                    pass
            print(json.dumps({'run_id': job['run_id'], 'event': 'processed', 'lane': args.lane}), flush=True)
            if args.once:
                break
        elif args.once or args.drain:
            break
        else:
            time.sleep(1)


if __name__ == '__main__':
    main()
