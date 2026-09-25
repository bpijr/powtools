"""Read-only geometry packing/culling audit; emits counts, never game payloads.

python potools/tests/corpus/corpus_geometry_scan.py <art>
"""
from collections import Counter
import copy
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import nlg_model as model
import nlg_asset as asset
import po_archive


def _reparse(result):
    from nlg_pack import Archive
    return Archive("rebuilt.dict", result[0], result[1])


def mesh_table(archive, ms, counts):
    """B016/B003 regenerate exactly; static sets take a whole-mesh duplicate + delete round
    trip and a deterministic deletion in memory. Refusals are counted by reason."""
    n = len(ms.meshes)
    if any(raw != archive.get_chunk_bytes(ri) for ri, raw in model.encode_model_set(ms, layout=list(range(n))).items()):
        raise ValueError(f"model set {ms.index}: material/node tables do not regenerate byte-exactly")
    counts["model sets mesh tables byte-exact"] += 1
    node = next((nd for nd in ms.nodes if nd.mesh_count > 1), None)
    if ms.skinned or node is None:
        counts["mesh table edits refused: " + ("skinned (B00B chunk per mesh)" if ms.skinned else "no node with two meshes")] += 1
        return
    first = node.first_mesh
    try:
        model.check_layout(ms, model.copies_layout(ms, [1 + (i == first) for i in range(n)]))
    except model.ModelFormatError as ex:
        if "referenced" not in str(ex): raise
        counts["mesh table edits refused: mesh name referenced by another chunk"] += 1
        return
    dup = asset.PatchSet([], layout={ms.index: [1 + (i == first) for i in range(n)]})
    one, two = asset._rebuild_topology(copy.deepcopy(archive), dup), asset._rebuild_topology(copy.deepcopy(archive), dup)
    if one[:2] != two[:2]: raise ValueError("mesh duplication is nondeterministic")
    grown = _reparse(one); gset = model.model_sets(grown)[ms.index]
    if len(gset.meshes) != n + 1 or model.culling_patches(grown): raise ValueError("duplicated mesh table did not reparse")
    back = asset._rebuild_topology(grown, asset.PatchSet([], layout={ms.index: [0 if i == first + 1 else 1 for i in range(n + 1)]}))
    restored = _reparse(back); rset = model.model_sets(restored)[ms.index]
    for ri in ms.chunk_ids():
        raw, original = restored.get_chunk_bytes(ri), archive.get_chunk_bytes(ri)
        if ri != ms.chunks[0xB006] and raw != original: raise ValueError("deleting the copy did not restore a coupled chunk")
    for m, r in zip(ms.meshes, rset.meshes):
        if [a.raw for a in m.attributes] != [a.raw for a in r.attributes]: raise ValueError("deleting the copy lost a vertex array")
    for ri in archive.find_chunks():
        if ri not in ms.chunk_ids() and restored.get_chunk_bytes(ri) != archive.get_chunk_bytes(ri):
            raise ValueError("mesh table edit changed a chunk outside its model set")
    gone = asset.PatchSet([], layout={ms.index: [0 if i == first else 1 for i in range(n)]})
    one, two = asset._rebuild_topology(copy.deepcopy(archive), gone), asset._rebuild_topology(copy.deepcopy(archive), gone)
    if one[:2] != two[:2] or len(model.model_sets(_reparse(one))[ms.index].meshes) != n - 1:
        raise ValueError("mesh deletion is nondeterministic or did not reparse")
    counts["static sets whole-mesh duplicate/delete round trips"] += 1


def skin(archive, ms, counts):
    """Retail skin rules on every vertex, then one controlled weight edit that adds a B00A bone
    to a palette, reparses, and is undone back to the original bones and weights."""
    bones = {h for h, _ in ms.bones}
    target = None
    for m in ms.meshes:
        w, i = model.skin_attributes(m)
        if w is None: continue
        model.check_skin(f"model set {ms.index} mesh {m.index}", w.raw, i.raw, m.palette, bones)
        counts["skinned meshes meet retail skin rules"] += 1
        if target is None and len(m.palette) < model.PALETTE_LIMIT and len(bones) > len(m.palette): target = m
    if target is None: return
    extra = next(h for h, _ in ms.bones if h not in target.palette)
    original = model.influences(target)[0]
    edit = model.reskin(target, model.mesh_data(target), {0: {target.palette[0]: 0.5, extra: 0.5}}, bones)
    result = asset._rebuild_topology(copy.deepcopy(archive), asset.PatchSet([], topology={ms.index: {target.index: edit}}))
    got = model.model_sets(_reparse(result))[ms.index]
    if model.influences(got.meshes[target.index])[0] != {target.palette[0]: 0.5, extra: 0.5} or got.meshes[target.index].palette[-1] != extra:
        raise ValueError("controlled skin edit did not reparse")
    undo = model.reskin(target, edit, {0: original}, bones)
    same = lambda a, b: a.keys() == b.keys() and all(abs(a[h] - b[h]) <= 1e-6 for h in a)
    # Palette order returns only when vertex 0 was not the sole user of a later entry.
    if sorted(undo.palette) != sorted(target.palette) or not all(map(same, model.influences(target, undo), model.influences(target))):
        raise ValueError("undoing the skin edit did not restore the palette and weights")
    counts["skinned sets controlled palette-extending weight edits"] += 1


def scan(root):
    counts = Counter(); failures = []
    for path in sorted(Path(root).rglob("*.dict")):
        try:
            archives = po_archive.load_sections(path); counts["archives"] += 1
            for archive in archives:
                sets = model.model_sets(archive)
                for ms in sets:
                    encoded = model.encode_model_set(ms)
                    if any(raw != archive.get_chunk_bytes(ri) for ri, raw in encoded.items()):
                        raise ValueError(f"model set {ms.index}: packing is not byte-exact")
                    counts["model sets byte-exact"] += 1
                    for mesh in ms.meshes:
                        model.check_mesh_data(mesh, model.mesh_data(mesh))
                        counts["meshes validated"] += 1
                        for attr in mesh.attributes:
                            if (attr.type, attr.stride) in ((0x16,4),(0x17,4),(0x26,4),(0xFC,4)):
                                counts[f"UV {attr.type:02X}/{attr.stride} shader {mesh.shader:08X}"] += 1
                tree = model.culling_tree(archive)
                if model.culling_patches(archive, sets): raise ValueError("culling boxes differ from geometry bounds")
                counts["culling boxes byte-exact"] += len(tree)
                for ms in sets:
                    for typ, ri in ms.trailing:
                        if typ == 0xB00C:
                            raw = archive.get_chunk_bytes(ri)
                            if model.encode_morphs(model.parse_morphs(raw)) != raw: raise ValueError("morph roundtrip differs")
                            counts["morph chunks byte-exact"] += 1
                    # One controlled add/delete/duplicate edit per set, entirely in memory.
                    mesh = next((m for m in ms.meshes if m.triangles()), None)
                    if mesh is not None:
                        source = list(range(mesh.vertex_count)) + [0]
                        edited = model.remap_mesh(mesh,source,mesh.triangles(),{("position",0):mesh.attribute("position").values()+[mesh.attribute("position").values()[0]]})
                        edited.strip = list(mesh.strip)
                        ps = asset.PatchSet([],topology={ms.index:{mesh.index:edited}})
                        first = asset._rebuild_topology(copy.deepcopy(archive),ps)
                        second = asset._rebuild_topology(copy.deepcopy(archive),ps)
                        if first[:2] != second[:2]: raise ValueError("controlled topology edit is nondeterministic")
                        result = copy.deepcopy(archive)
                        asset._relocate_chunks(result,dict(model.rebuild_chunks(ms,{mesh.index:edited})))
                        parsed = model.model_sets(result)
                        if parsed[ms.index].meshes[mesh.index].vertex_count != len(source): raise ValueError("edited count did not reparse")
                        restored = model.remap_mesh(parsed[ms.index].meshes[mesh.index],list(range(mesh.vertex_count)),mesh.triangles())
                        restored.strip = list(mesh.strip)
                        for ri,raw in model.encode_model_set(parsed[ms.index],{mesh.index:restored}).items():
                            original = archive.get_chunk_bytes(ri)
                            if ri != ms.chunks[0xB006]:
                                if raw != original: raise ValueError("delete after duplicate changed a non-vertex coupled chunk")
                            else:
                                # Regenerated alignment filler can lose an unused suffix when
                                # all gaps get shorter. Vertex payloads must still restore exactly.
                                if len(raw) != len(original): raise ValueError("restored vertex chunk size differs")
                                for m in ms.meshes:
                                    for attr in m.attributes:
                                        if raw[attr.offset:attr.offset+len(attr.raw)] != attr.raw:
                                            raise ValueError("delete after duplicate did not restore an original vertex array")
                        counts["model sets controlled topology edits"] += 1
                    mesh_table(archive, ms, counts)
                    skin(archive, ms, counts)
        except Exception as ex:
            failures.append((str(path.relative_to(root)), str(ex)))
    return counts, failures


if __name__ == "__main__":
    counts, failures = scan(Path(sys.argv[1]).resolve())
    for key, count in sorted(counts.items()): print(key, count)
    for path, error in failures[:30]: print("FAIL", path, error)
    if failures:
        print("CORPUS_GEOMETRY_SCAN_FAIL", len(failures)); sys.exit(1)
    print("CORPUS_GEOMETRY_SCAN_PASS")
