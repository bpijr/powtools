# Animation directories and bounded editing

This describes the independently reproduced scope in `nlg_animation.py` and
`io_punchout_animation.py`. Historical runtime claims and heuristic paths in
`nlg_anim2.py` / `nlg_anim_encode.py` are not evidence for these new features.
No emulator or console test was performed.

## Ownership and compatibility

`0x7000`, `0x8000` and `0x7100` records with the directory flag refer to chunk
record ranges, not bytes. The `0x7000` range owns one clip header and its node
directories; node directory number is the exact track node index. This works
for fighters, compact rigs, props, environment rigs and nested NIS sections.
There is no root-skipping or translation-flag ordering heuristic.

`0x8000` owns a 56-byte `0x8001` header, name, node hashes, signed parent indices,
local translations and translation flags. The codec validates all lengths,
finite translations, parent indices and cycles. Duplicate node hashes can
exist (for example two `root` nodes); they are preserved. Bind-bone matching
refuses hashes with multiple possible node indices.

The clip header's node count at +12 and signature at +84 must match the rig
header's count at +8 and signature at +12. Across the corpus, 2,107 clips have
exactly one matching local rig. There are 5,186 without one. Four clips in
archives containing rigs fail this match (`question_mark`, `ropes`, and two
`worldbelt` clips); the others need external rig data. Same filename, name or
bone count alone is insufficient. Explicit external rig import uses the same
count/signature gate; rig source hashes and the decoded hierarchy fingerprint
are pinned for export. The signature's generation algorithm is not established.

Generic bind-bone parenting uses the exact node hash and nearest represented
ancestor. Bone world bind matrices stay intact. This resolves 181 of 193
nonempty bind sets; 12 lack a unique local hierarchy and retain flat bind bones.
Static mesh node tables contain no proved parenting relationship; none is invented.

## Track layouts and executable evidence

All values are big-endian. The per-node `0x7003` flags select static versus
sampled storage; byte length must agree and never selects a fallback format.

| Type | Stored sample | Static flag | Evidence |
| --- | --- | --- | --- |
| 0x7101 | Flag 0x01: s16 angle around Z; 0x10: four s16 / 32768; 0x20: four packed signed 12-bit / 2048; otherwise four s8 / 128 | 0x02 | DOL dispatcher 0x80181ACC; loaders 0x80188B90/BA4/C0C and 0x8018585C |
| 0x7102 | Three f32 translations | 0x04 | 0x80182180 sampler, 12-byte steps |
| 0x7103 | Three unsigned u16 scales / 2048 | 0x08 | 0x80181E1C sampler, 6-byte steps; loader 0x80188C20; multiplicative consumer 0x80185BC4 and identity (1,1,1) at 0x80341DD8 |
| 0x7112 | u8 / 255; per-node key count from 0x7110, independent of the clip frame count | Separate count | Pointer table +0x2C at 0x80181A6C, sampler 0x801825EC, loader 0x80188C8C |

The last track's normalized scalar storage is proved, but its final role is not.
It remains inspect-only. The corpus contains no packed 12-bit rotation tracks;
that codec has executable evidence and synthetic tests, not retail mutation
coverage. Quaternion decoding normalizes each nonzero sample. Unchanged keys
reuse their original bytes, avoiding normalization-driven requantization.

The read-only scan checks the executable anchors and constants against SHA-256
`6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137`.
No disassembly or game payloads are included here.

## Editing and Blender

`AssetDocument.sections[n].animations` exposes validated rigs and clips. A clip
returns `nlg_model.Patch` values for existing rotation/translation/scale keys;
`PatchSet` and `write_rebuild` perform source-hash, changed-range, deterministic
rebuild and reparse checks. Absent tracks, nonfinite values, invalid hinges,
scale range overflow, aliased payloads and key-count changes are refused.
All side tables, events, morphs, scalar tracks and unknown bytes are preserved.
No track, clip, node or container resizing is implemented.

Blender's new **Punch-Out!! Source-node Animation** import/export operators use
the generic document. The Python API additionally accepts an explicit external
rig document and section. The source-node armature displays stored local
rotation, translation and scale samples in source coordinates, with an exact
parent hierarchy. It does not attach those actions to bind-pose skin rigs,
apply fighter facing conventions or reproduce engine placement, IK, gameplay
constraints, scalar side tracks or morph animation. Frame numbers are source
sample indices; playback rate remains unestablished.

Blender 3.6 uses legacy action curves; 5.x uses action slots/layers/channel bags.
Export accepts edits to existing source samples. It refuses changes to rig
identity, parenting/rest matrices, object placement, channel membership, key
times/counts/interpolation, drivers, NLA, curve modifiers, or constraints. Float32
Blender storage of untouched values is compared before deciding to encode a key.
Output must be a new private filename outside Git and the source tree (the
nearest parent with `hashid.bin`, or the source directory for synthetic files).
Multi-section documents remain read-only through the existing AssetDocument gate.

## Reproduced evidence

`tests/corpus/corpus_animation_scan.py <art> --dol <main.dol>` produced
`CORPUS_ANIMATION_SCAN_PASS` and `ANIMATION_DOL_ANCHORS_PASS`:

- 1,194 archives; 2,062 sections; all section no-op rebuilds byte-identical.
- 264 rigs; 7,293 clips; zero malformed/unsupported animation layouts.
- 9,236,096 decoded keys; 491,601 writable tracks with empty no-op patch sets.
- 6,118 scale tracks: 4,678 static and 1,440 animated.
- 6,487 scalar tracks: 4,996 static and 1,491 animated; decoded read-only.
- 295,166 rotation tracks and 190,317 translation tracks.

`test_animation.py` covers malformed directories, cycles, compatibility mismatch,
short-clip precision ambiguity, nonfinite/out-of-range inputs, alias rejection,
unknown-byte preservation, changed ranges, deterministic no-op and edited
rebuilds and an opt-in real scale edit.

`blender_animation_roundtrip.py` checks synthetic data plus minorbelt, ropes,
referee, doc, bearhugger, FE minor gameworld and minor-circuit gameworld on
Blender 3.6.1 and 5.2.0. It proves no-op bytes, changed key containment, source
compatibility refusals, action save/reopen, synthetic rotation/scale export and
bind parenting. The generic asset round-trip also passes on both versions
across all eleven synthetic/private cases.

Runtime follow-up: load each changed family in game, compare animation placement,
parenting, scale behavior and clip transitions; identify the 0x7112 consumer and
establish sample rate, external-rig resolution and skin/action coordinate mapping
before expanding claims. All runtime checks remain pending.
