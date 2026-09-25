"""Synthetic effect ownership, guarded patching, and opaque-byte preservation."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
from nlg_effect import Effects, EffectFormatError, ResourceIndex
from nlg_pack import Archive
import po_archive
import fixtures
import nlg_asset
from helpers import archive_bytes, texture_header


def effect_archive(groups=1, binding_sets=1, emitters=2, extra_tex=()):
    """Synthetic, nested table references, sentinels deliberately filled with nonzero junk."""
    table = [[0x92, 2, 0x4000, 0, 0] for _ in range(groups)]
    block = bytearray()
    def payload(kind, raw):
        ri = len(table)
        table.append([0x12, 2, kind, len(raw), len(block)])
        block.extend(raw)
        return ri
    for group in range(groups):
        start = len(table)
        table[group][3:] = [3 + emitters + binding_sets, start]
        payload(0x4001, struct.pack(">6I", 0, 0x100 + group, emitters, 0xABCD1234, binding_sets, 0x12345678))
        payload(0x4025, b"p" * (4 * emitters))
        payload(0x4026, b"q" * (4 * binding_sets))
        parents = []
        for ei in range(emitters):
            parents.append(len(table)); table.append([0x92, 2, 0x4002, 9, 0])
        binds = []
        for bi in range(binding_sets):
            binds.append(len(table)); table.append([0x92, 2, 0x4020, 2, 0])
        for ei, parent in enumerate(parents):
            table[parent][4] = len(table)
            raw = bytearray(b"z" * 220)
            struct.pack_into(">I", raw, 0, 0x200 + ei)
            raw[52:56] = bytes((0, 1, 0, 0))
            struct.pack_into(">II", raw, 56, 0x300 + ei % 2, 1)
            struct.pack_into(">I", raw, 84, 0xFFFFFFFF)
            payload(0x4003, raw)
            params = []
            for pi in range(8):
                params.append(len(table)); table.append([0x92, 2, 0x4004, 2 if pi == 1 else 1, 0])
            for pi, pp in enumerate(params):
                table[pp][4] = len(table)
                if pi == 1:
                    payload(0x4005, struct.pack(">IffII", 1, 0, 0, 2, 0xCAFEBABE))
                    payload(0x4006, struct.pack(">5fI5fI", 0, 1, 2, 3, 4, 0xABABABAB,
                                                0.5, 5, 6, 7, 8, 0xCDCDCDCD))
                else:
                    payload(0x4005, struct.pack(">IffII", 0, 1, 0.2, 0xFFFFFFFF, 0x41424344))
        for bi, bp in enumerate(binds):
            table[bp][4] = len(table)
            payload(0x4021, struct.pack(">IIII", 0x400 + bi, 0xDEADC0DE, emitters, 0xFACEFACE))
            raw = bytearray(b"x" * (88 * emitters))
            for ei in range(emitters):
                struct.pack_into(">I", raw, 88 * ei + 4, ei)
                struct.pack_into(">3f", raw, 88 * ei + 44, 0.25, 0.5, 0.75)
            payload(0x4022, raw)
    # Payload flag block is 1, as in the verified retail effect layout.
    records = [(0x300, 0), (0x301, 32)] + [(key, 64 + 32 * i) for i, key in enumerate(extra_tex)]
    tex = b"".join(texture_header(8, 8, 6, 1, off, key) for key, off in records)
    payload(0xB601, tex); payload(0xB603, bytes(32 * len(records)))
    dd, _ = archive_bytes([])
    dd = bytearray(dd)
    struct.pack_into(">II", dd, 16, groups, len(table) * 12)
    struct.pack_into(">I", dd, 32, len(block))
    da = bytes(block) + b"".join(struct.pack(">BBHII", *r) for r in table)
    return Archive("synthetic.dict", dd, da)


def write_effect(root, **kwargs):
    a = effect_archive(**kwargs)
    path = Path(root) / "effect.dict"
    path.write_bytes(a.build_dict()); path.with_suffix(".data").write_bytes(a.build_data())
    return path


class EffectTests(unittest.TestCase):
    def test_explicit_ownership_and_variants(self):
        a = effect_archive(groups=2, binding_sets=2)
        fx = Effects(a)
        self.assertEqual(len(fx.groups), 2)
        self.assertFalse(fx.unowned)
        for e in fx.groups:
            self.assertIsNone(e.reason)
            self.assertEqual(e.layout, "multiple binding sets")
            self.assertEqual(len(e.emitters), 2)
            self.assertEqual([b.emitter_index for b in e.binding_sets[0].bindings], [0, 1])
            self.assertEqual(e.binding_sets[0].bindings[0].origin_preview(e.emitters[0]), (0.25, 0.5, 0.75))
            self.assertEqual(len(e.emitters[0].colour_samples), 25)
        self.assertEqual(Effects(effect_archive(emitters=0)).groups[0].layout, "empty")

    def test_curve_polynomial_discriminates_segment_and_coefficient_order(self):
        p = Effects(effect_archive()).groups[0].emitters[0].parameters[1]
        self.assertEqual(p.sample(0), 4)
        self.assertAlmostEqual(p.sample(0.25), 4.890625)
        self.assertEqual(p.sample(0.5), 13.625)
        self.assertEqual(p.sample(1), 26)
        for t in (-1, 1.1, float("nan")):
            with self.assertRaises(EffectFormatError): p.sample(t)

    def test_noop_exact_rebuild_and_only_texture_hash_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_effect(tmp, groups=2)
            a = po_archive.load_archive(path); fx = Effects(a); e = fx.groups[0].emitters[0]
            self.assertEqual(fx.texture_patches(e.chunk, e.texture_hash, e.texture_hash), [])
            hashes = [nlg_asset.sha256(a.orig_dict), nlg_asset.sha256(a.orig_data)]
            noop = nlg_asset.PatchSet(hashes)
            self.assertEqual(nlg_asset.rebuild_with_patches(path, noop)[:2], (a.orig_dict, a.orig_data))
            ps = nlg_asset.PatchSet(hashes, fx.texture_patches(e.chunk, e.texture_hash, 0x301))
            first = nlg_asset.rebuild_with_patches(path, ps)
            self.assertEqual(first, nlg_asset.rebuild_with_patches(path, ps))
            out = Archive(str(path), first[0], first[1])
            for ri in a.find_chunks():
                old, new = a.get_chunk_bytes(ri), out.get_chunk_bytes(ri)
                if ri == e.chunk: self.assertEqual(new, old[:56] + struct.pack(">I", 0x301) + old[60:])
                else: self.assertEqual(old, new)
            self.assertEqual(a.build_chunk_table(), out.build_chunk_table())
            self.assertEqual(Effects(out).emitter(e.chunk).texture_hash, 0x301)
            self.assertGreater(first[2]["changed_bytes"], 0)


    def test_texture_refusal_guards(self):
        def change(offset, word):
            a = effect_archive(); e = Effects(a).groups[0].emitters[0]
            raw = bytearray(a.get_chunk_bytes(e.chunk)); struct.pack_into(">I", raw, offset, word)
            a.blocks[1][a.chunks[e.chunk][4]:a.chunks[e.chunk][4] + len(raw)] = raw
            return Effects(a), e.chunk
        for off, value, reason in ((60, 4, "atlas"), (84, 123, "Model"), (56, 99, "Source")):
            fx, chunk = change(off, value)
            with self.assertRaisesRegex(EffectFormatError, reason):
                fx.texture_patches(chunk, fx.emitter(chunk).texture_hash, 0x301)
        fx = Effects(effect_archive()); e = fx.groups[0].emitters[0]
        with self.assertRaisesRegex(EffectFormatError, "Target"): fx.texture_patches(e.chunk, e.texture_hash, 0x999)
        with self.assertRaisesRegex(EffectFormatError, "changed"): fx.texture_patches(e.chunk, 0x999, 0x301)
        a = effect_archive(extra_tex=(0x302,)); e = Effects(a).groups[0].emitters[0]
        ri = a.find_chunks(type_id=0xB601)[0]; hdr = bytearray(a.get_chunk_bytes(ri))
        struct.pack_into(">H", hdr, 192 + 4, 16)
        a.blocks[1][a.chunks[ri][4]:a.chunks[ri][4] + len(hdr)] = hdr
        with self.assertRaisesRegex(EffectFormatError, "dimensions"):
            Effects(a).texture_patches(e.chunk, e.texture_hash, 0x302)
        hdr[192 + 13] = 0; struct.pack_into(">H", hdr, 192 + 4, 8)
        a.blocks[1][a.chunks[ri][4]:a.chunks[ri][4] + len(hdr)] = hdr
        with self.assertRaisesRegex(EffectFormatError, "preview codec"):
            Effects(a).texture_patches(e.chunk, e.texture_hash, 0x302)

    def test_malformed_structure_is_preserved_and_refused(self):
        mutations = [lambda a: a.chunks[0].__setitem__(4, len(a.chunks)),
                     lambda a: a.chunks[0].__setitem__(3, 0xFFFFFFFF),
                     lambda a: a.chunks[0].__setitem__(0, 0x82),
                     lambda a: a.chunks[0].__setitem__(1, 3),
                     lambda a: a.chunks[4].__setitem__(4, 0),
                     lambda a: a.chunks[4].__setitem__(3, 8)]
        for mutate in mutations:
            a = effect_archive(); mutate(a); before = a.build_data(); fx = Effects(a)
            self.assertIsNotNone(fx.groups[0].reason)
            self.assertEqual(a.build_data(), before)
            with self.assertRaises(EffectFormatError): fx.emitter(7)
        a = effect_archive(groups=2)
        a.chunks[1][3:] = a.chunks[0][3:]
        self.assertTrue(all(g.reason for g in Effects(a).groups))

    def test_bad_curve_count_binding_index_and_nonfinite(self):
        for kind, offset, word in ((0x4005, 0, 2), (0x4022, 4, 200), (0x4006, 4, 0x7F800000)):
            a = effect_archive(); ri = a.find_chunks(type_id=kind)[0]
            struct.pack_into(">I", a.blocks[1], a.chunks[ri][4] + offset, word)
            self.assertIsNotNone(Effects(a).groups[0].reason)

    def test_resource_index_exact_references_only(self):
        a = effect_archive(); fx = Effects(a); idx = ResourceIndex(); idx.add(a)
        refs = idx.references(fx.groups[0].emitters[0])
        self.assertEqual(len(refs["textures"]), 1)
        self.assertEqual(refs["models"], [])
        self.assertEqual(refs["animations"], [])


    def test_private_shared_effects(self):
        path = fixtures.fixture("effects/effects.dict")
        if path is None: self.skipTest("Set PO_FIXTURE_ROOT for private effects")
        a = po_archive.load_archive(path); fx = Effects(a)
        self.assertEqual((len(fx.groups), sum(len(g.emitters) for g in fx.groups)), (156, 515))
        self.assertFalse(fx.unowned)
        self.assertFalse([g.reason for g in fx.groups if g.reason])
        self.assertEqual(a.build_data(), a.orig_data)


if __name__ == "__main__": unittest.main()
