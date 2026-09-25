"""Capability states and private-fixture manifest. Synthetic bytes only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
import po_capability as capability
import po_archive
import fixtures


class CapabilityTests(unittest.TestCase):
    def test_states_are_canonical_and_evidenced(self):
        families = [c.family for c in capability.REGISTRY]
        self.assertEqual(len(families), len(set(families)))
        for c in capability.REGISTRY:
            self.assertTrue(c.states <= set(capability.STATES), c.family)
            # Claims that go beyond reading bytes need a pointer to their proof.
            for state in (capability.RUNTIME, capability.BLENDER):
                if state in c.states:
                    self.assertIn(state, c.evidence, f"{c.family}: {state} without evidence")

    def test_status_never_implies_runtime(self):
        c = capability.cap("x", (capability.IDENTIFIED, capability.EDITABLE), "n")
        self.assertEqual(c.status(), "Identified, Editable offline; runtime unverified")
        self.assertIn("runtime verified", capability.by_family("Textures (CMPR)").status())
        self.assertEqual(capability.cap("y", (), "n").status(), "Not decoded; runtime unverified")
        with self.assertRaises(ValueError): capability.cap("z", ("Decoded",), "n")



class FixtureManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / "characters").mkdir()
        for suffix, data in ((".dict", b"d" * 88), (".data", b"payload")):
            (self.root / "characters" / ("prop" + suffix)).write_bytes(data)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps(fixtures.build(self.root, {"prop": "characters/prop.dict"})))

    def tearDown(self): self.tmp.cleanup()

    def test_manifest_has_no_game_bytes(self):
        text = self.manifest.read_text()
        self.assertNotIn("payload", text)
        self.assertEqual(fixtures.verify(self.root, self.manifest), {"characters/prop.dict": "ok"})

    def test_detects_changed_or_missing_fixtures(self):
        (self.root / "characters" / "prop.data").write_bytes(b"PAYLOAD")
        self.assertEqual(fixtures.verify(self.root, self.manifest)["characters/prop.dict"], "hash mismatch")
        (self.root / "characters" / "prop.data").write_bytes(b"longer payload")
        self.assertEqual(fixtures.verify(self.root, self.manifest)["characters/prop.dict"], "size mismatch")
        (self.root / "characters" / "prop.data").unlink()
        self.assertEqual(fixtures.verify(self.root, self.manifest)["characters/prop.dict"], "missing")

    def test_fixture_root_requires_env_and_match(self):
        with patch.dict(os.environ, {"PO_FIXTURE_ROOT": ""}):
            self.assertIsNone(fixtures.fixture_root())
        committed = fixtures.load()
        self.assertEqual(len({e["path"] for e in committed["fixtures"]}), len(fixtures.MATRIX))
        self.assertTrue(all(len(f["sha256"]) == 64 for e in committed["fixtures"] for f in e["files"].values()))


if __name__ == "__main__": unittest.main()
