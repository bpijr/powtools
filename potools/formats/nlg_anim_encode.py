"""
nlg_anim_encode.py — write animations back into a Punch-Out!! Wii character archive.

The INVERSE of nlg_anim2's decode. Structure-preserving: it keeps every track's exact size and
component layout (full xyzw / 3c / 2c(x,z) / 1c(z), static or per-frame) and only overwrites the
values. That means the edit is in-place, byte-size-identical, needs no resize, and matches whatever
the game expects for that node — the safest possible way to replace a move.

Use:
    rig = nlg_anim2.Rig(dict_path, hashid)
    dq, tr = read_pose_local(rig, 'drink_taunt_r')      # or build your own from Blender
    # ... edit dq / tr ...
    encode_animation(rig, 'drink_taunt_r', dq, tr)      # mutates rig.a in place
    rig.a.write(out_dict, out_data)

dq : {node_index: [ (x,y,z,w) per frame ]}   local rotation quats (same convention decode returns)
tr : {node_index: [ (x,y,z) per frame ]}      local translation (world for bip01), as decode returns
Only nodes that actually own a track are read/written; others are ignored.
"""
import struct
import math


def _clip16(v):
    return max(-32767, min(32767, int(round(v * 32767.0))))


def _qnorm(q):
    n = math.sqrt(sum(x * x for x in q)) or 1.0
    return tuple(x / n for x in q)


def rotation_track_nodes(rig, anim):
    """The node each 0x7101 track maps to, in order (mirrors Rig.pose)."""
    fr, rot, trn, t3 = rig.anims[anim]
    if any(t3):
        return [n for n in range(rig.nn) if t3[n] != 0]
    return [n for n in range(rig.nn) if n != 0]


def translation_track_nodes(rig, anim):
    """The node each 0x7102 track maps to, in order (tflag==0, skipping node0)."""
    return [n for n in range(1, rig.nn) if rig.tflag[n] == 0]


def _run_chunk_ids(rig, anim):
    """Return (rot_ids, trn_ids): archive record indices of this anim's 0x7101 / 0x7102 chunks,
    in file order (same order decode reads them)."""
    a = rig.a
    seq = [(a.chunks[i][2], i) for i in range(a.num_file_entries, len(a.chunks))]
    runs = []
    cur = None
    for t, i in seq:
        if t == 0x7001:
            cur = []
            runs.append(cur)
        if cur is not None:
            cur.append((t, i))
    for r in runs:
        nm = None
        for t, i in r:
            if t == 0x7002:
                nm = a.get_chunk_bytes(i).split(b"\0")[0].decode("latin1")
                break
        if nm == anim:
            rot_ids = [i for t, i in r if t == 0x7101]
            trn_ids = [i for t, i in r if t == 0x7102]
            return rot_ids, trn_ids
    raise KeyError(f"animation {anim!r} not found")


def _track_kind(nbytes, fr):
    """(bytes_per_key, is_static) from a rotation track's byte size — v12 PRECISION formats
    (mirrors nlg_anim2.decode_rot): 8=4xs16 quat, 6=4x12bit quat, 4=4xs8 quat, 2=s16 hinge."""
    for bk in (8, 6, 4, 2):
        if nbytes == bk * fr:
            return bk, False
    for bk in (8, 6, 4, 2):
        if nbytes == bk:
            return bk, True
    raise ValueError(f"rotation track size {nbytes} for {fr} frames")


def encode_rot(quats, nbytes, fr):
    """Encode per-frame local quats into a rotation track of EXACTLY nbytes, in the track's
    original v12 precision format (definitive main.dol decode — NOT component subsets):
      8B/key = 4x s16 /32768 | 6B/key = 4x 12-bit /2048 nibble-packed | 4B/key = 4x s8 /128
      2B/key = 1x s16 wrapping hinge ANGLE about local Z (radians = raw*pi/32768)."""
    bk, static = _track_kind(nbytes, fr)
    keys = 1 if static else fr
    out = bytearray()
    for f in range(keys):
        q = _qnorm(quats[min(f, len(quats) - 1)])
        x, y, z, w = q
        if bk == 2:
            # hinge: rotation about local Z; q must be ~(0,0,sin(a/2),cos(a/2))
            a = 2.0 * math.atan2(z, w)
            raw = int(round(a * 32768.0 / math.pi))
            raw = ((raw + 32768) & 0xFFFF) - 32768           # wrap into s16
            out += struct.pack(">h", raw)
        elif bk == 8:
            out += struct.pack(">4h", *[max(-32768, min(32767, int(round(v * 32768.0))))
                                        for v in (x, y, z, w)])
        elif bk == 6:
            v = [max(-2048, min(2047, int(round(c2 * 2048.0)))) & 0xFFF for c2 in (x, y, z, w)]
            out += bytes(((v[0] >> 4) & 0xFF,
                          ((v[0] & 0xF) << 4) | (v[1] >> 8),
                          v[1] & 0xFF,
                          (v[2] >> 4) & 0xFF,
                          ((v[2] & 0xF) << 4) | (v[3] >> 8),
                          v[3] & 0xFF))
        else:  # 4B = 4x s8
            out += struct.pack(">4b", *[max(-128, min(127, int(round(v * 128.0))))
                                        for v in (x, y, z, w)])
    assert len(out) == nbytes, (len(out), nbytes)
    return bytes(out)


def encode_trn(trans, nbytes, fr):
    """Encode per-frame local translations into a translation track of EXACTLY nbytes."""
    if nbytes == 12:   # static
        x, y, z = trans[0]
        return struct.pack(">3f", x, y, z)
    assert nbytes == 12 * fr, (nbytes, fr)
    out = bytearray()
    for f in range(fr):
        x, y, z = trans[min(f, len(trans) - 1)]
        out += struct.pack(">3f", x, y, z)
    return bytes(out)


def read_pose_local(rig, anim):
    """Decode an existing animation into per-node local rotation quats + local translations
    (the same data encode_animation consumes). Useful for round-trip tests and as a base to edit."""
    fr, rot, trn, t3 = rig.anims[anim]
    rnodes = rotation_track_nodes(rig, anim)
    tnodes = translation_track_nodes(rig, anim)
    dq = {}
    for i, n in enumerate(rnodes):
        dq[n] = rig.decode_rot(rot[i], fr, node=n, flag=t3[n])
    tr = {}
    ti = 0
    for n in tnodes:
        if ti < len(trn):
            tr[n] = rig.decode_trn(trn[ti], fr)
            ti += 1
    return dq, tr


def encode_animation(rig, anim, dq, tr):
    """Overwrite anim's rotation/translation tracks in rig.a from dq/tr (in place, same sizes)."""
    fr, rot, trn, t3 = rig.anims[anim]
    rnodes = rotation_track_nodes(rig, anim)
    tnodes = translation_track_nodes(rig, anim)
    rot_ids, trn_ids = _run_chunk_ids(rig, anim)
    a = rig.a

    for i, n in enumerate(rnodes):
        if n not in dq:
            continue
        orig = a.get_chunk_bytes(rot_ids[i])
        a.replace_chunk(rot_ids[i], encode_rot(dq[n], len(orig), fr))

    ti = 0
    for n in tnodes:
        if ti >= len(trn_ids):
            break
        if n in tr:
            orig = a.get_chunk_bytes(trn_ids[ti])
            a.replace_chunk(trn_ids[ti], encode_trn(tr[n], len(orig), fr))
        ti += 1
