"""Read-only full-corpus effect/reference scan and in-memory mutation audit.

python potools/tests/corpus/corpus_effect_scan.py <art-root>
No files are written. Counts only; no game payloads are published.
"""
import collections
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import nlg_model
from nlg_pack import Archive
from nlg_effect import Effects, ResourceIndex, NONE
import po_archive


def scan(root):
    counts = collections.Counter(); failures = []; effects = []; resources = ResourceIndex()
    for path in sorted(Path(root).rglob("*.dict")):
        try:
            sections = po_archive.load_sections(path)
            counts["archives"] += 1
            for si, a in enumerate(sections):
                resources.add(a, si)
                if any(c[2] >> 8 == 0x40 for c in a.chunks): effects.append((path, si, a))
        except (ValueError, OSError) as ex: failures.append((path.name, str(ex)))
    for path, si, a in effects:
        fx = Effects(a)
        counts["effect archives"] += 1
        counts["unowned particle records"] += len(fx.unowned)
        if fx.unowned: failures.append((path.name, "unowned particle records"))
        if (a.build_dict(), a.build_data()) != (a.orig_dict, a.orig_data): failures.append((path.name, "no-op mismatch"))
        for group in fx.groups:
            counts["groups"] += 1; counts["layout " + group.layout] += 1
            if group.reason:
                failures.append((path.name, group.reason)); continue
            counts["binding sets"] += len(group.binding_sets)
            counts["bindings"] += sum(len(s.bindings) for s in group.binding_sets)
            for e in group.emitters:
                counts["emitters"] += 1; counts["emitter " + e.layout] += 1
                counts["atlas cells " + str(e.atlas_cells)] += 1
                counts["parameters"] += len(e.parameters)
                counts["curves"] += sum(p.curved for p in e.parameters)
                counts["curve segments"] += sum(len(p.segments) for p in e.parameters)
                refs = resources.references(e)
                counts["texture references resolved" if refs["textures"] else "texture references unresolved"] += 1
                if e.model_hash != NONE:
                    counts["model references resolved" if refs["models"] else "model references unresolved"] += 1
                choices = fx.texture_candidates(e.chunk)
                if not choices:
                    counts["texture swaps refused"] += 1; continue
                counts["texture swaps eligible"] += 1
                assert not fx.texture_patches(e.chunk, e.texture_hash, e.texture_hash)
                choices = [t for t in choices if t.hash != e.texture_hash]
                if not choices: counts["no distinct compatible local texture"] += 1; continue
                patches = fx.texture_patches(e.chunk, e.texture_hash, choices[0].hash)
                copy = Archive(str(path), a.orig_dict, a.orig_data)
                nlg_model.apply_patches(copy, patches)
                out = copy.build_data()
                assert out == copy.build_data() and copy.build_dict() == a.orig_dict
                assert copy.build_chunk_table() == a.build_chunk_table()
                for ri in a.find_chunks():
                    old, new = a.get_chunk_bytes(ri), copy.get_chunk_bytes(ri)
                    if ri == e.chunk: assert old[:56] == new[:56] and old[60:] == new[60:]
                    else: assert old == new
                # Audit the full data image too: chunk alignment/gaps cannot be altered.
                start = sum(len(b) for b in a.blocks[:a._chunk_block(e.chunk)]) + a.chunks[e.chunk][4] + 56
                assert out[:start] == a.orig_data[:start] and out[start + 4:] == a.orig_data[start + 4:]
                parsed = Effects(Archive(str(path), copy.build_dict(), out))
                assert parsed.emitter(e.chunk).texture_hash == choices[0].hash
                reverse = parsed.texture_patches(e.chunk, choices[0].hash, e.texture_hash)
                nlg_model.apply_patches(copy, reverse)
                assert copy.build_data() == a.orig_data
                counts["isolated texture mutation and inverse audits"] += 1
    return counts, failures


if __name__ == "__main__":
    counts, failures = scan(Path(sys.argv[1]).resolve())
    for key, value in sorted(counts.items()): print(f"{key:48s} {value}")
    for failure in failures[:20]: print("FAIL", *failure)
    if failures:
        print("CORPUS_EFFECT_SCAN_FAIL", len(failures)); sys.exit(1)
    print("CORPUS_EFFECT_SCAN_PASS")
