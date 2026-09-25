"""Whole NIS cutscenes: shots on one timeline, actor resolution, node poses and edits (no bpy).

An NIS container's section 0 holds the local props (model sets and their 0x8000 rigs).
Every later section holds shots. A shot is a 0x6000 directory owning one 0x6001 header,
one NIS camera (0x5000) and one clip (0x7000) per actor. 0x6001 is four u32 words; words
+8/+12 are the shot's first and last frame on one 30 samples/s timeline (Glass Joe
knockout: 0-24, 25-60, 60-100, 101-120, ...) and last - first + 1 equals the frame count of
the shot's camera and of every clip in it (checked for every shot; see shots()). Word +0
counts camera changes and word +4 stays unnamed.

A clip header (0x7001) +4 is its actor's name hash ("glassjoe", "littlemac", "doc").
Clips of local props repeat the prop rig's name, once per placed instance. An actor is
resolved to the rig with the clip's exact node count and signature: a local section rig,
else characters/<name>.dict, else the one character archive whose rig matches. Clips with
no matching rig anywhere (Dummy*, FX_lensflare*, Circle01) are helpers: their node
hierarchy is unknown, so helpers are shown but never exported.

Poses follow the animation codec (nlg_animation): a node without a rotation track keeps
identity, without a translation track its rig bind translation (0x8010), without a scale
track unit scale; world = parent world x T x R x S. The game's runtime placement, foot
planting, IK and scalar side tracks are not simulated.
"""
from dataclasses import dataclass, field
import glob
import math
import os
import struct
from pathlib import Path

import nlg_animation
import nlg_asset
import nlg_cinematic
import nlg_model

FPS = 30
ROTATION, TRANSLATION, SCALE = 0x7101, 0x7102, 0x7103
CAMERA_TRACKS = ("position", "rotation_xyzw", "angle_radians")

# The arena is loaded by the game, not named in the NIS. This is the circuit each fighter
# fights in; it is a default for the preview and can be changed on import.
CIRCUITS = {"minorcircuit": ("glassjoe", "vonkaiser", "kidquick", "kinghippo"),
            "majorcircuit": ("pistonhondo", "bearhugger", "greattiger", "donflamenco"),
            "worldcircuit": ("aranryan", "sodapopinski", "baldbull", "supermachoman", "sandman", "donkeykong"),
            "traininggym": ("training",)}
ROPES = {"minorcircuit": "ropes.dict", "majorcircuit": "majorropes.dict", "worldcircuit": "worldropes.dict",
         "traininggym": "ropes.dict"}


class CutsceneError(ValueError):
    pass


def _require(ok, message):
    if not ok: raise CutsceneError(message)


@dataclass
class Shot:
    section: int
    index: int                   # order within its section
    directory: int
    words: tuple
    first: int
    last: int
    camera: object               # nlg_cinematic.CameraClip or None
    clips: list = field(default_factory=list)      # [(clip, actor key, instance)]

    @property
    def frames(self): return self.last - self.first + 1


def actor_key(clip):
    head = clip.archive.get_chunk_bytes(nlg_animation._one(clip.archive, clip.chunks, 0x7001))
    return (struct.unpack_from(">I", head, 4)[0], clip.node_count, clip.signature)


def shots(doc):
    """Every shot of an NIS AssetDocument, in timeline order."""
    out = []
    for s in doc.sections:
        a = s.archive
        try: dirs = nlg_cinematic.directories(a)
        except nlg_cinematic.CinematicError: continue
        clips = {c.directory: c for c in s.animations.clips}
        cameras = {c.directory: c for c in nlg_cinematic.camera_clips(a)}
        n = 0
        for d in dirs:
            if d["type"] != 0x6000: continue
            heads = [i for i in d["children"] if a.chunks[i][2] == 0x6001]
            _require(len(heads) == 1, "Section %d shot directory %d has no single 0x6001 header" % (s.index, d["chunk"]))
            raw = a.get_chunk_bytes(heads[0])
            _require(len(raw) == 16, "Section %d: unsupported 0x6001 shot header size" % s.index)
            words = struct.unpack(">4I", raw)
            cams = [cameras[i] for i in d["children"] if i in cameras]
            shot = Shot(s.index, n, d["chunk"], words, words[2], words[3], cams[0] if cams else None)
            _require(shot.last >= shot.first, "Section %d shot %d ends before it starts" % (s.index, n))
            seen = {}
            for i in d["children"]:
                if i not in clips: continue
                clip = clips[i]; key = actor_key(clip)
                _require(clip.frames == shot.frames, "Section %d shot %d: clip '%s' has %d frames, the shot %d"
                         % (s.index, n, clip.name, clip.frames, shot.frames))
                shot.clips.append((clip, key, seen.get(key, 0))); seen[key] = seen.get(key, 0) + 1
            if shot.camera is not None and not shot.camera.limitation:
                _require(shot.camera.frames == shot.frames, "Section %d shot %d: camera '%s' has %d samples, the shot %d"
                         % (s.index, n, shot.camera.name, shot.camera.frames, shot.frames))
            out.append(shot); n += 1
    out.sort(key=lambda sh: (sh.first, sh.section, sh.index))
    return out


def camera_aspect(archive, camera):
    """NIS header +12: authored aspect consumed at PAL 0x8012c61c.
    Camera projection uses 2*atan(tan(angle/2)*authored_aspect/output_aspect)
    in the aspect-adjusting mode; both modes agree at the authored aspect.
    """
    directory = next(d for d in nlg_cinematic.directories(archive) if d['chunk'] == camera.directory)
    headers = [i for i in directory['children'] if archive.chunks[i][2] == 0x5001]
    _require(len(headers) == 1, 'Camera has no unique NIS header')
    raw = archive.get_chunk_bytes(headers[0])
    _require(len(raw) == 48, 'Unsupported NIS header size')
    aspect = struct.unpack_from('>f', raw, 12)[0]
    _require(math.isfinite(aspect) and 0.5 <= aspect <= 4, 'Unsupported camera aspect')
    return aspect


def arena_placed_nodes(archive):
    """Names owned by the arena's BBox/spatial records, or None for unknown layouts.
    6101 is a 40-byte header followed by count(+24) node hashes. This does not
    reconstruct runtime instances of animated assets outside the spatial tree.
    """
    nodes = set()
    for i, chunk in enumerate(archive.chunks):
        if chunk[2] != 0x6101:
            continue
        raw = archive.get_chunk_bytes(i)
        if len(raw) < 40:
            return None
        count = struct.unpack_from('>I', raw, 24)[0]
        if len(raw) != 40 + 4*count:
            return None
        nodes.update(struct.unpack_from('>%dI' % count, raw, 40))
    return nodes or None


def arena_runtime_instances(archive, rigs):
    """Decoded animated-model instances from the arena's 0x6000 world definition.

    Each instance stores the rig's node-1 hash followed by ``(proxy hash, rig-node hash)``
    pairs for nodes 2..N.  The pair order is allowed to differ from rig order.  The world
    matrix begins ``36 + 8*node_count`` bytes after node 1.  This layout accounts for every
    World Circuit long/red banner and laser instance, with no unmatched candidate.  Matrices
    use the archive's normal row-vector convention.
    """
    out = []
    # 0x6000 is a flagged directory chunk, so Archive.find_chunks() intentionally omits it.
    for chunk, row in enumerate(archive.chunks):
        if row[2] != 0x6000: continue
        raw = archive.get_chunk_bytes(chunk)
        for rig in rigs:
            if rig.node_count < 3: continue
            needle = struct.pack(">I", rig.hashes[1])
            pos = 0
            while True:
                pos = raw.find(needle, pos)
                if pos < 0: break
                matrix_at = pos + 36 + 8 * rig.node_count
                # The affine matrix is followed by the instance's authored playback rate
                # and normalized starting phase.  Ignoring these two floats made the arena
                # rigs (most visibly the lasers) run at full clip speed and in lockstep.
                if matrix_at + 108 <= len(raw):
                    pairs = [struct.unpack_from(">II", raw, pos + 4 + 8*i)
                             for i in range(rig.node_count - 2)]
                    node_indices = [rig.hashes.index(node_hash) if node_hash in rig.hashes else -1
                                    for _, node_hash in pairs]
                    if sorted(node_indices) != list(range(2, rig.node_count)):
                        pos += 4
                        continue
                    matrix = struct.unpack_from(">16f", raw, matrix_at)
                    affine = all(math.isfinite(v) for v in matrix) and \
                        all(abs(matrix[i]) < 1e-5 for i in (3, 7, 11)) and abs(matrix[15] - 1.0) < 1e-5
                    if affine:
                        rate, phase = struct.unpack_from(">2f", raw, matrix_at + 100)
                        if not (math.isfinite(rate) and math.isfinite(phase) and
                                0.0 < rate <= 4.0 and -4.0 <= phase <= 4.0):
                            pos += 4
                            continue
                        bindings = [{"proxy_hash": proxy_hash, "node_hash": node_hash, "node": node}
                                    for (proxy_hash, node_hash), node in zip(pairs, node_indices)]
                        out.append({"chunk": chunk, "offset": pos, "matrix_offset": matrix_at,
                                    "rig": rig.index, "rig_name": rig.name, "matrix": matrix,
                                    "animation_rate": rate, "animation_phase": phase,
                                    "bindings": bindings})
                pos += 4
    return out


def arena_crowd_helpers(archive):
    """Decode the arena's authored ``crowd_helperNN`` region records.

    These are fixed 128-byte world-definition records.  The name hash is followed by a
    seven-word header, a normal row-vector affine matrix at +28, then nine runtime crowd
    parameters which are kept verbatim until their individual meanings are established.
    """
    import nlg_hash
    out = []
    for chunk, row in enumerate(archive.chunks):
        if row[2] != 0x6000:
            continue
        raw = archive.get_chunk_bytes(chunk)
        for number in range(100):
            name = "crowd_helper%02d" % number
            needle = struct.pack(">I", nlg_hash.string_to_hash(name))
            pos = raw.find(needle)
            if pos < 0 or pos + 128 > len(raw):
                continue
            header = struct.unpack_from(">7I", raw, pos)
            matrix = struct.unpack_from(">16f", raw, pos + 28)
            affine = all(math.isfinite(v) for v in matrix) and \
                all(abs(matrix[i]) < 1e-5 for i in (3, 7, 11)) and abs(matrix[15] - 1.0) < 1e-5
            if not affine:
                continue
            params = struct.unpack_from(">9f", raw, pos + 92)
            if not all(math.isfinite(v) for v in params):
                continue
            out.append({"chunk": chunk, "offset": pos, "name": name, "number": number,
                        "header": header, "matrix": matrix, "parameters": params})
    return sorted(out, key=lambda r: (r["chunk"], r["offset"]))


def arena_effect_placements(archive, effect_names):
    """Decode the arena's authored effect placements (``env_goldlens``, ``env_camera_flash_world``...).

    They are fixed 160-byte world-definition records in the flagged 0x6000 directory: the effect
    name hash at +0, then (at +80) a normal row-vector affine matrix whose last row is the world
    position.  Returns one dict per placement, in file order; the effect definitions themselves
    (textures, sizes, colour over life) live in effects/effects.dict.
    """
    import nlg_hash
    needles = {struct.pack(">I", nlg_hash.string_to_hash(n)): n for n in effect_names}
    out = []
    for chunk, row in enumerate(archive.chunks):
        if row[2] != 0x6000:
            continue
        raw = archive.get_chunk_bytes(chunk)
        for needle, name in needles.items():
            pos = 0
            while True:
                pos = raw.find(needle, pos)
                if pos < 0:
                    break
                if pos + 160 <= len(raw):
                    matrix = struct.unpack_from(">16f", raw, pos + 80)
                    affine = all(math.isfinite(v) for v in matrix) and                         all(abs(matrix[i]) < 1e-5 for i in (3, 7, 11)) and abs(matrix[15] - 1.0) < 1e-5
                    if affine:
                        out.append({"effect": name, "chunk": chunk, "offset": pos,
                                    "matrix": matrix, "position": tuple(matrix[12:15])})
                pos += 4
    return sorted(out, key=lambda r: (r["chunk"], r["offset"]))


@dataclass
class Actor:
    key: tuple
    instance: int
    name: str
    kind: str                    # "character", "prop" or "helper"
    source: str = None           # character archive (characters) / None (local props, helpers)
    rig: object = None           # nlg_animation.Rig
    model_sets: list = field(default_factory=list)   # local props: section-0 model sets on this rig
    appearances: list = field(default_factory=list)  # [(shot, clip)]
    note: str = ""               # how it was resolved

    @property
    def ident(self): return "%08X:%d:%08X#%d" % (self.key[0], self.key[1], self.key[2], self.instance)


_RIG_INDEX = {}


def character_rigs(characters):
    """{(node count, signature): [(archive path, rig)]} for one characters folder (cached)."""
    characters = os.path.normcase(os.path.abspath(str(characters)))
    if characters not in _RIG_INDEX:
        index = {}
        for path in sorted(glob.glob(os.path.join(characters, "*.dict"))):
            try: doc = nlg_asset.AssetDocument(path)
            except (ValueError, OSError, struct.error): continue
            for s in doc.sections:
                for r in s.animations.rigs: index.setdefault((r.node_count, r.signature), []).append((path, r))
        _RIG_INDEX[characters] = index
    return _RIG_INDEX[characters]


def _external(key, name, clip_name, characters, arena=None):
    """(archive, rig, note) for an actor outside the cutscene, or None."""
    index = character_rigs(characters)
    matches = index.get((key[1], key[2]), [])
    by_file = lambda f: next((m for m in matches if os.path.basename(m[0]).lower() == f), None)
    if name and by_file(name.lower() + ".dict"):
        return by_file(name.lower() + ".dict") + ("name and rig signature",)
    if arena and by_file(ROPES.get(arena, "")):                 # BoxingRing01: the arena's ropes
        return by_file(ROPES[arena]) + ("rig signature; ropes of the %s arena" % arena,)
    files = sorted({m[0] for m in matches})
    if len(files) == 1:
        return next(m for m in matches if m[0] == files[0]) + ("rig signature",)
    # Clones (GreatTiger2_3): a character's clip under another name and signature. Same node
    # count and the character's name as the clip prefix; the node order is assumed equal.
    stem = clip_name.lower(); best = None
    for (count, _), rows in sorted(index.items()):
        if count != key[1]: continue
        for path, rig in rows:
            base = os.path.basename(path).lower()[:-5]
            if len(base) > 3 and stem.startswith(base) and not base.startswith("fe") and (best is None or len(base) > best[0]):
                best = (len(base), path, rig)
    return (best[1], best[2], "node count and name prefix only (signature differs)") if best else None


def art_root(path):
    for p in Path(path).resolve().parents:
        if (p / "hashid.bin").is_file(): return p
    return None


class Cutscene:
    def __init__(self, path, names=None, arena=None):
        self.path = Path(path).resolve()
        if names is None:
            import nlg_hash
            found = nlg_hash.find_hashid_bin(str(self.path))[0]
            names = nlg_hash.load_hashid_bin(found) if found else {}
        self.doc = nlg_asset.AssetDocument(self.path, names)
        _require(len(self.doc.sections) > 1, "Not an NIS cutscene: the container has one section.")
        self.names = self.doc.names or {}
        self.shots = shots(self.doc)
        _require(self.shots, "No 0x6000 shots in this container.")
        self.frames = max(sh.last for sh in self.shots) + 1
        self.root = art_root(self.path)
        self.arena_name = arena or self.arena()
        self.actors = self._actors()

    def name(self, h, default):
        return (self.names.get(h) or default).rstrip("/").split("/")[-1]

    def _actors(self):
        local = [(r, s) for s in self.doc.sections for r in s.animations.rigs]
        actors = {}
        for shot in self.shots:
            for clip, key, instance in shot.clips:
                ident = (key, instance)
                if ident not in actors:
                    rigs = [(r, s) for r, s in local if (r.node_count, r.signature) == key[1:]]
                    if rigs:
                        rig, s = rigs[0]
                        sets = [m for m in s.model_sets if any(m.node_of(me.index).name_hash in rig.hashes for me in m.meshes)
                                or any(h in rig.hashes for h, _ in m.bones)]
                        actor = Actor(key, instance, rig.name.rstrip("/").split("/")[-1], "prop", rig=rig, model_sets=sets)
                    else:
                        name = self.names.get(key[0])
                        found = (_external(key, name, clip.name, self.root / "characters", self.arena_name)
                                 if self.root else None)
                        if found:
                            actor = Actor(key, instance, clip.name, "character", found[0], found[1], note=found[2])
                        else:
                            actor = Actor(key, instance, clip.name, "helper", note="no rig with this node count/signature")
                    actors[ident] = actor
                actors[ident].appearances.append((shot, clip))
        return list(actors.values())

    def arena(self):
        """Default arena folder name for this cutscene (the fighter's circuit), or None."""
        folder = self.path.parent.name.lower().removesuffix("2")
        stem = self.path.stem.lower()
        for circuit in ("minor", "major", "world"):
            if circuit in stem: return circuit + "circuit"
        for arena, fighters in CIRCUITS.items():
            if folder in fighters: return arena
        return "minorcircuit" if folder in ("littlemac", "referee") else None

    def summary(self):
        return {"source": str(self.path), "sha256": self.doc.source_hashes, "frames": self.frames, "fps": FPS,
                "shots": [{"section": sh.section, "index": sh.index, "first": sh.first, "last": sh.last,
                           "camera": sh.camera.name if sh.camera else None, "clips": len(sh.clips)} for sh in self.shots],
                "actors": [{"id": a.ident, "name": a.name, "kind": a.kind, "source": a.source,
                            "shots": len(a.appearances)} for a in self.actors]}


# ---------------------------------------------------------------------------------------
# Poses

def _parents(actor, clip):
    if actor is not None and actor.rig is not None: return list(actor.rig.parents), list(actor.rig.translations)
    return [i - 1 for i in range(clip.node_count)], [(0.0, 0.0, 0.0)] * clip.node_count   # helper: assumed chain


def clip_values(clip):
    """{(node, type): values} for rotation/translation/scale tracks (decoded once)."""
    return {k: t.values() for k, t in clip.tracks.items() if t.type in (ROTATION, TRANSLATION, SCALE)}


def local_pose(clip, values, translations, frame):
    """[(translation, rotation xyzw, scale)] per node for one clip frame."""
    out = []
    for n in range(clip.node_count):
        def key(t, default):
            v = values.get((n, t))
            return default if v is None else v[0 if len(v) == 1 else frame]
        out.append((tuple(key(TRANSLATION, translations[n])), tuple(key(ROTATION, (0.0, 0.0, 0.0, 1.0))),
                    tuple(key(SCALE, (1.0, 1.0, 1.0)))))
    return out


def order(parents):
    done, out = set(), []
    for start in range(len(parents)):
        chain = []; i = start
        while i != -1 and i not in done:
            chain.append(i); i = parents[i]
        for i in reversed(chain): done.add(i); out.append(i)
    return out


def _close(kind, a, b):
    if kind == ROTATION:
        return abs(sum(x * y for x, y in zip(a, b))) >= 1.0 - 2e-6      # ~0.2 degrees
    return all(abs(x - y) <= 1e-4 * max(1.0, abs(y)) for x, y in zip(a, b))


def clip_changes(clip, desired, translations):
    """Track changes for Clip.edit from desired local values {(node, type): [value per frame]}.
    Frames within tolerance keep their exact source value (no requantization); a static
    track that now varies becomes one key per frame. A change on a node channel without a
    source track is refused: the codec cannot add tracks."""
    values = clip_values(clip); changes = {}
    for (node, kind), wanted in desired.items():
        _require(len(wanted) == clip.frames, "Clip '%s' frame count is fixed" % clip.name)
        old = values.get((node, kind))
        if old is None:
            default = {ROTATION: (0.0, 0.0, 0.0, 1.0), TRANSLATION: tuple(translations[node]), SCALE: (1.0, 1.0, 1.0)}[kind]
            _require(all(_close(kind, w, default) for w in wanted),
                     "Clip '%s' node %d has no %s track; that movement cannot be saved"
                     % (clip.name, node, {ROTATION: "rotation", TRANSLATION: "translation", SCALE: "scale"}[kind]))
            continue
        source = [old[0 if len(old) == 1 else f] for f in range(clip.frames)]
        merged = [tuple(s) if _close(kind, w, s) else tuple(w) for w, s in zip(wanted, source)]
        if merged == [tuple(s) for s in source]: continue
        changes[(node, kind)] = [merged[0]] if len(old) == 1 and all(m == merged[0] for m in merged) else merged
    return changes


def camera_changes(clip, desired):
    """{track: values} for position / rotation_xyzw / angle_radians samples that changed."""
    out = {}
    for track in CAMERA_TRACKS:
        if track not in desired: continue
        kind = ROTATION if track == "rotation_xyzw" else TRANSLATION
        old = clip.tracks[track]; new = desired[track]
        _require(len(new) == len(old), "Camera '%s' sample count is fixed in cutscene export" % clip.name)
        merged = [tuple(o) if _close(kind, n, o) else tuple(n) for n, o in zip(new, old)]
        if merged != [tuple(o) for o in old]: out[track] = merged
    return out


def _camera_patches(a, section, clip, track, values):
    _require(not clip.limitation, "Camera '%s' is read-only: %s" % (clip.name, clip.limitation))
    width = {"position": 3, "rotation_xyzw": 4, "angle_radians": 1}[track]
    patches = []; ri = clip.chunks[track]; raw = a.get_chunk_bytes(ri)
    for i, (new, old) in enumerate(zip(values, clip.tracks[track])):
        if tuple(new) == tuple(old): continue
        _require(len(new) == width and all(math.isfinite(x) for x in new), "Camera '%s' %s sample %d is not finite" % (clip.name, track, i))
        if track == "rotation_xyzw":
            norm = math.sqrt(sum(x * x for x in new)); _require(norm > 1e-9, "Zero camera rotation")
            new = tuple(x / norm for x in new)
        try: packed = struct.pack(">%df" % width, *new)
        except (OverflowError, struct.error) as ex: raise CutsceneError("Camera value exceeds float32") from ex
        o = i * width * 4
        patches += nlg_model.diff_patches(ri, o, raw[o:o + width * 4], packed,
                                          "camera %s: %s sample %d" % (clip.name, track.split("_")[0], i), section=section)
    return patches


def patch_set(cutscene, clip_edits=None, camera_edits=None):
    """PatchSet from {(section, clip index): changes} and {(section, camera index): {track: values}}."""
    import nlg_container
    patches, resizes = [], []
    for (si, ci), changes in sorted((clip_edits or {}).items()):
        if not changes: continue
        a = cutscene.doc.sections[si].archive
        clip = cutscene.doc.sections[si].animations.clips[ci]
        try: fixed, resized = clip.edit(changes)
        except nlg_animation.AnimationError as ex: raise CutsceneError("Section %d clip '%s': %s" % (si, clip.name, ex)) from ex
        patches += [nlg_asset.SectionPatch(si, p.chunk, p.offset, p.old, p.new, "section %d: %s" % (si, p.label)) for p in fixed]
        resizes += [nlg_container.resize(a, si, chunk, payload, "section %d: %s" % (si, label)) for chunk, payload, label in resized]
    for (si, ci), tracks in sorted((camera_edits or {}).items()):
        a = cutscene.doc.sections[si].archive
        clip = nlg_cinematic.camera_clips(a)[ci]
        for track, values in sorted(tracks.items()):
            patches += [nlg_asset.SectionPatch(si, p.chunk, p.offset, p.old, p.new, p.label)
                        for p in _camera_patches(a, si, clip, track, values)]
    return nlg_asset.PatchSet(cutscene.doc.source_hashes, patches, resizes=resizes)
