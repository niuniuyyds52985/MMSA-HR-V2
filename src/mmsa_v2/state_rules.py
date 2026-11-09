"""Positive-expression thresholds on the native OpenFace intensity scale (0–5)."""

from typing import NamedTuple


class SmileThresholds(NamedTuple):
    upbeat_lip: float
    exuberant_lip: float
    exuberant_cheek: float


METHOD_V1 = "v2.observable-delivery.fer-au-audio.1"
LEGACY_METHOD = "v2.observable-delivery.fer-au-audio.2"
# New automatic analyses use direct AU prototypes plus independent speech emotion.
# Historical FER/AU rules retain their immutable version and thresholds below.
METHOD = "v2.multimodal-affect.au-ser.3"
SMILE_THRESHOLDS = {
    METHOD_V1: SmileThresholds(1, 2.5, 1),
    LEGACY_METHOD: SmileThresholds(0.75, 2.25, 0.75),
}
