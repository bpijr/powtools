# Particles and effects (0x40xx)

> **Experimental.** The graph is read losslessly, but the only edit is a texture swap and no
> changed copy has been tested in game.

Lossless ownership of the Punch-Out!! Wii particle/effect graph. Implementation:
`nlg_effect.py` (pure Python; no bpy). Blender display markers live in
`io_punchout_effects.py`.

**Runtime unverified.** No Dolphin or console load of a changed copy has been
claimed. This file names only fields that already have a discriminating test,
parser check, corpus scan, or static PAL DOL operand/sampler check. Unknown
bytes stay opaque. There is no particle simulation editor, world-placement
editor, or timing editor.

## Evidence

| Check | Where |
| --- | --- |
| Nested table ownership, opaque sentinels, curve polynomial, no-op rebuild, isolated 4003+56 patch, refusal guards | `tests/test_effects.py` |
| Full-corpus no-op identity, zero unowned 0x40xx records, isolated texture mutation and inverse | `tests/corpus/corpus_effect_scan.py` → `CORPUS_EFFECT_SCAN_PASS` |
| PAL executable operand immediates and original sampler vs Python preview | `tests/corpus/effect_dol_scan.py` → `EFFECT_DOL_SCAN_PASS` |
| Blender no-op, 4-byte texture export, moved-marker refusal | `tests/blender/blender_effects_roundtrip.py` → `BLENDER_EFFECTS_ROUNDTRIP_PASS` |

Private fixture `effects/effects.dict` (via `PO_FIXTURE_ROOT`): 156 groups, 515
emitters, no unowned records, no layout refusals, `.data` rebuilds identically.
Catalog inventory lists the payload types in 16 archives (`CATALOG_COVERAGE.json`);
that is a count, not a field decode.

PAL `main.dol` is pinned to sha256
`6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137`
(same dump file as `DATA/sys/main.dol`). The DOL scan is static: it never runs
the game.

## Table references vs payloads

Chunk family: `0x4000`, `0x4001`, `0x4002`, `0x4003`, `0x4004`, `0x4005`,
`0x4006`, `0x4020`, `0x4021`, `0x4022`, `0x4025`, `0x4026`.

`0x4000` / `0x4002` / `0x4004` / `0x4020` are **table references**, not payloads.
Their `flags`/`version` must be `0x92` / `2`. Their size/offset words are a child
count and a start index into the chunk table. Children must start after the
file-entry TOC, after the parent record, and stay in bounds. Cycles, aliases,
and records shared by two groups are refused; sharing invalidates **both**
owners, independent of table order.

Payloads use `flags`/`version` `0x12` / `2` (block 1, matching the retail effect
layout used by the synthetic fixture). Unexpected flags, versions, or byte
lengths leave the group inventoried with an explicit reason; source bytes are
not rewritten.

Serialized pointer-looking words are **opaque**. The loader overwrites them.
Tests fill those words with nonzero junk (`0xABCD1234`, `0xCAFEBABE`, `p`/`q`
runs, curve-segment tails) and require a no-op rebuild to preserve them.

Unowned `0x40xx` records (no verified group owner) are listed and cannot be
edited. The corpus scan fails if any remain.

## Group graph (`0x4000`)

Children, in order:

1. `0x4001` header, **24 bytes**.
2. `0x4025` payload, `n * 4` bytes (opaque).
3. `0x4026` payload, `m * 4` bytes (opaque).
4. `n` emitter tables (`0x4002`).
5. `m` binding-set tables (`0x4020`).

`n` is the emitter count at `0x4001+8`; `m` is the binding-set count at
`0x4001+16`. Child count must be `3 + n + m`. `m` must be non-zero; `n` may be
zero (layout `empty`). More than one binding set is layout `multiple binding
sets`; otherwise `standard`. Unsupported or shared groups are `unsupported`.

`0x4001+4` is the group name hash. Remaining `0x4001` words are opaque.

## Emitter (`0x4002` → `0x4003` + eight parameters)

`0x4002` must own **nine** children: one `0x4003` header of **220 bytes**, then
eight parameter tables (`0x4004`).

Named `0x4003` fields (big-endian):

| Offset | Size | Name | Evidence |
| --- | --- | --- | --- |
| +0 | u32 | `name_hash` | inspect / hashid |
| +52 | u8 | spawn-volume selector | `origin_preview` only when this byte is 0, 1, 3, or 4; any other value (including 2) returns no preview |
| +56 | u32 | `texture_hash` | DOL `lwz` immediate 56 at `0x8016B79C` and `0x8016E7D8`; the only writable field |
| +60 | u32 | `atlas_cells` | swap refused unless exactly 1 |
| +84 | u32 | `model_hash` | DOL `lwz` immediate 84 at `0x8016E7F8`; `0xFFFFFFFF` = sprite, any other value is a model emitter |
| +120..+220 | 25 × 4 | `colour_samples` | length 25 in tests; first sample is a Blender display colour only |

All other `0x4003` bytes are opaque. Layout is `model` when `model_hash` is not
`0xFFFFFFFF`, else `sprite`.

### Parameters (`0x4004` → `0x4005` / `0x4006`)

Each `0x4004` owns one or two children. `0x4005` is **20 bytes**:

- +0 `u32` mode: `0` = constant with variation, `1` = curve. Any other mode
  refuses the group.
- +4 `f32` constant base (mode 0). Preview returns this value only;
  variation needs game RNG and is not sampled.
- +8 `f32` present in the constant record; not a named physical quantity.
- +12 `u32` curve segment count (mode 1). Must be non-zero when curved.
- +16 opaque. The static sampler test writes a synthetic curve address here
  before executing `fn_8016B32C`; file bytes at this word are preserved.

Mode 0 must have no `0x4006` child and finite +4/+8. Mode 1 must have a
`0x4006` payload of `count * 24` bytes. Each 24-byte segment is five big-endian
floats then 4 opaque bytes. The floats are `(start, a, b, c, d)`:
`fn_8016B32C` selects the last segment whose `start <= t` and evaluates
`a*t^3 + b*t^2 + c*t + d`. Preview input `t` is finite and in `[0, 1]`. Segment
starts must be strictly increasing; nonfinite values refuse the group.

The 4-byte tail of each segment is opaque in the file (`nlg_effect.py` notes
the loader overwrites it). Polynomial preview does not read it. Quantity and
time units are **not established**; inspector text says so.

Discriminating values from `test_curve_polynomial_discriminates_segment_and_coefficient_order`
(segments `(0, 1, 2, 3, 4)` and `(0.5, 5, 6, 7, 8)`): `sample(0)=4`,
`sample(0.25)=4.890625`, `sample(0.5)=13.625`, `sample(1)=26`.

The DOL scan executes the original `fn_8016B32C` instructions (range
`0x8016B32C`..`0x8016B39C`) against synthetic segments for counts 1, 2 and 4,
12 seeds, 17 dyadic `t` values (612 evaluations) and requires bitwise-identical
single-precision results with `Parameter.sample`.

## Binding set (`0x4020` → `0x4021` + `0x4022`)

`0x4020` must own two children. `0x4021` is **16 bytes**: +0 name hash, +8
binding count. `0x4022` is `count * 88` bytes. DOL `stw` immediate 88 at
`0x8016B78C` matches that stride.

Per 88-byte binding:

| Offset | Size | Name | Evidence |
| --- | --- | --- | --- |
| +4 | u32 | `emitter_index` | DOL `lwz`/`stw` immediate 4 at `0x8016E2DC` / `0x8016E2E8`; must be `< n` |
| +12 | u32 | `attachment_hash` | inspect-only; transform not reconstructed |
| +44 | 3×f32 | local spawn offset | `origin_preview` when the emitter selector allows it; inspect-only |

Remaining binding bytes are opaque. Attachment, fighter-space, and world
placement are **not** reconstructed. Moving a Blender marker does not write
these fields.

DOL `lwz` immediates 8 and 16 at `0x8016E458` / `0x8016E4A0` match the
`0x4001` emitter and binding-set counts.

## Writable change

**Only** a compatible local texture-hash swap at `0x4003+56`.

`Effects.texture_patches` and the Blender export call
the same guard. A swap is refused when any of these hold:

- `model_hash != 0xFFFFFFFF` (model particles)
- `atlas_cells != 1` (animated atlas mapping)
- the archive texture table is unsupported
- the current hash does not uniquely resolve to one local texture
- the target hash is missing, external, or ambiguous
- the target format byte is not 5, 6, or 8 (RGB5A3 / CMPR / RGBA32)
- width, height, format, or mip count differ
- the recorded `old_hash` no longer matches (stale edit)

Eligible no-op patches are empty. A real swap is a 4-byte in-place patch; the
chunk table, every other chunk, and every byte of the emitter except +56..+59
stay identical. Inverse mutation restores the source image
(`corpus_effect_scan.py`). `ResourceIndex` never participates in this decision.

Blender export writes at most those 4 bytes and refuses a second write over an
existing output.

## ResourceIndex (inspect-only)

`ResourceIndex` records exact hash matches in archives the caller supplied.
An external match is not proof that a resource is loaded in a particular
scene. It does **not** authorize texture swaps: a hash that exists only in
another archive still fails unique local resolution
(`test_resource_index_never_authorizes_texture_swaps`).

No separate animation-clip hash field is established on the emitter. Inline
parameter curves have explicit owners.

## Blender visualization

Import builds empties (group / emitter / binding) from a deterministic
`view_specs` manifest. Locations are diagram positions or, for supported
spawn selectors, the local +44 offset. They are **display markers**, not a
game render, attachment reconstruction, or particle simulation.

Export (`collect_effect_patches`) refuses moved, rotated, scaled, reparented,
duplicated, or deleted markers, extra objects, constraints, animation, mesh
data, and schema/fingerprint changes. The only accepted edit is
`po_effect_texture` on an eligible emitter, chosen from
`texture_candidates`. Placement is not an editor.

## Locked

Model hashes, atlas cell counts other than 1, attachment hashes, world
placement, spawn volumes other than the four previewed selectors, parameter
quantity/units, curve variation RNG, opaque pointer-looking words, unowned or
malformed groups, and any 0x40xx byte other than `0x4003+56` under the guards
above. Runtime behavior of a swapped texture is unverified.
