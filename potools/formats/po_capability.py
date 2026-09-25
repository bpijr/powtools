"""Capability states per content family, so "decoded" is never mistaken for "safe to edit".

Each state is a separate claim with its own evidence; they are not a ladder. A family can be
rebuilt byte-exactly long before any field is editable, and an editable field is not runtime
verified until the game has loaded a changed copy. Only list a state that has evidence.
"""
from dataclasses import dataclass, field

IDENTIFIED = "Identified"                 # role and record boundaries known from bytes
VIEWABLE = "Viewable"                     # decoded for display (viewer, inspector or Blender)
EDITABLE = "Editable offline"             # typed edits exist and pass structural validation
EXACT = "Rebuilt byte-exactly"            # untouched data reproduces its source bytes
BLENDER = "Blender round-trip checked"    # import/export test proves no-op and edit behavior
RUNTIME = "Runtime verified"              # a changed copy was loaded by the game (Dolphin/console)
STATES = (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER, RUNTIME)


@dataclass(frozen=True)
class Capability:
    family: str
    states: frozenset
    note: str
    evidence: dict = field(default_factory=dict)   # state -> where the proof lives

    def status(self):
        """Short label: the reached states in canonical order, and runtime status explicitly."""
        reached = [s for s in STATES if s in self.states and s != RUNTIME]
        text = ", ".join(reached) if reached else "Not decoded"
        return text + ("; runtime verified" if RUNTIME in self.states else "; runtime unverified")


def cap(family, states, note, evidence=None):
    unknown = (set(states) | set(evidence or {})) - set(STATES)
    if unknown:
        raise ValueError(f"Unknown capability state(s) for {family}: {sorted(unknown)}")
    return Capability(family, frozenset(states), note, dict(evidence or {}))


HISTORICAL = "manual Dolphin test during development"

REGISTRY = [
    cap("Archive containers (single section)", (IDENTIFIED, VIEWABLE, EXACT),
        "Every retail single-section archive rebuilds exactly.",
        {EXACT: "po_archive.load_archive round-trip on the full retail corpus"}),
    cap("Cinematic containers (NIS, multi-section)", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER),
        "Experimental. Section/camera inspection and fixed-size camera position edits. Section-aware patch sets preserve "
        "all directory records, offsets and unknown bytes. Resizing and cross-section playback bindings remain locked.",
        {EXACT: "tests/corpus/corpus_cinematic_scan.py: all 260 NIS containers / 1,128 sections",
         EDITABLE: "tests/test_cinematic.py; 407 corpus mutation/isolation checks",
         BLENDER: "tests/blender/blender_cutscene_roundtrip.py: Blender 3.6.1 and 5.2.0, real NIS fixture"}),
    cap("Textures (CMPR)", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER, RUNTIME),
        "Decode/encode and Blender image edits with exact dimensions and full mip chains.",
        {RUNTIME: "green Glass Joe skin, " + HISTORICAL + " (FILE_FORMAT.md section 6)",
         BLENDER: "tests/blender/blender_fighter_roundtrip.py and blender_geometry_roundtrip.py"}),
    cap("Textures (RGB5A3, RGBA32)", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT),
        "Same editor as CMPR; aliased pixel storage (frontend bundles, global.dict) stays read-only."),
    cap("Fighter materials (hippodiffuseskin, 204-byte)", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT),
        "Tint, alpha and specular power only; meshes sharing a record change together."),
    cap("Other material layouts", (IDENTIFIED,),
        "Use Shader material inputs below: texture inputs are decoded; other numeric state stays opaque."),
    cap("Fighter geometry (main model)", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER, RUNTIME),
        "Blender import/export of the main body model; auxiliary vertex attributes preserved.",
        {RUNTIME: "injected rigid UV-sphere, " + HISTORICAL + " (FILE_FORMAT.md section 5)",
         BLENDER: "tests/blender/blender_fighter_roundtrip.py (Bear Hugger, Glass Joe, referee)"}),
    cap("Model sets (all models, nodes, transforms)", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER),
        "Every model set, node and transform. Blender: one object per mesh; export writes moved "
        "vertices, all available proven UV layers and static-object transforms as audited patches. "
        "Cinematic sections stay read-only.",
        {EXACT: "tests/corpus/corpus_model_scan.py (356 sets, no-op patch sets empty); tests/test_model.py",
         EDITABLE: "nlg_asset.write_rebuild audit and reparse; tests/test_model.py",
         BLENDER: "tests/blender/blender_asset_roundtrip.py, Blender 3.6.1 and 5.2.0, ten fixture families"}),
    cap("Skeleton and skinning", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER),
        "Fighter bind poses and palettes; static and compact rigs are not generalized yet.",
        {BLENDER: "tests/blender/blender_fighter_roundtrip.py"}),
    cap("Animation clips", (IDENTIFIED, VIEWABLE, RUNTIME),
        "Sampled pose playback; sample rate unestablished. Encoder exists but is not integrated.",
        {RUNTIME: "delta-quaternion probes, " + HISTORICAL + " (ANIMATION_FORMAT.md)"}),
    cap("Cameras", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER),
        "1,688 directory-owned NIS/sampled clips; existing position and target samples are editable through "
        "Blender path meshes. Experimental. Camera/lens previews do not simulate runtime. Rotation, lens, timing, "
        "new clips and resizing remain locked; hashed controllers are excluded.",
        {IDENTIFIED: "CINEMATICS.md: directory and camera consumer DOL addresses; corpus scanner",
         EXACT: "tests/corpus/corpus_cinematic_scan.py: 2,418 track no-ops and 407 deterministic isolated mutations",
         EDITABLE: "tests/test_cinematic.py",
         BLENDER: "tests/blender/blender_cutscene_roundtrip.py: Blender 3.6.1 and 5.2.0"}),
    cap("Fighter behavior definitions", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT),
        "Experimental. Lossless 0x50xx scripts and 0xE0xx combo graphs; TimeScale 0.0–3.0 and BlendAmount 0.0–0.5 only. Instructions, unestablished words and other floats stay opaque.",
        {IDENTIFIED: "tests/test_behavior.py; tests/corpus/corpus_behavior_scan.py; tests/corpus/behavior_dol_audit.py",
         VIEWABLE: "nlg_behavior.Definitions; read-only Blender behavior panel",
         EDITABLE: "nlg_behavior.edit_patchset; tests/test_behavior.py refusals and isolation",
         EXACT: "tests/test_behavior.py no-op PatchSet; tests/corpus/corpus_behavior_scan.py byte_exact_noops"}),
    cap("Particles and effects (0x40xx)", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER),
        "Experimental. Lossless 0x40xx graph; 0x4000/4002/4004/4020 are table references, not payloads. "
        "The only writable change is a compatible local texture-hash swap at 4003+56 for "
        "single-cell sprite emitters whose texture uniquely resolves in the same archive. "
        "Model emitters, atlas sheets, attachments, world placement, timing and unproven "
        "fields stay inspect-only. Blender markers are display-only; moving them is refused. "
        "ResourceIndex is inspect-only and never authorizes swaps.",
        {IDENTIFIED: "nlg_effect.py; tests/test_effects.py; tests/corpus/effect_dol_scan.py EFFECT_DOL_SCAN_PASS (PAL main.dol sha256 6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137)",
         VIEWABLE: "nlg_effect.inspect; Blender effect markers",
         EDITABLE: "nlg_effect.texture_patches; tests/test_effects.py guards",
         EXACT: "tests/test_effects.py no-op rebuild; tests/corpus/corpus_effect_scan.py CORPUS_EFFECT_SCAN_PASS",
         BLENDER: "tests/blender/blender_effects_roundtrip.py BLENDER_EFFECTS_ROUNDTRIP_PASS, Blender 3.6 and 5.2"}),
    cap("Game audio", (IDENTIFIED, VIEWABLE),
        "Wii DSP-ADPCM encode/decode (nlg_dsp) and Wwise bank/WEM parsing (nlg_wwise). No replacement workflow is included."),
    cap("Crowd, HUD, lighting and damage-state", (IDENTIFIED, VIEWABLE),
        "Inspect-only inventory (chunk type/size/sha256/owner-or-unknown). Crowdskin layouts belong to materials; HUD tips belong to behavior; lighting ramps are material slots; damage names sit at mesh +20. No state-switch editor. Unknown 0x6501-0x6523 and 0x80xx stay unidentified."),
    cap("New fighters, stages, gameplay slots", (),
        "Requires executable/game registration work; out of scope until existing assets are complete."),
    cap("Model topology and culling bounds", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER),
        "Add/delete vertices and faces inside existing mesh slots using original-vertex provenance. "
        "Rebuilds arrays, strips, pointers, counts, morph references and culling bounds together. "
        "Bone palettes and unknown vertex data are preserved. No whole mesh/node insertion or deletion.",
        {EXACT: "tests/corpus/corpus_geometry_scan.py: 356 byte-exact source repacks and controlled topology edits; 273 culling boxes",
         EDITABLE: "tests/test_geometry.py; nlg_asset schema-2 PatchSet relocation and padding audit",
         BLENDER: "tests/blender/blender_geometry_roundtrip.py, Blender 3.6.1 and 5.2.0, synthetic plus four fixture archives"}),
    cap("Blender texture painting through patch sets", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER),
        "Same-size image edits rebuild every mip level; unchanged encoded pixels remain byte-identical. "
        "Aliases and dimension changes are refused.",
        {EXACT: "tests/test_geometry.py; tests/blender/blender_geometry_roundtrip.py no-op checks",
         EDITABLE: "nlg_asset.texture_patches",
         BLENDER: "tests/blender/blender_geometry_roundtrip.py, Blender 3.6.1 and 5.2.0, synthetic RGBA32 and private microphone texture"}),
    cap("Shader material inputs (12 observed layouts)", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER),
        "Shader/span/state guarded local texture-input replacement; skin tint, alpha and specular power. "
        "Unknown shader state is preserved; shared records report all mesh users. Cinematics stay read-only.",
        {IDENTIFIED: "tests/corpus/MATERIAL_CORPUS_AUDIT.json: 6,263 records, 12 exact layouts, 39,007 texture references",
         VIEWABLE: "Blender material panel; tests/test_material_layout.py",
         EDITABLE: "tests/test_material_layout.py; tests/corpus/corpus_material_scan.py: 48 field mutations, range audit and second rebuild",
         EXACT: "tests/corpus/corpus_material_scan.py: 99 ordinary model archives rebuilt byte-exactly twice",
         BLENDER: "tests/blender/blender_material_roundtrip.py: Blender 3.6.1 and 5.2.0, all 12 synthetic/private layouts"}),
    cap("Generic animation node rigs", (IDENTIFIED, VIEWABLE, EXACT, BLENDER),
        "Exact node-directory hierarchy; bind-bone parenting only where hashes identify one local rig. "
        "Ambiguous/missing hierarchies stay flat. Rig structure editing and automatic skin retargeting are locked.",
        {IDENTIFIED: "nlg_animation.Rig; ANIMATION_EDITING.md directory and compatibility evidence",
         EXACT: "tests/corpus/corpus_animation_scan.py: 264 rigs, 181 uniquely matched nonempty bind sets",
         BLENDER: "tests/blender/blender_animation_roundtrip.py and blender_asset_roundtrip.py, Blender 3.6.1/5.2.0"}),
    cap("Source-node rotation, translation and scale keys", (IDENTIFIED, VIEWABLE, EDITABLE, EXACT, BLENDER),
        "Existing samples only, strict format flags and source rig signature. Blender source-node actions "
        "support 3.6 and 5.x; frame rate, engine placement and IK are unestablished. Multi-section export is locked.",
        {IDENTIFIED: "DOL dispatch/loaders and full-corpus directory audit; ANIMATION_EDITING.md",
         EDITABLE: "tests/test_animation.py: malformed/refusal tests and private scale mutation",
         EXACT: "tests/corpus/corpus_animation_scan.py: 491601 writable tracks with empty no-op patch sets",
         BLENDER: "tests/blender/blender_animation_roundtrip.py, seven private families + synthetic, Blender 3.6.1/5.2.0"}),
    cap("Normalized scalar animation tracks (0x7112)", (IDENTIFIED, VIEWABLE, EXACT),
        "u8/255 storage and separate key counts established; final consumer meaning remains unproved. Editing locked.",
        {IDENTIFIED: "DOL 0x801825EC/0x80188C8C and 0x7110 count correlation",
         EXACT: "tests/corpus/corpus_animation_scan.py: 6487 tracks decoded and preserved in exact section rebuilds"}),
]


def by_family(family):
    for c in REGISTRY:
        if c.family == family:
            return c
    raise KeyError(family)


def rows():
    """(family, status, note) rows."""
    return [(c.family, c.status(), c.note) for c in REGISTRY]
