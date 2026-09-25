"""Generic Blender material state. Parsing and edits live in nlg_material_layout.

The source shader is not simulated. Explicit texture nodes are inspection previews;
only the bounded controls in the Material inputs panel are written on asset export.
"""
import json
import os

import bpy
from bpy.props import EnumProperty, IntProperty

import nlg_asset
import nlg_material_layout as materials
import po_errors

SCHEMA = 1


def _path(value): return os.path.normcase(os.path.abspath(value))


def _metadata(mat):
    try: info = json.loads(mat["po_material"])
    except (KeyError, TypeError, ValueError) as ex: raise ValueError("Material has no valid source provenance.") from ex
    if not isinstance(info, dict) or info.get("binding_export") != SCHEMA:
        raise ValueError("Material was not imported with editable shader inputs.")
    required = ("section", "model_set", "chunk", "offset", "shader", "record_sha256", "source")
    if any(key not in info for key in required) or not isinstance(info["source"], dict) or not all(
            key in info["source"] for key in ("path", "sha256")):
        raise ValueError("Material source provenance is incomplete.")
    return info


def _edits(mat):
    try: edits = json.loads(mat.get("po_material_edits", "{}"))
    except (TypeError, ValueError) as ex: raise ValueError("Invalid staged material inputs.") from ex
    if not isinstance(edits, dict): raise ValueError("Invalid staged material inputs.")
    return edits


def build_material(doc, section, model_set, mesh, meta, cache, image_loader, load_textures=True):
    info = meta["material"]; info["binding_export"] = SCHEMA
    key = (section, model_set.index, info["offset"])
    if key in cache: return cache[key]
    mat = bpy.data.materials.new(("%s @%d" % (info["shader_name"], info["offset"]))[:60])
    mat["po_material"] = json.dumps({**info, "section": section, "model_set": model_set.index,
                                    "source": {"path": str(doc.path), "sha256": doc.source_hashes},
                                    "editable_container": doc.editable})
    mat["po_material_edits"] = "{}"
    rec = materials.record_for_mesh(model_set, mesh)
    for name, value in rec.values().items():
        if value is None: continue
        spec = next(f for f in rec.layout.fields if f.name == name)
        mat["po_field_" + name] = value
        mat.id_properties_ui("po_field_" + name).update(min=0, max=spec.maximum, soft_min=0, soft_max=spec.maximum,
                                                       description=spec.label + "; saved through guarded game material patches")
    values = rec.values()
    alpha = values.get("alpha"); alpha = 1.0 if alpha is None else alpha
    entries = {e.index: e for e in doc.sections[section].textures}
    refs = info.get("texture_inputs", [])
    local = {r["slot"]: entries[r["indices"][0]] for r in refs if load_textures and r["indices"] and r["indices"][0] in entries}
    import nlg_texture_animation
    try:
        sequences = nlg_texture_animation.sequences(doc.sections[section].archive)
    except ValueError as ex:
        sequences = {}
        mat["po_texture_sequence_error"] = str(ex)
    by_hash = {e.hash: e for e in entries.values()}
    animated = {}
    for ref in refs:
        sequence = sequences.get(ref["hash"])
        if load_textures and sequence and all(h in by_hash for h, _ in sequence.frames):
            local[ref["slot"]] = by_hash[sequence.frames[0][0]]
            animated[ref["slot"]] = sequence
    shader = materials.LAYOUTS[rec.shader].name if rec.layout else None
    note = "Approximate source preview: input 0 on UV0."
    try: mat.use_nodes = True
    except AttributeError: pass
    if shader and load_textures:
        note = _game_preview(mat, shader, values, local, doc.sections[section].archive, image_loader, refs)
    tree = mat.node_tree
    emission = next((n for n in tree.nodes if n.type == "EMISSION" and n.name == "PO_TEV_Output"), None)
    if emission is None:
        # Unknown shaders still preview through an unlit output.  A Principled fallback
        # reintroduces scene-light/PBR terms which the source GX material never requested.
        tree.nodes.clear()
        out = tree.nodes.new("ShaderNodeOutputMaterial")
        emission = tree.nodes.new("ShaderNodeEmission"); emission.name = "PO_TEV_Output"
        emission.inputs["Color"].default_value = (0.8, 0.8, 0.8, 1.0)
        tree.links.new(emission.outputs[0], out.inputs["Surface"])
    for ref in refs:
        tex = tree.nodes.new("ShaderNodeTexImage"); tex.name = "PO_Input_%d" % ref["slot"]
        tex.label = "%s: %s" % (ref["label"], ref["name"])
        tex.location = (-1600, 300 - ref["slot"] * 300)
        tex["po_input_slot"] = ref["slot"]
        if ref["slot"] in local:
            tex.image = image_loader(local[ref["slot"]])
        # Without a game preview (textures off, unknown shader), input 0 on UV0 is shown lit.
        if note.startswith("Approximate") and ref["slot"] == 0 and tex.image is not None:
            uv = tree.nodes.new("ShaderNodeUVMap"); uv.uv_map = "UV"; uv.location = (-1850, 300)
            tree.links.new(uv.outputs["UV"], tex.inputs["Vector"])
            tree.links.new(tex.outputs["Color"], emission.inputs["Color"])
    mat.diffuse_color = (0.8, 0.8, 0.8, alpha)
    if animated:
        import io_punchout_texture_animation
        for slot, sequence in animated.items():
            first_image = image_loader(by_hash[sequence.frames[0][0]])
            for node in tree.nodes:
                if node.type == "TEX_IMAGE" and node.image == first_image:
                    io_punchout_texture_animation.bind(node, sequence, by_hash, image_loader)
    _surface_preview(mat, mesh, refs)
    # po_shader stamps po_material=True; the provenance record must be the last word.
    mat["po_material"] = json.dumps({**info, "section": section, "model_set": model_set.index,
                                    "source": {"path": str(doc.path), "sha256": doc.source_hashes},
                                    "editable_container": doc.editable})
    mat["po_preview_note"] = note + " Only Material inputs panel controls are exported; node/image edits are preview-only."
    cache[key] = mat
    return mat


def _surface_preview(mat, mesh, refs):
    """Preview alpha and authored vertex colors. Effect naming is a preview policy,
    not a claim that the opaque GX blend-state words have been decoded."""
    tree = mat.node_tree
    emission = tree.nodes.get("PO_TEV_Output") or next((n for n in tree.nodes if n.type == "EMISSION"), None)
    tex = tree.nodes.get("PO_Slot0") or tree.nodes.get("PO_Input_0")
    if emission is None or tex is None or tex.image is None:
        return
    alpha = tex.outputs["Alpha"]
    try: opacity = float(mat.get("po_field_alpha", 1.0))
    except (TypeError, ValueError): opacity = 1.0
    if opacity != 1.0:
        multiply = tree.nodes.new("ShaderNodeMath"); multiply.operation = "MULTIPLY"
        tree.links.new(alpha, multiply.inputs[0]); multiply.inputs[1].default_value = opacity
        alpha = multiply.outputs[0]
    attr = mesh.attribute("color", 0)
    if attr is not None and attr.storage:
        vertex = tree.nodes.new("ShaderNodeVertexColor")
        vertex.layer_name = "PO_Color0"; vertex.name = "PO_VertexColor"
        # Preserve the existing TEV graph, multiplying at its final color input.
        socket = emission.inputs["Color"]
        if socket.is_linked:
            source = socket.links[0].from_socket
            multiply = tree.nodes.new("ShaderNodeMixRGB"); multiply.blend_type = "MULTIPLY"
            multiply.inputs[0].default_value = 1.0
            tree.links.new(source, multiply.inputs[1]); tree.links.new(vertex.outputs["Color"], multiply.inputs[2])
            tree.links.new(multiply.outputs[0], socket)
        multiply = tree.nodes.new("ShaderNodeMath"); multiply.operation = "MULTIPLY"
        tree.links.new(alpha, multiply.inputs[0]); tree.links.new(vertex.outputs["Alpha"], multiply.inputs[1])
        alpha = multiply.outputs[0]
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "DITHERED"
    elif hasattr(mat, "blend_method"):
        mat.blend_method = "HASHED"
    names = " ".join(r["name"].lower() for r in refs if r["slot"] == 0)
    if any(word in names for word in ("/laser", "/glow", "/lightshaft", "/spotlightglow")):
        emission.inputs["Strength"].default_value = 4.0
        # Alpha is already applied once by PO_AlphaBlend below.  Premultiplying the colour
        # here as well squared the source alpha and made the authored green beams disappear
        # against the arena.  GX's blended effect texture carries straight RGB + alpha.
        tree.links.new(tex.outputs["Color"], emission.inputs["Color"])
        mat["po_blend_preview"] = "additive (effect texture naming; runtime state unverified)"
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = "BLENDED"
        elif hasattr(mat, "blend_method"):
            mat.blend_method = "BLEND"
        try: mat.use_backface_culling = False
        except AttributeError: pass
    # GX alpha is represented with a transparent/emission mix.  This keeps every
    # surface on the same unlit final path while retaining authored cutouts.
    out = next(n for n in tree.nodes if n.type == "OUTPUT_MATERIAL")
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    mix = tree.nodes.new("ShaderNodeMixShader"); mix.name = "PO_AlphaBlend"
    tree.links.new(alpha, mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1]); tree.links.new(emission.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], out.inputs["Surface"])


# ---------------------------------------------------------------------------------------
# Game previews. Slot roles come from the texture names every record of a shader uses
# across the retail dump (e.g. hippobasicenvironment: surface / white / *_lightmap* /
# specramp / white); a texture slot N reads UV set N (lightmap atlases sit inside 0..1 on
# UV2, or UV3 for stadiumflatreflection). GX blends gamma values, so images are Non-Color,
# products stay in gamma space and one Gamma 2.2 node converts at the end, as po_shader does.

# Skin-family shaders share the fighter graph (po_shader.po_build_shader). Only the
# documented skin record has a known tint, so only it drives the tinted rim light.
SKIN_FAMILY = {"hippodiffuseskin": dict(detail=0, specmask=2, ramp=3, rim=4, hdr=5, fresnel=6, specramp=7),
               "hippodiffusenonskin": dict(detail=0, specmask=2, ramp=3, rim=4, hdr=5, fresnel=6, specramp=7),
               "crowdskin": dict(detail=0, ramp=1), "crowdskindk": dict(detail=0, ramp=1)}
# (multiplied slots, lightmap slot, lit by lamps). Baked-lightmap shaders are unlit.
MULTIPLY_FAMILY = {"hippobasicenvironment": ((0, 1), 2, False), "hippohwlitenvironment": ((0, 1), 2, True),
                   "stadiumflatreflection": ((0,), 3, False), "ropeskin": ((0, 1), None, True),
                   "diffusedetail": ((0, 1), None, False), "diffuse": ((0,), None, False),
                   "constantcolour": ((0,), None, False)}
# The TEV scale applied to lightmaps is not decoded from main.dol; 2x is the usual GX
# "modulate 2x" and is exposed per material as the PO_LightmapScale node.
LIGHTMAP_SCALE = 2.0


def _game_preview(mat, shader, values, local, archive, image_loader, refs):
    import po_shader
    if shader in SKIN_FAMILY:
        roles = SKIN_FAMILY[shader]
        import nlg_texture

        def stops(slot):
            if slot in local:
                e = local[slot]; raw = nlg_texture.decode_texture(archive, e); w, h = e.width, e.height
                if h > w:       # tall strips (crowd_gradient, 8 x 32) run along V: read them lengthwise
                    raw = [raw[(y * w + x) * 4 + c] for x in range(w) for y in range(h) for c in range(4)]; w, h = h, w
                return po_shader.ramp_stops_from_texture(raw, w, h)
            h = next((r["hash"] for r in refs if r["slot"] == slot), None)
            c = po_shader.GLOBAL_CONST.get(h, (1.0, 1.0, 1.0))
            return [(0.0, c), (1.0, c)]
        def lut_image(slot):
            if slot not in local: return None
            e = local[slot]; raw = nlg_texture.decode_texture(archive, e)
            width, height = e.width, e.height
            if height > width:
                raw = [raw[(y * width + x) * 4 + c]
                       for x in range(width) for y in range(height) for c in range(4)]
                width, height = height, width
            cols = po_shader._ramp_columns(raw, width, height)
            name = "PO exact LUT %08X" % e.hash
            image = bpy.data.images.get(name)
            if image is None or tuple(image.size) != (width, 1):
                if image is not None: bpy.data.images.remove(image)
                image = bpy.data.images.new(name, width, 1, alpha=True)
            try: image.colorspace_settings.name = "Non-Color"
            except (AttributeError, TypeError): pass
            # Populate even when the image datablock already exists: several materials share
            # one LUT hash, and Blender can retain an allocated-but-uncommitted image between
            # graph builds.  update() is required before packing or the saved .blend reloads a
            # black LUT, which made DK's bananas and barrel nearly black.
            image.pixels.foreach_set([v for c in cols for v in (c[0], c[1], c[2], 1.0)])
            image.update()
            image.pack()
            return image
        img = lambda role: image_loader(local[roles[role]]) if role in roles and roles[role] in local else None
        # Neutral white/black slot values disable the environment-map stage.  Treating a
        # literal ``white`` placeholder as HDR emission bleached DK's barrel and ladder.
        hdr = None
        if "hdr" in roles and roles["hdr"] in local and "hdr" in local[roles["hdr"]].name.lower():
            hdr = img("hdr")
        po_shader.po_build_shader(mat, dict(
            ramp=stops(roles["ramp"]), detail=img("detail"), damage=None,
            ramp_image=lut_image(roles["ramp"]),
            specmask=img("specmask"),
            specramp=(stops(roles["specramp"]) if "specramp" in roles and roles["specramp"] in local else None),
            specramp_image=(lut_image(roles["specramp"]) if "specramp" in roles else None),
            rim=(stops(roles["rim"]) if "rim" in roles and roles["rim"] in local else None),
            hdr=hdr, fresnel=(stops(roles["fresnel"]) if hdr is not None and "fresnel" in roles and roles["fresnel"] in local else None),
            tint=tuple(values.get("tint") or (1.0, 1.0, 1.0)),
            spec_power=values.get("specular_power") or 32.0,
            alpha=1.0 if values.get("alpha") is None else values["alpha"], hurt=False,
            tex_names={r["slot"]: r["name"] for r in refs}))
        return "Game preview (%s, the fighter skin graph)." % shader
    tree = mat.node_tree; tree.nodes.clear()
    out = tree.nodes.new("ShaderNodeOutputMaterial"); out.location = (1100, 0)
    emission = tree.nodes.new("ShaderNodeEmission"); emission.name = "PO_TEV_Output"; emission.location = (850, 0)
    tree.links.new(emission.outputs[0], out.inputs["Surface"])

    def texture(slot, y):
        if slot not in local: return None
        uv = tree.nodes.new("ShaderNodeUVMap"); uv.uv_map = "UV" if slot == 0 else "UV%d" % slot
        uv.location = (-1100, y)
        t = tree.nodes.new("ShaderNodeTexImage"); t.name = "PO_Slot%d" % slot; t.location = (-900, y)
        t.image = image_loader(local[slot]); t.extension = "REPEAT"
        try: t.image.colorspace_settings.name = "Non-Color"
        except (AttributeError, TypeError): pass
        tree.links.new(uv.outputs["UV"], t.inputs["Vector"])
        return t.outputs["Color"]

    def vmath(op, a, b, x, y):
        n = tree.nodes.new("ShaderNodeVectorMath"); n.operation = op; n.location = (x, y)
        tree.links.new(a, n.inputs[0])
        if isinstance(b, tuple): n.inputs[1].default_value = b
        else: tree.links.new(b, n.inputs[1])
        return n

    if shader == "stadiumdetailmaskwithuvsliding":      # base + banner x mask (neon, tickers)
        base, banner, mask = texture(0, 300), texture(1, 0), texture(2, -300)
        color = vmath("MULTIPLY", banner, mask, -600, 0).outputs[0] if banner and mask else banner
        color = vmath("ADD", base, color, -400, 150).outputs[0] if base and color else (base or color)
        lit = False
    elif shader in MULTIPLY_FAMILY:
        slots, lightmap, lit = MULTIPLY_FAMILY[shader]
        color = None; y = 300
        for slot in slots:
            c = texture(slot, y); y -= 300
            if c is not None: color = c if color is None else vmath("MULTIPLY", color, c, -600, y + 150).outputs[0]
        if lightmap is not None and lightmap in local and "lightmap" in local[lightmap].name.lower():
            scale = vmath("SCALE", texture(lightmap, y), (0, 0, 0), -600, y)
            scale.name = "PO_LightmapScale"; scale.label = "Lightmap scale (TEV scale unverified)"
            scale.inputs["Scale"].default_value = LIGHTMAP_SCALE
            color = scale.outputs[0] if color is None else vmath("MULTIPLY", color, scale.outputs[0], -400, y).outputs[0]
    else:
        return "Approximate source preview: input 0 on UV0."
    if color is not None and lit:
        # Fixed-function preview of the source light-coordinate stage: N dot L is a
        # scalar TEV input, not a Blender lamp.  The lower bound preserves the game's
        # deliberately readable shadow palette rather than producing PBR black.
        geo = tree.nodes.new("ShaderNodeNewGeometry"); geo.location = (-400, -500)
        light = tree.nodes.new("ShaderNodeCombineXYZ"); light.name = "PO_LightVector"; light.location = (-400, -650)
        light.inputs["X"].default_value, light.inputs["Y"].default_value, light.inputs["Z"].default_value = po_shader.KEY_LIGHT_DIR
        dot = tree.nodes.new("ShaderNodeVectorMath"); dot.operation = "DOT_PRODUCT"; dot.name = "PO_NdotL"; dot.location = (-150, -500)
        tree.links.new(geo.outputs["Normal"], dot.inputs[0])
        tree.links.new(light.outputs[0], dot.inputs[1])
        mapped = tree.nodes.new("ShaderNodeMapRange"); mapped.name = "PO_NdotL_Map"; mapped.location = (50, -500)
        mapped.inputs[1].default_value = -1.0; mapped.inputs[2].default_value = 1.0
        mapped.inputs[3].default_value = 0.25; mapped.inputs[4].default_value = 1.0; mapped.clamp = True
        tree.links.new(dot.outputs["Value"], mapped.inputs[0])
        shade = tree.nodes.new("ShaderNodeVectorMath"); shade.operation = "SCALE"; shade.location = (250, -100)
        tree.links.new(color, shade.inputs[0]); tree.links.new(mapped.outputs["Result"], shade.inputs["Scale"])
        color = shade.outputs[0]
    if color is not None:
        clamp = vmath("MINIMUM", color, (1.0, 1.0, 1.0), -200, 0)
        gamma = tree.nodes.new("ShaderNodeGamma"); gamma.location = (0, 0)
        gamma.inputs["Gamma"].default_value = 2.2
        tree.links.new(clamp.outputs[0], gamma.inputs["Color"])
        color = gamma.outputs["Color"]
    if color is not None: tree.links.new(color, emission.inputs["Color"])
    else: emission.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    emission.inputs["Strength"].default_value = 1.0
    return "Game preview (%s)." % shader


def _source(mat):
    info = _metadata(mat)
    from io_punchout_asset import _names_for
    doc = nlg_asset.AssetDocument(info["source"]["path"], _names_for(info["source"]["path"]))
    if not doc.editable: raise ValueError("This source container is read-only.")
    if info["source"]["sha256"] != doc.source_hashes: raise ValueError("Source archive changed after material import.")
    if info["section"] != 0: raise ValueError("Only single-section material edits can be exported.")
    a = doc.sections[0].archive
    rec = materials.find_record(a, info["chunk"], info["offset"])
    if rec.shader != info["shader"] or rec.describe()["record_sha256"] != info["record_sha256"]:
        raise ValueError("Material source record differs from the imported provenance.")
    return doc, rec


def stage_texture(mat, slot, texture_hash):
    doc, rec = _source(mat)
    field = "texture_%d" % slot if type(slot) is int else "invalid"
    materials.patches(doc.sections[0].archive, rec.chunk, rec.offset, field, texture_hash)
    edits = _edits(mat); edits[field] = texture_hash
    mat["po_material_edits"] = json.dumps(edits, sort_keys=True)


def collect_material_patches(doc, objects):
    """Validate ownership and merge the material edits from imported mesh objects.

    Shared source records can have several Blender copies only if every copy agrees.
    Material reassignment, shader changes, opaque state edits, and new keys are refused.
    """
    result = []; signatures = {}
    for obj, meta in objects:
        expected = meta["material"]
        if expected.get("binding_export") != SCHEMA: continue  # older generic scenes had no material export
        with po_errors.context(model_set=meta["model_set"], mesh=obj.name):
            result += _object_material_patches(doc, obj, meta, signatures)
    return result


def _object_material_patches(doc, obj, meta, signatures):
    result = []
    if len(obj.data.materials) != 1 or obj.data.materials[0] is None or any(p.material_index != 0 for p in obj.data.polygons):
        raise po_errors.Refusal("keep the one imported material assignment; edit its inputs in the Material inputs panel.",
                                field="material slots")
    mat = obj.data.materials[0]; info = _metadata(mat)
    ms = doc.sections[meta["source"]["section"]].model_sets[meta["model_set"]]
    mesh = ms.meshes[meta["mesh"]["index"]]; rec = materials.record_for_mesh(ms, mesh)
    if (info.get("source", {}).get("sha256") != doc.source_hashes or
            _path(info["source"]["path"]) != _path(str(doc.path)) or info.get("section") != 0 or
            info.get("model_set") != ms.index or info.get("chunk") != rec.chunk or
            info.get("offset") != rec.offset or info.get("shader") != rec.shader or
            info.get("record_sha256") != rec.describe()["record_sha256"]):
        raise po_errors.Refusal("material assignment/source provenance changed; shader and material reassignment are locked.",
                                field="material '%s' po_material" % mat.name)
    edits = _edits(mat)
    if any(not isinstance(k, str) or not k.startswith("texture_") for k in edits):
        raise po_errors.Refusal("Staged inputs contain an opaque or unsupported material field.",
                                field="material '%s' po_material_edits" % mat.name)
    for field, old in rec.values().items():
        if old is None: continue
        name = "po_field_" + field
        if name not in mat: raise po_errors.Refusal("Imported material control is missing: " + field, field=name)
        value = mat[name]
        if isinstance(old, list): value = list(value)
        if value != old: edits[field] = value
    patches = []
    for field, value in sorted(edits.items()):
        with po_errors.context(field="material '%s' %s" % (mat.name, field)):
            patches += materials.patches(ms.archive, rec.chunk, rec.offset, field, value)
    signature = tuple((p.offset, p.old, p.new) for p in patches)
    key = (rec.chunk, rec.offset)
    if key in signatures:
        if signatures[key] != signature:
            raise po_errors.Refusal("Copies of shared material @%d disagree. Apply the same inputs to all %d mesh users."
                                    % (rec.offset, len(rec.meshes)), field="material '%s'" % mat.name)
    else:
        signatures[key] = signature; result += patches
    return result


def _texture_choices(self, context):
    return getattr(self, "_choices", (("NONE", "No local textures", ""),))


class PO_MATERIAL_OT_texture(bpy.types.Operator):
    """Choose a texture already in the source archive for this shader input"""
    bl_idname = "po.material_texture"
    bl_label = "Choose material texture"
    bl_options = {"REGISTER", "UNDO"}
    slot: IntProperty(name="Input", min=0, max=7)
    texture: EnumProperty(name="Texture", items=_texture_choices)

    def invoke(self, context, event):
        try:
            doc, rec = _source(context.object.active_material)
            # Keep enum strings alive for Blender's dynamic enum storage.
            self._choices = [(str(e.hash), e.name, "%d × %d; texture %d" % (e.width, e.height, e.index))
                             for e in doc.sections[0].textures]
            if not self._choices: raise ValueError("No local texture replacements in this archive.")
            self.texture = self._choices[0][0]
            return context.window_manager.invoke_props_dialog(self, width=600)
        except (ValueError, OSError) as ex:
            self.report({"ERROR"}, str(ex)); return {"CANCELLED"}

    def execute(self, context):
        try:
            stage_texture(context.object.active_material, self.slot, int(self.texture))
        except (ValueError, OSError) as ex:
            self.report({"ERROR"}, str(ex)); return {"CANCELLED"}
        self.report({"INFO"}, "Texture input staged. Export the asset to save the guarded material patch.")
        return {"FINISHED"}


class PO_MATERIAL_PT_inputs(bpy.types.Panel):
    bl_idname = "PO_PT_material_inputs"
    bl_label = "Material inputs"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PO Tools"

    @classmethod
    def poll(cls, context):
        mat = context.object.active_material if context.object and context.object.type == "MESH" else None
        record = mat.get("po_material") if mat is not None else None
        # Generic-asset materials carry a JSON record; combined fighters use the Material editor.
        return isinstance(record, str) and record.startswith("{")

    def draw(self, context):
        mat = context.object.active_material; col = self.layout
        try: info = _metadata(mat); edits = _edits(mat)
        except ValueError as ex: col.label(text=str(ex)); return
        col.label(text=info["shader_name"])
        col.label(text="%d bytes; %d source mesh users" % (info["bytes"], len(info["meshes"])))
        if info.get("limitation"): col.label(text=info["limitation"], icon="LOCKED"); return
        controls = col.column(); controls.enabled = bool(info.get("editable_container"))
        for field in materials.LAYOUTS[info["shader"]].fields:
            key = "po_field_" + field.name
            if key in mat: controls.prop(mat, '["%s"]' % key, text=field.label)
        for ref in info["texture_inputs"]:
            row = controls.row(); field = "texture_%d" % ref["slot"]
            row.label(text=ref["label"] + (" (staged)" if field in edits else ""))
            op = row.operator(PO_MATERIAL_OT_texture.bl_idname, text="Choose…"); op.slot = ref["slot"]
            controls.label(text=ref["name"] + " · " + ref["resolution"])
        col.label(text="Only these controls export; other shader state is preserved.", icon="INFO")


CLASSES = (PO_MATERIAL_OT_texture, PO_MATERIAL_PT_inputs)


def register():
    for cls in CLASSES: bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES): bpy.utils.unregister_class(cls)
