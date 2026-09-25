# Cinematic sections and camera tracks

> **Experimental.** Offline round-trips pass, but no edited cinematic has been tested in game.

Cinematic support uses strict directory ownership. No runtime verification is claimed.
Private corpus scan: `tests/corpus/corpus_cinematic_scan.py <art> --dol <main.dol>`.
Counts, consumer addresses and hashes below are evidence; game bytes are never committed.

## Directory and container evidence

The 12-byte chunk records include directory records both before and after
`numFileEntries`. A directory's `size` is its number of **immediate children** and
`offset` is the first child index in the same chunk table. Children can themselves
be directories; a parent's count is not the number of all descendant records.
`flags >> 4 >= 8` distinguishes these directory records in the corpus. Remaining
flag bits and the second byte are preserved, not renamed.

Consumer `0x8012bfb0` multiplies the directory's +8 value by 12 to find its first
child, and uses +4 as child count. It resolves each child's block and byte offset.
The NIS loader then uses the first five children positionally. Directory graph
checks reject out-of-range children, cycles and shared camera ownership.

The established outer dictionary contains `12 + 76 * sectionCount` bytes.
Section data offsets are 0x800 aligned. Inter-section gaps are zero. A fixed-size
patch copies this original dictionary and data, replacing only unchanged-length
sections; it never alters directory records, offsets, padding or unknown bytes.
Patches carry source hashes, explicit section/chunk/byte addresses and old bytes.
Aliases, overlapping patch ranges, address errors and size changes are refused.

Resizing remains locked: exact no-op splitting does not prove all embedded
offsets, runtime relocation fields or cross-section ownership required by resizing.

## Two camera families

| Family | Owner | Samples | Position | Orientation | Lens angle | Target |
| --- | --- | --- | --- | --- | --- | --- |
| NIS (958 clips) | 0x5000 directory | 0x5001 +8, u32 | 0x5003, N x vec3 f32 | 0x5004, N x xyzw f32 | 0x5005, N x f32 radians | none |
| Sampled (730 clips) | 0x5030 directory | 0x5031, u32 | 0x5003, N x vec3 f32 | 0x5004, N x xyzw f32 | 0x5028, N x f32 radians | 0x5023, N x vec3 f32 |

Every clip has a NUL-terminated ASCII name at 0x5002. A printable name alone is
insufficient: 0x5000 also owns hashed controllers (0x5011/12/13), which are excluded.
All decoded arrays must have exactly N finite samples and unit quaternions.
NIS headers must be 48 bytes and preserve all unnamed fields. The first five
children must be 5001, 5002, 5003, 5004, 5005 in that order.

The NIS relocation consumer (`0x8012bfb0..0x8012c08c`) places name/position/rotation/
angle pointers at header +4/+0x14/+0x18/+0x1c. Sampler `0x8012c180..0x8012c3f8`
indexes vec3 with stride 12, quaternion with stride 16 and scalar with stride 4.
The controller at `0x8012c444..0x8012c480` divides `(N - 1)` by the constant 30.0
before advancing normalized time. This establishes **30 samples/s for this NIS
controller**, not a universal animation rate. Game speed scaling remains external.
Projection setup `0x8012c5f4..0x8012c64c` uses half-angle trigonometry and the
header's +12 scalar; lens/aspect behavior is deliberately not exposed for editing.

The sampled-camera loader (`0x80022674..0x800227dc`) maps position/target/quaternion/
angle/another scalar to camera-data offsets +8/+12/+16/+20/+24. Sampler
`0x80022ac8..0x80022ad4` and `0x80022ce4..0x80022d18` reads **0x5028**, multiplies
by 180.0 and divides by pi, storing camera +0xec. Earlier notes and the old
inspector incorrectly labelled 0x5028 as keyframe times and 0x5026 as FOV.
0x5026 and 5022/24/25, initial matrices and remaining fields stay unnamed and locked.

Explicit relationships include section containment, directory child edges and
the model codec's model-to-rig ownership. Merely sharing a section or an ordinal
does not establish a camera-to-animation or animation-to-rig binding. Such bindings
remain unresolved; the UI must not imply synchronized skeletal playback.

## Supported boundary

Position and existing target samples may change, with fixed sample counts. Lens,
quaternion, timing, names, new clips, section reordering and resizing are locked.
Unknown chunks, untouched camera channels and other sections are preserved exactly.
Offline camera previews do not reproduce runtime camera mirroring, parent transforms,
aspect conversion, motion interpolation or NIS speed scaling.

## Blender workflow

Blender: install/reload the bootstrap and use **File > Import > Punch-Out!!
Cinematic Cameras**. Each section has preview cameras and named position/target
path meshes. Edit vertices in those paths, then refresh from the Cinematic samples
panel or scrub the timeline. Frame 1 corresponds to source sample 0. Every clip
uses its own local sample origin; this does not assert simultaneous NIS playback.
Camera preview lens is fixed at 50 mm and does not claim original framing. Export
with **Punch-Out!! Camera Edits**. Quaternion/lens/timing edits are refused, as are
changed topology, modifiers, missing/duplicate paths and altered source identity.
No Blender Action or FCurve API is used.

Only directory signature `(flags=0x93, second byte=2)` is accepted for camera
editing. The consumed track records likewise require the second-byte discriminator
2. These bytes remain unnamed; matching a different signature is not evidence of
the same format. Sampled-camera frame counts must precede consumed track pointers,
because that loader resets its pointers when reading 0x5031.

## Whole cutscenes (nlg_cutscene, io_punchout_cutscene)

An NIS container is a cutscene. Section 0 holds the local props (model sets and their
0x8000 rigs). Every later section holds **shots**: a 0x6000 directory owning one 0x6001
header, one NIS camera (0x5000) and one 0x7000 clip per actor. 0x6001 is four u32 words;
+8/+12 are the shot's first and last frame on one 30 samples/s timeline and
`last - first + 1` equals the frame count of the shot's camera and of every clip in it —
checked for all 952 shots of the 260 NIS containers (tests/test_cutscene.py). Word +0 counts
camera changes; +4 stays unnamed. Most shots meet at a shared frame; a few overlap (the later
shot owns shared frames in Blender).

A clip header (0x7001) +4 is the actor's name hash (`glassjoe`, `littlemac`, `doc`,
`referee`, `boxingring01`). Actors resolve to the rig with the clip's exact node count and
signature: a local section rig (props; one clip per placed instance), else
`characters/<name>.dict`, else the only character archive with that rig (ring ropes: the
arena's rope archive). Great Tiger's clones use their own signature and resolve by name
prefix and node count. 350 character actors, 261 prop instances; the remaining clips
(Dummy*, FX_lensflare*, face controllers, emitters) have no rig anywhere and import as
display-only helpers with an assumed chain hierarchy.

Blender (Import mode **Whole cutscene**, automatic for NIS files with shots): characters come through the fighter importer (mesh,
bind-pose rig, materials, face shapes driven by the clip's 0x7009/0x700B morph tracks),
props through the model-set importer, one camera follows every shot with a marker per
shot, and the arena defaults to the fighter's circuit (the NIS does not name one). Pose
bones hold `bind⁻¹ · parent bind · parent world⁻¹ · world`, so the rig's pose equals the
node world matrices the game skins with. Export samples actor rigs, prop placements and
camera location/rotation/lens on the frames each shot owns, turns them back into
node-local values and writes only values that moved beyond tolerance (untouched keys keep
their bytes; a static track that now varies becomes per-frame). A no-edit export is empty.
Locked: shot timing and frame counts, helpers, face-morph tracks, adding a track to a node
channel that has none, and the actors' own archives. The NIS angle is shown as vertical
field of view (unverified).

## Recorded offline evidence

`CORPUS_CINEMATIC_SCAN_PASS`: 1,194 archives; 327,932 directory records;
260 NIS containers / 1,128 sections rebuilt exactly; 958 NIS clips / 46,422 samples;
730 sampled clips / 187,629 samples; 2,418 track no-ops; 407 deterministic isolated
archive mutations. No fixture archive or payload was written by the scanner.

`CINEMATIC_DOL_EVIDENCE_PASS`: six static consumer assertions against DOL SHA-256
`6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137`.

`BLENDER_CUTSCENE_ROUNDTRIP_PASS` (`tests/blender/blender_cutscene_roundtrip.py`): Blender
3.6.1 and 5.2.0 with the Glass Joe knockout NIS. A no-edit export is empty; an actor bone,
a prop and the camera lens edited on one frame come back exactly. Runtime checks are pending: load a changed camera in game and compare
framing, parent transforms, mirrored playback, cuts and section transitions.
