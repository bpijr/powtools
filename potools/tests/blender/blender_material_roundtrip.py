"""Headless Blender material controls, provenance refusals, and audited rebuilds.

Run in Blender 3.6 and 5.2. Always tests all 12 synthetic layouts. Opt-in private
fixtures cover all 12 layouts; game output is temporary and never committed.
"""
from pathlib import Path
import json
import struct
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import bpy
import io_punchout_asset as asset
import io_punchout_material as material
import nlg_material_layout as layouts
import nlg_model
import po_archive
import fixtures
from test_material_layout import material_payloads
from test_model import write_archive


def refused(fn, text):
    try: fn()
    except ValueError: return
    raise AssertionError(text)


def run(source, folder, label, previews):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    doc, root, objects = asset.import_asset(str(source), load_textures=previews)
    assert objects, label
    materials = [obj.data.materials[0] for obj in objects]
    assert all(mat.get("po_material") for mat in materials), "Material state missing when previews are disabled"
    if previews:
        # Game previews: straight sRGB like the Wii (no Filmic/AgX), shader-specific graphs.
        assert bpy.context.scene.view_settings.view_transform == "Standard"
        for mat in materials:
            info = json.loads(mat["po_material"])
            if not info["limitation"]:
                assert mat["po_preview_note"].startswith("Game preview"), (label, mat["po_preview_note"])
    ps, _ = asset.collect_patch_set(str(source)); assert not ps.patches
    dest = folder / (label + "_noop.dict")
    report, _ = asset.export_asset(str(source), str(dest))
    assert (dest.read_bytes(), dest.with_suffix(".data").read_bytes()) == (source.read_bytes(), source.with_suffix(".data").read_bytes())
    assert report["deterministic"] and report["audit"]["changed_bytes"] == 0
    changed = set()
    for obj in objects:
        mat = obj.data.materials[0]; info = json.loads(mat["po_material"])
        if info["shader"] in changed or info["limitation"]: continue
        rec = layouts.find_record(doc.sections[0].archive, info["chunk"], info["offset"])
        nodes = [n for n in mat.node_tree.nodes if n.name.startswith("PO_Input_")]
        assert len(nodes) == len(rec.layout.slots)
        assert any(n.type == "EMISSION" for n in mat.node_tree.nodes), "preview must use the unlit TEV output"
        candidates = [e for e in doc.sections[0].textures if e.hash != struct.unpack_from(">I", rec.raw)[0]]
        if not candidates: continue
        material.stage_texture(mat, 0, candidates[0].hash)
        if rec.shader == layouts.SKIN:
            mat["po_field_alpha"] = 0.25
            mat["po_field_tint"] = [0.25, 0.5, 0.75]
            mat["po_field_specular_power"] = 64.0
        ps, _ = asset.collect_patch_set(str(source))
        assert ps.patches and all(p.chunk == rec.chunk for p in ps.patches)
        allowed = [(rec.offset, rec.offset + 4)]
        if rec.shader == layouts.SKIN: allowed += [(rec.offset + f.offset, rec.offset + f.offset + f.count * 4) for f in rec.layout.fields]
        assert all(any(lo <= p.offset < p.offset + len(p.new) <= hi for lo, hi in allowed) for p in ps.patches)
        output = folder / (label + "_%08x.dict" % rec.shader)
        report, _ = asset.export_asset(str(source), str(output))
        result = po_archive.load_archive(output)
        edited = layouts.find_record(result, rec.chunk, rec.offset)
        assert struct.unpack_from(">I", edited.raw)[0] == candidates[0].hash
        assert report["deterministic"] and report["audit"]["changed_bytes"] > 0
        mat["po_material_edits"] = "{}"
        for field, value in rec.values().items():
            if value is not None: mat["po_field_" + field] = value
        assert not asset.collect_patch_set(str(source))[0].patches
        changed.add(rec.shader)
    return changed


def negative_controls(source):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    doc, _, objects = asset.import_asset(str(source), load_textures=False)
    first = objects[0].data.materials[0]; second = objects[1].data.materials[0]
    objects[0].data.materials[0] = second
    refused(lambda: asset.collect_patch_set(str(source)), "Material reassignment accepted")
    objects[0].data.materials[0] = first
    refused(lambda: material.stage_texture(first, 0, 999999), "Unknown texture key accepted")
    refused(lambda: material.stage_texture(first, 8, 100), "Extra texture input accepted")
    first["po_material_edits"] = json.dumps({"opaque_word": 3})
    refused(lambda: asset.collect_patch_set(str(source)), "Opaque material field accepted")
    first["po_material_edits"] = "{}"
    first["po_field_alpha"] = 2
    refused(lambda: asset.collect_patch_set(str(source)), "Out-of-bounds scalar accepted")
    first["po_field_alpha"] = 0.5
    info = json.loads(first["po_material"]); info["shader"] = 0
    first["po_material"] = json.dumps(info)
    refused(lambda: asset.collect_patch_set(str(source)), "Changed shader provenance accepted")


def shared_controls(source):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    doc, _, objects = asset.import_asset(str(source), load_textures=False)
    first = objects[0].data.materials[0]
    assert first == objects[1].data.materials[0], "Shared record must import as one Blender material"
    first["po_field_alpha"] = 0.25
    ps, review = asset.collect_patch_set(str(source))
    assert ps.patches and "2 mesh users" in review[0]
    objects[1].data.materials[0] = first.copy()
    objects[1].data.materials[0]["po_field_alpha"] = 0.75
    refused(lambda: asset.collect_patch_set(str(source)), "Conflicting shared material edits accepted")


def main():
    material.register(); asset.register()
    synthetic = set(); private = set(); imports = 0
    try:
        assert hasattr(bpy.types, "PO_PT_material_inputs")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); source_dir = tmp / "source"; source_dir.mkdir()
            for shader in layouts.LAYOUTS:
                source = write_archive(source_dir, material_payloads((shader, shader)), "%08X" % shader)
                synthetic.update(run(source, tmp, "%08X" % shader, True)); imports += 1
            source = write_archive(source_dir, material_payloads(), "mixed")
            negative_controls(source)
            shared = write_archive(source_dir, material_payloads((layouts.SKIN, layouts.SKIN), True), "shared")
            shared_controls(shared)
            assert synthetic == set(layouts.LAYOUTS)
            root = fixtures.fixture_root()
            if root:
                for entry in fixtures.load()["fixtures"]:
                    if entry["path"].lower().startswith("nis/"): continue
                    # The matrix also holds fixtures for other phases (behavior definitions)
                    # that carry no model sets; every layout must still be covered below.
                    if not nlg_model.model_sets(po_archive.load_archive(root / entry["path"])): continue
                    private.update(run(root / entry["path"], tmp, "private_%d" % imports, False)); imports += 1
                assert private == set(layouts.LAYOUTS), private
            else: print("Private material fixtures skipped (set PO_FIXTURE_ROOT)")
    finally:
        asset.unregister(); material.unregister()
    print("BLENDER_MATERIAL_ROUNDTRIP_PASS", bpy.app.version_string, "imports", imports,
          "synthetic layouts", len(synthetic), "private layouts", len(private))


main()
