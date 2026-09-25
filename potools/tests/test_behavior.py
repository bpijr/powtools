"""Synthetic definition directory/graph corruption and isolated typed-edit gates."""
from pathlib import Path
import json
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
from nlg_pack import Archive
from nlg_hash import string_to_hash
import nlg_behavior as behavior
from nlg_asset import AssetDocument, PatchSet, rebuild_with_patches, write_rebuild
import po_archive


def words(*values): return struct.pack(f">{len(values)}I", *values)
def key(name): return string_to_hash(name, True)


def definition_bytes():
    """Invented two-node combo plus a class, import directory and opaque instructions."""
    graph = [(0xE001, words(2, 0x10203040, 0, 0, 0, 0, 0xDEADBEEF, 0xDEADBEEF)),
             (0xE002, b"Synthetic\0\0\0")]
    for index, name in enumerate(("NodeA", "NodeB")):
        info = [0] * 11; info[5] = info[7] = info[8] = 1
        row = [0] * 17; row[12] = 0x10204000; row[16] = index
        graph += [(0xE010, words(0x10203000, 0, key(name), 0x10300000, index)),
                  (0xE011, words(*info)), (0xE012, name.encode() + b"\0"),
                  (0xE030, words(*row)),
                  (0xE013, words(0x10400000, 2, 0xDEADBEEF, 0xDEADBEEF)),
                  (0xE014, words(key("UnestablishedWord"), 1, 0xA1B2C3D4)),
                  (0xE014, words(key("AnimProperties"), 3, 0x10500000)),
                  (0xE013, words(0x10600000, 3, 0xDEADBEEF, 0xDEADBEEF, 0xDEADBEEF)),
                  (0xE014, words(key("TimeScale"), 2) + struct.pack(">f", 1.0)),
                  (0xE014, words(key("BlendAmount"), 2) + struct.pack(">f", 0.25)),
                  (0xE014, words(key("OpaqueFloat"), 2) + struct.pack(">f", 37.5)),
                  (0xE031, (b"NodeB\0" if index == 0 else b"ExternalNode\0")),
                  (0xE013, words(0x10700000, 1, 0xDEADBEEF)),
                  (0xE014, words(key("Condition"), 1, 7)),
                  (0xE015, words(key("test_clip"))), (0xE015, words(0))]
    script = [(0x5001, words(0, key("ComboScriptInterpreter"), key("synthetic.script"), 1, 4, 6, 4, 99)
               + words(0x12345678) + b"\xFF\xFD\xFC\xFA\xFB\xF8" + b"abc\0"),
              (0x5002, words(key("Global"), 2, 0x00040001, key("float"), 0)),
              (0x5013, words(key("First"), 0, 0x89ABCDEF, key("Last"), 2, 0xFEDCBA98)),
              (0x5011, words(1, key("synthetic.script"), 1, key("Global"), 1, key("First")))]
    records = [[0x92, 0x11, 1, 1, 2], [0x92, 0, 0x5000, len(script) + 1, 3 + len(graph)],
               [0x92, 1, 0xE000, len(graph), 3]]
    payload = bytearray()
    for kind, raw in graph + script:
        records.append([0x12, 1, kind, len(raw), len(payload)])
        payload += raw + bytes(-len(raw) % 4)
    records.append([0xF2, 0, 0x5020, 0, len(records) + 1])
    payload += bytes(-len(payload) % 2048)
    table = b"".join(struct.pack(">BBHII", *r) for r in records)
    dd = struct.pack(">IHBBIIII", 0xA9F32458, 0x0601, 0, 0, 1, 0, 2, len(table))
    dd += b"".join(words(len(payload) if i == 1 else 0, 4) for i in range(8))
    return dd, bytes(payload) + table


def write_fixture(root):
    root.mkdir(parents=True, exist_ok=True); path = root / "synthetic.dict"
    dd, da = definition_bytes(); path.write_bytes(dd); path.with_suffix(".data").write_bytes(da)
    return path


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.path = write_fixture(self.base / "dump")
        self.doc = AssetDocument(self.path)

    def data(self): return behavior.Definitions(po_archive.load_archive(self.path))

    def op(self, value=1.5, **kwargs):
        return dict({"kind": "behavior_float", "combo": 0, "node": 0, "field": "TimeScale", "value": value}, **kwargs)


    def test_noop_mutation_isolation_determinism_and_sidecar(self):
        empty = behavior.edit_patchset(self.doc, [(0, 0, "TimeScale", 1.0)])
        self.assertEqual(empty.patches, [])
        self.assertEqual(rebuild_with_patches(self.path, empty)[:2], definition_bytes())
        ps = behavior.edit_patchset(self.doc, [(0, 0, "TimeScale", 1.5)])
        self.assertEqual({p.chunk for p in ps.patches}, {13})
        dd, da, audit = rebuild_with_patches(self.path, ps)
        self.assertEqual((dd, da), rebuild_with_patches(self.path, ps)[:2])
        self.assertEqual(dd, self.path.read_bytes()); self.assertGreater(audit["changed_bytes"], 0)
        a = Archive(str(self.path), dd, da); d = behavior.Definitions(a)
        self.assertEqual(behavior.editable_fields(d, 0, 0)["TimeScale"]["value"], 1.5)
        self.assertEqual(behavior.editable_fields(d, 0, 1)["TimeScale"]["value"], 1.0)
        before = self.doc.sections[0].archive
        for ri in before.find_chunks():
            if ri != 13: self.assertEqual(before.get_chunk_bytes(ri), a.get_chunk_bytes(ri))
        out = self.base / "out" / "copy.dict"
        report = write_rebuild(self.path, ps, out)
        self.assertTrue(report["deterministic"])
        self.assertEqual(out.with_suffix(".data").read_bytes(), da)
        serialized = PatchSet.from_dict(ps.as_dict())
        self.assertEqual(rebuild_with_patches(self.path, serialized)[:2], (dd, da))

    def test_edit_refusals(self):
        for edit in [(0, 0, "HoldFrame", 0), (0, 0, "TimeScale", -0.1), (0, 0, "TimeScale", 3.1),
                     (0, 0, "BlendAmount", 0.51), (0, 0, "TimeScale", float("nan")),
                     (0, 0, "TimeScale", float("inf")), (0, 0, "TimeScale", True),
                     (0, 0, "TimeScale", "1.0"), (0, -1, "TimeScale", 1), (True, 0, "TimeScale", 1)]:
            with self.subTest(edit=edit), self.assertRaises(ValueError): behavior.edit_patchset(self.doc, [edit])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            behavior.edit_patchset(self.doc, [(0, 0, "TimeScale", 1)] * 2)
        self.doc.sections[0].archive.version = 2
        with self.assertRaisesRegex(ValueError, "version"):
            behavior.edit_patchset(self.doc, [(0, 0, "TimeScale", 1)])

    def test_malformed_graph_and_script_boundaries(self):
        # Every mutation changes a structural discriminator, not an opaque field.
        cases = [(3, 0, 500), (5, 16, 5), (6, 28, 200), (8, 64, 9),
                 (9, 4, 50), (10, 4, 3), (11, 4, 1)]
        # Tag 1 on the nested group leaves its children unowned and must refuse.
        for ri, offset, value in cases:
            a = po_archive.load_archive(self.path); bi = a._chunk_block(ri)
            struct.pack_into(">I", a.blocks[bi], a.chunks[ri][4] + offset, value)
            with self.subTest(ri=ri, offset=offset), self.assertRaises(behavior.BehaviorFormatError): behavior.Definitions(a)
        a = po_archive.load_archive(self.path); a.chunks[2][3] += 1
        with self.assertRaises(behavior.BehaviorFormatError): behavior.Definitions(a)
        for kind, offset, value in [(0x5001, 16, 4000), (0x5002, 8, 4000), (0x5013, 4, 500), (0x5011, 0, 500)]:
            a = po_archive.load_archive(self.path); ri = a.find_chunks(kind)[0]
            struct.pack_into(">I", a.blocks[a._chunk_block(ri)], a.chunks[ri][4] + offset, value)
            with self.subTest(kind=kind), self.assertRaises(behavior.BehaviorFormatError): behavior.Definitions(a)

    def test_unknown_tags_and_invalid_existing_fields_are_readonly(self):
        a = self.doc.sections[0].archive
        struct.pack_into(">I", a.blocks[a._chunk_block(10)], a.chunks[10][4] + 4, 99)
        self.assertEqual(behavior.Definitions(a).summary()["unknown_property_tags"], [99])
        with self.assertRaisesRegex(ValueError, "Unknown property"):
            behavior.edit_patchset(self.doc, [(0, 0, "TimeScale", 1.5)])
        self.doc = AssetDocument(self.path); a = self.doc.sections[0].archive
        struct.pack_into(">I", a.blocks[a._chunk_block(13)], a.chunks[13][4] + 4, 1)
        with self.assertRaisesRegex(ValueError, "absent, duplicated"):
            behavior.edit_patchset(self.doc, [(0, 0, "TimeScale", 1.5)])


    def test_animation_hash_index_and_technique_table(self):
        from helpers import archive_bytes
        dd, da = archive_bytes([(0x7001, words(0, 0, 60)), (0x7002, b"test_clip\0")])
        idx = behavior.animation_index(Archive("test.dict", dd, da))
        self.assertEqual(idx[key("test_clip")][0]["frames"], 60)
        rows = behavior.techniques(words(0x4254474E, 2, 2, 1, 77, 88, 99, 99))
        self.assertEqual(rows[1]["shader_hash"], 88)
        for raw in [b"", words(0x4254474E, 3, 0, 1), words(0x4254474E, 2, 10, 1)]:
            with self.assertRaises(ValueError): behavior.techniques(raw)


    def test_private_definition_families(self):
        from fixtures import fixture
        relatives = ["characterdefinitions/behaviours.bun.dict"] + [
            f"characterdefinitions/glassjoe/glassjoe{role}.dict" for role in ("combodata", "action", "reaction", "hudtips")]
        paths = [fixture(p) for p in relatives]
        if not all(paths): self.skipTest("Set PO_FIXTURE_ROOT to the verified private fixture corpus.")
        for path in paths:
            doc = AssetDocument(path); d = doc.sections[0].definitions
            self.assertTrue(d.owner); self.assertEqual(set(d.owner), set(d.archive.find_chunks()))
            self.assertEqual(rebuild_with_patches(path, PatchSet(doc.source_hashes))[:2],
                             (d.archive.orig_dict, d.archive.orig_data))

    def test_directories_graph_types_and_script_imports(self):
        d = self.data(); s = d.summary()
        self.assertEqual((s["nodes"], s["transitions"], s["properties"], s["property_groups"]), (2, 2, 12, 6))
        self.assertEqual(s["unresolved_transitions"], 1)
        self.assertEqual(set(d.owner), set(d.archive.find_chunks()))
        self.assertEqual(d.combos[0]["nodes"][0]["transitions"][0]["target_nodes"], [1])
        script = d.scripts[0]
        self.assertEqual(script["code_bytes"], 6)
        self.assertEqual(script["strings"], [{"offset": 0, "text": "abc"}])
        self.assertEqual(script["classes"][0]["functions"][1]["code_byte_offset"], 4)
        self.assertEqual(script["imports"][0]["classes"][0]["resolved_function_hashes"], [key("First")])
        self.assertEqual(d.properties[next(iter(d.properties))]["word_raw"], 0xA1B2C3D4)
        self.assertTrue(all(row["owner"] for row in self.doc.sections[0].inventory()))


if __name__ == "__main__": unittest.main()
