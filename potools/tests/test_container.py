"""Container writer: exact re-layout, audited chunk resizes and malformed-input refusals."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
from helpers import archive_bytes, container_bytes
from nlg_pack import Archive
import nlg_asset
import nlg_container as container
from nlg_model import Patch
import po_archive


def padded(pair):
    """Retail blocks are 0x800 padded; synthetic helpers are not."""
    a = Archive("pad.dict", *pair)
    for b in a.blocks:
        b += bytes(-len(b) % 0x800)
    return a.build_dict(), a.build_data()


def three_sections():
    return [padded(archive_bytes([(0xDD00, b"zero" * 10)])),
            padded(archive_bytes([(0xDD01, b"a" * 40), (0xDD02, b"b" * 100), (0xDD03, b"c" * 7)])),
            padded(archive_bytes([(0xDD04, b"tail section")]))]


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.base = Path(self.temp.name)
        self.dd, self.da = container_bytes(three_sections())

    def write(self, name="source", pair=None):
        folder = self.base / name; folder.mkdir(parents=True, exist_ok=True)
        dd, da = pair or (self.dd, self.da)
        path = folder / "nis.dict"; path.write_bytes(dd); path.with_suffix(".data").write_bytes(da)
        return path

    def test_writer_reproduces_single_and_multi_section_containers(self):
        self.assertTrue(container.rebuild_exact(self.dd, self.da))
        self.assertTrue(container.rebuild_exact(*archive_bytes([(0xDD00, b"x")])))
        self.assertTrue(container.rebuild_exact(*padded(archive_bytes([(0xDD00, b"x")]))))
        records, archives = container.parse(self.dd, self.da)
        self.assertEqual([r["offset"] for r in records], [0, 0x1000, 0x2000])
        self.assertEqual(container.write(self.dd[:12], archives)[2], [0, 0x1000, 0x2000])

    def resize_set(self, source, size, chunk=1):
        _, archives = container.parse(self.dd, self.da)
        r = container.resize(archives[1], 1, chunk, b"B" * size, "grow")
        return nlg_asset.PatchSet([po_archive.digest(self.dd), po_archive.digest(self.da)], resizes=[r])

    def test_grow_and_shrink_relocate_sections_with_content_audit(self):
        source = self.write()
        for size in (3000, 5000, 1, 0, 100):
            with self.subTest(size=size):
                ps = self.resize_set(source, size)
                dd, da, audit = nlg_asset.rebuild_with_patches(source, ps)
                self.assertEqual((dd, da, audit), nlg_asset.rebuild_with_patches(source, ps))
                records, after = container.parse(dd, da)
                _, before = container.parse(self.dd, self.da)
                self.assertEqual(after[1].get_chunk_bytes(1), b"B" * size)
                for s in (0, 2): self.assertEqual(after[s].build_data(), before[s].build_data())
                for ri in (0, 2): self.assertEqual(after[1].get_chunk_bytes(ri), before[1].get_chunk_bytes(ri))
                self.assertEqual([r[:3] for r in after[1].chunks], [r[:3] for r in before[1].chunks])
                self.assertTrue(all(r["offset"] % 0x800 == 0 for r in records))
                self.assertEqual(audit["untouched_sections_identical"], 2); self.assertEqual(audit["sections_changed"], [1])
                up = lambda n: (n + 0x7FF) // 0x800 * 0x800; one = audit["sections"][1]
                self.assertEqual(audit["sections"][2]["new_offset"] - audit["sections"][2]["old_offset"], up(one["new_bytes"]) - up(one["old_bytes"]))
                self.assertEqual(dd[:12], self.dd[:12])
                self.assertTrue(container.rebuild_exact(dd, da))

    def test_patch_set_schema_three_roundtrip_and_write_rebuild(self):
        source = self.write(); ps = self.resize_set(source, 3000)
        ps.patches = [nlg_asset.SectionPatch(1, 0, 0, b"a", b"Z", "one byte")]
        value = ps.as_dict(); self.assertEqual(value["schema"], 4)
        again = nlg_asset.PatchSet.from_dict(value)
        self.assertEqual(nlg_asset.rebuild_with_patches(source, again), nlg_asset.rebuild_with_patches(source, ps))
        report = nlg_asset.write_rebuild(source, again, self.base / "out" / "grown.dict")
        self.assertTrue(report["deterministic"]); self.assertEqual(len(po_archive.load_sections(self.base / "out" / "grown.dict")), 3)
        for bad in (dict(value, schema=1), dict(value, resizes=[dict(value["resizes"][0], new="00")])):
            with self.assertRaises(ValueError): nlg_asset.PatchSet.from_dict(bad)
        with self.assertRaises(ValueError): nlg_asset.PatchSet.from_dict(dict(value, resizes=[]))

    def test_malformed_resizes_and_patches_refused(self):
        source = self.write(); hashes = [po_archive.digest(self.dd), po_archive.digest(self.da)]
        _, archives = container.parse(self.dd, self.da); ok = container.resize(archives[1], 1, 1, b"x", "ok")
        R = container.Resize
        cases = [[R(3, 1, ok.old_sha256, b"x", "section")], [R(True, 1, ok.old_sha256, b"x", "bool")],
                 [R(1, 99, ok.old_sha256, b"x", "chunk")], [R(1, 1, "0" * 64, b"x", "hash")], [ok, ok]]
        for resizes in cases:
            with self.assertRaises(ValueError): nlg_asset.rebuild_with_patches(source, nlg_asset.PatchSet(hashes, resizes=resizes))
        for patch in (nlg_asset.SectionPatch(1, 1, 0, b"b", b"c", "on resized chunk"), nlg_asset.SectionPatch(1, 0, 39, b"aa", b"cc", "leaves chunk"),
                      nlg_asset.SectionPatch(1, 0, 0, b"q", b"c", "wrong old bytes")):
            with self.assertRaises(ValueError): nlg_asset.rebuild_with_patches(source, nlg_asset.PatchSet(hashes, [patch], resizes=[ok]))
        two = [nlg_asset.SectionPatch(1, 0, 0, b"aa", b"cc", "a"), nlg_asset.SectionPatch(1, 0, 1, b"aa", b"dd", "b")]
        with self.assertRaisesRegex(ValueError, "overlap"): nlg_asset.rebuild_with_patches(source, nlg_asset.PatchSet(hashes, two, resizes=[ok]))
        with self.assertRaisesRegex(ValueError, "different source"): nlg_asset.rebuild_with_patches(source, nlg_asset.PatchSet(["a", "b"], resizes=[ok]))

    def test_unsafe_blocks_refused(self):
        base = three_sections()
        # Unpadded synthetic block: a no-op relayout would add padding, so resizing is refused.
        a = Archive("x.dict", *archive_bytes([(0xDD01, b"a" * 40)]))
        self.assertIn("nonzero gap", container.block_limitation(a, 0))
        variants = {}
        a = Archive("x.dict", *base[1]); a.blocks[0][40] = 7; variants["gap"] = a          # byte after the 40-byte chunk
        a = Archive("x.dict", *base[1]); a.chunks.append([0, 0, 0xDD09, 8, a.chunks[1][4]]); variants["alias"] = a
        a = Archive("x.dict", *base[1]); a.chunks.insert(0, [0x00, 2, 0x6000, 64, 0]); a.num_file_entries = 1
        variants["toc byte range"] = a
        for name, a in variants.items():
            with self.subTest(name):
                dd, da = container_bytes([base[0], (a.build_dict(), a.build_data()), base[2]]); source = self.write(name, (dd, da))
                _, archives = container.parse(dd, da)
                r = container.resize(archives[1], 1, archives[1].find_chunks(type_id=0xDD02)[0], b"z" * 9, name)
                with self.assertRaises(ValueError):
                    nlg_asset.rebuild_with_patches(source, nlg_asset.PatchSet([po_archive.digest(dd), po_archive.digest(da)], resizes=[r]))

    def test_parse_rejects_malformed_containers(self):
        dd, da = bytearray(self.dd), bytearray(self.da)
        cases = []
        x = bytearray(da); x[0xFFF] = 1; cases.append((dd, x))             # hidden data in a gap
        cases.append((dd, da + b"\0"))                                          # trailing bytes
        y = bytearray(dd); struct.pack_into(">I", y, 12 + 76, 0x10); cases.append((y, da))  # overlap
        y = bytearray(dd); y[6] = 1; cases.append((y, da))                     # compressed
        y = bytearray(dd); struct.pack_into(">I", y, 12 + 76 + 8, 13); cases.append((y, da))  # table size
        cases.append((dd[:-1], da))
        for pair in cases:
            with self.assertRaises(ValueError): container.parse(*pair)

    def test_audit_detects_tampering(self):
        _, archives = container.parse(self.dd, self.da)
        r = container.resize(archives[1], 1, 1, b"B" * 3000, "grow")
        dd, da, _ = container.rebuild_bytes(self.dd, self.da, [], [r])
        records, _ = container.parse(dd, da)
        for offset in (records[0]["offset"] + 3, records[2]["offset"] + 1, records[1]["offset"] + 1):
            bad = bytearray(da); bad[offset] ^= 0xFF
            with self.assertRaises(ValueError): container.audit_rebuild(self.dd, self.da, dd, bytes(bad), {(1, 1): b"B" * 3000}, {})
        with self.assertRaises(ValueError): container.audit_rebuild(self.dd, self.da, dd, da, {}, {})

    def test_single_section_in_place_apply(self):
        a = Archive("single.dict", *padded(archive_bytes([(0xDD00, b"a" * 40), (0xDD01, b"b" * 8)])))
        before = a.get_chunk_bytes(1)
        audit = container.apply_to_archive(a, [], [container.resize(a, 0, 0, b"c" * 5000, "grow")])
        self.assertEqual(a.get_chunk_bytes(0), b"c" * 5000); self.assertEqual(a.get_chunk_bytes(1), before)
        self.assertEqual(audit["changed_chunks"][0]["new_bytes"], 5000)
        self.assertTrue(container.rebuild_exact(a.build_dict(), a.build_data()))


if __name__ == "__main__": unittest.main()
