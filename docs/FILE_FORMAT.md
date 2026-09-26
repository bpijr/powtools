# Punch-Out!! Wii — File Format Reference

Consolidated research reference for the NLG "godatabase" character archive. Evidence varies by
field: byte comparisons, historical cross-checks, and selected recorded in-game probes do not
validate every interpretation. All values are **big-endian** (Wii/PPC).

For the animation format see **ANIMATION_FORMAT.md**. See `MODDING_RESEARCH.md` for open questions.

---

## 1. The archive triplet

Each asset group is a triplet keyed by name:

- **`.data`** — raw concatenated resource payloads (index buffers, vertex data, animations,
  textures). Character data is **uncompressed**.
- **`.dict`** — tiny top-level directory (magic + block table): 88 B, or `12 + 76 × n` B for the
  multi-section NIS cinematics.
- **`.debug`** — the full fine-grained index (optional; `.dict` is a coarse subset).

**`art/hashid.bin`** is a global registry for known hash/name pairs. Most resource references are
hash-based, although some animation and camera chunks contain ASCII labels.

### `.dict` header (big-endian)

```
u32 magic = 0xA9F32458
u16 ver   = 0x0601
u8  isCompressed        (0 in every retail archive)
u8  pad
u32 sectionCount        (1 for ordinary archives)
sectionCount × Section {                   # 76 bytes each
  u32 dataOffset        (where this section starts in .data; 0 for the first)
  u32 numFileEntries
  u32 fileTableSize
  8 × BlockInfo { u32 size, u32 param }   # block offsets accumulate; size 0 = unused
}
```

An ordinary archive is one section, which is the 88-byte dictionary (`sectionCount = 1`,
`dataOffset = 0`). The 260 **NIS cinematic archives are not compressed**: they hold 2–13
sections (1,128 in total), so their dictionaries are `12 + 76 × n` bytes. Each section is laid
out exactly like a whole archive; splitting on the section records and re-joining with zero
padding rebuilds all 260 `.dict`/`.data` pairs byte-for-byte. Sections carry camera tracks
(`0x5001`–`0x5005`), animations (4,632 `0x7001` clips) and, in 148 sections, models and textures.

### `.data` layout

```
[ Block0 .. Block7 ][ ChunkTable @ sum(blockSizes) ]              # one section
[ Section0 ][ zero pad to 0x800 ][ Section1 @ dataOffset ] ...    # multi-section (NIS)
```

---

## 2. Chunk table — two back-to-back lists

Both lists are arrays of 12-byte records:

```
ChunkDataInfo { u8 flags, u8 unk=1, u16 type, u32 size, u32 offset }
```

1. **First `numFileEntries` records = table of contents** (flags `0x9x`). Their `offset`/`size` are
   counts/indices, **not** byte offsets. The parser skips them (block index = −1). Resize-in-place
   never touches these.
2. **Remaining records to EOF = data chunks.** The block index is `flags >> 4` (if < 7). A chunk's
   absolute position = `block.offset + chunk.offset` (offset is *within* its block).

Remaining records also include non-owning records
(`flags >> 4 >= 7`), which `find_chunks` skips. Bear Hugger has 3,347 such records after
`numFileEntries`; record 59 is `{0x92, 1, 0xB008, 42, 60}`. Do not treat their offsets as payload
addresses. The two-list description above is an incomplete model of the table's organization.

### Block architecture

Exactly 4 active blocks, each a contiguous run of its data chunks (4-byte inter-chunk alignment;
block total padded up to `0x800` / 2048 B):

| Block | Chunk flags | Contents |
|-------|-------------|----------|
| 0 | `0x02` | **IndexData** (`0xB007`) — triangle-strip index buffers |
| 1 | `0x12` | **metadata + ANIMATION** — texture/material/mesh/bone chunks + all `0x7xxx`/`0x8xxx` anim chunks (biggest block) |
| 2 | `0x25` | **TextureData** (`0xB603`) — texture pixels |
| 4 | `0x45` | **VertexData** (`0xB006`) — vertex buffers |

### Section magics (`u16`)

`TextureHeaders 0xB601` (96 B/tex), `TextureData 0xB603`, `MaterialData 0xB016`, `IndexData 0xB007`,
`VertexData 0xB006`, `VertexAttributePointerData 0xB005` (8 B), `MeshData 0xB004` (52 B),
`ModelData 0xB003` (12 B), `MatrixData 0xB002`, `SkeletonData 0xB008` (empty — hierarchy is in the
anim node table, not here), `BoneHashes 0xB00B`, `BoneData 0xB00A` (68 B), `UnknownHashList 0xB00C`.

---

## 3. Hashing (`nlg_hash.py`)

```
h = -1
for each byte c (A–Z lowercased if case-insensitive):
    h = (h * 33 + c) & 0xFFFFFFFF
```

The current registry mixes policies: all 14,987 names match either case-sensitive or ASCII-folded
hashing; 14,984 match case-sensitive hashing, 11,128 match case-insensitive hashing, and three match
only the latter. New hashes can be calculated, but the correct case policy and runtime registration
requirements must be validated for the target resource family. `hashid.bin` is big-endian:
`u32 count`, `count × (u32 hash, u32 strOffset)`, then a string table at `count*8 + 4`.

---

## 4. Repacking & resize-safe injection (`nlg_pack.py`)

The repacker rebuilds `.data` + `.dict` and **round-trips byte-identically on all 95 character
archives** — the capability nobody had (Switch-Toolbox's `PO_DICT.Save()` is empty).

`Archive.replace_chunk(ri, new_bytes)` rebuilds the target block: it keeps every other chunk's bytes
+ padding and sets the resized chunk's trailing gap so its region stays ≡ original (mod 32), so
downstream chunks shift by a multiple of 32 and **all alignments are preserved**; the block is
repadded to `0x800`, every chunk offset/size in that block and the block size in the `.dict` are
recomputed, and the TOC records are left untouched. API: `find_chunks(type_id, block)`,
`get_chunk_bytes(ri)`, `replace_chunk(ri, new_bytes)`.

For full custom models, replacing geometry means editing correlated chunks together
(VertexData + IndexData + MeshData + BoneData); `replace_chunk` is the primitive that a higher-level
"set_model" drives with a rebuilt mesh table.

---

## 5. Geometry (`nlg_geom.py`)

### Model sets (`nlg_model.py`)

A section holds any number of **model sets**, each a run of chunks in a fixed order, verified
on all 356 sets in the retail dump (99 ordinary archives, 125 NIS sections):

```
B016 B007 B006 B005 B004 B002 B003 [B00B × meshCount, B00A, B00C]      # bracket = skinned (212 sets)
```

`D001`/`D002` pairs (31) sit between sets and are preserved untouched.

- **`B003` node** (12 B): `u32 nameHash, u32 meshCount, u32 0`. Nodes partition the mesh table in
  order; the counts always sum to the mesh count. Names are the source scene objects
  (`_feminor/punchingbag/capsule01`, `.../major_c_logo.nlg/major_root`).
- **`B002` transform** (64 B): 4×4 f32, row-vector convention, translation in row 3. A mesh picks
  one with its `+24` index. Vertices are **node-local**: world = `[x y z 1] · M` (Blender's matrix
  is the transpose). Nodes may share a transform; every transform is used. Checked on the
  punching-bag props of `feminor` and the NIS round counters. Skinned sets apply it too: crowd
  vertices (z −1.67…0.52) only meet their bones (z 0…1.91) after its z = 1.677 translation;
  fighters carry near-identity transforms (Glass Joe: z = 0.013).
- **`0x6101`** (ring environments, 273 chunks) is a **culling tree**: AABB (6 f32), item count,
  flags, then item hashes. Every item names a `B003` node (`partitionedgeometryobjectNN`). It
  holds no transforms; scenes rebuild from `B002`/`B003` alone.

### Mesh record (52 B)

```
0  u32 IndexStartOffset          (into IndexData chunk)
4  u32 indexFlags                (count = low 24 bits; fmt = high 8: 0 = u16 indices, else u8)
8  u16 VertexCount
10 u8  = 1                       (every retail mesh)
11 u8  NumAttributePointers
12 u32 byte offset of this mesh's first B005 pointer (cumulative, always in order)
16 u32 ShaderHash                (selects the material record layout, see below)
20 u32 MeshHash
24 u32 TransformIndex            (into B002)
28 u32 format word               (0x000D0007; 0x000C2015 on 82 fighter meshes) — preserved
32 u32 format word               (varies with vertex layout) — preserved
36 u32 MaterialOffset            (byte offset into B016)
40/44/48 u32 = 0                 (every retail mesh)
```

### Attribute semantics come from the pointer flags

The pointer's `u16 flags` carry the meaning: **high byte** 1 position, 2 normal, 3 colour,
4 texture coordinates, 5 weights, 7 bone indices; **low byte** is the set number. The `u8 type`
names the storage. Established storages: `0x0A`/`0x67` 3×f32, `0xFE` 3×f32 or 3×s8/64,
`0xE9`/`0xB6` RGBA8, `0xCC` 2×s16/1024, `0x26`/8 2×f32, `0xD4` 4×u8, `0xB0` 4×f32, and
`0x05 0x3D 0x4C 0x52 0xF9 0x2E` 2×s16/1024 — these repeat UV0's raw integers exactly on 14–98%
of vertices in the same meshes, which only happens at the same scale; their extreme values
belong to unused sets holding junk. The storage byte is therefore not a scale code. Only
`0x16 0x17 0xFC 0x26/4` (510 arrays, never alongside UV0) remain unestablished: they are
preserved byte-for-byte and never re-encoded.

### Material record size follows the shader

Records are addressed by byte offset and span to the next referenced offset. Each shader occurs
with exactly one size: `hippodiffuseskin` 204, `hippobasicenvironment` 140,
`hippodiffusenonskin` 184, `hippohwlitenvironment` 364, `stadiumdetailmaskwithuvsliding` 52,
`crowdskin`/`crowdskindk` 40, `stadiumflatreflection` 48, `ropeskin` 32, `diffusedetail` 20,
`constantcolour` 24, `diffuse` 8 (`nlg_model.MATERIAL_SIZES`).

### Patch sets (`nlg_asset.py`)

Generic edits never rebuild a chunk: they become fixed-size byte patches recording the original
bytes. Rebuilding applies them to the exact source (hash-checked), refuses any byte change
outside the recorded ranges, and must produce identical output twice. Unchanged elements keep
their original bytes whatever the storage quantization, so a no-op export is identical by
construction.

All meshes are **triangle strips**. The index buffer lives in IndexData (block 0) at
`IndexStartOffset`, `IndexCount` `u16` values = one strip.

### Vertex attributes — SoA, 8 per boxer mesh

Attribute pointer (8 B): `u32 offset` (rel. to VertexData chunk), `u8 type`, `u8 stride`, `u16 flags`.
Each attribute is a contiguous array of `stride × VertexCount` bytes, indexed by vertex id.

| # | Type | Stride | Meaning | flags |
|---|------|--------|---------|-------|
| 0 | `0x0A` | 12 | POSITION (3× f32 BE) | 0x100 |
| 1 | `0xFE` | 12 | NORMAL (3× f32 BE) | 0x200 |
| 2 | `0xCC` | 4 | UV0 (2× **s16** / 1024 — SIGNED) | 0x400 |
| 3 | `0x05` | 4 | aux (color/tangent?) | 0x401 |
| 4 | `0x3D` | 4 | aux | 0x402 |
| 5 | `0xE9` | 4 | aux | 0x301 |
| 6 | `0xD4` | 4 | BONE INDICES (4× u8) | 0x701 |
| 7 | `0xB0` | 16 | BONE WEIGHTS (4× f32 BE) | 0x500 |

Coordinate convention: Punch-Out is **Z-up native** (X = left/right, Y = depth, Z = up). `nlg_geom`
exports OBJ as Y-up (`po_to_obj: x, z, −y`) so Blender's default import stands the model up;
`obj_to_po` inverts. Validated on glassjoe: 42 meshes, 6394 verts, bone weights sum = 1.000.

**Encoder** (`read_model_full` / `encode_model`) rebuilds IndexData/VertexData/MeshData/attr-pointer
chunks from scratch for any per-mesh vertex/index count. Recipe to inject a new mesh: template an
existing mesh (keep record/material/attr types + flags), replace attrs 0/1/2/6/7 with new
pos/normal/uv/boneIdx/weights, copy aux attrs 3/4/5 constant, build the strip
(`tris_to_strip` — degenerate-triangle joiner), set vcount + index flags, `encode_model`. A custom
UV-sphere rigged rigidly onto a bone was injected and confirmed in-game.

> **Insight:** meshes are grouped by **material/shader, not body part.** Several meshes can share a
> name like `gj_skin` while each covers a different region, and one mesh can span multiple body parts
> (the replaced `mesh[15]` spanned left arm + chest). Plan whole-character work around material
> groups; to target a body part, filter vertices by **bone weight**, not by mesh name.

---

## 5b. Materials — see `MATERIALS.md`

Material records (`MaterialData 0xB016`, 204 B each in the supported preset) expose 8 texture
slots (detail/damage/spec-mask/cell-ramp/rimlight-ramp/gloss/fresnel/spec-ramp), a spec power,
render switches and an outline colour. Every field is named by the shader's own parameter
table; see **`MATERIALS.md`**. Editor: `nlg_material.py`. To recolor a surface, edit its cell
ramp (slot 3); `+0x9C` is the outline colour, not a tint.

## 6. Textures (`nlg_texture.py`)

Character skins are **CMPR** (GameCube DXT1). `TextureHeaders` (`0xB601`, 96 B/tex, block 1):
`u32 hash, u16 w, u16 h, u16 0, 3×u8, u8 format, 3×u16, u32 dataOffset, u32 0x00412BA1`.
Pixels live in `TextureData` (`0xB603`, block 2); a CMPR texture's size is
`ceil(w/8) × ceil(h/8) × 32`.

Only three format bytes occur in the retail archives: **6 = CMPR** (every character skin),
**5 = RGB5A3** (menu/HUD bundles, effects, rings: 4×4 tiles of BE u16, top bit set = opaque RGB555,
clear = A3 RGB444) and **8 = RGBA32** (the 128×4 toon ramps of Bald Bull, Don Flamenco, Disco Kid,
Great Tiger and Mr. Sandman, plus ring and global lookup maps: 4×4 tiles of 64 B, sixteen A,R pairs
then sixteen G,B pairs). Sizes sum every mip level at the format's own tile size; with those sizes
every texture ends inside its pixel chunk.

Some archives have **several texture tables**: Big Mac, Little Mac, Glass Joe 2, Great Tiger (both),
`effects/effects.dict` and the four frontend bundles hold two or three `0xB601`/`0xB603` pairs. Each
header table indexes its own pixel chunk, which directly follows it in the chunk list.
`list_all_textures` numbers them continuously (identical to `list_textures` for one table). The
frontend bundles and `global.dict` also alias pixel storage between records; those records stay
read-only because changing one would change the others.

Codec: `decode_cmpr` / `encode_cmpr` handle the GC 8×8 super-tile (four 4×4 DXT1 sub-blocks, RGB565
BE endpoints, MSB-first indices); `decode_rgb5a3` / `encode_rgb5a3` and `decode_rgba32` /
`encode_rgba32` handle the tiled formats (re-encoding a decoded base level reproduces its bytes);
plus PNG import/export (Pillow). Every format is fixed-size per dimensions, so texture edits are
**in-place** (no resize) and written in the record's own format.

> Punch-Out uses heavy **shader ramp** textures (many "textures" are gradients, not literal albedo).
> To recolor a surface: read the mesh's material → the first `u32` in `MaterialData` (`0xB016`) at
> `matChunkPos + mesh.MaterialOffset` is the diffuse texture hash → de-hash → repaint that texture.
> Glass Joe's skin diffuse is `glassjoe/white` (32×32) — the flesh tone comes from the skin shader
> multiplying white, so recoloring `white` recolors the body. Textures are per-character (local),
> so edits don't leak between fighters. A full green Glass Joe was confirmed in-game.

---

## 7. Skeleton (`BoneData` `0xB00A`)

Array of bones, **68 B each** = `u32 nameHash` + 4×4 f32 bind matrix.

- The matrix is the **WORLD bind pose**. **Rotation is column-major (transpose it!)**; translation is
  in row 3 (e.g. `bip01 head` at Z ≈ 1.81). glassjoe = 53 main bones + 22 shadow bones (2nd BoneData
  chunk).
- `SkeletonData` (`0xB008`) is **empty** — hierarchy is not stored there. It lives in the animation
  node table (`0x8009`; see ANIMATION_FORMAT.md), or implicitly in the 3ds-Max biped names.
- `BoneHashes` (`0xB00B`): **one list per mesh** = that mesh's bone **palette**. The vertex `boneIdx`
  attribute (`0xD4`, 4× u8) indexes into this palette → hash → `BoneData` bone. This is how skinning
  resolves.

Reminder (repeated because it caused a major bug): use the transposed (column-major) rotation, and
remember `BoneData` is for **skinning only** — animation FK composes in node space, not bone space.

---

## 8. Roster / new-character feasibility (summary)

The roster is name-keyed (hashed) across layers: assets (`art/characters/<name>.data`), behavior
(`characterdefinitions/behaviours.bun.data` — AI, combos, HP), and the data-driven front-end menus.
`main.dol` (3.6 MB PPC) holds a **contiguous character registry table** (~byte 3.26M): 80 entries of
`{DisplayName, "art/characters/<Name>", lowercase_id}`. There are pre-registered unused/template
slots — **`JoeTemplate`** is a complete unused fighter (model + defs + full AI + registry entry),
plus `AverageJoe` and `Question` ("?").

- **Replace an existing slot** with a custom model/texture/behaviors — **fully doable now.**
- **Commandeer `JoeTemplate`** — promising; uses the real template slot.
- **A true 31st selectable slot** keeping all originals — hardest; likely needs menu-count / `main.dol`
  patching.


## 9. Generic geometry editing audit

`GEOMETRY_EDITING.md` describes the current bounded writer and its evidence. It rebuilds
vertices/faces within existing mesh slots, remaps all attribute arrays and local B00C morph
indices from explicit vertex provenance, and recomputes 0x6101 culling bounds while retaining
node hashes and the pre-order full-binary tree. All 356 source model-set repacks and all
273 source culling boxes reproduce exactly in `tests/corpus/corpus_geometry_scan.py`.

The remaining 510 UV arrays have executable-backed scales in their matching shader only:
0x16/0x17 are s16/256 for diffusedetail, 0x26 with stride 4 is s16/4096 for constantcolour,
and 0xFC is s16/1024 for stadiumflatreflection. The read-only DOL scan verifies constructor
hashes, material sizes, vtable setup calls and 12 constant GX VAT calls against one identified
executable. It does not generalize these storage bytes to unrelated shaders/builds.

Topology PatchSets use schema 2, audit relocated chunk identities/payloads and preserved
archive padding, reparse, and rebuild deterministically. Blender 3.6/5.2 checks include UV
seam splitting, inherited morphs, and image painting with full mips.
No new runtime claim follows from these offline checks. Whole mesh/node insertion/deletion,
new bone palettes and multi-section archive writes remain outside this writer.

## Shader/material registry

`nlg_material_layout.py` selects all 12 observed layouts by mesh shader and exact
record span, with consistent ownership and the observed texture-reference state.
See [MATERIAL_LAYOUTS.md](MATERIAL_LAYOUTS.md) and the counts-only
`tests/corpus/MATERIAL_CORPUS_AUDIT.json`: 6,263 records, 39,007 explicit texture inputs,
all 48 editable fields checked through deterministic changed-range rebuilds.
Non-skin scalar state is opaque; bounded local texture-input replacement is available
for every known layout. Cinematics and unproved layouts remain locked. Runtime pending.

## Animation directories and bounded source samples

The independently reproduced path is documented in [ANIMATION_EDITING.md](ANIMATION_EDITING.md).
`0x7000`/`0x7100` directories give exact node ownership; `0x8009` gives parenting.
`0x7103` stores unsigned XYZ scale over 2048. `0x7112` stores normalized u8 over
255 with independent counts in `0x7110`; its final meaning remains unproved and
editing is locked. `nlg_animation` preserves all untouched keys and unknown data
and writes only fixed-size rotation/translation/scale patches through AssetDocument.
No runtime verification is claimed by this implementation.

## Cinematic cameras and section patches

See [CINEMATICS.md](CINEMATICS.md) for directory ownership, both camera families,
static executable consumer addresses, corpus counts and the supported edit boundary.
Directory records also occur after the initial table of contents: their count covers
immediate children, including nested directories. NIS 0x5001 +8 holds camera samples;
5003/5004/5005 are position/quaternion/angle arrays. Sampled 5030 directories use
5031 for sample count, 5003 position, 5023 target and **5028 angle in radians**.
The old 5028 "time" / 5026 "FOV" labels are not correct. 5026 remains unresolved.

Section-aware PatchSets now permit existing camera position/target sample edits
while preserving outer dictionary bytes, directory records, every offset, gaps,
unknown fields and other sections. NIS resizing, lens/quaternion/timing edits and
unproven camera/animation/rig bindings remain locked. No runtime verification exists.

---

## 9. Fighter behavior definitions (`nlg_behavior.py`)

See **BEHAVIOR_FORMAT.md**. Archives under `characterdefinitions/` hold compiled scripts
(`0x50xx`) and combo graphs (`0xE0xx`). Directories own every data chunk. Script
instructions and unestablished property words stay opaque. Serialized address-looking
words are never used as file offsets.

The only writable fields are existing `AnimProperties` floats **TimeScale** (0.0–3.0) and
**BlendAmount** (0.0–0.5). Those bounds are the offline editor envelope, not a runtime
guarantee. Named hashes and property-tag discriminators are pinned by
`tests/corpus/behavior_dol_audit.py` against `main.dol` sha256
`6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137`. Full-corpus no-op
rebuilds and isolated mutations: `tests/corpus/corpus_behavior_scan.py`
(`CORPUS_BEHAVIOR_SCAN_PASS`). Game load of an edited copy is not claimed.

---

## 9. Particles and effects (0x40xx)

See **EFFECTS.md** for the graph, named offsets, PAL DOL operand/sampler checks and
corpus scan. Summary only:

- Family `0x4000`–`0x4006`, `0x4020`–`0x4022`, `0x4025`–`0x4026`. `0x4000` /
  `0x4002` / `0x4004` / `0x4020` are table references (flags `0x92`, version 2),
  not payloads. Serialized pointer-looking words are opaque; the loader overwrites
  them.
- Ownership is exclusive. Shared, cyclic, unowned or malformed `0x40xx` records
  are inventoried and refused. Parameter curves are inline (`0x4005`/`0x4006`);
  no independent animation-clip hash field is established.
- **Only writable change:** `0x4003` +56, a texture name hash, and only for a
  single-cell sprite emitter (`atlas_cells == 1`, `model_hash == 0xFFFFFFFF`)
  whose current and replacement textures each uniquely resolve in the same
  archive with matching width, height, format and mip count (format bytes 5, 6,
  8). `ResourceIndex` matches in other archives are inspect-only and never
  authorize a swap.
- Model hashes, atlas sheets, attachment hashes, spawn offsets, world placement,
  timing and remaining payload bytes stay inspect-only. Blender empties are
  display markers; moving them is refused on export.
- **Runtime unverified.** No Dolphin or console load of a changed copy.

---

## 9. Wii Wwise DSP audio and inspect-only content

Old Wii Wwise media is big-endian **RIFX/WAVE** with `fmt` tag `0xFFF0` (Nintendo DSP-ADPCM).
Each ADPCM frame is 8 bytes / 14 samples. The 46-byte `WiiH` chunk is the DSP ADPCM context
(16 coefficients, gain, predictor/scale, history) produced by `nlg_dsp.context`.

`nlg_dsp.encode` / `decode` convert mono s16 PCM to and from DSP-ADPCM, and `nlg_wwise`
parses banks (BKHD/DIDX/DATA/HIRC) and RIFX WEM media. No audio replacement workflow is
included. Runtime unverified.

Crowd / HUD / lighting / damage-state records are inspect-only (chunk type, size, sha256,
owner-or-unknown). Crowdskin layouts belong to materials; HUD tip scripts belong
to behavior; lighting ramps are material slots; damage names sit at mesh +20. Unknown
families `0x6501`–`0x6523` and `0x80xx` stay unidentified. No state-switch editor is claimed.
