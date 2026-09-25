# Punch-Out!! Wii — Fighter behavior definitions

> **Experimental.** The graph reader is lossless, but only two float fields are editable and no
> edited copy has been tested in game.

Evidence-only notes for `nlg_behavior.py`. Field names appear only where a discriminating
test exists (synthetic parse/mutation, private corpus scan, or a pinned static operand
check in `main.dol`). Unknown bytes stay opaque. **Runtime unverified**: no edited copy
has been loaded by the game.

Canonical codec: `potools/formats/nlg_behavior.py`. Blender shows definitions read-only in the
PO Tools behavior panel.

## 1. Identification

A single-section archive is treated as a definition archive when it has:

- a non-data directory chunk `0xE000`, or
- a non-data directory `0x5000` whose first child is an owned `0x5001` whose second word
  is one of the interpreter hashes below.

`AssetDocument` attaches `Definitions` to every section; non-definition archives yield an
empty owner map and are left to other codecs.

Retail paths used as private fixtures (sizes/hashes only in `tests/private_fixtures.json`):

- `characterdefinitions/behaviours.bun.dict`
- `characterdefinitions/glassjoe/glassjoecombodata.dict`
- `characterdefinitions/glassjoe/glassjoeaction.dict`
- `characterdefinitions/glassjoe/glassjoereaction.dict`
- `characterdefinitions/glassjoe/glassjoehudtips.dict`

## 2. Directories

Non-data chunk types accepted as directories: `0x0001`, `0x5000`, `0xE000`, `0x5020`.
Each directory's `first`/`count` names a contiguous run of later chunks. Ownership is
exclusive and must cover every data chunk.

| Directory | Expanded as |
| --- | --- |
| `0x5000` | compiled script (interpreter, classes, optional imports, debug directory) |
| `0xE000` | combo graph (named nodes, transitions, property groups, animation hash lists) |
| `0x5020` | debug directory; consumed as the last child of its script (`count` is 0 or 4) |
| `0x0001` | recorded only; not expanded into a script or combo |

Serialized words that look like addresses are stored raw. They are never followed as
file offsets. Ownership comes from directories, ordered records and checked counts.

## 3. Compiled scripts (`0x50xx`)

Interpreter hashes are case-sensitive `string_to_hash` of:

`BehaviourScriptInterpreter`, `ComboScriptInterpreter`, `CharacterActionScript`,
`CharacterReactionScript`, `FightHudTips`.

`0x5001` compiled module, 32-byte header then three spans:

| Header word | Parser use |
| --- | --- |
| 0 | must be 0 |
| 1 | interpreter hash |
| 2 | module name hash |
| 3 | class count |
| 4 | constant-pool bytes (`pool % 4 == 0`) |
| 5 | code bytes (`code % 2 == 0`) |
| 6 | string-pool bytes |
| 7 | kept as `header_unknown_raw` |

Layout: `len(raw) == 32 + pool + code + strings`. Constants are untyped `u32` words.
Code bytes are hashed and not decoded. String pool is null-terminated `latin1`.
Function `pc` values are in 2-byte units and must fall inside the code span.

Then, per class: `0x5002` (name hash, function count, packed `storage_bytes << 16 |
variable_count`, then 8-byte variables `{type_hash, storage_offset << 16 | index}`)
and `0x5013` (12 bytes per function: name hash, code-unit offset, `signature_raw`).

Optional `0x5011` import directory: a word stream of module count, then per module
`(name_hash, class_count)` and per class `(name_hash, n, n function hashes)`. Import
hashes that match another local module are listed; this is a name-hash index, not a
loader.

Debug children, when present, are `0x5021` `0x5022` `0x5023` `0x5024` in that order.
Payloads are hashed. `0x5022` may contribute hash-id names (`count`, then `count ×
{hash, offset}` into a trailing string table). Other debug words stay opaque.

**Instructions remain opaque.** There is no bytecode editor.

## 4. Combo graphs (`0xE0xx`)

`0xE001` header: first word is node count; total words = `6 + node_count`; remaining
header words stay raw. `0xE002` is the combo name (ASCII, NUL, zero padding).

Each node, in order:

| Chunk | Size / rule |
| --- | --- |
| `0xE010` | 20 bytes; word 2 is the node name hash, word 4 is the node index |
| `0xE011` | 44 bytes metadata; word 5 is animation-hash count; words 7 and 8 are the transition count and must match |
| `0xE012` | node name (ASCII, NUL, zero padding); must hash to `0xE010` word 2 |
| `0xE030` | `68 × transition_count` bytes |
| `0xE013` / `0xE014` | node property group (below) |
| per transition `0xE031` | target name (ASCII); optional property group when `0xE030` word 12 is nonzero |
| two `0xE015` | `4 × animation_count` bytes each: clip-name hashes, then an auxiliary word list kept raw |

Transition records are 17 words. Word 16 is a serial index across the combo. Word 12
is treated as a “has properties” flag, not as a file offset. Target names are resolved
to local node names only (`local name match` or `unresolved (no local name match)`).

## 5. Property groups

`0xE013`: `{unrelocated_word, child_count, child_count × 0xDEADBEEF}`. The first word
is stored raw. Nested groups are bounded to depth 64.

`0xE014` is 12 bytes `{name_hash, tag, value}`:

| Tag | Parser |
| --- | --- |
| 1 | `word_raw` (integer / hash / enum not distinguished) |
| 2 | `float32` at offset 8; non-finite values are stored as `value = None` |
| 3 | nested `0xE013` group |
| other | recorded in `unknown_property_tags`; editing disabled |

Unknown tags parse for inspection and refuse `edit_patchset`.

## 6. Writable fields (two existing floats only)

`AnimProperties`, `TimeScale` and `BlendAmount` are case-sensitive hashes. A node is
editable only when it has exactly one `AnimProperties` group (tag 3) and that group
contains exactly one finite tag-2 float of each name whose **existing** value already
lies in the editor range:

| Field | Existing and written range |
| --- | --- |
| `TimeScale` | 0.0–3.0 |
| `BlendAmount` | 0.0–0.5 |

Those ranges are an offline editor envelope (`nlg_behavior.FLOAT_FIELDS`), not a
runtime guarantee. Other floats (including other tag-2 properties in the same group)
are inspect-only. Duplicate targets, missing/wrong-type fields, unknown tags, non-finite
values, `bool`/`str`, and archive version/flags other than `0x0601` / pad 0 are refused.

Patches replace the 4-byte float in place. Instructions, strings, counts, hashes,
transitions and unestablished words are never written. A no-op value produces an empty
`PatchSet` and a byte-identical rebuild. Restoring the previous value restores the
original bytes (IEEE-754 values used in tests are exact: 0, 0.25, 0.5, 1, 1.5, 2, 3).

## 7. Static executable checks

`tests/corpus/behavior_dol_audit.py` is pinned to the PAL (R7PP01) `main.dol` sha256

`6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137`

It is not a disassembler dump, not a control-flow proof, and not a runtime test. It
rechecks instruction operands and absolute call targets:

- `AnimProperties` hash is constructed at `0x8000A3C0` / `0x8000A400`; `0x8000A40C`
  calls `0x80135E94`.
- `BlendAmount` hash at `0x8000A488` / `0x8000A490`; destination displacement `0x78`;
  `0x8000A4A0` calls `0x80135B54`.
- `TimeScale` hash at `0x8000A4F8` / `0x8000A500`; destination displacement `0x90`;
  `0x8000A510` calls `0x80135B54`.
- Immediate compares to property tags 2, 1 and 3 at `0x80135BB8`, `0x80135C88` and
  `0x80135EF8`.
- Compiled-script header span 32 and size words at +16 / +20 (`0x80166054`,
  `0x8016606C`, `0x80166088`); function-record stride 12 at `0x80166260`.

The codec locates writable floats by hash inside `AnimProperties`, not by the DOL
destination displacements. Those displacements are recorded because the audit pins
them; their containing object is not named here.

## 8. Related tables (not fighter tactics)

`animation_index` matches `0xE015` hashes to `0x7002` clip names in a character
animation archive (case-sensitive). Collisions and duplicates are retained.
`techniques` reads `globaltechniques.bin` (`BTGN` magic `0x4254474E`, version 2, one
column of shader/technique hashes). The corpus scan records numeric overlaps with
model shader hashes and Wwise XML ids; numeric matches alone do not prove references.

## 9. Tests and scans

- `tests/test_behavior.py` — synthetic parse, malformed refusal, byte-identical no-op,
  isolated mutation, unknown-byte preservation, inverse restore of TimeScale and
  BlendAmount, deterministic second rebuild, private fixtures.
- `tests/corpus/corpus_behavior_scan.py` — read-only private corpus; no-op rebuilds;
  isolated in-memory mutation; prints `CORPUS_BEHAVIOR_SCAN_PASS`. JSON reports are
  counts and digests only.
- `tests/corpus/behavior_dol_audit.py` — prints `BEHAVIOR_DOL_AUDIT_PASS`.

Still locked: script instructions, unestablished tag-1 words, other floats, transition
records beyond the serial/target-name/property-flag checks above, debug payloads other
than `0x5022` names, `0x0001` directory role, animation-aux words, and any runtime
effect of editing TimeScale or BlendAmount.
