"""Private read-only corpus audit. JSON contains counts/digests, never asset payloads.

python potools/tests/corpus/corpus_behavior_scan.py <art-root> [--report <private-json>]
Original game archives are only read. In-memory edits never write into the dump.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import nlg_behavior
import nlg_model
from nlg_asset import AssetDocument, PatchSet, rebuild_with_patches, sha256
from nlg_hash import load_hashid_bin
from nlg_pack import Archive
import po_archive


def scan(root):
    root = Path(root).resolve(); files = sorted((root / "characterdefinitions").rglob("*.dict"))
    if not files: raise ValueError("No definition archives found below this art root.")
    names = load_hashid_bin(str(root / "hashid.bin"))
    counts = Counter(); properties = Counter(); opaque = Counter(); chunks = Counter()
    tags = Counter(); supported = Counter(); modules = []; imports = []; anim = Counter()
    fingerprint = hashlib.sha256(); constant_words = set()
    for path in files:
        doc = AssetDocument(path, names); d = doc.sections[0].definitions
        assert d is not None and d.owner, (path, doc.sections[0].definition_note)
        summary = d.summary()
        counts.update({k: v for k, v in summary.items() if type(v) is int})
        assert not summary["unknown_property_tags"]
        a = d.archive; counts["archives"] += 1
        for digest in doc.source_hashes: fingerprint.update(bytes.fromhex(digest))
        dd, da, audit = rebuild_with_patches(path, PatchSet(doc.source_hashes))
        assert dd == a.orig_dict and da == a.orig_data and audit["changed_bytes"] == 0
        counts["byte_exact_noops"] += 1
        chunks.update(f"0x{a.chunks[i][2]:04X}" for i in a.find_chunks())
        for p in d.properties.values():
            tags[str(p["tag"])] += 1; properties[p["name_hash"]] += 1
        for s in d.scripts:
            modules.append(s); imports.extend(s["imports"])
            opaque["script_code_bytes"] += s["code_bytes"]
            counts["constant_words"] += len(s["constants"])
            counts["script_strings"] += len(s["strings"])
            constant_words.update(c["word_raw"] for c in s["constants"])
            counts["variables"] += sum(len(c["variables"]) for c in s["classes"])
        candidate = root / "characters" / (path.parent.name + ".dict")
        clip_index = nlg_behavior.animation_index(po_archive.load_archive(candidate)) if d.combos and candidate.is_file() else {}
        edits = []
        for ci, c in enumerate(d.combos):
            for n in c["nodes"]:
                fields = nlg_behavior.editable_fields(d, ci, n["index"])
                supported.update(fields.keys())
                for h in n["animation_hashes"]:
                    anim["slots"] += 1
                    anim["matched_slots" if h in clip_index else "unresolved_slots"] += 1
                    if len(clip_index.get(h, [])) > 1: anim["ambiguous_slots"] += 1
                if not edits and fields:
                    name = next(iter(fields)); current = fields[name]["value"]
                    value = 0.25 if current != 0.25 else 0.5
                    edits = [(ci, n["index"], name, value)]
        if edits:
            ps = nlg_behavior.edit_patchset(doc, edits)
            dd, da, audit = rebuild_with_patches(path, ps)
            assert (dd, da) == rebuild_with_patches(path, ps)[:2]
            parsed = Archive(str(path), dd, da); after = nlg_behavior.Definitions(parsed, names)
            assert after.summary() == summary and audit["changed_bytes"] > 0
            touched = {p.chunk for p in ps.patches}
            for ri in a.find_chunks():
                if ri not in touched: assert a.get_chunk_bytes(ri) == parsed.get_chunk_bytes(ri)
            counts["mutation_isolation_passes"] += 1
    module_index = {}
    for s in modules: module_index.setdefault(s["name_hash"], []).append(s)
    refs = Counter()
    for imp in imports:
        refs["script_imports"] += 1; candidates = module_index.get(imp["name_hash"], [])
        refs["resolved_script_imports"] += bool(candidates)
        for c in imp["classes"]:
            classes = [x for m in candidates for x in m["classes"] if x["name_hash"] == c["name_hash"]]
            found = {f["name_hash"] for cls in classes for f in cls["functions"]}
            refs["imported_functions"] += len(c["function_hashes"])
            refs["resolved_imported_functions"] += sum(h in found for h in c["function_hashes"])
    extra = {}
    table = root.parent / "materials" / "globaltechniques.bin"
    if table.is_file():
        raw = table.read_bytes(); rows = nlg_behavior.techniques(raw, names)
        extra["material_techniques"] = {"sha256": sha256(raw), "bytes": len(raw), "entries": len(rows),
            "known_model_shaders_matched": len({r["shader_hash"] for r in rows} & nlg_model.MATERIAL_SIZES.keys()),
            "fighter_behavior_relationship": "none established; shader hashes identify material table"}
    xml = root / "wwiseaudio" / "SoundbanksInfo.xml"
    if xml.is_file():
        raw = xml.read_bytes(); ids = {int(e.attrib["Id"]) for e in ET.fromstring(raw).iter()
                                    if e.attrib.get("Id", "").isdigit()}
        extra["audio_xml"] = {"sha256": sha256(raw), "distinct_ids": len(ids),
                              "untyped_script_constant_id_matches": len(ids & constant_words),
                              "status": "numeric matches alone do not prove audio references"}
    dol = root.parents[1] / "sys" / "main.dol"
    if dol.is_file(): extra["main_dol_sha256"] = sha256(dol.read_bytes())
    return {"format": "po-behavior-corpus-audit", "version": 1, "counts": dict(sorted(counts.items())),
            "chunk_counts": dict(sorted(chunks.items())), "property_tags": dict(tags),
            "distinct_property_keys": len(properties), "supported_float_fields": dict(supported),
            "opaque": dict(opaque), "script_references": dict(refs), "animation_references": dict(anim),
            "ordered_archive_sha256_fingerprint": fingerprint.hexdigest(),
            "hash_registry_sha256": sha256((root / "hashid.bin").read_bytes()), "primary_evidence": extra,
            "runtime_verified": False, "notice": "Counts and digests only; supply your own private corpus."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("art_root"); parser.add_argument("--report", type=Path)
    args = parser.parse_args(); report = scan(args.art_root)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report: args.report.write_text(text, encoding="utf-8")
    print(text)
    print("CORPUS_BEHAVIOR_SCAN_PASS")
