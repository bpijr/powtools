# Shader/material layout registry

`nlg_material_layout.py` is the shared material codec for generic assets. Layout selection requires the mesh shader hash, the span between mesh
material references, consistent owners, complete chunk coverage, and the observed
texture-reference disk state. Length divisibility alone is never evidence.

| Shader | Bytes | Texture inputs | Records in private corpus |
| --- | ---: | ---: | ---: |
| hippodiffuseskin | 204 | 8 | 2,792 |
| hippobasicenvironment | 140 | 5 | 2,573 |
| hippodiffusenonskin | 184 | 8 | 206 |
| hippohwlitenvironment | 364 | 5 | 183 |
| stadiumdetailmaskwithuvsliding | 52 | 3 | 163 |
| crowdskin | 40 | 3 | 38 |
| crowdskindk | 40 | 3 | 2 |
| stadiumflatreflection | 48 | 4 | 25 |
| ropeskin | 32 | 2 | 14 |
| diffusedetail | 20 | 2 | 239 |
| constantcolour | 24 | 1 | 7 |
| diffuse | 8 | 1 | 21 |

The table covers fighters and supporting characters, crowds, belts and microphone
props, ropes, ring/environment sets, effects, and embedded cinematic models. The
counts include repeated appearances in cinematic sections, not unique artwork.

Each texture input is an 8-byte prefix record: a big-endian u32 texture key then
the disk state word `FFFF0000`. Every one of the 39,007 input records has that state.
For every registered input offset, keys correlate with separately parsed texture
headers across the corpus; no words outside those prefixes match corpus texture
keys in this scan. Full per-offset counts and unresolved-key counts are in
`tests/corpus/MATERIAL_CORPUS_AUDIT.json`. The weaker idea of scanning arbitrary material
words for names/texture hashes has been removed from generic asset metadata.

Independent executable evidence for the skin layout comes from static analysis of the
PAL DOL (SHA-256
`6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137`): resolver
`0x80151EC4` and eight ordered calls at `0x80112868`. It distinguishes the u16 cached
index at input +4, control byte at +6 and opaque byte at +7. The cache sentinel and
control interpretation for other shaders are a structural inference from the shared
disk layout; the registry does not label their rendering roles or edit their state.

Only the skin shader's fields documented in `MATERIALS.md` receive semantic scalar
names: specular power at +0x84 (editor range 0–256), outline colour RGB at +0x9C (0–1 each,
labelled "tint" in older tools), and its alpha at +0xA8 (0–1). These bounds are deliberately narrower than all possible
f32 values. Other bytes remain opaque with offset/size/hash inspection. Shader
programs, renderer flags, UV sliding, reflection parameters, secondary colour and
other numeric words have no generic editor.

## Editing contract

Texture input replacement changes exactly the selected u32 key and accepts only a
uniquely resolving texture already present in the same archive section. The old
reference may be external/unresolved; an unchanged key is always preserved. No
cross-archive source is automatically loaded and a name-registry match is not a
resolved texture. Unknown shaders, wrong spans, conflicting owners, orphan chunks,
partial mesh tables, and changed disk-state words lock the complete material chunk.

Scalar edits and texture-input replacement produce `nlg_model.Patch` objects and
feed the existing `nlg_asset.PatchSet` rebuild/audit path. Shared records
report all owning mesh users. Unknown bytes, texture state bytes, mesh records,
shader keys and texture payloads do not change.

## Evidence and limits

`tests/corpus/corpus_material_scan.py` is a read-only scan of all 1,194 archives / 2,062
sections. It found 224 archives with models, 356 model sets and 6,263 records; 99
single-section model archives passed byte-exact no-op rebuilds twice. All 39,007
texture-field no-ops and 8,376 scalar-field no-ops yielded zero patches. Controlled
edits to all 45 texture-input positions plus the three skin scalar fields passed
changed-range audits and deterministic second rebuilds (`CORPUS_MATERIAL_SCAN_PASS`).
Multi-section cinematic containers remain read-only.

`tests/test_material_layout.py` covers malformed layouts/owners/state, collisions,
invalid values, explicit-only texture relationships, all-layout no-ops and mutation
isolation and shared users. Opt-in fixtures include every layout, verified through
`tests/fixtures.py` before use. No game bytes
or decoded artwork are committed; the audit contains counts and offsets only.

No runtime verification was performed. Game checks still need to inspect a changed
copy for texture selection, wrap/UV behavior, alpha/depth behavior, shared-material
effects and lighting. A generic preview cannot reproduce the proprietary shader.

## Blender material controls

The generic importer uses `io_punchout_material.py`, registered by the bootstrap.
Every imported mesh has material provenance and shader state, including when
texture previews are disabled. The PO Tools sidebar's Material inputs panel shows
the proven scalar controls, explicit texture inputs, resolution status and shared
mesh users. Texture selection stages a bounded replacement for asset export.

Only those panel controls export. Shader nodes are source inspection previews;
arbitrary node edits, image painting and new material assignments do not become
game material edits. The approximate preview uses source input 0 on UV0, without
inferring transparency from texture alpha. Other input images stay unconnected,
because their shader roles and coordinate routing are not established. Preview
nodes are not refreshed from staged panel edits; the staged controls and export
review are the authoritative edited state.

Source hashes, material chunk/offset, shader and record hash are rechecked on
export. Reassigning a material or changing shader provenance is refused. Copies
of one source material must agree on their edits. Shared records were not observed
in this corpus (all 6,263 meshes reference distinct records); shared-record export
is tested synthetically, including a conflicting-copy negative control.

`tests/blender/blender_material_roundtrip.py` passed in Blender 3.6.1 and 5.2.0 LTS:
12 synthetic layout imports plus 15 private fixture imports per version, with
byte-exact no-op exports and isolated edits covering every layout. The existing
generic asset round-trip also passed on its synthetic case plus ten private model
families in each Blender version, and bootstrap registration passed in both.
