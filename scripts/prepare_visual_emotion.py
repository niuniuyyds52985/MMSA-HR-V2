"""Download the fixed official EmotiEffNet ONNX comparator, with SHA-256 checks.

This is explicit installation only. Runtime inference never downloads weights.
Run with the project's Python; no pip installation or credential is needed.
"""
import base64
import hashlib
import json
from pathlib import Path
import requests

REVISION = "520a051c64cd191521e5934655314e769a319684"
PROJECT = Path(__file__).resolve().parents[1]
FILES = {
    "model.onnx": ("models/affectnet_emotions/onnx/enet_b0_8_va_mtl.onnx", "c43e056ad388d4a8dc911832b8291435b2af537f967e5870ebd731574ec7e812"),
    "upstream_facial_analysis.py": ("emotiefflib/facial_analysis.py", "e17c7cf7a273739485b2fc5b56b7b438c5b838079f5bda09ea20636b267e506b"),
    "upstream_utils.py": ("emotiefflib/utils.py", "53d6c063b0ef2cf75856f0b68e3edd646fb069546e7b1a4d1c77bff0f157837e"),
    "LICENSE": ("LICENSE", "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"),
}


def main():
    folder = PROJECT / "suit-demo/models/emotieffnet"
    folder.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.trust_env = False
    api = "https://api.github.com/repos/sb-ai-lab/EmotiEffLib"
    response = session.get(api + "/git/trees/" + REVISION + "?recursive=1", timeout=(10, 30))
    response.raise_for_status()
    entries = {item["path"]: item for item in response.json()["tree"]}
    manifest = {"repository": "https://github.com/sb-ai-lab/EmotiEffLib", "revision": REVISION,
                "model_id": "EmotiEffLib/enet_b0_8_va_mtl", "files": {}}
    for name, (relative, expected) in FILES.items():
        destination = folder / name
        data = destination.read_bytes() if destination.is_file() else b""
        entry = entries[relative]
        if hashlib.sha256(data).hexdigest() != expected:
            response = session.get(api + "/git/blobs/" + entry["sha"], timeout=(10, 90))
            response.raise_for_status()
            data = base64.b64decode(response.json()["content"])
            if hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError("Official artifact hash mismatch: " + name)
            temporary = destination.with_suffix(destination.suffix + ".download")
            temporary.write_bytes(data)
            temporary.replace(destination)
        manifest["files"][name] = {"source": "https://raw.githubusercontent.com/sb-ai-lab/EmotiEffLib/" + REVISION + "/" + relative,
                                    "sha256": expected, "git_blob_sha": entry["sha"], "bytes": len(data)}
        print("Verified", name, len(data))
    (folder / "source.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
