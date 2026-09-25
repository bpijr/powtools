"""Strict animation directories, rigs and fixed-size track patches (no Blender dependency).

0x7000/0x8000 and 0x7100 are chunk-table directories, not payloads. Following their
record ranges gives exact track -> node ownership, including compact and environment
rigs. DOL 0x801819b8 dispatches these per-node records. Rotation flags select the
2/4/6/8-byte codecs; translation is f32 XYZ; scale is u16 XYZ / 2048 (0x80188c20).
0x7112 is normalized u8 (0x80188c8c), with key count in 0x7110: inspect-only because
its final consumer semantics are not established. Unknown records are never rewritten.

Whole-track re-encoding (Clip.edit) may change a track between one static key and one key
per clip frame, or change a rotation track's layout. The only coupled fields are the node's
0x7003 static/layout bits (samplers 0x80181ACC/0x80181E1C/0x80182180 test bits 0x02/0x08/0x04,
then 0x01/0x10/0x20, and index animated keys by the clip frame count at header +8) and the
chunk size; AnimSetup_ChunkPointers (0x801819B8) takes track pointers from chunk records.
The clip frame count itself stays fixed: header +52 (equal to it in 2,976 clips) and the
per-frame 0x7007/0x7008/0x700B/0x7009 morph tables are coupled to it with unproven meaning.
"""
import hashlib
import math
import struct

import nlg_model


class AnimationError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise AnimationError(message)


def _words(raw, count, code="I"):
    _require(len(raw) == count * 4, "Node table length does not match its declared count")
    return struct.unpack(">%d%s" % (count, code), raw)


def _directory(a, ri, type_):
    flags, _, actual, count, offset = a.chunks[ri]
    _require(actual == type_ and flags & 0x80, "Expected a chunk-table directory")
    _require(a.num_file_entries <= offset <= len(a.chunks) and offset + count <= len(a.chunks),
             "Directory points outside the chunk table")
    return list(range(offset, offset + count))


def _one(a, ids, type_, optional=False):
    found = [i for i in ids if a.chunks[i][2] == type_]
    _require(len(found) == 1 or (optional and not found), "Missing or duplicate 0x%04X record" % type_)
    if not found:
        return None
    _require(not a.chunks[found[0]][0] & 0x80, "Payload points at a directory")
    return found[0]


def _name(a, ids, type_):
    ri = _one(a, ids, type_)
    raw = a.get_chunk_bytes(ri)
    _require(b"\0" in raw, "Unterminated animation name")
    return raw.split(b"\0", 1)[0].decode("latin1")


class Rig:
    def __init__(self, archive, directory, index):
        self.archive, self.directory, self.index = archive, directory, index
        self.chunks = _directory(archive, directory, 0x8000)
        get = lambda t: archive.get_chunk_bytes(_one(archive, self.chunks, t))
        head = get(0x8001)
        _require(len(head) == 56, "Unsupported rig header length")
        self.node_count, self.signature = struct.unpack_from(">II", head, 8)
        _require(0 < self.node_count <= 65536, "Invalid rig node count")
        self.name = _name(archive, self.chunks, 0x8002)
        self.hashes = _words(get(0x8003), self.node_count)
        self.parents = _words(get(0x8009), self.node_count, "i")
        raw = get(0x8010)
        _require(len(raw) == self.node_count * 12, "Invalid local translation table")
        self.translations = tuple(struct.iter_unpack(">3f", raw))
        _require(all(math.isfinite(x) for v in self.translations for x in v), "Nonfinite rig translation")
        self.translation_flags = tuple(get(0x8011))
        _require(len(self.translation_flags) == self.node_count and set(self.translation_flags) <= {0, 1},
                 "Invalid rig translation flags")
        self.order = self._order()
        self.fingerprint = hashlib.sha256(b"".join(get(t) for t in (0x8003, 0x8009, 0x8010, 0x8011))).hexdigest()

    def _order(self):
        n = self.node_count
        _require(all(-1 <= p < n and p != i for i, p in enumerate(self.parents)), "Invalid rig parent")
        order, done = [], set()
        for start in range(n):
            chain, seen = [], set()
            i = start
            while i != -1 and i not in done:
                _require(i not in seen, "Rig hierarchy contains a cycle")
                seen.add(i); chain.append(i); i = self.parents[i]
            for i in reversed(chain):
                done.add(i); order.append(i)
        return tuple(order)

    def bone_parents(self, bone_hashes):
        """Nearest represented ancestor by exact node hash; ambiguous hashes are refused."""
        _require(len(set(bone_hashes)) == len(bone_hashes), "Duplicate bind bone hashes")
        mapping = {}
        for h in bone_hashes:
            ids = [i for i, value in enumerate(self.hashes) if value == h]
            _require(len(ids) == 1, "Bind bone is absent or ambiguous in the rig node table")
            mapping[ids[0]] = h
        result = {}
        for node, h in mapping.items():
            p = self.parents[node]
            while p != -1 and p not in mapping:
                p = self.parents[p]
            result[h] = mapping.get(p)
        return result


def _finite(value, components):
    _require(isinstance(value, (list, tuple)) and len(value) == components, "Wrong track component count")
    _require(all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in value),
             "Track values must be finite numbers")
    return tuple(float(x) for x in value)


def _quat(value):
    q = _finite(value, 4)
    norm = math.hypot(*q)
    _require(math.isfinite(norm) and norm > 1e-12, "Zero or overflowing quaternion is not a rotation")
    return tuple(x / norm for x in q)


STATIC_BIT = {0x7101: 0x02, 0x7102: 0x04, 0x7103: 0x08}
LAYOUTS = {"hinge": (0x01, 2), "s16": (0x10, 8), "s12": (0x20, 6), "s8": (0x00, 4)}   # 0x7003 bit, key bytes
LAYOUT_BITS = 0x31
CLIP_LENGTH_LOCKED = ("The clip frame count is fixed: header +52 and the per-frame 0x7007/0x7008/0x700B/0x7009 "
                      "tables depend on it with unproven meaning. A track holds one static key or one key per frame.")


class Track:
    def __init__(self, archive, chunk, node, flag, frames, scalar_keys=None):
        self.archive, self.chunk, self.node = archive, chunk, node
        self.type, self.flag, self.frames = archive.chunks[chunk][2], flag, frames
        self.raw = archive.get_chunk_bytes(chunk)
        if self.type == 0x7101:
            self.kind = "rotation"
            self.stride = 2 if flag & 1 else 8 if flag & 0x10 else 6 if flag & 0x20 else 4
            self.keys = 1 if flag & 2 else frames
        elif self.type == 0x7102:
            self.kind, self.stride = "translation", 12
            self.keys = 1 if flag & 4 else frames
        elif self.type == 0x7103:
            self.kind, self.stride = "scale", 6
            self.keys = 1 if flag & 8 else frames
        elif self.type == 0x7112:
            self.kind, self.stride, self.keys = "normalized_scalar", 1, scalar_keys
        else:
            raise AnimationError("Unsupported track type")
        _require(type(self.keys) is int and self.keys > 0 and len(self.raw) == self.keys * self.stride,
                 "0x%04X node %d: flags/key count disagree with track length" % (self.type, node))

    def values(self):
        out = []
        for offset in range(0, len(self.raw), self.stride):
            if self.type == 0x7102:
                v = struct.unpack_from(">3f", self.raw, offset)
                _require(all(math.isfinite(x) for x in v), "Nonfinite translation key")
            elif self.type == 0x7103:
                v = tuple(x / 2048 for x in struct.unpack_from(">3H", self.raw, offset))
            elif self.type == 0x7112:
                v = (self.raw[offset] / 255,)
            elif self.stride == 2:
                angle = struct.unpack_from(">h", self.raw, offset)[0] * math.pi / 32768
                v = (0.0, 0.0, math.sin(angle / 2), math.cos(angle / 2))
            elif self.stride == 6:
                b = self.raw[offset:offset + 6]
                v = [(b[0] << 4) | (b[1] >> 4), ((b[1] & 15) << 8) | b[2],
                     (b[3] << 4) | (b[4] >> 4), ((b[4] & 15) << 8) | b[5]]
                v = _quat([float(x - 4096 if x >= 2048 else x) for x in v])
            else:
                v = _quat(struct.unpack_from(">4" + ("h" if self.stride == 8 else "b"), self.raw, offset))
            out.append(v)
        return out

    @property
    def layout(self):
        return {2: "hinge", 8: "s16", 6: "s12", 4: "s8"}[self.stride] if self.type == 0x7101 else None

    def _encode(self, value, stride=None):
        stride = stride or self.stride
        if self.type == 0x7102:
            v = _finite(value, 3)
            try:
                return struct.pack(">3f", *v)
            except (OverflowError, struct.error) as ex:
                raise AnimationError("Translation is outside f32 range") from ex
        if self.type == 0x7103:
            v = _finite(value, 3)
            _require(all(0 <= x <= 65535 / 2048 for x in v), "Scale is outside u16/2048 range")
            return struct.pack(">3H", *(round(x * 2048) for x in v))
        q = _quat(value)
        if stride == 2:
            _require(abs(q[0]) < 1e-7 and abs(q[1]) < 1e-7, "Hinge track only supports rotation about local Z")
            v = round(2 * math.atan2(q[2], q[3]) * 32768 / math.pi)
            return struct.pack(">h", ((v + 32768) % 65536) - 32768)
        scale = {8: 32768, 6: 2048, 4: 128}[stride]
        ints = [max(-scale, min(scale - 1, round(x * scale))) for x in q]
        if stride != 6:
            return struct.pack(">4" + ("h" if stride == 8 else "b"), *ints)
        v = [x & 4095 for x in ints]
        return bytes((v[0] >> 4, ((v[0] & 15) << 4) | (v[1] >> 8), v[1] & 255,
                      v[2] >> 4, ((v[2] & 15) << 4) | (v[3] >> 8), v[3] & 255))

    def reencode(self, values, layout=None, flag=None, reuse=True):
        """(payload, 0x7003 flag word) storing `values` as one static key or one key per clip
        frame, optionally in another rotation layout. Keys equal to the decoded source key at
        the same position (key 0 across static/animated changes) reuse its bytes, so an
        unchanged track re-encodes byte-identically; `reuse=False` measures pure requantization."""
        _require(self.type != 0x7112, "Normalized scalar track meaning is unproved; editing is locked")
        _require(isinstance(values, (list, tuple)) and len(values) in (1, self.frames), CLIP_LENGTH_LOCKED)
        flag = self.flag if flag is None else flag
        layout = layout or self.layout
        _require(layout == self.layout if self.type != 0x7101 else layout in LAYOUTS, "Only rotation tracks have alternative layouts")
        stride = LAYOUTS[layout][1] if self.type == 0x7101 else self.stride
        bit = STATIC_BIT[self.type]
        static = len(values) == 1 and (self.frames > 1 or bool(self.flag & bit))
        if static != bool(self.flag & bit): flag ^= bit
        if layout != self.layout: flag = flag & ~LAYOUT_BITS | LAYOUTS[layout][0]
        old = self.values(); out = bytearray()
        for i, value in enumerate(values):
            checked = _finite(value, 4 if self.kind == "rotation" else 3)
            j = i if len(values) == self.keys else 0
            if reuse and stride == self.stride and checked == old[j]:
                out += self.raw[j * stride:(j + 1) * stride]
            else:
                out += self._encode(checked, stride)
        return bytes(out), flag

    def patches(self, values, label="animation"):
        _require(self.type != 0x7112, "Normalized scalar track meaning is unproved; editing is locked")
        _require(isinstance(values, (list, tuple)) and len(values) == self.keys,
                 "Key count changed; static tracks and clip length cannot be resized")
        old = self.values(); out = []
        for i, (before, after) in enumerate(zip(old, values)):
            checked = _finite(after, 4 if self.kind == "rotation" else 3)
            if checked == before:
                continue  # normalization must never requantize an untouched key
            encoded = self._encode(checked)
            offset = i * self.stride
            out += nlg_model.diff_patches(self.chunk, offset, self.raw[offset:offset + self.stride], encoded,
                                         "%s: node %d %s key %d changed" % (label, self.node, self.kind, i))
        if out:
            a = self.archive; start = a.chunks[self.chunk][4]; end = start + len(self.raw)
            for ri in a.find_chunks(block=a._chunk_block(self.chunk)):
                _, _, _, size, offset = a.chunks[ri]
                _require(ri == self.chunk or size == 0 or offset >= end or start >= offset + size,
                         "Track storage overlaps another payload; editing is locked")
        return out


class Clip:
    def __init__(self, archive, directory, index):
        self.archive, self.directory, self.index = archive, directory, index
        self.chunks = _directory(archive, directory, 0x7000)
        get = lambda t: archive.get_chunk_bytes(_one(archive, self.chunks, t))
        head = get(0x7001)
        _require(len(head) == 88, "Unsupported clip header length")
        self.frames, self.node_count = struct.unpack_from(">II", head, 8)
        self.signature = struct.unpack_from(">I", head, 84)[0]
        _require(0 < self.frames <= 1000000 and 0 < self.node_count <= 65536, "Invalid clip dimensions")
        self.name = _name(archive, self.chunks, 0x7002)
        self.flags = _words(get(0x7003), self.node_count)
        scalar_ri = _one(archive, self.chunks, 0x7110, optional=True)
        scalar_counts = _words(archive.get_chunk_bytes(scalar_ri), self.node_count) if scalar_ri is not None else (0,) * self.node_count
        nodes = [i for i in self.chunks if archive.chunks[i][2] == 0x7100]
        _require(len(nodes) == self.node_count, "Node directory count disagrees with clip header")
        self.tracks, self.unknown, seen = {}, [], set()
        for node, ri in enumerate(nodes):
            for child in _directory(archive, ri, 0x7100):
                _require(child not in seen, "Track is shared by multiple nodes")
                seen.add(child)
                _require(not archive.chunks[child][0] & 0x80, "Nested track directory is unsupported")
                type_ = archive.chunks[child][2]
                if type_ not in (0x7101, 0x7102, 0x7103, 0x7112):
                    self.unknown.append(child); continue
                key = (node, type_)
                _require(key not in self.tracks, "Duplicate track type on one node")
                self.tracks[key] = Track(archive, child, node, self.flags[node], self.frames, scalar_counts[node])
        self.chunks += sorted(seen)

    def compatible(self, rig):
        _require(self.node_count == rig.node_count and self.signature == rig.signature,
                 "Clip and rig node count/signature differ; retargeting is not supported")
        return True

    def patches(self, changes, rig=None):
        if rig is not None:
            self.compatible(rig)
        out = []
        for key, values in changes.items():
            _require(key in self.tracks, "Cannot add a track absent from the source clip")
            out += self.tracks[key].patches(values, self.name)
        return out

    def edit(self, changes, rig=None, layouts=None):
        """(fixed patches, [(chunk, payload, label)]) for key edits and whole-track re-encodes.
        Unchanged key count and layout -> fixed-size patches exactly as patches(); otherwise the
        track is rebuilt (one static key or one key per clip frame) and its node's 0x7003
        static/layout bits are patched. The caller relays resized chunks (nlg_container)."""
        if rig is not None:
            self.compatible(rig)
        layouts = layouts or {}
        patches, resized, flags = [], [], list(self.flags)
        for key in sorted(set(changes) | set(layouts)):
            _require(key in self.tracks, "Cannot add a track absent from the source clip")
            track = self.tracks[key]; layout = layouts.get(key)
            values = changes[key] if key in changes else track.values()
            _require(isinstance(values, (list, tuple)), "Track values must be a list of keys")
            if len(values) == track.keys and layout in (None, track.layout):
                patches += track.patches(values, self.name); continue
            payload, flags[track.node] = track.reencode(values, layout, flags[track.node])
            a = self.archive; start = a.chunks[track.chunk][4]; end = start + len(track.raw)
            for ri in a.find_chunks(block=a._chunk_block(track.chunk)):
                _, _, _, size, offset = a.chunks[ri]
                _require(ri == track.chunk or size == 0 or offset >= end or start >= offset + size,
                         "Track storage overlaps another payload; editing is locked")
            if payload != track.raw:
                resized.append((track.chunk, payload, "%s: node %d %s re-encoded as %d %s key%s" % (
                    self.name, track.node, track.kind, len(values), track.layout if layout is None else layout,
                    "" if len(values) == 1 else "s")))
        if flags != list(self.flags):
            ri = _one(self.archive, self.chunks, 0x7003)
            patches += nlg_model.diff_patches(ri, 0, struct.pack(">%dI" % len(flags), *self.flags),
                                             struct.pack(">%dI" % len(flags), *flags), "%s: 0x7003 track flags" % self.name)
        return patches, resized


class AnimationSet:
    def __init__(self, archive):
        self.rigs, self.clips, self.issues = [], [], []
        # Isolate unsupported resources so an unrelated asset can still be inspected.
        for ri, row in enumerate(archive.chunks):
            if row[2] not in (0x7000, 0x8000) or not row[0] & 0x80:
                continue
            target, cls = (self.clips, Clip) if row[2] == 0x7000 else (self.rigs, Rig)
            try:
                target.append(cls(archive, ri, len(target)))
            except (AnimationError, struct.error) as ex:
                self.issues.append({"directory": ri, "type": "0x%04X" % row[2], "reason": str(ex)})

    def rig_for(self, clip):
        matches = [rig for rig in self.rigs if (rig.node_count, rig.signature) == (clip.node_count, clip.signature)]
        _require(len(matches) == 1, "Clip needs exactly one rig with the same node count/signature")
        return matches[0]

    def rig_for_bones(self, bone_hashes):
        matches = []
        for rig in self.rigs:
            try:
                parents = rig.bone_parents(bone_hashes)
                matches.append((rig, parents))
            except AnimationError:
                pass
        _require(len(matches) == 1, "Bind bones do not identify exactly one local rig")
        return matches[0]
