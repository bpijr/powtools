"""
nlg_pack.py — Punch-Out!! Wii (.dict/.data) archive REPACKER.

Model the archive fully in memory and re-serialize it.

Milestone 1 (this file): byte-identical round-trip. load(orig) -> build() must reproduce the
exact original .dict and .data bytes. That proves our model captures 100% of the container and
that the packer is trustworthy before we start changing anything.

Design:
  Archive holds: dict header fields, 8 blocks (raw payload bytes), and the chunk table as a list
  of records [flags, unk, type, size, offset]. .data = concat(blocks) + serialized chunk table.
  .dict = header + 8x(blockSize, blockParam).

Block 'param' values observed: 4, 8, 32, 32 — likely alignment requirements of each block. Noted
for the resize path (a grown block must keep the following blocks aligned); irrelevant for a pure
round-trip.
"""
import struct
import os
import sys
from pathlib import Path


def _u32(b, o): return struct.unpack_from(">I", b, o)[0]
def _u16(b, o): return struct.unpack_from(">H", b, o)[0]


class Archive:
    HEADER_SIZE = 24
    N_BLOCKS = 8

    def __init__(self, dict_path, dict_bytes=None, data_bytes=None):
        # dict_bytes/data_bytes parse an in-memory section (e.g. one part of a multi-section NIS
        # container) instead of reading the files; dict_path is then only a label.
        self.dict_path = dict_path
        self.data_path = (dict_path[:-5] + ".data") if dict_path.endswith(".dict") else dict_path + ".data"
        self._load(dict_bytes, data_bytes)

    def _load(self, dict_bytes=None, data_bytes=None):
        dd = Path(self.dict_path).read_bytes() if dict_bytes is None else bytes(dict_bytes)
        self.orig_dict = dd
        self.magic = _u32(dd, 0)
        assert self.magic == 0xA9F32458, f"bad magic {self.magic:08X}"
        self.version = _u16(dd, 4)
        self.is_compressed = dd[6]
        self.pad = dd[7]
        self.const1 = _u32(dd, 8)
        self.const0 = _u32(dd, 12)
        self.num_file_entries = _u32(dd, 16)
        self.file_table_size = _u32(dd, 20)

        block_sizes, self.block_params = [], []
        for i in range(self.N_BLOCKS):
            block_sizes.append(_u32(dd, 24 + i * 8))
            self.block_params.append(_u32(dd, 24 + i * 8 + 4))

        da = Path(self.data_path).read_bytes() if data_bytes is None else bytes(data_bytes)
        self.orig_data = da

        self.blocks = []
        off = 0
        for sz in block_sizes:
            self.blocks.append(bytearray(da[off:off + sz]))
            off += sz
        self.file_table_offset = off

        # chunk table = fixed 12-byte records to EOF
        self.chunks = []
        pos = off
        while pos <= len(da) - 12:
            self.chunks.append([da[pos], da[pos + 1], _u16(da, pos + 2),
                                _u32(da, pos + 4), _u32(da, pos + 8)])
            pos += 12
        self.table_trailing = da[pos:]  # should be empty (table is a clean multiple of 12)

    # ---- serialization ----
    def build_chunk_table(self):
        out = bytearray()
        for flags, unk, typ, size, offset in self.chunks:
            out += bytes((flags, unk))
            out += struct.pack(">H", typ)
            out += struct.pack(">I", size)
            out += struct.pack(">I", offset)
        out += self.table_trailing
        return bytes(out)

    def build_data(self):
        out = bytearray()
        for b in self.blocks:
            out += b
        out += self.build_chunk_table()
        return bytes(out)

    def build_dict(self):
        table = self.build_chunk_table()
        out = bytearray()
        out += struct.pack(">I", self.magic)
        out += struct.pack(">H", self.version)
        out += bytes((self.is_compressed, self.pad))
        out += struct.pack(">I", self.const1)
        out += struct.pack(">I", self.const0)
        out += struct.pack(">I", self.num_file_entries)
        out += struct.pack(">I", len(table))  # fileTableSize (recomputed)
        for i in range(self.N_BLOCKS):
            out += struct.pack(">I", len(self.blocks[i]))
            out += struct.pack(">I", self.block_params[i])
        return bytes(out)

    def write(self, dict_path, data_path=None):
        data_path = data_path or ((dict_path[:-5] + ".data") if dict_path.endswith(".dict") else dict_path + ".data")
        Path(dict_path).write_bytes(self.build_dict())
        Path(data_path).write_bytes(self.build_data())

    # ---- resize / replace API ----
    BLOCK_PAD = 0x800   # blocks pad up to 2 KB
    SHIFT_ALIGN = 32    # downstream shifts kept to multiples of 32 to preserve all alignments

    def _chunk_block(self, ri):
        flags = self.chunks[ri][0]
        bf = flags >> 4
        return bf if bf < 7 else -1

    def data_chunks_in_block(self, bi):
        """Record indices of block bi's DATA chunks (second list only), sorted by offset."""
        items = []
        for i in range(self.num_file_entries, len(self.chunks)):
            if self._chunk_block(i) == bi:
                items.append((i, self.chunks[i][4], self.chunks[i][3]))  # (record_index, offset, size)
        items.sort(key=lambda t: t[1])
        return items

    def get_chunk_bytes(self, ri):
        bi = self._chunk_block(ri)
        _, _, _, size, off = self.chunks[ri]
        return bytes(self.blocks[bi][off:off + size])

    def find_chunks(self, type_id=None, block=None):
        """Return record indices of data chunks matching a SectionMagic type and/or block."""
        out = []
        for i in range(self.num_file_entries, len(self.chunks)):
            bi = self._chunk_block(i)
            if bi < 0:
                continue
            if block is not None and bi != block:
                continue
            if type_id is not None and self.chunks[i][2] != type_id:
                continue
            out.append(i)
        return out

    def replace_chunk(self, ri, new_bytes):
        """Replace one data chunk's payload with new_bytes (any size). Rebuilds its block,
        recomputes every chunk offset in that block, repads to 0x800, updates the dict block
        size. Downstream chunks shift by a multiple of 32, preserving all alignments. The
        table-of-contents (file-entry) records are untouched."""
        bi = self._chunk_block(ri)
        if bi < 0:
            raise ValueError(f"chunk {ri} is not a block-owning data chunk")
        layout = self.data_chunks_in_block(bi)
        blk = self.blocks[bi]
        newblock = bytearray()
        for idx, (rec, off, size) in enumerate(layout):
            is_last = idx == len(layout) - 1
            orig_gap = 0 if is_last else (layout[idx + 1][1] - (off + size))
            if rec == ri:
                payload = bytes(new_bytes)
                region_old = size + orig_gap
                newlen = len(payload)
                # keep new region ≡ region_old (mod 32) so downstream shift is a multiple of 32
                gap = (region_old - newlen) % self.SHIFT_ALIGN
            else:
                payload = bytes(blk[off:off + size])
                gap = orig_gap
            self.chunks[rec][4] = len(newblock)     # new offset within block
            self.chunks[rec][3] = len(payload)       # new size
            newblock += payload + bytes(gap)
        while len(newblock) % self.BLOCK_PAD:
            newblock += b"\x00"
        self.blocks[bi] = newblock


def roundtrip_check(dict_path):
    a = Archive(dict_path)
    new_dict = a.build_dict()
    new_data = a.build_data()
    dict_ok = new_dict == a.orig_dict
    data_ok = new_data == a.orig_data
    name = os.path.basename(dict_path)
    status = "OK " if (dict_ok and data_ok) else "FAIL"
    detail = ""
    if not dict_ok:
        detail += f" dict差{_firstdiff(new_dict, a.orig_dict)}"
    if not data_ok:
        detail += f" data diff@{_firstdiff(new_data, a.orig_data)} (len {len(new_data)} vs {len(a.orig_data)})"
    print(f"[{status}] {name:28s} blocks={sum(1 for b in a.blocks if b)} "
          f"chunks={len(a.chunks)} fileEntries={a.num_file_entries}{detail}")
    return dict_ok and data_ok


def _firstdiff(x, y):
    n = min(len(x), len(y))
    for i in range(n):
        if x[i] != y[i]:
            return hex(i)
    return hex(n) if len(x) != len(y) else "none"


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    args = sys.argv[1:]
    if args:
        targets = args
    else:
        chdir = os.path.join(here, "..", "..", "art", "characters")
        targets = [os.path.join(chdir, f) for f in sorted(os.listdir(chdir)) if f.endswith(".dict")]
    ok = fail = 0
    for t in targets:
        try:
            if roundtrip_check(t):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            print(f"[ERR ] {os.path.basename(t)}: {e}")
            fail += 1
    print(f"\nround-trip: {ok} OK, {fail} FAIL out of {ok+fail}")
