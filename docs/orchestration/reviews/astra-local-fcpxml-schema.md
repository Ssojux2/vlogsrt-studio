# Installed Apple FCPXML schema evidence

Inspected read-only on 2026-09-09 KST. Final Cut Pro is installed at
`/Applications/Final Cut Pro.app`, version **12.3**, build **450152**, read from
its application `Contents/Info.plist`. No library, project database, or user media
was opened or modified.

The exact local FCPXML 1.14 schema is:

`/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/Versions/A/Resources/FCPXMLv1_14.dtd`

- Size: 42,981 bytes.
- SHA-256: `33bb44530790be145d87ed2d5c347166aab945c90f1cfbc03c8be545aaf73cb5`.
- Discovery used a narrow application-bundle file listing, not Downloads, media
  folders, other projects, or a Final Cut library database.
- Workers may read this exact application resource. Do not copy Apple's DTD into
  the repository or treat a hand-written schema as equivalent evidence.

## Consequences for the current implementation

P1-04 and P1-05 can execute actual local `xmllint` checks with this schema. The
schema is available in this environment; it is not an unexecuted DTD-unavailable
gate. Availability alone does not mean any fixture has passed validation yet.
Final Cut application import/re-export, relink, and playback remain separate
acceptance gates.

An independent synthetic check used P1-02's inline `build_fcpxml()` fixture
from its in-progress worktree, written only to a temporary directory. Actual
`/usr/bin/xmllint --nonet --noout --dtdvalid <installed-DTD-file-URI> <temp-XML>`
returned **3** for its initial `media-rep kind="original"` / `kind="proxy"`
spellings. Replacing just those values with the actual schema enumerators
`original-media` / `proxy-media` returned **0**. This validates that synthetic
document against the installed schema; it is not an application import test or
approval of the unfinished parser. The internal model may retain `original` /
`proxy` names only through explicit translation at the XML boundary.

The schema distinguishes contained lane zero from nonzero anchored lanes, and
documents zero-second defaults for common clip offset/start attributes. Asset
names are optional. Audio/video-presence attributes are implied without a declared
default of one. Ingest must not invent a default of one or describe legitimate
schema defaults as malformed FCPXML. A narrower supported-subset readiness
requirement must be reported explicitly as such.

Apple's primary asset documentation explains that omitted media-derived properties
are resolved from the media file. Absence therefore does not establish either audio
present or audio absent. Opus independently confirmed this in
`opus-hasaudio-semantics.md`.

Astra's binding v1 disposition requires explicit `hasAudio="1"` or `"0"` at the
XML boundary. Omission is a narrower supported-subset rejection with an asset
location and actionable reason, not malformed FCPXML. Successfully parsed models
retain their existing boolean field because no unspecified declaration reaches
construction. Only explicit zero authorizes `declared_no_audio`. Future measured
presence resolution requires its own reviewed change; actual export compatibility
remains an acceptance gate. `hasVideo` has no modeled presence decision: present
values are limited to zero/one, while omission remains uninterpreted in the raw XML.

Apple's [asset documentation](https://developer.apple.com/documentation/professional-video-applications/asset)
describes the presence flags, and its
[document type definition reference](https://developer.apple.com/documentation/professional-video-applications/document-type-definition)
provides general schema context. The installed 1.14 file above is the version-specific
local evidence for this run.
