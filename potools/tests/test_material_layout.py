"""Shader provenance and bounded material patches."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import nlg_asset
import nlg_material_layout as materials
import nlg_model
import nlg_texture
from test_model import model_set_payloads, write_archive
from helpers import texture_header
import po_archive
import fixtures


def record_bytes(shader):
    layout = materials.LAYOUTS[shader]
    raw = bytearray((i * 37 + 19) % 256 for i in range(layout.size))
    for i in range(len(layout.slots)): struct.pack_into(">II", raw, i * 8, 100, 0xFFFF0000)
    for f in layout.fields: struct.pack_into(">%df" % f.count, raw, f.offset, *([0.5] * f.count))
    return bytes(raw)


def material_payloads(shaders=(materials.SKIN, 0x55951F36), shared=False):
    payloads = dict(model_set_payloads())
    records = [record_bytes(s) for s in shaders]
    payloads[0xB016] = records[0] if shared else b"".join(records)
    meshes = bytearray(payloads[0xB004])
    for i, shader in enumerate(shaders):
        struct.pack_into(">I", meshes, i * 52 + 16, shader)
        struct.pack_into(">I", meshes, i * 52 + 36, 0 if shared else sum(len(x) for x in records[:i]))
    payloads[0xB004] = bytes(meshes)
    pixels = nlg_texture.encode_rgba32(bytes([255, 0, 0, 255]) * 16, 4, 4)
    return list(payloads.items()) + [(0xB601, texture_header(4, 4, 8, 1, 0, 100) + texture_header(4, 4, 8, 1, len(pixels), 200)),
                                    (0xB603, pixels + pixels), (0xD001, b"opaque trailing payload")]


class MaterialLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.root = self.base / "source"; self.root.mkdir()
        self.source = write_archive(self.root, material_payloads())


    def test_noop_all_layouts_are_exact(self):
        for shader in materials.LAYOUTS:
            with self.subTest(shader=hex(shader)):
                src = write_archive(self.root, material_payloads((shader, shader)), str(shader))
                doc = nlg_asset.AssetDocument(src); a = doc.sections[0].archive
                for rec in materials.records(a):
                    for ref in rec.references():
                        self.assertEqual(materials.field_patches(rec, "texture_%d" % ref["slot"], ref["hash"]), [])
                    for name, value in rec.values().items():
                        self.assertEqual(materials.field_patches(rec, name, value), [])
                ps = nlg_asset.PatchSet(doc.source_hashes)
                report = nlg_asset.write_rebuild(src, ps, self.base / (str(shader) + ".dict"))
                self.assertEqual(report["audit"]["changed_bytes"], 0)
                self.assertTrue(report["deterministic"])

    def test_every_layout_texture_edit_is_bounded_and_unknown_bytes_survive(self):
        for shader in materials.LAYOUTS:
            src = write_archive(self.root, material_payloads((shader, shader)), str(shader))
            doc = nlg_asset.AssetDocument(src); a = doc.sections[0].archive
            rec = materials.records(a)[1]
            patches = materials.patches(a, rec.chunk, rec.offset, "texture_0", 200)
            self.assertTrue(patches)
            for p in patches: self.assertTrue(rec.offset <= p.offset < p.offset + len(p.new) <= rec.offset + 4)
            ps = nlg_asset.PatchSet(doc.source_hashes, patches)
            target = self.base / (str(shader) + ".dict")
            report = nlg_asset.write_rebuild(src, nlg_asset.PatchSet.from_dict(ps.as_dict()), target)
            reread = po_archive.load_archive(target)
            expected = bytearray(a.get_chunk_bytes(rec.chunk)); struct.pack_into(">I", expected, rec.offset, 200)
            self.assertEqual(reread.get_chunk_bytes(rec.chunk), expected)
            self.assertTrue(0 < report["audit"]["changed_bytes"] <= 4)
            for ri in a.find_chunks():
                if ri != rec.chunk: self.assertEqual(a.get_chunk_bytes(ri), reread.get_chunk_bytes(ri))
            self.assertEqual(target.read_bytes(), src.read_bytes())

    def test_scalar_mutations_and_shared_record_review(self):
        src = write_archive(self.root, material_payloads((materials.SKIN, materials.SKIN), shared=True), "shared")
        doc = nlg_asset.AssetDocument(src); a = doc.sections[0].archive; rec = materials.records(a)[0]
        for name, value, lo, hi in (("alpha", 0.25, 0xA8, 0xAC), ("tint", [1, 0.25, 0], 0x9C, 0xA8),
                                    ("specular_power", 64, 0x84, 0x88)):
            patches = materials.patches(a, rec.chunk, 0, name, value)
            self.assertTrue(all(lo <= p.offset < p.offset + len(p.new) <= hi for p in patches))
            ps = nlg_asset.PatchSet(doc.source_hashes, patches)
            self.assertIn("2 mesh users", ps.review()[0])
            _, _, audit = nlg_asset.rebuild_with_patches(src, ps)
            self.assertTrue(0 < audit["changed_bytes"] <= hi - lo)

    def test_adjacent_field_audit_allows_union_but_never_a_gap(self):
        payloads = dict(material_payloads()); raw = bytearray(payloads[0xB016])
        struct.pack_into(">f", raw, 0xA4, 0.12345); payloads[0xB016] = raw
        src = write_archive(self.root, list(payloads.items()), "adjacent")
        doc = nlg_asset.AssetDocument(src); a = doc.sections[0].archive
        patches = materials.patches(a, 0, 0, "tint", [0.25, 0.5, 0.75]) + materials.patches(a, 0, 0, "alpha", 0.25)
        ps = nlg_asset.PatchSet(doc.source_hashes, patches)
        _, _, audit = nlg_asset.rebuild_with_patches(src, ps)
        self.assertTrue(audit["changed_bytes"] > 0)
        apply = nlg_model.apply_patches
        def stray(archive, changes):
            apply(archive, changes)
            if not changes: return   # derived culling patches (none in this archive)
            # The space between the specular and tint fields has no authorized edits.
            ri = changes[0].chunk; offset = archive.chunks[ri][4]
            archive.blocks[archive._chunk_block(ri)][offset + 0x98] ^= 0xFF
        with patch.object(nlg_model, "apply_patches", side_effect=stray):
            with self.assertRaisesRegex(ValueError, "outside"):
                nlg_asset.rebuild_with_patches(src, ps)

    def test_unknown_layouts_states_spans_and_owners_refuse(self):
        source = dict(material_payloads())
        mutations = {}
        unknown = bytearray(source[0xB004]); struct.pack_into(">I", unknown, 16, 123)
        mutations["unknown shader"] = {0xB004: unknown}
        wrong = bytearray(source[0xB004]); struct.pack_into(">I", wrong, 52 + 36, 200)
        mutations["wrong span"] = {0xB004: wrong}
        conflict = bytearray(source[0xB004]); struct.pack_into(">I", conflict, 52 + 36, 0)
        mutations["conflicting shader"] = {0xB004: conflict, 0xB016: source[0xB016][:204]}
        state = bytearray(source[0xB016]); state[6] = 3
        mutations["runtime texture state"] = {0xB016: state}
        mutations["partial mesh"] = {0xB004: source[0xB004] + b"\0"}
        mutations["empty mesh"] = {0xB004: b""}
        for name, replacement in mutations.items():
            src = write_archive(self.root, list({**source, **replacement}.items()), name)
            a = po_archive.load_archive(src)
            self.assertTrue(all(r.error for r in materials.records(a)), name)
            with self.assertRaises(materials.MaterialError): materials.patches(a, 0, 0, "alpha", 0.5)
        src = write_archive(self.root, [(0xB016, record_bytes(materials.SKIN))], "orphan")
        a = po_archive.load_archive(src)
        self.assertEqual(po_archive.material_chunks(a), [])
        with self.assertRaises(materials.MaterialError): materials.patches(a, 0, 0, "alpha", 0.5)

    def test_invalid_fields_values_and_texture_candidates_refuse(self):
        a = po_archive.load_archive(self.source)
        for field, value in (("alpha", float("nan")), ("alpha", True), ("alpha", -1), ("alpha", 1.01),
                             ("specular_power", 257), ("tint", [1, 2, 3]), ("tint", [1]),
                             ("colour2", [1, 1, 1]), ("texture_8", 200), ("texture_0", 999), ("texture_0", True),
                             ("texture_0", 200.0), ("shader", 0)):
            with self.subTest(field=field, value=value), self.assertRaises(materials.MaterialError):
                materials.patches(a, 0, 0, field, value)
        rec = materials.records(a)[1]
        with self.assertRaises(materials.MaterialError): materials.patches(a, rec.chunk, rec.offset, "alpha", 0.5)
        for chunk, off in ((True, 0), (0, True), (0, 1), (0, -1), (99, 0)):
            with self.assertRaises(materials.MaterialError): materials.patches(a, chunk, off, "alpha", 0.5)
        entries = po_archive.texture_entries(a)
        with self.assertRaises(materials.MaterialError): materials.field_patches(rec, "texture_0", 200, entries + [entries[1]])

    def test_replacement_texture_must_have_a_supported_complete_pixel_chain(self):
        for offset, value in ((96 + 13, 77), (96 + 11, 12)):
            payloads = dict(material_payloads()); headers = bytearray(payloads[0xB601]); headers[offset] = value
            payloads[0xB601] = headers
            src = write_archive(self.root, list(payloads.items()), "bad_texture_%d" % offset)
            with self.assertRaises(materials.MaterialError): materials.patches(po_archive.load_archive(src), 0, 0, "texture_0", 200)

    def test_metadata_never_invents_texture_links_from_opaque_words(self):
        payloads = dict(material_payloads())
        raw = bytearray(payloads[0xB016]); struct.pack_into(">I", raw, 204 + 60, 200); payloads[0xB016] = raw
        src = write_archive(self.root, list(payloads.items()), "coincidence")
        doc = nlg_asset.AssetDocument(src); ms = doc.sections[0].model_sets[0]
        meta = doc.mesh_metadata(0, ms, ms.meshes[1])
        self.assertEqual([r["word"] for r in meta["material"]["textures"]], [0, 8, 16, 24, 32])
        self.assertEqual(meta["material"]["shader_name"], "hippobasicenvironment")

    def test_legacy_material_editor_requires_skin_ownership_and_bounded_fields(self):
        from nlg_material import Materials
        with patch("nlg_hash.load_hashid_bin", return_value={}):
            with self.assertRaises(ValueError): Materials(str(self.source))
        src = write_archive(self.root, material_payloads((materials.SKIN, materials.SKIN)), "legacy_skin")
        with patch("nlg_hash.load_hashid_bin", return_value={}): editor = Materials(str(src))
        before = bytes(editor.mat)
        for offset, value in ((0x88, 1), (0x84, 257), (0x9C, float("nan")), (0xA8, True)):
            with self.assertRaises(ValueError): editor.set_f(0, offset, value)
        with self.assertRaises(ValueError): editor.set_slot(0, 8, 200)
        self.assertEqual(bytes(editor.mat), before)

    def test_registry_covers_observed_sizes_and_explicit_inputs(self):
        self.assertEqual({h: v.size for h, v in materials.LAYOUTS.items()}, nlg_model.MATERIAL_SIZES)
        a = po_archive.load_archive(self.source)
        rows = materials.inspect_archive(a)
        self.assertEqual([r["bytes"] for r in rows], [204, 140])
        self.assertEqual([len(r["texture_inputs"]) for r in rows], [8, 5])
        self.assertEqual(rows[1]["editable_fields"], ["texture_%d" % i for i in range(5)])
        self.assertTrue(all(r["resolution"] == "local" for r in rows[1]["texture_inputs"]))
        self.assertEqual(sum(r["bytes"] for r in rows[1]["opaque_ranges"]), 100)
        self.assertNotIn("runtime_verified", rows[1]["editable_fields"])



class PrivateMaterialTests(unittest.TestCase):
    def test_fixture_layout_noop_and_local_replacement(self):
        root = fixtures.fixture_root()
        if root is None: self.skipTest("PO_FIXTURE_ROOT not set or private fixture hashes differ")
        found = set()
        for fixture in fixtures.load()["fixtures"]:
            doc = nlg_asset.AssetDocument(root / fixture["path"])
            for section in doc.sections:
                a = section.archive; entries = po_archive.texture_entries(a)
                for rec in materials.records(a):
                    self.assertIsNone(rec.error)
                    for ref in rec.references():
                        self.assertEqual(materials.field_patches(rec, "texture_%d" % ref["slot"], ref["hash"]), [])
                    if not doc.editable or rec.shader in found: continue
                    candidates = [e for e in entries if e.hash != struct.unpack_from(">I", rec.raw)[0]]
                    if not candidates: continue
                    patches = materials.field_patches(rec, "texture_0", candidates[0].hash, entries)
                    ps = nlg_asset.PatchSet(doc.source_hashes, patches)
                    first = nlg_asset.rebuild_with_patches(doc.path, ps)
                    second = nlg_asset.rebuild_with_patches(doc.path, ps)
                    self.assertEqual(first, second)
                    self.assertTrue(0 < first[2]["changed_bytes"] <= 4)
                    found.add(rec.shader)
        self.assertEqual(found, set(materials.LAYOUTS))


if __name__ == "__main__": unittest.main()
