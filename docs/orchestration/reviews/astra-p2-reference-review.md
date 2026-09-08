# Astra P2-02 reference review

Date: 2026-09-09 KST. Status: accepted and integrated after the bounded
correction chain below. Earlier candidate dispositions are historical.

Initial candidate: `bc034a557afc6a85baf671ea229d67aa1601ef79`.
Follow-up candidate: `28fc8c3e8a84a5627a9a53a0d17b3bc7d6311a65`.
Base: `5ea9719cb2fb3ece20ad228fa78481ae60bbdb3b`.

The initial code accepted equal-length changed PCM in both `write_wav_slice` and
`quiet_sample`; root reproduced both with a 100-frame synthetic raw file and a
previously recorded hash. Its publication order also replaced PCM before writing
metadata, accepted source/destination collisions, and allowed malformed sidecar
fractions to escape invalid-cache handling. Sol grouped these with constructor,
original identity, and public-interface corrections in the same GLM Dispatch.

The follow-up adds the content and path checks but still replaces the previous
PCM before `_write_sidecar`. Root reproduced the remaining failure using two
temporary original files with real hashes and a mocked 100-frame mono extractor:

1. Build a valid first reference containing 100 samples of value one.
2. Request a different original at the same destination, returning value two.
3. Inject `OSError` at metadata publication.
4. Compare the old reference's path hash and try to slice it.

Measured results: **previous PCM preserved: false**; **previous reference usable:
false**, because slicing raises a hash mismatch. No real media was used.

The follow-up metadata-failure test corrupts the old PCM before the attempted
replacement, so it does not exercise preservation of a previously valid pair.
Rejecting a mismatched pair and extracting again is not preservation of an
immutable reference. The next correction must keep the old reference usable
without re-extraction after any failed new publication, and must distinguish a
single atomic commit point from a sequence of individual file replacements.

## Publication follow-up cc079a8: not accepted

The rollback revision passed Sol's one-shot exception probe, but moving the old
PCM away before publication leaves existing references unavailable. Astra ran an
independent temporary-file child-process probe: publish a valid old reference,
start a changed-source build, then `os._exit(79)` immediately after parking the
old PCM. The parent observed `old_reference_path_exists=False`; slicing the old
reference raised `RuntimeError`. No process-finally block can repair that exit.
The change also narrowed rename failure injection to its first occurrence;
persistent write/rename failure cannot be dismissed as unrealistic.

Astra froze the final correction direction in `msg_34a6d6780804`: the caller's
destination is a logical cache address, new generations are immutable, and one
manifest/pointer replacement publishes a completely verified generation.
`PcmReference.path` must name the immutable PCM generation. Existing generations
remain usable after failed and successful updates. Public signatures stay the
same; no backup/rename sequence is accepted as an atomic publication substitute.
GLM owns the correction and tests through the existing Sol Dispatch.

Opus independently reproduced the same abrupt-exit failure and a persistent
rename failure (`msg_8fdce4771dbc`), supporting rejection. The final implementation
pins (`msg_2023f8c8883e`, acknowledged by Opus in `msg_6928671a60eb`) require exclusive
generation creation and no overwrite, not merely hash-derived directory names.
There is no generation pruning/garbage collector in v1 P2/P7. The guarantee is
atomic visibility and process-exit preservation, not durability across power loss.

## Generation follow-up a4b5633: one commit-boundary correction

At `a4b56339bcf0e23f34b4099eb50fed8f852a96e7`, the immutable-generation architecture
closes prior-reference loss. All 107 focused PCM tests passed with the worker
checkout explicitly on `PYTHONPATH` (the borrowed root editable venv otherwise
makes the test's child process import the not-yet-integrated root checkout).

Astra independently injected `PermissionError` while cleaning the pointer temp
file after a successful pointer replace. `_extract_reference` still considered
the operation unpublished and its finally deleted the newly referenced files:
`pointer_changed=True`, `published_generation_exists=False`, while
`prior_reference_exists=True`. A post-commit exception must not turn the visible
pointer into an incomplete pair. The same shape exists for interruption between
pointer publication and the local published flag.

The bounded correction sent in `msg_c8cfc81309e2` removes destructive generation
cleanup when publication may already have occurred; retaining private attempted
generations is permitted by the frozen v1 contract. A focused post-commit failure
test must prove that every visible pointer still selects a complete valid pair.

## Final acceptance

Final worker SHA: `b56c64069058b550ffbbb643589b4c2efbeddff2` (parent `aa7713f`).
Destructive generation cleanup is removed; post-commit pointer-temp `OSError`
is best effort. Astra independently repeated the narrow PermissionError and
KeyboardInterrupt injections and immediately checked both visible PCM/metadata
and the held old reference. Both pairs remained valid and sliceable without
rebuild; ordinary cleanup failure returned success, while interruption propagated.

Root direct full check on the exact worker SHA passed **1,017 tests**, Ruff
lint/format, mypy, and 19 invariant groups. Opus independently closed all four
publication cases and approved the exact SHA in `msg_3939175c24ae`, superseding
its withdrawn earlier approval. Sol final approval is `msg_c293e97d143f`.

Astra integrated it as `35af34b4cdab2e0d96af2b8f6a5530c764e78d12`. Direct
integrated `make check` passed **1,156 tests**, Ruff lint/format (56 files),
mypy (29 source files), and all 19 invariant groups.

P2-02 offline persistence and slicing is complete. P2-03 still owns the retained
range/S17 regression join. Real camera audio and the historical 77-minute incident
remain pending external acceptance. No power-loss durability or generation
garbage-collection guarantee is claimed.
