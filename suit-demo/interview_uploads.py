"""Resumable, version-bound media uploads. Chunks are not playable media files.

Only a fully assembled, hash-checked and decoded container can become an answer.
No caller-supplied filesystem path is ever accepted.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

from interview_core import DomainError, digest, dumps, uid, utc

MAX_UPLOAD = 100 * 1024 * 1024
MIN_CHUNK = 64 * 1024
MAX_CHUNK = 4 * 1024 * 1024
EXTENSIONS = {'.mp4', '.webm', '.wav', '.m4a', '.mp3', '.ogg'}


class UploadStore:
    def __init__(self, core):
        self.core = core
        self.root = core.data_dir / 'uploads'
        if self.root.is_symlink() or not self.root.resolve().is_relative_to(core.data_dir.resolve()):
            raise DomainError('invalid_upload_storage', '上传目录不能是外部链接', 422)
        self.root.mkdir(exist_ok=True)
        with core._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS media_uploads(
                id TEXT PRIMARY KEY,session_id TEXT NOT NULL,round_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,metadata TEXT NOT NULL,status TEXT NOT NULL,
                created_at TEXT NOT NULL,completion_hash TEXT,result TEXT,
                UNIQUE(session_id,attempt_id));
            CREATE TABLE IF NOT EXISTS media_upload_chunks(
                upload_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,PRIMARY KEY(upload_id,chunk_index));
            ''')

    @staticmethod
    def _hash(value):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
            raise DomainError('invalid_media_hash', '需要小写 SHA-256 摘要', 422)
        return value

    def _row(self, db, sid, upload_id):
        if not isinstance(upload_id, str) or not re.fullmatch(r'[a-f0-9]{32}', upload_id):
            raise DomainError('not_found', '本场上传记录不存在', 404)
        row = db.execute('SELECT * FROM media_uploads WHERE id=? AND session_id=?', (upload_id, sid)).fetchone()
        if not row:
            raise DomainError('not_found', '本场上传记录不存在', 404)
        meta = json.loads(row['metadata'])
        if (type(meta.get('total_size')) is not int or not 0 < meta['total_size'] <= MAX_UPLOAD
                or type(meta.get('chunk_size')) is not int or not MIN_CHUNK <= meta['chunk_size'] <= MAX_CHUNK
                or meta.get('suffix') not in EXTENSIONS or meta.get('round_id') != row['round_id']
                or meta.get('attempt_id') != row['attempt_id']):
            raise DomainError('invalid_upload_record', '上传记录不符合长度或版本约束', 422)
        self._hash(meta.get('sha256'))
        return row

    def _folder(self, upload_id):
        folder = self.root / upload_id
        if folder.is_symlink() or not folder.resolve().is_relative_to(self.root.resolve()):
            raise DomainError('invalid_upload_storage', '上传分片目录不能是外部链接', 422)
        return folder

    def _status(self, db, row):
        meta = json.loads(row['metadata'])
        chunks = list(db.execute('SELECT chunk_index,size FROM media_upload_chunks WHERE upload_id=? ORDER BY chunk_index', (row['id'],)))
        if row['status'] != 'completed':
            # A restored DB or interrupted disk write cannot pretend missing bytes exist.
            available = []
            for chunk in chunks:
                if type(chunk['chunk_index']) is not int or not 0 <= chunk['chunk_index'] < math.ceil(meta['total_size'] / meta['chunk_size']):
                    continue
                part = self._folder(row['id']) / f"{chunk['chunk_index']}.part"
                try:
                    if not part.is_symlink() and part.is_file() and part.stat().st_size == chunk['size']:
                        available.append(chunk)
                except OSError:
                    pass
            chunks = available
        result = json.loads(row['result']) if row['result'] else None
        return {'upload_id': row['id'], 'round_id': row['round_id'], 'attempt_id': row['attempt_id'],
                'status': row['status'], 'chunk_size': meta['chunk_size'], 'total_size': meta['total_size'],
                'total_chunks': math.ceil(meta['total_size'] / meta['chunk_size']),
                'received_chunks': [r['chunk_index'] for r in chunks],
                'received_bytes': sum(r['size'] for r in chunks), 'sha256': meta['sha256'],
                'answer_id': result.get('answer_id') if result else None}

    def status(self, sid, upload_id):
        with self.core._db() as db:
            return self._status(db, self._row(db, sid, upload_id))

    def _invalidate_chunk(self, sid, upload_id, index):
        with self.core._db(True) as db:
            self._row(db, sid, upload_id)
            db.execute('DELETE FROM media_upload_chunks WHERE upload_id=? AND chunk_index=?', (upload_id, index))

    def create(self, sid, *, round_id, attempt_id, filename, total_size, chunk_size, sha256, idempotency_key):
        if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 100:
            raise DomainError('invalid_attempt', '无效录制版本', 422)
        if type(total_size) is not int or not 0 < total_size <= MAX_UPLOAD:
            raise DomainError('invalid_upload_size', '媒体须大于0字节且不超过100MB', 422)
        if type(chunk_size) is not int or not MIN_CHUNK <= chunk_size <= MAX_CHUNK:
            raise DomainError('invalid_chunk_size', '分片大小须在64KB至4MB之间', 422)
        suffix = Path(filename).suffix.lower()
        if suffix not in EXTENSIONS:
            raise DomainError('unsupported_media', '支持mp4/webm/wav/m4a/mp3/ogg', 422)
        metadata = {'round_id': round_id, 'attempt_id': attempt_id, 'suffix': suffix,
                    'total_size': total_size, 'chunk_size': chunk_size, 'sha256': self._hash(sha256)}
        with self.core._db(True) as db:
            scope = 'upload-init:' + sid
            previous = self.core._idempotent(db, scope, idempotency_key, metadata)
            if previous:
                return self._status(db, self._row(db, sid, previous['upload_id']))
            existing = db.execute('SELECT * FROM media_uploads WHERE session_id=? AND attempt_id=?', (sid, attempt_id)).fetchone()
            if existing:
                existing = self._row(db, sid, existing['id'])
                if json.loads(existing['metadata']) != metadata:
                    raise DomainError('upload_attempt_conflict', '该录制版本已绑定不同媒体，请重新核对')
                result = self._status(db, existing)
            else:
                session = self.core._session(db, sid)
                latest = db.execute('SELECT id FROM rounds WHERE session_id=? ORDER BY ordinal DESC LIMIT 1', (sid,)).fetchone()
                if session['status'] != 'answering' or not latest or latest['id'] != round_id:
                    raise DomainError('round_closed', '只能上传当前正式问题的回答')
                if db.execute('SELECT 1 FROM answers WHERE session_id=? AND attempt_id=?', (sid, attempt_id)).fetchone():
                    raise DomainError('attempt_conflict', '该录制版本已正式提交')
                count = db.execute('SELECT count(*) FROM media_uploads WHERE session_id=? AND round_id=?', (sid, round_id)).fetchone()[0]
                if count >= 10:
                    raise DomainError('upload_limit', '本题上传尝试已达上限；请恢复已有上传', 429)
                upload_id = uid()
                db.execute('INSERT INTO media_uploads VALUES(?,?,?,?,?,?,?,?,?)',
                           (upload_id, sid, round_id, attempt_id, dumps(metadata), 'uploading', utc(), None, None))
                self._folder(upload_id).mkdir(exist_ok=True)
                result = self._status(db, self._row(db, sid, upload_id))
            self.core._remember(db, scope, idempotency_key, metadata, {'upload_id': result['upload_id']})
            return result

    def put_chunk(self, sid, upload_id, index, data, sha256):
        if type(index) is not int or index < 0:
            raise DomainError('invalid_chunk_index', '无效分片序号', 422)
        self._hash(sha256)
        if not data or len(data) > MAX_CHUNK or hashlib.sha256(data).hexdigest() != sha256:
            raise DomainError('invalid_chunk_hash', '分片长度或摘要不匹配，请重传原分片', 422)
        with self.core._db(True) as db:
            row = self._row(db, sid, upload_id)
            meta = json.loads(row['metadata'])
            count = math.ceil(meta['total_size'] / meta['chunk_size'])
            size = min(meta['chunk_size'], meta['total_size'] - index * meta['chunk_size'])
            if index >= count or len(data) != size:
                raise DomainError('invalid_chunk_size', '分片序号或长度不符合本次上传约定', 422)
            old = db.execute('SELECT * FROM media_upload_chunks WHERE upload_id=? AND chunk_index=?', (upload_id, index)).fetchone()
            if old and (old['sha256'] != sha256 or old['size'] != len(data)):
                raise DomainError('chunk_conflict', '已收到的分片不能替换为不同内容')
            if row['status'] == 'completed':
                if old:
                    return self._status(db, row)
                raise DomainError('upload_completed', '此媒体已经正式提交')
            folder = self._folder(row['id'])
            folder.mkdir(exist_ok=True)
            target = folder / f'{index}.part'
            # Atomic replacement also repairs an interrupted file write before its DB commit.
            temp = folder / f'{index}.{uid()}.tmp'
            try:
                with temp.open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    import os
                    os.fsync(stream.fileno())
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
            db.execute('INSERT OR IGNORE INTO media_upload_chunks VALUES(?,?,?,?)', (upload_id, index, len(data), sha256))
            return self._status(db, row)

    def complete(self, sid, upload_id, *, transcript='', language='zh', replace_answer_id=None, idempotency_key):
        request = {'transcript': transcript, 'language': language, 'replace_answer_id': replace_answer_id,
                   'idempotency_key': idempotency_key}
        with self.core._db(True) as db:
            # Validate the key before assembling anything; the final answer uses the same key.
            self.core._idempotent(db, 'upload-complete:' + sid, idempotency_key, {'upload_id': upload_id, **request})
            row = self._row(db, sid, upload_id)
            if row['completion_hash'] and row['completion_hash'] != digest(request):
                raise DomainError('upload_completion_conflict', '完成请求已固定，请使用原提交内容和幂等键')
            if row['result']:
                return json.loads(row['result'])
            meta = json.loads(row['metadata'])
            chunks = [dict(r) for r in db.execute('SELECT * FROM media_upload_chunks WHERE upload_id=? ORDER BY chunk_index', (upload_id,))]
            count = math.ceil(meta['total_size'] / meta['chunk_size'])
            if len(chunks) != count or [c['chunk_index'] for c in chunks] != list(range(count)):
                raise DomainError('upload_incomplete', '还有分片尚未保存，请先恢复上传')
            db.execute('UPDATE media_uploads SET completion_hash=? WHERE id=?', (digest(request), upload_id))
        path = self.core.data_dir / 'media' / (uid() + meta['suffix'])
        try:
            sha = hashlib.sha256()
            with path.open('xb') as output:
                for chunk in chunks:
                    part = self._folder(upload_id) / f"{chunk['chunk_index']}.part"
                    try:
                        if part.is_symlink() or not part.resolve().is_relative_to(self.root.resolve()):
                            raise OSError('invalid chunk location')
                        data = part.read_bytes()
                    except OSError as exc:
                        self._invalidate_chunk(sid, upload_id, chunk['chunk_index'])
                        raise DomainError('upload_chunk_missing', '已保存分片缺失，请重新上传该分片', 422) from exc
                    if len(data) != chunk['size'] or hashlib.sha256(data).hexdigest() != chunk['sha256']:
                        self._invalidate_chunk(sid, upload_id, chunk['chunk_index'])
                        raise DomainError('upload_chunk_corrupt', '已保存分片校验失败，请重新上传原分片', 422)
                    sha.update(data)
                    output.write(data)
            if path.stat().st_size != meta['total_size'] or sha.hexdigest() != meta['sha256']:
                raise DomainError('upload_hash_mismatch', '完整媒体摘要不符，未提交回答', 422)
            from interview_media import inspect_media
            try:
                media_meta = inspect_media(path)
            except Exception as exc:
                raise DomainError('invalid_media', '媒体容器未通过解码检查，未提交回答', 422) from exc
            if not (media_meta.get('has_audio') or media_meta.get('has_video')):
                raise DomainError('invalid_media', '文件没有有效音视频轨道', 422)
            if not 0 < media_meta.get('duration_ms', 0) <= 180000:
                raise DomainError('invalid_duration', '音视频必须大于0秒且不超过3分钟', 422)
            result = self.core.submit_answer(sid, row['round_id'], row['attempt_id'], idempotency_key,
                                            transcript, path, media_meta, meta['sha256'], language, replace_answer_id)
            committed, _ = self.core.media_path(sid, result['answer_id'])
            if committed != path:
                path.unlink(missing_ok=True)
            with self.core._db(True) as db:
                db.execute("UPDATE media_uploads SET status='completed',result=? WHERE id=? AND session_id=?",
                           (dumps(result), upload_id, sid))
            return result
        finally:
            # A crash after answer commit must never delete the committed original.
            with self.core._db() as db:
                retained = db.execute('SELECT 1 FROM answers WHERE session_id=? AND path=?', (sid, str(path))).fetchone()
            if not retained:
                path.unlink(missing_ok=True)
