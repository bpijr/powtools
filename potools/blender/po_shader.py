"""
po_shader.py -- the Punch-Out!! hippodiffuseskin material, defined ONCE.

Both addons import this: io_import_punchout builds a material from the archive, and
io_export_punchout reads one back out. Keeping the graph in a single place is the only way an
authored material and an imported one can be guaranteed to behave the same -- and the only way
a round-trip can be bit-perfect.

Lives in potools/blender; the bootstrap puts it and potools/formats on sys.path.
"""

import bpy
from mathutils import Vector

# ---------------------------------------------------------------------------------------
# The retail material is a fixed-function TEV program. Slot 3 is a colour lookup table and
# the surface/light dot product is its coordinate; it is not a conventional diffuse bitmap.
# Blender therefore reconstructs the combiner math and sends the finished colour through an
# Emission shader. Scene lamps and Principled BSDF are deliberately excluded: either would
# light the already-combined Wii colour a second time.
# ---------------------------------------------------------------------------------------

# Direction TOWARDS the key light, world space. PO is Z-up (X = left/right, Y = depth,
# Z = up) and the fighter faces -Y, so this is a front-left-above key.
KEY_LIGHT_DIR = (0.75, -0.50, 0.43)

PO_RAMP_POS = 0.50        # legacy/export inspection sample; live preview is N dot L driven

# Per-vertex INT attribute on imported fighter meshes: VERTEX_SOURCE_SLOT * mesh slot + local
# vertex index + 1 of the archive vertex it came from; 0 = none (Blender fills vertices you join
# or add with 0). Morph records and aux attributes are keyed by that identity on export;
# position alone cannot separate coincident vertices.
VERTEX_SOURCE_ATTR = "po_vertex_source"
VERTEX_SOURCE_SLOT = 65536
# Keep the source colour product intact.  The old 1.16 gain plus 0.28 gray lift was fitted to
# a few screenshots, but it clips neutral/HDR-backed props (DK's barrel and ladder) and turns
# saturated character art pastel.  Arena lights provide exposure; albedo must not do so too.
PO_ALBEDO_GAIN = 0.80
PO_ALBEDO_LIFT = 0.04
PO_GLOW = 0.14            # rim TEV add; source textures already carry highlight intensity
# HDR sphere-map reflection (slot 5 x fresnel slot 6). The exact GX path (po_gx) leaves this term
# off because retail footage does not show it; at PO_GLOW it painted a wet, wavy sheen on skin.
PO_HDR_GLOW = 0.0
PO_AMBIENT = 0.22         # world background level, the arena's soft fill
PO_SPECULAR_SCALE = 0.08  # slot2 mask x slot7 response add (keeps glove colour intact)
SHOW_DAMAGE = False       # damage texture preview: False = clean/normal, True = hurt

# Substrings that mark a material as a damage-state overlay. Taken from the real names across
# bearhugger / glassjoe / littlemac / kinghippo / sodapopinski: bh_nose_damage, gj_damage_cheek,
# kh_belly_damage, sp_brow_damage, lm_mats/blackeye, lm_mats/mark2, bh_bump, kh_bumps ...
DAMAGE_NAME_HINTS = ("damage", "blackeye", "welt", "bruise", "_bump", "bumps", "/mark")
OPTIONAL_DAMAGE_NAME_HINTS = ("bandage",)


def _po_nodes(name):
    for m in bpy.data.materials:
        nd = m.node_tree.nodes.get(name) if m.use_nodes and m.node_tree else None
        if nd:
            yield nd


def po_set_ramp_pos(value):
    """Slide every material along its slot3 ramp (0 = shadow end, 1 = lit end)."""
    n = 0
    for nd in _po_nodes("PO_RampPos"):
        nd.outputs[0].default_value = value; n += 1
    return n


def po_set_albedo_lift(value):
    """The additive term in the albedo. Lower = punchier and more saturated darks,
    higher = washed out. The source-preserving default is zero."""
    n = 0
    for nd in _po_nodes("PO_AlbedoLift"):
        nd.inputs[1].default_value = (value,) * 3; n += 1
    return n


def po_show_damage(show=True):
    """Switch slot-1 damage artwork without hiding its replacement geometry."""
    n = 0
    for nd in _po_nodes("PO_DamageMix"):
        nd.inputs[0].default_value = 1.0 if show else 0.0; n += 1
    # Optional overlays (DK's forehead bandage) genuinely disappear in Normal. Required
    # cheek/lip/torso replacement geometry never receives this node.
    for nd in _po_nodes("PO_DamageVis"):
        nd.inputs["Fac"].default_value = 1.0 if show else 0.0; n += 1
    return n


def po_set_albedo_gain(value):
    """Overall albedo trim across every imported material."""
    n = 0
    for nd in _po_nodes("PO_AlbedoGain"):
        nd.inputs["Scale"].default_value = value; n += 1
    return n


def build_light_rig(strength=1.0, ambient=None):
    """Create an editable key-vector controller, not Blender lamps.

    The shader reads ``KEY_LIGHT_DIR`` directly. The empty records that lighting is TEV math,
    while old preview lamps are disabled when an older scene is migrated.
    """
    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new("PO_World"); bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        bg.inputs["Strength"].default_value = 0.0
    for obj in bpy.data.objects:
        if obj.type == "LIGHT" and obj.name.startswith(("PO_Key", "PO_Fill", "PO_Rim")):
            obj.data.energy = 0.0
            obj.hide_render = True
    control = bpy.data.objects.get("PO_TEV_KeyVector")
    if control is None:
        control = bpy.data.objects.new("PO_TEV_KeyVector", None)
        bpy.context.collection.objects.link(control)
    control.empty_display_type = "SINGLE_ARROW"
    control.empty_display_size = 0.7
    v = Vector(KEY_LIGHT_DIR).normalized()
    control.rotation_euler = v.to_track_quat("Z", "Y").to_euler()
    control["po_tev_light_vector"] = list(v)
    control["po_note"] = "Preview lighting is N dot L TEV math; this is not a Blender lamp."
    return [control]





# =======================================================================================
# MATERIAL AUTHORING
# Every boxer material is the same 'hippodiffuseskin' preset: same 8 slots, same 204B record.
# So authoring a custom character does not need a new shader -- it needs these templates, the
# ramps/detail maps painted, and the whole lot written back. po_build_shader() below is the
# single definition of that node graph: the importer builds one from the archive, and
# po_new_material() builds the same graph from a preset, so what you author is what you get.
# =======================================================================================

RIM_DEFAULT = [(0.0, (0.49, 0.49, 0.35)), (0.22, (0.0, 0.0, 0.0)), (1.0, (0.0, 0.0, 0.0))]
FRESNEL_DEFAULT = [(0.0, (1.0, 1.0, 1.0)), (0.40, (0.0, 0.0, 0.0)), (1.0, (0.0, 0.0, 0.0))]

# Ramp end-points below are the real measured values from Bear Hugger's shipped ramps, so a
# fresh template already sits in the range the game's own art occupies.
PO_PRESETS = {
    "skin":  dict(spec_power=32.0, ramp=[(0.0, (0.451, 0.286, 0.259)), (1.0, (0.647, 0.475, 0.443))]),
    "cloth": dict(spec_power=2.0,  ramp=[(0.0, (0.416, 0.486, 0.478)), (1.0, (0.604, 0.667, 0.678))]),
    "hair":  dict(spec_power=8.0,  ramp=[(0.0, (0.224, 0.110, 0.000)), (1.0, (0.388, 0.173, 0.031))]),
    "glove": dict(spec_power=64.0, ramp=[(0.0, (0.424, 0.086, 0.000)), (1.0, (0.596, 0.118, 0.000))], glow=True),
    "metal": dict(spec_power=64.0, ramp=[(0.0, (0.518, 0.271, 0.071)), (1.0, (0.612, 0.412, 0.259))], glow=True),
    "boot":  dict(spec_power=8.0,  ramp=[(0.0, (0.420, 0.263, 0.063)), (1.0, (0.580, 0.396, 0.192))]),
    "flat":  dict(spec_power=32.0, ramp=[(0.0, (0.500, 0.500, 0.500)), (1.0, (0.700, 0.700, 0.700))]),
}


def _po_ramp_node(nt, stops, loc, label, name):
    n = nt.nodes.new("ShaderNodeValToRGB")
    n.location = loc; n.label = label; n.name = name
    cr = n.color_ramp; cr.interpolation = "LINEAR"
    while len(cr.elements) > 1:
        cr.elements.remove(cr.elements[-1])
    for i, (pos, c) in enumerate(stops):
        e = cr.elements[0] if i == 0 else cr.elements.new(pos)
        e.position = pos
        e.color = (c[0], c[1], c[2], 1.0)
    return n


def _po_img_node(nt, img, loc, name):
    t = nt.nodes.new("ShaderNodeTexImage")
    t.location = loc; t.name = name
    # GX uses filtered sampling for these TEV inputs. Point sampling exaggerated the CMPR
    # blocks into the conspicuous pixel stair-steps visible on DK's fur and gloves.
    t.image = img; t.interpolation = "Linear"; t.extension = "REPEAT"
    try:
        img.colorspace_settings.name = "Non-Color"      # GX blends on gamma values
    except Exception:
        pass
    return t


def _po_vec(nt, op, loc):
    n = nt.nodes.new("ShaderNodeVectorMath"); n.operation = op; n.location = loc
    return n


# ---------------------------------------------------------------------------------------
# The hippodiffuseskin program as two shared node groups.
#
#   PO Skin TexGen   geometry -> the texture coordinates GX generates for each texture map
#   PO Skin TEV      sampled textures + record switches -> the TEV stage math
#
# Every material keeps its texture slots at the top level (the exporter finds them there by
# name) and wires them between the two groups; open a group (Tab) to read its math. Both are
# transcribed from main.dol by symbolic execution; see decomp/research/rendering_pipeline.md
# and po_gx.program_skin. Two constants are not decoded but calibrated against retail footage
# (po_gx.RIM_LIGHT_KSEL, po_gx.ENV_MAP_LEVEL_SCALE); they are separate group inputs so they can
# be told apart from what the executable says.
# ---------------------------------------------------------------------------------------

SKIN_GROUP_VERSION = 3
TEXGEN_GROUP = "PO Skin TexGen"
TEV_GROUP = "PO Skin TEV"
# Character light: normalize(80412D54), set by the presentation script to (2.5, 3.65, 1.0);
# the other character gets (-x, -y, z). The mirrored one matches the opponent in a bout (checked
# against Dolphin footage of Glass Joe), so it is the default.
CHAR_LIGHT = tuple(Vector((2.5, 3.65, 1.0)).normalized())
BOUT_LIGHT = tuple(Vector((-2.5, -3.65, 1.0)).normalized())
RIM_CONSTANT = 0.25        # po_gx.RIM_LIGHT_KSEL 1/4: calibrated, the executable reads K0
ENV_LEVEL_SCALE = 0.0      # po_gx.ENV_MAP_LEVEL_SCALE: calibrated, the executable reads 1
# hippodiffuseskin record offsets (the shader's own parameter table, 8033B270)
SKIN_PARAMS = dict(fresnelpower=(0x80, "f"), specpower=(0x84, "f"), rimlight=(0x90, "I"),
                   additiverimlight=(0x94, "I"), enabledamagetexture=(0x98, "I"),
                   enableenvmap=(0xB0, "I"), envmaphorizscale=(0xB4, "f"),
                   envmapvertscale=(0xB8, "f"), envmaptexturelevel=(0xBC, "f"))
# What a record without a source (a new custom material) gets: the retail majority.
SKIN_DEFAULTS = dict(fresnelpower=0.0, specpower=32.0, rimlight=1, additiverimlight=1,
                     enabledamagetexture=0, enableenvmap=0, envmaphorizscale=1.0,
                     envmapvertscale=1.0, envmaptexturelevel=1.0)


def skin_params(record=None, spec_power=None):
    """hippodiffuseskin switches and scalars from a 204-byte record (or the defaults)."""
    import struct
    out = dict(SKIN_DEFAULTS)
    if record:
        for k, (off, fmt) in SKIN_PARAMS.items():
            if off + 4 <= len(record):
                out[k] = struct.unpack_from(">" + fmt, record, off)[0]
    if spec_power is not None:
        out["specpower"] = float(spec_power)
    return out


def _socket(ng, name, in_out, kind, default=None, lo=None, hi=None):
    """Add a group socket on Blender 3.x (inputs/outputs) and 4.x+ (interface)."""
    stype = {"COLOR": "NodeSocketColor", "FLOAT": "NodeSocketFloat",
             "VECTOR": "NodeSocketVector"}[kind]
    if hasattr(ng, "interface"):
        s = ng.interface.new_socket(name=name, in_out=in_out, socket_type=stype)
    else:
        s = (ng.inputs if in_out == "INPUT" else ng.outputs).new(stype, name)
    if default is not None and hasattr(s, "default_value"):
        s.default_value = default
    if lo is not None and hasattr(s, "min_value"):
        s.min_value = lo
    if hi is not None and hasattr(s, "max_value"):
        s.max_value = hi
    return s


class _Group:
    """Small node-building helper for the shared groups: column layout and frames."""

    # Each section is a frame holding a small grid of nodes; sections sit side by side.
    COL_W, ROW_H, PER_COL, GAP = 220, 260, 4, 140

    def __init__(self, ng):
        self.ng, self.nodes, self.links = ng, ng.nodes, ng.links
        self.x, self.k, self.frame, self.width = 0, 0, None, 0

    def at(self, col, frame_label=None):
        """Start the next section. `col` only orders sections; frame_label names it (with the
        full formula in the frame's text, readable in the sidebar)."""
        if self.k or self.frame is not None:
            self.x += self.width + self.GAP
        self.k, self.width = 0, self.COL_W
        if frame_label:
            self.frame = self.nodes.new("NodeFrame")
            title, _, detail = frame_label.partition(": ")
            self.frame.label = title
            self.frame.label_size = 20
            if detail:
                self.frame["po_formula"] = detail
        else:
            self.frame = None

    def new(self, kind, label=None, **props):
        n = self.nodes.new(kind)
        for k, v in props.items():
            setattr(n, k, v)
        col, row = divmod(self.k, self.PER_COL)
        n.location = (self.x + col * self.COL_W, -row * self.ROW_H)
        self.width = max(self.width, (col + 1) * self.COL_W)
        self.k += 1
        n.hide = kind in ("ShaderNodeVectorMath", "ShaderNodeMath") and not label
        if label:
            n.label = label
        if self.frame is not None:
            n.parent = self.frame
        return n

    def link(self, a, b):
        self.links.new(a, b)

    def vmath(self, op, a, b=None, label=None):
        n = self.new("ShaderNodeVectorMath", label, operation=op)
        for i, x in enumerate((a, b)):
            if x is None:
                continue
            if isinstance(x, (tuple, list)):
                n.inputs[i].default_value = x
            else:
                self.link(x, n.inputs[i])
        return n.outputs[1] if op in ("DOT_PRODUCT", "LENGTH") else n.outputs[0]

    def scale(self, v, s, label=None):
        n = self.new("ShaderNodeVectorMath", label, operation="SCALE")
        self.link(v, n.inputs[0])
        if isinstance(s, (int, float)):
            n.inputs["Scale"].default_value = s
        else:
            self.link(s, n.inputs["Scale"])
        return n.outputs[0]

    def math(self, op, a, b=None, c=None, label=None, clamp=False):
        n = self.new("ShaderNodeMath", label, operation=op, use_clamp=clamp)
        for i, x in enumerate((a, b, c)):
            if x is None:
                continue
            if isinstance(x, (int, float)):
                n.inputs[i].default_value = float(x)
            else:
                self.link(x, n.inputs[i])
        return n.outputs[0]

    def clamp01(self, v, label=None):
        lo = self.vmath("MAXIMUM", v, (0.0, 0.0, 0.0))
        return self.vmath("MINIMUM", lo, (1.0, 1.0, 1.0), label or "clamp 0..1 (TEV)")

    def lerp(self, a, b, t, label=None):
        """(1 - t)·a + t·b for vectors a, b and scalar t."""
        d = self.vmath("SUBTRACT", b, a)
        return self.vmath("ADD", a, self.scale(d, t), label)

    def xyz(self, x, y, z=0.0, label=None):
        n = self.new("ShaderNodeCombineXYZ", label)
        for i, c in enumerate((x, y, z)):
            if isinstance(c, (int, float)):
                n.inputs[i].default_value = float(c)
            else:
                self.link(c, n.inputs[i])
        return n.outputs[0]

    def split(self, v):
        n = self.new("ShaderNodeSeparateXYZ")
        self.link(v, n.inputs[0])
        return n.outputs[0], n.outputs[1], n.outputs[2]


def _ensure_group(name, build):
    ng = bpy.data.node_groups.get(name)
    if ng is not None and ng.get("po_version") == SKIN_GROUP_VERSION:
        return ng
    if ng is None:
        ng = bpy.data.node_groups.new(name, "ShaderNodeTree")
    else:                                   # rebuild in place: every material updates at once
        ng.nodes.clear()
        if hasattr(ng, "interface"):
            ng.interface.clear()
        else:
            ng.inputs.clear(); ng.outputs.clear()
    build(ng)
    ng["po_version"] = SKIN_GROUP_VERSION
    return ng


def _build_texgen(ng):
    g = _Group(ng)
    _socket(ng, "Light", "INPUT", "VECTOR", BOUT_LIGHT)
    _socket(ng, "Spec Power", "INPUT", "FLOAT", 32.0, 0.0, 256.0)
    _socket(ng, "Fresnel Power", "INPUT", "FLOAT", 0.0)
    _socket(ng, "Env H Scale", "INPUT", "FLOAT", 1.0)
    _socket(ng, "Env V Scale", "INPUT", "FLOAT", 1.0)
    for name in ("UV0", "UV1 (damage)", "UV2 (spec mask)", "Ramp", "Spec", "Fresnel", "Rim", "Sphere"):
        _socket(ng, name, "OUTPUT", "VECTOR")
    for name in ("Ramp S", "Spec S", "Fresnel S", "Rim S"):
        _socket(ng, name, "OUTPUT", "FLOAT")
    g.at(-4)
    gi = g.new("NodeGroupInput")
    g.at(-3, "View space (GX TEXMTX0: normal matrix; +Z toward the viewer)")
    geo = g.new("ShaderNodeNewGeometry")
    vt = g.new("ShaderNodeVectorTransform", "normal: world to camera", vector_type="VECTOR",
               convert_from="WORLD", convert_to="CAMERA")
    g.link(geo.outputs["Normal"], vt.inputs[0])
    n = g.vmath("NORMALIZE", g.vmath("MULTIPLY", vt.outputs[0], (1.0, 1.0, -1.0), "flip Z"), label="N")
    lt = g.new("ShaderNodeVectorTransform", "light: world to camera", vector_type="VECTOR",
               convert_from="WORLD", convert_to="CAMERA")
    g.link(gi.outputs["Light"], lt.inputs[0])
    l = g.vmath("NORMALIZE", g.vmath("MULTIPLY", lt.outputs[0], (1.0, 1.0, -1.0), "flip Z"), label="L")
    nx, ny, nz = g.split(n)
    uv = []
    g.at(-2, "UV sets: TEX0 0xCC, TEX1 0x05, TEX2 0x3D")
    for name in ("UV", "UV1", "UV2"):
        node = g.new("ShaderNodeUVMap", name, uv_map=name)
        uv.append(node.outputs[0])
    g.at(-1, "PTTEXMTX3 ramp: s = 0.5 N.L + 0.5, t = 0")
    ramp_s = g.math("MULTIPLY_ADD", g.vmath("DOT_PRODUCT", n, l), 0.5, 0.5, "0.5 N.L + 0.5")
    ramp = g.xyz(ramp_s, 1.0)
    g.at(0, "PTTEXMTX2 spec: s = N.H, t = specpower / 128")
    h = g.vmath("NORMALIZE", g.vmath("ADD", l, (0.0, 0.0, 1.0)), label="H = normalize(L + V)")
    spec_s = g.vmath("DOT_PRODUCT", n, h, "N.H")
    spec = g.xyz(spec_s, g.math("MULTIPLY_ADD", gi.outputs["Spec Power"], -1.0 / 128.0, 1.0, "v = 1 - p/128"))
    g.at(1, "PTTEXMTX1 fresnel: s = ((f2 f3 - 1) Nz + 1) / ((f3 - 1) Nz + 1), f2 = 0")
    q = g.math("MULTIPLY_ADD", g.math("SUBTRACT", gi.outputs["Fresnel Power"], 1.0), nz, 1.0, "q")
    fres_s = g.math("DIVIDE", g.math("SUBTRACT", 1.0, nz), q, label="s / q")
    fres = g.xyz(fres_s, 1.0)
    g.at(2, "PTTEXMTX4 rim: q = 0, so s = Nz / 2, t = (1 - Ny) / 2")
    rim_s = g.math("MULTIPLY", nz, 0.5)
    rim = g.xyz(rim_s, g.math("MULTIPLY_ADD", ny, 0.5, 0.5, "v = 1 - t"))
    g.at(3, "PTTEXMTX5 sphere map")
    sph_s = g.math("MULTIPLY_ADD", nx, g.math("MULTIPLY", gi.outputs["Env H Scale"], 0.5), 0.5)
    sph_t = g.math("MULTIPLY_ADD", ny, g.math("MULTIPLY", gi.outputs["Env V Scale"], 0.5), 0.5, "v = 1 - t")
    sphere = g.xyz(sph_s, sph_t)
    g.at(5)
    go = g.new("NodeGroupOutput")
    for name, sock in zip(("UV0", "UV1 (damage)", "UV2 (spec mask)", "Ramp", "Spec", "Fresnel", "Rim",
                           "Sphere", "Ramp S", "Spec S", "Fresnel S", "Rim S"),
                          uv + [ramp, spec, fres, rim, sphere, ramp_s, spec_s, fres_s, rim_s]):
        g.link(sock, go.inputs[name])


def _build_tev(ng):
    g = _Group(ng)
    W = (1.0, 1.0, 1.0, 1.0)
    for name, kind, d, lo, hi in (
            ("Detail", "COLOR", W, None, None), ("Detail Alpha", "FLOAT", 1.0, 0.0, 1.0),
            ("Damage", "COLOR", W, None, None), ("Damage Level", "FLOAT", 0.0, 0.0, 1.0),
            ("Spec Mask", "COLOR", W, None, None), ("Ramp", "COLOR", W, None, None),
            ("Rim", "COLOR", (0.0, 0.0, 0.0, 1.0), None, None),
            ("Gloss", "COLOR", (0.0, 0.0, 0.0, 1.0), None, None),
            ("Fresnel", "COLOR", W, None, None), ("Spec (fresnel)", "COLOR", W, None, None),
            ("Spec (N.H)", "COLOR", W, None, None), ("Light Intensity", "FLOAT", 1.0, 0.0, 1.0),
            ("Rim Light", "FLOAT", 1.0, 0.0, 1.0), ("Additive Rim", "FLOAT", 1.0, 0.0, 1.0),
            ("Rim Constant (calibrated)", "FLOAT", RIM_CONSTANT, 0.0, 1.0),
            ("Env Map", "FLOAT", 0.0, 0.0, 1.0), ("Env Level", "FLOAT", 1.0, 0.0, 4.0),
            ("Env Scale (calibrated)", "FLOAT", ENV_LEVEL_SCALE, 0.0, 1.0)):
        _socket(ng, name, "INPUT", kind, d, lo, hi)
    _socket(ng, "Color", "OUTPUT", "COLOR")
    _socket(ng, "Alpha", "OUTPUT", "FLOAT")
    g.at(-1)
    gi = g.new("NodeGroupInput")
    i = gi.outputs
    k0 = i["Light Intensity"]
    g.at(0, "Stages 0-1: REG0 = K0 x ramp3 x detail0 (x mix(1, damage1, level) when enabled)")
    dmg = g.lerp((1.0, 1.0, 1.0), i["Damage"], i["Damage Level"], "mix(1, damage, level)")
    c = g.clamp01(g.scale(i["Ramp"], k0, "K0 x ramp"))
    reg0 = g.clamp01(g.vmath("MULTIPLY", g.vmath("MULTIPLY", c, i["Detail"]), dmg), "REG0")
    g.at(1, "Stages 2-3: C = K0 x specramp7(fresnel) x specramp7(N.H)")
    sp = g.clamp01(g.scale(i["Spec (fresnel)"], k0, "K0 x specramp(fresnel)"))
    sp = g.clamp01(g.vmath("MULTIPLY", sp, i["Spec (N.H)"]), "REG1")
    g.at(2, "Env map (enableenvmap): C = REG1 + K1 x gloss5 x fresnel6")
    kenv = g.math("MULTIPLY", i["Env Level"], i["Env Scale (calibrated)"], label="K1", clamp=True)
    gl = g.clamp01(g.scale(i["Gloss"], kenv, "K1 x gloss"))
    env = g.clamp01(g.vmath("ADD", sp, g.vmath("MULTIPLY", gl, i["Fresnel"])))
    sp = g.lerp(sp, env, i["Env Map"], "env on/off")
    g.at(3, "Stage 4: C = REG0 + C x specmask2")
    c = g.clamp01(g.vmath("ADD", reg0, g.vmath("MULTIPLY", sp, i["Spec Mask"])), "C")
    g.at(4, "Stage 5 (rimlight): additive C + K x rimramp4, else 2 x C x rimramp4")
    add = g.clamp01(g.vmath("ADD", c, g.scale(i["Rim"], i["Rim Constant (calibrated)"])), "additive rim")
    mul = g.clamp01(g.scale(g.vmath("MULTIPLY", c, i["Rim"]), 2.0), "multiplied rim")
    rim = g.lerp(mul, add, i["Additive Rim"], "additive?")
    out = g.lerp(c, rim, i["Rim Light"], "rim on/off")
    g.at(6)
    go = g.new("NodeGroupOutput")
    g.link(out, go.inputs["Color"])
    g.link(i["Detail Alpha"], go.inputs["Alpha"])


def skin_groups():
    """The shared TexGen and TEV groups, (re)built when missing or out of date."""
    return _ensure_group(TEXGEN_GROUP, _build_texgen), _ensure_group(TEV_GROUP, _build_tev)


def po_build_shader(m, cfg):
    """Build the hippodiffuseskin graph on material `m` from `cfg`. Single source of truth for
    both the importer and po_new_material, so authored materials and imported ones behave
    identically. cfg keys: ramp (stops), ramp_image, detail (Image|None), damage (Image|None),
    specmask, rim (stops), rim_image, hdr, fresnel (stops), fresnel_image, specramp (stops),
    specramp_image, record (204 bytes), tint (+0x9C, the outline colour), spec_power, alpha,
    hurt, optional_damage, tex_names, light."""
    texgen, tev = skin_groups()
    m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    L, N = nt.links, nt.nodes
    params = skin_params(cfg.get("record"), cfg.get("spec_power"))

    tg = N.new("ShaderNodeGroup"); tg.node_tree = texgen; tg.name = "PO_TexGen"; tg.location = (-1500, 0)
    tg.label = "texture coordinates (GX texgen)"
    tg.inputs["Spec Power"].default_value = params["specpower"]
    tg.inputs["Fresnel Power"].default_value = params["fresnelpower"]
    tg.inputs["Env H Scale"].default_value = params["envmaphorizscale"]
    tg.inputs["Env V Scale"].default_value = params["envmapvertscale"]
    light = N.new("ShaderNodeCombineXYZ"); light.name = "PO_LightVector"; light.location = (-1750, 0)
    light.label = "character light (world, towards the light)"
    for k, v in enumerate(cfg.get("light") or BOUT_LIGHT):
        light.inputs[k].default_value = v
    L.new(light.outputs[0], tg.inputs["Light"])
    tv = N.new("ShaderNodeGroup"); tv.node_tree = tev; tv.name = "PO_TEV"; tv.location = (-200, 0)
    tv.label = "hippodiffuseskin TEV stages"
    tv.inputs["Rim Light"].default_value = 1.0 if params["rimlight"] else 0.0
    tv.inputs["Additive Rim"].default_value = 1.0 if params["additiverimlight"] else 0.0
    tv.inputs["Env Map"].default_value = 1.0 if params["enableenvmap"] else 0.0
    tv.inputs["Env Level"].default_value = params["envmaptexturelevel"]

    def img(name, image, coord, y, extension="REPEAT", label=None):
        t = _po_img_node(nt, image, (-1100, y), name)
        t.extension = extension
        if label: t.label = label
        L.new(tg.outputs[coord], t.inputs["Vector"])
        return t

    def lut(slot_name, stops, image, coord, fac, y, label, sample_name=None):
        """Exact texture (preview) + editable ColorRamp (export source) + switch between them."""
        ramp = _po_ramp_node(nt, stops, (-800, y - 120), "editable %s (export source)" % label, slot_name)
        L.new(tg.outputs[fac], ramp.inputs["Fac"])
        if image is None:
            return ramp.outputs["Color"]
        tex = img(sample_name or slot_name + "Texture", image, coord, y, "EXTEND", "exact %s" % label)
        choose = N.new("ShaderNodeMixRGB"); choose.location = (-500, y)
        choose.name = slot_name + "Source"; choose.label = "0 exact / 1 edited ramp"
        choose.inputs[0].default_value = 0.0
        L.new(tex.outputs["Color"], choose.inputs[1]); L.new(ramp.outputs["Color"], choose.inputs[2])
        return choose.outputs[0]

    # Slots that resolve to a constant global texture (global/black, global/white) feed that
    # constant; a missing spec mask is black, which is what the exporter writes for one.
    const = cfg.get("slot_const") or {}
    for slot, sock in ((0, "Detail"), (2, "Spec Mask"), (4, "Rim"), (5, "Gloss"), (6, "Fresnel")):
        if slot in const:
            tv.inputs[sock].default_value = tuple(const[slot]) + (1.0,)
    if cfg.get("specmask") is None and 2 not in const:
        tv.inputs["Spec Mask"].default_value = (0.0, 0.0, 0.0, 1.0)
    if cfg.get("detail") is not None:
        d = img("PO_Detail", cfg["detail"], "UV0", 600, label="slot 0 detail (albedo)")
        L.new(d.outputs["Color"], tv.inputs["Detail"]); L.new(d.outputs["Alpha"], tv.inputs["Detail Alpha"])
    # Slot 1 is the bruise/black-eye artwork: technique 3 multiplies mix(1, damage, level) in.
    damage_img = cfg.get("damage")
    if damage_img is not None and not isinstance(damage_img, bool):
        dt = img("PO_Damage", damage_img, "UV1 (damage)", 400, label="slot 1 damage")
        dm = N.new("ShaderNodeMixRGB"); dm.location = (-800, 400)
        dm.name = "PO_DamageMix"; dm.label = "Normal / Hurt"
        dm.inputs[0].default_value = 1.0 if SHOW_DAMAGE else 0.0
        dm.inputs[1].default_value = (1.0, 1.0, 1.0, 1.0)
        L.new(dt.outputs["Color"], dm.inputs[2])
        L.new(dm.outputs[0], tv.inputs["Damage"])
        tv.inputs["Damage Level"].default_value = 1.0
    if cfg.get("specmask") is not None:
        s = img("PO_SpecMask", cfg["specmask"], "UV2 (spec mask)", 200, label="slot 2 spec mask")
        L.new(s.outputs["Color"], tv.inputs["Spec Mask"])
    L.new(lut("PO_Ramp", cfg["ramp"], cfg.get("ramp_image"), "Ramp", "Ramp S", 0, "slot 3 cell ramp"),
          tv.inputs["Ramp"])
    if cfg.get("rim"):
        L.new(lut("PO_RimRamp", cfg["rim"], cfg.get("rim_image"), "Rim", "Rim S", -300, "slot 4 rim ramp"),
              tv.inputs["Rim"])
    if cfg.get("hdr") is not None:
        h = img("PO_Hdr", cfg["hdr"], "Sphere", -600, "EXTEND", "slot 5 gloss (sphere map)")
        L.new(h.outputs["Color"], tv.inputs["Gloss"])
        L.new(lut("PO_Fresnel", cfg.get("fresnel") or FRESNEL_DEFAULT, cfg.get("fresnel_image"), "Rim",
                  "Rim S", -800, "slot 6 fresnel"), tv.inputs["Fresnel"])
    spec_stops = cfg.get("specramp") or [(0.0, (0, 0, 0)), (1.0, (1, 1, 1))]
    spec_nh = lut("PO_SpecRamp", spec_stops, cfg.get("specramp_image"), "Spec", "Spec S", -1100,
                  "slot 7 spec ramp at N.H", "PO_SpecRampTexture")
    if cfg.get("specramp_image") is not None:
        sf = img("PO_SpecRampFresnel", cfg["specramp_image"], "Fresnel", -1400, "EXTEND",
                 "exact slot 7 spec ramp at fresnel")
        L.new(spec_nh, tv.inputs["Spec (N.H)"])
        L.new(sf.outputs["Color"], tv.inputs["Spec (fresnel)"])
    elif cfg.get("specramp_missing"):
        # A shared texture we could not load (global/specramp without global.dict). Its
        # ColorRamp is only a white placeholder, which would light every masked surface to
        # full white; preview no specular instead and say so.
        tv.inputs["Spec (N.H)"].default_value = (0.0, 0.0, 0.0, 1.0)
        tv.label = "hippodiffuseskin TEV stages (spec ramp texture not found: specular off)"
    else:
        sfr = _po_ramp_node(nt, spec_stops, (-800, -1400), "slot 7 spec ramp at fresnel", "PO_SpecRampFresnel")
        L.new(tg.outputs["Fresnel S"], sfr.inputs["Fac"]); L.new(sfr.outputs["Color"], tv.inputs["Spec (fresnel)"])
        L.new(spec_nh, tv.inputs["Spec (N.H)"])

    # +0x9C is outlinecolour in the shader's own parameter table, not a light tint. Kept for
    # export (the record value round-trips through it); nothing in the surface reads it.
    tint = cfg.get("tint", (1.0, 1.0, 1.0))
    t = N.new("ShaderNodeCombineXYZ"); t.location = (-200, -500)
    t.label = "outlinecolour (+0x9C), not lighting"; t.name = "PO_Tint"
    for k, v in enumerate(tint):
        t.inputs[k].default_value = v
    rp = N.new("ShaderNodeValue"); rp.location = (-200, -650)
    rp.label = "Legacy export ramp sample"; rp.name = "PO_RampPos"
    rp.outputs[0].default_value = PO_RAMP_POS

    out = N.new("ShaderNodeOutputMaterial"); out.location = (500, 0)
    gamma = N.new("ShaderNodeGamma"); gamma.location = (60, 0); gamma.name = "PO_GammaToLinear"
    # TEV combines display-encoded values; this linearises the result so Blender's Standard
    # sRGB view transform encodes it back to exactly what the console writes.
    gamma.inputs["Gamma"].default_value = 2.2
    L.new(tv.outputs["Color"], gamma.inputs["Color"])
    emission = N.new("ShaderNodeEmission"); emission.location = (260, 0); emission.name = "PO_TEV_Output"
    emission.inputs["Strength"].default_value = 1.0
    L.new(gamma.outputs["Color"], emission.inputs["Color"])
    surface = emission.outputs["Emission"]

    # These records are replacement pieces of the character surface, not optional overlay
    # polygons. They remain opaque in both states; only their slot-1 multiply changes.
    # `damage is True` keeps compatibility with callers using the old boolean API.
    if cfg.get("hurt") or cfg.get("damage") is True:
        m["po_damage_mesh"] = True
    if cfg.get("optional_damage"):
        transparent = N.new("ShaderNodeBsdfTransparent"); transparent.location = (260, 300)
        visible = N.new("ShaderNodeMixShader"); visible.location = (400, 250)
        visible.name = "PO_DamageVis"; visible.label = "Normal hidden / Hurt visible"
        visible.inputs[0].default_value = 1.0 if SHOW_DAMAGE else 0.0
        L.new(transparent.outputs[0], visible.inputs[1]); L.new(surface, visible.inputs[2])
        surface = visible.outputs[0]
        m["po_damage_mesh"] = True
        m["po_optional_damage"] = True
        try: m.surface_render_method = "DITHERED"
        except AttributeError: pass
    alpha = float(cfg.get("alpha", 1.0))
    if alpha < 0.999:
        tr2 = N.new("ShaderNodeBsdfTransparent"); tr2.location = (260, 450)
        mx2 = N.new("ShaderNodeMixShader"); mx2.location = (400, 400); mx2.name = "PO_Alpha"
        L.new(tr2.outputs["BSDF"], mx2.inputs[1]); L.new(surface, mx2.inputs[2])
        mx2.inputs["Fac"].default_value = alpha
        surface = mx2.outputs["Shader"]
        for attr, val in (("blend_method", "HASHED"), ("surface_render_method", "DITHERED")):
            try: setattr(m, attr, val)
            except Exception: pass
    L.new(surface, out.inputs["Surface"])
    try:
        m.use_backface_culling = True
    except Exception:
        pass
    m["po_material"] = True
    m["po_tint"] = list(tint)
    m["po_spec_power"] = params["specpower"]
    m["po_alpha"] = alpha
    for k, v in (cfg.get("tex_names") or {}).items():
        m["po_tex_slot%d" % int(k)] = v
    stamp_fingerprints(m)
    return m


def po_new_material(name, preset="skin", detail=None, glow=None, tint=(0.675, 0.400, 0.149)):
    """Create a fresh, game-accurate PO material ready to author against.

        mat = po_new_material("bry_jacket", "cloth")

    Edit the 'diffuse ramp (slot3)' ColorRamp for the colour, drop an image into PO_Detail for
    the pattern, then po_export_materials() bakes both back into the archive."""
    p = dict(PO_PRESETS.get(preset) or PO_PRESETS["flat"])
    m = bpy.data.materials.new(name)
    cfg = dict(ramp=p["ramp"], rim=RIM_DEFAULT, fresnel=FRESNEL_DEFAULT,
               detail=detail, tint=tint, spec_power=p["spec_power"], alpha=1.0,
               hdr=None, tex_names={})
    if (p.get("glow") if glow is None else glow):
        # a placeholder sphere map so the bloom path exists and is editable
        img = bpy.data.images.new(name + "_hdr", 64, 64, alpha=True)
        cfg["hdr"] = img
    po_build_shader(m, cfg)
    m["po_preset"] = preset
    return m




def ramp_to_rgba(node, w=128, h=8):
    """Bake a ColorRamp into raw RGBA bytes at the size PO ships its ramps (128x8 covers the
    128x4 records; CMPR wants multiples of 8). Values are gamma, exactly as the game stores them."""
    cr = node.color_ramp
    rows = bytearray()
    for x in range(w):
        c = cr.evaluate(x / (w - 1.0))
        rows += bytes((max(0, min(255, int(round(c[0] * 255)))),
                       max(0, min(255, int(round(c[1] * 255)))),
                       max(0, min(255, int(round(c[2] * 255)))), 255))
    return bytes(rows) * h          # every row identical -> no punch-through surprises


def image_to_rgba(img):
    """Blender image -> top-down RGBA bytes. Blender's buffer is bottom-up, and these images are
    Non-Color so the floats already are the gamma values the game stores."""
    w, h = img.size
    px = list(img.pixels)
    out = bytearray(w * h * 4)
    for y in range(h):
        src = (h - 1 - y) * w * 4
        dst = y * w * 4
        for i in range(w * 4):
            out[dst + i] = max(0, min(255, int(round(px[src + i] * 255))))
    return bytes(out), w, h


# =======================================================================================
# MATERIAL I/O -- one code path, shared by the importer and the exporter.
#
# This is deliberately free of any Blender scene state (no objects, no armature, no ops), so
# the whole import->export round-trip can be exercised headlessly against a real archive. That
# test is the only thing that can prove a material survives the trip unchanged.
# =======================================================================================
import struct as _struct
import zlib as _zlib

GLOBAL_CONST = {0x72AA2940: (1.0, 1.0, 1.0), 0x713033FC: (0.0, 0.0, 0.0)}


def _tex_table(archive):
    th = archive.find_chunks(type_id=0xB601)
    if not th:
        return {}
    hdr = archive.get_chunk_bytes(th[0]); out = {}
    for i in range(len(hdr) // 96):
        o = i * 96
        out[_struct.unpack_from(">I", hdr, o)[0]] = (
            _struct.unpack_from(">H", hdr, o + 4)[0], _struct.unpack_from(">H", hdr, o + 6)[0],
            hdr[o + 13], _struct.unpack_from(">I", hdr, o + 20)[0])
    return out


def _ramp_columns(raw, w, ht):
    """Return the alpha-repaired RGB column samples used by a GX ramp lookup."""
    cols = []
    for x in range(w):
        acc = [0.0, 0.0, 0.0]; cnt = 0
        for y in range(ht):
            o = (y * w + x) * 4
            if raw[o + 3] >= 128:
                acc[0] += raw[o]; acc[1] += raw[o + 1]; acc[2] += raw[o + 2]; cnt += 1
        cols.append(tuple(v / cnt / 255.0 for v in acc) if cnt else None)
    valid = [i for i, c in enumerate(cols) if c is not None]
    if not valid: return [(1.0, 1.0, 1.0)] * w
    for i in range(w):
        if cols[i] is not None: continue
        lo = [v for v in valid if v < i]; hi = [v for v in valid if v > i]
        lo = lo[-1] if lo else valid[0]; hi = hi[0] if hi else valid[-1]
        if lo == hi: cols[i] = cols[lo]
        else:
            t = (i - lo) / float(hi - lo)
            cols[i] = tuple(cols[lo][k] * (1.0 - t) + cols[hi][k] * t for k in range(3))
    return cols


def ramp_stops_from_texture(raw, w, ht, stops=32):
    """Sample a ramp texture into ColorRamp stops (gamma space).

    CMPR carries 1-bit punch-through alpha and the shipped 128x4 ramps decode with 15-27
    transparent texels PER ROW (bh_skin 24/15/22/19). Read literally those become hard black
    contour bands. Average the valid texels down each column and interpolate across dead ones."""
    cols = _ramp_columns(raw, w, ht)
    return [(i / (stops - 1.0), cols[min(w - 1, int(round(i / (stops - 1.0) * (w - 1))))])
            for i in range(stops)]


def fp_ramp(node):
    """Fingerprint of a ColorRamp, so export can tell whether you actually edited it."""
    if node is None:
        return ""
    e = node.color_ramp.elements
    return "|".join("%.5f:%.4f,%.4f,%.4f" % (x.position, x.color[0], x.color[1], x.color[2])
                    for x in e)


def fp_image(img):
    """CRC of an image's quantised RGBA. Same rounding the exporter uses, so an untouched
    image fingerprints identically on the way out as it did on the way in."""
    if img is None:
        return ""
    px = list(img.pixels)
    b = bytes(max(0, min(255, int(round(v * 255)))) for v in px)
    return "%d:%dx%d:%08x" % (len(px), img.size[0], img.size[1], _zlib.crc32(b))


def set_slot_hash(mat, i, h):
    """Store a texture-slot name hash on a material.

    Blender ID-properties store Python ints as a SIGNED 32-bit C int. PO name hashes are
    unsigned 32-bit and most of them have the top bit set (e.g. 0xdf0f8a43), so assigning one
    directly raises 'Python int too large to convert to C int'. Store the hex text instead --
    lossless, readable in the N-panel, and unambiguous."""
    mat["po_slot_hash%d" % i] = "%08x" % (h & 0xFFFFFFFF)


def get_slot_hash(mat, i):
    """Read back a slot hash as an unsigned int, or None. Accepts the old int form too."""
    v = mat.get("po_slot_hash%d" % i)
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return int(v, 16)
        except ValueError:
            return None
    return int(v) & 0xFFFFFFFF


def expected_ramp_fp(archive, tex_hash):
    """The fp_ramp() value an UNTOUCHED ramp slot would have had at import, computed from the
    archive alone -- no Blender node needed.

    Why this exists: dirty_slots() decides what to re-bake by comparing fp_ramp(node) against
    a stored fingerprint. If the fingerprint is MISSING the slot counts as dirty, so a ramp you
    never touched gets re-derived -- and a 24-stop resample of a 128-texel ramp is lossy, so
    the rewrite can come back near-flat. A flat/black lighting ramp collapses the toon shading
    into quantisation bands, i.e. stripes on a surface that should be smooth.

    _po_ramp_node() builds elements directly from these stops, so recomputing the stops from
    the archive texture reproduces the fingerprint exactly.
    """
    import nlg_texture
    textbl = {e.hash: e for e in nlg_texture.list_all_textures(archive)}
    if tex_hash not in textbl:
        c = GLOBAL_CONST.get(tex_hash, (1.0, 1.0, 1.0))
        stops = [(0.0, c), (1.0, c)]
    else:
        entry = textbl[tex_hash]
        raw = nlg_texture.decode_texture(archive, entry)
        stops = ramp_stops_from_texture(raw, entry.width, entry.height)
    return "|".join("%.5f:%.4f,%.4f,%.4f" % (pos, c[0], c[1], c[2]) for pos, c in stops)


def stamp_fingerprints(mat):
    """Record what the material looked like when it was built. Export compares against this."""
    nt = mat.node_tree
    g = nt.nodes.get
    mat["po_fp_ramp"] = fp_ramp(g("PO_Ramp"))
    mat["po_fp_rim"] = fp_ramp(g("PO_RimRamp"))
    mat["po_fp_fres"] = fp_ramp(g("PO_Fresnel"))
    d = g("PO_Detail"); dmg = g("PO_Damage"); sm = g("PO_SpecMask"); h = g("PO_Hdr")
    mat["po_fp_detail"] = fp_image(d.image if d is not None else None)
    mat["po_fp_damage"] = fp_image(dmg.image if dmg is not None else None)
    mat["po_fp_specmask"] = fp_image(sm.image if sm is not None else None)
    mat["po_fp_specramp"] = fp_ramp(g("PO_SpecRamp"))
    mat["po_fp_hdr"] = fp_image(h.image if h is not None else None)
    t = g("PO_Tint")
    tint = ((t.inputs[0].default_value, t.inputs[1].default_value, t.inputs[2].default_value)
            if t is not None else (1.0, 1.0, 1.0))
    mat["po_fp_params"] = "%.6f,%.6f,%.6f|%.4f|%.6f" % (
        tint + (float(mat.get("po_spec_power", 32.0)), float(mat.get("po_alpha", 1.0))))


# which fingerprint guards which texture slot
FP_SLOTS = (("po_fp_detail", 0, "PO_Detail", True),
            ("po_fp_damage", 1, "PO_Damage", True),
            ("po_fp_specmask", 2, "PO_SpecMask", True),
            ("po_fp_ramp",   3, "PO_Ramp",    False),
            ("po_fp_rim",    4, "PO_RimRamp", False),
            ("po_fp_hdr",    5, "PO_Hdr",     True),
            ("po_fp_fres",   6, "PO_Fresnel", False),
            ("po_fp_specramp", 7, "PO_SpecRamp", False))


# Editable ramp -> the switch that previews it instead of the exact source texture.
RAMP_SOURCES = (("PO_Ramp", "PO_RampSource", "po_fp_ramp"),
                ("PO_RimRamp", "PO_RimRampSource", "po_fp_rim"),
                ("PO_Fresnel", "PO_FresnelSource", "po_fp_fres"),
                ("PO_SpecRamp", "PO_SpecRampSource", "po_fp_specramp"))


def show_edited_ramps(mat):
    """Point each edited ramp's preview at the edit. Once per edit: flipping the switch back to
    compare against the source sticks until the ramp changes again."""
    nt = getattr(mat, "node_tree", None)
    if nt is None:
        return
    for ramp_name, switch_name, key in RAMP_SOURCES:
        ramp, switch = nt.nodes.get(ramp_name), nt.nodes.get(switch_name)
        stored = mat.get(key)
        if ramp is None or switch is None or stored is None:
            continue
        now = fp_ramp(ramp)
        if now != stored and mat.get(key + "_shown") != now:
            mat[key + "_shown"] = now
            switch.inputs[0].default_value = 1.0


def dirty_slots(mat, resolved=None, explain=None):
    """Slot indices whose ramp/image actually changed since the material was built.

    Export re-bakes ONLY these. Re-encoding an untouched texture still costs quality -- our
    CMPR compressor is not Nintendo's, and a 24-stop ColorRamp is a lossy resample of a
    128-texel ramp, which can come back near-flat and band the shading into stripes -- so
    'rebuild the whole material' is not good enough.

    A MISSING fingerprint means "we do not know", NOT "it changed". Treating unknown as dirty
    is what silently re-derived ramps nobody had touched. If the fingerprint is absent, leave
    the slot alone; po_recover_slots() reconstructs the import-time fingerprints from the
    archive, so a genuinely edited slot still has one to differ from.

    `resolved` is an optional {slot: node} map (see io_export_punchout.resolve_po_nodes) so the
    caller's topology-based lookup is used instead of fragile node names.
    `explain` is an optional list that receives a human-readable reason per dirty slot.
    """
    out = set()
    nt = mat.node_tree
    if nt is None:
        return out
    for key, slot, node, is_img in FP_SLOTS:
        nd = (resolved or {}).get(slot) or nt.nodes.get(node)
        if nd is None:
            continue
        stored = mat.get(key, None)
        if stored is None:
            if explain is not None:
                explain.append("slot %d: no fingerprint, assuming UNCHANGED" % slot)
            continue
        now = fp_image(nd.image) if is_img else fp_ramp(nd)
        if stored != now:
            out.add(slot)
            if explain is not None:
                explain.append("slot %d changed: %.40s... -> %.40s..." % (slot, stored, now))
    return out


def material_is_pristine(mat):
    """True when nothing that reaches the archive has changed since the material was built."""
    if not mat.get("po_record"):
        return False
    if mat.get("po_fp_params", None) is not None:
        nt0 = mat.node_tree
        t = nt0.nodes.get("PO_Tint") if nt0 else None
        tint = ((t.inputs[0].default_value, t.inputs[1].default_value, t.inputs[2].default_value)
                if t is not None else (1.0, 1.0, 1.0))
        now = "%.6f,%.6f,%.6f|%.4f|%.6f" % (
            tint + (float(mat.get("po_spec_power", 32.0)), float(mat.get("po_alpha", 1.0))))
        if mat["po_fp_params"] != now:
            return False
    nt = mat.node_tree
    if nt is None:
        return False
    g = nt.nodes.get
    checks = (("po_fp_ramp", fp_ramp(g("PO_Ramp"))),
              ("po_fp_rim", fp_ramp(g("PO_RimRamp"))),
              ("po_fp_fres", fp_ramp(g("PO_Fresnel"))),
              ("po_fp_specramp", fp_ramp(g("PO_SpecRamp"))))
    for k, now in checks:
        if mat.get(k, None) != now:
            return False
    d = g("PO_Detail"); dmg = g("PO_Damage"); sm = g("PO_SpecMask"); h = g("PO_Hdr")
    if mat.get("po_fp_detail", "") != fp_image(d.image if d is not None else None):
        return False
    if mat.get("po_fp_damage", "") != fp_image(dmg.image if dmg is not None else None):
        return False
    if mat.get("po_fp_specmask", "") != fp_image(sm.image if sm is not None else None):
        return False
    if mat.get("po_fp_hdr", "") != fp_image(h.image if h is not None else None):
        return False
    return True


def build_materials_from_archive(archive, hashnames, image_factory, global_archive=None):
    """Build every material in a character archive. Returns (materials, mesh_to_material).

    `image_factory(hash, name, rgba, w, h)` makes the Blender image (injected so this function
    stays scene-free and testable). Each material is stamped with the ORIGINAL 204B record and
    per-slot hashes, which is what lets the exporter write back byte-identical data for
    anything you did not touch, whatever each field means.

    `global_archive` (global.dict) supplies preview images for shared textures the character
    archive only references (global/specramp). Editable ramps and fingerprints still come from
    the character archive alone, so export decisions do not depend on it.
    """
    import nlg_texture
    matcs = archive.find_chunks(type_id=0xB016); meshcs = archive.find_chunks(type_id=0xB004)
    matdata = archive.get_chunk_bytes(matcs[0]) if matcs else b""
    meshdata = archive.get_chunk_bytes(meshcs[0]) if meshcs else b""
    textbl = {e.hash: e for e in nlg_texture.list_all_textures(archive, hashnames)}

    px_cache, img_cache, ramp_img_cache, mat_cache = {}, {}, {}, {}

    def _raw(h):
        if h not in textbl:
            return None
        if h not in px_cache:
            entry = textbl[h]
            px_cache[h] = (nlg_texture.decode_texture(archive, entry), entry.width, entry.height)
        return px_cache[h]

    def _stops(h):
        p = _raw(h)
        if p is None:
            c = GLOBAL_CONST.get(h, (1.0, 1.0, 1.0))
            return [(0.0, c), (1.0, c)]
        return ramp_stops_from_texture(*p)

    def _img(h):
        if h not in img_cache:
            raw, w, ht = _raw(h)
            nm = hashnames.get(h, "%08x" % h).split("/")[-1]
            img_cache[h] = image_factory(h, nm, raw, w, ht)
        return img_cache[h]

    gtbl = ({e.hash: e for e in nlg_texture.list_all_textures(global_archive, hashnames)}
            if global_archive is not None else {})

    def _preview_img(h):
        """Full texture for a 2D TEV lookup: the character's own, else global.dict's."""
        if h in textbl:
            return _img(h)
        if h not in gtbl:
            return None
        key = ("global", h)
        if key not in img_cache:
            e = gtbl[h]
            nm = hashnames.get(h, "%08x" % h).split("/")[-1]
            img_cache[key] = image_factory(h, nm, nlg_texture.decode_texture(global_archive, e), e.width, e.height)
        return img_cache[key]

    def _ramp_img(h):
        """One-row, alpha-repaired LUT retaining every authored source column."""
        if h not in textbl: return None
        if h not in ramp_img_cache:
            raw, w, ht = _raw(h); cols = _ramp_columns(raw, w, ht)
            clean = bytes(v for c in cols for v in (
                max(0, min(255, int(round(c[0] * 255)))),
                max(0, min(255, int(round(c[1] * 255)))),
                max(0, min(255, int(round(c[2] * 255)))), 255))
            nm = hashnames.get(h, "%08x" % h).split("/")[-1] + " · exact LUT"
            ramp_img_cache[h] = image_factory(h, nm, clean, w, 1)
        return ramp_img_cache[h]

    def _slot(mo, s):
        return _struct.unpack_from(">I", matdata, mo + s * 8)[0] if mo + s * 8 + 4 <= len(matdata) else 0

    mats, mesh_to_mat = [], {}
    for mi in range(len(meshdata) // 52):
        o = mi * 52
        matoff = _struct.unpack_from(">I", meshdata, o + 36)[0]
        matname = hashnames.get(_struct.unpack_from(">I", meshdata, o + 20)[0], "")
        if matoff in mat_cache:
            mesh_to_mat[mi] = mat_cache[matoff]
            continue
        hs = [_slot(matoff, i) for i in range(8)]
        detail_h, ramp_h, rim_h, hdr_h, fres_h = hs[0], hs[3], hs[4], hs[5], hs[6]
        tint = tuple(_struct.unpack_from(">fff", matdata, matoff + 0x9C))
        alpha = _struct.unpack_from(">f", matdata, matoff + 0xA8)[0]
        specp = _struct.unpack_from(">f", matdata, matoff + 0x84)[0]

        low = (matname or "").lower()
        is_optional_damage = any(k in low for k in OPTIONAL_DAMAGE_NAME_HINTS)
        is_hurt = any(k in low for k in DAMAGE_NAME_HINTS) or is_optional_damage
        nm = hashnames.get(ramp_h, "%08x" % ramp_h).split("/")[-1]
        if nm in ("white", "black", "global") or ramp_h in GLOBAL_CONST:
            nm = hashnames.get(detail_h, nm).split("/")[-1]
        # Ramp-derived names made every skin-based hurt material appear as lm_skin.004/.005,
        # which is miserable to navigate. Prefer the archive material resource name and make
        # hurt entries unmistakable while retaining po_mesh_material for exact export naming.
        resource_name = (matname or "").split("/")[-1]
        if resource_name:
            nm = ("hurt_" + resource_name) if is_hurt else resource_name
        m = bpy.data.materials.new(nm or ("mat_%04x" % matoff))
        po_build_shader(m, dict(
            ramp=_stops(ramp_h),
            ramp_image=_ramp_img(ramp_h),
            detail=(_img(detail_h) if detail_h in textbl else None),
            damage=(_img(hs[1]) if is_hurt and hs[1] in textbl else None),
            specmask=(_img(hs[2]) if hs[2] in textbl else None),
            rim=(_stops(rim_h) if rim_h in textbl else None),
            rim_image=(_preview_img(rim_h) if rim_h in textbl else None),
            hdr=(_img(hdr_h) if hdr_h in textbl else None),
            fresnel=(_stops(fres_h) if fres_h in textbl else None),
            fresnel_image=_preview_img(fres_h),
            specramp=_stops(hs[7]),
            specramp_image=_preview_img(hs[7]),
            specramp_missing=hs[7] not in textbl and hs[7] not in gtbl and hs[7] not in GLOBAL_CONST,
            record=matdata[matoff:matoff + 204],
            slot_const={i: GLOBAL_CONST[h] for i, h in enumerate(hs) if h not in textbl and h in GLOBAL_CONST},
            tint=tint, spec_power=specp, alpha=alpha,
            # Always tag hurt materials. Older code only built PO_DamageVis when the module
            # default was False, permanently disabling toggling if that default was changed.
            hurt=is_hurt,
            optional_damage=is_optional_damage,
            tex_names={i: hashnames.get(hs[i], "#%08x" % hs[i]) for i in range(8)},
        ))
        m["po_mesh_material"] = matname
        m["po_matoff"] = matoff
        # Stamp the slot NOW, at import, while it is known for free. The old route was
        # a face-order recovery helper, which had to run before you edited geometry -- a trap
        # nobody should have to know about. matoff is
        # 1:1 with the mesh slot on every shipped character, so this is exact.
        m["po_slot"] = mi
        m["po_record"] = matdata[matoff:matoff + 204].hex()
        for i in range(8):
            set_slot_hash(m, i, hs[i])
        stamp_fingerprints(m)
        mat_cache[matoff] = m
        mats.append(m)
        mesh_to_mat[mi] = m
    return mats, mesh_to_mat
