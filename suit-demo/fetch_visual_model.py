"""Fetch and verify the official OpenCV Zoo face-box detector assets only."""
from pathlib import Path
import hashlib
import json
import re
import requests

ROOT = Path(__file__).resolve().parent
NAME = 'face_detection_yunet_2023mar.onnx'
DIRECTORY = 'models/face_detection_yunet/'
session = requests.Session()
session.headers['User-Agent'] = 'Suit-local-visual-model-setup'

def get(url):
    response = session.get(url, timeout=(10, 60))
    response.raise_for_status()
    return response

commit = get('https://api.github.com/repos/opencv/opencv_zoo/commits/main').json()['sha']
if not re.fullmatch('[0-9a-f]{40}', commit):
    raise ValueError('Unexpected source commit')
raw = f'https://raw.githubusercontent.com/opencv/opencv_zoo/{commit}/{DIRECTORY}'
pointer = get(raw + NAME).text
sha = re.search(r'^oid sha256:([0-9a-f]{64})$', pointer, re.M).group(1)
size = int(re.search(r'^size (\d+)$', pointer, re.M).group(1))
if not 0 < size < 5 * 1024 * 1024:
    raise ValueError('Unexpected detector size')
url = f'https://media.githubusercontent.com/media/opencv/opencv_zoo/{commit}/{DIRECTORY}{NAME}'
content = get(url).content
if len(content) != size or hashlib.sha256(content).hexdigest() != sha:
    raise ValueError('Detector does not match official Git LFS pointer')
license_text = get(raw + 'LICENSE').text
target = ROOT / 'models'
target.mkdir(exist_ok=True)
(target / NAME).write_bytes(content)
(target / (NAME + '.LICENSE')).write_text(license_text, encoding='utf-8')
record = {'source': 'OpenCV Zoo', 'source_commit': commit, 'url': url,
          'sha256': sha, 'bytes': size, 'license_source': raw + 'LICENSE',
          'use': 'Local face bounding boxes only; no identity, emotion, personality or integrity inference.'}
(target / (NAME + '.source.json')).write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps({'downloaded': NAME, 'bytes': size, 'sha256': sha, 'source_commit': commit}))
