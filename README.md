# Punch-Out!! Wii asset tools

Python tools and a Blender add-on for reading, editing and rebuilding the asset archives of
Punch-Out!! Wii (Next Level Games, 2009). They unpack and repack the game's `.dict`/`.data`
archives, decode textures, geometry, materials, skeletons, morphs and animations, and map them to
and from Blender.

Executable addresses in the docs refer to PAL `R7PP01`, revision 0.

This is research software. Work on copies, keep the original archives beside every mod, and validate
output before loading it in Dolphin or on hardware. No game files are included; you need your own
dump.

## Layout

```text
potools/
  formats/     pure-Python codecs (no Blender needed)
    nlg_pack.py              lossless .dict/.data container model
    nlg_hash.py              name hashing and hashid.bin registry
    nlg_texture.py           GX texture formats (CMPR, RGB565, RGB5A3, RGBA32, ...) and mip chains
    nlg_texture_animation.py animated texture sequences
    nlg_geom.py, nlg_model.py  geometry, triangle strips, model sets and topology
    nlg_material.py          204-byte fighter material records
    nlg_material_layout.py   all 12 observed shader/material layouts
    nlg_skeleton.py          bind matrices
    nlg_morph.py             facial morph targets
    nlg_anim2.py             fighter animation decoder
    nlg_anim_encode.py       fighter animation encoder
    nlg_animation.py         generic node-directory animation codec
    nlg_asset.py             generic asset document and audited patch sets
    nlg_container.py         multi-section containers
    nlg_dsp.py, nlg_wwise.py Wii DSP-ADPCM audio and Wwise banks
    po_archive.py            strict loading, output guards, validation
    nlg_cinematic.py, nlg_cutscene.py   cinematics      (experimental)
    nlg_behavior.py          fighter behavior graphs     (experimental)
    nlg_effect.py            particles and effects       (experimental)
  blender/     Blender add-on (install po_tools_bootstrap.py)
  tools/       command-line tools: validate_mod, nlg_unpack, dol_tool, nlg_dol
  tests/       unit tests (synthetic data)
    blender/   headless Blender round-trips
    corpus/    whole-dump scans that back the format docs
docs/          format references, Blender workflow, Dolphin testing
art/           put your game's art/ files here (ignored by Git)
```

## Blender add-on

Install only `potools/blender/po_tools_bootstrap.py` (Edit ▸ Preferences ▸ Add-ons ▸ Install).
It loads the rest in place from this repository. If it cannot find the folder, set its
**potools folder** preference or the `PO_POTOOLS` environment variable.

Blender 3.0 or newer is required; import/export round-trips are tested on 3.6 and 5.2.
Use File ▸ Import ▸ Punch-Out!! Asset and File ▸ Export ▸ Punch-Out!! Asset, and the
**PO Tools** tab in the 3D view sidebar. See [docs/MOD_PIPELINE.md](docs/MOD_PIPELINE.md) for the
fighter workflow and [docs/DOLPHIN_TESTING.md](docs/DOLPHIN_TESTING.md) for loading results in game.

Cinematics/cutscenes, behavior graphs and effects are **experimental**: they round-trip offline,
but no edited copy has been tested in game.

## Command line

Requires Python 3.10+. From the repository root, with your game files under `art/`:

```bash
python potools/tools/nlg_unpack.py art/characters/fighter.dict
python potools/formats/nlg_material.py list art/characters/fighter.dict
python potools/tools/validate_mod.py path/to/mod/fighter.dict art/characters/fighter.dict
```

The repacker reads and writes `.dict` and `.data`. It does **not** regenerate `.debug`.

## Format in one paragraph

The archive is a hash-indexed resource container. The 88-byte `.dict` header declares eight block
sizes. `.data` contains those blocks followed by 12-byte chunk records. A chunk record identifies a
block, type, payload size and block-relative offset. Common chunks contain vertex/index buffers,
mesh records, bone data, materials, textures, morphs and animation tracks. Most cross-references use
32-bit name hashes; some animation and camera chunks also contain ASCII labels. `hashid.bin` is a
registry of known hash/name pairs, not proof that every possible hash has a name.

Details: [FILE_FORMAT](docs/FILE_FORMAT.md), [ANIMATION_FORMAT](docs/ANIMATION_FORMAT.md),
[ANIMATION_EDITING](docs/ANIMATION_EDITING.md), [MATERIALS](docs/MATERIALS.md),
[MATERIAL_LAYOUTS](docs/MATERIAL_LAYOUTS.md), [GEOMETRY_EDITING](docs/GEOMETRY_EDITING.md),
[CINEMATICS](docs/CINEMATICS.md), [BEHAVIOR_FORMAT](docs/BEHAVIOR_FORMAT.md),
[EFFECTS](docs/EFFECTS.md), [open questions](docs/MODDING_RESEARCH.md).

## Status

| Capability | Evidence | Limit |
| --- | --- | --- |
| Unmodified `.dict`/`.data` rebuild | Byte-identical across 1,194 retail archives, including 260 NIS containers | Only bounded, proven mutations are offered |
| Geometry, textures and materials | Proven layouts, topology/texture patches, guarded material inputs and Blender round-trips | New mesh/node slots and unproven shader fields remain locked |
| Fighter animation | Decoder, encoder and Blender import/export | Engine timing and IK remain unestablished |
| Cinematics (experimental) | Strict directory ownership, bounded camera samples, Blender cutscene round-trip | Lens, timing, bindings and runtime framing untested |
| Behavior and effects (experimental) | Readable graphs with narrow float/texture-hash editors | Scripts, particle placement and most fields remain opaque |
| New selectable roster slot | Not implemented | Requires work outside the archive pipeline |

## Tests

```bash
python -m unittest discover -s potools/tests
blender --background --factory-startup --python-exit-code 1 --python potools/tests/blender/blender_asset_roundtrip.py
```

Unit tests use synthetic archives. To also run the dump-backed cases, point `PO_FIXTURE_ROOT` at
your dump's `art` folder; `python potools/tests/fixtures.py verify <art>` reports which files
match the hashes in `potools/tests/private_fixtures.json`.

## Hash-policy caveat

The retail registry mixes case-sensitive and case-insensitive hashes: of 14,987 names, 14,984 match
case-sensitive hashing, 11,128 match case-insensitive hashing, and three match only the
case-insensitive policy. The case-insensitive default in `nlg_hash.string_to_hash` is not a
universal engine rule. Prefer existing lowercase naming conventions and verify new names in game.

## Credits

Created by Bryan Intindola.

HiddenBoi's earlier community research and documentation informed this work. Switch-Toolbox's open
NLG readers provided important comparative format information. RoadrunnerWMC is credited for the
NLG name-hash algorithm noted in `nlg_hash.py`.

Developed with AI-assisted tooling; format claims were checked by byte comparison, round-trip tests
and Dolphin runs.

## License

MIT; see [LICENSE](LICENSE).

Punch-Out!!, Wii and Nintendo are trademarks of their respective owners. This is an unofficial
project and is not affiliated with or endorsed by Nintendo or Next Level Games. No game assets are
distributed with these tools.
