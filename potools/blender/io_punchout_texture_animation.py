"""Packed-image sequence previews persisted on material nodes, including saved blends."""
import json
import hashlib
from array import array
import bpy
from bpy.app.handlers import persistent
import nlg_texture_animation


def _atlas(images):
    """Pack equal-sized source images horizontally into a reusable, packed Blender image."""
    if not images or any(i is None for i in images):
        return None
    width, height = images[0].size
    if not width or not height or any(tuple(i.size) != (width, height) for i in images):
        return None
    signature = hashlib.sha1("\0".join(i.name for i in images).encode("utf8")).hexdigest()[:12]
    name = "PO_IFL_ATLAS_%s" % signature
    old = bpy.data.images.get(name)
    if old is not None:
        return old
    atlas = bpy.data.images.new(name, width * len(images), height, alpha=True)
    # Colorspace changes can reinitialize a generated image's buffer in Blender 5.2, so set
    # it before copying pixels (doing this afterward silently replaced cutout alpha with 1).
    try: atlas.colorspace_settings.name = images[0].colorspace_settings.name
    except Exception: pass
    source = []
    for image in images:
        # Image loaders populate generated datablocks with foreach_set.  Commit those pixels
        # before reading them back; otherwise Blender exposes its initial opaque buffer here
        # even though the original image later saves with the correct cutout alpha.
        image.update()
        pixels = array("f", [0.0]) * (width * height * 4)
        image.pixels.foreach_get(pixels); source.append(pixels)
    joined = array("f", [0.0]) * (width * len(images) * height * 4)
    stride = width * 4; atlas_stride = width * len(images) * 4
    for y in range(height):
        for index, pixels in enumerate(source):
            a = y * atlas_stride + index * stride
            joined[a:a + stride] = pixels[y * stride:(y + 1) * stride]
    atlas.pixels.foreach_set(joined)
    # Commit the generated pixel buffer before packing.  Without update(), Blender saved the
    # RGB but regenerated alpha as 1.0 on reload, turning every crowd-card transparent area
    # into a large opaque black triangle.
    atlas.update(); atlas.pack(); atlas.use_fake_user = True
    return atlas


def _bake_node(node, frames):
    """Turn an IFL into packed atlas UV keys so it survives save/reopen without handlers."""
    unique_names = list(dict.fromkeys(name for name, _ in frames))
    images = [bpy.data.images.get(name) for name in unique_names]
    atlas = _atlas(images)
    if atlas is None or len(images) < 2 or not node.inputs["Vector"].is_linked:
        return False
    tree = node.id_data; source = node.inputs["Vector"].links[0].from_socket
    tree.links.remove(node.inputs["Vector"].links[0])
    separate = tree.nodes.new("ShaderNodeSeparateXYZ"); separate.name = node.name + " · IFL split"
    scale = tree.nodes.new("ShaderNodeMath"); scale.operation = "MULTIPLY"
    scale.name = node.name + " · IFL scale"; scale.inputs[1].default_value = 1.0 / len(images)
    offset = tree.nodes.new("ShaderNodeMath"); offset.operation = "ADD"
    offset.name = node.name + " · IFL frame"
    combine = tree.nodes.new("ShaderNodeCombineXYZ"); combine.name = node.name + " · IFL UV"
    tree.links.new(source, separate.inputs[0]); tree.links.new(separate.outputs[0], scale.inputs[0])
    tree.links.new(scale.outputs[0], offset.inputs[0]); tree.links.new(offset.outputs[0], combine.inputs[0])
    tree.links.new(separate.outputs[1], combine.inputs[1]); tree.links.new(separate.outputs[2], combine.inputs[2])
    tree.links.new(combine.outputs[0], node.inputs["Vector"])
    node.image = atlas; node.extension = "EXTEND"; node["po_texture_atlas"] = atlas.name
    sequence = nlg_texture_animation.Sequence(0, tuple(frames))
    previous = None
    for frame in range(1, bpy.context.scene.frame_end + 2):
        current = sequence.texture_at((frame - 1) * bpy.context.scene.render.fps_base /
                                      bpy.context.scene.render.fps)
        index = unique_names.index(current)
        if index != previous:
            offset.inputs[1].default_value = index / len(images)
            offset.inputs[1].keyframe_insert("default_value", frame=frame)
            previous = index
    action = tree.animation_data.action if tree.animation_data else None
    if action is not None:
        import po_action
        for curve in po_action.all_fcurves(action):
            if offset.name in curve.data_path:
                for point in curve.keyframe_points: point.interpolation = "CONSTANT"
    return True


def bind(node, sequence, entries, image_loader):
    frames = []
    for h, duration in sequence.frames:
        if h not in entries:
            return False
        image = image_loader(entries[h])
        if image is None:
            return False
        # Inactive frames are referenced by metadata strings, not Blender ID links.
        # Keep them across save/reload even while another frame is on the node.
        image.use_fake_user = True
        frames.append((image.name, duration))
    node["po_texture_sequence"] = json.dumps(frames)
    node.image = bpy.data.images.get(frames[0][0])
    if _bake_node(node, frames):
        return True
    if refresh not in bpy.app.handlers.frame_change_pre:
        bpy.app.handlers.frame_change_pre.append(refresh)
    if restore not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(restore)
    return True


@persistent
def refresh(scene, *unused):
    seconds = (scene.frame_current-1) * scene.render.fps_base / scene.render.fps
    for mat in bpy.data.materials:
        if not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.get("po_texture_atlas"):
                continue
            raw = node.get("po_texture_sequence")
            if raw:
                seq = nlg_texture_animation.Sequence(0, tuple(json.loads(raw)))
                image = bpy.data.images.get(seq.texture_at(seconds))
                if image is not None and node.image != image:
                    node.image = image


@persistent
def restore(*unused):
    if refresh not in bpy.app.handlers.frame_change_pre:
        bpy.app.handlers.frame_change_pre.append(refresh)
    scene = getattr(bpy.context, "scene", None)     # absent while Blender is still starting up
    if scene is not None:
        refresh(scene)


def register():
    if restore not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(restore)
    restore()


def unregister():
    for handler, callbacks in ((refresh, bpy.app.handlers.frame_change_pre),
                               (restore, bpy.app.handlers.load_post)):
        if handler in callbacks:
            callbacks.remove(handler)
