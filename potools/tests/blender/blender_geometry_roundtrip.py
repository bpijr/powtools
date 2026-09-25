"""Headless topology, vertex provenance, UV seams, and texture pixels.

blender --background --factory-startup --python-exit-code 1 --python this_file
Private fixture checks opt in with PO_FIXTURE_ROOT; outputs use private temporary folders.
"""
from pathlib import Path
import json
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import bpy
import bmesh
import io_punchout_asset as blender
import nlg_asset as asset
import nlg_model as model
import nlg_texture as texture
import po_archive
import fixtures
from test_model import write_archive
from test_geometry import canonical_payloads
from helpers import texture_header


def roundtrip(path,folder,textures=False):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    doc,_,objects = blender.import_asset(str(path),load_textures=textures)
    assert objects
    ps,_=blender.collect_patch_set(str(path)); assert not ps.patches and not ps.topology, ps.review()
    noop=folder/"noop.dict"; blender.export_asset(str(path),str(noop))
    assert noop.read_bytes()==path.read_bytes() and noop.with_suffix(".data").read_bytes()==path.with_suffix(".data").read_bytes()
    obj=next(o for o in objects if o.data.polygons)
    meta=json.loads(obj["po_asset"]); ms=doc.sections[0].model_sets[meta["model_set"]]; original=ms.meshes[meta["mesh"]["index"]]
    bm=bmesh.new(); bm.from_mesh(obj.data); bm.faces.ensure_lookup_table()
    face=bm.faces[0]
    new=bmesh.ops.duplicate(bm,geom=[face,*face.verts,*face.edges])["geom"]
    duplicated=[v for v in new if isinstance(v,bmesh.types.BMVert)]
    duplicated[0].co.z += 0.125
    bmesh.ops.delete(bm,geom=[face],context="FACES_ONLY")
    bm.to_mesh(obj.data); bm.free(); obj.data.update()
    if textures:
        img=next(i for i in bpy.data.images if i.get("po_texture"))
        paint_meta=json.loads(img["po_texture"])
        paint_entry=doc.sections[0].textures[paint_meta["index"]]
        px=[0.0]*(len(img.pixels)); img.pixels.foreach_get(px)
        for i in range(0,len(px),4): px[i:i+4]=[0.125,0.75,0.25,1]
        img.pixels.foreach_set(px); img.update()
    ps,_=blender.collect_patch_set(str(path)); assert ps.topology
    edited=ps.topology[ms.index][original.index]
    assert edited.vertex_count==original.vertex_count+3
    out=folder/"edited.dict"; report,_=blender.export_asset(str(path),str(out))
    loaded=asset.AssetDocument(out); got=loaded.sections[0].model_sets[ms.index].meshes[original.index]
    assert got.vertex_count==edited.vertex_count and len(got.triangles())==len(original.triangles())
    assert [a.raw for a in got.attributes]==edited.arrays
    for typ,ri in ms.trailing:
        if typ==0xB00C:
            old=model.parse_morphs(doc.sections[0].archive.get_chunk_bytes(ri))
            expected=model.encode_morphs(model.remap_morphs(old,{original.index:edited.source}))
            assert loaded.sections[0].archive.get_chunk_bytes(ri)==expected, "morph records did not follow vertex provenance"
    assert report["deterministic"] and report["audit"]["changed_chunks"]
    if textures:
        sec=loaded.sections[0]
        entry=sec.textures[paint_meta["index"]]
        expected,_=texture.build_mip_chain(bytes([32,191,64,255])*(entry.width*entry.height),entry.width,entry.height,entry.levels,entry.fmt)
        assert sec.archive.get_chunk_bytes(entry.data_chunk)[entry.data_offset:entry.data_offset+entry.chain_size]==expected
    # Whole-object duplication must never silently overwrite one source mesh slot.
    duplicate=obj.copy(); bpy.context.scene.collection.objects.link(duplicate)
    try:
        blender.collect_patch_set(str(path)); raise AssertionError("duplicate source mesh was accepted")
    except blender.ExportRefused: pass
    bpy.data.objects.remove(duplicate,do_unlink=True)
    # A new vertex with the default source id 0 cannot inherit arbitrary hidden attributes.
    bm=bmesh.new(); bm.from_mesh(obj.data); bm.verts.new((0,0,0)); bm.to_mesh(obj.data); bm.free()
    try:
        blender.collect_patch_set(str(path)); raise AssertionError("unproven vertex was accepted")
    except blender.ExportRefused: pass
    return len(objects)


def seam(path,folder):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    doc,_,objects=blender.import_asset(str(path),load_textures=False)
    obj=objects[0]
    bm=bmesh.new(); bm.from_mesh(obj.data); bm.verts.ensure_lookup_table()
    v0,v1,v2=list(bm.verts)
    layer=bm.loops.layers.uv["UV"]
    uv={loop.vert:loop[layer].uv.copy() for face in bm.faces for loop in face.loops}
    new=next(v for v in bmesh.ops.duplicate(bm,geom=[v0])["geom"] if isinstance(v,bmesh.types.BMVert)); new.co.z += 1
    uv[new]=uv[v0]
    face=bm.faces.new((v1,v2,new))
    for loop in face.loops: loop[layer].uv=uv[loop.vert]
    bm.to_mesh(obj.data); bm.free()
    # Establish the extra face first, then edit one side of a shared-vertex UV seam.
    out=folder/"seam_faces.dict"; blender.export_asset(str(path),str(out))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    doc,_,objects=blender.import_asset(str(out),load_textures=False); obj=objects[0]
    loop=obj.data.polygons[1].loop_indices[0]; obj.data.uv_layers["UV"].data[loop].uv=(0.3,0.7)
    ps,_=blender.collect_patch_set(str(out))
    assert ps.topology[0][0].vertex_count==5, "UV seam did not split a vertex"
    destination=folder.parent/"seam_result"/"seam.dict"
    asset.write_rebuild(out,ps,destination)


def main():
    counts=[]
    with tempfile.TemporaryDirectory(prefix="po-geometry-") as tmp:
        tmp=Path(tmp); (tmp/"source").mkdir()
        rgba=bytes([200,20,30,255])*256; pixels,_=texture.build_mip_chain(rgba,16,16,2,8)
        path=write_archive(tmp/"source",canonical_payloads()+[(0xB601,texture_header(16,16,8,2,0,123)),(0xB603,pixels)])
        (tmp/"synthetic").mkdir(); counts.append(roundtrip(path,tmp/"synthetic",True))
        (tmp/"seam").mkdir(); seam(path,tmp/"seam")
        root=fixtures.fixture_root()
        if root:
            for n,rel in enumerate(("characters/vs_microphone.dict","characters/ropes.dict","environments/minorcircuit/gameworld.dict","characters/referee.dict")):
                folder=tmp/str(n); folder.mkdir(); counts.append(roundtrip(root/rel,folder,textures=(n==0)))
    print("BLENDER_GEOMETRY_ROUNDTRIP_PASS",bpy.app.version_string,"archives",len(counts),"meshes",sum(counts))


main()
