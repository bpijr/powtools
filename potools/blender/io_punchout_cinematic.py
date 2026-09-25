"""Blender cinematic viewer with bounded editing of sampled position/target path meshes.

Import reads AssetDocument; export produces SectionPatch/PatchSet through shared codecs.
Camera objects are previews. Edit vertices of the named position/target path meshes,
then refresh the preview or scrub the sample timeline. No Action API is required.
NIS position paths may gain or lose vertices while they stay one open chain; the export then
resizes the clip and resamples its orientation/lens angle over normalized clip time.
"""
import json
from pathlib import Path
import uuid

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper
from mathutils import Quaternion

import nlg_asset
import nlg_cinematic
import po_errors
import po_cinematics

_DOCS = {}
KEY = "po_cinematic_path"
ROOT = "po_cinematic_root"
VIEW = "po_cinematic_view"


def _document(source):
    source=str(Path(source).resolve())
    if source not in _DOCS: _DOCS[source]=po_cinematics.load(source)
    return _DOCS[source]


def _clips(doc,section):
    if not hasattr(doc,"_camera_clip_cache"):doc._camera_clip_cache={}
    if section not in doc._camera_clip_cache:doc._camera_clip_cache[section]=nlg_cinematic.camera_clips(doc.sections[section].archive)
    return doc._camera_clip_cache[section]


def import_cinematic(source):
    doc=po_cinematics.load(source);_DOCS[str(doc.path.resolve())]=doc
    collection=bpy.data.collections.new(doc.path.stem+" · cinematic")
    bpy.context.scene.collection.children.link(collection)
    session=uuid.uuid4().hex;expected=[];paths=[];cameras=[];maximum=1
    for section in doc.sections:
        local=bpy.data.collections.new(f"Section {section.index}");collection.children.link(local)
        for clip in _clips(doc,section.index):
            if clip.limitation: continue
            maximum=max(maximum,clip.frames)
            meta={"schema":1,"source":str(doc.path.resolve()),"sha256":doc.source_hashes,
                  "section":section.index,"clip":clip.index,"session":session,"samples":clip.frames}
            for track in ("position","target"):
                if track not in clip.tracks:continue
                name=f"S{section.index} · {clip.name} · {track} samples"
                mesh=bpy.data.meshes.new(name);mesh.from_pydata(clip.tracks[track],[(i,i+1) for i in range(clip.frames-1)],[]);mesh.update()
                obj=bpy.data.objects.new(name,mesh);local.objects.link(obj)
                obj[KEY]=json.dumps(dict(meta,track=track));obj.show_in_front=True;paths.append(obj)
                expected.append([section.index,clip.index,track])
            data=bpy.data.cameras.new(clip.name+" · preview");data.lens=50
            camera=bpy.data.objects.new(f"S{section.index} · {clip.name} · preview camera",data);local.objects.link(camera)
            camera[VIEW]=json.dumps(meta);camera.lock_location=(True,True,True);camera.lock_rotation=(True,True,True);camera.lock_scale=(True,True,True)
            cameras.append(camera)
    collection[ROOT]=json.dumps({"schema":1,"source":str(doc.path.resolve()),"sha256":doc.source_hashes,
                                 "session":session,"paths":expected,"cameras":len(cameras)})
    bpy.context.scene.frame_start=1;bpy.context.scene.frame_end=maximum
    bpy.context.scene["po_cinematic_source"]=str(doc.path.resolve())
    if cameras:bpy.context.scene.camera=cameras[0]
    refresh_view(bpy.context.scene)
    return doc,collection,paths,cameras


def _chain(obj):
    """Vertex indices of a single open edge chain in sample order, or None. Orientation: the
    direction in which vertex indices mostly increase (subdivided/extruded vertices get new
    high indices, deletions renumber in order); ties start at the lower-index endpoint."""
    mesh=obj.data;n=len(mesh.vertices)
    if n==0 or mesh.polygons or len(mesh.edges)!=n-1:return None
    links={i:[] for i in range(n)}
    for e in mesh.edges:
        a,b=e.vertices
        if a==b:return None
        links[a].append(b);links[b].append(a)
    if any(len(v)>2 for v in links.values()):return None
    ends=sorted(i for i,v in links.items() if len(v)<2)
    if n>1 and len(ends)!=2:return None
    order=[ends[0]];seen={ends[0]}
    while len(order)<n:
        nxt=[v for v in links[order[-1]] if v not in seen]
        if not nxt:return None
        order.append(nxt[0]);seen.add(nxt[0])
    rising=sum(b>a for a,b in zip(order,order[1:]))
    return order if 2*rising>=n-1 else order[::-1]


def _points(obj):
    """World-space sample points in chain order (None when the path is not one open chain)."""
    n=len(obj.data.vertices)
    canonical={tuple(sorted(e.vertices)) for e in obj.data.edges}=={(i,i+1) for i in range(n-1)} and not obj.data.polygons
    order=list(range(n)) if canonical else _chain(obj)
    if order is None:return None
    return [obj.matrix_world @ obj.data.vertices[i].co for i in order]


def _orientation(doc,clip,section,count):
    """Source orientation, or the normalized-time resampling an export would write."""
    if count==clip.frames:return clip.tracks["rotation_xyzw"]
    cache=doc.__dict__.setdefault("_camera_retime_cache",{})
    key=(section,clip.index,count)
    if key not in cache:cache[key]=nlg_cinematic.retimed(clip.tracks,count)[0]
    return cache[key]


@persistent
def refresh_view(scene,*unused):
    paths={}
    for obj in scene.objects:
        if KEY not in obj:continue
        try:
            meta=json.loads(obj[KEY]);paths[(meta["session"],meta["section"],meta["clip"],meta["track"])]=obj
        except (ValueError,KeyError):continue
    for camera in scene.objects:
        if VIEW not in camera or camera.type!="CAMERA":continue
        try:
            meta=json.loads(camera[VIEW]);doc=_document(meta["source"])
            clip=_clips(doc,meta["section"])[meta["clip"]]
            key=(meta["session"],meta["section"],meta["clip"])
            points=_points(paths[key+("position",)])
            if not points:continue
            index=min(max(scene.frame_current-1,0),len(points)-1)
            camera.location=points[index]
            target=paths.get(key+("target",))
            camera.rotation_mode="QUATERNION"
            if target:
                aims=_points(target)
                if not aims or len(aims)!=len(points):continue
                direction=aims[index]-camera.location
                if direction.length>1e-8:camera.rotation_quaternion=direction.to_track_quat("-Z","Y")
            else:
                x,y,z,w=_orientation(doc,clip,meta["section"],len(points))[index];camera.rotation_quaternion=Quaternion((w,x,y,z))
            camera["po_cinematic_preview_pose"]=list(camera.location)+list(camera.rotation_quaternion)
        except (OSError,ValueError,KeyError,IndexError,AttributeError):continue


def collect_patch_set(source):
    doc=po_cinematics.load(source);resolved=str(doc.path.resolve())
    roots=[]
    for collection in bpy.data.collections:
        if ROOT in collection:
            meta=json.loads(collection[ROOT])
            if meta.get("source")==resolved:roots.append((collection,meta))
    if len(roots)!=1:raise po_errors.Refusal("Keep exactly one cinematic import of this source.",archive=resolved,field="cinematic collections")
    collection,root=roots[0]
    if root.get("schema")!=1 or root["sha256"]!=doc.source_hashes:raise po_errors.Refusal("Cinematic source identity changed.",archive=resolved,field="po_cinematic_root")
    expected={(s.index,c.index,t) for s in doc.sections for c in _clips(doc,s.index) if not c.limitation
              for t in ("position","target") if t in c.tracks}
    if {tuple(k) for k in root["paths"]}!=expected:raise po_errors.Refusal("Cinematic import inventory changed.",archive=resolved,field="po_cinematic_root paths")
    found={};camera_count=0
    for obj in collection.all_objects:
        if VIEW not in obj and KEY not in obj:raise po_errors.Refusal("Cinematic collection contains an extra object; keep only imported sampled paths and previews.",archive=resolved,mesh=obj.name,field="collection membership")
        if VIEW in obj:
            camera_count+=1
            if obj.type!="CAMERA" or obj.data.type!="PERSP" or obj.data.lens!=50 or obj.animation_data or obj.constraints or obj.rotation_mode!="QUATERNION":
                raise po_errors.Refusal("Camera previews are locked. Edit the sampled path mesh vertices.",archive=resolved,mesh=obj.name,field="preview camera lens/type/animation")
            pose=list(obj.location)+list(obj.rotation_quaternion)
            expected_pose=list(obj.get("po_cinematic_preview_pose",[]))
            if len(expected_pose)!=7 or any(abs(x-y)>1e-6 for x,y in zip(pose,expected_pose)) or tuple(obj.scale)!=(1,1,1) or obj.parent or tuple(obj.delta_location)!=(0,0,0) or tuple(obj.delta_scale)!=(1,1,1):
                raise po_errors.Refusal("Camera preview pose is locked. Edit the sampled path mesh vertices.",archive=resolved,mesh=obj.name,field="preview camera pose")
        if KEY not in obj:continue
        meta=json.loads(obj[KEY]);key=(meta["section"],meta["clip"],meta["track"])
        if meta.get("schema")!=1 or meta["sha256"]!=doc.source_hashes or meta["source"]!=resolved or meta["session"]!=root["session"]:
            raise po_errors.Refusal("Camera path provenance changed.",archive=resolved,mesh=obj.name,field=KEY)
        if key in found:raise po_errors.Refusal("Duplicate camera path; remove the duplicate before export.",archive=resolved,section=key[0],mesh=obj.name,field=key[2]+" path")
        found[key]=(obj,meta)
    if set(found)!=expected or camera_count!=root["cameras"]:raise po_errors.Refusal("Camera paths or preview objects were added or removed.",archive=resolved,field="camera inventory")
    patches=[];resizes=[]
    clips_by_section={s.index:_clips(doc,s.index) for s in doc.sections}
    for (si,ci,track),(obj,meta) in sorted(found.items()):
        clip=clips_by_section[si][ci];a=doc.sections[si].archive
        if obj.type!="MESH" or obj.modifiers or obj.constraints or obj.animation_data or obj.data.shape_keys or obj.data.animation_data:
            raise po_errors.Refusal("Only sampled path mesh vertices and object transforms can be edited.",archive=resolved,section=si,mesh=obj.name,field="modifiers/constraints/animation/shape keys")
        if obj.mode=="EDIT":obj.update_from_editmode()
        edges={tuple(sorted(e.vertices)) for e in obj.data.edges}
        if len(obj.data.vertices)!=clip.frames or edges!={(i,i+1) for i in range(clip.frames-1)} or obj.data.polygons:
            points=_points(obj)
            if points is None:raise po_errors.Refusal("Camera path must stay one open chain of vertices; resizing needs an unbroken path.",archive=resolved,section=si,mesh=obj.name,field=track+" path")
            reason=nlg_cinematic.resize_limitation(a,clip) if track=="position" else "target paths keep their sample count."
            if reason:raise po_errors.Refusal("Camera sample count or path topology changed; "+reason,archive=resolved,section=si,mesh=obj.name,field=track+" sample count")
            rotations,angles=nlg_cinematic.retimed(clip.tracks,len(points))
            fixed,more=nlg_cinematic.resize_edit(a,clip,{"position":[tuple(v) for v in points],"rotation_xyzw":rotations,"angle_radians":angles},si)
            patches+=[nlg_asset.SectionPatch(si,p.chunk,p.offset,p.old,p.new,p.label) for p in fixed];resizes+=more
            continue
        for index,(vertex,old) in enumerate(zip(obj.data.vertices,clip.tracks[track])):
            value=tuple(obj.matrix_world @ vertex.co)
            if all(abs(x-y)<=1e-6 for x,y in zip(value,old)):continue
            for p in nlg_cinematic._sample_patches(a,clip,track,index,value):
                patches.append(nlg_asset.SectionPatch(si,p.chunk,p.offset,p.old,p.new,p.label))
    return nlg_asset.PatchSet(doc.source_hashes,patches,resizes=resizes)


def export_cinematic(source,output):
    ps=collect_patch_set(source)
    return po_cinematics.write_patch_set(source,ps,output)


class IMPORT_OT_punchout_cinematic(bpy.types.Operator,ImportHelper):
    bl_idname="import_scene.punchout_cinematic";bl_label="Import Punch-Out!! Cinematic Cameras";bl_options={"REGISTER","UNDO"}
    filename_ext=".dict"
    filter_glob:StringProperty(default="*.dict",options={"HIDDEN"})
    def execute(self,context):
        try:
            _,_,paths,cameras=import_cinematic(self.filepath)
            self.report({"INFO"},f"Imported {len(cameras)} preview cameras and {len(paths)} editable sampled paths. Runtime lens/bindings are not simulated.")
            return {"FINISHED"}
        except (ValueError,OSError) as ex:self.report({"ERROR"},str(ex));return {"CANCELLED"}


class EXPORT_OT_punchout_cinematic(bpy.types.Operator,ExportHelper):
    bl_idname="export_scene.punchout_cinematic";bl_label="Export Punch-Out!! Camera Edits"
    filename_ext=".dict"
    filter_glob:StringProperty(default="*.dict",options={"HIDDEN"})
    source_dict:StringProperty(name="Source archive",subtype="FILE_PATH")
    def invoke(self,context,event):
        self.source_dict=self.source_dict or context.scene.get("po_cinematic_source","")
        self.filepath=self.filepath or context.scene.get("po_cinematic_output","")
        return ExportHelper.invoke(self,context,event)
    def execute(self,context):
        try:
            report=export_cinematic(bpy.path.abspath(self.source_dict),bpy.path.abspath(self.filepath))
            self.report({"INFO"},f"Exported {report['audit']['changed_bytes']} changed bytes; runtime unverified.")
            return {"FINISHED"}
        except (ValueError,OSError) as ex:self.report({"ERROR"},str(ex));return {"CANCELLED"}


class PO_OT_cinematic_refresh(bpy.types.Operator):
    bl_idname="po.cinematic_refresh";bl_label="Refresh camera sample preview"
    def execute(self,context):
        if context.object and context.object.mode=="EDIT":context.object.update_from_editmode()
        refresh_view(context.scene);return {"FINISHED"}


class PO_PT_cinematic(bpy.types.Panel):
    bl_idname="PO_PT_cinematic";bl_label="Cinematic samples";bl_space_type="VIEW_3D";bl_region_type="UI";bl_category="PO Tools"
    @classmethod
    def poll(cls,context):return bool(context.scene.get("po_cinematic_source"))
    def draw(self,context):
        self.layout.label(text="Edit position/target path mesh vertices.")
        self.layout.label(text="Timeline frame 1 = source sample 0.")
        self.layout.label(text="Clips start locally; rig bindings unresolved.")
        self.layout.label(text="NIS position paths: add/remove vertices")
        self.layout.label(text="(one open chain) to resize the clip.")
        self.layout.label(text="Lens, rotation and target counts locked.")
        self.layout.operator("po.cinematic_refresh")
        self.layout.operator("export_scene.punchout_cinematic")


def menu_import(self,context):self.layout.operator(IMPORT_OT_punchout_cinematic.bl_idname,text="Punch-Out!! Cinematic Cameras (.dict)")
def menu_export(self,context):self.layout.operator(EXPORT_OT_punchout_cinematic.bl_idname,text="Punch-Out!! Camera Edits (.dict)")
CLASSES=(IMPORT_OT_punchout_cinematic,EXPORT_OT_punchout_cinematic,PO_OT_cinematic_refresh,PO_PT_cinematic)


def register():
    for cls in CLASSES:bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_import);bpy.types.TOPBAR_MT_file_export.append(menu_export)
    bpy.app.handlers.frame_change_post.append(refresh_view)


def unregister():
    if refresh_view in bpy.app.handlers.frame_change_post:bpy.app.handlers.frame_change_post.remove(refresh_view)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import);bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
    _DOCS.clear()
