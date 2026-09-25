"""Pure parts of the Blender plug-in: scene metadata schema, migration, panel profiles,
located refusals, hashid.bin lookup and the plain-language change review. No bpy."""
from pathlib import Path
import glob
import json
import os
import re
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
import nlg_hash
import po_errors
import po_metadata as meta
from test_model import model_set_payloads, write_archive


def obj(name, type_="MESH", roots=(), **props):
    return {"name": name, "type": type_, "props": props, "roots": list(roots)}


def legacy_snapshot(src="C:/dump/characters/prop.dict"):
    """What a version-0 scene (only scattered keys) looked like, one asset of every kind."""
    summary = {"schema": 1, "path": src, "sha256": ["a", "b"], "editable": True, "limitation": None,
               "sections": [{"index": 0, "model_sets": [{"index": 0, "nodes": 2, "meshes": 2, "transforms": 2,
                                                          "skinned": False, "bones": 0}],
                             "textures": 1, "texture_note": None, "rigs": 0, "animation_clips": 0,
                             "animation_issues": [], "unowned_chunks": 1}]}
    mesh_meta = {"schema": 1, "source": {"path": src, "sha256": ["a", "b"], "section": 0}, "model_set": 0}
    nis = "C:/dump/NIS/intro.dict"; fx = "C:/dump/effects/fx.dict"; fighter = "C:/dump/characters/glassjoe.dict"
    return {
        "scene": {"po_export_source_dict": src, "po_cinematic_source": nis, "po_effect_source": fx,
                  "po_workshop_source": fighter, "po_workshop_output": "C:/work/blender/abc/returned.dict",
                  "po_workshop_mode": "fighter", "po_material_state": "HURT"},
        "collections": [
            {"name": "PO prop", "props": {"po_asset_document": json.dumps(summary)}},
            {"name": "intro · cinematic", "props": {"po_cinematic_root": json.dumps(
                {"schema": 1, "source": nis, "sha256": ["c", "d"], "session": "s", "paths": [], "cameras": 1})}},
            {"name": "PO Effects fx", "props": {"po_effect_document": json.dumps({"schema": 1, "source": fx, "sha256": ["e", "f"]})}}],
        "objects": [
            obj("a:mesh", roots=["PO prop"], po_asset=json.dumps(mesh_meta), po_source_dict=src),
            obj("b:mesh", roots=["PO prop"], po_asset=json.dumps(mesh_meta), po_source_dict=src),
            obj("Skeleton 0", "ARMATURE", roots=["PO prop"], po_skeleton=json.dumps({"model_set": 0})),
            obj("glassjoe", "ARMATURE", po_source_dict=fighter),
            obj("PO_Character", po_source_dict=fighter, po_mesh_tris=[4, 5]),
            obj("S0 path", roots=["intro · cinematic"], po_cinematic_path=json.dumps({"source": nis, "sha256": ["c", "d"]})),
            obj("S0 camera", "CAMERA", roots=["intro · cinematic"], po_cinematic_view=json.dumps({"source": nis})),
            obj("Group", "EMPTY", roots=["PO Effects fx"], po_effect=json.dumps({"role": "group"}), po_effect_source=fx),
            obj("Nodes rig", "ARMATURE", roots=["PO Animation idle"], po_animation=json.dumps(
                {"schema": 1, "source": "C:/dump/characters/ropes.dict", "sha256": ["g", "h"]})),
            obj("Cube", props={})],
        "materials": [{"name": "fighter skin", "props": {"po_material": True, "po_record": "00", "po_fp_ramp": "x"}},
                      {"name": "shader @0", "props": {"po_material": json.dumps({"shader": 1}), "po_field_alpha": 0.5}},
                      {"name": "PO_Outline", "props": {"po_outline": True}}],
        "images": [{"name": "tex", "props": {"po_texture": "{}"}}],
    }


class Errors(unittest.TestCase):
    def test_nested_context_names_every_part_and_keeps_type(self):
        class CodecError(ValueError): pass
        with self.assertRaises(CodecError) as caught:
            with po_errors.context(archive="C:/dump/characters/glassjoe.dict", section=0):
                with po_errors.context(model_set=1, mesh="Body:skin"):
                    raise po_errors.located(CodecError("skin weights changed"), field="vertex groups")
        text = str(caught.exception)
        self.assertEqual(text, "glassjoe.dict · section 0 · model set 1 · mesh 'Body:skin' · field vertex groups: skin weights changed")
        reason, where = po_errors.parts(caught.exception)
        self.assertEqual(reason, "skin weights changed")
        self.assertEqual(set(where), set(po_errors.PARTS))
        rows = po_errors.lines(po_errors.as_record(caught.exception))
        self.assertEqual(rows[0], "skin weights changed")
        self.assertEqual(len(rows), 1 + len(po_errors.PARTS))

    def test_inner_parts_win_and_missing_parts_are_explicit(self):
        err = po_errors.Refusal("locked", mesh="inner")
        with self.assertRaises(po_errors.Refusal) as caught:
            with po_errors.context(mesh="outer", archive="x.dict"):
                raise err
        self.assertIn("mesh 'inner'", str(caught.exception))
        rows = po_errors.lines(po_errors.as_record(caught.exception))
        self.assertIn("Section: -", rows)
        self.assertIn("Archive: x.dict", rows)
        with self.assertRaises(TypeError):
            po_errors.describe({"bone": 1})
        # Non-ValueErrors pass through untouched.
        with self.assertRaises(KeyError):
            with po_errors.context(archive="x.dict"):
                raise KeyError("k")


class Hashid(unittest.TestCase):
    def test_walks_any_parent_without_folder_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp) / "my dump" / "whatever" / "nested" / "deeper"
            deep.mkdir(parents=True)
            archive = deep / "thing.dict"
            found, searched = nlg_hash.find_hashid_bin(archive)
            self.assertIsNone(found)
            self.assertGreaterEqual(len(searched), 4)
            message = nlg_hash.missing_hashid_message(str(archive), searched)
            self.assertIn(str(archive), message); self.assertIn("hashid.bin", message)
            (Path(tmp) / "my dump" / "hashid.bin").write_bytes(b"\0\0\0\0")
            found, searched = nlg_hash.find_hashid_bin(archive)
            self.assertEqual(Path(found), Path(tmp) / "my dump" / "hashid.bin")
            self.assertEqual(len(searched), 4)

    def test_blender_side_has_no_art_characters_guessing(self):
        for name in ("io_import_punchout.py", "io_export_punchout.py", "io_punchout_asset.py", "po_tools_bootstrap.py"):
            text = (HERE.parent / "blender" / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\.\./art|\"art\", \"hashid|art/characters|name\.lower\(\) == \"art\"", name)


class Metadata(unittest.TestCase):

    def test_version_zero_scene_migrates_to_one_document(self):
        doc = meta.migrate(legacy_snapshot())
        self.assertEqual((doc["format"], doc["version"]), ("po-scene", 1))
        kinds = sorted(a["kind"] for a in doc["assets"])
        self.assertEqual(kinds, ["animation", "cinematic", "effects", "fighter", "model_sets"])
        prop = next(a for a in doc["assets"] if a["kind"] == "model_sets")
        self.assertEqual(prop["objects"], 3)                   # two meshes plus the skeleton via its root
        self.assertEqual(prop["roles"], {"mesh": 2, "skeleton": 1})
        self.assertEqual(prop["content"]["model_sets"][0]["meshes"], 2)
        fighter = next(a for a in doc["assets"] if a["kind"] == "fighter")
        self.assertEqual(fighter["roles"], {"armature": 1, "combined mesh": 1})
        self.assertEqual(doc["workshop"]["mode"], "fighter")
        self.assertEqual(Path(doc["workshop"]["session"]).name, "abc")
        self.assertEqual(meta.workshop_asset(doc)["kind"], "fighter")
        self.assertEqual(doc["preferences"]["material_state"], "HURT")
        self.assertEqual(doc["active"], prop["id"])            # po_export_source_dict hint
        self.assertIn("po_asset", doc["legacy_keys"]); self.assertIn("po_fp_*", doc["legacy_keys"])
        snap = legacy_snapshot()
        cube = next(o for o in snap["objects"] if o["name"] == "Cube")
        self.assertIsNone(meta.asset_for_object(doc, cube))
        skeleton = next(o for o in snap["objects"] if o["name"] == "Skeleton 0")
        self.assertEqual(meta.asset_for_object(doc, skeleton)["id"], prop["id"])

    def test_stored_document_round_trips_and_survives_edits(self):
        snap = legacy_snapshot()
        doc = meta.migrate(snap)
        prop = next(a for a in doc["assets"] if a["kind"] == "model_sets")
        prop["content"] = dict(prop["content"], source="archive", cameras=0)
        prop["import"] = {"mode": "model_sets", "blender": "3.6.1"}
        doc["last_refusal"] = {"reason": "x"}
        snap["scene"]["po_metadata"] = meta.dump(doc)
        snap["objects"] = [o for o in snap["objects"] if not o["props"].get("po_cinematic_path")
                           and not o["props"].get("po_cinematic_view")]
        snap["collections"] = [c for c in snap["collections"] if "po_cinematic_root" not in c["props"]]
        again = meta.migrate(snap)
        self.assertEqual(again["last_refusal"], {"reason": "x"})
        self.assertNotIn("cinematic", [a["kind"] for a in again["assets"]])   # deleted import drops out
        kept = meta.find_asset(again, prop["id"])
        self.assertEqual(kept["import"]["blender"], "3.6.1")
        self.assertEqual(kept["content"]["source"], "archive")
        self.assertEqual(again["legacy_keys"], doc["legacy_keys"])
        self.assertEqual(meta.load(meta.dump(again)), again)

    def test_newer_or_malformed_documents_are_refused(self):
        for bad in ('{"format": "po-scene", "version": 2}', '{"format": "other", "version": 1}',
                    '{"format": "po-scene", "version": 0}', "not json",
                    '{"format": "po-scene", "version": 1, "assets": [{"kind": "wat"}]}'):
            with self.assertRaises(meta.MetadataError):
                meta.load(bad)
        self.assertIsNone(meta.load(None))

    def test_material_roles_distinguish_the_two_po_material_meanings(self):
        mats = {m["name"]: meta.material_role(m) for m in legacy_snapshot()["materials"]}
        self.assertEqual(mats, {"fighter skin": "fighter", "shader @0": "model_sets", "PO_Outline": "outline"})
        self.assertEqual(meta.generic_material(legacy_snapshot()["materials"][1]), {"shader": 1})

    def test_profiles_offer_only_applicable_controls(self):
        static = {"kind": "model_sets", "content": {"sections": 1, "editable": True, "textures": 2, "rigs": 0,
                  "clips": 0, "cameras": 0, "effect_groups": 0,
                  "model_sets": [{"meshes": 3, "skinned": False, "bones": 0}]}}
        p = meta.profile(static)
        self.assertTrue(p["meshes"] and p["materials"] and p["exportable"])
        self.assertFalse(p["rig"] or p["fighter_controls"] or p["timeline"] or p["cameras"] or p["effects"] or p["behavior"])
        skinned = {"kind": "model_sets", "content": dict(static["content"], rigs=1, clips=13,
                   model_sets=[{"meshes": 34, "skinned": True, "bones": 83}], cameras=4)}
        p = meta.profile(skinned)
        self.assertTrue(p["rig"] and p["skinned"] and p["clips"])
        self.assertFalse(p["fighter_controls"] or p["cameras"] or p["timeline"])    # sampled cameras: summary only
        fighter = meta.profile({"kind": "fighter", "content": {"cameras": 2, "sections": 1}})
        self.assertTrue(fighter["fighter_controls"] and fighter["rig"] and fighter["behavior"])
        self.assertFalse(fighter["timeline"] or fighter["cameras"])
        nis = {"kind": "model_sets", "content": dict(static["content"], sections=3, cameras=2, editable=False,
                                                    limitation="read-only")}
        p = meta.profile(nis)
        self.assertTrue(p["cameras"]); self.assertFalse(p["timeline"] or p["exportable"])
        self.assertEqual(p["export_note"], "read-only")
        self.assertTrue(meta.profile({"kind": "cinematic", "content": None})["timeline"])
        fx = meta.profile({"kind": "model_sets", "content": dict(static["content"], effect_groups=156)})
        self.assertTrue(fx["effects"]); self.assertFalse(fx["effect_graph"])
        self.assertTrue(meta.profile({"kind": "effects", "content": None})["effect_graph"])
        defs = meta.profile({"kind": "model_sets", "content": {"sections": 1, "model_sets": [],
                                                             "definitions": {"scripts": 1, "combos": 0}}})
        self.assertTrue(defs["behavior"]); self.assertFalse(defs["meshes"] or defs["materials"] or defs["exportable"])
        self.assertIn("nlg_behavior", defs["export_note"])
        self.assertFalse(meta.profile(None)["exportable"])

    def test_capability_families_exist_in_registry(self):
        import po_capability as capability
        known = {c.family for c in capability.REGISTRY}
        for kind in meta.KINDS:
            asset = {"kind": kind, "content": {"sections": 2, "textures": 1, "rigs": 1, "clips": 1, "cameras": 1,
                                               "effect_groups": 1, "definitions": {"scripts": 1, "combos": 1},
                                               "model_sets": [{"meshes": 1, "skinned": True, "bones": 2}]}}
            families = meta.capability_families(asset)
            self.assertTrue(set(families) <= known, set(families) - known)
            rows = meta.capabilities(asset)
            self.assertEqual(len(rows), len(families))
            self.assertTrue(all("runtime" in status for _, status, _ in rows))

    def test_content_from_a_synthetic_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_archive(tmp, [(0x5002, b"\x01" * 12)] + model_set_payloads())
            c = meta.content_from_archive(path)
        self.assertEqual((c["source"], c["sections"], c["editable"]), ("archive", 1, True))
        self.assertEqual(len(c["model_sets"]), 1)
        self.assertEqual((c["cameras"], c["effect_groups"]), (0, 0))
        self.assertTrue(meta.profile({"kind": "model_sets", "content": c})["meshes"])

    def test_every_property_written_by_the_plugin_is_registered(self):
        sources = [*glob.glob(str(HERE.parent / "blender" / "io_*.py")), str(HERE.parent / "blender" / "po_shader.py"),
                   str(HERE.parent / "blender" / "po_tools_bootstrap.py")]
        found = set()
        for f in sources:
            found |= set(re.findall(r"[\"'](po_[a-z0-9_]+)[\"']", Path(f).read_text(encoding="utf-8")))
        found -= {"po_shader"}   # module name, not a property
        self.assertEqual(sorted(n for n in found if meta.key_for(n) is None), [])


class Review(unittest.TestCase):
    LINES = ["model set 0, mesh 1: 3 vertex positions changed (12 bytes)",
             "model set 0, mesh 1: 2 UVs changed (8 bytes)",
             "model set 0: transform 1 changed (places 1 mesh) (4 bytes)",
             "culling bounds of 0x6101 chunk 9 follow the edited geometry (8 bytes)",
             "texture 3: pixels and mip chain changed (512 bytes)",
             "model set 0: material @0 tint (2 mesh users) (12 bytes)",
             "section 1: camera cam_a: position sample 2 (4 bytes)",
             "section 1: camera cam_a: position sample 5 (4 bytes)",
             "idle: node 3 rotation key 0 changed (8 bytes)", "idle: node 4 rotation key 1 changed (8 bytes)",
             "effect emitter 12: texture 0000ABCD -> 0000BEEF (4 bytes)",
             "model set 0, mesh 2: rebuild topology (5 vertices, 3 faces)", "something new"]

    def test_groups_every_line_in_plain_language(self):
        headline, sections = meta.review(self.LINES, "model_sets", "C:/dump/prop.dict")
        names = [h for h, _ in sections]
        self.assertEqual(names, ["Geometry", "Placement", "Textures", "Materials", "Animation keys",
                                 "Camera samples", "Effects", "Other", "Stays locked"])
        rows = dict(sections)
        self.assertEqual(rows["Animation keys"], ["idle: 2 keys changed on 2 nodes (rotation)"])
        self.assertEqual(rows["Camera samples"], ["camera cam_a: 2 position samples moved (first 2, last 5)"])
        self.assertIn("prop.dict", headline)
        self.assertEqual(len(rows["Geometry"]), 4)

    def test_no_change_and_fighter_reviews(self):
        headline, sections = meta.review(["No changes: the export will be byte-identical to the source."], "cinematic")
        self.assertTrue(headline.startswith("No changes"))
        self.assertEqual([h for h, _ in sections], ["Stays locked"])
        headline, sections = meta.fighter_review({"meshes": ["PO_Character"], "vertices": 120, "source_slots": 42,
                                                 "changed_materials": ["skin"], "reshaped_bones": []}, "glassjoe.dict")
        self.assertIn("1 edited material, 0 reshaped bones", headline)
        self.assertEqual([h for h, _ in sections], ["Geometry", "Materials", "Stays locked"])

    def test_audit_lines_always_say_runtime_unverified(self):
        rows = meta.audit_lines({"audit": {"changed_bytes": 4, "patches": 1}, "deterministic": True,
                                 "validation": {"passed": True, "warnings": []}})
        self.assertEqual(rows[0], "4 bytes changed in 1 patch; everything else matches the source.")
        self.assertIn("Validation: passed", rows)
        self.assertTrue(rows[-1].startswith("Runtime unverified"))
        rows = meta.audit_lines({"audit": {"audit_mode": "chunk comparison", "changed_bytes": 10, "patches": 0,
                                           "changed_chunks": [{"chunk": 4, "type": "0xB601", "old_bytes": 8, "new_bytes": 16, "resized": True}]}})
        self.assertIn("differ in 1 chunk", rows[0]); self.assertIn("resized", rows[1])

    def test_chunk_audit_of_an_untouched_copy_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write_archive(tmp, model_set_payloads())
            (Path(tmp) / "out").mkdir()
            copy = Path(tmp) / "out" / "prop.dict"
            copy.write_bytes(source.read_bytes()); copy.with_suffix(".data").write_bytes(source.with_suffix(".data").read_bytes())
            self.assertEqual(meta.chunk_audit(source, copy)["changed_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
