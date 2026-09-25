"""Animation directory/codec safety and audited rebuilds using synthetic bytes."""
from pathlib import Path
import math
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
import nlg_animation as anim
import nlg_asset
from nlg_pack import Archive
from helpers import archive_bytes
import po_archive
import fixtures


def animation_bytes(frames=3, rotation_flag=0x10, rotation=None, pad=False):
    rig_head = bytearray(56); struct.pack_into(">II", rig_head, 8, 3, 0x12345678)
    clip_head = bytearray(88); struct.pack_into(">II", clip_head, 8, frames, 3)
    struct.pack_into(">I", clip_head, 84, 0x12345678)
    key = rotation if rotation is not None else struct.pack(">4h", 0, 0, 0, 32767)
    payloads = [(0x8001, bytes(rig_head)), (0x8002, b"test_rig\0"),
                (0x8003, struct.pack(">3I", 100, 101, 102)), (0x8009, struct.pack(">3i", -1, 0, 1)),
                (0x8010, struct.pack(">9f", 0, 0, 0, 0, 0, 1, 0, 0, 1)), (0x8011, bytes((1, 0, 1))),
                (0x7001, bytes(clip_head)), (0x7002, b"test_clip\0"),
                (0x7003, struct.pack(">3I", 0, rotation_flag | 8, 15)),
                (0x7110, struct.pack(">3I", 0, frames, 0)),
                (0x7100, b""), (0x7100, b""), (0x7100, b""),
                (0x7101, key * (1 if rotation_flag & 2 else frames)),
                (0x7102, struct.pack(">3f", 0, 0, 1) * frames),
                (0x7103, struct.pack(">3H", 2048, 2048, 2048)),
                (0x7112, bytes([255] * frames)), (0x7115, b"unknown preserved"),
                (0x7101, struct.pack(">h", 0))]
    d, data = archive_bytes(payloads)
    a = Archive("synthetic.dict", dict_bytes=d, data_bytes=data)
    a.chunks[10] = [0x80, 1, 0x7100, 0, 13]
    a.chunks[11] = [0x80, 1, 0x7100, 5, 13]
    a.chunks[12] = [0x80, 1, 0x7100, 1, 18]
    a.chunks += [[0x80, 1, 0x8000, 6, 0], [0x80, 1, 0x7000, 7, 6]]
    for block in a.blocks if pad else ():
        block += bytes(-len(block) % 0x800)   # retail blocks are 0x800 padded; resizing requires it
    return a.build_dict(), a.build_data()


def write_animation(folder, **kwargs):
    d, data = animation_bytes(**kwargs)
    path = Path(folder) / "animation.dict"
    path.write_bytes(d); path.with_suffix(".data").write_bytes(data)
    return path


class AnimationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        # The source sits in its own folder: exports must land outside the source tree.
        self.root = Path(self.tmp.name); (self.root / "src").mkdir()
        self.source = write_animation(self.root / "src")
        self.doc = nlg_asset.AssetDocument(self.source)
        self.set = self.doc.sections[0].animations
        self.clip = self.set.clips[0]; self.rig = self.set.rigs[0]


    def test_noop_and_mutation_preserve_every_other_byte(self):
        changes = {key: track.values() for key, track in self.clip.tracks.items() if track.type != 0x7112}
        self.assertEqual(self.clip.patches(changes), [])
        noop = nlg_asset.PatchSet(self.doc.source_hashes)
        out = self.root / "out" / "noop.dict"
        report = nlg_asset.write_rebuild(self.source, noop, out)
        self.assertEqual(out.with_suffix(".data").read_bytes(), self.source.with_suffix(".data").read_bytes())
        self.assertEqual(report["audit"]["changed_bytes"], 0)
        changes[(1, 0x7102)][1] = (0.25, 0, 1)
        changes[(1, 0x7103)][0] = (1, 1.5, 1)
        patches = self.clip.patches(changes, self.rig)
        ps = nlg_asset.PatchSet(self.doc.source_hashes, patches)
        self.assertEqual({p.chunk for p in patches}, {14, 15})
        result = nlg_asset.rebuild_with_patches(self.source, ps)
        self.assertEqual(result, nlg_asset.rebuild_with_patches(self.source, ps))
        self.assertEqual(result[0], self.source.read_bytes())
        copy = Archive("mutated.dict", dict_bytes=result[0], data_bytes=result[1])
        changed = anim.AnimationSet(copy).clips[0]
        self.assertEqual(changed.tracks[(1, 0x7102)].values()[1], (0.25, 0, 1))
        self.assertEqual(copy.get_chunk_bytes(17), b"unknown preserved")
        self.assertEqual(copy.get_chunk_bytes(16), bytes([255] * 3))

    def test_short_clip_format_comes_from_flags(self):
        # Both layouts occupy 8 bytes; only the flags disambiguate them.
        for flag, raw, stride, keys in [(0x12, struct.pack(">4h", 0, 0, 0, 32767), 8, 1),
                                       (0, struct.pack(">4b", 0, 0, 0, 127), 4, 2),
                                       (0x20, bytes((0, 0, 0, 0, 7, 255)), 6, 2),
                                       (1, struct.pack(">h", 2048), 2, 2)]:
            d, data = animation_bytes(frames=2, rotation_flag=flag, rotation=raw)
            a = Archive("short.dict", dict_bytes=d, data_bytes=data)
            track = anim.AnimationSet(a).clips[0].tracks[(1, 0x7101)]
            self.assertEqual((track.stride, track.keys), (stride, keys))
            values = track.values(); self.assertEqual(track.patches(values), [])
            values[0] = (0, 0, math.sin(0.1), math.cos(0.1))
            patches = track.patches(values); self.assertTrue(patches)
            self.assertTrue(all(p.offset + len(p.new) <= stride for p in patches))

    def test_unsafe_values_and_resizing_are_rejected(self):
        cases = [((1, 0x7102), [(0, 0, 1)]), ((1, 0x7102), [(float("nan"), 0, 0)] * 3),
                 ((1, 0x7102), [(1e100, 0, 0)] * 3), ((1, 0x7103), [(-1, 1, 1)]),
                 ((1, 0x7103), [(32, 1, 1)]), ((1, 0x7112), [(0,)] * 3),
                 ((2, 0x7101), [(0.1, 0, 0, 1)]), ((1, 0x7101), [(0, 0, 0, 0)] * 3),
                 ((1, 0x7101), [(1e308, 1e308, 1e308, 1e308)] * 3),
                 ((0, 0x7101), [(0, 0, 0, 1)])]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaises(anim.AnimationError): self.clip.patches({key: value})
        with self.assertRaises(anim.AnimationError): self.rig.bone_parents([101, 101])
        with self.assertRaises(anim.AnimationError): self.rig.bone_parents([999])

    def test_malformed_directories_lengths_flags_and_cycles(self):
        cases = [lambda a: a.chunks[11].__setitem__(4, 100000),
                 lambda a: a.chunks[11].__setitem__(3, 6),
                 lambda a: a.chunks[13].__setitem__(3, 7),
                 lambda a: a.chunks[12].__setitem__(4, 13),
                 lambda a: a.chunks[9].__setitem__(3, 4)]
        for mutate in cases:
            a = po_archive.load_archive(self.source); mutate(a)
            self.assertTrue(anim.AnimationSet(a).issues)
        a = po_archive.load_archive(self.source)
        offset = a.chunks[3][4]
        a.blocks[0][offset:offset + 12] = struct.pack(">3i", -1, 2, 1)
        self.assertTrue(anim.AnimationSet(a).issues)

    def test_aliased_payload_edit_is_locked(self):
        a = po_archive.load_archive(self.source)
        a.chunks.append([0, 0, 0x9999, 2, a.chunks[15][4]])
        track = anim.AnimationSet(a).clips[0].tracks[(1, 0x7103)]
        with self.assertRaises(anim.AnimationError): track.patches([(1.5, 1, 1)])



class TrackResizeTests(unittest.TestCase):
    """Whole-track re-encoding: static <-> animated keys and rotation layouts, audited relayout."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root / "src").mkdir()
        self.source = write_animation(self.root / "src", pad=True)
        self.doc = nlg_asset.AssetDocument(self.source)
        self.clip = self.doc.sections[0].animations.clips[0]; self.rig = self.doc.sections[0].animations.rigs[0]

    def rebuild(self, changes, layouts=None):
        import nlg_container
        a = self.doc.sections[0].archive
        patches, resized = self.clip.edit(changes, self.rig, layouts)
        ps = nlg_asset.PatchSet(self.doc.source_hashes, patches, resizes=[nlg_container.resize(a, 0, ri, raw, label) for ri, raw, label in resized])
        first = nlg_asset.rebuild_with_patches(self.source, ps)
        self.assertEqual(first, nlg_asset.rebuild_with_patches(self.source, ps))
        copy = Archive("out.dict", dict_bytes=first[0], data_bytes=first[1])
        return ps, first[2], copy, anim.AnimationSet(copy).clips[0]

    def test_noop_reencode_reproduces_every_track(self):
        for key, track in self.clip.tracks.items():
            if track.type == 0x7112: continue
            self.assertEqual(track.reencode(track.values()), (track.raw, track.flag))
        self.assertEqual(self.clip.edit({k: t.values() for k, t in self.clip.tracks.items() if t.type != 0x7112}), ([], []))

    def test_static_animated_and_layout_changes(self):
        cases = [({(1, 0x7102): [(0, 0, 1)]}, None, (1, 0x7102), 1, 0x04),                         # animated -> static
                 ({(1, 0x7103): [(1, 1, 1), (1.5, 1, 1), (2, 1, 1)]}, None, (1, 0x7103), 3, 0x08),  # static -> animated
                 ({}, {(1, 0x7101): "s8"}, (1, 0x7101), 3, 0x10),
                 ({}, {(1, 0x7101): "s12"}, (1, 0x7101), 3, 0x30),
                 ({(2, 0x7101): [(0, 0, math.sin(t / 4), math.cos(t / 4)) for t in range(3)]}, {(2, 0x7101): "s16"}, (2, 0x7101), 3, 0x13)]
        before = Archive("in.dict", dict_bytes=self.source.read_bytes(), data_bytes=self.source.with_suffix(".data").read_bytes())
        for changes, layouts, key, keys, bits in cases:
            with self.subTest(key=key, layouts=layouts):
                ps, audit, copy, clip = self.rebuild(changes, layouts)
                track = clip.tracks[key]; node = key[0]
                self.assertEqual(track.keys, keys)
                self.assertEqual(clip.flags[node] ^ self.clip.flags[node], bits)
                self.assertEqual([f for i, f in enumerate(clip.flags) if i != node], [f for i, f in enumerate(self.clip.flags) if i != node])
                for (k, t), (k2, t2) in zip(sorted(self.clip.tracks.items()), sorted(clip.tracks.items())):
                    if k != key: self.assertEqual(t.raw, t2.raw)
                expected = changes.get(key, self.clip.tracks[key].values())
                for got, want in zip(track.values(), expected):
                    self.assertTrue(all(abs(g - w) < 2e-2 for g, w in zip(got, want)), (got, want))
                self.assertEqual(copy.get_chunk_bytes(17), b"unknown preserved")
                self.assertEqual([r[:3] for r in copy.chunks], [r[:3] for r in before.chunks])
                self.assertEqual(audit["audit_mode"], "section content identity with relocation")
        # Static values reuse the source key bytes when an animated track holds the same value.
        _, _, _, clip = self.rebuild({(1, 0x7103): [(1, 1, 1)] * 3})
        self.assertEqual(clip.tracks[(1, 0x7103)].raw, self.clip.tracks[(1, 0x7103)].raw * 3)

    def test_resize_refusals(self):
        cases = [({(1, 0x7102): [(0, 0, 1)] * 2}, None, "frame count is fixed"),
                 ({(1, 0x7112): [(0,)]}, None, "unproved"), ({}, {(1, 0x7102): "s8"}, "alternative layouts"),
                 ({}, {(1, 0x7101): "s4"}, "alternative layouts"), ({(1, 0x7101): [(0.3, 0, 0, 1)]}, {(1, 0x7101): "hinge"}, "local Z"),
                 ({(0, 0x7102): [(0, 0, 0)]}, None, "absent"), ({(1, 0x7102): [(float("inf"), 0, 0)]}, None, "finite"),
                 ({(1, 0x7102): "bad"}, None, "list")]
        for changes, layouts, message in cases:
            with self.subTest(message):
                with self.assertRaisesRegex(anim.AnimationError, message): self.clip.edit(changes, self.rig, layouts)
        self.rig.signature ^= 1
        with self.assertRaises(anim.AnimationError): self.clip.edit({(1, 0x7102): [(0, 0, 1)]}, self.rig)
        # Unpadded synthetic blocks would not survive a no-op relayout, so the container refuses them.
        (self.root / "raw").mkdir(); raw = write_animation(self.root / "raw")
        doc = nlg_asset.AssetDocument(raw); clip = doc.sections[0].animations.clips[0]
        import nlg_container
        patches, resized = clip.edit({(1, 0x7102): [(0, 0, 1)]})
        ps = nlg_asset.PatchSet(doc.source_hashes, patches, resizes=[nlg_container.resize(doc.sections[0].archive, 0, ri, b, l) for ri, b, l in resized])
        with self.assertRaisesRegex(ValueError, "relayout"): nlg_asset.rebuild_with_patches(raw, ps)



class PrivateAnimationTests(unittest.TestCase):
    def test_fixture_rigs_and_fixed_size_scale_edit(self):
        root = fixtures.fixture_root()
        if root is None: self.skipTest("PO_FIXTURE_ROOT not set or fixture manifest differs")
        doc = nlg_asset.AssetDocument(root / "characters/bearhugger.dict")
        s = doc.sections[0].animations
        self.assertFalse(s.issues)
        clip = next(c for c in s.clips if any(t.type == 0x7103 for t in c.tracks.values()))
        rig = s.rig_for(clip)
        key, track = next((k, t) for k, t in clip.tracks.items() if t.type == 0x7103)
        values = track.values(); v = values[0]; values[0] = (v[0] + 1 / 2048, v[1], v[2])
        ps = nlg_asset.PatchSet(doc.source_hashes, clip.patches({key: values}, rig))
        result = nlg_asset.rebuild_with_patches(doc.path, ps)
        self.assertTrue(0 < result[2]["changed_bytes"] <= 2)

    def test_fixture_static_rotation_becomes_animated(self):
        root = fixtures.fixture_root()
        if root is None: self.skipTest("PO_FIXTURE_ROOT not set or fixture manifest differs")
        import nlg_container
        doc = nlg_asset.AssetDocument(root / "characters/bearhugger.dict"); a = doc.sections[0].archive
        s = doc.sections[0].animations
        clip = next(c for c in s.clips if c.frames > 10 and any(t.type == 0x7101 and t.keys == 1 for t in c.tracks.values()))
        key, track = next((k, t) for k, t in sorted(clip.tracks.items()) if t.type == 0x7101 and t.keys == 1)
        q = track.values()[0]; wobble = anim._quat((q[0] + 0.05, q[1], q[2], q[3]))
        values = [q] * clip.frames; values[clip.frames // 2] = wobble
        patches, resized = clip.edit({key: values}, s.rig_for(clip))
        ps = nlg_asset.PatchSet(doc.source_hashes, patches, resizes=[nlg_container.resize(a, 0, ri, raw, label) for ri, raw, label in resized])
        first = nlg_asset.rebuild_with_patches(doc.path, ps)
        self.assertEqual(first, nlg_asset.rebuild_with_patches(doc.path, ps))
        copy = Archive("out.dict", dict_bytes=first[0], data_bytes=first[1])
        after = anim.AnimationSet(copy); changed = after.clips[clip.index]
        self.assertFalse(after.issues); self.assertEqual(changed.tracks[key].keys, clip.frames)
        self.assertEqual(changed.tracks[key].raw[:track.stride], track.raw)
        for other, old in zip(after.clips, s.clips):
            for k, t in old.tracks.items():
                if (other.index, k) != (clip.index, key): self.assertEqual(other.tracks[k].raw, t.raw)
        self.assertEqual({c["chunk"] for c in first[2]["changed_chunks"]}, {track.chunk, next(i for i in clip.chunks if a.chunks[i][2] == 0x7003)})


if __name__ == "__main__": unittest.main()
