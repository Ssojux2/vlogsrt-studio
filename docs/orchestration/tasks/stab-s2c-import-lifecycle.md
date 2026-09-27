# STAB-S2C — Roughcut generation, import request and confirmation

Read [the common execution contract](stab-execution-contract.md) before edits; it fixes the required High Agency, mailbox, protected-file and verification workflow.

## Scope
GLM implements Python workflow/roughcut state, Swift runtime/importer/job/extension UI and tests after S2B. Sol owns docs/review. Read approved stabilization plan; current checkout, serial writer, explicit-path commits.

Impact: propagating | files<=14 | modules<=4 | boundary=shared-api

## Decisions and requirements
1. Writing validated roughcut XML transitions to `roughcut_generated`, never `roughcut_imported`. Track import request and confirmed completion separately, bound to source input revision, edit-plan/output hash and resulting project UID.
2. Sending an Apple Event/open-document request records `import_requested`; transport success alone is not proof the user has opened the intended project. Mark confirmed only from reliable matching project identity observation or an explicit UI confirmation with identity shown. A failed import remains generated/retryable with actionable error.
3. Retry import uses the same existing validated roughcut output without rerunning analysis or paid calls and without regenerating a differently identified project. Check hash, current selection/revision and cancellation before each request; stale or modified output requires review and must not be imported automatically.
4. Add app/extension action for retrying failed import and confirming/observing imported state. Surface generated/requested/confirmed/failed clearly. Duplicate requests use S2A gates and cannot send overlapping Apple Events. Do not silently mark legacy `roughcut_imported` entries as newly verified acceptance.
5. Retain source job/revision-to-result UID mapping for S4; support multiple jobs without a single global active-origin default. Store canonical project identity, not mutable display name. Protocol readers keep old state records readable while new writes express honest state.

## Acceptance
- Python command/entrypoint roughcut writes exact expected XML and only generated state, with zero unapproved deletions.
- Swift fake importer test: failure then retry invokes importer twice, engine once, analysis zero additional times; output hash and UID remain fixed.
- Transport success stays requested until matching UID confirmation; wrong project identity, changed output, stale selection, cancellation and duplicate requests cannot confirm/import.
- New/legacy Python and Swift protocol readers agree; run targeted/full required checks and record exact exits.

## Boundaries
No real FCP, API, keychain, original media or protected env/fixture access; no push/deployment. No product/test edits by Sol. Report commit SHA/files/commands and real-FCP acceptance as pending, then settle once.
