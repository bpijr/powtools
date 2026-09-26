"""Fighter path in Blender 3.6 and 5.x: combined import with every animation and facial
morph strip, NLA evaluation through po_action, unedited and edited export, and re-import of
the export.

blender --background --factory-startup --python-exit-code 1 --python this_file
Needs PO_FIXTURE_ROOT (Bear Hugger, Glass Joe and the referee); prints a skip otherwise.
Outputs go to a private temporary folder outside Git and the dump.
"""
from pathlib import Path
import math
import os
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import bpy
import po_tools_bootstrap as boot
import po_action
import nlg_anim2
import nlg_model
from nlg_pack import Archive
import fixtures

GEOMETRY = {0xB601, 0xB603, 0xB016, 0xB007, 0xB006, 0xB005, 0xB004, 0xB00B, 0xB00A, 0xB00C}


def close(a, b, tol=1e-4):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def check_import(path, arm, obj):
    rig = nlg_anim2.Rig(str(path), str(path.parents[1] / "hashid.bin"))
    track = arm.animation_data.nla_tracks["PunchOut Anims"]
    strips = {s.name: s for s in track.strips}
    assert sorted(strips) == sorted(rig.anims), (len(strips), len(rig.anims))
    assert arm.animation_data.action is None, "an active action would override the NLA strips"
    bones = [pb for pb in arm.pose.bones]
    for name, strip in strips.items():
        frames = rig.anims[name][0]
        curves = list(po_action.fcurves(strip.action, strip.action_slot if po_action.LAYERED else None))
        if po_action.LAYERED:
            assert po_action.layout(strip.action) == (1, 1, 1, 1), po_action.layout(strip.action)
            assert po_action.slot_target(strip.action_slot) == "OBJECT"
        assert len(curves) == 7 * len(bones), (name, len(curves))
        assert all(len(fc.keyframe_points) == frames for fc in curves), name
    # Play the longest clip through the NLA: every bone must follow its F-curves.
    name = max(strips, key=lambda n: rig.anims[n][0])
    strip = strips[name]; frames = rig.anims[name][0]; k = frames // 2
    curves = {(fc.data_path, fc.array_index): fc for fc in po_action.fcurves(strip.action, strip.action_slot if po_action.LAYERED else None)}
    bpy.context.scene.frame_set(int(strip.frame_start) + k)
    bpy.context.view_layer.update()
    moved = 0
    for pb in bones:
        q = [curves[(pb.path_from_id("rotation_quaternion"), i)].evaluate(k + 1) for i in range(4)]
        t = [curves[(pb.path_from_id("location"), i)].evaluate(k + 1) for i in range(3)]
        assert close(pb.rotation_quaternion, q) and close(pb.location, t), (name, pb.name)
        moved += not close(pb.rotation_quaternion, (1, 0, 0, 0), 1e-3)
    assert moved, "NLA evaluation left every bone at rest"
    morphs = 0
    sk = obj.data.shape_keys
    if sk is not None and sk.animation_data:
        for mstrip in sk.animation_data.nla_tracks["PunchOut Morphs"].strips:
            slot = mstrip.action_slot if po_action.LAYERED else None
            if po_action.LAYERED: assert po_action.slot_target(slot) == "KEY"
            mcurves = list(po_action.fcurves(mstrip.action, slot)); morphs += 1
            assert mcurves and all(fc.data_path.startswith("key_blocks[") for fc in mcurves)
            anim = mstrip.name[:-len("_morphs")]
            assert mstrip.frame_start == strips[anim].frame_start
            f = max(1, len(mcurves[0].keyframe_points) // 2)
            bpy.context.scene.frame_set(int(mstrip.frame_start) + f - 1)
            bpy.context.view_layer.update()
            for fc in mcurves:
                assert abs(sk.path_resolve(fc.data_path) - fc.evaluate(f)) < 1e-4, (mstrip.name, fc.data_path)
    bpy.context.scene.frame_set(0)
    return len(strips), morphs


def normals_preserved(source, output):
    """Share of exported vertices (matched by position and UV) keeping the authored normal."""
    def key(p, t): return tuple(round(x, 3) for x in p) + tuple(round(x, 3) for x in t)
    src, out = nlg_model.model_sets(Archive(str(source)))[0], nlg_model.model_sets(Archive(str(output)))[0]
    total = same = 0
    for ma, mb in zip(src.meshes, out.meshes):
        assert len(ma.triangles()) == len(mb.triangles()), ma.index
        ref = {}
        for p, t, n in zip(ma.attribute("position").values(), ma.attribute("texcoord").values(), ma.attribute("normal").values()):
            ref.setdefault(key(p, t), []).append(n)
        for p, t, n in zip(mb.attribute("position").values(), mb.attribute("texcoord").values(), mb.attribute("normal").values()):
            cands = ref.get(key(p, t))
            if not cands: continue        # decal patches are lifted off the skin on import
            total += 1
            ln = math.sqrt(sum(x * x for x in n)) or 1
            same += max(sum(x * y for x, y in zip(n, c)) / ln / (math.sqrt(sum(x * x for x in c)) or 1) for c in cands) > 0.9999
    return same / total


def morph_records(path):
    """Every morph record as (mesh, channel, shape, vertex position, delta), counted.

    Local vertex indices are renumbered on export, so records are compared by the position of
    the vertex they move. Counting them catches a delta copied onto a coincident vertex it never
    belonged to (a hair flap's delta landing on the scalp under it)."""
    from collections import Counter
    from nlg_morph import Morphs
    m = Morphs(str(path))
    out = Counter()
    for ch in range(m.B):
        for mi, si, recs in m.channel_deltas(ch):
            pos = m.meshes[mi].pos
            for idx, dx, dy, dz in recs:
                out[(mi, ch, si) + tuple(round(c, 4) for c in pos[idx])
                    + tuple(round(c, 4) for c in (dx, dy, dz))] += 1
    return out


def chunk_diff(a_path, b_path):
    a, b = Archive(str(a_path)), Archive(str(b_path))
    assert len(a.find_chunks()) == len(b.find_chunks())
    return {a.chunks[i][2] for i in a.find_chunks() if a.get_chunk_bytes(i) != b.get_chunk_bytes(i)}


def fighter(path, tmp):
    import io_import_punchout as importer
    import io_export_punchout as exporter
    bpy.ops.wm.read_factory_settings(use_empty=True)
    arm, obj = importer.do_import(str(path), do_anims=True)
    clips, morphs = check_import(path, arm, obj)
    art = tmp / path.stem / "art"; (art / "characters").mkdir(parents=True)
    shutil.copyfile(path.parents[1] / "hashid.bin", art / "hashid.bin")
    noop = art / "characters" / (path.stem + ".dict")
    exporter.do_export(str(path), str(noop))
    nlg_model.model_sets(Archive(str(noop)))
    changed = chunk_diff(path, noop)
    assert changed <= GEOMETRY, ["%04X" % t for t in changed - GEOMETRY]   # rig, clips, side tables untouched
    share = normals_preserved(path, noop)
    assert share > 0.99, share
    src_m, out_m = morph_records(path), morph_records(noop)
    assert src_m == out_m, ("unedited export changed morph records", len(src_m - out_m), len(out_m - src_m))
    # Edited: move one joint and one vertex. Bind translation and node offset follow; clips stay.
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones["bip01 r upperarm"]
    eb.head.z += 0.05; eb.tail.z += 0.05
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.data.vertices[0].co.z += 0.01
    edited = tmp / path.stem / "edited" / "art" / "characters" / (path.stem + ".dict")
    exporter.do_export(str(path), str(edited))
    assert chunk_diff(noop, edited) & {0xB00A, 0x8010} == {0xB00A, 0x8010}
    assert not {t for t in chunk_diff(path, edited) if 0x7000 <= t < 0x8000}, "local joint edit rewrote clips"
    # The exported archive imports again with every clip in the same Blender.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    arm2, obj2 = importer.do_import(str(noop), do_anims=True)
    assert check_import(noop, arm2, obj2)[0] == clips
    return path.stem, clips, morphs, round(share, 4)


def main():
    boot.register()
    try:
        assert not boot._last_error, boot._last_error
        root = fixtures.fixture_root()
        if not root:
            print("BLENDER_FIGHTER_ROUNDTRIP_PASS", bpy.app.version_string, "skipped (set PO_FIXTURE_ROOT)"); return
        paths = [root / "characters/bearhugger.dict", root / "characters/glassjoe.dict", root / "characters/referee.dict"]
        import io_punchout_ui as ui
        for path in paths:      # Automatic import must take the fighter path, never loose model sets
            assert ui.detect_mode(str(path))[0] == "FIGHTER", path
        for prop in ("characters/ropes.dict", "characters/vs_microphone.dict"):
            if (root / prop).is_file(): assert ui.detect_mode(str(root / prop))[0] == "MODEL_SETS", prop
        results = []
        with tempfile.TemporaryDirectory(prefix="po-fighter-") as tmp:
            for path in paths:
                results.append(fighter(Path(path), Path(tmp)))
        for r in results: print(*r)
        print("BLENDER_FIGHTER_ROUNDTRIP_PASS", bpy.app.version_string, "fighters", len(results),
              "clips", sum(r[1] for r in results), "morph strips", sum(r[2] for r in results))
    finally:
        boot.unregister()


main()
