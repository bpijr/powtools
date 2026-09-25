"""Blender 3.6/5.x effect inspection and bounded export smoke.

Always synthetic; PO_FIXTURE_ROOT opts into every effects archive, frontend/shared/fighters.
No assets leave a private temporary directory. Runtime rendering is not claimed.
"""
from pathlib import Path
import json
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import bpy
import io_punchout_effects as effects
from nlg_effect import Effects, EffectFormatError
import po_archive
import fixtures
from test_effects import write_effect


def refused(fn, phrase):
    try: fn()
    except EffectFormatError as ex:
        assert phrase.lower() in str(ex).lower(), str(ex)
    else: raise AssertionError("Unsupported edit was not refused: " + phrase)


def roundtrip(source, out, label, negative=False, dependencies=()):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    doc, root, emitters = effects.import_effects(source, dependencies)
    assert emitters
    ps = effects.collect_effect_patches(source); assert not ps.patches
    noop = out / (label + "_noop.dict")
    result = effects.export_effects(source, noop)
    assert result["deterministic"] and result["audit"]["changed_bytes"] == 0
    assert noop.read_bytes() == Path(source).read_bytes()
    assert noop.with_suffix(".data").read_bytes() == Path(source).with_suffix(".data").read_bytes()
    fx = Effects(doc.sections[0].archive)
    selected = None
    for obj in emitters:
        spec = json.loads(obj["po_effect"])
        choices = [t for t in fx.texture_candidates(spec["chunk"]) if t.hash != spec["texture_hash"]]
        if choices: selected = obj, spec, choices[0]; break
    changed = 0
    if selected:
        obj, spec, texture = selected
        obj["po_effect_texture"] = f"{texture.hash:08X}"
        edit = out / (label + "_edit.dict")
        result = effects.export_effects(source, edit)
        changed = result["audit"]["changed_bytes"]
        assert 0 < changed <= 4
        parsed = Effects(po_archive.load_archive(edit)); assert parsed.emitter(spec["chunk"]).texture_hash == texture.hash
        original = doc.sections[0].archive; result_archive = po_archive.load_archive(edit)
        for ri in original.find_chunks():
            old, new = original.get_chunk_bytes(ri), result_archive.get_chunk_bytes(ri)
            if ri == spec["chunk"]: assert old[:56] == new[:56] and old[60:] == new[60:]
            else: assert old == new
        obj["po_effect_texture"] = f"{spec['texture_hash']:08X}"
    if negative:
        obj = emitters[0]; location = obj.location.copy()
        obj.location.x += 1
        refused(lambda: effects.collect_effect_patches(source), "display markers")
        obj.location = location
        value = obj["po_effect_texture"]; obj["po_effect_texture"] = "00000999"
        refused(lambda: effects.collect_effect_patches(source), "Target")
        obj["po_effect_texture"] = value
        duplicate = obj.copy(); root.objects.link(duplicate)
        refused(lambda: effects.collect_effect_patches(source), "duplicated")
        bpy.data.objects.remove(duplicate, do_unlink=True)
        metadata = root["po_effect_document"]
        root["po_effect_document"] = metadata.replace('"schema": 1', '"schema": 2')
        refused(lambda: effects.collect_effect_patches(source), "fingerprint")
        root["po_effect_document"] = metadata
        refused(lambda: effects.export_effects(source, noop), "existing")
        bpy.data.objects.remove(obj, do_unlink=True)
        refused(lambda: effects.collect_effect_patches(source), "missing")
    return len(emitters), changed


effects.register()
try:
    with tempfile.TemporaryDirectory(prefix="po-effects-blender-") as tmp:
        out = Path(tmp); (out / "src").mkdir(); source = write_effect(out / "src", groups=2, binding_sets=2)
        results = [("synthetic", *roundtrip(source, out, "synthetic", negative=True))]
        art = fixtures.fixture_root()
        if art is None: print("private fixtures skipped (set PO_FIXTURE_ROOT)")
        else:
            for source in sorted((art / "effects").glob("*.dict")):
                if not Effects(po_archive.load_archive(source)).groups: continue
                results.append((source.name, *roundtrip(source, out, source.stem,
                               dependencies=[art / "effects/effects.dict"] if source.name != "effects.dict" else [])))
        for row in results: print("effect graph:", *row)
        print("BLENDER_EFFECTS_ROUNDTRIP_PASS", bpy.app.version_string, "archives", len(results))
finally: effects.unregister()
