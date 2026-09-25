"""Synthetic camera/NIS ownership, malformed input and audited section patch tests."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
from helpers import archive_bytes, container_bytes
from nlg_pack import Archive
import nlg_asset
import nlg_cinematic as cinematic
import po_archive
import fixtures


def camera_bytes(family="nis", frames=3, pad=False, extra=()):
    header = bytearray(48); struct.pack_into(">If", header, 8, frames, 4/3)
    positions = struct.pack(">%df" % (frames*3), *(float(i) for i in range(frames*3)))
    rotations = struct.pack(">4f", 0, 0, 0, 1) * frames
    angle = struct.pack(">f", 0.8) * frames
    if family == "nis":
        payloads = [(0x5001, header), (0x5002, b"Camera01\0"), (0x5003, positions), (0x5004, rotations), (0x5005, angle), *extra]
    else:
        payloads = [(0x5002, b"Camera01\0"), (0x5031, struct.pack(">I",frames)), (0x5003,positions),
                    (0x5004,rotations), (0x5023,positions), (0x5028,angle), (0x5026,struct.pack(">f", 4.5)*frames)]
    dd, da = archive_bytes(payloads + [(0xDD01,b"unknown payload")])
    a = Archive("synthetic.dict", dd, da)
    for row in a.chunks[:len(payloads)]: row[1]=2
    a.chunks.insert(0, [0x93, 2, 0x5000 if family == "nis" else 0x5030, len(payloads), 1])
    a.num_file_entries = 1
    for block in a.blocks if pad else ():
        block += bytes(-len(block) % 0x800)   # retail blocks are 0x800 padded; resizing requires it
    return a.build_dict(), a.build_data()


def write_camera(root, parts=None, pad=False):
    root.mkdir(parents=True, exist_ok=True)
    dd, da = container_bytes(parts or [archive_bytes([(0xDD02,b"section zero")]), camera_bytes(pad=pad)])
    path = root / "camera.dict"; path.write_bytes(dd); path.with_suffix(".data").write_bytes(da)
    return path


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.base = Path(self.temp.name)

    def test_decode_two_families_and_directory_ownership(self):
        for family in ("nis", "sampled"):
            a = Archive("x.dict", *camera_bytes(family)); clip = cinematic.camera_clips(a)[0]
            self.assertIsNone(clip.limitation); self.assertEqual(clip.frames,3)
            self.assertEqual(clip.tracks["position"][1], (3,4,5))
            self.assertEqual(clip.summary()["sample_rate"], 30 if family=="nis" else None)
            self.assertEqual(cinematic.camera_patches(a,0,"position",0,[0,1,2]), [])
            self.assertIn("angle_radians",clip.tracks)
        a = Archive("x.dict", *archive_bytes([(0x5002,b"Camera01\0"),(0x5003,bytes(12))]))
        self.assertEqual(cinematic.camera_clips(a), [])

    def test_malformed_headers_tracks_and_ownership_locked(self):
        for change in ("count", "nan", "quat", "missing", "shared", "order", "signature", "discriminator"):
            a=Archive("x.dict",*camera_bytes()); clip=cinematic.camera_clips(a)[0]
            if change=="count": struct.pack_into(">I",a.blocks[0],a.chunks[1][4]+8,4)
            elif change=="nan": struct.pack_into(">f",a.blocks[0],a.chunks[3][4],float("nan"))
            elif change=="quat": struct.pack_into(">f",a.blocks[0],a.chunks[4][4],2)
            elif change=="missing": a.chunks[5][2]=0x50FF
            elif change=="shared": a.chunks.append([0x93,2,0xDD00,1,3])
            elif change=="signature": a.chunks[0][1]=0
            elif change=="discriminator": a.chunks[3][1]=0
            else: a.chunks[3],a.chunks[4]=a.chunks[4],a.chunks[3]
            self.assertIsNotNone(cinematic.camera_clips(a)[0].limitation,change)
            with self.assertRaises(ValueError): cinematic.camera_patches(a,0,"position",0,[1,2,3])

    def test_invalid_directory_graphs(self):
        for row in ([0x93,2,0x5000,100,1],[0x93,2,0x5000,1,0]):
            a=Archive("x.dict",*camera_bytes()); a.chunks[0]=row
            with self.assertRaises(ValueError): cinematic.directories(a)
        a=Archive("x.dict",*camera_bytes()); a.chunks.append([0x93,2,0xDD00,1,len(a.chunks)])
        with self.assertRaisesRegex(ValueError,"cycle"): cinematic.directories(a)

    def test_invalid_edits(self):
        a=Archive("x.dict",*camera_bytes())
        for track,sample,value in [("angle_radians",0,[1,2,3]),("position",-1,[1,2,3]),("position",3,[1,2,3]),
                                    ("position",True,[1,2,3]),("position",0,[float("inf"),0,0]),("position",0,[1e90,0,0]),("position",0,[True,0,0])]:
            with self.assertRaises(ValueError): cinematic.camera_patches(a,0,track,sample,value)

    def test_multisection_noop_edit_isolation_serialization(self):
        source=write_camera(self.base/"source"); doc=nlg_asset.AssetDocument(source)
        ps=nlg_asset.PatchSet(doc.source_hashes)
        dd,da,audit=nlg_asset.rebuild_with_patches(source,ps)
        self.assertEqual(dd,source.read_bytes()); self.assertEqual(da,source.with_suffix(".data").read_bytes())
        self.assertEqual(audit["changed_bytes"],0)
        a=doc.sections[1].archive
        p=cinematic.camera_patches(a,0,"position",1,[3.25,4,5])[0]
        ps.patches=[nlg_asset.SectionPatch(1,p.chunk,p.offset,p.old,p.new,p.label)]
        ps=nlg_asset.PatchSet.from_dict(ps.as_dict())
        out=self.base/"output"/"edited.dict"; report=nlg_asset.write_rebuild(source,ps,out)
        self.assertTrue(report["deterministic"]); self.assertEqual(report["audit"]["sections_changed"],[1])
        before=po_archive.load_sections(source); after=po_archive.load_sections(out)
        self.assertEqual(before[0].build_data(),after[0].build_data())
        for ri in a.find_chunks():
            if ri!=p.chunk: self.assertEqual(before[1].get_chunk_bytes(ri),after[1].get_chunk_bytes(ri))
        self.assertEqual(cinematic.camera_clips(after[1])[0].tracks["position"][1],(3.25,4,5))

    def test_bad_patch_addresses_overlap_source_hash_and_resize_refused(self):
        source=write_camera(self.base/"source"); doc=nlg_asset.AssetDocument(source)
        for si,chunk,offset,old,new in [(2,3,0,b"",b""),(True,3,0,b"",b""),(1,-1,0,b"",b""),(1,0,0,b"",b""),
                                          (1,3,-1,b"x",b"y"),(1,3,0,b"",b"x"),(1,3,0,b"bad",b"new")]:
            with self.assertRaises(ValueError): nlg_asset.rebuild_with_patches(source,nlg_asset.PatchSet(doc.source_hashes,[nlg_asset.SectionPatch(si,chunk,offset,old,new,"bad")]))
        p=nlg_asset.SectionPatch(1,3,0,bytes(4),struct.pack(">f",1),"move")
        with self.assertRaisesRegex(ValueError,"overlap"): nlg_asset.rebuild_with_patches(source,nlg_asset.PatchSet(doc.source_hashes,[p,p]))
        with self.assertRaisesRegex(ValueError,"different source"): nlg_asset.rebuild_with_patches(source,nlg_asset.PatchSet(["bad","bad"],[p]))

    def test_aliased_payload_and_unaligned_sections_refused(self):
        a=Archive("x.dict",*camera_bytes());a.chunks.append([0,0,0xDD00,4,a.chunks[3][4]])
        source=write_camera(self.base/"alias",[archive_bytes([(0xDD00,b"safe")]),(a.build_dict(),a.build_data())])
        doc=nlg_asset.AssetDocument(source)
        p=nlg_asset.SectionPatch(1,3,0,bytes(4),struct.pack(">f",1),"move")
        with self.assertRaisesRegex(ValueError,"aliased"):nlg_asset.rebuild_with_patches(source,nlg_asset.PatchSet(doc.source_hashes,[p]))
        dd=bytearray(source.read_bytes());da=source.with_suffix(".data").read_bytes()
        offset=struct.unpack_from(">I",dd,88)[0];struct.pack_into(">I",dd,88,offset+1)
        source.write_bytes(dd);source.with_suffix(".data").write_bytes(da[:offset]+b"\0"+da[offset:])
        hashes=[po_archive.digest(source.read_bytes()),po_archive.digest(source.with_suffix(".data").read_bytes())]
        with self.assertRaisesRegex(ValueError,"alignment"):nlg_asset.rebuild_with_patches(source,nlg_asset.PatchSet(hashes))

    def test_real_nis_fixture(self):
        source=fixtures.fixture("NIS/Transitions/round_transition_1.dict")
        if source is None: self.skipTest("Private fixtures not configured")
        doc=nlg_asset.AssetDocument(source)
        ps=nlg_asset.PatchSet(doc.source_hashes)
        dd,da,_=nlg_asset.rebuild_with_patches(source,ps)
        self.assertEqual(dd,source.read_bytes()); self.assertEqual(da,source.with_suffix(".data").read_bytes())
        self.assertTrue(any(s["cameras"] for s in doc.cinematic_summary()))

    def test_typed_build_and_output_guards(self):
        import po_cinematics as cinematics
        source=write_camera(self.base/"source"); doc=cinematics.load(source)
        op={"kind":"camera_sample","section":1,"clip":0,"track":"position","sample":0,"value":[0.25,1,2]}
        out=self.base/"build"/"edited.dict"
        report=cinematics.build(doc,[op],out)
        self.assertEqual(report["runtime"],"unverified");self.assertGreater(report["audit"]["changed_bytes"],0)
        for target in (out,source,self.base/"source"/"bad.dict"):
            with self.assertRaises(ValueError):cinematics.build(doc,[op],target)
        repo=self.base/"repo";repo.mkdir();(repo/".git").write_text("gitdir: somewhere")
        with self.assertRaises(ValueError):cinematics.build(doc,[op],repo/"bad.dict")
        with self.assertRaises(ValueError):cinematics.patch_set(doc,[op,op])
        with self.assertRaises(ValueError):cinematics.patch_set(doc,[dict(op,section=99)])
        with self.assertRaises(ValueError):cinematics.patch_set(doc,[dict(op,extra=1)])
        self.assertEqual(cinematics.preview(doc,1,0,[op])["path"][0],(0.25,1,2))

    def test_resize_limitations_by_family(self):
        a=Archive("x.dict",*camera_bytes(pad=True)); clip=cinematic.camera_clips(a)[0]
        self.assertIsNone(cinematic.resize_limitation(a,clip))
        a=Archive("x.dict",*camera_bytes("sampled",pad=True)); clip=cinematic.camera_clips(a)[0]
        self.assertIn("0x5026",cinematic.resize_limitation(a,clip))
        with self.assertRaises(ValueError): cinematic.resize_edit(a,clip,clip.tracks)
        a=Archive("x.dict",*camera_bytes(pad=True,extra=[(0x5006,bytes(12))])); clip=cinematic.camera_clips(a)[0]
        self.assertIsNone(clip.limitation); self.assertIn("0x5006",cinematic.resize_limitation(a,clip))
        a=Archive("x.dict",*camera_bytes(pad=True)); clip=cinematic.camera_clips(a)[0]
        bad=dict(clip.tracks,rotation_xyzw=[(0,0,0,2)]*3)
        for tracks in (bad,dict(clip.tracks,angle_radians=[(0.8,)]*2),{k:v[:1] for k,v in clip.tracks.items()},
                       dict(clip.tracks,position=[(float("nan"),0,0)]*3),dict(clip.tracks,position=[(1e39,0,0)]*3)):
            with self.assertRaises(ValueError): cinematic.resize_edit(a,clip,tracks)
        self.assertEqual(cinematic.resize_edit(a,clip,clip.tracks),([],[]))

    def test_insert_remove_camera_samples_relayout_and_audit(self):
        import po_cinematics as cinematics
        source=write_camera(self.base/"source",pad=True); doc=cinematics.load(source)
        before=po_archive.load_sections(source)
        insert={"kind":"camera_insert","section":1,"clip":0,"sample":1,"value":[9,9,9]}
        edit={"kind":"camera_sample","section":1,"clip":0,"track":"position","sample":1,"value":[8,8,8]}
        ps=cinematics.patch_set(doc,[insert,edit]); self.assertEqual(len(ps.resizes),3)
        self.assertEqual(nlg_asset.PatchSet.from_dict(ps.as_dict()).as_dict(),ps.as_dict())
        report=cinematics.build(doc,[insert,edit],self.base/"out"/"inserted.dict")
        audit=report["audit"]; self.assertEqual(audit["sections_changed"],[1]); self.assertEqual(audit["untouched_sections_identical"],1)
        after=po_archive.load_sections(self.base/"out"/"inserted.dict"); clip=cinematic.camera_clips(after[1])[0]
        self.assertEqual(clip.frames,4); self.assertEqual(clip.tracks["position"],[(0,1,2),(8,8,8),(3,4,5),(6,7,8)])
        self.assertEqual(clip.tracks["rotation_xyzw"],[(0,0,0,1)]*4); self.assertEqual(clip.tracks["angle_radians"],[(struct.unpack(">f",struct.pack(">f",0.8))[0],)]*4)
        old=before[1]; head=old.find_chunks(type_id=0x5001)[0]
        self.assertEqual(after[1].get_chunk_bytes(head)[12:],old.get_chunk_bytes(head)[12:])
        for t in (0x5002,0xDD01): self.assertEqual(after[1].get_chunk_bytes(after[1].find_chunks(type_id=t)[0]),old.get_chunk_bytes(old.find_chunks(type_id=t)[0]))
        self.assertEqual(after[0].build_data(),before[0].build_data())
        self.assertEqual([r[:3] for r in after[1].chunks],[r[:3] for r in old.chunks])
        self.assertEqual(json.loads((self.base/"out"/"inserted.patchset.json").read_text())["schema"],4)
        self.assertEqual(cinematics.preview(doc,1,0,[insert])["path"][1],(9,9,9))
        remove={"kind":"camera_remove","section":1,"clip":0,"sample":0}
        report=cinematics.build(doc,[remove],self.base/"out"/"removed.dict")
        clip=cinematic.camera_clips(po_archive.load_sections(self.base/"out"/"removed.dict")[1])[0]
        self.assertEqual((clip.frames,clip.tracks["position"]),(2,[(3,4,5),(6,7,8)]))
        self.assertEqual(cinematics.patch_set(doc,[insert,dict(remove,sample=1)]).resizes,[])
        for ops in ([remove,remove],[dict(insert,sample=4)],[dict(insert,value=[float("nan"),0,0])],[dict(insert,extra=1)],
                    [dict(insert,sample=True)],[insert,dict(edit,track="target")],[insert,dict(edit,sample=4)]):
            with self.assertRaises(ValueError): cinematics.patch_set(doc,ops)
        sampled=cinematics.load(write_camera(self.base/"sampled",[camera_bytes("sampled",pad=True)]))
        with self.assertRaisesRegex(ValueError,"unproven"): cinematics.patch_set(sampled,[dict(insert,section=0)])
        unpadded=cinematics.load(write_camera(self.base/"unpadded"))
        with self.assertRaisesRegex(ValueError,"relayout"): cinematics.build(unpadded,[insert],self.base/"out"/"unpadded.dict")

    def test_real_nis_insert_keeps_other_sections(self):
        source=fixtures.fixture("NIS/Transitions/round_transition_1.dict")
        if source is None: self.skipTest("Private fixtures not configured")
        import po_cinematics as cinematics
        doc=cinematics.load(source); before=po_archive.load_sections(source)
        si=next(s.index for s in doc.sections if cinematic.camera_clips(s.archive)); clip=cinematic.camera_clips(before[si])[0]
        ops=[{"kind":"camera_insert","section":si,"clip":0,"sample":clip.frames,"value":list(clip.tracks["position"][-1])},
             {"kind":"camera_remove","section":si,"clip":0,"sample":0}]
        ps=cinematics.patch_set(doc,ops[:1]); first=nlg_asset.rebuild_with_patches(source,ps)
        self.assertEqual(first,nlg_asset.rebuild_with_patches(source,ps))
        from nlg_container import parse
        _,after=parse(first[0],first[1])
        for s,(x,y) in enumerate(zip(before,after)):
            if s!=si: self.assertEqual(x.build_data(),y.build_data())
        changed=cinematic.camera_clips(after[si])[0]; self.assertEqual(changed.frames,clip.frames+1)
        self.assertEqual(changed.tracks["position"][:clip.frames],clip.tracks["position"])
        self.assertEqual(cinematics.patch_set(doc,ops[1:]).resizes[0].chunk,clip.chunks["position"])


if __name__=="__main__": unittest.main()
