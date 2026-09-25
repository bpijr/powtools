"""Section container writer and audited chunk-resize rebuilds (no UI or Blender code).

.dict: 12-byte header {magic, u16 version, u8 compressed, u8 pad, u32 sections}, then one
76-byte record per section {u32 dataOffset, u32 entries, u32 tableBytes, 8 x (u32 blockSize,
u32 blockParam)}. Ordinary archives are the one-section case (88 bytes, offset 0). A section
body is its eight blocks then the 12-byte chunk table; sections start on 0x800 boundaries
and the gaps between them are zero.

PAL R7PP01 loader (DOL SHA-256 6ae3388f...): fn_801274F0 indexes records by section * 0x4C
(0x8012753C), keeps +4 as the entry count (0x80127548) and passes +0 to the stream seek
(0x80127564). fn_801277C4 walks the block sizes at +0xC (0x8012780C, empty blocks skipped)
with +0x10 as allocation alignment (minimum 0x20, 0x80127868), then reads +8 table bytes
(0x801279A0). Chunk offsets are block-relative and directory records store record indices,
so moving a section only changes its dataOffset; resizing a chunk only changes its block.

A resize replaces a whole chunk payload through nlg_pack.Archive.replace_chunk (32-byte
downstream shifts, 0x800 block padding). It is refused where a no-op replace_chunk would not
reproduce the block (leading, overlapping or nonzero gap bytes) or where a non-directory
table-of-contents record addresses bytes in it: those byte ranges have no proven update rule.
"""
import hashlib
import struct
from pathlib import Path

from nlg_pack import Archive
import nlg_model

MAGIC = 0xA9F32458
RECORD = 76
ALIGN = 0x800
MAX_CHUNK = 64 << 20


class ContainerError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _require(condition, message):
    if not condition:
        raise ContainerError(message)


def parse(dd, da, label="container.dict"):
    """Strict ([{offset, length}], [Archive]) from bytes; mirrors po_archive.load_sections."""
    _require(len(dd) >= 12 and struct.unpack_from(">I", dd, 0)[0] == MAGIC, "Not a Punch-Out archive dictionary.")
    _require(not dd[6], "Compressed archive dictionaries are not supported.")
    count = struct.unpack_from(">I", dd, 8)[0]
    _require(count and len(dd) == 12 + RECORD * count, "Archive dictionary has an unsupported layout.")
    records, archives, end = [], [], 0
    for s in range(count):
        o = 12 + RECORD * s
        offset, entries, table = struct.unpack_from(">III", dd, o)
        length = table + sum(struct.unpack_from(">I", dd, o + 12 + 8 * i)[0] for i in range(8))
        _require(not table % 12 and entries <= table // 12, "Section table size or entry count is inconsistent.")
        _require(end <= offset and offset + length <= len(da) and not any(da[end:offset]),
                 "Archive sections overlap, hide data or extend past the payload.")
        single = bytes(dd[:8]) + struct.pack(">II", 1, 0) + bytes(dd[o + 4:o + RECORD])
        body = bytes(da[offset:offset + length])
        a = Archive(label, single, body)
        _require(a.build_dict() == single and a.build_data() == body, f"Section {s} does not round-trip exactly.")
        for i in range(a.num_file_entries, len(a.chunks)):
            b = a._chunk_block(i)
            _require(b < 0 or a.chunks[i][4] + a.chunks[i][3] <= len(a.blocks[b]), f"Section {s} chunk {i} leaves its block.")
        records.append({"offset": offset, "length": length}); archives.append(a); end = offset + length
    _require(end == len(da), "Archive payload has data after its last section.")
    return records, archives


def write(header, archives, align=ALIGN):
    """(dict, data, offsets): sections laid out in order on `align` boundaries, zero gaps."""
    _require(len(header) == 12 and struct.unpack_from(">I", header, 8)[0] == len(archives),
             "Header section count differs from the sections written.")
    dd, da, offsets = bytearray(header), bytearray(), []
    for i, a in enumerate(archives):
        if i: da += bytes(-len(da) % align)
        offsets.append(len(da))
        dd += struct.pack(">I", len(da)) + a.build_dict()[16:]
        da += a.build_data()
    return bytes(dd), bytes(da), offsets


def rebuild_exact(dd, da):
    """True when the unedited container reproduces itself through parse + write."""
    _, archives = parse(dd, da)
    return write(dd[:12], archives)[:2] == (bytes(dd), bytes(da))


def _copy(a):
    return Archive(a.dict_path, a.build_dict(), a.build_data())


def block_limitation(a, block):
    """Why replace_chunk cannot safely re-lay `block` of section archive `a`, or None."""
    if any(a.chunks[i][0] >> 4 < 8 and a._chunk_block(i) == block for i in range(a.num_file_entries)):
        return "A table-of-contents byte range addresses this block; its update rule is unproven."
    layout = a.data_chunks_in_block(block)
    if not layout:
        return "The block holds no chunks."
    trial = _copy(a)
    trial.replace_chunk(layout[0][0], trial.get_chunk_bytes(layout[0][0]))
    if trial.blocks[block] != a.blocks[block] or trial.chunks != a.chunks:
        return "The block has leading, overlapping or nonzero gap bytes; nlg_pack relayout would not reproduce it."
    return None


def _overlaps(a, ri):
    block = a._chunk_block(ri); off, size = a.chunks[ri][4], a.chunks[ri][3]
    return any(other != ri and a.chunks[other][3] and size and a.chunks[other][4] < off + size and off < a.chunks[other][4] + a.chunks[other][3]
               for other in a.find_chunks(block=block))


class Resize:
    """Whole-payload replacement of one data chunk (any length) in one section."""
    __slots__ = ("section", "chunk", "old_sha256", "new", "label")

    def __init__(self, section, chunk, old_sha256, new, label):
        self.section, self.chunk, self.old_sha256, self.new, self.label = section, chunk, old_sha256, bytes(new), label

    def as_dict(self):
        return {"section": self.section, "chunk": self.chunk, "old_sha256": self.old_sha256,
                "bytes": len(self.new), "new_sha256": sha256(self.new), "label": self.label}

    @classmethod
    def from_dict(cls, value):
        new = bytes.fromhex(value["new"])
        if sha256(new) != value.get("new_sha256", sha256(new)):
            raise ContainerError("Resize payload does not match its recorded hash.")
        return cls(value["section"], value["chunk"], value["old_sha256"], new, value["label"])


def resize(a, section, chunk, new, label):
    """Resize entry for chunk `chunk` of section archive `a` (records the current payload hash)."""
    return Resize(section, chunk, sha256(a.get_chunk_bytes(chunk)), new, label)


def rebuild(path, patch_set):
    """(dict, data, audit) for a source with fixed-size patches and chunk resizes applied."""
    path = Path(path); dd = path.read_bytes(); da = path.with_suffix(".data").read_bytes()
    _require([sha256(dd), sha256(da)] == list(patch_set.source_hashes), "Patch set was made from a different source archive.")
    return rebuild_bytes(dd, da, patch_set.patches, getattr(patch_set, "resizes", ()), str(path))


def rebuild_bytes(dd, da, patches, resizes, label="container.dict"):
    records, archives = parse(dd, da, label)
    chunk_sets = [set(a.find_chunks()) for a in archives]
    resized, allowed = {}, {}
    for r in resizes:
        _require(isinstance(r, Resize), "Unsupported resize entry.")
        s = r.section
        _require(type(s) is int and 0 <= s < len(archives), "Resize section is outside the source.")
        a = archives[s]
        _require(type(r.chunk) is int and r.chunk in chunk_sets[s], "A resize must address a data chunk.")
        _require((s, r.chunk) not in resized, "A chunk is resized twice.")
        _require(len(r.new) <= MAX_CHUNK, "Resized payload exceeds the supported size.")
        _require(sha256(a.get_chunk_bytes(r.chunk)) == r.old_sha256, "Resized chunk differs from the recorded source payload.")
        _require(not _overlaps(a, r.chunk), "Resized chunk has overlapping/aliased storage.")
        reason = block_limitation(a, a._chunk_block(r.chunk))
        _require(reason is None, reason)
        resized[(s, r.chunk)] = r.new
    by_section = {}
    for p in patches:
        s = getattr(p, "section", 0)
        _require(type(s) is int and 0 <= s < len(archives), "Patch section is outside the source.")
        a = archives[s]
        _require(type(p.chunk) is int and p.chunk in chunk_sets[s], "Patch must address a data chunk.")
        _require((s, p.chunk) not in resized, "A fixed-size patch addresses a resized chunk.")
        _require(type(p.offset) is int and p.offset >= 0 and len(p.old) == len(p.new) and p.offset + len(p.new) <= a.chunks[p.chunk][3],
                 "Only fixed-size patches inside an existing chunk are supported.")
        _require(not _overlaps(a, p.chunk), "Patched chunk has overlapping/aliased storage.")
        span = (p.offset, p.offset + len(p.new)); spans = allowed.setdefault((s, p.chunk), [])
        _require(not any(span[0] < hi and lo < span[1] for lo, hi in spans), "Patch ranges overlap.")
        spans.append(span); by_section.setdefault(s, []).append(p)
    work = [_copy(a) for a in archives]
    for s, items in sorted(by_section.items()):
        nlg_model.apply_patches(work[s], items)
    for (s, ri), new in sorted(resized.items()):
        work[s].replace_chunk(ri, new)
    new_dd, new_da, _ = write(dd[:12], work)
    audit = audit_rebuild(dd, da, new_dd, new_da, resized, allowed, label)
    audit.update(patches=len(patches), resizes=len(resizes))
    return new_dd, new_da, audit


def _covered(ranges, spans):
    merged = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]: merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else: merged.append((lo, hi))
    return all(any(lo <= a and b <= hi for lo, hi in merged) for a, b in ranges)


def _gaps(length, spans):
    out, at = [], 0
    for lo, hi in spans:
        if lo > at: out.append((at, lo))
        at = max(at, hi)
    if at < length: out.append((at, length))
    return out


def audit_rebuild(dd, da, new_dd, new_da, resized, allowed, label="container.dict"):
    """Content-identity audit of a relocated container. Offsets move, so every untouched chunk,
    record and section is compared by content, never by absolute position."""
    old_records, old = parse(dd, da, label)
    new_records, new = parse(new_dd, new_da, label)
    _require(new_dd[:12] == dd[:12] and len(new) == len(old), "Container header or section count changed.")
    end = 0
    for rec in new_records:
        _require(rec["offset"] == end + (-end % ALIGN if end else 0), "Sections are not packed on 0x800 boundaries.")
        end = rec["offset"] + rec["length"]
    sections, changes, changed_bytes = [], [], 0
    for s, (a, b) in enumerate(zip(old, new)):
        _require(b.num_file_entries == a.num_file_entries and len(b.chunks) == len(a.chunks) and b.block_params == a.block_params
                 and (b.version, b.is_compressed, b.pad) == (a.version, a.is_compressed, a.pad), f"Section {s} header or record count changed.")
        state, moved, relaid = "untouched", 0, set()
        for i, (x, y) in enumerate(zip(a.chunks, b.chunks)):
            _require(x[:3] == y[:3], f"Section {s} record {i} identity changed.")
            if i < a.num_file_entries or a._chunk_block(i) < 0:
                _require(x == y, f"Section {s} directory or table-of-contents record {i} changed.")
                continue
            before, after = a.get_chunk_bytes(i), b.get_chunk_bytes(i)
            moved += x[4] != y[4]
            if (s, i) in resized:
                _require(after == resized[(s, i)], f"Section {s} chunk {i} does not hold its replacement payload.")
                relaid.add(a._chunk_block(i)); state = "resized"
                if before != after:
                    changes.append({"section": s, "chunk": i, "type": "0x%04X" % x[2], "old_bytes": len(before), "new_bytes": len(after),
                                    "old_sha256": sha256(before), "new_sha256": sha256(after), "resized": len(before) != len(after), "ranges": []})
                    changed_bytes += max(len(before), len(after))
                continue
            _require(len(before) == len(after), f"Section {s} chunk {i} changed length without a resize entry.")
            ranges = nlg_model.changed_ranges(before, after)
            _require(_covered(ranges, allowed.get((s, i), [])), f"Section {s} chunk {i}: bytes outside recorded patch ranges changed.")
            if ranges:
                if state == "untouched": state = "patched"
                changes.append({"section": s, "chunk": i, "type": "0x%04X" % x[2], "old_bytes": len(before), "new_bytes": len(after),
                                "old_sha256": sha256(before), "new_sha256": sha256(after), "resized": False, "ranges": ranges})
                changed_bytes += sum(hi - lo for lo, hi in ranges)
        for bi in range(8):
            spans = sorted((b.chunks[i][4], b.chunks[i][4] + b.chunks[i][3]) for i in b.find_chunks(block=bi) if b.chunks[i][3])
            gaps = _gaps(len(b.blocks[bi]), spans)
            if bi in relaid:
                _require(all(p[1] <= q[0] for p, q in zip(spans, spans[1:])), f"Section {s} block {bi} chunks overlap after relayout.")
                _require(all(bytes(b.blocks[bi][lo:hi]) == bytes(hi - lo) for lo, hi in gaps), f"Section {s} block {bi} padding is not zero.")
                _require(not len(b.blocks[bi]) % Archive.BLOCK_PAD, f"Section {s} block {bi} is not 0x800 padded.")
            else:
                _require(len(b.blocks[bi]) == len(a.blocks[bi]) and all(a.chunks[i] == b.chunks[i] for i in a.find_chunks(block=bi)),
                         f"Section {s} block {bi} changed layout without a resize.")
                _require(all(b.blocks[bi][lo:hi] == a.blocks[bi][lo:hi] for lo, hi in gaps), f"Section {s} block {bi} bytes outside chunks changed.")
        if state == "untouched":
            _require(b.build_data() == a.build_data() and b.build_dict() == a.build_dict(), f"Untouched section {s} changed.")
        sections.append({"section": s, "state": state, "old_offset": old_records[s]["offset"], "new_offset": new_records[s]["offset"],
                         "old_bytes": old_records[s]["length"], "new_bytes": new_records[s]["length"], "moved_chunks": moved,
                         "sha256": [sha256(a.build_data()), sha256(b.build_data())]})
    return {"audit_mode": "section content identity with relocation", "sections": sections, "changed_chunks": changes,
            "changed_ranges": [], "changed_bytes": changed_bytes,
            "untouched_sections_identical": sum(x["state"] == "untouched" for x in sections),
            "sections_changed": [x["section"] for x in sections if x["state"] != "untouched"],
            "output_sha256": [sha256(new_dd), sha256(new_da)]}


def apply_to_archive(a, patches, resizes):
    """In-place variant for one single-section Archive: same checks and audit."""
    dd, da = a.build_dict(), a.build_data()
    _require(struct.unpack_from(">II", dd, 8) == (1, 0), "In-place resizing needs a single-section archive.")
    new_dd, new_da, audit = rebuild_bytes(dd, da, patches, resizes, a.dict_path)
    a._load(new_dd, new_da)
    return audit
