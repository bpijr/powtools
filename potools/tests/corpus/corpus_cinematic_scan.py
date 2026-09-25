"""Read-only camera corpus scan. Reports counts only; keeps game bytes in memory.

python potools/tests/corpus/corpus_cinematic_scan.py <art> [--dol <main.dol>]
"""
import argparse
from collections import Counter
import hashlib
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
import po_archive
import nlg_cinematic as cinematic
import nlg_asset
import nlg_container


def scan(root):
    counts=Counter()
    for path in sorted(Path(root).rglob("*.dict")):
        parts=po_archive.sections(path.read_bytes()); archives=po_archive.load_sections(path); counts["archives"]+=1
        counts["directory_records"]+=sum(len(cinematic.directories(a)) for a in archives)
        hashes=[po_archive.digest(path.read_bytes()),po_archive.digest(path.with_suffix(".data").read_bytes())]
        # Container writer: every archive re-lays exactly; every block survives a no-op relayout or is refused.
        assert nlg_container.rebuild_exact(path.read_bytes(),path.with_suffix(".data").read_bytes()),path
        counts["writer_exact_containers" if len(parts)>1 else "writer_exact_single_sections"]+=1
        for a in archives:
            for block in range(7):
                if not a.data_chunks_in_block(block): continue
                reason=nlg_container.block_limitation(a,block)
                counts["relayout_safe_blocks" if reason is None else "relayout_refused_blocks"]+=1
                if len(parts)>1: assert reason is None,(path,block,reason)
        if len(parts)>1:
            resize_mutation(path,hashes,parts,archives,counts)
            counts["nis_containers"]+=1; counts["nis_sections"]+=len(parts)
            dd,da,audit=nlg_asset.rebuild_with_patches(path,nlg_asset.PatchSet(hashes))
            assert [po_archive.digest(dd),po_archive.digest(da)]==hashes and audit["changed_bytes"]==0
            counts["nis_exact_noops"]+=1
        mutation=None
        for si,a in enumerate(archives):
            for clip in cinematic.camera_clips(a):
                assert clip.limitation is None, (path,si,clip.index,clip.limitation)
                counts[clip.family+"_clips"]+=1; counts[clip.family+"_samples"]+=clip.frames
                for track in ("position","target"):
                    if track not in clip.tracks: continue
                    assert struct.pack(">3f",*clip.tracks[track][0]) == a.get_chunk_bytes(clip.chunks[track])[:12]
                    counts["track_noops"]+=1
                if mutation is None:
                    values=list(clip.tracks["position"][0]); values[0]+=0.125
                    patches=cinematic.camera_patches(a,clip.index,"position",0,values)
                    mutation=nlg_asset.PatchSet(hashes,[nlg_asset.SectionPatch(si,p.chunk,p.offset,p.old,p.new,p.label) for p in patches])
        if mutation is not None:
            first=nlg_asset.rebuild_with_patches(path,mutation); second=nlg_asset.rebuild_with_patches(path,mutation)
            assert first==second and first[2]["changed_bytes"]>0
            assert first[0]==path.read_bytes()
            for si,part in enumerate(parts):
                before=archives[si].build_data(); after=first[1][part["offset"]:part["offset"]+part["length"]]
                if si not in first[2]["sections_changed"]: assert before==after
                check=type(archives[si])(str(path),part["dictionary"],after)
                assert check.chunks==archives[si].chunks
                assert all(c.limitation is None for c in cinematic.camera_clips(check))
            counts["deterministic_isolated_mutations"]+=1
    return dict(sorted((k,v) for k,v in counts.items() if not k.startswith("_done_")))


def resize_mutation(path,hashes,parts,archives,counts):
    """Insert/remove one sample in the first resizable NIS camera; audit by content."""
    for si,a in enumerate(archives):
        clips=cinematic.camera_clips(a)
        for clip in clips:
            reason=cinematic.resize_limitation(a,clip)
            if reason: counts["nis_clips_resize_locked"]+=1; continue
            counts["nis_clips_resizable"]+=1
        target=next((c for c in clips if cinematic.resize_limitation(a,c) is None),None)
        if target is None or counts.get("_done_"+str(path)): continue
        counts["_done_"+str(path)]=1
        grown=target.tracks
        for i in range(80):  # 80 x 32 bytes crosses a 0x800 boundary, forcing later sections to move
            grown=cinematic.insert_sample(grown,1+i,tuple(v+0.5 for v in target.tracks["position"][0]))
        for label,tracks in (("insert",cinematic.insert_sample(target.tracks,target.frames,target.tracks["position"][-1])),
                             ("remove",cinematic.remove_sample(target.tracks,0) if target.frames>cinematic.MIN_SAMPLES else None),
                             ("grow",grown)):
            if tracks is None: continue
            fixed,resizes=cinematic.resize_edit(a,target,tracks,si)
            ps=nlg_asset.PatchSet(hashes,[nlg_asset.SectionPatch(si,p.chunk,p.offset,p.old,p.new,p.label) for p in fixed],resizes=resizes)
            first=nlg_asset.rebuild_with_patches(path,ps); second=nlg_asset.rebuild_with_patches(path,ps)
            assert first==second and first[2]["sections_changed"]==[si]
            assert first[2]["untouched_sections_identical"]==len(parts)-1
            _,after=nlg_container.parse(first[0],first[1])
            for s,(x,y) in enumerate(zip(archives,after)):
                assert [r[:3] for r in x.chunks]==[r[:3] for r in y.chunks]
                if s!=si: assert x.build_data()==y.build_data()
            changed=cinematic.camera_clips(after[si])
            f32=[struct.unpack(">3f",struct.pack(">3f",*v)) for v in tracks["position"]]
            assert changed[target.index].frames==len(tracks["position"]) and changed[target.index].tracks["position"]==f32
            assert all(c.tracks==o.tracks for c,o in zip(changed,cinematic.camera_clips(a)) if c.index!=target.index)
            counts["nis_resize_mutations_"+label]+=1
            counts["nis_resize_moved_sections"]+=sum(x["new_offset"]!=x["old_offset"] for x in first[2]["sections"])


def dol_evidence(path):
    from nlg_dol import Dol
    d=Dol(str(path)); sda2,_=d.resolve_sda()
    # Prove the exact constants/instructions used in the documented local DOL consumer.
    assert d.word(0x8012c198)==0x80a30008  # NIS frame count at header +8
    assert d.word(0x8012c450)==0x8083000c  # camera header pointer
    assert struct.unpack(">f",d.read(sda2-0x6c34,4))[0]==30
    assert d.word(0x800227bc)==0x901c0014  # 5028 track pointer -> camera data +14
    assert struct.unpack(">f",d.read(sda2-0x7db8,4))[0]==180
    assert abs(struct.unpack(">f",d.read(sda2-0x7db4,4))[0]-3.14159265)<1e-6
    # Section loader: records of 0x4C bytes; +4 entries, +0 seek offset, +0xC/+0x10 block size/alignment, +8 table bytes.
    assert d.word(0x8012753c)==0x1cdf004c and d.word(0x80127548)==0x80030004 and d.word(0x80127564)==0x7ca5302e
    assert d.word(0x80127800)==0x1c00004c and d.word(0x8012780c)==0x8005000c and d.word(0x80127860)==0x80050010
    assert d.word(0x80127868)==0x28000020 and d.word(0x801279a0)==0x80ba0008
    return {"sha256":hashlib.sha256(d.d).hexdigest(),"camera_consumer_assertions":6,"section_loader_assertions":8}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("art"); parser.add_argument("--dol")
    args=parser.parse_args(); print(scan(args.art))
    if args.dol: print("CINEMATIC_DOL_EVIDENCE_PASS",dol_evidence(args.dol))
    print("CORPUS_CINEMATIC_SCAN_PASS")
