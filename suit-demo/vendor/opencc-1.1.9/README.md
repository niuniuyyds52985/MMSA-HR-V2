# OpenCC t2s source data

Pinned official release ver.1.1.9, commit 556ed22496d650bd0b13b6c163be9814637970ae. Files are unmodified official source and Apache-2.0 license. SHA-256 and exact URLs are in SOURCE.json.

The app uses the complete TSCharacters and TSPhrases mappings, taking the first upstream target. An explicit local adapter performs longest forward phrase matching followed by character conversion. Source/target mixtures are generated position by position to preserve phrase exceptions when text is already simplified or mixed. This is a versioned local adapter using OpenCC data, not a claim that the full native OpenCC runtime is installed. No regional vocabulary substitution is enabled.

All 4113 character mappings, 278 original phrases, and 735 derived phrase forms preserve Unicode codepoint length. The Python loader enforces this invariant and the data hash. Canonical transcripts, sentence IDs, original quotes, timings and versions are not rewritten. Browser text is a reading projection; resolved citations always return original text.
