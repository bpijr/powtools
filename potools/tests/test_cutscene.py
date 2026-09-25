"""Whole-cutscene codec (nlg_cutscene): edit rules on synthetic clips, and with PO_FIXTURE_ROOT
the real Glass Joe knockout (shots, timeline, actor resolution, a single-key round trip) plus a
parse of every NIS container in the dump."""
from pathlib import Path
import glob
import os
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
import nlg_asset
import nlg_cutscene as cut
import fixtures


class FakeTrack:
    def __init__(self, type_, keys): self.type = type_; self._keys = keys
    def values(self): return list(self._keys)


class FakeClip:
    def __init__(self, frames, tracks, name="clip"):
        self.frames, self.name, self.node_count = frames, name, 2
        self.tracks = {k: FakeTrack(k[1], v) for k, v in tracks.items()}


class EditRules(unittest.TestCase):
    def setUp(self):
        self.clip = FakeClip(3, {(1, cut.ROTATION): [(0.0, 0.0, 0.0, 1.0)],                          # static
                                 (1, cut.TRANSLATION): [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0)]})
        self.rest = [(0.0, 0.0, 0.0), (0.5, 0.0, 0.0)]

    def test_values_within_tolerance_keep_their_source_bytes(self):
        near = {(1, cut.TRANSLATION): [(0.0, 0.0, 0.00001), (1.00001, 0.0, 0.0), (2.0, 0.0, 0.0)],
                (1, cut.ROTATION): [(0.0, 0.0, 0.0001, 1.0)] * 3}
        self.assertEqual(cut.clip_changes(self.clip, near, self.rest), {})

    def test_changed_frame_only_and_static_track_becomes_per_frame(self):
        import math
        turned = (0.0, 0.0, math.sin(0.25), math.cos(0.25))
        changes = cut.clip_changes(self.clip, {(1, cut.TRANSLATION): [(0.0, 0.0, 0.0), (1.0, 0.5, 0.0), (2.0, 0.0, 0.0)],
                                               (1, cut.ROTATION): [(0.0, 0.0, 0.0, 1.0), turned, (0.0, 0.0, 0.0, 1.0)]}, self.rest)
        self.assertEqual(changes[(1, cut.TRANSLATION)], [(0.0, 0.0, 0.0), (1.0, 0.5, 0.0), (2.0, 0.0, 0.0)])
        self.assertEqual(len(changes[(1, cut.ROTATION)]), 3)
        self.assertEqual(changes[(1, cut.ROTATION)][0], (0.0, 0.0, 0.0, 1.0))
        # a uniformly moved static track stays one key
        whole = cut.clip_changes(self.clip, {(1, cut.ROTATION): [turned] * 3}, self.rest)
        self.assertEqual(whole[(1, cut.ROTATION)], [turned])

    def test_movement_without_a_source_track_is_refused(self):
        with self.assertRaises(cut.CutsceneError):
            cut.clip_changes(self.clip, {(0, cut.TRANSLATION): [(0.0, 0.0, 1.0)] * 3}, self.rest)
        # the codec default (bind translation, identity rotation) is not a change
        self.assertEqual(cut.clip_changes(self.clip, {(0, cut.TRANSLATION): [(0.0, 0.0, 0.0)] * 3,
                                                      (0, cut.ROTATION): [(0.0, 0.0, 0.0, 1.0)] * 3}, self.rest), {})
        with self.assertRaises(cut.CutsceneError):     # frame counts are fixed
            cut.clip_changes(self.clip, {(1, cut.TRANSLATION): [(0.0, 0.0, 0.0)] * 2}, self.rest)

    def test_camera_changes(self):
        class Cam:
            name = "Camera01"
            tracks = {"position": [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)], "rotation_xyzw": [(0.0, 0.0, 0.0, 1.0)] * 2,
                      "angle_radians": [(0.5,), (0.5,)]}
        same = {k: list(v) for k, v in Cam.tracks.items()}
        self.assertEqual(cut.camera_changes(Cam, same), {})
        moved = dict(same, angle_radians=[(0.5,), (0.7,)])
        self.assertEqual(cut.camera_changes(Cam, moved), {"angle_radians": [(0.5,), (0.7,)]})
        with self.assertRaises(cut.CutsceneError): cut.camera_changes(Cam, dict(same, position=[(0.0, 0.0, 0.0)]))

    def test_order_puts_parents_first(self):
        parents = [2, -1, 1, 0]
        seen = set()
        for n in cut.order(parents):
            self.assertTrue(parents[n] == -1 or parents[n] in seen); seen.add(n)


class ArenaRuntimeInstances(unittest.TestCase):
    def test_shuffled_proxy_bindings_and_world_matrix(self):
        class Archive:
            chunks = [[0, 2, 0x6000, 216, 0]]
            def get_chunk_bytes(self, _): return bytes(raw)
        class Rig:
            index, name, node_count = 7, "fixture", 5
            hashes = (0x100, 0x101, 0x102, 0x103, 0x104)

        pos = 32
        raw = bytearray(216)
        struct.pack_into(">I", raw, pos, Rig.hashes[1])
        for i, pair in enumerate(((0xA4, Rig.hashes[4]),
                                  (0xA2, Rig.hashes[2]),
                                  (0xA3, Rig.hashes[3]))):
            struct.pack_into(">II", raw, pos + 4 + 8*i, *pair)
        matrix_at = pos + 36 + 8*Rig.node_count
        matrix = (1.0, 0.0, 0.0, 0.0,
                  0.0, 1.0, 0.0, 0.0,
                  0.0, 0.0, 1.0, 0.0,
                  3.0, 4.0, 5.0, 1.0)
        struct.pack_into(">16f", raw, matrix_at, *matrix)
        struct.pack_into(">2f", raw, matrix_at + 100, 0.3, 0.5)
        records = cut.arena_runtime_instances(Archive(), [Rig()])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["matrix"], matrix)
        self.assertEqual([(b["proxy_hash"], b["node"]) for b in records[0]["bindings"]],
                         [(0xA4, 4), (0xA2, 2), (0xA3, 3)])
        self.assertAlmostEqual(records[0]["animation_rate"], 0.3)
        self.assertAlmostEqual(records[0]["animation_phase"], 0.5)

    def test_crowd_helper_matrix_and_opaque_parameters(self):
        class Archive:
            chunks = [[0, 2, 0x6000, 192, 0]]
            def get_chunk_bytes(self, _): return bytes(raw)
        import nlg_hash
        raw = bytearray(192); pos = 20
        matrix = (1.0, 0.0, 0.0, 0.0,
                  0.0, 1.0, 0.0, 0.0,
                  0.0, 0.0, 1.0, 0.0,
                  4.5, -4.5, -1.25, 1.0)
        struct.pack_into(">7I", raw, pos, nlg_hash.string_to_hash("crowd_helper02"),
                         263, 1, 262160, 0xffffffff, 0, 14025208)
        struct.pack_into(">16f", raw, pos + 28, *matrix)
        struct.pack_into(">9f", raw, pos + 92, *range(9))
        records = cut.arena_crowd_helpers(Archive())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "crowd_helper02")
        self.assertEqual(records[0]["matrix"], matrix)
        self.assertEqual(records[0]["parameters"], tuple(float(i) for i in range(9)))

    def test_effect_placement_matrix_is_the_last_row_at_plus_80(self):
        class Archive:
            chunks = [[0, 2, 0x6000, 400, 0]]
            def get_chunk_bytes(self, _): return bytes(raw)
        import nlg_hash
        raw = bytearray(400)
        for at, pos in ((16, (-19.5, -12.25, 14.0)), (200, (3.0, 4.0, 5.0))):
            struct.pack_into(">I", raw, at, nlg_hash.string_to_hash("env_goldlens"))
            struct.pack_into(">16f", raw, at + 80, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, *pos, 1.0)
        struct.pack_into(">I", raw, 360, nlg_hash.string_to_hash("env_goldlens"))    # truncated: no room for a matrix
        found = cut.arena_effect_placements(Archive(), ["env_goldlens", "env_camera_flash_world"])
        self.assertEqual([p["position"] for p in found], [(-19.5, -12.25, 14.0), (3.0, 4.0, 5.0)])
        self.assertEqual({p["effect"] for p in found}, {"env_goldlens"})


class RealCutscene(unittest.TestCase):
    KNOCKOUT = "NIS/GlassJoe/knockout.dict"

    def setUp(self):
        self.root = fixtures.fixture_root((self.KNOCKOUT, "characters/glassjoe.dict", "characters/littlemac.dict"))
        if self.root is None: self.skipTest("PO_FIXTURE_ROOT not set or cutscene fixtures differ")

    def test_knockout_shots_timeline_and_actors(self):
        cs = cut.Cutscene(self.root / self.KNOCKOUT)
        self.assertEqual([(s.first, s.last) for s in cs.shots], [(0, 24), (25, 60), (60, 100), (101, 120), (120, 180), (180, 205)])
        self.assertEqual(cs.frames, 206)
        self.assertTrue(all(s.camera is not None and s.camera.frames == s.frames for s in cs.shots))
        by = {a.name: a for a in cs.actors if a.kind == "character"}
        self.assertEqual(os.path.basename(by["GlassJoe"].source), "glassjoe.dict")
        self.assertEqual(os.path.basename(by["LittleMac"].source), "littlemac.dict")
        props = [a for a in cs.actors if a.kind == "prop"]
        self.assertEqual(len(props), 8); self.assertTrue(all(a.model_sets for a in props))
        self.assertEqual(cs.arena_name, "minorcircuit")
        self.assertTrue(all(len(a.appearances) == len(cs.shots) for a in cs.actors))

    def test_world_circuit_effect_placements(self):
        import nlg_hash
        path = self.root / "environments" / "worldcircuit" / "gameworld.dict"
        if not path.is_file(): self.skipTest("World Circuit arena not in the dump")
        doc = nlg_asset.AssetDocument(path, nlg_hash.load_hashid_bin(str(self.root / "hashid.bin")))
        found = cut.arena_effect_placements(doc.sections[0].archive, ["env_goldlens", "env_camera_flash_world"])
        counts = {n: sum(1 for p in found if p["effect"] == n) for n in ("env_goldlens", "env_camera_flash_world")}
        self.assertEqual(counts, {"env_goldlens": 48, "env_camera_flash_world": 88})

    def test_single_key_edit_changes_exactly_that_track(self):
        cs = cut.Cutscene(self.root / self.KNOCKOUT)
        clip = cs.doc.sections[1].animations.clips[13]
        before = cut.clip_values(clip); key = (26, cut.ROTATION)
        new = list(before[key]); q = new[14]; new[14] = (q[1], q[0], q[2], q[3])
        ps = cut.patch_set(cs, {(1, 13): {key: new}})
        self.assertEqual(len(ps.patches), 1); self.assertFalse(ps.resizes)
        first = nlg_asset.rebuild_with_patches(cs.path, ps)
        self.assertEqual(first, nlg_asset.rebuild_with_patches(cs.path, ps))
        self.assertTrue(0 < first[2]["changed_bytes"] <= 8)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.dict"; out.write_bytes(first[0]); out.with_suffix(".data").write_bytes(first[1])
            after = cut.clip_values(nlg_asset.AssetDocument(out).sections[1].animations.clips[13])
        self.assertEqual([k for k in before if before[k] != after[k]], [key])
        self.assertEqual(sum(1 for a, b in zip(before[key], after[key]) if a != b), 1)
        self.assertEqual(cut.patch_set(cs, {}, {}).patches, [])

    def test_every_nis_container_parses(self):
        count = shots = 0
        for path in sorted(glob.glob(str(self.root / "NIS" / "**" / "*.dict"), recursive=True)):
            cs = cut.Cutscene(path); count += 1; shots += len(cs.shots)
            for s in cs.shots:
                for clip, _, _ in s.clips: self.assertEqual(clip.frames, s.frames)
        self.assertGreater(count, 0); self.assertGreaterEqual(shots, count)


if __name__ == "__main__":
    unittest.main()
