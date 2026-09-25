"""Strict camera clips and NIS section/directory inspection; no UI or Blender code.

Camera ownership comes from directory records, never adjacency to a printable name.
Directory size/offset are immediate-child count/table index (including nested directories).
NIS cameras: 5001 header + 5002 name + 5003 vec3 + 5004 xyzw + 5005 angle.
Sampled cameras: 5030 directory; 5031 sample count, 5003 position, 5023 target,
5004 quaternion, 5028 angle in radians. See CINEMATICS.md for DOL evidence.
Only position and target samples are writable. Counts, lens, quaternion, timing,
directory relationships and resizing remain locked; unknown bytes stay untouched.
"""
from dataclasses import dataclass
import math
import struct
from pathlib import Path

import nlg_model


class CinematicError(ValueError):
    pass


def directories(a):
    """Validated immediate child-index edges; flags retain their unproven low bits."""
    result = []
    for i, (flags, unknown, kind, count, first) in enumerate(a.chunks):
        if flags >> 4 < 8:
            continue
        if first < a.num_file_entries or first + count > len(a.chunks):
            raise CinematicError(f"Directory {i} has children outside the chunk table.")
        result.append({"chunk": i, "type": kind, "children": list(range(first, first + count)),
                       "flags": flags, "unknown": unknown})
    graph = {d["chunk"]: d["children"] for d in result}
    active, done = set(), set()
    for root in graph:
        stack=[(root,False)]
        while stack:
            i,closing=stack.pop()
            if closing: active.remove(i);done.add(i);continue
            if i in active: raise CinematicError("Chunk directory contains a cycle.")
            if i in done or i not in graph: continue
            active.add(i);stack.append((i,True))
            stack.extend((child,False) for child in reversed(graph[i]))
    return result


def _name(raw):
    if b"\0" not in raw: return None
    name, tail = raw.split(b"\0", 1)
    if not name or any(tail) or not all(32 <= c < 127 for c in name): return None
    return name.decode("ascii")


@dataclass
class CameraClip:
    index: int
    directory: int
    name_chunk: int
    name: str
    family: str
    frames: int
    chunks: dict
    tracks: dict
    limitation: str = None

    def summary(self):
        return {"index": self.index, "chunk": self.name_chunk, "directory": self.directory,
                "name": self.name, "family": self.family, "frames": self.frames,
                "sample_rate": 30 if self.family == "nis" else None,
                "duration_seconds": (self.frames - 1) / 30 if self.family == "nis" and self.frames else None,
                "timing_note": "30 samples/s in NIS controller; runtime speed scaling may apply" if self.family == "nis" else "Sample rate not established",
                "editable_tracks": [k for k in ("position", "target") if k in self.tracks] if not self.limitation else [],
                "limitation": self.limitation,
                "tracks": [{"kind": k, "chunk": self.chunks[k], "samples": len(v), "first_sample": v[0] if v else None}
                           for k, v in self.tracks.items()]}


def camera_clips(a):
    """Return only directory-owned cameras. Unsupported known families remain inspectable."""
    ds = directories(a); result = []
    owners = {}
    for d in ds:
        for i in d["children"]: owners.setdefault(i, []).append(d["chunk"])
    for d in ds:
        if d["type"] not in (0x5000, 0x5030): continue
        children = d["children"]
        by = {}
        for i in children: by.setdefault(a.chunks[i][2], []).append(i)
        if len(by.get(0x5002, [])) != 1: continue
        ni = by[0x5002][0]; name = _name(a.get_chunk_bytes(ni))
        # 5000 also owns unrelated hashed controllers; their explicit 501x family wins.
        if name is None or any(t in by for t in (0x5011, 0x5012, 0x5013)): continue
        family = "nis" if d["type"] == 0x5000 else "sampled"
        clip = CameraClip(len(result), d["chunk"], ni, name, family, 0, {}, {})
        result.append(clip)
        try:
            if (d["flags"],d["unknown"]) != (0x93,2): raise CinematicError("Unsupported camera directory signature.")
            if any(len(v) != 1 for v in by.values()): raise CinematicError("Duplicate camera chunk type.")
            if any(len(owners[i]) != 1 for i in children): raise CinematicError("Camera chunks have shared directory ownership.")
            if any(a._chunk_block(i) < 0 for i in children): raise CinematicError("Nested camera directory is unsupported.")
            one = {t: v[0] for t, v in by.items()}
            header = a.get_chunk_bytes(one[0x5001 if family == "nis" else 0x5031])
            if len(header) != (48 if family == "nis" else 4): raise CinematicError("Unsupported camera header size.")
            clip.frames = struct.unpack_from(">I", header, 8 if family == "nis" else 0)[0]
            if not 0 < clip.frames <= 100000: raise CinematicError("Camera sample count is outside supported bounds.")
            if family == "nis" and children[:5] != [one[t] for t in (0x5001, 0x5002, 0x5003, 0x5004, 0x5005)]:
                raise CinematicError("NIS camera positional directory order changed.")
            specs = [(0x5003, "position", 3), (0x5004, "rotation_xyzw", 4),
                     (0x5005 if family == "nis" else 0x5028, "angle_radians", 1)]
            if family == "sampled": specs.append((0x5023, "target", 3))
            for kind, label, width in specs:
                i = one[kind]; raw = a.get_chunk_bytes(i)
                if a.chunks[i][1] != 2: raise CinematicError("Unsupported camera track discriminator.")
                if family == "sampled" and one[0x5031] > i: raise CinematicError("Camera sample count follows its tracks.")
                if len(raw) != clip.frames * width * 4: raise CinematicError(f"{label} sample count differs from header.")
                values = list(struct.iter_unpack(">%df" % width, raw))
                if not all(math.isfinite(v) for row in values for v in row): raise CinematicError(f"{label} has nonfinite samples.")
                if label == "rotation_xyzw" and any(abs(sum(v*v for v in row)-1) > 0.002 for row in values):
                    raise CinematicError("Camera quaternion is not unit length.")
                clip.chunks[label] = i; clip.tracks[label] = values
        except (KeyError, struct.error, CinematicError) as ex:
            clip.limitation = str(ex)
    return result


def camera_patches(a, clip_index, track, sample, value):
    """Patch one proven vec3 sample. Header, other tracks and ancillary bytes are untouched."""
    clips = camera_clips(a)
    if type(clip_index) is not int or not 0 <= clip_index < len(clips): raise CinematicError("Choose a camera clip.")
    clip = clips[clip_index]
    return _sample_patches(a, clip, track, sample, value)


def _sample_patches(a, clip, track, sample, value):
    """Internal batch helper; clip must come from camera_clips(a), not user metadata."""
    if clip.limitation: raise CinematicError("Camera is read-only: " + clip.limitation)
    if track not in ("position", "target") or track not in clip.tracks: raise CinematicError("Only existing position and target samples are editable.")
    if type(sample) is not int or not 0 <= sample < clip.frames: raise CinematicError("Sample is outside the existing track.")
    if not isinstance(value, (list, tuple)) or len(value) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        raise CinematicError("Use three finite coordinates.")
    try: new = struct.pack(">3f", *value)
    except (OverflowError, struct.error) as ex: raise CinematicError("Coordinates exceed float32 range.") from ex
    ri = clip.chunks[track]; old = a.get_chunk_bytes(ri)[sample*12:sample*12+12]
    return nlg_model.diff_patches(ri, sample*12, old, new, f"camera {clip.name}: {track} sample {sample}")


NIS_TRACKS = {0x5001: "header", 0x5002: "name", 0x5003: "position", 0x5004: "rotation_xyzw", 0x5005: "angle_radians"}
MIN_SAMPLES, MAX_SAMPLES = 2, 100000   # corpus minimum is 2; the controller divides by (N - 1)


def resize_limitation(a, clip):
    """Why a clip's sample count is locked, or None. Only NIS 5001..5005 clips qualify: their
    header +8 is the only count (DOL 0x8012c198) and the three tracks are proven per-sample
    arrays. Sampled cameras carry 5022/5024/5025/5026 arrays of the same length whose meaning
    is unproven, so no value could be chosen for an inserted sample."""
    if clip.limitation: return "Camera is read-only: " + clip.limitation
    if clip.family != "nis":
        return "Sampled cameras also store per-sample 0x5022/0x5024/0x5025/0x5026 arrays with unproven meaning; sample count is locked."
    d = a.chunks[clip.directory]
    extra = sorted({a.chunks[i][2] for i in range(d[4], d[4] + d[3])} - set(NIS_TRACKS))
    if extra:
        return "This camera also owns unproven chunk(s) %s; sample count is locked." % ", ".join("0x%04X" % t for t in extra)
    return None


def _unit(q):
    n = math.sqrt(sum(x * x for x in q))
    if not math.isfinite(n) or n < 1e-12: raise CinematicError("Camera rotation must be a nonzero quaternion.")
    return tuple(x / n for x in q)


def slerp(q0, q1, t):
    """Shortest-arc spherical interpolation of xyzw quaternions (inserted/retimed samples only)."""
    q0, q1 = _unit(q0), _unit(q1); dot = sum(a * b for a, b in zip(q0, q1))
    if dot < 0: q1 = tuple(-x for x in q1); dot = -dot
    if dot > 0.9995: return _unit(tuple(a + (b - a) * t for a, b in zip(q0, q1)))
    theta = math.acos(min(1.0, dot)); s = math.sin(theta)
    return _unit(tuple((math.sin((1 - t) * theta) * a + math.sin(t * theta) * b) / s for a, b in zip(q0, q1)))


def insert_sample(tracks, index, position):
    """New track lists with a sample inserted before `index` (0..N). Orientation and lens
    angle of the new sample interpolate its neighbours (copied at either end); every other
    sample is untouched."""
    n = len(tracks["position"])
    if type(index) is not int or not 0 <= index <= n: raise CinematicError("Insert position is outside the clip.")
    lo, hi = max(index - 1, 0), min(index, n - 1)
    rot = tracks["rotation_xyzw"][lo] if lo == hi else slerp(tracks["rotation_xyzw"][lo], tracks["rotation_xyzw"][hi], 0.5)
    angle = tracks["angle_radians"][lo] if lo == hi else ((tracks["angle_radians"][lo][0] + tracks["angle_radians"][hi][0]) / 2,)
    out = {k: list(v) for k, v in tracks.items()}
    for key, value in (("position", tuple(position)), ("rotation_xyzw", tuple(rot)), ("angle_radians", tuple(angle))):
        out[key].insert(index, value)
    return out


def remove_sample(tracks, index):
    n = len(tracks["position"])
    if type(index) is not int or not 0 <= index < n: raise CinematicError("Sample is outside the clip.")
    if n - 1 < MIN_SAMPLES: raise CinematicError("A NIS camera needs at least %d samples." % MIN_SAMPLES)
    return {k: v[:index] + v[index + 1:] for k, v in tracks.items()}


def retimed(tracks, count):
    """Orientation/lens angle resampled over normalized clip time for a path of `count` samples
    (Blender edits where individual inserted vertices cannot be identified)."""
    n = len(tracks["rotation_xyzw"])
    if count == n: return list(tracks["rotation_xyzw"]), list(tracks["angle_radians"])
    rotations, angles = [], []
    for j in range(count):
        t = j * (n - 1) / (count - 1) if count > 1 else 0.0
        i = min(int(t), n - 2); f = t - i
        if f == 0: rotations.append(tracks["rotation_xyzw"][i]); angles.append(tracks["angle_radians"][i]); continue
        if i + 1 >= n or f == 1:
            rotations.append(tracks["rotation_xyzw"][i + 1]); angles.append(tracks["angle_radians"][i + 1]); continue
        rotations.append(slerp(tracks["rotation_xyzw"][i], tracks["rotation_xyzw"][i + 1], f))
        a0, a1 = tracks["angle_radians"][i][0], tracks["angle_radians"][i + 1][0]
        angles.append((a0 + (a1 - a0) * f,))
    return rotations, angles


def _pack(values, width, label):
    out = bytearray()
    for v in values:
        if not isinstance(v, (list, tuple)) or len(v) != width or any(type(x) not in (int, float) or not math.isfinite(x) for x in v):
            raise CinematicError(f"{label} needs {width} finite numbers per sample.")
        try: out += struct.pack(">%df" % width, *v)
        except (OverflowError, struct.error) as ex: raise CinematicError(f"{label} exceeds float32 range.") from ex
    return bytes(out)


def resize_edit(a, clip, tracks, section=0):
    """(fixed patches, nlg_container.Resize list) giving a NIS clip len(position) samples.
    Header +8 (sample count) is the only coupled field; every other header byte, the name and
    all other chunks are preserved. Section re-layout is left to the container writer."""
    import nlg_container
    reason = resize_limitation(a, clip)
    if reason: raise CinematicError(reason)
    n = len(tracks["position"])
    if not MIN_SAMPLES <= n <= MAX_SAMPLES: raise CinematicError("A NIS camera needs %d to %d samples." % (MIN_SAMPLES, MAX_SAMPLES))
    if len(tracks["rotation_xyzw"]) != n or len(tracks["angle_radians"]) != n: raise CinematicError("Camera tracks disagree on sample count.")
    for q in tracks["rotation_xyzw"]:
        if not isinstance(q, (list, tuple)) or len(q) != 4 or any(type(x) not in (int, float) or not math.isfinite(x) for x in q) \
                or abs(sum(x * x for x in q) - 1) > 0.002:
            raise CinematicError("Camera rotation samples must be unit quaternions.")
    payloads = {"position": _pack(tracks["position"], 3, "Position"), "rotation_xyzw": _pack(tracks["rotation_xyzw"], 4, "Rotation"),
                "angle_radians": _pack(tracks["angle_radians"], 1, "Lens angle")}
    head = clip.chunks.get("header") or next(i for i in range(a.chunks[clip.directory][4], a.chunks[clip.directory][4] + a.chunks[clip.directory][3])
                                            if a.chunks[i][2] == 0x5001)
    old = a.get_chunk_bytes(head)[8:12]
    patches = nlg_model.diff_patches(head, 8, old, struct.pack(">I", n), f"camera {clip.name}: sample count {clip.frames} -> {n}", section=section)
    resizes = [nlg_container.resize(a, section, clip.chunks[k], payloads[k], f"camera {clip.name}: {k} {clip.frames} -> {n} samples")
               for k in ("position", "rotation_xyzw", "angle_radians") if a.get_chunk_bytes(clip.chunks[k]) != payloads[k]]
    return patches, resizes


def relationships(section):
    """Explicit containment only. Co-resident models/rigs are never asserted as bindings."""
    a = section.archive; result = []
    for d in directories(a):
        result.append({"relation": "directory contains", "directory": d["chunk"], "type": f"0x{d['type']:04X}", "chunks": d["children"]})
    for model in section.model_sets:
        result.append({"relation": "model owns rig" if model.skinned else "static model", "model_set": model.index,
                       "chunks": model.chunk_ids(), "bones": len(model.bones)})
    return result


def rebuild_sections(path, patch_set):
    """Fixed-size section patches; dictionary, directories, offsets and gaps never move.

    Aliased payloads, invalid addresses, overlapping patches and all resizing are refused.
    The outer container is copied and each rebuilt section replaces its original span.
    """
    import po_archive
    from nlg_asset import sha256, _chunk_data_offset
    path = Path(path); dd = path.read_bytes(); data = path.with_suffix(".data").read_bytes()
    if [sha256(dd), sha256(data)] != patch_set.source_hashes: raise CinematicError("Patch set was made from a different source archive.")
    parts = po_archive.sections(dd); archives = po_archive.load_sections(path)
    if len(parts) > 1 and any(p["offset"] % 2048 for p in parts): raise CinematicError("NIS section alignment is unsupported.")
    for a in archives: directories(a)
    allowed = []; by_section = {}
    for p in patch_set.patches:
        si = getattr(p, "section", 0)
        if type(si) is not int or not 0 <= si < len(archives): raise CinematicError("Patch section is outside the source.")
        a = archives[si]
        if type(p.chunk) is not int or p.chunk not in a.find_chunks(): raise CinematicError("Patch must address a data chunk.")
        if type(p.offset) is not int or p.offset < 0 or len(p.old) != len(p.new) or p.offset + len(p.new) > a.chunks[p.chunk][3]:
            raise CinematicError("Only fixed-size patches inside an existing chunk are supported.")
        block = a._chunk_block(p.chunk); off = a.chunks[p.chunk][4]; size = a.chunks[p.chunk][3]
        for ri in a.find_chunks(block=block):
            other_off = a.chunks[ri][4]; other_size = a.chunks[ri][3]
            if ri != p.chunk and size and other_size and off < other_off + other_size and other_off < off + size:
                raise CinematicError("Patched chunk has overlapping/aliased storage.")
        start = parts[si]["offset"] + _chunk_data_offset(a, p.chunk) + p.offset
        span = (start, start + len(p.new))
        if any(span[0] < hi and lo < span[1] for lo, hi in allowed): raise CinematicError("Patch ranges overlap.")
        allowed.append(span); by_section.setdefault(si, []).append(p)
    out = bytearray(data)
    for si, patches in by_section.items():
        a = archives[si]; nlg_model.apply_patches(a, patches)
        body = a.build_data(); part = parts[si]
        if a.build_dict() != part["dictionary"] or len(body) != part["length"]: raise CinematicError("Patch changed section layout.")
        out[part["offset"]:part["offset"] + part["length"]] = body
    changed = nlg_model.changed_ranges(data, out)
    # Adjacent explicit patches may jointly cover one contiguous changed range.
    merged = []
    for lo, hi in sorted(allowed):
        if merged and lo <= merged[-1][1]: merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else: merged.append((lo, hi))
    if any(not any(lo <= start and end <= hi for lo, hi in merged) for start, end in changed):
        raise CinematicError("Bytes outside recorded patch ranges changed.")
    result = bytes(out)
    return dd, result, {"changed_ranges": changed, "changed_bytes": sum(hi-lo for lo,hi in changed),
                        "patches": len(patch_set.patches), "sections_changed": sorted(by_section),
                        "output_sha256": [sha256(dd), sha256(result)]}
