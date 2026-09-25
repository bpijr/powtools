"""Headless whole-cutscene import/export (io_punchout_cutscene) with the real Glass Joe knockout.

    blender --background --factory-startup --python-exit-code 1 --python this_file

Needs PO_FIXTURE_ROOT (the NIS and its actors are game data); prints a skip otherwise.
Checks: Automatic import picks the cutscene, every shot lands on one timeline with both
actors, eight props, helpers and the camera; a no-edit export is empty; an actor bone, a
prop and the camera lens edited on one frame come back exactly from the written archive. Outputs stay in temp.
"""
from pathlib import Path
import json
import math
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import bpy
from mathutils import Quaternion, Vector
import po_tools_bootstrap as boot
import fixtures

KNOCKOUT = "NIS/GlassJoe/knockout.dict"


def reset():
    for o in list(bpy.data.objects): bpy.data.objects.remove(o)
    for c in list(bpy.data.collections): bpy.data.collections.remove(c)
    for a in list(bpy.data.actions): bpy.data.actions.remove(a)
    bpy.context.scene.timeline_markers.clear()
    bpy.context.scene.pop("po_metadata", None)


def main():
    root = fixtures.fixture_root((KNOCKOUT, "characters/glassjoe.dict", "characters/littlemac.dict"))
    if root is None:
        print("BLENDER_CUTSCENE_ROUNDTRIP_PASS", bpy.app.version_string, "skipped (set PO_FIXTURE_ROOT)"); return
    boot.register()
    import io_punchout_ui as ui, io_punchout_cutscene as ioc, nlg_cutscene as cut, po_scene
    source = str((root / KNOCKOUT).resolve())
    kind, message = ui.import_any(source, "AUTO", load_textures=False, import_outline=False, arena="NONE")
    assert kind == "cutscene", kind
    scene = bpy.context.scene
    assert scene.frame_start == 1 and scene.frame_end == 206 and scene.render.fps == 30
    assert len(scene.timeline_markers) == 6 and scene.camera is not None and scene.camera.animation_data.action
    actors = {json.loads(o["po_cutscene_actor"])["name"]: o for o in bpy.data.objects
              if "po_cutscene_actor" in o and json.loads(o["po_cutscene_actor"])["role"] == "actor rig"}
    assert set(actors) == {"GlassJoe", "LittleMac"}, set(actors)
    props = {json.loads(o["po_cutscene_actor"])["id"] for o in bpy.data.objects
             if "po_cutscene_actor" in o and json.loads(o["po_cutscene_actor"])["kind"] == "prop"}
    assert len(props) == 8, props
    doc = po_scene.sync(); asset = next(a for a in doc["assets"] if a["kind"] == "cutscene")
    p = ui.meta.profile(asset); assert p["cameras"] and p["rig"] and p["exportable"]
    assert not ioc.collect_patch_set(source).patches, "a no-edit export must be empty"

    # Edit one frame of an actor bone, a prop and the camera lens.
    F = 40
    joe = actors["GlassJoe"]
    bone = next(b for b in joe.data.bones if "head" in b.name.lower()); pb = joe.pose.bones[bone.name]
    scene.frame_set(F)
    pb.rotation_quaternion = pb.rotation_quaternion @ Quaternion(Vector((1, 0, 0)), math.radians(30))
    pb.keyframe_insert("rotation_quaternion", frame=F)
    head = ioc._armature_world(joe)(F)[bone.name].copy()
    prop = next(o for o in bpy.data.objects if o.name.startswith("cros01:"))
    prop.location.z += 0.5; prop.keyframe_insert("location", frame=F)
    placed = ioc._object_world(prop)(F).copy()
    cam = scene.camera; cam.data.lens *= 1.5; cam.data.keyframe_insert("lens", frame=F); lens = cam.data.lens
    bpy.context.view_layer.objects.active = joe
    assert bpy.ops.po.review_changes() == {"FINISHED"}
    review = dict(po_scene.document()["last_review"]["sections"])
    assert "Animation keys" in review and "Camera samples" in review, review
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "knockout_edit.dict"
        assert bpy.ops.export_scene.punchout_any("EXEC_DEFAULT", filepath=str(out)) == {"FINISHED"}
        last = po_scene.document()["last_export"]; assert last["target"] == "file", last
        assert out.with_suffix(".patchset.json").is_file()
        written = cut.Cutscene(out, names=None)
        original = cut.Cutscene(source)
        shot = next(s for s in written.shots if s.first < F <= s.last + 1); i = F - 1 - shot.first
        joe_actor = next(a for a in original.actors if a.name == "GlassJoe")
        index = next(c for s, c in joe_actor.appearances if (s.section, s.index) == (shot.section, shot.index)).index
        world = ioc._Poser(joe_actor).world(written.doc.sections[shot.section].animations.clips[index], i)[int(bone["po_node"])]
        assert (world.translation - head.translation).length < 1e-3
        assert math.degrees(world.to_quaternion().rotation_difference(head.to_quaternion()).angle) < 0.5
        meta = json.loads(prop["po_cutscene_actor"]); prop_actor = next(a for a in original.actors if a.ident == meta["id"])
        index = next(c for s, c in prop_actor.appearances if (s.section, s.index) == (shot.section, shot.index)).index
        pw = ioc._Poser(prop_actor).world(written.doc.sections[shot.section].animations.clips[index], i)[meta["node"]]
        assert (pw.translation - placed.translation).length < 1e-4
        assert abs(shot.camera.tracks["angle_radians"][i][0] - 2 * math.atan(ioc.SENSOR / 2 / lens)) < 1e-5

    print("BLENDER_CUTSCENE_ROUNDTRIP_PASS", bpy.app.version_string, "shots", len(scene.timeline_markers))


main()
