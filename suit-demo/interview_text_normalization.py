"""Versioned orthographic reading/matching projection; never a transcript edit.

Complete pinned OpenCC t2s dictionaries, with explicit longest-phrase matching.
Every mapping preserves Unicode codepoint length. Therefore a match [start:end]
in the simplified shadow refers to EXACTLY the same span in the original text.
No ASR correction, regional vocabulary replacement or model rewrite is performed.
"""
from __future__ import annotations
from functools import lru_cache
import hashlib
import json
from pathlib import Path

DATA_PATH = Path(__file__).with_name('config') / 'text-normalization.v1.json'
DATA_SHA256 = '7010aad86c98dca8a47e4ce661802644079ae06fa44543bdc64ea69dcff0f49c'
VERSION = 'opencc-t2s-cp.v1'


@lru_cache(maxsize=1)
def _load():
    raw = DATA_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DATA_SHA256:
        raise ValueError('text_normalization_data_hash_mismatch')
    data = json.loads(raw)
    if data.get('version') != VERSION:
        raise ValueError('text_normalization_version_mismatch')
    characters, phrases = data['characters'], data['phrases']
    for source, target in [*characters.items(), *phrases.items()]:
        if not source or not isinstance(target, str) or len(source) != len(target):
            raise ValueError('text_normalization_offset_invariant_failed')
    if any(len(key) != 1 for key in characters):
        raise ValueError('text_normalization_character_dictionary_invalid')
    by_first = {}
    for source, target in phrases.items():
        by_first.setdefault(source[0], []).append((source, target))
    for options in by_first.values():
        options.sort(key=lambda item: (-len(item[0]), item[0]))
    return characters, by_first


def normalize_for_match(text: str) -> str:
    """Return simplified shadow, retaining identity Unicode-codepoint offsets."""
    if not isinstance(text, str):
        raise TypeError('text_normalization_requires_string')
    characters, phrases = _load()
    output, index = [], 0
    while index < len(text):
        for source, target in phrases.get(text[index], ()):
            if text.startswith(source, index):
                output.append(target)
                index += len(source)
                break
        else:
            output.append(characters.get(text[index], text[index]))
            index += 1
    result = ''.join(output)
    if len(result) != len(text):
        raise ValueError('text_normalization_offset_invariant_failed')
    return result


def normalization_metadata():
    return {'version': VERSION, 'data_sha256': DATA_SHA256,
            'upstream': 'BYVoid/OpenCC ver.1.1.9',
            'upstream_commit': '556ed22496d650bd0b13b6c163be9814637970ae',
            'scope': 'orthographic_t2s_only',
            'original_transcript_unchanged': True,
            'offset_mapping': 'identity_unicode_codepoints',
            'matching_surface': 'simplified_shadow',
            'quote_surface': 'immutable_original'}
