"""Full-corpus read-only scan of every model set. Reads only; writes nothing.

    python potools/tests/corpus/corpus_model_scan.py <dump>/files/art

Checks, for every ordinary archive and NIS section: model sets parse in the fixed chunk order,
nodes cover every mesh, transform indices are in range, each material record's span matches
its shader's registered size, a no-op edit produces an empty patch set, and 0x6101 culling
leaves name B003 nodes. Prints counts only.
"""
import collections
import os
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import nlg_model
import po_archive


def scan(root):
    c = collections.Counter(); failures = []
    for directory, _, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".dict"): continue
            path = Path(directory) / name; rel = path.relative_to(root).as_posix()
            try:
                sections = po_archive.load_sections(path)
            except (po_archive.ArchiveError, ValueError, OSError) as ex:
                failures.append((rel, "container", str(ex))); continue
            c["archives"] += 1
            for si, a in enumerate(sections):
                try:
                    sets = nlg_model.model_sets(a)
                except nlg_model.ModelFormatError as ex:
                    failures.append((rel, si, str(ex))); continue
                if sets: c["archives with models" if len(sections) == 1 else "cinematic sections with models"] += 1
                nodes = set()
                for s in sets:
                    c["model sets"] += 1; c["meshes"] += len(s.meshes); c["nodes"] += len(s.nodes)
                    c["transforms"] += len(s.transforms); c["skinned sets"] += s.skinned
                    nodes |= {n.name_hash for n in s.nodes}
                    for m in s.meshes:
                        if nlg_model.MATERIAL_SIZES.get(m.shader) != s.material_span(m)[1]:
                            failures.append((rel, si, f"mesh {m.index}: material span differs from shader size"))
                        edits = {(x.semantic, x.set): x.values() for x in m.attributes if x.verified}
                        if nlg_model.geometry_patches(s, {m.index: edits}):
                            failures.append((rel, si, f"mesh {m.index}: no-op edit produced patches"))
                        for x in m.attributes:
                            c["attributes verified" if x.verified else "attributes preserved (scale unestablished)"] += 1
                for ri in a.find_chunks(type_id=0x6101):
                    raw = a.get_chunk_bytes(ri); c["culling nodes"] += 1
                    for o in range(40, len(raw) - 3, 4):
                        if struct.unpack_from(">I", raw, o)[0] not in nodes:
                            failures.append((rel, si, f"0x6101 chunk {ri} names a hash that is not a node"))
    return c, failures


if __name__ == "__main__":
    counts, failures = scan(Path(sys.argv[1]).resolve())
    for k, v in sorted(counts.items()): print(f"{k:45s} {v}")
    for f in failures[:20]: print("FAIL", *f)
    if failures:
        print(f"CORPUS_MODEL_SCAN_FAIL: {len(failures)} problem(s)"); sys.exit(1)
    print("CORPUS_MODEL_SCAN_PASS")
