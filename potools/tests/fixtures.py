"""Private-fixture manifest: relative paths, sizes and SHA-256 of the real archives the
integration tests expect. The manifest is committed; the archives never are.

    python potools/tests/fixtures.py verify <art-root>      # which fixtures match this dump
    python potools/tests/fixtures.py build  <art-root>      # rewrite the manifest (maintainers only)

Tests that need real data call `fixture_root()`; it returns None (skip) unless
PO_FIXTURE_ROOT points at an art folder whose files match the manifest.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "tests" / "private_fixtures.json"

# role -> archive (.dict; its .data companion is recorded alongside). The fixture matrix
# covers one asset from every model family, not every archive.
MATRIX = {
    "fighter": "characters/glassjoe.dict",
    "fighter (repository example)": "characters/bearhugger.dict",
    "non-fighter skeletal character": "characters/referee.dict",
    "non-fighter skeletal character (two models)": "characters/doc.dict",
    "crowd model": "characters/crowd_light_1.dict",
    "prop": "characters/vs_microphone.dict",
    "belt": "characters/minorbelt.dict",
    "animated rope": "characters/ropes.dict",
    "low-poly rope": "characters/ropes_lp.dict",
    "frontend ring": "environments/feminor/gameworld.dict",
    "full gameworld": "environments/minorcircuit/gameworld.dict",
    "effects collection": "effects/effects.dict",
    "cinematic with embedded models": "NIS/Transitions/round_transition_1.dict",
    "DK crowd material layout": "characters/crowd_dk.dict",
    "sliding mask material layout": "environments/femajor/gameworld.dict",
    "diffuse material with local textures": "environments/majorcircuit/gameworld.dict",
    "fighter behavior script bundle": "characterdefinitions/behaviours.bun.dict",
    "fighter combo graph": "characterdefinitions/glassjoe/glassjoecombodata.dict",
    "fighter action script": "characterdefinitions/glassjoe/glassjoeaction.dict",
    "fighter reaction script": "characterdefinitions/glassjoe/glassjoereaction.dict",
    "fighter HUD tip script": "characterdefinitions/glassjoe/glassjoehudtips.dict",
    "whole cutscene (shots, props, two actors)": "NIS/GlassJoe/knockout.dict",
    "cutscene co-star": "characters/littlemac.dict",
}


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build(root, matrix=MATRIX):
    root = Path(root).resolve(); entries = []
    for role, rel in matrix.items():
        files = {}
        for suffix in (".dict", ".data"):
            p = root / Path(rel).with_suffix(suffix)
            files[suffix[1:]] = {"bytes": p.stat().st_size, "sha256": _sha(p)}
        entries.append({"role": role, "path": rel, "files": files})
    return {"format": "po-private-fixtures", "version": 1,
            "notice": "Paths, sizes and hashes only. Supply your own dump; never commit game files.",
            "fixtures": entries}


def load(manifest=MANIFEST):
    value = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if value.get("format") != "po-private-fixtures" or value.get("version") != 1:
        raise ValueError("Unsupported fixture manifest.")
    return value


def verify(root, manifest=MANIFEST):
    """{path: "ok" | "missing" | "size mismatch" | "hash mismatch"} for every fixture."""
    root = Path(root).resolve(); result = {}
    for entry in load(manifest)["fixtures"]:
        status = "ok"
        for kind, expected in entry["files"].items():
            p = root / Path(entry["path"]).with_suffix("." + kind)
            if not p.is_file(): status = "missing"; break
            if p.stat().st_size != expected["bytes"]: status = "size mismatch"; break
            if _sha(p) != expected["sha256"]: status = "hash mismatch"; break
        result[entry["path"]] = status
    return result


def fixture_root(required=()):
    """The verified art root from PO_FIXTURE_ROOT, or None when it is unset or any required
    fixture is missing or different. Hashes are checked once per process."""
    root = os.environ.get("PO_FIXTURE_ROOT")
    if not root or not Path(root).is_dir(): return None
    global _VERIFIED
    try: status = _VERIFIED[root]
    except (NameError, KeyError):
        status = verify(root)
        _VERIFIED = {**globals().get("_VERIFIED", {}), root: status}
    wanted = required or status.keys()
    return Path(root) if all(status.get(p) == "ok" for p in wanted) else None


def fixture(path):
    """Absolute path of one verified fixture, or None."""
    root = fixture_root((path,))
    return root / path if root else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("verify", "build")); parser.add_argument("root")
    args = parser.parse_args()
    if args.action == "build":
        MANIFEST.write_text(json.dumps(build(args.root), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {MANIFEST}")
    else:
        for path, status in verify(args.root).items():
            print(f"{status:14s} {path}")
