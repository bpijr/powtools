"""
export_anim_blender.py — run INSIDE Blender (Scripting tab).
Poses you edit on the armature get written back over ONE animation in the game archive.

SETUP:
  1. Set the animation you're fixing as the armature's ACTIVE action (e.g. push/solo the
     'drink_taunt_l' NLA strip down to the active action, or pick it in the Action Editor).
  2. Edit its keyframes however you like (fix the left-hand drink, move the glass, etc.).
  3. Set the configuration below or provide the PO_SOURCE_DICT, PO_HASHID, PO_ANIM and PO_OUT
     environment variables, then run this script.
Output overwrites OUT (drop it into Riivolution like any other build).
"""
import bpy, os, sys, math

# ---- config ----
# Defaults are relative to this script. Override them for a real project; no game files are bundled.
POTOOLS     = os.environ.get("PO_POTOOLS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT        = os.path.dirname(POTOOLS)
SOURCE_DICT = os.environ.get("PO_SOURCE_DICT", os.path.join(ROOT, "art", "characters", "fighter.dict"))
HASHID      = os.environ.get("PO_HASHID", os.path.join(ROOT, "art", "hashid.bin"))
ANIM        = os.environ.get("PO_ANIM", "idle")
OUT         = os.environ.get("PO_OUT", os.path.join(ROOT, "mod_output", "fighter.dict"))
# ----------------

FORMATS = os.path.join(POTOOLS, "formats")
if FORMATS not in sys.path:
    sys.path.append(FORMATS)
import importlib, nlg_anim2, nlg_anim_encode
importlib.reload(nlg_anim2); importlib.reload(nlg_anim_encode)
from nlg_anim2 import Rig, qmul, qconj, qrot, qnorm, WORLD_YAW
import nlg_anim_encode as E


def bone_names(rig):
    used, out = set(), []
    for n in range(rig.nn):
        nm = rig.names[n] or f"node{n}"
        base, k = nm, 1
        while nm in used:
            nm = f"{base}.{k:03d}"; k += 1
        used.add(nm); out.append(nm)
    return out


def run():
    rig = Rig(SOURCE_DICT, HASHID)
    fr, rot, trn, t3 = rig.anims[ANIM]
    names = bone_names(rig)

    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    pbs = [arm.pose.bones.get(names[n]) for n in range(rig.nn)]
    missing = [names[n] for n in range(rig.nn) if pbs[n] is None]
    if missing:
        print("[anim export] WARNING missing bones:", missing[:8])

    rnodes = set(E.rotation_track_nodes(rig, ANIM))
    tnodes = set(E.translation_track_nodes(rig, ANIM))
    dq = {n: [] for n in rnodes}
    tr = {n: [] for n in tnodes}

    scene = bpy.context.scene
    for f in range(fr):
        scene.frame_set(f + 1)
        bpy.context.view_layer.update()
        wq, wp = [None] * rig.nn, [None] * rig.nn
        for n in range(rig.nn):
            pb = pbs[n]
            M = pb.matrix if pb is not None else None
            if M is None:
                wq[n] = (0, 0, 0, 1); wp[n] = (0, 0, 0)
            else:
                q = M.to_quaternion()      # (w,x,y,z)
                wq[n] = (q.x, q.y, q.z, q.w)
                t = M.to_translation()
                wp[n] = (t.x, t.y, t.z)
        for n in range(rig.nn):
            p = rig.par[n]
            if p < 0:
                continue
            if p == 0:
                qn = qnorm(qmul(qconj(WORLD_YAW), wq[n]))
                tn = qrot(qconj(WORLD_YAW), wp[n])
            else:
                qn = qnorm(qmul(qconj(wq[p]), wq[n]))
                dp = tuple(wp[n][k] - wp[p][k] for k in range(3))
                tn = qrot(qconj(wq[p]), dp)
            if n in rnodes: dq[n].append(qn)
            if n in tnodes: tr[n].append(tn)

    E.encode_animation(rig, ANIM, dq, tr)
    if not OUT.endswith(".dict"):
        raise ValueError("OUT must end in .dict")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    rig.a.write(OUT, OUT[:-5] + ".data")
    print(f"[anim export] wrote {ANIM} ({fr} frames) -> {OUT}")


if __name__ == "__main__":
    run()
