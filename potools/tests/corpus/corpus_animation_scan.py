"""Read-only corpus audit; prints counts, never game payloads or generated game files.

python potools/tests/corpus/corpus_animation_scan.py <art> [--layout-only] [--dol <main.dol>]
The full scan decodes every key and proves no-op patches empty; all sections must rebuild
byte-identically. Optional DOL verification reports the executable hash and checks the
static scale/scalar constants and the known dispatch/loader anchors, without disassembly.
"""
import argparse
from collections import Counter
import hashlib
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import nlg_animation as anim
import nlg_model
import po_archive


def scan(root, layout_only=False):
    counts, failures = Counter(), []
    for path in sorted(root.rglob("*.dict")):
        counts["archives"] += 1
        for a in po_archive.load_sections(path):
            counts["sections"] += 1
            if a.build_dict() != a.orig_dict or a.build_data() != a.orig_data:
                failures.append((str(path.relative_to(root)), "section no-op rebuild differs"))
            s = anim.AnimationSet(a)
            for issue in s.issues:
                failures.append((str(path.relative_to(root)), issue))
            if len(s.rigs) != len(a.find_chunks(type_id=0x8001)) or len(s.clips) != len(a.find_chunks(type_id=0x7001)):
                failures.append((str(path.relative_to(root)), "directory/header coverage mismatch"))
            counts["rigs"] += len(s.rigs); counts["clips"] += len(s.clips)
            for ms in nlg_model.model_sets(a):
                if not ms.bones: continue
                try:
                    s.rig_for_bones([h for h, _ in ms.bones])
                    counts["bind sets with unique local hierarchy"] += 1
                except anim.AnimationError:
                    counts["bind sets needing external/ambiguous hierarchy"] += 1
            for clip in s.clips:
                try:
                    s.rig_for(clip); counts["clips with compatible local rig"] += 1
                except anim.AnimationError:
                    counts["clips needing external/incompatible rig"] += 1
                counts["unknown child tracks preserved"] += len(clip.unknown)
                for track in clip.tracks.values():
                    label = "%s %d-byte %s tracks" % (track.kind, track.stride, "static" if track.keys == 1 else "animated")
                    counts[label] += 1; counts["keys"] += track.keys
                    if layout_only: continue
                    try:
                        values = track.values()
                        if track.type != 0x7112:
                            if track.patches(values):
                                failures.append((str(path.relative_to(root)), "no-op track patch differs"))
                            counts["writable tracks with byte-exact no-op"] += 1
                        else:
                            counts["scalar tracks decoded read-only"] += 1
                    except anim.AnimationError as ex:
                        failures.append((str(path.relative_to(root)), str(ex)))
        if counts["archives"] % 100 == 0:
            print("audited", counts["archives"], "archives", flush=True)
    return counts, failures


def verify_dol(path):
    from nlg_dol import Dol
    d = Dol(str(path))
    assert struct.unpack(">f", d.read(0x8041e2c0 - 0x64f0, 4))[0] == 1 / 2048
    assert struct.unpack(">f", d.read(0x8041e2c0 - 0x64e0, 4))[0] == 255
    assert struct.unpack(">3f", d.read(0x80341dd8, 12)) == (1, 1, 1)
    assert d.word(0x80181a48) & 0xFFFF == 0x7103
    assert d.word(0x80181a6c) & 0xFFFF == 0x7112
    # Unsigned halfword scale components, unsigned byte scalar component.
    assert all(d.word(address) >> 26 == 40 for address in (0x80188c28, 0x80188c30, 0x80188c38))
    assert d.word(0x80188c94) >> 26 == 34
    print("ANIMATION_DOL_ANCHORS_PASS", hashlib.sha256(d.d).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--layout-only", action="store_true")
    parser.add_argument("--dol", type=Path)
    args = parser.parse_args()
    counts, failures = scan(args.root, args.layout_only)
    for key, value in sorted(counts.items()): print("%-54s %d" % (key, value))
    for failure in failures[:20]: print("FAIL", failure)
    if failures:
        print("CORPUS_ANIMATION_SCAN_FAIL", len(failures)); sys.exit(1)
    if args.dol: verify_dol(args.dol)
    print("CORPUS_ANIMATION_LAYOUT_PASS" if args.layout_only else "CORPUS_ANIMATION_SCAN_PASS")
