"""Real bounded proof on the repository sample, never on participant media."""
from __future__ import annotations
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from interview_multimodal import run_multimodal

ROOT = Path(__file__).resolve().parent


def peak_process_working_set():
    if os.name != 'nt':
        return None
    class MemoryCounters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
            'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
    value = MemoryCounters()
    value.cb = ctypes.sizeof(value)
    process = ctypes.windll.kernel32.GetCurrentProcess
    process.restype = ctypes.c_void_p
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(MemoryCounters), ctypes.c_ulong]
    if query(process(), ctypes.byref(value), value.cb):
        return value.PeakWorkingSetSize
    return None


def main():
    destination = ROOT / 'local-data' / 'interview-validation' / 'original-au-net'
    source = ROOT / 'data' / 'small-question-video.mp4'
    before, cpu = time.perf_counter(), time.process_time()
    result = run_multimodal(source, {'answer_id': 'repository_sample_only',
        'transcript_revision': 1}, destination, ['au_net'])
    module = result['modules']['au_net']
    assert module['status'] == 'complete', module.get('reason')
    assert module['summary']['sampled_windows'] == module['summary']['valid_windows'] > 3
    assert not module['summary']['truncated_by_window_limit']
    for observation in module['observations']:
        assert len(observation['values']['activation_scores']) == 15
        assert all(0 <= value <= 1 for value in observation['values']['activation_scores'].values())
        assert len(set(observation['input_frame_times_ms'])) == 3
        assert observation['last_input_ms'] == max(observation['input_frame_times_ms'])
    proof = {'schema_version': 'original-au-net-proof.v1', 'created_at': datetime.now(timezone.utc).isoformat(),
        'status': 'passed', 'media_fixture': 'data/small-question-video.mp4',
        'media_sha256': result['media_sha256'], 'run_id': result['run_id'], 'module_run_id': module['run_id'],
        'duration_ms': result['duration_ms'], 'module': module['model_id'], 'summary': module['summary'],
        'elapsed_seconds': round(time.perf_counter() - before, 3),
        'cpu_process_seconds': round(time.process_time() - cpu, 3),
        'peak_process_working_set_bytes': peak_process_working_set(),
        'peak_memory_scope': 'current isolated Python process lifetime, including imports/model load; Windows working set',
        'result_artifact': result['artifact'], 'module_elapsed_seconds': module['elapsed_seconds'],
        'code_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ('interview_au_net.py', 'interview_multimodal.py')},
        'limitations': ['One repository sample is engineering execution proof, not AU detection accuracy.',
            'YuNet replaces upstream face_alignment detection; no preprocessing equivalence claim.',
            'Offline future input frames; not evidence available at centre timestamp.',
            'No participant sessions, cloud requests or credentials accessed.']}
    path = destination / ('check-' + result['run_id'] + '.json')
    path.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(proof, ensure_ascii=False))


if __name__ == '__main__':
    main()
