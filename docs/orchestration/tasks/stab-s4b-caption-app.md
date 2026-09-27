# STAB-S4B — Share-to-job mapping, estimate approval and SRT delivery UI

## Scope
GLM connects app share handling, core ShareInbox/job mapping, extension/app caption UI and tests after S4A. Sol owns docs/review. Read approved plan and S2C origin/result mapping. Current checkout, product/tests only.

Impact: propagating | files<=14 | modules<=4 | boundary=shared-api

## Decisions
1. Resolve received WAV/FCPXML by the final project UID to the persisted original roughcut job/revision mapping. Remove reliance on the never-populated global `activeOriginRevision`. Multiple jobs and app relaunch must preserve associations. Unknown/ambiguous UID presents a clear selection/error path, never attaches silently to the last job.
2. Share receipt validates and stages an immutable export pair, invokes caption-estimate, displays final duration and independent cost, then waits for explicit approval. Approval writes the exact estimate-bound authorization and invokes caption-run through normal runtime/preflight/locks. Do not run paid captions on receipt alone.
3. Show running/failed/retry/succeeded/no-speech states automatically. Present SRT reveal/open action, final warnings and the instruction `Final Cut Pro: File > Import > Captions > Relative to Timeline`. Never send an SRT import Apple Event.
4. Failed jobs can retry without restaging or losing valid approval; altered export creates a new revision and requires fresh estimate/approval. Reject duplicate/partial inputs and leave no usable half-staged job; on relaunch recover durable state without automatically repeating ambiguous paid work.

## Acceptance
Integration through the actual share-handler service/controller: two different roughcut jobs and exported UIDs map correctly; restart preserves mapping; ambiguous/wrong UID fails closed. Stage -> estimate -> user approve -> run -> SRT/warnings works with a fake executor and zero network; rejection causes zero run calls. Error then retry preserves association and estimate; duplicate request gate works; SRT automatic import count remains zero. Run Swift suite and affected Python/protocol tests/static checks; explicit-path commit and command exits. No real FCP/keychain/API or protected env/fixture/media access, push/deployment.
