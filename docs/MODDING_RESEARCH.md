# Open modding research questions

Unresolved questions ranked by how testable they are. Nothing here is a runtime claim unless it
says so.

## Ranked unresolved questions

Ranking is a judgment about the next useful experiment, not proof of engine
capabilities. Feasibility and value are qualitative.

| Rank | Question | Feasibility / value | Evidence boundary and discriminating next step |
| --- | --- | --- | --- |
| 1 | Camera lens, timing and bindings | High offline / medium-high | Position/target samples are decoded and editable; `0x5028` is the view angle in radians (see CINEMATICS.md). `0x5026`, shot timing and camera/animation bindings are unresolved. Change one field on a copy and compare framing in Dolphin. |
| 2 | New resource-name registration | Medium / high | Material negative control writes a new local texture hash and resolves it offline without registry insertion. That is not runtime evidence that `hashid.bin` is optional. Compare otherwise identical existing-name and new-name replacements in Dolphin, then a registry-augmented copy only if needed. |
| 3 | Damage-state selection | Low-medium / high | Name filtering is implemented in the importer. No engine selector is identified. External behavior/code control remains a hypothesis, not an exclusion of archive-side control. Trace one known damage material/mesh hash while damage changes; compare visibility with slot-1 texture changes. |
| 4 | Fixed material limits | Medium for current preset, low for new shader / medium | Eight hash/parameter pairs at `0xB016 +0x00..0x3F`, 204-byte records, and mesh material offset at `0xB004 +36` describe the supported preset. New replacement records are implemented. Record count, texture-stage count, shader type and mesh allocation are distinct limits. Runtime-test an added record referenced by an existing mesh before investigating a ninth input. |
| 5 | Replacement versus new selectable roster slot | High for asset replacement, very low for new slot / high | Replacement tooling exists. Historical notes suggest a DOL registry and JoeTemplate, but those executable/behavior references have not been rechecked. Exposing an unused pre-registered slot would not demonstrate arbitrary registration. First confirm one registry entry and its menu/behavior references from a supplied executable. |
