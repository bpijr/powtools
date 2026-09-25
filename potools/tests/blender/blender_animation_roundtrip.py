"""Blender 3.6/5.x source-node action no-op, key edits, and compatibility/refusal smoke.

Uses synthetic bytes always; opt-in private fixture families require PO_FIXTURE_ROOT.
No game data or generated game files are written into the repository.
"""
import json
from pathlib import Path
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import bpy
import io_punchout_animation as io
import io_punchout_asset as asset
import nlg_animation as codec
import nlg_asset
from test_animation import write_animation
import fixtures


def refused(fn):
    try: fn()
    except codec.AnimationError: return
    raise AssertionError("Unsafe edit was not refused")


def roundtrip(source, out, label, clip_index=0, section_index=0):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    doc, obj, action = io.import_clip(source, clip_index, section_index)
    meta = json.loads(obj["po_animation"])
    clip = doc.sections[section_index].animations.clips[clip_index]
    assert len(obj.data.bones) == clip.node_count
    assert any(b.parent for b in obj.data.bones)
    curves = list(io._curves(obj, action))
    if not doc.editable:
        refused(lambda: io.collect_patch_set(obj))
        return (label, "read-only cinematic", len(curves))
    path = out / (label + "_noop.dict")
    report = io.export_clip(obj, path)
    assert report["audit"]["changed_bytes"] == 0
    assert path.read_bytes() == Path(source).read_bytes()
    assert path.with_suffix(".data").read_bytes() == Path(source).with_suffix(".data").read_bytes()
    if label == "synthetic":
        saved = out / "synthetic.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(saved))
        bpy.ops.wm.open_mainfile(filepath=str(saved))
        obj = next(o for o in bpy.data.objects if "po_animation" in o)
        action = obj.animation_data.action
        curves = list(io._curves(obj, action))
        assert not io.collect_patch_set(obj)[1].patches
    channel = next(c for c in meta["channels"] if c["type"] == 0x7102 and c["component"] == 0)
    curve = next(c for c in curves if c.data_path == channel["path"] and c.array_index == 0)
    curve.keyframe_points[0].co[1] += 0.125
    curve.update()
    path = out / (label + "_edit.dict")
    report = io.export_clip(obj, path)
    assert 0 < report["audit"]["changed_bytes"] <= 4
    reloaded = nlg_asset.AssetDocument(path).sections[0].animations.clips[clip_index]
    old = clip.tracks[(channel["node"], 0x7102)].values()[0]
    new = reloaded.tracks[(channel["node"], 0x7102)].values()[0]
    assert abs(new[0] - old[0] - 0.125) < 1e-5
    assert new[1:] == old[1:]
    if label == "synthetic":
        for type_, component, value in ((0x7103, 0, 1.25), (0x7101, 3, 0.1)):
            ch = next(c for c in meta["channels"] if c["type"] == type_ and c["node"] == 1 and c["component"] == component)
            fc = next(c for c in curves if c.data_path == ch["path"] and c.array_index == component)
            fc.keyframe_points[0].co[1] = value; fc.update()
        extra = out / "synthetic_scale_rotation.dict"
        io.export_clip(obj, extra)
        edited = nlg_asset.AssetDocument(extra).sections[0].animations.clips[0]
        assert edited.tracks[(1, 0x7103)].values() == [(1.25, 1, 1)]
        assert edited.tracks[(1, 0x7101)].values()[0][2] > 0.09
        refused(lambda: io.export_clip(obj, extra))
    curve.keyframe_points[0].co[0] = 0
    refused(lambda: io.collect_patch_set(obj))
    curve.keyframe_points[0].co[0] = 1
    obj.location.x += 1
    bpy.context.view_layer.update()
    refused(lambda: io.collect_patch_set(obj))
    return (label, "no-op exact and edited key audited", len(curves))


def main():
    io.register()
    results = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); src = tmp / "src"; src.mkdir()
            synthetic = write_animation(src)
            results.append(roundtrip(synthetic, tmp, "synthetic"))
            # Explicit mismatched rig signature must fail before any scene import occurs.
            bad = tmp / "bad"; bad.mkdir(); mismatch = write_animation(bad)
            from nlg_pack import Archive
            a = Archive(str(mismatch)); ri = 0; off = a.chunks[ri][4]
            a.blocks[0][off + 12:off + 16] = bytes(4)
            mismatch.with_suffix(".data").write_bytes(a.build_data())
            refused(lambda: io.import_clip(synthetic, rig_source=mismatch))
            root = fixtures.fixture_root()
            if root is None:
                print("Private fixtures skipped (set PO_FIXTURE_ROOT)")
            else:
                for rel in ("characters/minorbelt.dict", "characters/ropes.dict", "characters/referee.dict",
                            "characters/doc.dict", "characters/bearhugger.dict", "environments/feminor/gameworld.dict",
                            "environments/minorcircuit/gameworld.dict"):
                    path = root / rel
                    label = rel.replace("/", "_").removesuffix(".dict")
                    results.append(roundtrip(path, tmp, label))
                # Bind parenting is a generic-asset hook and must preserve all no-op mesh bytes.
                bpy.ops.wm.read_factory_settings(use_empty=True)
                path = root / "characters/minorbelt.dict"
                doc, col, objects = asset.import_asset(str(path), load_textures=False)
                arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
                assert arms and any(b.parent for b in arms[0].data.bones)
                report, _ = asset.export_asset(str(path), str(tmp / "bind_noop.dict"))
                assert report["audit"]["changed_bytes"] == 0
    finally:
        io.unregister()
    for item in results: print(*item)
    print("BLENDER_ANIMATION_ROUNDTRIP_PASS", bpy.app.version_string, len(results))


main()
