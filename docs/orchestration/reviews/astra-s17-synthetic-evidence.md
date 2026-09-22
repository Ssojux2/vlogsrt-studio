# Synthetic MP3 copy-segment timing evidence

Measured twice by Astra on 2026-09-09 KST, FFmpeg 9.0.1, Python 3.12.14.
The unchanged legacy `vlogsrt.preprocess.split_at` was imported from the integration
checkout. All media was generated under temporary directories and removed when
the experiment exited. No source code, protected media, fixtures, or env files changed.

## Reproduction

1. Generate 192,000 mono signed-16 samples at 16 kHz (12 seconds), initially zero.
   Set sample `second * 16000 + 8000` to 24000 for each integer second 0 through 11.
2. Encode the raw bytes with `ffmpeg -v error -f s16le -ar 16000 -ac 1 -i input.s16
   -c:a libmp3lame -b:a 64k input.mp3`. This matches the legacy rate/channel/bitrate defaults.
3. Call `split_at(input.mp3, [2, 4, 6, 8, 10], workdir)` and separately
   `split_at(input.mp3, [2, 4], other_workdir)`.
4. Decode the full MP3 and each resulting chunk with `ffmpeg -v error -i INPUT
   -f s16le -ar 16000 -ac 1 -`. Interpret bytes as signed-16 samples on this
   little-endian machine. Locate the strongest impulse in each expected window.
5. Compare `Fraction(str(chunk.offset)) * 16000 + chunk_peak_index` against
   the corresponding full-decode peak sample index. Fraction arithmetic is used
   for the comparison; no offset correction is applied.

The full decode contains exactly 192,000 samples. Its impulse peaks are exactly
`[8000, 24000, 40000, 56000, 72000, 88000, 104000, 120000, 136000, 152000,
168000, 184000]`, matching the synthetic input positions.

## Observed result

For the six-chunk case:

| Chunk | CSV offset (s) | Decoded samples | First peak index | Mapped first peak (s) | Error (samples) |
| --- | --- | --- | --- | --- | --- |
| 0 | 0 | 31151 | 8000 | 0.500 | 0 |
| 1 | 2.016 | 31727 | 8320 | 2.536 | +576 |
| 2 | 4.032 | 31151 | 8064 | 4.536 | +576 |
| 3 | 6.012 | 31727 | 8384 | 6.536 | +576 |
| 4 | 8.028 | 31151 | 8128 | 8.536 | +576 |
| 5 | 10.008 | 32448 | 8448 | 10.536 | +576 |

All later impulses have the same +576-sample / +36-ms mapped error. A second
independent temporary-media run asserted the full peak positions and every row's
error, confirming the result. Direct byte slicing of the decoded full PCM matches
the same full-PCM sample region exactly; that control is evidence for the coordinate
choice, not a claim that the future project helper has already been implemented.

With cuts `[2, 4]`, the last chunk starts at the same CSV offset 4.032 seconds,
has 128,064 decoded samples, and has first peak 8064. Its mapped peaks are 4.536,
5.536, ..., 11.536 seconds. Making this offset final instead of intermediate did
**not** change its observed peak displacement.

## Scope of conclusion

This reproduces a synthetic MP3 copy-segment coordinate mismatch using the
legacy defaults. It does not establish the historical 77-minute S17 report's
exact root cause, reported magnitude, or final-versus-intermediate difference.
Those historical specifics remain **not reproduced / PENDING_HUMAN**.
The experiment does not isolate which packet/decoder metadata causes the shift.

P2-03 should preserve the honest distinction and independently exercise the new
PCM sample-index range implementation, including final and repair ranges. Keep
legacy source behavior unchanged. Do not encode 36 ms, 576 samples, 0.2 seconds,
or any other measured constant as a timing correction.
