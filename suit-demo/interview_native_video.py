"""Bounded sequential native-PTS sampling, including MediaRecorder VFR WebM.

Container frame-rate metadata is informational only. No resampling, frame
duplication, interpolation or FPS-derived frame index is used.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import deque
import hashlib
import math
from pathlib import Path


class NativeVideoSampler:
    VERSION = 'native-pts-sequential.v2'
    MAX_REQUESTS = 7

    def __init__(self, path, *, max_time_ms=181000, max_frames=50000):
        import av
        self.container = av.open(str(path), mode='r')
        videos = list(self.container.streams.video)
        if not videos:
            self.container.close()
            raise ValueError('native_video_track_missing')
        self.stream = videos[0]
        self.decoder = iter(self.container.decode(self.stream))
        self.max_time_ms, self.max_frames = max_time_ms, max_frames
        # Container origin retains audio/video offsets. Missing origin means
        # timestamps are already relative to the recording, not a made-up FPS.
        self.origin_ms = float(self.container.start_time / 1000) if self.container.start_time is not None else 0.0
        self.frames = deque()
        self.ended = False
        self.decoded_count = 0
        self.last_time = None
        self.last_request_start = -math.inf
        self.provenance = {
            'sampler_version': self.VERSION,
            'sampler_code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'timestamp_source': 'decoded frame PTS multiplied by stream time_base, minus container start_time',
            'timestamp_origin_ms': self.origin_ms,
            'reported_base_rate': str(self.stream.base_rate) if self.stream.base_rate else None,
            'reported_average_rate': str(self.stream.average_rate) if self.stream.average_rate else None,
            'container_fps_used_for_sampling': False,
            'resampled_input': False,
            'nearest_tie_policy': 'legacy even decoded frame index; only duplicate equal-nearest choices are repaired to a strictly increasing sequence, then minimize total timing error and interval error before even-index preference',
        }

    def _advance(self):
        if self.ended:
            return
        try:
            frame = next(self.decoder)
        except StopIteration:
            self.ended = True
            return
        except Exception as error:
            raise ValueError('native_video_decode_failed') from error
        self.decoded_count += 1
        if self.decoded_count > self.max_frames:
            raise ValueError('native_video_frame_limit')
        if frame.pts is None or frame.time_base is None:
            raise ValueError('native_video_timestamp_missing')
        at = float(frame.pts * frame.time_base * 1000) - self.origin_ms
        if not math.isfinite(at) or at < -1 or at > self.max_time_ms:
            raise ValueError('native_video_timestamp_out_of_bounds')
        if self.last_time is not None and at <= self.last_time:
            raise ValueError('native_video_timestamps_not_increasing')
        self.frames.append((at, frame, self.decoded_count - 1))
        self.last_time = at

    def _nearest_indices(self, times, requests):
        candidates, legacy = [], []
        for request in requests:
            right = bisect_left(times, request)
            neighbours = [i for i in (right - 1, right) if 0 <= i < len(times)]
            distance = min(round(abs(times[i] - request), 6) for i in neighbours)
            nearest = [i for i in neighbours if round(abs(times[i] - request), 6) == distance]
            candidates.append(nearest)
            legacy.append(min(nearest, key=lambda i: (self.frames[i][2] % 2, times[i])))
        # Leave every already-distinct legacy choice unchanged, including the
        # normal constant-rate MP4 path. Never select a more distant frame.
        if all(a < b for a, b in zip(legacy, legacy[1:])):
            return legacy
        if not any(len(options) > 1 for options in candidates):
            raise ValueError('native_video_temporal_density_insufficient')
        # A half-frame phase offset at exactly 8 Hz can make independent
        # even-index ties duplicate frames. Keep at most two DP states per
        # request (its left/right equal-nearest frame); at most four transitions
        # per stage and seven requests. No exponential path enumeration.
        paths = {}
        for index in candidates[0]:
            score = (round(abs(times[index] - requests[0]), 6), 0.0,
                     (self.frames[index][2] % 2,), (times[index],))
            paths[index] = (score, (index,))
        for position in range(1, len(requests)):
            next_paths = {}
            for index in candidates[position]:
                options = []
                for previous, (score, path) in paths.items():
                    if previous >= index:
                        continue
                    interval_error = abs((times[index] - times[previous])
                                         - (requests[position] - requests[position - 1]))
                    extended = (round(score[0] + abs(times[index] - requests[position]), 6),
                                round(score[1] + interval_error, 6),
                                score[2] + (self.frames[index][2] % 2,),
                                score[3] + (times[index],))
                    options.append((extended, path + (index,)))
                if options:
                    next_paths[index] = min(options, key=lambda item: item[0])
            if not next_paths:
                raise ValueError('native_video_temporal_density_insufficient')
            paths = next_paths
        return list(min(paths.values(), key=lambda item: item[0])[1])

    def sample(self, requests_ms, *, max_error_ms=62.5, max_frame_gap_ms=None):
        requests = [float(value) for value in requests_ms]
        if (not requests or len(requests) > self.MAX_REQUESTS
                or any(not math.isfinite(x) or x < 0 for x in requests)
                or any(b <= a for a, b in zip(requests, requests[1:]))
                or requests[0] < self.last_request_start):
            raise ValueError('native_video_request_order_invalid')
        self.last_request_start = requests[0]
        # Preserve the left neighbour; overlapping Gaze360 windows need only
        # about one second of decoded frames, not the complete recording.
        while len(self.frames) > 1 and self.frames[1][0] < requests[0] - max_error_ms:
            self.frames.popleft()
        while not self.ended and (not self.frames or self.frames[-1][0] <= requests[-1]):
            self._advance()
            while len(self.frames) > 1 and self.frames[1][0] < requests[0] - max_error_ms:
                self.frames.popleft()
        # One following native frame establishes the selected frame duration.
        # At EOF it stays unknown; no nominal-FPS duration is invented.
        if not self.ended:
            self._advance()
        times = [item[0] for item in self.frames]
        if not times:
            raise ValueError('native_video_frame_missing')
        selected = []
        for request, index in zip(requests, self._nearest_indices(times, requests)):
            at, frame, _ = self.frames[index]
            if abs(at - request) > max_error_ms + 1e-6:
                raise ValueError('native_video_sampling_gap')
            # A true low-frame-rate input must not pass merely because the
            # container claimed 1000 fps. Use real neighbouring PTS intervals.
            adjacent = []
            if index:
                adjacent.append(at - times[index - 1])
            if index + 1 < len(times):
                adjacent.append(times[index + 1] - at)
            if max_frame_gap_ms is not None and (not adjacent or max(adjacent) > max_frame_gap_ms + 1e-6):
                raise ValueError('native_video_temporal_density_insufficient')
            duration = times[index + 1] - at if index + 1 < len(times) else None
            selected.append({
                'frame': frame.to_ndarray(format='bgr24'),
                'at_ms': round(at, 3),
                'duration_ms': round(duration, 3) if duration is not None else None,
                'duration_source': 'next_decoded_pts' if duration is not None else 'unknown_at_stream_end',
            })
        return selected

    def close(self):
        self.frames.clear()
        self.container.close()
