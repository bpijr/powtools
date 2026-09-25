"""Morph target names and the clip-list vote that orders a model's unnamed channels."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
import nlg_morph


class MorphNameTests(unittest.TestCase):
    def test_known_names_match_the_hashes_found_in_retail_clips(self):
        for h, name in ((0xEEC98792, "blink_r_top"), (0xFCFC8B6E, "blink_l_bottom"),
                        (0xE745B5DE, "dk_laugh"), (0xA1CED7FA, "pantsdrop"), (0x5C2246F5, "damage_stache")):
            self.assertEqual(nlg_morph.target_name(h), name)
        self.assertEqual(nlg_morph.target_name(0x0C1D2CEE), "target_0C1D2CEE")
        self.assertIsNone(nlg_morph.target_name(None))

    def test_vote_takes_the_common_position_and_never_reuses_a_hash(self):
        full = [1, 2, 3, 4]
        lists = [full] * 5 + [[1, 2, 4, 3]] + [[9]] + [[]]
        self.assertEqual(nlg_morph.canonical_targets(lists, 5), [1, 2, 3, 4, None])
        self.assertEqual(nlg_morph.canonical_targets([[7, 7, 8]] * 2, 3), [7, None, 8])
        self.assertEqual(nlg_morph.canonical_targets([], 2), [None, None])

    def test_glass_joe_hurt_keys_are_his_damage_targets(self):
        import fixtures
        root = fixtures.fixture_root(("characters/glassjoe.dict",))
        if root is None: self.skipTest("PO_FIXTURE_ROOT not set or Glass Joe fixture differs")
        m = nlg_morph.Morphs(str(root / "characters/glassjoe.dict"), str(root / "hashid.bin"))
        hurt = [m.key_names(ch)[0] for ch in m.hurt_channels()]
        self.assertEqual(hurt, ["01_damage_cheek", "02_damage_eye", "03_damage_lips", "04_damage_hair", "05_damage_ear"])
        self.assertNotIn(m.chan_hashes.index(0xEEC98792), m.hurt_channels())   # blink_r_top is animated


if __name__ == "__main__":
    unittest.main()
