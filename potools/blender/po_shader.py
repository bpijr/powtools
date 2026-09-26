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
# vertex index of the archive vertex it came from. Morph records and aux attributes are keyed
# by that identity on export; position alone cannot separate coincident vertices.
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


def _po_tev_coordinates(nt, geo):
    """Build the shared TEV coordinates: N dot L, view vector and N dot V."""
    key = nt.nodes.new("ShaderNodeCombineXYZ")
    key.location = (-1220, 80); key.name = "PO_LightVector"; key.label = "TEV light vector (world)"
    for i, value in enumerate(Vector(KEY_LIGHT_DIR).normalized()):
        key.inputs[i].default_value = value

    ndl = _po_vec(nt, "DOT_PRODUCT", (-1000, 80))
    ndl.name = "PO_NdotL"; ndl.label = "True Normal dot Light"
    # GX lights the interpolated authored vertex normal. Blender's True Normal is the
    # geometric face normal and visibly facets DK; Normal carries the imported split normals.
    normal = geo.outputs["Normal"]
    nt.links.new(normal, ndl.inputs[0]); nt.links.new(key.outputs["Vector"], ndl.inputs[1])
    mapped = nt.nodes.new("ShaderNodeMapRange")
    mapped.location = (-820, 80); mapped.name = "PO_NdotL_Map"; mapped.label = "-1..1 to LUT 0..1"
    mapped.inputs["From Min"].default_value = -1.0
    mapped.inputs["From Max"].default_value = 1.0
    mapped.inputs["To Min"].default_value = 0.0
    mapped.inputs["To Max"].default_value = 1.0
    try: mapped.clamp = True
    except AttributeError: pass
    nt.links.new(ndl.outputs["Value"], mapped.inputs["Value"])

    view = _po_vec(nt, "SCALE", (-1000, -360))
    view.name = "PO_ViewVector"; view.label = "surface to camera"
    view.inputs["Scale"].default_value = -1.0
    nt.links.new(geo.outputs["Incoming"], view.inputs[0])
    ndv = _po_vec(nt, "DOT_PRODUCT", (-800, -360)); ndv.name = "PO_NdotV"
    nt.links.new(geo.outputs["Normal"], ndv.inputs[0]); nt.links.new(view.outputs["Vector"], ndv.inputs[1])
    ndvc = nt.nodes.new("ShaderNodeMath"); ndvc.operation = "MAXIMUM"
    ndvc.location = (-620, -360); ndvc.name = "PO_NdotV_Clamp"; ndvc.inputs[1].default_value = 0.0
    nt.links.new(ndv.outputs["Value"], ndvc.inputs[0])
    return key.outputs["Vector"], mapped.outputs["Result"], view.outputs["Vector"], ndvc.outputs[0]


def po_build_shader(m, cfg):
    """Build the hippodiffuseskin graph on material `m` from `cfg`. Single source of truth for
    both the importer and po_new_material, so authored materials and imported ones behave
    identically. cfg keys: ramp (stops), detail (Image|None), rim (stops|None),
    damage (Image|None), hdr (Image|None), fresnel (stops|None), tint, spec_power, alpha,
    hurt (bool), tex_names."""
    m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (1420, 0)
    geo = nt.nodes.new("ShaderNodeNewGeometry"); geo.location = (-1420, -260)
    light_vec, ndl_coord, view_vec, ndv_coord = _po_tev_coordinates(nt, geo)

    ramp = _po_ramp_node(nt, cfg["ramp"], (-600, 180), "editable diffuse LUT (slot3)", "PO_Ramp")
    nt.links.new(ndl_coord, ramp.inputs["Fac"])
    rp = nt.nodes.new("ShaderNodeValue"); rp.location = (-820, 300)
    rp.label = "Legacy export ramp sample"; rp.name = "PO_RampPos"
    rp.outputs[0].default_value = PO_RAMP_POS
    base = ramp.outputs["Color"]
    if cfg.get("ramp_image") is not None:
        coord = nt.nodes.new("ShaderNodeCombineXYZ"); coord.location = (-800, 460)
        coord.name = "PO_RampUV"; coord.inputs["Y"].default_value = 0.5
        nt.links.new(ndl_coord, coord.inputs["X"])
        exact = _po_img_node(nt, cfg["ramp_image"], (-600, 460), "PO_RampTexture")
        exact.label = "exact repaired source LUT (slot3)"; exact.extension = "EXTEND"; exact.interpolation = "Linear"
        nt.links.new(coord.outputs[0], exact.inputs["Vector"])
        choose = nt.nodes.new("ShaderNodeMixRGB"); choose.location = (-380, 400)
        choose.name = "PO_RampSource"; choose.label = "0 exact source / 1 editable ramp"
        choose.inputs[0].default_value = 0.0
        nt.links.new(exact.outputs["Color"], choose.inputs[1]); nt.links.new(ramp.outputs["Color"], choose.inputs[2])
        base = choose.outputs[0]

    if cfg.get("detail") is not None:
        duv = nt.nodes.new("ShaderNodeUVMap"); duv.uv_map = "UV"; duv.location = (-1220, 520)
        dtex = _po_img_node(nt, cfg["detail"], (-1000, 520), "PO_Detail")
        nt.links.new(duv.outputs["UV"], dtex.inputs["Vector"])
        mul = _po_vec(nt, "MULTIPLY", (-380, 260))
        nt.links.new(base, mul.inputs[0])
        nt.links.new(dtex.outputs["Color"], mul.inputs[1])
        base = mul.outputs["Vector"]

    # Slot 1 is the actual bruise/black-eye artwork. It is an opaque, mostly-white multiply
    # overlay (white = no change), not an alpha mask. The old preview ignored this slot, so a
    # visible hurt mesh showed only its underlying skin/detail and looked like broken geometry.
    damage_img = cfg.get("damage")
    if damage_img is not None and not isinstance(damage_img, bool):
        damage = _po_img_node(nt, damage_img, (-1000, 650), "PO_Damage")
        damage.label = "hurt multiply overlay (slot1)"
        damage_uv = nt.nodes.new("ShaderNodeUVMap")
        damage_uv.location = (-1200, 650)
        damage_uv.name = "PO_DamageUV"; damage_uv.label = "Damage UV (texcoord 1)"
        damage_uv.uv_map = "UV_Damage"
        nt.links.new(damage_uv.outputs["UV"], damage.inputs["Vector"])
        damage_mix = nt.nodes.new("ShaderNodeMixRGB")
        damage_mix.location = (-700, 580)
        damage_mix.name = "PO_DamageMix"; damage_mix.label = "Normal / Hurt"
        damage_mix.blend_type = "MIX"
        damage_mix.inputs[0].default_value = 1.0 if SHOW_DAMAGE else 0.0
        damage_mix.inputs[1].default_value = (1.0, 1.0, 1.0, 1.0)
        nt.links.new(damage.outputs["Color"], damage_mix.inputs[2])
        dmul = _po_vec(nt, "MULTIPLY", (-440, 430))
        nt.links.new(base, dmul.inputs[0])
        nt.links.new(damage_mix.outputs[0], dmul.inputs[1])
        base = dmul.outputs["Vector"]

    specp = float(cfg.get("spec_power", 32.0))
    tint = cfg.get("tint", (1.0, 1.0, 1.0))
    tv = nt.nodes.new("ShaderNodeCombineXYZ"); tv.location = (-420, -520)
    tv.label = "tint (+0x9C)"; tv.name = "PO_Tint"
    for i, v in enumerate(tint):
        tv.inputs[i].default_value = v

    combined = base

    # TEV spec stage: slot 2 mask x slot 7 response, driven by a Blinn half-vector and the
    # material record's specular exponent. Missing global specramp resolves to white.
    if cfg.get("specmask") is not None:
        half_add = _po_vec(nt, "ADD", (-600, -40)); half_add.name = "PO_HalfVectorAdd"
        nt.links.new(light_vec, half_add.inputs[0]); nt.links.new(view_vec, half_add.inputs[1])
        half_norm = _po_vec(nt, "NORMALIZE", (-420, -40)); half_norm.name = "PO_HalfVector"
        nt.links.new(half_add.outputs["Vector"], half_norm.inputs[0])
        ndh = _po_vec(nt, "DOT_PRODUCT", (-240, -40)); ndh.name = "PO_NdotH"
        nt.links.new(geo.outputs["Normal"], ndh.inputs[0]); nt.links.new(half_norm.outputs["Vector"], ndh.inputs[1])
        ndhc = nt.nodes.new("ShaderNodeMath"); ndhc.operation = "MAXIMUM"; ndhc.location = (-60, -40)
        ndhc.inputs[1].default_value = 0.0; nt.links.new(ndh.outputs["Value"], ndhc.inputs[0])
        power = nt.nodes.new("ShaderNodeMath"); power.operation = "POWER"; power.location = (120, -40)
        power.name = "PO_SpecPower"; power.inputs[1].default_value = max(1.0, specp)
        nt.links.new(ndhc.outputs[0], power.inputs[0])
        sr = _po_ramp_node(nt, cfg.get("specramp") or [(0.0, (0, 0, 0)), (1.0, (1, 1, 1))],
                           (300, -40), "specular response (slot7)", "PO_SpecRamp")
        nt.links.new(power.outputs[0], sr.inputs["Fac"])
        spec_response = sr.outputs["Color"]
        if cfg.get("specramp_image") is not None:
            scoord = nt.nodes.new("ShaderNodeCombineXYZ"); scoord.location = (100, -320)
            scoord.name = "PO_SpecRampUV"; scoord.inputs["Y"].default_value = 0.5
            nt.links.new(power.outputs[0], scoord.inputs["X"])
            sexact = _po_img_node(nt, cfg["specramp_image"], (300, -320), "PO_SpecRampTexture")
            sexact.extension = "EXTEND"; sexact.interpolation = "Linear"
            nt.links.new(scoord.outputs[0], sexact.inputs["Vector"])
            schoose = nt.nodes.new("ShaderNodeMixRGB"); schoose.location = (480, -300)
            schoose.name = "PO_SpecRampSource"; schoose.label = "0 exact source / 1 editable ramp"
            schoose.inputs[0].default_value = 0.0
            nt.links.new(sexact.outputs["Color"], schoose.inputs[1]); nt.links.new(sr.outputs["Color"], schoose.inputs[2])
            spec_response = schoose.outputs[0]
        suv = nt.nodes.new("ShaderNodeUVMap"); suv.uv_map = "UV"; suv.location = (-80, -180)
        sm = _po_img_node(nt, cfg["specmask"], (120, -220), "PO_SpecMask")
        nt.links.new(suv.outputs["UV"], sm.inputs["Vector"])
        smul = _po_vec(nt, "MULTIPLY", (500, -80))
        nt.links.new(spec_response, smul.inputs[0]); nt.links.new(sm.outputs["Color"], smul.inputs[1])
        stint = _po_vec(nt, "MULTIPLY", (680, -80))
        nt.links.new(smul.outputs[0], stint.inputs[0]); nt.links.new(tv.outputs["Vector"], stint.inputs[1])
        sscale = _po_vec(nt, "SCALE", (840, -80)); sscale.inputs["Scale"].default_value = PO_SPECULAR_SCALE
        nt.links.new(stint.outputs[0], sscale.inputs[0])
        sadd = _po_vec(nt, "ADD", (1000, 180))
        nt.links.new(combined, sadd.inputs[0]); nt.links.new(sscale.outputs[0], sadd.inputs[1])
        combined = sadd.outputs[0]

    if cfg.get("rim"):
        rr = _po_ramp_node(nt, cfg["rim"], (-220, -420), "N dot V rim LUT (slot4)", "PO_RimRamp")
        nt.links.new(ndv_coord, rr.inputs["Fac"])
        rm = _po_vec(nt, "MULTIPLY", (0, -420))
        nt.links.new(rr.outputs["Color"], rm.inputs[0])
        nt.links.new(tv.outputs["Vector"], rm.inputs[1])
        rscale = _po_vec(nt, "SCALE", (180, -420)); rscale.inputs["Scale"].default_value = PO_GLOW
        nt.links.new(rm.outputs[0], rscale.inputs[0])
        radd = _po_vec(nt, "ADD", (1000, 80))
        nt.links.new(combined, radd.inputs[0]); nt.links.new(rscale.outputs[0], radd.inputs[1])
        combined = radd.outputs[0]

    if cfg.get("hdr") is not None:
        vtf = nt.nodes.new("ShaderNodeVectorTransform"); vtf.location = (-1180, -640)
        vtf.vector_type = "VECTOR"; vtf.convert_from = "WORLD"; vtf.convert_to = "CAMERA"
        nt.links.new(geo.outputs["Normal"], vtf.inputs[0])
        sep = nt.nodes.new("ShaderNodeSeparateXYZ"); sep.location = (-1000, -640)
        nt.links.new(vtf.outputs["Vector"], sep.inputs[0])
        mrs = []
        for k, yy in ((0, -560), (1, -760)):
            mr = nt.nodes.new("ShaderNodeMapRange"); mr.location = (-820, yy)
            mr.inputs["From Min"].default_value = -1.0
            mr.inputs["From Max"].default_value = 1.0
            nt.links.new(sep.outputs[k], mr.inputs["Value"])
            mrs.append(mr)
        cxy = nt.nodes.new("ShaderNodeCombineXYZ"); cxy.location = (-640, -640)
        nt.links.new(mrs[0].outputs["Result"], cxy.inputs[0])
        nt.links.new(mrs[1].outputs["Result"], cxy.inputs[1])
        htex = _po_img_node(nt, cfg["hdr"], (-460, -640), "PO_Hdr"); htex.extension = "EXTEND"
        nt.links.new(cxy.outputs["Vector"], htex.inputs["Vector"])
        fr = _po_ramp_node(nt, cfg.get("fresnel") or FRESNEL_DEFAULT,
                           (-460, -880), "fresnel ramp (slot6)", "PO_Fresnel")
        nt.links.new(ndv_coord, fr.inputs["Fac"])
        bl = _po_vec(nt, "MULTIPLY", (-120, -640))
        nt.links.new(htex.outputs["Color"], bl.inputs[0])
        nt.links.new(fr.outputs["Color"], bl.inputs[1])
        hscale = _po_vec(nt, "SCALE", (80, -640)); hscale.inputs["Scale"].default_value = PO_HDR_GLOW
        hscale.name = "PO_HdrGlow"
        nt.links.new(bl.outputs[0], hscale.inputs[0])
        hadd = _po_vec(nt, "ADD", (1000, -20))
        nt.links.new(combined, hadd.inputs[0]); nt.links.new(hscale.outputs[0], hadd.inputs[1])
        combined = hadd.outputs[0]

    gain = _po_vec(nt, "SCALE", (1040, 300)); gain.name = "PO_AlbedoGain"; gain.label = "PO TEV gain"
    gain.inputs["Scale"].default_value = PO_ALBEDO_GAIN; nt.links.new(combined, gain.inputs[0])
    lift = _po_vec(nt, "ADD", (1040, 440)); lift.name = "PO_AlbedoLift"; lift.label = "PO TEV lift"
    lift.inputs[1].default_value = (PO_ALBEDO_LIFT,) * 3; nt.links.new(gain.outputs[0], lift.inputs[0])
    clmp = _po_vec(nt, "MINIMUM", (1200, 300)); clmp.inputs[1].default_value = (1.0, 1.0, 1.0)
    nt.links.new(lift.outputs[0], clmp.inputs[0])
    gamma = nt.nodes.new("ShaderNodeGamma"); gamma.location = (1200, 160); gamma.name = "PO_GammaToLinear"
    # Source textures are tagged Non-Color because TEV combines their encoded values.  The
    # final 2.2 node linearizes that result before Blender's Standard sRGB display transform.
    gamma.inputs["Gamma"].default_value = 2.2
    nt.links.new(clmp.outputs[0], gamma.inputs["Color"])
    emission = nt.nodes.new("ShaderNodeEmission"); emission.location = (1260, 20); emission.name = "PO_TEV_Output"
    emission.inputs["Strength"].default_value = 1.0; nt.links.new(gamma.outputs["Color"], emission.inputs["Color"])
    surface = emission.outputs["Emission"]

    # These records are replacement pieces of the character surface, not optional overlay
    # polygons. They remain opaque in both states; only their slot-1 multiply changes.
    # `damage is True` keeps compatibility with callers using the old boolean API.
    if cfg.get("hurt") or cfg.get("damage") is True:
        m["po_damage_mesh"] = True

    if cfg.get("optional_damage"):
        transparent = nt.nodes.new("ShaderNodeBsdfTransparent"); transparent.location = (1020, 520)
        visible = nt.nodes.new("ShaderNodeMixShader"); visible.location = (1180, 500)
        visible.name = "PO_DamageVis"; visible.label = "Normal hidden / Hurt visible"
        visible.inputs[0].default_value = 1.0 if SHOW_DAMAGE else 0.0
        nt.links.new(transparent.outputs[0], visible.inputs[1]); nt.links.new(surface, visible.inputs[2])
        surface = visible.outputs[0]
        m["po_damage_mesh"] = True
        m["po_optional_damage"] = True
        try: m.surface_render_method = "DITHERED"
        except AttributeError: pass

    alpha = float(cfg.get("alpha", 1.0))
    if alpha < 0.999:
        tr2 = nt.nodes.new("ShaderNodeBsdfTransparent"); tr2.location = (1020, 420)
        mx2 = nt.nodes.new("ShaderNodeMixShader"); mx2.location = (1100, 380)
        mx2.name = "PO_Alpha"
        nt.links.new(tr2.outputs["BSDF"], mx2.inputs[1])
        nt.links.new(surface, mx2.inputs[2])
        mx2.inputs["Fac"].default_value = alpha
        surface = mx2.outputs["Shader"]
        for attr, val in (("blend_method", "HASHED"), ("surface_render_method", "DITHERED")):
            try:
                setattr(m, attr, val)
            except Exception:
                pass

    nt.links.new(surface, out.inputs["Surface"])
    try:
        m.use_backface_culling = True
    except Exception:
        pass
    m["po_material"] = True
    m["po_tint"] = list(tint)
    m["po_spec_power"] = specp
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


def build_materials_from_archive(archive, hashnames, image_factory):
    """Build every material in a character archive. Returns (materials, mesh_to_material).

    `image_factory(hash, name, rgba, w, h)` makes the Blender image (injected so this function
    stays scene-free and testable). Each material is stamped with the ORIGINAL 204B record and
    per-slot hashes, which is what lets the exporter write back byte-identical data for
    anything you did not touch -- including the ~15 u32s in the record we still cannot name.
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
            hdr=(_img(hdr_h) if hdr_h in textbl else None),
            fresnel=(_stops(fres_h) if fres_h in textbl else None),
            specramp=_stops(hs[7]),
            specramp_image=_ramp_img(hs[7]),
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
