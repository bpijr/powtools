"""Headless Blender import/export round-trip for generic assets.

    blender --background --factory-startup --python-exit-code 1 --python this_file

Always runs a synthetic two-node archive. With PO_FIXTURE_ROOT pointing at a dump that
matches tests/private_fixtures.json it also runs every model family in the fixture matrix.
For each archive: import, export untouched (must be byte-identical), then move one vertex,
change one UV and, for a static set, move one object; export again and check that the new
values decode back and nothing outside the recorded patch ranges changed. Outputs go to a
temporary folder that is deleted afterwards.
"""
from pathlib import Path
import json
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import bpy
from mathutils import Matrix

import io_punchout_asset as asset
import nlg_model
from nlg_pack import Archive
import fixtures

FAMILIES = ["characters/crowd_light_1.dict", "characters/vs_microphone.dict", "characters/minorbelt.dict",
            "characters/ropes.dict", "characters/ropes_lp.dict", "environments/feminor/gameworld.dict",
            "environments/minorcircuit/gameworld.dict", "effects/effects.dict", "characters/referee.dict",
            "characters/doc.dict"]


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def pick(objects, static):
    for obj in objects:
        meta = json.loads(obj["po_asset"])
        if static is None or static != meta["skeleton"]["skinned"]:
            if obj.data.uv_layers.get("UV") is not None and "texcoord0" in meta["editable"]:
                return obj, meta
    return None, None


def roundtrip(source, out_dir, label):
    reset()
    doc, root, objects = asset.import_asset(str(source), load_textures=False)
    assert objects, f"{label}: nothing imported"
    noop = out_dir / (label + "_noop.dict")
    report, review = asset.export_asset(str(source), str(noop))
    assert noop.read_bytes() == Path(source).read_bytes(), f"{label}: dictionary changed on no-op export"
    assert noop.with_suffix(".data").read_bytes() == Path(source).with_suffix(".data").read_bytes(), \
        f"{label}: no-op export changed {report['audit']['changed_bytes']} byte(s): {review[:3]}"

    obj, meta = pick(objects, static=None)
    assert obj is not None, f"{label}: no mesh with editable UV0"
    v = obj.data.vertices[0]; before = tuple(v.co)
    v.co = (before[0] + 0.125, before[1], before[2])
    layer = obj.data.uv_layers["UV"].data
    target = obj.data.loops[0].vertex_index
    new_uv = (0.25, 0.75)
    for loop in obj.data.loops:
        if loop.vertex_index == target:
            layer[loop.index].uv = new_uv
    moved_obj, moved_meta = pick(objects, static=True)
    if moved_obj is not None:
        moved_obj.matrix_world = Matrix.Translation((0.5, 0, 0)) @ moved_obj.matrix_world
        shared = [o for o in objects if json.loads(o["po_asset"])["model_set"] == moved_meta["model_set"]
                  and json.loads(o["po_asset"])["transform"]["index"] == moved_meta["transform"]["index"] and o != moved_obj]
        for o in shared:   # objects sharing a transform must move together
            o.matrix_world = Matrix.Translation((0.5, 0, 0)) @ o.matrix_world
    bpy.context.view_layer.update()
    edited = out_dir / (label + "_edit.dict")
    report, review = asset.export_asset(str(source), str(edited))
    ms = nlg_model.model_sets(Archive(str(edited)))[meta["model_set"]]
    mesh = ms.meshes[meta["mesh"]["index"]]
    got = mesh.attribute("position").values()[0]
    assert abs(got[0] - (before[0] + 0.125)) < 1e-6, f"{label}: moved vertex not exported"
    got_uv = mesh.attribute("texcoord").values()[target]
    assert abs(got_uv[0] - 0.25) < 1e-3 and abs(got_uv[1] - 0.25) < 1e-3, f"{label}: UV not exported {got_uv}"
    if moved_obj is not None:
        t = nlg_model.model_sets(Archive(str(edited)))[moved_meta["model_set"]].transforms[moved_meta["transform"]["index"]]
        assert abs(t[12] - (moved_meta["transform"]["matrix"][12] + 0.5)) < 1e-5, f"{label}: transform not exported"
    assert report["audit"]["changed_bytes"] > 0 and report["deterministic"]
    return len(objects), report["audit"]["changed_bytes"], review


def main():
    asset.register()
    results = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            from test_model import model_set_payloads, write_archive
            (tmp / "src").mkdir()
            synthetic = write_archive(tmp / "src", [(0x5002, b"\x01" * 12)] + model_set_payloads())
            results.append(("synthetic",) + roundtrip(synthetic, tmp, "synthetic")[:2])
            root = fixtures.fixture_root()
            if root is None:
                print("private fixtures skipped (set PO_FIXTURE_ROOT)")
            else:
                for rel in FAMILIES:
                    label = rel.replace("/", "_").removesuffix(".dict")
                    results.append((rel,) + roundtrip(root / rel, tmp, label)[:2])
    finally:
        asset.unregister()
    for name, objects, changed in results:
        print(f"  {name}: {objects} objects, no-op identical, edit changed {changed} byte(s)")
    print("BLENDER_ASSET_ROUNDTRIP_PASS", bpy.app.version_string, len(results))


main()
