"""Read-only shader/material evidence scan. Prints counts, never game payloads.

    python potools/tests/corpus/corpus_material_scan.py <art-root>
"""
import argparse
import collections
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import nlg_model
import nlg_asset
import nlg_material_layout as materials
import po_archive


def family(path):
    rel = path.as_posix().lower()
    if rel.startswith("nis/"): return "cinematics"
    if rel.startswith("effects/"): return "effects"
    if rel.startswith("environments/"): return "ring and environments"
    if "crowd" in rel: return "crowds"
    if "ropes" in rel: return "ropes"
    if any(x in rel for x in ("belt", "microphone")): return "props"
    return "fighters and supporting models"


def scan(root):
    records = collections.defaultdict(list); texture_keys = set(); counts = collections.Counter()
    families = collections.defaultdict(collections.Counter); mutations = collections.Counter(); failures = []
    for path in sorted(root.rglob("*.dict")):
        try:
            sections = po_archive.load_sections(path); counts["archives"] += 1
            has_materials = any(a.find_chunks(type_id=0xB016) for a in sections)
            if has_materials: counts["archives_with_materials"] += 1
            if has_materials and len(sections) == 1:
                doc = nlg_asset.AssetDocument(path)
                empty = nlg_asset.PatchSet(doc.source_hashes)
                rebuilt = nlg_asset.rebuild_with_patches(path, empty)
                assert rebuilt == nlg_asset.rebuild_with_patches(path, empty)
                assert rebuilt[:2] == (path.read_bytes(), path.with_suffix(".data").read_bytes())
                counts["single_section_noop_rebuilds"] += 1
            for a in sections:
                counts["sections"] += 1
                try: textures = po_archive.texture_entries(a)
                except po_archive.ArchiveError: textures = []
                texture_keys.update(e.hash for e in textures)
                recs = materials.records(a)
                sets = nlg_model.model_sets(a)
                counts["model_sets"] += len(sets)
                counts["mesh_users"] += sum(len(ms.meshes) for ms in sets)
                for rec in recs:
                    assert not rec.error, rec.error
                    records[rec.shader].append(rec.raw)
                    families[rec.shader][family(path.relative_to(root))] += 1
                    counts["records"] += 1
                    counts["shared_records"] += len(rec.meshes) > 1
                    for ref in rec.references(textures):
                        assert not materials.field_patches(rec, "texture_%d" % ref["slot"], ref["hash"], textures)
                        counts["noop_texture_fields"] += 1
                        counts["local_references" if ref["indices"] else "external_or_unresolved_references"] += 1
                    for field, value in rec.values().items():
                        spec = next(f for f in rec.layout.fields if f.name == field)
                        vals = [value] if spec.count == 1 else value
                        if vals is not None and all(v is not None and 0 <= v <= spec.maximum for v in vals):
                            assert not materials.field_patches(rec, field, value)
                            counts["noop_scalar_fields"] += 1
                        else: counts["source_scalar_outside_editor_bounds"] += 1
                    if len(sections) != 1: continue
                    for field in rec.editable_fields:
                        key = "%08X:%s" % (rec.shader, field)
                        if mutations[key]: continue
                        if field.startswith("texture_"):
                            off = int(field[8:]) * 8; old = struct.unpack_from(">I", rec.raw, off)[0]
                            candidates = [e for e in textures if e.hash != old and sum(x.hash == e.hash for x in textures) == 1]
                            if not candidates: continue
                            value = candidates[0].hash
                        else:
                            spec = next(f for f in rec.layout.fields if f.name == field)
                            value = 0.25 if rec.values()[field] != 0.25 else 0.5
                            if spec.count == 3: value = [0.25, 0.5, 0.75]
                        patches = materials.patches(a, rec.chunk, rec.offset, field, value)
                        if not patches: continue
                        ps = nlg_asset.PatchSet(doc.source_hashes, patches)
                        first = nlg_asset.rebuild_with_patches(path, ps)
                        assert first == nlg_asset.rebuild_with_patches(path, ps)
                        assert first[2]["changed_bytes"] > 0
                        mutations[key] += 1
        except Exception as ex:
            failures.append({"family": family(path.relative_to(root)), "error": type(ex).__name__ + ": " + str(ex)})
    layouts = []
    for shader, recs in sorted(records.items()):
        layout = materials.LAYOUTS[shader]; refs = []
        for i in range(len(layout.slots)):
            off = i * 8; values = [struct.unpack_from(">I", r, off)[0] for r in recs]
            refs.append({"offset": off, "records": len(recs), "distinct_keys": len(set(values)),
                         "matches_corpus_texture_key": sum(v in texture_keys for v in values),
                         "disk_state_matches": sum(r[off + 4:off + 8] == b"\xff\xff\0\0" for r in recs)})
        layouts.append({"shader": "%08X" % shader, "layout": layout.name, "bytes": layout.size,
                        "records": len(recs), "families": dict(sorted(families[shader].items())), "texture_inputs": refs,
                        "outside_prefix_texture_key_matches": sum(struct.unpack_from(">I", r, off)[0] in texture_keys
                            for r in recs for off in range(len(layout.slots) * 8, layout.size, 4)),
                        "opaque_bytes_per_record": layout.size - len(layout.slots) * 8 - sum(f.count * 4 for f in layout.fields)})
    expected = sum(len(x.slots) + len(x.fields) for x in materials.LAYOUTS.values())
    if len(mutations) != expected: failures.append({"error": "Mutation coverage missing: %d/%d fields" % (len(mutations), expected)})
    counts["corpus_texture_keys"] = len(texture_keys)
    counts["mutation_fields_rebuilt_twice"] = sum(mutations.values())
    return {"format": "po-material-corpus-audit", "schema": 1, "counts": dict(sorted(counts.items())),
            "layouts": layouts, "failures": failures, "runtime_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0]); parser.add_argument("root")
    parser.add_argument("--report", help="Write counts-only JSON audit")
    args = parser.parse_args(); report = scan(Path(args.root).resolve())
    if args.report: Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"], indent=2))
    for row in report["layouts"]: print(row["layout"], row["bytes"], row["records"], "records", len(row["texture_inputs"]), "inputs")
    for failure in report["failures"][:20]: print("FAIL", failure)
    if report["failures"]: raise SystemExit("CORPUS_MATERIAL_SCAN_FAIL")
    print("CORPUS_MATERIAL_SCAN_PASS")
