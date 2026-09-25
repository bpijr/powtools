"""Generic model sets, asset documents and patch sets. Synthetic bytes, plus opt-in private
fixtures through PO_FIXTURE_ROOT (see art/README.md)."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import nlg_asset
import nlg_model
from nlg_pack import Archive
from helpers import archive_bytes
import fixtures

IDENTITY = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
MOVED = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 2.5, -1, 0.25, 1)


def model_set_payloads(transform_index=1, node_counts=(1, 1), order=None, extra_ptr=0):
    """Two nodes, two 3-vertex meshes: f32 positions, s8 normals, UV0 (s16/1024), a colour
    and a texture set whose storage scale is unestablished (0x16)."""
    vtx = bytearray(); ptrs = bytearray(); meshes = bytearray(); idx = bytearray()
    for m in range(2):
        idx += bytes(-len(idx) % 32); istart = len(idx)
        idx += struct.pack(">3H", 0, 1, 2)
        attrs = [
            (0x0A, 12, 0x100, struct.pack(">9f", 0, 0, m, 1, 0, m, 0, 1, m)),
            (0xFE, 3, 0x200, struct.pack(">9b", 0, 0, 64, 0, 0, 64, 0, 0, 64)),
            (0xCC, 4, 0x400, struct.pack(">6h", 0, 0, 1024, 0, 0, 1024)),
            (0xE9, 4, 0x301, bytes([255, 128, 0, 255] * 3)),
            (0x16, 4, 0x401, struct.pack(">6h", 7, 8, 9, 10, 11, 12)),
        ]
        first_ptr = len(ptrs)
        for typ, stride, flags, raw in attrs:
            vtx += bytes(-len(vtx) % 32)
            ptrs += struct.pack(">IBBH", len(vtx), typ, stride, flags)
            vtx += raw
        rec = bytearray(52)
        struct.pack_into(">IIHBB", rec, 0, istart, 3, 3, 1, len(attrs))
        struct.pack_into(">7I", rec, 12, first_ptr + (extra_ptr if m else 0), 0x21DB4385, 0x1000 + m,
                         transform_index if m else 0, 0x000D0007, 0xD0540001, 8 * m)
        meshes += rec
    nodes = b"".join(struct.pack(">III", 0x2000 + i, n, 0) for i, n in enumerate(node_counts))
    materials = struct.pack(">II", 0xAAAA0001, 0) * 2
    payloads = {0xB016: materials, 0xB007: bytes(idx), 0xB006: bytes(vtx), 0xB005: bytes(ptrs),
                0xB004: bytes(meshes), 0xB002: struct.pack(">16f", *IDENTITY) + struct.pack(">16f", *MOVED),
                0xB003: nodes}
    return [(t, payloads[t]) for t in (order or nlg_model.MAIN)]


def write_archive(folder, payloads, name="prop"):
    d, data = archive_bytes(payloads)
    path = Path(folder) / (name + ".dict")
    path.write_bytes(d); path.with_suffix(".data").write_bytes(data)
    return path


class ModelCodecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = Path(self.tmp.name)
        (self.dir / "src").mkdir()
        self.src = write_archive(self.dir / "src", [(0x5002, b"\x01" * 12)] + model_set_payloads())

    def tearDown(self): self.tmp.cleanup()

    def test_decodes_nodes_transforms_and_semantics(self):
        sets = nlg_model.model_sets(Archive(str(self.src)))
        self.assertEqual(len(sets), 1)
        s = sets[0]
        self.assertEqual([(n.name_hash, list(n.meshes)) for n in s.nodes], [(0x2000, [0]), (0x2001, [1])])
        self.assertEqual(s.meshes[1].transform, 1)
        self.assertEqual(s.transforms[1][12:15], (2.5, -1.0, 0.25))
        mesh = s.meshes[1]
        self.assertEqual(mesh.attribute("position").values()[1], (1.0, 0.0, 1.0))
        self.assertEqual(mesh.attribute("normal").values()[0], (0.0, 0.0, 1.0))
        self.assertEqual(mesh.attribute("texcoord").values()[1], (1.0, 0.0))
        self.assertEqual(mesh.attribute("color", 1).values()[0], (255, 128, 0, 255))
        self.assertFalse(mesh.attribute("texcoord", 1).verified)
        self.assertEqual(mesh.triangles(), [(0, 1, 2)])
        self.assertEqual(s.material_span(mesh), (8, 8))
        # world = [x y z 1] . M: the stored translation becomes the last column.
        self.assertEqual([row[3] for row in nlg_model.world_matrix(s.transforms[1])], [2.5, -1.0, 0.25, 1.0])

    def test_noop_is_byte_identical(self):
        doc = nlg_asset.AssetDocument(self.src)
        s = doc.sections[0].model_sets[0]
        edits = {m.index: {(a.semantic, a.set): a.values() for a in m.attributes if a.verified} for m in s.meshes}
        patches = nlg_model.geometry_patches(s, edits)
        self.assertEqual(patches, [])
        out = self.dir / "out" / "prop.dict"
        report = nlg_asset.write_rebuild(self.src, nlg_asset.PatchSet(doc.source_hashes, patches), out)
        self.assertEqual(out.read_bytes(), self.src.read_bytes())
        self.assertEqual(out.with_suffix(".data").read_bytes(), self.src.with_suffix(".data").read_bytes())
        self.assertEqual(report["audit"]["changed_bytes"], 0)

    def test_position_uv_and_transform_edits_are_confined(self):
        doc = nlg_asset.AssetDocument(self.src); s = doc.sections[0].model_sets[0]
        pos = s.meshes[1].attribute("position").values(); pos[2] = (0.5, 0.5, 3.0)
        uv = s.meshes[0].attribute("texcoord").values(); uv[1] = (0.5, 0.25)
        patches = nlg_model.geometry_patches(s, {1: {("position", 0): pos}, 0: {("texcoord", 0): uv}})
        patches += nlg_asset.transform_patches(s, 1, IDENTITY)
        # Byte-minimal: each patch stays inside the one element it edits.
        pos_attr = s.meshes[1].attribute("position"); uv_attr = s.meshes[0].attribute("texcoord")
        spans = {"1 vertex position changed": (pos_attr.offset + 24, pos_attr.offset + 36),
                 "1 UV changed": (uv_attr.offset + 4, uv_attr.offset + 8),
                 "transform 1 changed (places 1 mesh)": (64 + 48, 64 + 60)}
        self.assertEqual(len(patches), 3)
        for p in patches:
            lo, hi = spans[next(k for k in spans if k in p.label)]
            self.assertTrue(lo <= p.offset and p.offset + len(p.new) <= hi, p.label)
        ps = nlg_asset.PatchSet(doc.source_hashes, patches)
        self.assertEqual(len(ps.review()), 3)
        out = self.dir / "out" / "prop.dict"
        report = nlg_asset.write_rebuild(self.src, ps, out)
        self.assertEqual(out.read_bytes(), self.src.read_bytes())
        # The audit counts bytes that really differ; merged patches may span a few equal bytes.
        self.assertTrue(0 < report["audit"]["changed_bytes"] <= sum(len(p.new) for p in patches))
        edited = nlg_model.model_sets(Archive(str(out)))[0]
        self.assertEqual(edited.meshes[1].attribute("position").values()[2], (0.5, 0.5, 3.0))
        self.assertEqual(edited.meshes[0].attribute("texcoord").values()[1], (0.5, 0.25))
        self.assertEqual(edited.transforms[1], tuple(float(x) for x in IDENTITY))
        # Patch sets survive serialization.
        again = nlg_asset.PatchSet.from_dict(ps.as_dict())
        self.assertEqual(nlg_asset.rebuild_with_patches(self.src, again)[:2],
                         (out.read_bytes(), out.with_suffix(".data").read_bytes()))

    def test_refuses_unsafe_edits(self):
        doc = nlg_asset.AssetDocument(self.src); s = doc.sections[0].model_sets[0]
        with self.assertRaises(nlg_model.ModelFormatError):   # unestablished storage scale
            nlg_model.geometry_patches(s, {0: {("texcoord", 1): [(0, 0)] * 3}})
        with self.assertRaises(nlg_model.ModelFormatError):   # topology changed
            nlg_model.geometry_patches(s, {0: {("position", 0): [(0, 0, 0)] * 4}})
        with self.assertRaises(nlg_model.ModelFormatError):   # outside s16/1024 range
            nlg_model.geometry_patches(s, {0: {("texcoord", 0): [(40.0, 0)] * 3}})
        patch = nlg_model.geometry_patches(s, {0: {("position", 0): [(9, 9, 9)] * 3}})
        with self.assertRaises(ValueError):                    # different source
            nlg_asset.rebuild_with_patches(self.src, nlg_asset.PatchSet(["0" * 64, "0" * 64], patch))
        bad = nlg_model.Patch(patch[0].chunk, patch[0].offset, b"\xff" * len(patch[0].old), patch[0].new, "tampered")
        with self.assertRaises(ValueError):                    # recorded original bytes differ
            nlg_asset.rebuild_with_patches(self.src, nlg_asset.PatchSet(doc.source_hashes, [bad]))
        with self.assertRaises(ValueError):                    # never overwrite the source
            nlg_asset.write_rebuild(self.src, nlg_asset.PatchSet(doc.source_hashes, []), self.src)

    def test_malformed_model_sets_are_rejected(self):
        cases = {
            "transform index": model_set_payloads(transform_index=2),
            "node coverage": model_set_payloads(node_counts=(1, 2)),
            "chunk order": model_set_payloads(order=(0xB016, 0xB007, 0xB006, 0xB004, 0xB005, 0xB002, 0xB003)),
            "pointer order": model_set_payloads(extra_ptr=8),
        }
        for name, payloads in cases.items():
            with self.subTest(name):
                path = write_archive(self.dir, payloads, name.replace(" ", "_"))
                with self.assertRaises(nlg_model.ModelFormatError):
                    nlg_model.model_sets(Archive(str(path)))
        partial = [(t, raw + b"\0" if t == 0xB002 else raw) for t, raw in model_set_payloads()]
        with self.assertRaises(nlg_model.ModelFormatError):
            nlg_model.model_sets(Archive(str(write_archive(self.dir, partial, "partial"))))

    def test_inventory_marks_unknown_chunks_preserved(self):
        doc = nlg_asset.AssetDocument(self.src)
        inv = doc.sections[0].inventory()
        self.assertEqual([c["owner"] for c in inv if c["type"] == "0x5002"], [None])
        self.assertTrue(all(c["owner"] == "model set 0" for c in inv if c["type"].startswith("0xB0")))
        meta = doc.mesh_metadata(0, doc.sections[0].model_sets[0], doc.sections[0].model_sets[0].meshes[1])
        self.assertEqual(meta["schema"], nlg_asset.SCHEMA)
        self.assertIn("texcoord1", meta["preserved"]); self.assertIn("transform", meta["editable"])
        self.assertEqual(meta["transform"]["matrix"][12:15], [2.5, -1.0, 0.25])


class PrivateFixtureModelTests(unittest.TestCase):
    """Real archives from the fixture matrix; skipped unless PO_FIXTURE_ROOT verifies."""

    def setUp(self):
        self.root = fixtures.fixture_root()
        if self.root is None:
            self.skipTest("PO_FIXTURE_ROOT not set or fixtures differ from tests/private_fixtures.json")

    def test_every_fixture_parses_and_noop_patches_are_empty(self):
        for entry in fixtures.load()["fixtures"]:
            with self.subTest(entry["path"]):
                doc = nlg_asset.AssetDocument(self.root / entry["path"])
                for section in doc.sections:
                    for s in section.model_sets:
                        for m in s.meshes:
                            edits = {(a.semantic, a.set): a.values() for a in m.attributes if a.verified}
                            self.assertEqual(nlg_model.geometry_patches(s, {m.index: edits}), [])
                            self.assertEqual(s.material_span(m)[1], nlg_model.MATERIAL_SIZES[m.shader])

    def test_gameworld_edit_is_confined(self):
        path = self.root / "environments/minorcircuit/gameworld.dict"
        doc = nlg_asset.AssetDocument(path); s = doc.sections[0].model_sets[0]
        pos = s.meshes[0].attribute("position").values()
        pos[0] = (pos[0][0] + 0.125, pos[0][1], pos[0][2])
        ps = nlg_asset.PatchSet(doc.source_hashes, nlg_model.geometry_patches(s, {0: {("position", 0): pos}}))
        _, data, audit = nlg_asset.rebuild_with_patches(path, ps)
        self.assertTrue(0 < audit["changed_bytes"] <= 4)   # inside one f32 of one vertex
        self.assertEqual(len(data), path.with_suffix(".data").stat().st_size)


if __name__ == "__main__": unittest.main()
