"""Run risk tests and save a reproducible, credential-free validation manifest."""
import hashlib
import importlib.metadata
import argparse
import io
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--node', default=shutil.which('node'), help='Node.js executable for the synthetic DOM regression suites')
args = parser.parse_args()
stream = io.StringIO()
suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
destination = ROOT / 'local-data/interview-validation'
destination.mkdir(parents=True, exist_ok=True)
(destination / 'unit-tests.txt').write_text(stream.getvalue(), encoding='utf-8')
node_results = []
for script in sorted((ROOT / 'tests').glob('test_*.cjs')):
    record = {'file': script.name, 'scope': 'Synthetic VM/Fake DOM event regression; not browser or decoder validation.'}
    if not args.node:
        record.update(passed=False, reason='Node.js not found; rerun with --node pointing to its executable.', checks_passed=0)
    else:
        try:
            proc = subprocess.run([args.node, str(script)], cwd=ROOT.parent, capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=120)
            output = proc.stdout + proc.stderr
            (destination / (script.stem + '.txt')).write_text(output, encoding='utf-8')
            record.update(passed=proc.returncode == 0, exit_code=proc.returncode,
                          checks_passed=sum(line.startswith('PASS ') for line in proc.stdout.splitlines()))
        except (OSError, subprocess.TimeoutExpired) as error:
            record.update(passed=False, reason=type(error).__name__, checks_passed=0)
    node_results.append(record)
files = [*ROOT.glob('interview_*.py'), *ROOT.glob('*interview*.ps1'), *ROOT.glob('config/interview.v*.json'),
         ROOT / 'config/question-bank.v1.json', *ROOT.glob('site/*interview*.html'),
         ROOT / 'site/js/interview.js', ROOT / 'site/js/hr-review.js',
         ROOT / 'site/hr-review.html', ROOT / 'site/css/interview.css',
         *ROOT.glob('site/workbench.html'), *ROOT.glob('site/js/workbench.js'),
         *ROOT.glob('site/css/workbench.css'), *ROOT.glob('site/js/interview-report.js'),
         *ROOT.glob('site/css/interview-report.css'), *ROOT.glob('site/js/visual-evidence.js'),
         *ROOT.glob('site/css/visual-evidence.css'), *ROOT.glob('site/js/evidence-timeline.js'),
         *ROOT.glob('site/css/evidence-timeline.css'), *ROOT.glob('site/*research*.html'),
         *ROOT.glob('site/js/*research*.js'), *ROOT.glob('site/css/*research*.css'),
         *ROOT.glob('site/js/potential-profile.js'), *ROOT.glob('site/css/potential-profile.css'),
         *ROOT.glob('site/js/alignment-editor.js'), ROOT / 'delivery_tools.py', ROOT / 'requirements-interview.txt',
         ROOT / 'config/text-baseline.v1.json', ROOT / 'validate_interview.py', ROOT / 'check_mcp_live.py',
         *ROOT.glob('check_delivery_*.py'), *ROOT.glob('tests/test_*.py'), *ROOT.glob('tests/test_*.cjs')]
hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
passed = result.wasSuccessful() and bool(node_results) and all(item['passed'] for item in node_results)
summary = {'validated_at_utc': datetime.now(timezone.utc).isoformat(),
           'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
           'skipped': len(result.skipped), 'python_passed': result.wasSuccessful(), 'passed': passed,
           'node_suites': node_results, 'node_checks_passed': sum(item['checks_passed'] for item in node_results),
           'test_scope': 'All Python test_*.py including delivery tools, plus all Node test_*.cjs. Synthetic domain/provider/UI contracts and local media checks; not live LLM, browser, Qwen desktop import, or webcam validation.',
           'python': sys.version, 'platform': platform.platform(),
           'dependencies': {name: importlib.metadata.version(name) for name in
                            ('fastapi', 'pydantic', 'requests', 'uvicorn', 'openai-whisper', 'numpy', 'torch')},
           'source_sha256': hashes}
(destination / 'validation-manifest.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({k: summary[k] for k in ('tests_run', 'failures', 'errors', 'skipped', 'node_checks_passed', 'node_suites', 'passed')}))
sys.exit(0 if passed else 1)
