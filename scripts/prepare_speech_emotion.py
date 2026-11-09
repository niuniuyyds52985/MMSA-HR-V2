"""Install pinned local speech-emotion assets. Run with .venv-suit Python.

Downloads only public model assets; never reads or uploads interview recordings.
"""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.mmsa_v2.speech_emotion import FILES, MODEL_DIR, DEPENDENCIES


def main():
    subprocess.run([sys.executable, "-m", "pip", "install", "--target", str(DEPENDENCIES),
                    "--no-deps", "omegaconf==2.4.0", "antlr4-python3-runtime==4.13.2"], check=True)
    import requests
    session = requests.Session()
    session.trust_env = False
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    assets = {**FILES, "README.md": "cb36ab32c3196085453c1c5a9076a0c7fed121a4487ab89a0a36c62e5ad613e0"}
    for name, expected in assets.items():
        revision = {"tokens.txt": "97b6c3aeb9878641d4721675edd605e1577d0b5d",
                    "README.md": "a4a780d91424116a2d46330c045336a6f62a66a1"}.get(
                        name, "58354f1e5b6c118cf8b175b5b1955d6b07df42c2")
        url = f"https://modelscope.cn/models/iic/emotion2vec_plus_base/resolve/{revision}/{name}"
        target = MODEL_DIR / name
        verified = False
        if target.is_file():
            with target.open("rb") as handle:
                verified = hashlib.file_digest(handle, "sha256").hexdigest() == expected
        if not verified:
            print("Downloading", name, flush=True)
            response = session.get(url, stream=True, timeout=(20, 120))
            response.raise_for_status()
            partial = target.with_suffix(target.suffix + ".partial")
            digest = hashlib.sha256()
            with partial.open("wb") as handle:
                for block in response.iter_content(1024 * 1024):
                    handle.write(block)
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise RuntimeError("Official model checksum mismatch: " + name)
            partial.replace(target)
        records.append({"file": name, "sha256": expected, "revision": revision, "url": url})
    (MODEL_DIR / "installed-assets.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    print("Local speech-emotion assets verified; inference never downloads or uploads data.")


if __name__ == "__main__":
    main()
