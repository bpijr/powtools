"""Lossless particle/effect graph. See EFFECTS.md for PAL DOL and corpus evidence.

0x4000/4002/4004/4020 are *table references*, not payloads. Their size/offset
words select child table records. Serialized pointer-looking bytes are opaque;
the loader overwrites them. Only the proven 4003+56 texture hash is writable.
Unknown or malformed groups are inventoried with an explicit refusal reason.
No bpy, UI, random particle simulation, or inferred world placement here.
"""
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import math
import struct

import nlg_model
import nlg_texture

FAMILY = {0x4000, 0x4001, 0x4002, 0x4003, 0x4004, 0x4005, 0x4006,
          0x4020, 0x4021, 0x4022, 0x4025, 0x4026}
NONE = 0xFFFFFFFF
PARAMETERS = 8


class EffectFormatError(ValueError):
    pass


def u32(raw, offset): return struct.unpack_from(">I", raw, offset)[0]
def digest(raw): return hashlib.sha256(raw).hexdigest()


def names_for(archive):
    from nlg_hash import load_hashid_bin
    for parent in Path(archive.dict_path).resolve().parents:
        path = parent / "hashid.bin"
        if path.is_file():
            try: return load_hashid_bin(path)
            except (OSError, ValueError, IndexError, struct.error): return {}
    return {}


@dataclass(frozen=True)
class Parameter:
    index: int
    chunk: int
    raw: bytes
    curve_chunk: object = None
    curve_raw: bytes = b""

    @property
    def curved(self): return u32(self.raw, 0) != 0

    @property
    def segments(self):
        # +20 is overwritten with an integrated running total by the game loader.
        return tuple(struct.unpack_from(">5f", self.curve_raw, o)
                     for o in range(0, len(self.curve_raw), 24))

    def sample(self, t):
        """Read-only polynomial value, no physical quantity or time unit inferred.

        fn_8016B32C selects the last segment whose start <= t and evaluates
        a*t^3+b*t^2+c*t+d. The input is deliberately bounded to [0, 1].
        Constant-mode variation needs game RNG, so this returns its base only.
        """
        if not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 <= t <= 1:
            raise EffectFormatError("Curve preview input must be finite and between 0 and 1.")
        if not self.curved: return struct.unpack_from(">f", self.raw, 4)[0]
        eligible = [s for s in self.segments if s[0] <= t]
        if not eligible: raise EffectFormatError("Curve preview precedes its first segment.")
        _, a, b, c, d = eligible[-1]
        return ((a * t + b) * t + c) * t + d


@dataclass(frozen=True)
class Emitter:
    index: int
    chunk: int
    raw: bytes
    parameters: tuple

    @property
    def name_hash(self): return u32(self.raw, 0)
    @property
    def texture_hash(self): return u32(self.raw, 56)
    @property
    def atlas_cells(self): return u32(self.raw, 60)
    @property
    def model_hash(self): return u32(self.raw, 84)
    @property
    def colour_samples(self): return tuple(tuple(self.raw[o:o + 4]) for o in range(120, 220, 4))
    @property
    def layout(self): return "model" if self.model_hash != NONE else "sprite"


@dataclass(frozen=True)
class Binding:
    index: int
    chunk: int
    offset: int
    raw: bytes

    @property
    def emitter_index(self): return u32(self.raw, 4)
    @property
    def attachment_hash(self): return u32(self.raw, 12)

    def origin_preview(self, emitter):
        # The four consumers selected by fn_8016E130 add +44/+48/+52 to spawn
        # positions. Mode 2 has a different volume calculation and stays opaque.
        if emitter.raw[52] not in (0, 1, 3, 4): return None
        values = struct.unpack_from(">3f", self.raw, 44)
        return values if all(math.isfinite(x) for x in values) else None


@dataclass(frozen=True)
class BindingSet:
    index: int
    chunk: int
    raw: bytes
    bindings: tuple

    @property
    def name_hash(self): return u32(self.raw, 0)


@dataclass
class Effect:
    index: int
    table: int
    chunk: object = None
    raw: bytes = b""
    emitters: list = field(default_factory=list)
    binding_sets: list = field(default_factory=list)
    records: set = field(default_factory=set)
    reason: object = None

    @property
    def name_hash(self): return u32(self.raw, 4) if len(self.raw) == 24 else None
    @property
    def layout(self):
        if self.reason: return "unsupported"
        return "empty" if not self.emitters else "multiple binding sets" if len(self.binding_sets) > 1 else "standard"


class Effects:
    def __init__(self, archive, names=None):
        self.archive = archive
        self.names = names or {}
        self.groups = []
        self.owners = {}
        self.unowned = []
        self._data = set(archive.find_chunks())
        for ri, c in enumerate(archive.chunks[:archive.num_file_entries]):
            if c[2] != 0x4000: continue
            effect = Effect(len(self.groups), ri)
            self.groups.append(effect)
            try: self._group(effect)
            except EffectFormatError as ex:
                effect.reason = str(ex)
                effect.emitters = []; effect.binding_sets = []
        # Shared records invalidate *both* owners, independent of their table order.
        memberships = {}
        for e in self.groups:
            for ri in e.records: memberships.setdefault(ri, []).append(e)
        for ri, groups in memberships.items():
            if len(groups) > 1:
                for e in groups:
                    e.reason = "Table record is shared by multiple effect groups."
                    e.emitters = []; e.binding_sets = []
            else: self.owners[ri] = groups[0].index
        self.unowned = [ri for ri, c in enumerate(archive.chunks)
                        if c[2] >> 8 == 0x40 and ri not in memberships]
        try:
            self.textures = nlg_texture.list_all_textures(archive, self.names)
            self.texture_reason = None
        except ValueError as ex:
            self.textures = []; self.texture_reason = str(ex)

    def _claim(self, e, ri):
        if ri in e.records: raise EffectFormatError("Effect graph reuses a table record (cycle or alias).")
        e.records.add(ri)

    def _children(self, e, ri, kind):
        self._claim(e, ri)
        if not 0 <= ri < len(self.archive.chunks): raise EffectFormatError("Table reference is out of bounds.")
        flags, version, actual, count, start = self.archive.chunks[ri]
        if actual != kind or flags != 0x92 or version != 2:
            raise EffectFormatError(f"Unsupported 0x{kind:04X} table flags/version.")
        if start < self.archive.num_file_entries or start <= ri or count > len(self.archive.chunks) - start:
            raise EffectFormatError("Table child range is out of bounds or points backwards.")
        return list(range(start, start + count))

    def _payload(self, e, ri, kind, size=None):
        self._claim(e, ri)
        if ri not in self._data: raise EffectFormatError("Expected a payload, found a table reference.")
        flags, version, actual, count, start = self.archive.chunks[ri]
        if actual != kind or flags != 0x12 or version != 2:
            raise EffectFormatError(f"Unsupported 0x{kind:04X} payload flags/version.")
        raw = self.archive.get_chunk_bytes(ri)
        if len(raw) != count or size is not None and len(raw) != size:
            raise EffectFormatError(f"0x{kind:04X} payload has an unsupported byte length.")
        return raw

    def _group(self, e):
        children = self._children(e, e.table, 0x4000)
        if len(children) < 3: raise EffectFormatError("Effect group lacks its header and pointer tables.")
        e.chunk = children[0]
        e.raw = self._payload(e, e.chunk, 0x4001, 24)
        n, m = u32(e.raw, 8), u32(e.raw, 16)
        if len(children) != 3 + n + m or not m:
            raise EffectFormatError("Effect child count does not match emitter and binding-set counts.")
        self._payload(e, children[1], 0x4025, n * 4)
        self._payload(e, children[2], 0x4026, m * 4)
        for index, ri in enumerate(children[3:3 + n]):
            subs = self._children(e, ri, 0x4002)
            if len(subs) != 9: raise EffectFormatError("Emitter must own one header and eight parameters.")
            raw = self._payload(e, subs[0], 0x4003, 220)
            parameters = []
            for pi, parent in enumerate(subs[1:]):
                pcs = self._children(e, parent, 0x4004)
                if len(pcs) not in (1, 2): raise EffectFormatError("Unsupported parameter children.")
                praw = self._payload(e, pcs[0], 0x4005, 20)
                mode = u32(praw, 0)
                if mode not in (0, 1): raise EffectFormatError("Unknown parameter mode; preserved opaque.")
                curve = b""
                if mode:
                    if len(pcs) != 2 or not u32(praw, 12): raise EffectFormatError("Curve mode has no segments.")
                    curve = self._payload(e, pcs[1], 0x4006, u32(praw, 12) * 24)
                    starts = [struct.unpack_from(">f", curve, o)[0] for o in range(0, len(curve), 24)]
                    values = [v for o in range(0, len(curve), 24) for v in struct.unpack_from(">5f", curve, o)]
                    if not all(math.isfinite(v) for v in values) or any(a >= b for a, b in zip(starts, starts[1:])):
                        raise EffectFormatError("Curve segments have nonfinite values or non-increasing starts.")
                elif len(pcs) != 1: raise EffectFormatError("Constant parameter has unexpected curve data.")
                elif not all(math.isfinite(v) for v in struct.unpack_from(">2f", praw, 4)):
                    raise EffectFormatError("Constant parameter has nonfinite values.")
                parameters.append(Parameter(pi, pcs[0], praw, pcs[1] if mode else None, curve))
            e.emitters.append(Emitter(index, subs[0], raw, tuple(parameters)))
        for index, ri in enumerate(children[3 + n:]):
            subs = self._children(e, ri, 0x4020)
            if len(subs) != 2: raise EffectFormatError("Binding set must own two chunks.")
            raw = self._payload(e, subs[0], 0x4021, 16)
            data = self._payload(e, subs[1], 0x4022, 88 * u32(raw, 8))
            bindings = tuple(Binding(i, subs[1], i * 88, data[i * 88:(i + 1) * 88])
                             for i in range(len(data) // 88))
            if any(b.emitter_index >= n for b in bindings):
                raise EffectFormatError("Binding references an emitter outside its owning group.")
            e.binding_sets.append(BindingSet(index, subs[0], raw, bindings))

    def emitter(self, chunk):
        if type(chunk) is not int: raise EffectFormatError("Emitter chunk must be an integer.")
        for group in self.groups:
            if group.reason: continue
            for emitter in group.emitters:
                if emitter.chunk == chunk: return emitter
        raise EffectFormatError("Emitter is absent or its layout/ownership is unsupported.")

    def texture_swap_reason(self, emitter, target=None):
        if emitter.model_hash != NONE: return "Model particles use a separate model resource; texture swaps are disabled."
        if emitter.atlas_cells != 1: return "Animated atlas mapping is inspect-only; only single-cell sprites can swap textures."
        if self.texture_reason: return "Texture table is unsupported: " + self.texture_reason
        old = [t for t in self.textures if t.hash == emitter.texture_hash]
        if len(old) != 1: return "Source texture is external, missing, or ambiguous; local unique resolution is required."
        if target is None: return None
        new = [t for t in self.textures if t.hash == target]
        if len(new) != 1: return "Target texture is external, missing, or ambiguous; choose a unique local texture."
        old, new = old[0], new[0]
        if new.fmt not in (5, 6, 8): return "Target texture has no verified preview codec."
        if (old.width, old.height, old.fmt, old.levels) != (new.width, new.height, new.fmt, new.levels):
            return "Texture dimensions, format, and mip count must match."
        return None

    def texture_candidates(self, chunk):
        emitter = self.emitter(chunk)
        return [t for t in self.textures if self.texture_swap_reason(emitter, t.hash) is None]

    def texture_patches(self, chunk, old_hash, new_hash):
        emitter = self.emitter(chunk)
        if any(type(h) is not int or not 0 <= h <= NONE for h in (old_hash, new_hash)):
            raise EffectFormatError("Texture references must be unsigned 32-bit hashes.")
        if emitter.texture_hash != old_hash: raise EffectFormatError("Texture reference changed since this edit was prepared.")
        reason = self.texture_swap_reason(emitter, new_hash)
        if reason: raise EffectFormatError(reason)
        return nlg_model.diff_patches(chunk, 56, emitter.raw[56:60], struct.pack(">I", new_hash),
                                      f"effect emitter {chunk}: texture {old_hash:08X} -> {new_hash:08X}")

    def inspect(self):
        def name(h): return self.names.get(h) or (f"{h:08X}" if h is not None else "Unknown")
        rows = []
        for group in self.groups:
            rows.append({"chunk": group.chunk if group.chunk is not None else group.table,
                         "table_record": group.table, "name": name(group.name_hash), "layout": group.layout,
                         "summary": f"{len(group.emitters)} emitters; {len(group.binding_sets)} binding sets" if not group.reason else group.reason,
                         "reason": group.reason, "owned_table_records": sorted(group.records),
                         "emitters": [{"chunk": e.chunk, "index": e.index, "name": name(e.name_hash),
                                       "layout": e.layout, "texture_hash": e.texture_hash,
                                       "texture_name": name(e.texture_hash), "atlas_cells": e.atlas_cells,
                                       "model_hash": e.model_hash, "model_name": name(e.model_hash) if e.model_hash != NONE else None,
                                       "texture_swap_reason": self.texture_swap_reason(e),
                                       "colour_samples_rgba": e.colour_samples,
                                       "parameters": [{"chunk": p.chunk, "slot": p.index, "mode": "curve" if p.curved else "constant with variation",
                                                       "curve_chunk": p.curve_chunk, "segments": len(p.segments),
                                                       "semantic": "Quantity and input units not established; inspect-only"} for p in e.parameters],
                                       "opaque_sha256": digest(e.raw)} for e in group.emitters],
                         "binding_sets": [{"chunk": s.chunk, "name": name(s.name_hash),
                                           "bindings": [{"chunk": b.chunk, "offset": b.offset,
                                                         "emitter_index": b.emitter_index,
                                                         "attachment_hash": b.attachment_hash,
                                                         "attachment_name": name(b.attachment_hash),
                                                         "note": "Attachment transform and runtime placement are not reconstructed"} for b in s.bindings]}
                                          for s in group.binding_sets],
                         "animation_note": "Inline parameter curves owned explicitly; no independent animation-clip reference field established.",
                         "edit_note": "Texture reference only; all other payload bytes are opaque or inspect-only. Runtime unverified."})
        for ri in self.unowned:
            rows.append({"chunk": ri, "name": f"Unowned 0x{self.archive.chunks[ri][2]:04X}", "layout": "unsupported",
                         "summary": "No verified group owner; editing refused.", "reason": "No verified group owner."})
        return rows


def inspect(archive): return Effects(archive, names_for(archive)).inspect()


class ResourceIndex:
    """Exact hash matches in explicitly supplied archives, for read-only dependency inspection.

    An external match is not proof that a resource is loaded in a particular scene.
    Never feeds texture swap authorization; that requires unique local resolution.
    """
    def __init__(self):
        self.textures = {}; self.models = {}; self.animations = {}; self.notes = []

    def add(self, archive, section=0):
        source = {"archive": str(archive.dict_path), "section": section}
        try:
            for t in nlg_texture.list_all_textures(archive):
                self.textures.setdefault(t.hash, []).append(dict(source, chunk=t.header_chunk, texture=t.index))
        except ValueError as ex: self.notes.append(str(ex))
        try:
            for s in nlg_model.model_sets(archive):
                for n in s.nodes:
                    self.models.setdefault(n.name_hash, []).append(dict(source, model_set=s.index, node=n.index))
        except nlg_model.ModelFormatError as ex: self.notes.append(str(ex))
        from nlg_hash import string_to_hash
        for ri in archive.find_chunks(type_id=0x7002):
            raw = archive.get_chunk_bytes(ri).split(b"\0")[0]
            if raw and all(32 <= v < 127 for v in raw):
                self.animations.setdefault(string_to_hash(raw.decode("ascii")), []).append(dict(source, chunk=ri))

    def references(self, emitter):
        return {"textures": self.textures.get(emitter.texture_hash, []),
                "models": [] if emitter.model_hash == NONE else self.models.get(emitter.model_hash, []),
                "animations": [],
                "animation_reason": "No separate animation-clip hash field is established; inline curves have explicit owners."}
