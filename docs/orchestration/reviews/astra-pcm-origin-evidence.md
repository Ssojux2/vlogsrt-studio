# Astra offline PCM origin experiment

Measured on 2026-09-08 with installed FFmpeg 9.0.1. This is engineering evidence
for P2-01, not an implementation or a claim that historical S17 or real-media
acceptance passed. All media lived in temporary directories and was removed.

## Method

Generated one second of 16 kHz signed-16 stereo PCM, otherwise zero, with impulses
at per-channel sample frame 4000: left 20000, right -12000. Thus the known event is
at source-audio time 1/4 second. Muxed a 16x16, 25 fps synthetic video with audio
timestamps shifted using `asetpts=PTS+(offset)/TB`, PCM or AAC audio, and
`-avoid_negative_ts disabled`. PCM used MOV/MKV; AAC used MOV/MKV as below.

Decoded the actual selected audio through this command shape, preserving channel
count and without `-start_at_zero` or timestamp-normalizing `asetpts`:

```text
ffmpeg -hide_banner -loglevel info -copyts -i MEDIA -map 0:a:0 -vn
  -af aresample=16000,aformat=sample_fmts=s16,asettb=1/16000,ashowinfo
  -c:a pcm_s16le -f s16le OUTPUT.pcm
```

Read the integer `pts` from the first post-conversion `ashowinfo` frame and divided
by 16000 exactly. Located the largest absolute left-channel decoded sample.
This only exercised the chosen pipeline; implementation must validate parsing,
sample counts, channel metadata, missing timestamps, and continuity explicitly.

## Measured results

| Codec/container | Authored offset | ffprobe stream start | First packet PTS | Decoded sample-zero time | Output frames/channel | Peak frame | Peak file time |
|---|---:|---:|---:|---:|---:|---:|---:|
| PCM/MOV | +2 s | 2 | 2 | 2 | 16000 | 4000 | 9/4 |
| AAC/MOV | +2 s | 1.936 | 1.936 | 242/125 | 17408 | 5024 | 9/4 |
| AAC/MOV | 0 s | 0 | -0.064 | 0 | 16000 | 4000 | 1/4 |
| PCM/MKV | -1/5 s | -0.200 | -0.200 | -1/5 | 16000 | 4000 | 1/20 |
| AAC/MKV | -1/5 s | -0.264 | -0.264 | -1/5 | 16384 | 4000 | 1/20 |

PCM/MOV output was also byte-identical to the generated input PCM. AAC output is
lossy and was not claimed byte-identical. Every observed peak maps to the known
authored file time with `sample_zero + peak_frame/16000`.

The negative AAC/MKV case is the decisive counterexample to using raw
`stream.start_time` as decoded sample-zero time: it differs by 64 ms. The positive
AAC/MOV case also shows why blindly subtracting a fixed codec delay is wrong:
priming samples remain in that decoded output and its correct origin is 1.936 s.
Use the measured output clock, not either constant compensation.

## Limits and next task

P2-01 GLM should turn a small representative subset into owned meaningful tests,
including positive/negative epochs and decoder priming. These experiments do not
cover resampling from 44.1/48 kHz, unusual edit lists, missing/discontinuous audio
PTS, variable channel layouts, real camera media, or the historical 77-minute S17
recording. Preserve those distinctions in reports. All-frame continuity should
be checked before treating a full PCM file as one constant-origin clock.

The command choice follows FFmpeg's documented timestamp-preservation option and
audio-frame reporting filter; the table above is local experimental evidence.
References: [FFmpeg timestamp options](https://ffmpeg.org/ffmpeg.html#Advanced-options),
[ashowinfo filter](https://ffmpeg.org/ffmpeg-filters.html#ashowinfo),
[asettb filter](https://ffmpeg.org/ffmpeg-filters.html#settb_002c-asettb).
