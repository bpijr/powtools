"""Arena effect sprites (lamp glows and stars) from the game's own placement and effect data.

The World Circuit arena places ``env_goldlens`` at 48 authored positions (six ceiling rigs of eight
lamps); Major Circuit places ``env_lensflare``.  Each names a group in ``effects/effects.dict`` made
of sprite emitters.  What is decoded (PAL main.dol, fn_8016BEF8 spawn / fn_8016C7B4 particle setup /
fn_8016C3A8 draw / fn_8016D0A0 emitter update):

* particle lifetime = header float +16 with +20 as its total range (centred: base + range * (u - 0.5));
* spawn rate (particles per second) = parameter 0; the spawn accumulator adds ``dt * rate``;
* size = parameter 1 * parameter 2 (world units, full quad width; the draw halves it);
* start rotation = header +40 base, +44 range (degrees; 360 for the star), then drift at parameter 3;
* colour and alpha = the emitter's 25 colour samples read at ``24 * age / life``, additive blend.

Not decoded: what the drift unit is, and the phase of an emitter when the fight scene starts, so every
lamp gets a deterministic pseudo-random phase (a steady state), and the drift is ignored.  Emitters
whose parameters are curves (the crowd camera flashes) are triggered by gameplay and are not built.
"""
import json
import math
import random
import struct
from pathlib import Path

import bpy

import nlg_effect
import nlg_hash
import nlg_pack
import nlg_texture

SUPPORTED = ("env_goldlens", "env_lensflare")
TIME_NODE = "PO_FX_Time"


def _param(em, index):
    """(base, range) of a constant parameter, or None when the parameter is a curve."""
    p = em.parameters[index]
    if p.curved:
        return None
    return struct.unpack_from(">ff", p.raw, 4)


def read_emitters(effects_dict, names, effect_name):
    """Sprite emitters of one effect group as plain dicts; [] when it cannot be built exactly."""
    arc = nlg_pack.Archive(str(effects_dict))
    fx = nlg_effect.Effects(arc, nlg_effect.names_for(arc))
    group = next((g for g in fx.groups if names.get(g.name_hash) == effect_name and g.emitters), None)
    if group is None:
        return arc, []
    out = []
    for em in group.emitters:
        if em.layout != "sprite" or em.raw[52] != 0:                       # point spawn volume only
            return arc, []
        rate, size1, size2 = _param(em, 0), _param(em, 1), _param(em, 2)
        if rate is None or size1 is None or size2 is None:
            return arc, []
        life, life_range = struct.unpack_from(">ff", em.raw, 16)
        out.append({
            "name": names.get(em.name_hash) or "emitter %d" % em.index,
            "texture": names.get(em.texture_hash) or "%08X" % em.texture_hash,
            "texture_hash": em.texture_hash,
            "rate": rate[0], "life": life, "life_range": life_range,
            "size1": size1, "size2": size2,
            "spin_range": struct.unpack_from(">f", em.raw, 44)[0],
            "colours": [tuple(c) for c in em.colour_samples][:25],
        })
    return arc, out


def _image(arc, names, texture_hash, label):
    name = "GX " + label
    img = bpy.data.images.get(name)
    if img is not None:
        return img
    entry = next(e for e in nlg_texture.list_all_textures(arc, names) if e.hash == texture_hash)
    rgba = nlg_texture.decode_texture(arc, entry)
    w, h = entry.width, entry.height
    img = bpy.data.images.new(name, w, h, alpha=True)
    img.colorspace_settings.name = "Non-Color"
    img.alpha_mode = "CHANNEL_PACKED"
    px = [0.0] * (w * h * 4)
    for y in range(h):
        src = (h - 1 - y) * w * 4; dst = y * w * 4          # Blender rows are bottom-up
        px[dst:dst + w * 4] = [v / 255.0 for v in rgba[src:src + w * 4]]
    img.pixels.foreach_set(px)
    img.update(); img.pack()
    return img


def _time_socket(nt):
    node = nt.nodes.get(TIME_NODE)
    if node is None:
        node = nt.nodes.new("ShaderNodeValue"); node.name = TIME_NODE
        node.label = "Seconds since frame 1"
        driver = node.outputs[0].driver_add("default_value").driver
        driver.type = "SCRIPTED"; driver.expression = "(frame - 1) / 30.0"
    return node.outputs[0]


def _material(name, image, emitter, slots):
    """Additive sprite whose age, size, rotation and colour come from per-quad attributes."""
    mat = bpy.data.materials.new(name); mat.use_nodes = True
    nt = mat.node_tree; nt.nodes.clear(); N, L = nt.nodes, nt.links

    def math_node(op, a, b=None, c=None, clamp=False):
        n = N.new("ShaderNodeMath"); n.operation = op; n.use_clamp = clamp
        for i, v in enumerate((a, b, c)):
            if v is None:
                continue
            if isinstance(v, (int, float)):
                n.inputs[i].default_value = float(v)
            else:
                L.new(v, n.inputs[i])
        return n.outputs[0]

    def hash01(x, salt):
        # fract(sin(x * 12.9898 + salt) * 43758.5453): a stable per-cycle random number
        return math_node("FRACT", math_node("MULTIPLY", math_node("SINE", math_node("MULTIPLY_ADD", x, 12.9898, salt)), 43758.5453))

    fx = N.new("ShaderNodeAttribute"); fx.attribute_type = "GEOMETRY"; fx.attribute_name = "po_fx"
    sep = N.new("ShaderNodeSeparateXYZ"); L.new(fx.outputs["Vector"], sep.inputs[0])
    offset, period, seed = sep.outputs[0], sep.outputs[1], sep.outputs[2]
    t = _time_socket(nt)
    cyc = math_node("DIVIDE", math_node("SUBTRACT", t, offset), period)
    m = math_node("FLOOR", cyc)                                          # which particle of this quad's slot
    age = math_node("MULTIPLY", math_node("SUBTRACT", cyc, m), period)   # seconds since it spawned
    life_var = hash01(math_node("ADD", m, seed), 1.7)
    life = math_node("MULTIPLY_ADD", math_node("SUBTRACT", life_var, 0.5), emitter["life_range"], emitter["life"])
    frac = math_node("DIVIDE", age, life)
    alive = math_node("LESS_THAN", frac, 1.0)
    # size = size1 * size2, each base + range * (u - 0.5)
    s1 = math_node("MULTIPLY_ADD", math_node("SUBTRACT", hash01(math_node("ADD", m, seed), 3.1), 0.5), emitter["size1"][1], emitter["size1"][0])
    s2 = math_node("MULTIPLY_ADD", math_node("SUBTRACT", hash01(math_node("ADD", m, seed), 5.3), 0.5), emitter["size2"][1], emitter["size2"][0])
    size = math_node("MULTIPLY", s1, s2)
    # quad is built at the largest size; shrink the sampled area instead of the geometry
    scale = math_node("DIVIDE", slots["max_size"], math_node("MAXIMUM", size, 1e-4))
    ang = math_node("MULTIPLY", math_node("MULTIPLY_ADD", hash01(math_node("ADD", m, seed), 7.9), 1.0, 0.0),
                    math.radians(emitter["spin_range"]))

    uv = N.new("ShaderNodeUVMap"); uv.uv_map = "UV"
    ctr = N.new("ShaderNodeVectorMath"); ctr.operation = "SUBTRACT"; ctr.inputs[1].default_value = (0.5, 0.5, 0.0)
    L.new(uv.outputs[0], ctr.inputs[0])
    sc = N.new("ShaderNodeVectorMath"); sc.operation = "SCALE"
    L.new(ctr.outputs[0], sc.inputs[0]); L.new(scale, sc.inputs["Scale"])
    rot = N.new("ShaderNodeVectorRotate"); rot.rotation_type = "Z_AXIS"
    L.new(sc.outputs[0], rot.inputs["Vector"]); L.new(ang, rot.inputs["Angle"])
    back = N.new("ShaderNodeVectorMath"); back.operation = "ADD"; back.inputs[1].default_value = (0.5, 0.5, 0.0)
    L.new(rot.outputs[0], back.inputs[0])
    tex = N.new("ShaderNodeTexImage"); tex.image = image; tex.extension = "EXTEND"; tex.interpolation = "Linear"
    L.new(back.outputs[0], tex.inputs["Vector"])

    ramp = N.new("ShaderNodeValToRGB"); cr = ramp.color_ramp; cr.interpolation = "LINEAR"
    cols = emitter["colours"]
    cr.elements[0].position = 0.0; cr.elements[1].position = 1.0
    for i in range(1, len(cols) - 1):                           # insert the interior stops in order
        cr.elements.new(i / (len(cols) - 1))
    for i, c in enumerate(cols):                                # positions are now sorted; colour by index
        cr.elements[i].color = tuple(v / 255.0 for v in c)
    L.new(math_node("MINIMUM", frac, 1.0), ramp.inputs["Fac"])

    tint = N.new("ShaderNodeVectorMath"); tint.operation = "MULTIPLY"
    L.new(tex.outputs["Color"], tint.inputs[0]); L.new(ramp.outputs["Color"], tint.inputs[1])
    gain = N.new("ShaderNodeVectorMath"); gain.operation = "SCALE"
    L.new(tint.outputs[0], gain.inputs[0])
    L.new(math_node("MULTIPLY", ramp.outputs["Alpha"], alive), gain.inputs["Scale"])
    emit = N.new("ShaderNodeEmission"); L.new(gain.outputs[0], emit.inputs["Color"])
    clear = N.new("ShaderNodeBsdfTransparent")
    add = N.new("ShaderNodeAddShader"); L.new(clear.outputs[0], add.inputs[0]); L.new(emit.outputs[0], add.inputs[1])
    out = N.new("ShaderNodeOutputMaterial"); L.new(add.outputs[0], out.inputs["Surface"])
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "BLENDED"
    else:
        mat.blend_method = "BLEND"
    mat.use_backface_culling = False
    mat["po_gx_program"] = json.dumps({"shader": "particle sprite", "blend": "ADD", "effect": emitter["name"]})
    return mat


def build(scene, collection, camera, effects_dict, names, placements, effect_name, seed=0x5EED):
    """Create one additive billboard object per emitter of ``effect_name``; returns the objects."""
    import po_crowd
    arc, emitters = read_emitters(effects_dict, names, effect_name)
    if not emitters or not placements:
        return []
    objs = []
    rng = random.Random("%s|%d" % (effect_name, seed))
    phases = [rng.random() for _ in placements]                # one phase per lamp, shared by its emitters
    for emitter in emitters:
        rate = max(emitter["rate"], 1e-6)
        life_max = emitter["life"] + abs(emitter["life_range"]) / 2
        slots = max(1, int(math.ceil(life_max * rate - 1e-9)))    # particles of one emitter alive at once
        period = slots / rate
        s1, s2 = emitter["size1"], emitter["size2"]
        max_size = (s1[0] + abs(s1[1]) / 2) * (s2[0] + abs(s2[1]) / 2)
        half = max_size / 2.0
        verts, faces, centres, corners, halves, fxs, uvs = [], [], [], [], [], [], []
        corner_uv = ((1, 1, 1, 1), (-1, 1, 0, 1), (-1, -1, 0, 0), (1, -1, 1, 0))
        for lamp, (place, phase) in enumerate(zip(placements, phases)):
            for slot in range(slots):
                c = tuple(place["position"])
                base = len(verts)
                offset = (slot + phase) / rate                 # spawn instants (k + phase) / rate, k = slot mod slots
                salt = float(lamp * 17 + slot * 5 + 1)
                for sx, sy, su, sv in corner_uv:
                    verts.append(c); centres.append(c); corners.append((sx, sy, 0.0)); halves.append((half, half, 0.0))
                    fxs.append((offset, period, salt)); uvs.append((su, sv))
                faces.append((base, base + 1, base + 2, base + 3))
        me = bpy.data.meshes.new("PO fx " + emitter["name"])
        me.from_pydata(verts, [], faces); me.update()
        for attr, data in (("po_center", centres), ("po_corner", corners), ("po_half", halves), ("po_fx", fxs)):
            a = me.attributes.new(attr, "FLOAT_VECTOR", "POINT")
            a.data.foreach_set("vector", [v for p in data for v in p])
        uvl = me.uv_layers.new(name="UV")
        uvl.data.foreach_set("uv", [v for li in range(len(me.loops)) for v in uvs[me.loops[li].vertex_index]])
        obj = bpy.data.objects.new("%s · %s" % (effect_name, emitter["name"]), me)
        collection.objects.link(obj)
        po_crowd.add_facing_modifier(obj, camera)
        image = _image(arc, names, emitter["texture_hash"], emitter["texture"].split("/")[-1])
        me.materials.append(_material("PO fx %s" % emitter["name"], image, emitter, {"max_size": max_size}))
        obj["po_effect_sprites"] = json.dumps({
            "effect": effect_name, "emitter": emitter["name"], "texture": emitter["texture"], "lamps": len(placements),
            "quads_per_lamp": slots, "life": emitter["life"], "rate": emitter["rate"], "size": [s1, s2],
            "placements": [[p["chunk"], p["offset"]] for p in placements],
            "phase": "pseudo-random steady state (start-of-scene emitter phase is not decoded)"})
        objs.append(obj)
    return objs
