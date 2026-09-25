"""Camera documents, section inspection and private typed builds shared by UI and Blender."""
from pathlib import Path
import struct

import nlg_asset
import nlg_cinematic
import po_archive


def load(path):
    return nlg_asset.AssetDocument(path)


def inspect_document(doc):
    rows = doc.cinematic_summary();parts=po_archive.sections(doc.path.read_bytes())
    for section, row in zip(doc.sections, rows):
        a = section.archive; animations = []
        for d in nlg_cinematic.directories(a):
            if d["type"] != 0x7000: continue
            by = {a.chunks[i][2]: i for i in d["children"] if a._chunk_block(i) >= 0}
            if 0x7001 not in by or 0x7002 not in by: continue
            raw = a.get_chunk_bytes(by[0x7001]); name = a.get_chunk_bytes(by[0x7002]).split(b"\0")[0].decode("latin1")
            animations.append({"directory": d["chunk"], "name": name,
                               "frames": struct.unpack_from(">I", raw, 8)[0] if len(raw)>=12 else None,
                               "rig_binding": "Unresolved; directory containment does not prove a rig binding"})
        clips = nlg_cinematic.camera_clips(a)
        for c in row["cameras"]:
            c["resize_limitation"] = nlg_cinematic.resize_limitation(a, clips[c["index"]])
        row["animations"] = animations
        row["model_sets"] = len(section.model_sets)
        row["bytes"] = len(a.orig_data)
        row["data_offset"] = parts[section.index]["offset"]
    return rows


FIELDS={"camera_sample":{"kind","section","clip","track","sample","value"},
        "camera_insert":{"kind","section","clip","sample","value"},"camera_remove":{"kind","section","clip","sample"}}


def _edit_state(doc, operations):
    """Apply typed camera operations in order; sample indices refer to the clip as already edited.
    Returns {(section, clip): (clip, tracks, resized)} for every clip an operation touched."""
    if not isinstance(operations,list) or len(operations)>100000: raise po_archive.ArchiveError("Too many camera sample edits.")
    state={}; seen=set(); clips={}; generation={}
    for op in operations:
        if not isinstance(op,dict) or op.get("kind") not in FIELDS or set(op)!=FIELDS[op["kind"]]:
            raise po_archive.ArchiveError("Camera edits have missing or unknown fields.")
        si=op["section"]
        if type(si) is not int or not 0<=si<len(doc.sections): raise po_archive.ArchiveError("Choose a section in this source.")
        if si not in clips:clips[si]=nlg_cinematic.camera_clips(doc.sections[si].archive)
        ci=op["clip"]
        if type(ci) is not int or not 0<=ci<len(clips[si]):raise po_archive.ArchiveError("Choose a camera clip.")
        clip=clips[si][ci]; a=doc.sections[si].archive
        if (si,ci) not in state:
            if clip.limitation: raise po_archive.ArchiveError("Camera is read-only: "+clip.limitation)
            state[(si,ci)]=[clip,{k:list(v) for k,v in clip.tracks.items()},False]
        entry=state[(si,ci)]; tracks=entry[1]
        try:
            if op["kind"]=="camera_sample":
                key=(si,ci,op["track"],op["sample"],generation.get((si,ci),0))
                try:
                    if key in seen: raise po_archive.ArchiveError("A camera sample was edited twice; keep one final value.")
                    seen.add(key)
                except TypeError as ex: raise po_archive.ArchiveError("Invalid camera edit address.") from ex
                if entry[2]:
                    if op["track"]!="position": raise po_archive.ArchiveError("Only position samples can be edited in a resized camera.")
                    if type(op["sample"]) is not int or not 0<=op["sample"]<len(tracks["position"]): raise po_archive.ArchiveError("Sample is outside the clip.")
                    nlg_cinematic._pack([op["value"]],3,"Position")
                else:
                    nlg_cinematic._sample_patches(a,clip,op["track"],op["sample"],op["value"])  # validates address/value
                tracks[op["track"]][op["sample"]]=tuple(float(v) for v in op["value"])
            else:
                reason=nlg_cinematic.resize_limitation(a,clip)
                if reason: raise po_archive.ArchiveError(reason)
                if op["kind"]=="camera_insert":
                    nlg_cinematic._pack([op["value"]],3,"Position")
                    new=nlg_cinematic.insert_sample(tracks,op["sample"],tuple(float(v) for v in op["value"]))
                else: new=nlg_cinematic.remove_sample(tracks,op["sample"])
                if len(new["position"])>nlg_cinematic.MAX_SAMPLES: raise po_archive.ArchiveError("Camera sample limit reached.")
                entry[1]=new; entry[2]=True; generation[(si,ci)]=generation.get((si,ci),0)+1
        except nlg_cinematic.CinematicError as ex: raise po_archive.ArchiveError(str(ex)) from ex
    return state


def patch_set(doc, operations):
    """Typed camera edits -> PatchSet. Unresized clips produce fixed-size sample patches; an
    insert/remove produces chunk resizes plus the header sample-count patch."""
    patches=[]; resizes=[]
    for (si,ci),(clip,tracks,resized) in sorted(_edit_state(doc,operations).items()):
        a=doc.sections[si].archive
        try:
            if not resized or (len(tracks["position"])==clip.frames and all(tracks[k]==clip.tracks[k] for k in ("rotation_xyzw","angle_radians"))):
                for track in ("position","target"):
                    for i,(new,old) in enumerate(zip(tracks.get(track,[]),clip.tracks.get(track,[]))):
                        if new!=old:
                            for p in nlg_cinematic._sample_patches(a,clip,track,i,new):
                                patches.append(nlg_asset.SectionPatch(si,p.chunk,p.offset,p.old,p.new,p.label))
            else:
                fixed,more=nlg_cinematic.resize_edit(a,clip,tracks,si)
                patches+=[nlg_asset.SectionPatch(si,p.chunk,p.offset,p.old,p.new,p.label) for p in fixed]; resizes+=more
        except nlg_cinematic.CinematicError as ex: raise po_archive.ArchiveError(str(ex)) from ex
    return nlg_asset.PatchSet(doc.source_hashes,patches,resizes=resizes)


def write_patch_set(source, ps, output):
    source=Path(source).resolve()
    root=next((p for p in source.parents if p.name.lower()=="art"),source.parent)
    output=po_archive.external(output,root)
    if output.suffix.lower()!=".dict": raise po_archive.ArchiveError("Choose a new .dict filename.")
    if any(output.with_suffix(s).exists() for s in (".dict",".data",".patchset.json")):
        raise po_archive.ArchiveError("Choose an unused output pair; existing files are never replaced.")
    return nlg_asset.write_rebuild(source,ps,output)


def build(doc, operations, output):
    ps=patch_set(doc,operations)
    report=write_patch_set(doc.path,ps,output)
    report["runtime"]="unverified"
    return report


def preview(doc,section,clip_index,operations=()):
    clip=nlg_cinematic.camera_clips(doc.sections[section].archive)[clip_index]
    if clip.limitation: raise po_archive.ArchiveError(clip.limitation)
    mine=[op for op in operations if (op.get("section"),op.get("clip"))==(section,clip_index)]
    tracks=_edit_state(doc,mine)[(section,clip_index)][1] if mine else clip.tracks
    positions=list(tracks["position"]); targets=list(tracks.get("target",[])); count=len(positions)
    note="" if count==clip.frames else f" (source {clip.frames})"
    return {"frames":[[]],"numbers":list(range(count)),"path":positions,"targets":targets,
            "label":f"{clip.name} · section {section} · {count} recorded samples{note} · path preview; runtime lens and timing are not simulated"}


def resize_capability(doc,section,clip_index):
    """None when insert/remove is available for this camera, else the reason it is locked."""
    a=doc.sections[section].archive
    return nlg_cinematic.resize_limitation(a,nlg_cinematic.camera_clips(a)[clip_index])
