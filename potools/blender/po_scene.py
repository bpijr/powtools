"""Blender side of po_metadata: snapshot the scene, keep the versioned document, find the
active asset. Readers never write (panels may call them from draw); writers run from
operators, import hooks, the load_post handler and a one-shot timer."""
import os
import time

import bpy
from bpy.app.handlers import persistent

import po_metadata as meta

_CACHE = {}          # scene pointer -> (generation, document)
_CONTENT = {}        # (normalized archive, mtime) -> content dict
_GENERATION = [0]
_PENDING = [False]


def _plain(value):
    if hasattr(value, "to_dict"): return value.to_dict()
    if hasattr(value, "to_list"): return value.to_list()
    if isinstance(value, (str, int, float, bool)) or value is None: return value
    try: return list(value)
    except TypeError: return str(value)


def _props(idb):
    return {k: _plain(idb[k]) for k in idb.keys() if k.startswith("po_")}


def _roots(scene):
    """Collection name -> name of its top-level collection under the scene collection."""
    out = {}
    for top in scene.collection.children:
        out[top.name] = top.name
        for child in top.children_recursive:
            out.setdefault(child.name, top.name)
    return out


def object_snapshot(obj, roots=None):
    roots = roots if roots is not None else _roots(bpy.context.scene)
    item = {"name": obj.name, "type": obj.type, "props": _props(obj),
            "roots": sorted({roots[c.name] for c in obj.users_collection if c.name in roots}),
            "parent": obj.parent.name if obj.parent else None,
            "armature": next((m.object.name for m in getattr(obj, "modifiers", [])
                              if m.type == "ARMATURE" and m.object is not None), None)}
    if obj.type == "ARMATURE":
        item["bones"] = [{"name": b.name, "props": _props(b)} for b in obj.data.bones if "po_node" in b]
    return item


def snapshot(scene):
    roots = _roots(scene)
    scene_props = _props(scene)
    for name in ("po_material_state",):
        if hasattr(scene, name): scene_props.setdefault(name, getattr(scene, name))
    cols = [scene.collection] + list(scene.collection.children_recursive)
    return {"scene": scene_props, "roots": [c.name for c in scene.collection.children],
            "collections": [{"name": c.name, "props": _props(c), "children": [x.name for x in c.children]}
                            for c in cols[1:]],
            "objects": [object_snapshot(o, roots) for o in scene.objects],
            "materials": [{"name": m.name, "props": _props(m)} for m in bpy.data.materials if any(k.startswith("po_") for k in m.keys())],
            "images": [{"name": i.name, "props": _props(i)} for i in bpy.data.images if "po_texture" in i]}


def invalidate(*_unused):
    _GENERATION[0] += 1


def document(scene=None):
    """Current document (stored + derived). Never writes; safe inside Panel.draw."""
    scene = scene or bpy.context.scene
    key = scene.as_pointer(); hit = _CACHE.get(key)
    if hit and hit[0] == _GENERATION[0]:
        return hit[1]
    try:
        doc = meta.migrate(snapshot(scene))
    except meta.MetadataError as ex:
        doc = meta.new_document(); doc["error"] = str(ex)
    for asset in doc["assets"]:
        cached = _CONTENT.get(_content_key(asset["archive"]))
        if cached is not None and (asset["content"] is None or asset["content"].get("source") != "archive"):
            asset["content"] = cached
    _CACHE[key] = (_GENERATION[0], doc)
    return doc


def _content_key(path):
    try: return (meta._norm(path), os.path.getmtime(path))
    except OSError: return (meta._norm(path), None)


def content(asset):
    """Archive counts (cached per file and mtime); None when the source is missing."""
    key = _content_key(asset["archive"])
    if key[1] is None: return None
    if key not in _CONTENT:
        _CONTENT[key] = meta.content_from_archive(asset["archive"])
    return _CONTENT[key]


def needs_sync(scene):
    stored = scene.get(meta.PROPERTY)
    if not stored: return bool(document(scene)["assets"] or document(scene)["workshop"])
    try: previous = meta.load(stored)
    except meta.MetadataError: return False
    return ([a["id"] for a in previous["assets"]] != [a["id"] for a in document(scene)["assets"]] or
            any(a["content"] is None or a["content"].get("source") != "archive" for a in previous["assets"]))


def write(scene, doc):
    text = meta.dump(doc)
    if scene.get(meta.PROPERTY) != text:
        scene[meta.PROPERTY] = text
    invalidate()
    return doc


def sync(scene=None, read_archives=True):
    """Migrate old keys, fill archive content, and store one version-1 document."""
    scene = scene or bpy.context.scene
    invalidate()
    doc = meta.migrate(snapshot(scene))
    if read_archives:
        for asset in doc["assets"]:
            if asset["content"] and asset["content"].get("source") == "archive": continue
            try:
                found = content(asset)
            except (ValueError, OSError, IndexError, KeyError) as ex:
                asset["content_error"] = "%s: %s" % (os.path.basename(asset["archive"]), ex); continue
            if found is None:
                asset["content_error"] = "Source archive not found: %s" % asset["archive"]
            else:
                asset["content"] = found; asset.pop("content_error", None)
                asset["sha256"] = asset["sha256"] or found.get("sha256")
    return write(scene, doc)


def record_import(scene, kind, archive, options=None):
    doc = sync(scene)
    ident = meta.asset_id(kind, archive)
    asset = meta.find_asset(doc, ident)
    if asset is not None:
        asset["import"] = {"mode": kind, "options": options or {}, "blender": bpy.app.version_string,
                           "when": time.strftime("%Y-%m-%d %H:%M:%S")}
        doc["active"] = ident
    return write(scene, doc), asset


def record(scene, field, value, asset=None):
    """Store last_review / last_export / last_refusal (with the asset id and a timestamp)."""
    doc = meta.migrate(snapshot(scene))
    if value is not None:
        value = dict(value, asset=asset["id"] if asset else None, when=time.strftime("%Y-%m-%d %H:%M:%S"))
    doc[field] = value
    return write(scene, doc)


def set_active(scene, ident):
    doc = meta.migrate(snapshot(scene)); doc["active"] = ident
    return write(scene, doc)


def active_asset(context):
    """Asset owning the active object, else the document's active asset."""
    doc = document(context.scene)
    obj = getattr(context, "active_object", None)
    if obj is not None:
        found = meta.asset_for_object(doc, object_snapshot(obj))
        if found is not None: return doc, found
    return doc, meta.find_asset(doc, doc.get("active"))


def asset_objects(scene, asset):
    """Scene objects that belong to one asset (same rules as the document)."""
    doc = document(scene); roots = _roots(scene)
    return [o for o in scene.objects if (meta.asset_for_object(doc, object_snapshot(o, roots)) or {}).get("id") == asset["id"]]


def _timer():
    _PENDING[0] = False
    for scene in bpy.data.scenes:
        try:
            if needs_sync(scene): sync(scene)
        except Exception as ex:           # never let a background refresh raise into Blender
            print("PO Tools: metadata refresh skipped:", ex)
    return None


def schedule_sync():
    """Ask for a write outside draw (panels may not modify data)."""
    if not _PENDING[0] and not bpy.app.background:
        _PENDING[0] = True
        bpy.app.timers.register(_timer, first_interval=0.2)


@persistent
def _on_load(*_unused):
    invalidate()
    for scene in bpy.data.scenes:
        try:
            if needs_sync(scene): sync(scene)      # old-key scenes gain a version-1 document
        except Exception as ex:
            print("PO Tools: metadata migration skipped for %s: %s" % (scene.name, ex))


@persistent
def _on_update(*_unused):
    invalidate()


def register():
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.handlers.depsgraph_update_post.append(_on_update)
    bpy.app.handlers.undo_post.append(_on_update)
    bpy.app.handlers.redo_post.append(_on_update)


def unregister():
    for handlers, fn in ((bpy.app.handlers.load_post, _on_load), (bpy.app.handlers.depsgraph_update_post, _on_update),
                         (bpy.app.handlers.undo_post, _on_update), (bpy.app.handlers.redo_post, _on_update)):
        while fn in handlers: handlers.remove(fn)
    _CACHE.clear()
