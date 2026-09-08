# Final Cut proxy-driven roughcut + SRT — binding specification
Date: 2026-09-08. Baseline: de17d875c2b3061153699ea0998aec698d74db06.
This is the accepted replacement of Downloads/final_cut_ai_vlog_video_analysis_srt_plan.md after 3 actual Claude Code Fable 5.1 high critique rounds and user decisions. This file is the product authority; docs/orchestration/README.md governs execution.

## User-locked outcome
Automatically create a conservative roughcut, reviewed afterward in Final Cut, and export original-linked FCPXML + synchronized separate SRT. Add local VAD with optional dependencies. Re-transcribe only retained audio at already-cut sentence boundaries. Preserve all source subtitle records. User authorizes implementation, independent parallel branches/worktrees, and Astra merging at phase boundaries.
No embedded captions/Motion titles, dialogue summarization, source-media deletion, or per-cut preapproval v1.

## Input and output
FCPXML1.14 flat primary storyline asset-clip/gap, repeated source instances, enabled=0 and srcEnable video/all. Accept .fcpxml and .fcpxmld/Info.fcpxml. Reject unresolved refs, unsupported versions, connected media/audio, compound/sync/multicam, retime, transitions, audio routing overrides and time-varying effects with XML location/reason, never silently flatten.
Normal-speed matching source/project CFR: 24,25,30,50,60,24000/1001,30000/1001,60000/1001 via parameterized fixtures. Preserve supported static attributes/resources/UID; new project identity, tcStart=0, same source order/gaps and disabled durations. Source project is prepared before music/titles/effects.
Explicit media-map overrides XML media-rep original/proxy paths. No filename-only matching. Missing proxy -> frame sampling from original; unresolved original blocks ready package. Original audio always for STT/VAD, proxy only images. FCP DB untouched.
Outputs share one EditPlan revision: VLOG_AI_EDIT.fcpxml, VLOG_AI_EDIT.srt, VLOG_AI_SOURCE.srt, project_trace.json, review.json. Include review markers on generated XML. Record removed source ranges/restoration mapping and all uncertainty. Atomic staging/publication. Draft may have explicit unresolved warnings, never labeled final acceptance.
Final Cut SRT import: Relative to Timeline. SOURCE/EDIT SRT are elapsed zero-based. Reproject after user recuts, before unsupported connected music/effects.

## Source contracts and architecture
Reuse existing Whisper/repair/refine/polish engine and public defaults. Source Trace/spine/cues/SRT are immutable to project operations. No verify.py output mutation; project protection does not depend on verify on/off. All subtitle text/timestamps come from Whisper segments, including separate boundary traces; no prompt/forced alignment/word timing/ratio text split added.
New project package separates frozen models, media/XML, timing/captions, speech/vision, edit policy, orchestration/state. No premature ABC/Protocol.
Core direct dependencies remain openai,tomli-w,rich; optional project extra numpy,onnxruntime. Add scoped invariant exceptions/documented source-vs-derived contracts before project changes. Existing checks remain active; legacy source modules cannot import project implementation. API calls remain transcribe.py; ffmpeg/ffprobe remain preprocess.py; explicitly allow xmllint-only subprocess in XML validation boundary (existing checker bans all subprocess elsewhere). No bypass via renaming.
Original config limits remain chunk_max_seconds180, chunk_max_bytes24000000, transcribe_temperature0.6; repair/polish/verification defaults preserved. Offline synthetic fixtures only during development; source API fixtures/golden/.env untouched. Existing user edit to .env.example is excluded from all agent changes/commits/merges.
All new tunables in project config. Model names only config. Existing output behavior with same offline fixtures must remain compatible.

## Source analysis, clocks, identity
Transcribe each selected unique source file FULLY once, using explicit per-asset work/fixture namespace rather than process-global env mutations/stem-only keys. Typed success/empty/error and per-chunk coverage. EmptyTranscript(RuntimeError) preserves existing source error behavior while adapter records successful empty response. Missing segments/malformed response/call failure NEVER imply empty/no speech.
Persistent original16k PCM reference with hash, audio stream/channel policy and measured audio_origin. S17: reproduce last/intermediate/repair chunk drift against decoded PCM; correct measured origins/splitting, never magic +0.2s. Audio sync acceptance including boundary snippets gated on this result; XML parser work is independent.
file_time=audio_time+original_audio_origin.
input_project_elapsed=clip.offset-sequence.tcStart+asset.start+file_time-clip.start.
XML times Fraction; convert legacy floats via Fraction(str(value)), no early ms quantization. Source frame grid anchored to asset.start; output integer frames, intervals [start,end). Duration/order/no-overlap/ref checks beyond DTD. Untouched XML time strings preserved. Output SRT round half-up to ms ONCE at final serialization; same boundary same value. Adjacent cues may satisfy previous.end==next.start; reject previous.end>next.start overlaps and individual cue.end<=cue.start invalid durations. Clip-end cue uses exact slice endpoint; don't invent 1frame gaps.
IDs: resource IDs scoped to input XML hash, source content identity separate from clip instance. Original file content+audio selection/config in strong cache keys. Reexport rebinds by verified resource identity/content, never r2 or filename alone.

## Boundary transcripts
Padding-only cue trimming: if all referenced Whisper segments are contained, clamp presentation without retranscription. Any true source segment truncation at existing input cuts -> boundary task; no guessed tolerance for omitted words.
Logically coalesce adjacent slices only if same source content, contiguous file AND timeline ranges, same audible policy; preserve authored XML blade boundaries.
Grow boundary span to fixed point of overlapping cue/segment dependencies, bounded by retained logical slice; merge only compatible requests. Extract from the SAME stored PCM, start=ceil(sample index), end=floor(sample index), thus only retained audio. Record actual sample origin. Existing chunk/repair/refine rules process extraction; no additional context from removed audio.
BoundaryTranscript immutable; key includes original hash, retained sample range, channel policy, source Trace ref and model/config. Projection replacement keyed by (logical slice/instance, source interval); no deletion of other repeated instances. Use boundary trace INSTEAD OF overlapping old cues, not append.
Never recursively retranscribe a BoundaryTranscript due to its own endpoint; flagged cut_truncated is terminal. Input instances requiring boundary retranscription locked against additional automatic shortening in that run.
On failure keep original text in review; a clamped original cue may remain in explicit unresolved draft with marker. Final human acceptance must resolve flagged dialogue. No silent missing captions.

## VAD guard
Pinned Silero v6.2.1 ONNX artifact+SHA256+license, CPU numpy/onnxruntime; no torch.16k source PCM; separate audible mono/stereo channels then union (avoid phase cancellation).512sample steps,64sample context, recurrent state continuous perasset/channel, reset between assets/channels. Last partial window zero-pad for inference, exclude padding from time and conservatively protect real tail.
Default positive>=0.35; no min250ms speech dropping. Protect union of ALL Whisper segments/source cues/VAD/repair/failed or uncertain coverage, plus0.5s outward padding; map source guard into each instance with outward frame floor/ceil. Empty STT alone not silence. VAD speech without usable STT keep+review. Dependency/model/hash absence blocks automatic analysis; perasset runtime error keep+review, never downgrade silently to STT-only.
VAD is probabilistic; no universal zero speech-loss claims. Quantify protected proportion and audit every removed audio interval on pilot.

## Visual scoring and deterministic roughcut
Config model glm-5.3-flash (distinct from implementation GLM-5.3). Official image_url/Base64 support verified; live account capability not yet tested. Sample proxy long-edge768; HDR/Log no color/exposure ranking absent validated display conversion. Sparse images not proof of motion/blur/no speech.
Eligible runs: complements of guard within each original clip, all analysis succeeded, length>=6s. Partition into <=10s blocks: n=ceil(L/max); balanced integer-frame lengths if every block>=min, otherwise greedy max and keep short remainder. Frame-aligned minceil6s/maxfloor10s/keepnearest3s. Generate beginning/middle/end keep candidates, dedup.
Whole block sampled1fps plus block endpoints and3 candidate centers, unique frame IDs, <=16 images. Include adjacent scene/transcript context. AI chooses candidate ID OR keep_all/uncertain, reason/evidence/confidence. confidence>=0.8 only for repetitive ordinary B-roll; unique events/uncertainty keep_all. Reject unknown IDs/NaN/invalid structure. Model gets no XML/time/path/mute authority.
Apply at mostone candidate/block: retain3s and ripple remove rest; preserve source audio/order/existing gaps, no fullclip deletion or dialogue shortening. No newly introduced partial source/derived cues; validate actual snapped cuts against guard after selection.
Plan/export pure and deterministic from saved analysis/config, not temperature-based determinism promise. API failures preserve affected block and explicit review; no silent model replacement.

## Persistence, CLI and UI
Workspace immutable content-keyed analysis reusable across resumes/reproject; no implicit global/cross-workspace cache. JSON manifests + atomically written completed artifacts (no SQLite v1). Save input/config/model/response/plan revisions, error states and cost/usage. Stale plan or mixed output versions rejected. Cancellation stops scheduling and checkpoints completed requests. No exactly-once billing guarantee for lost responses.
Estimate unique full source audio plus repair/verify/boundary and image request budgets before actual calls. Live STT/vision fixture recording stays a human step per repository rules; requested coding-model calls are explicitly authorized by the user.
New project inspect/analyze/plan/export/captions/run. Only analyze/runtime human-authorized online work may call network. plan/export/captions no network. captions reads updated supported timeline, reuses source and cached boundaries, never re-runs vision/edit policy; uncached boundary/new source -> needs_analysis and explicit analysis pass. Existing captions may be ignored for this read-only SRT regeneration with replace-existing-SRT notice, but not connected spoken audio/transitions.
Existing Gradio local-path workflow remains. Add separate lightweight project input/status/result flow after CLI end-to-end; no browser video upload, full NLE or mandatory hand-edited JSON.

## Phases and acceptance
P0 developer environment on available Python3.12.14, preserve runtime>=3.11 unless justified; source/derived contract exceptions and baseline makecheck.
P1 identity FCPXML/bundle/media/time roundtrip. P2 S17/PCM sync independent of P1; both precede audio sync acceptance.
P3 perasset source adapter and SOURCE SRT. P4 boundary transcription/projection.
P5 VAD and vision may run as independent branches after shared contracts. P6 deterministic edit/shared export. P7 persistence, CLI, UI/reproject integration and actual final acceptance.
Sol decomposes each phase into small independent tasks with exact interface/file ownership and tests; no cross-task symbol guesswork. Phase merges by Astra after review and integration checks, not by GLM/Sol.
Acceptance: source immutability, same-name/repeated asset/nonzero epochs/muted/disabled/gaps, all fps synthetic incl30vs29.97, correct PTS/sample origins, no empty/error conflation, continuous blade no extraSTT, instance-scoped boundary replacement no recursive tasks,0frame identity mismatch, <=1ms pure serialization error/no cumulative drift, no source guard removal/new partial cues, safe fixture must actually shorten, malformedvision/modelmissing/429/timeout/cancel/resume/staleplans, corrected user cuts reproject.
Pilot2min then10-20min actual FCP import-reexport, original4K relink/playback, faint/short/noisy speech and all removed audio audited. SKIP/blocked !=PASS.

## Evidence at handoff
Baseline source commit matches original doc. Original plan SHA2568b09883ece2d9542119e9594fcb1acf52ffedc137d14763e993bfd22ad2e3e8f.
Local Final Cut12.3 + FCPXML1.14 DTD; synthetic XML with original/proxy/gap/repeated disabled clips passed xmllint. Existing16 invariant groups passed under python3.12.
At the initial design handoff, full makecheck was unrun because dev dependencies were absent. Execution bootstrap restored Python3.12.14 via uv, installed .[dev], and passed make check (389 tests, Ruff, mypy, all16 invariant groups); uv pip check confirmed30 compatible packages. Sandbox localhost/AppleScript failures disappeared when rerun outside sandbox. No actual FCPXML/media sample import, production STT/GLM-Flash or VAD artifact verification claimed.
Sources: https://docs.z.ai/guides/vlm/glm-5.3-flash ; https://developer.apple.com/documentation/professional-video-applications/fcpxml-bundle-reference?changes=_3 ; https://support.apple.com/en-ca/guide/final-cut-pro/ver4185ef95a/mac ; https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/src/silero_vad/utils_vad.py
