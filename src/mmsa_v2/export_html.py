"""Portable version-pinned local reports, with exactly their referenced media."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import quote

from .product_html import batch_html, personal_html


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,96}", value):
        raise ValueError("无效的导出对象编号")
    return value


def _save_html(target, html):
    target.parent.mkdir(parents=True, exist_ok=True)
    path = None
    try:
        with NamedTemporaryFile(dir=target.parent, delete=False, mode="w", encoding="utf-8", newline="") as output:
            path = Path(output.name)
            output.write(html)
        os.replace(path, target)
    finally:
        if path:
            path.unlink(missing_ok=True)
    return target


def _digest(path):
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_asset(store, media, folder):
    fingerprint = media.get("sha256", "")
    if not re.fullmatch("[0-9a-f]{64}",fingerprint):
        raise ValueError("媒体指纹无效")
    source = store.media_path(media).resolve()
    suffix = source.suffix.lower()
    if (source.parent != (store.root / "media").resolve()
            or source.name != fingerprint + suffix
            or suffix not in {".webm",".mp4",".mov",".wav",".mp3",".ogg",".m4a",".flac"}):
        raise ValueError("媒体引用不属于该场次的归档")
    assets = folder / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    if assets.resolve().parent != folder.resolve():
        raise ValueError("资源目录不在导出目录内")
    target = assets / source.name
    if target.is_symlink():
        raise ValueError("资源路径不允许符号链接")
    if not target.is_file() or _digest(target) != fingerprint:
        pending = None
        digest = sha256()
        try:
            with source.open("rb") as stream, NamedTemporaryFile(dir=assets,delete=False) as output:
                pending = Path(output.name)
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != fingerprint:
                raise ValueError("原片与已记录指纹不一致，已停止导出")
            os.replace(pending,target)
        finally:
            if pending:
                pending.unlink(missing_ok=True)
    return "assets/" + quote(target.name)


def export_personal(store, sid, export_dir, version=None):
    """Write the frozen personal HTML and its own media assets to export_dir."""
    folder = Path(export_dir)
    report = store.report(_identifier(sid), version)
    recordings = list(report["answers"].values())
    introduction = report.get("introduction") or {}
    if introduction.get("status") == "saved":
        recordings.append(introduction)
    for answer in recordings:
        media = answer.get("media")
        if media:
            media["export_path"] = _copy_asset(store, media, folder)
    target = folder / f"personal-{sid}-v{int(report['version'])}.html"
    return _save_html(target, personal_html(report))


def export_batch(store, bid, export_dir):
    """Export only members of one batch, binding individuals to versions in its snapshot."""
    folder = Path(export_dir)
    batch = store.batch(_identifier(bid))
    snapshot = json.dumps(batch, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    revision = sha256(snapshot).hexdigest()[:16]
    for candidate in batch["candidates"]:
        if candidate.get("session_id"):
            saved = export_personal(store, candidate["session_id"], folder, version=candidate["version"])
            candidate["export_path"] = quote(saved.name)
    return _save_html(folder / f"batch-{bid}-{revision}.html", batch_html(batch))
