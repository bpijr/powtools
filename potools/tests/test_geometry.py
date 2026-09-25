"""Topology, coupled data, culling and texture edit safety using synthetic payloads."""
from collections import Counter
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
import nlg_asset as asset
import nlg_model as model
import nlg_texture as texture
import po_archive
from test_model import model_set_payloads, write_archive
from helpers import texture_header


def canonical_payloads():
    payloads = dict(model_set_payloads())
    payloads[0xB007] = struct.pack(">6H", 0,1,2,0,1,2)
    recs = bytearray(payloads[0xB004]); struct.pack_into(">I", recs, 52, 6)
    payloads[0xB004] = bytes(recs)
    payloads[0xB006] += bytes(-len(payloads[0xB006]) % 32)
    return [(t, payloads[t]) for t in model.MAIN]


def cyclic(t): return min(t, t[1:]+t[:1], t[2:]+t[:2])


BONES = (0xB0000001, 0xB0000002, 0xB0000003)


def skinned_payloads(palettes=((BONES[0],), (BONES[0], BONES[1]))):
    """Retail-packed skinned set: two 3-vertex meshes with f32 weights (0xB0) and palette slot
    indices (0xD4, set 1 as on hippodiffuseskin), one B00B palette per mesh and three bones."""
    idx, vtx, ptrs, recs = bytearray(), bytearray(), bytearray(), bytearray()
    for m, palette in enumerate(palettes):
        istart = len(idx); idx += struct.pack(">3H", 0, 1, 2)
        if len(palette) == 1: w, i = [(1, 0, 0, 0)] * 3, [(0, 0, 0, 0)] * 3
        else: w, i = [(1, 0, 0, 0), (0.5, 0.5, 0, 0), (1, 0, 0, 0)], [(0, 0, 0, 0), (1, 0, 0, 0), (1, 0, 0, 0)]
        attrs = [(0x0A, 12, 0x100, struct.pack(">9f", 0, 0, m, 1, 0, m, 0, 1, m)),
                 (0xB0, 16, 0x500, b"".join(struct.pack(">4f", *x) for x in w)),
                 (0xD4, 4, 0x701, b"".join(bytes(x) for x in i))]
        first = len(ptrs)
        for typ, stride, flags, raw in attrs:
            vtx += bytes(-len(vtx) % 32); ptrs += struct.pack(">IBBH", len(vtx), typ, stride, flags); vtx += raw
        rec = bytearray(52)
        struct.pack_into(">IIHBB", rec, 0, istart, 3, 3, 1, len(attrs))
        struct.pack_into(">7I", rec, 12, first, 0x21DB4385, 0x1000 + m, 0, 0x000D0007, 0xD0540001, 8 * m)
        recs += rec
    vtx += bytes(-len(vtx) % 32)
    main = {0xB016: struct.pack(">II", 0xAAAA0001, 0) * 2, 0xB007: bytes(idx), 0xB006: bytes(vtx), 0xB005: bytes(ptrs),
            0xB004: bytes(recs), 0xB002: struct.pack(">16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1),
            0xB003: struct.pack(">III", 0x2000, 2, 0)}
    bones = b"".join(struct.pack(">I16f", h, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1) for h in BONES)
    return [(t, main[t]) for t in model.MAIN] + [(0xB00B, struct.pack(">%dI" % len(p), *p)) for p in palettes] + [(0xB00A, bones)]


def one_node(payloads):
    """Canonical static payloads with both meshes inside one B003 node."""
    return [(t, struct.pack(">III", 0x2000, 2, 0) if t == 0xB003 else raw) for t, raw in payloads]


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name); (self.dir/"src").mkdir()
        self.path = write_archive(self.dir/"src", canonical_payloads()+[(0xD001,b"unknown-data")])
        self.doc = asset.AssetDocument(self.path); self.ms = self.doc.sections[0].model_sets[0]

    def test_strip_preserves_winding_disconnected_shared_and_duplicate_faces(self):
        random.seed(712)
        for count in (1,2,3,20,100):
            tris = [tuple(random.sample(range(12),3)) for _ in range(count)]
            self.assertEqual(Counter(map(cyclic,tris)), Counter(map(cyclic,model.strip_to_tris(model.tris_to_strip(tris)))))
        with self.assertRaises(ValueError): model.tris_to_strip([(0,1)])

    def test_add_delete_vertices_faces_and_unknown_data_survive_rebuild(self):
        m = self.ms.meshes[0]
        d = model.remap_mesh(m,[0,1,2,0],[(0,1,2),(3,2,1)], {("position",0):[(0,0,0),(1,0,0),(0,1,0),(0,0,2)]})
        ps = asset.PatchSet(self.doc.source_hashes,topology={0:{0:d}})
        out = self.dir/"out"/"new.dict"; report = asset.write_rebuild(self.path,ps,out)
        rebuilt = asset.AssetDocument(out); changed = rebuilt.sections[0].model_sets[0]
        self.assertEqual(changed.meshes[0].vertex_count,4)
        self.assertEqual(len(changed.meshes[0].triangles()),2)
        self.assertEqual(changed.meshes[0].attributes[-1].raw, m.attributes[-1].raw + m.attributes[-1].raw[:4])
        self.assertEqual(changed.meshes[1].attributes[0].raw,self.ms.meshes[1].attributes[0].raw)
        for typ in (0xB016,0xB002,0xB003,0xD001):
            a,b = self.doc.sections[0].archive, rebuilt.sections[0].archive
            self.assertEqual(a.get_chunk_bytes(a.find_chunks(typ)[0]),b.get_chunk_bytes(b.find_chunks(typ)[0]))
        self.assertTrue(report["deterministic"])
        restored = asset.PatchSet.from_dict(ps.as_dict())
        self.assertEqual(asset.rebuild_with_patches(self.path,restored)[:2],(out.read_bytes(),out.with_suffix(".data").read_bytes()))
        reduced = model.remap_mesh(changed.meshes[0],[3,2,1],[(0,1,2)])
        out2 = self.dir/"reduced.dict"
        asset.write_rebuild(out,asset.PatchSet(rebuilt.source_hashes,topology={0:{0:reduced}}), self.dir/"elsewhere"/"reduced.dict")

    def test_refuses_missing_provenance_unknown_changes_bad_indices_and_nonretail_packing(self):
        m = self.ms.meshes[0]
        for source,tris in (([0,1,99],[(0,1,2)]),([0,1,2],[(0,1,99)]),([0,1,2],[])):
            with self.assertRaises(ValueError): model.remap_mesh(m,source,tris)
        d = model.remap_mesh(m,[0,1,2,0],[(0,1,2)])
        d.source = None
        with self.assertRaises(ValueError): model.check_mesh_data(m,d)
        d = model.mesh_data(m); d.arrays[-1] = bytes(len(d.arrays[-1]))
        with self.assertRaises(ValueError): model.check_mesh_data(m,d)
        d = model.mesh_data(m); d.arrays[0] = struct.pack(">f",float("nan"))+d.arrays[0][4:]
        with self.assertRaises(ValueError): model.check_mesh_data(m,d)
        bad = asset.AssetDocument(write_archive(self.dir/"src", model_set_payloads(),"nonretail")).sections[0].model_sets[0]
        with self.assertRaisesRegex(ValueError,"packing"): model.rebuild_chunks(bad,{0:model.mesh_data(bad.meshes[0])})

    def test_morph_remapping_preserves_raw_deltas_and_rejects_truncated_data(self):
        delta = struct.pack(">3f",1,2,3)
        morph = {"head":struct.pack(">II",0,1), "lists":[[[[(delta,0),(delta,2)]]]]}
        raw = model.encode_morphs(morph)
        self.assertEqual(model.encode_morphs(model.parse_morphs(raw)),raw)
        mapped = model.remap_morphs(morph,{0:[2,0,0]})
        self.assertEqual(mapped["lists"][0][0][0],[(delta,1),(delta,2),(delta,0)])
        for broken in (b"",raw[:-1],raw+b"x"):
            with self.assertRaises(ValueError): model.parse_morphs(broken)


    def test_culling_recomputed_and_references_preserved(self):
        bounds = model.node_bounds([self.ms]); lo,hi=bounds[0x2000]
        raw = struct.pack(">6f5I",*lo,*hi,1,0,0,0,0x2000)
        path=write_archive(self.dir/"src",canonical_payloads()+[(0x6101,raw)],"cull")
        doc=asset.AssetDocument(path); ms=doc.sections[0].model_sets[0]
        pos=ms.meshes[0].attribute("position").values(); pos[0]=(0,0,-4)
        ps=asset.PatchSet(doc.source_hashes,model.geometry_patches(ms,{0:{("position",0):pos}}))
        out=self.dir/"cull.dict"; report=asset.write_rebuild(path,ps,out)
        a=po_archive.load_archive(out); got=a.get_chunk_bytes(a.find_chunks(0x6101)[0])
        self.assertEqual(struct.unpack_from(">f",got,8)[0],-4)
        self.assertEqual(got[24:],raw[24:]); self.assertTrue(report["audit"]["derived_culling_patches"])
        self.assertEqual(model.culling_patches(a),[])
        for invalid in (raw[:-1],raw[:32]+struct.pack(">2I",1,0)+raw[40:],raw[:40]+struct.pack(">I",999)):
            path2=write_archive(self.dir/"src",canonical_payloads()+[(0x6101,invalid)],"invalid")
            with self.assertRaises(ValueError): model.culling_patches(po_archive.load_archive(path2))

    def test_uv_scales_require_shader_provenance(self):
        for typ,shader,scale in ((0x16,0x46ABE398,256),(0x17,0x46ABE398,256),(0x26,0xEE9D919D,4096),(0xFC,0x32BC21E8,1024)):
            raw=struct.pack(">2h",scale,-scale)
            a=model.Attribute(typ,4,0x400,0,1,raw,shader)
            self.assertEqual(a.values(),[(1,-1)]); self.assertEqual(a.encode_element(a.values()[0]),raw)
            self.assertFalse(model.Attribute(typ,4,0x400,0,1,raw,0x21DB4385).verified)
            self.assertFalse(model.Attribute(typ,4,0x407,0,1,raw,shader).verified)
            self.assertFalse(model.Attribute(typ,4,0x100,0,1,raw,shader).verified)

    def test_relocation_keeps_nonzero_gap_and_tail_bytes(self):
        a=po_archive.load_archive(self.path); first=a.find_chunks()[0]; bi=a._chunk_block(first)
        layout=a.data_chunks_in_block(bi); ri,off,size=layout[-1]
        # Add opaque nonzero tail bytes only to this in-memory synthetic archive.
        a.blocks[bi][off+size:off+size+4]=b"TAIL"
        before={r:a.get_chunk_bytes(r) for r in a.find_chunks()}
        old=bytes(a.blocks[bi]); old_gaps=[old[o+n:layout[i+1][1] if i+1<len(layout) else len(old)] for i,(_,o,n) in enumerate(layout)]
        audit=asset._relocate_chunks(a,{first:before[first]+bytes(37)})
        self.assertTrue(any(g["nonzero_bytes"] for g in audit))
        for i,(r,_,_) in enumerate(layout):
            now=a.chunks[r][4]+a.chunks[r][3]
            self.assertEqual(bytes(a.blocks[bi][now:now+len(old_gaps[i])]),old_gaps[i])
            if r!=first: self.assertEqual(a.get_chunk_bytes(r),before[r])

    def test_skin_edit_extends_palette_through_b00a_and_rebuilds(self):
        path = write_archive(self.dir/"src", skinned_payloads(), "skin")
        doc = asset.AssetDocument(path); ms = doc.sections[0].model_sets[0]; bones = {h for h, _ in ms.bones}
        m = ms.meshes[0]; self.assertEqual(model.influences(m)[1], {BONES[0]: 1.0})
        self.assertEqual(model.encode_model_set(ms, layout=[0, 1])[ms.chunks[0xB016]], ms.materials)
        d = model.reskin(m, model.mesh_data(m), {1: {BONES[2]: 3, BONES[0]: 1}}, bones)
        self.assertEqual(d.palette, [BONES[0], BONES[2]])
        wts, idx = model.skin_attributes(m)
        self.assertEqual(d.arrays[m.attributes.index(wts)][16:32], struct.pack(">4f", 0.75, 0.25, 0, 0))
        self.assertEqual(d.arrays[m.attributes.index(idx)][4:8], bytes([1, 0, 0, 0]))
        self.assertEqual(d.arrays[m.attributes.index(wts)][:16], wts.raw[:16])   # other vertices keep their bytes
        ps = asset.PatchSet(doc.source_hashes, topology={0: {0: d}})
        out = self.dir/"skin"/"skin.dict"; report = asset.write_rebuild(path, ps, out)
        got = asset.AssetDocument(out).sections[0].model_sets[0]
        self.assertEqual(got.meshes[0].palette, [BONES[0], BONES[2]])
        self.assertEqual(model.influences(got.meshes[0])[1], {BONES[2]: 0.75, BONES[0]: 0.25})
        self.assertEqual(model.influences(got.meshes[1]), model.influences(ms.meshes[1]))
        resized = {c["type"] for c in report["audit"]["changed_chunks"] if c["resized"]}
        self.assertEqual(resized, {"0xB00B"}); self.assertTrue(report["deterministic"])
        self.assertEqual(asset.rebuild_with_patches(path, asset.PatchSet.from_dict(ps.as_dict()))[:2],
                         (out.read_bytes(), out.with_suffix(".data").read_bytes()))
        # Dropping a bone compacts the palette; kept vertices only renumber their slots.
        m1 = ms.meshes[1]; d1 = model.reskin(m1, model.mesh_data(m1), {1: {BONES[1]: 1}, 0: {BONES[1]: 1}}, bones)
        self.assertEqual(d1.palette, [BONES[1]])
        self.assertEqual([x for x in model.influences(m1, d1)], [{BONES[1]: 1.0}] * 3)
        # Same palette: the change is two fixed-size attribute patches, no resize.
        d2 = model.reskin(m1, model.mesh_data(m1), {1: {BONES[0]: 1, BONES[1]: 3}}, bones)
        self.assertEqual(d2.palette, m1.palette)
        w1, i1 = model.skin_attributes(m1)
        edits = {("weights", w1.set): [struct.unpack_from(">4f", d2.arrays[m1.attributes.index(w1)], 16*v) for v in range(3)],
                 ("indices", i1.set): [tuple(d2.arrays[m1.attributes.index(i1)][4*v:4*v+4]) for v in range(3)]}
        patches = model.geometry_patches(ms, {1: edits})
        self.assertTrue(patches and all(p.chunk == ms.chunks[0xB006] for p in patches))
        for bad in ({1: {0xDEAD: 1}}, {1: {BONES[0]: 1, BONES[1]: 1, BONES[2]: 1, 0xB0000004: 1, 0xB0000005: 1}},
                    {1: {BONES[0]: 0}}, {7: {BONES[0]: 1}}, {1: {BONES[0]: float("nan")}}):
            with self.assertRaises(ValueError): model.reskin(m, model.mesh_data(m), bad, bones)
        with self.assertRaises(ValueError): model.reskin(m, model.mesh_data(m), {1: {BONES[2]: 1}}, bones - {BONES[2]})
        tampered = model.mesh_data(m); raw = bytearray(tampered.arrays[1]); raw[0:4] = struct.pack(">f", 0.5)
        tampered.arrays[1] = bytes(raw)
        for b in (bones, None):
            with self.assertRaises(ValueError): model.check_mesh_data(m, tampered, b)
        big = model.MeshData(d.strip, d.vertex_count, d.arrays, [BONES[0], BONES[2], *range(1, 9)], d.source)
        with self.assertRaises(ValueError): model.check_mesh_data(m, big, bones | set(range(1, 9)))   # 10 bones

    def test_whole_mesh_duplicate_and_delete_in_static_sets(self):
        path = write_archive(self.dir/"src", one_node(canonical_payloads()), "table")
        doc = asset.AssetDocument(path); ms = doc.sections[0].model_sets[0]
        noop = asset.PatchSet(doc.source_hashes, layout={0: [1, 1]})
        self.assertEqual(asset.rebuild_with_patches(path, noop)[:2], (path.read_bytes(), path.with_suffix(".data").read_bytes()))
        moved = model.remap_mesh(ms.meshes[0], [0, 1, 2], ms.meshes[0].triangles(), {("position", 0): [(0, 0, 5), (1, 0, 5), (0, 1, 5)]})
        ps = asset.PatchSet(doc.source_hashes, topology={0: {1: moved}}, layout={0: [2, 1]})
        value = ps.as_dict(); self.assertEqual(value["schema"], 3)
        out = self.dir/"dup"/"dup.dict"; report = asset.write_rebuild(path, ps, out)
        got = asset.AssetDocument(out).sections[0].model_sets[0]
        self.assertEqual(len(got.meshes), 3); self.assertEqual([n.mesh_count for n in got.nodes], [3])
        self.assertEqual([m.material_offset for m in got.meshes], [0, 8, 16])
        self.assertEqual(got.materials, ms.materials[:8] * 2 + ms.materials[8:])
        self.assertEqual(got.meshes[1].attribute("position").values()[0], (0, 0, 5))
        self.assertEqual(got.meshes[0].attributes[3].raw, ms.meshes[0].attributes[3].raw)
        self.assertEqual(got.meshes[2].attributes[0].raw, ms.meshes[1].attributes[0].raw)
        self.assertTrue(report["deterministic"]); self.assertIn("duplicated source meshes [0]", " ".join(ps.review()))
        self.assertEqual(asset.rebuild_with_patches(path, asset.PatchSet.from_dict(value))[:2],
                         (out.read_bytes(), out.with_suffix(".data").read_bytes()))
        # Deleting the copy again restores every original chunk payload (synthetic blocks are
        # not 0x800-padded like retail ones, so only the block tail differs).
        from nlg_pack import Archive
        back = asset.PatchSet(asset.AssetDocument(out).source_hashes, layout={0: [1, 0, 1]})
        restored = Archive("restored.dict", *asset.rebuild_with_patches(out, back)[:2]); original = po_archive.load_archive(path)
        self.assertEqual([restored.get_chunk_bytes(ri) for ri in restored.find_chunks()],
                         [original.get_chunk_bytes(ri) for ri in original.find_chunks()])
        deleted = self.dir/"del"/"del.dict"; asset.write_rebuild(path, asset.PatchSet(doc.source_hashes, layout={0: [0, 1]}), deleted)
        kept = asset.AssetDocument(deleted).sections[0].model_sets[0]
        self.assertEqual((len(kept.meshes), kept.meshes[0].material_offset, len(kept.materials)), (1, 0, 8))
        self.assertEqual(kept.meshes[0].attributes[0].raw, ms.meshes[1].attributes[0].raw)
        for bad in ({0: [0, 0]}, {0: [1]}, {0: [1, -1]}):
            with self.assertRaises(ValueError): asset.rebuild_with_patches(path, asset.PatchSet(doc.source_hashes, layout=bad))
        for broken in (dict(value, schema=2), dict(value, mesh_layout=[], schema=3)):
            with self.assertRaises(ValueError): asset.PatchSet.from_dict(broken)
        two_nodes = asset.AssetDocument(self.path)
        with self.assertRaisesRegex(ValueError, "last mesh"):
            asset.rebuild_with_patches(self.path, asset.PatchSet(two_nodes.source_hashes, layout={0: [0, 1]}))
        named = write_archive(self.dir/"src", one_node(canonical_payloads()) + [(0x7001, struct.pack(">I", 0x1000))], "named")
        ndoc = asset.AssetDocument(named)
        with self.assertRaisesRegex(ValueError, "referenced"):
            asset.rebuild_with_patches(named, asset.PatchSet(ndoc.source_hashes, layout={0: [2, 1]}))
        asset.rebuild_with_patches(named, asset.PatchSet(ndoc.source_hashes, layout={0: [1, 2]}))
        skin = write_archive(self.dir/"src", skinned_payloads(), "skinlayout"); sdoc = asset.AssetDocument(skin)
        with self.assertRaisesRegex(ValueError, "B00B"):
            asset.rebuild_with_patches(skin, asset.PatchSet(sdoc.source_hashes, layout={0: [2, 1]}))

    def test_texture_patches_all_mips_alias_guard_and_untouched_identity(self):
        rgba=bytes([200,20,30,255])*256; pixels,_=texture.build_mip_chain(rgba,16,16,2,8)
        head=texture_header(16,16,8,2,0,123)
        path=write_archive(self.dir/"src",[(0xB601,head),(0xB603,pixels)],"texture")
        doc=asset.AssetDocument(path); sec=doc.sections[0]; entry=sec.textures[0]
        self.assertEqual(asset.texture_patches(sec,entry,rgba),[])
        replacement=bytes([20,200,30,255])*256
        ps=asset.PatchSet(doc.source_hashes,asset.texture_patches(sec,entry,replacement))
        out=self.dir/"texture.dict"; asset.write_rebuild(path,ps,out)
        got=asset.AssetDocument(out).sections[0]
        self.assertEqual(got.archive.get_chunk_bytes(got.textures[0].data_chunk),texture.build_mip_chain(replacement,16,16,2,8)[0])
        path2=write_archive(self.dir/"src",[(0xB601,head+texture_header(16,16,8,2,0,124)),(0xB603,pixels)],"alias")
        sec=asset.AssetDocument(path2).sections[0]
        with self.assertRaisesRegex(ValueError,"Aliased"): asset.texture_patches(sec,sec.textures[0],replacement)


if __name__ == "__main__": unittest.main()
