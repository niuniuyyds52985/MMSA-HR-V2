"""Offline measurements for recorded simulation answers, in answer-relative seconds."""

from hashlib import sha256
import math
from pathlib import Path
import sys
from threading import BoundedSemaphore


LEGACY = Path(__file__).resolve().parents[2] / "suit-demo"
if str(LEGACY) not in sys.path:
    sys.path.append(str(LEGACY))

_SLOTS = BoundedSemaphore(1)
MAX_SECONDS = 1800
WINDOW_SECONDS = 5
def _container(path):
    import av
    try:
        return av.open(str(path), options={"protocol_whitelist": "file,pipe",
                       "format_whitelist": "mov,matroska,webm,wav,mp3,ogg,aac,flac"})
    except av.FFmpegError as exc:
        raise ValueError("媒体无法解码，请使用有效的本机录音或录像") from exc


def media_info(path):
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= 100 * 1024 * 1024:
        raise ValueError("录制文件须为非空且不超过 100 MB")
    import av
    with _container(path) as container:
        has_audio = bool(container.streams.audio)
        has_video = bool(container.streams.video)
        if not (has_audio or has_video):
            raise ValueError("没有可解码的音频或视频轨道")
        origin = (container.start_time or 0) / av.time_base
        seconds = (container.duration or 0) / av.time_base
        if not seconds:  # MediaRecorder WebM, including files without a duration header.
            for packet in container.demux():
                if packet.pts is not None and packet.time_base:
                    seconds = max(seconds, float((packet.pts + (packet.duration or 0)) * packet.time_base) - origin)
                if seconds > MAX_SECONDS:
                    break
    seconds = round(seconds, 3)
    if not 0 < seconds <= MAX_SECONDS:
        raise ValueError("每段录制应在 0 至 30 分钟内")
    return {"duration_seconds": seconds, "has_audio": has_audio, "has_video": has_video}


def _audio_samples(path, seconds):
    import av
    import numpy as np
    # Preserve gaps and the shared timeline. The duration check bounds the buffer.
    samples = np.zeros(math.ceil(seconds * 16000), dtype="<i2")
    with _container(path) as container:
        origin = (container.start_time or 0) / av.time_base
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        cursor = 0
        def store(frame):
            nonlocal cursor
            start = round((float(frame.time) - origin) * 16000) if frame.pts is not None else cursor
            raw = frame.to_ndarray().ravel()
            left, right = max(0, start), min(len(samples), start + len(raw))
            if right > left:
                samples[left:right] = raw[left-start:right-start]
            cursor = start + len(raw)
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                store(converted)
        for converted in resampler.resample(None):
            store(converted)
    return samples


def analyze_recording(path, *, transcribe=True):
    """Return measurable evidence and ASR. The reviewer labels staged performance in the UI."""
    if not _SLOTS.acquire(blocking=False):
        raise ValueError("本机正在处理另一段录制，请稍后重试")
    try:
        return _analyze(Path(path), transcribe)
    finally:
        _SLOTS.release()


def _analyze(path, transcribe):
    import numpy as np
    import webrtcvad
    from interview_asr import FAST_ENGINE, transcribe as transcribe_local

    info = media_info(path)
    seconds = info["duration_seconds"]
    rows = [{"start": start, "end": min(start + WINDOW_SECONDS, seconds)}
            for start in range(0, math.ceil(seconds), WINDOW_SECONDS)]
    transcript, notices = [], []
    if info["has_audio"]:
        vad = webrtcvad.Vad(2)
        audio = _audio_samples(path, seconds)
        for offset in range(0, math.ceil(seconds), 60):
            pcm = audio[offset*16000:(offset+60)*16000].tobytes()
            signal = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768
            voiced_count = 0
            for at in range(0, len(signal), WINDOW_SECONDS * 16000):
                index = (offset + at // 16000) // WINDOW_SECONDS
                if index >= len(rows):
                    break
                sample = signal[at:at + WINDOW_SECONDS * 16000]
                frames = [pcm[i:i+640] for i in range(at*2, at*2+sample.size*2-639, 640)]
                voiced = sum(vad.is_speech(frame, 16000) for frame in frames)
                voiced_count += voiced
                levels = [20 * math.log10(max(float(np.sqrt((block**2).mean())), 1e-8))
                          for block in np.array_split(sample, max(1, len(sample)//1600))]
                rows[index].update(energy_spread_db=round(float(np.percentile(levels, 75) - np.percentile(levels, 25)), 2),
                                   rms_dbfs=round(20 * math.log10(max(float(np.sqrt((sample**2).mean())), 1e-8)), 2),
                                   speech_fraction=round(voiced/len(frames), 4) if frames else None)
            if transcribe and voiced_count:
                try:
                    result = transcribe_local(signal, "zh", FAST_ENGINE)
                    for seg in result["segments"]:
                        start = max(offset, min(seconds, offset + float(seg["start"])))
                        end = max(start, min(seconds, offset + float(seg["end"])))
                        if end > start and seg["text"].strip():
                            transcript.append({"start": round(start, 3), "end": round(end, 3),
                                               "text": seg["text"].strip()})
                except (RuntimeError, OSError, ValueError, ImportError):
                    notices.append(f"{offset} 秒起的片段转写失败，可在回听后补充原话")
    if info["has_video"]:
        import cv2
        from interview_visual import _load_detector, _detect_boxes
        detector, reason = _load_detector(cv2)
        if reason:
            notices.append(reason)
        import av
        with _container(path) as container:
            origin = (container.start_time or 0) / av.time_base
            previous_second = -1
            last_face = None
            changes = [[] for _ in rows]
            for frame in container.decode(video=0):
                if frame.pts is None:
                    continue
                second = math.floor(frame.time - origin)
                index = second // WINDOW_SECONDS
                if second <= previous_second or not 0 <= index < len(rows):
                    continue
                h = max(1, round(320 * frame.height / max(1, frame.width)))
                thumb = frame.reformat(width=320, height=min(640, h), format="bgr24").to_ndarray()
                gray = cv2.cvtColor(thumb, cv2.COLOR_BGR2GRAY)
                boxes = _detect_boxes(detector, thumb) if detector is not None else []
                row = rows[index]
                count = len(boxes) if detector is not None else None
                row["frame_luma"] = round(float(gray.mean()), 1)
                if "visible_faces" not in row or count != 1:
                    row["visible_faces"] = count
                current = None
                if len(boxes) == 1:
                    b = boxes[0]
                    x,y = b["x"],b["y"]
                    patch = gray[y:y+b["height"], x:x+b["width"]]
                    current = cv2.equalizeHist(cv2.resize(patch, (32,32)))
                    if last_face is not None and previous_second // WINDOW_SECONDS == index and second == previous_second + 1:
                        changes[index].append(float(cv2.absdiff(current,last_face).mean()) / 255)
                last_face, previous_second = current, second
            for row, differences in zip(rows, changes):
                row["face_region_motion"] = round(float(np.median(differences)), 4) if differences else None
    with path.open("rb") as stream:
        import hashlib
        digest = hashlib.file_digest(stream, sha256).hexdigest()
    return {**info, "media_sha256": digest, "status": "awaiting_annotation", "method": "offline-observations.v2.1",
            "transcript": "".join(s["text"] for s in transcript), "transcript_segments": transcript,
            "observations": rows, "segments": [], "notices": notices,
            "label_source": "waiting_for_teacher", "network_used": False}


def propose_states(target, examples):
    """Suggest labels for staged simulation takes only after a held-out recording check.

    Only manually annotated reference intervals can train the demonstration.
    The caller retains suggestions separately from confirmed scoring intervals.
    """
    import numpy as np
    from .scoring import STATE_POINTS

    def features(row):
        values = [row.get(k) for k in ("speech_fraction", "rms_dbfs", "energy_spread_db","face_region_motion")]
        if any(v is None or not math.isfinite(v) for v in values) or values[0] <= 0:
            return None
        if row.get("visible_faces") != 1:
            return None
        return values

    result = {"method": "staged-five-state.references.v1", "segments": [], "validation": {},
              "reason": "需要每种状态至少两段不同模拟录像的人工标注", "review_required": True}
    samples = {}
    for source, labels in examples:
        digest = source.get("media_sha256")
        if not digest or digest == target.get("media_sha256") or digest in samples:
            continue
        take = []
        for row in source.get("observations", []):
            x = features(row)
            if x is None:
                continue
            for label in labels:
                if label["start"] <= row["start"] and label["end"] >= row["end"] and label["state"] in STATE_POINTS:
                    take.append((x, label["state"]))
                    break
        if take:
            samples[digest] = take
    if any(sum(any(y == label for _, y in take) for take in samples.values()) < 2 for label in STATE_POINTS):
        return result

    def model(takes):
        pairs = [pair for take in takes for pair in take]
        vectors = np.asarray([x for x, _ in pairs])
        spread = np.maximum(np.std(vectors, axis=0), [0.1, 3, 2, 0.03])
        centers = [np.median([x for x, y in pairs if y == state], axis=0) for state in STATE_POINTS]
        def predict(x):
            distances = np.linalg.norm((np.asarray(centers) - x) / spread, axis=1)
            return list(STATE_POINTS)[int(distances.argmin())], float(distances.min())
        return predict

    hits = {label: [] for label in STATE_POINTS}
    for take_id, held_out in samples.items():
        predict = model(take for key, take in samples.items() if key != take_id)
        for x, expected in held_out:
            hits[expected].append(predict(x)[0] == expected)
    recalls = {state: sum(h)/len(h) for state, h in hits.items()}
    result["validation"] = {"recordings": len(samples), "recall_by_state": recalls,
                            "method": "leave_one_recording_out", "minimum_each_state": 0.6}
    if min(recalls.values()) < 0.6:
        result["reason"] = "留出录像验证未达到本演示的分类门槛，需增加标注或直接复核"
        return result
    predict = model(samples.values())
    for row in target.get("observations", []):
        x = features(row)
        if x is not None:
            state, distance = predict(x)
            if distance <= 2.5:
                result["segments"].append({"start": row["start"], "end": row["end"], "state": state})
    result["reason"] = "仅供模拟片段复核，确认后的时段进入计分"
    return result
