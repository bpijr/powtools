"""
nlg_unpack.py — Punch-Out!! Wii (.dict/.data) archive parser.

Format decoded from Switch-Toolbox's PO_DICT.cs / PO_Mesh.cs (FileFormats/NLG/PunchOutWii).
Switch-Toolbox can READ these but its Save() is empty — nobody repacks them. This is our
own clean reimplementation, the base for a future repacker.

.dict layout (BIG-endian):
    u32 magic = 0xA9F32458
    u16 version (0x0601)
    u8  isCompressed
    u8  pad
    u32 (=1)
    u32 (=0)
    u32 numFileEntries        # entries in the file-chunk table (also mirrored in .debug)
    u32 fileTableSize
    8 x BlockInfo { u32 size, u32 param }   # 8 data blocks; offsets accumulate; size 0 = unused

.data layout (BIG-endian):
    [Block0][Block1]...[Block7][ChunkTable]
    ChunkTable starts at sum(block sizes) = fileTableOffset. Two back-to-back lists of 12-byte
    ChunkDataInfo { u8 flags, u8 unk(=1), u16 type(SectionMagic), u32 size, u32 offset }:
      - first numFileEntries records (block index from flags: 0x12->0, 0x25->1, 0x02->2, 0x42->3, 0x03->0)
      - then records to EOF (block index = flags>>4 if <7)
    Each chunk's absolute position = Blocks[blockIndex].offset + chunk.offset.

SectionMagic (u16):
    TextureHeaders 0xB601 (96 B/tex)  TextureData 0xB603   MaterialData 0xB016
    IndexData 0xB007  VertexData 0xB006  VertexAttributePointerData 0xB005 (8 B/attr)
    MeshData 0xB004 (52 B/mesh)  ModelData 0xB003 (12 B: hash,numMeshes,0)
    MatrixData 0xB002  SkeletonData 0xB008  BoneHashes 0xB00B  BoneData 0xB00A (68 B/bone)
    UnknownHashList 0xB00C
"""
import struct
import sys
import os

SECTION_NAMES = {
    0xB601: "TextureHeaders", 0xB603: "TextureData", 0xB016: "MaterialData",
    0xB007: "IndexData", 0xB006: "VertexData", 0xB005: "VertexAttributePointerData",
    0xB004: "MeshData", 0xB003: "ModelData", 0xB002: "MatrixData",
    0xB008: "SkeletonData", 0xB00B: "BoneHashes", 0xB00A: "BoneData",
    0xB00C: "UnknownHashList",
}


def _u32(b, o): return struct.unpack_from(">I", b, o)[0]
def _u16(b, o): return struct.unpack_from(">H", b, o)[0]
def _f32(b, o): return struct.unpack_from(">f", b, o)[0]


class Block:
    __slots__ = ("index", "offset", "size", "param")


class Chunk:
    __slots__ = ("flags", "unk", "type", "size", "offset", "block_index", "abs_pos", "is_file_entry")


class PODict:
    def __init__(self, dict_path):
        self.dict_path = dict_path
        self.data_path = dict_path[:-5] + ".data" if dict_path.endswith(".dict") else dict_path + ".data"
        self.blocks = []
        self.chunks = []
        self._read()

    def _read(self):
        d = open(self.dict_path, "rb").read()
        self.magic = _u32(d, 0)
        assert self.magic == 0xA9F32458, f"bad magic {self.magic:08X}"
        self.version = _u16(d, 4)
        self.is_compressed = d[6] == 1
        self.const1 = _u32(d, 8)     # =1
        self.const0 = _u32(d, 12)    # =0
        self.num_file_entries = _u32(d, 16)
        self.file_table_size = _u32(d, 20)

        offset = 0
        for i in range(8):
            b = Block()
            b.index = i
            b.offset = offset
            b.size = _u32(d, 24 + i * 8)
            b.param = _u32(d, 24 + i * 8 + 4)
            self.blocks.append(b)
            offset += b.size
        self.file_table_offset = offset

        self._parse_chunks()

    def _parse_chunks(self):
        data = open(self.data_path, "rb").read()
        self.data_len = len(data)
        pos = self.file_table_offset

        def read_chunk(is_file_entry):
            nonlocal pos
            c = Chunk()
            c.flags = data[pos]
            c.unk = data[pos + 1]
            c.type = _u16(data, pos + 2)
            c.size = _u32(data, pos + 4)
            c.offset = _u32(data, pos + 8)
            c.is_file_entry = is_file_entry
            c.block_index = -1
            if is_file_entry:
                fmap = {0x12: 0, 0x25: 1, 0x02: 2, 0x42: 3, 0x03: 0}
                c.block_index = fmap.get(c.flags, -1)
            else:
                bf = c.flags >> 4
                if bf < 7:
                    c.block_index = bf
            if c.block_index >= 0:
                c.abs_pos = self.blocks[c.block_index].offset + c.offset
            else:
                c.abs_pos = None
            pos += 12
            return c

        for _ in range(self.num_file_entries):
            self.chunks.append(read_chunk(True))
        while pos <= len(data) - 12:
            self.chunks.append(read_chunk(False))

    # -- accessors --
    def chunk_data(self, c):
        data = open(self.data_path, "rb").read()
        return data[c.abs_pos:c.abs_pos + c.size]


def summarize(dict_path, hashnames=None):
    po = PODict(dict_path)
    print(f"== {os.path.basename(dict_path)} ==")
    print(f"magic={po.magic:08X} version={po.version:04X} compressed={po.is_compressed}")
    print(f"numFileEntries={po.num_file_entries} fileTableSize={po.file_table_size:#x} "
          f"fileTableOffset={po.file_table_offset:#x} dataLen={po.data_len:#x}")
    print("blocks:")
    for b in po.blocks:
        if b.size:
            print(f"  block{b.index}: off={b.offset:#010x} size={b.size:#x} param={b.param}")
    # count chunk types
    from collections import Counter
    cnt = Counter(SECTION_NAMES.get(c.type, f"{c.type:04X}") for c in po.chunks if not c.is_file_entry)
    print("data chunk types:")
    for k, v in cnt.most_common():
        print(f"  {v:4d}  {k}")

    # models / meshes / bones
    for c in po.chunks:
        if c.is_file_entry or c.abs_pos is None:
            continue
        if c.type == 0xB003:  # ModelData
            body = po.chunk_data(c)
            n = c.size // 12
            print(f"\nModelData: {n} model(s)")
            for i in range(n):
                h = _u32(body, i * 12); nm = _u32(body, i * 12 + 4)
                name = hashnames.get(h, f"{h:08X}") if hashnames else f"{h:08X}"
                print(f"  model[{i}] hash={h:08X} '{name}' numMeshes={nm}")
        if c.type == 0xB00A:  # BoneData
            n = c.size // 68
            body = po.chunk_data(c)
            print(f"\nBoneData: {n} bone(s)")
            for i in range(min(n, 8)):
                h = _u32(body, i * 68)
                name = hashnames.get(h, f"{h:08X}") if hashnames else f"{h:08X}"
                px = _f32(body, i * 68 + 52); py = _f32(body, i * 68 + 56); pz = _f32(body, i * 68 + 60)
                print(f"  bone[{i}] hash={h:08X} '{name}' pos=({px:.3f},{py:.3f},{pz:.3f})")
            if n > 8:
                print(f"  ... (+{n-8} more)")
        if c.type == 0xB004:  # MeshData
            n = c.size // 52
            body = po.chunk_data(c)
            print(f"\nMeshData: {n} mesh(es)")
            for i in range(n):
                o = i * 52
                idx_start = _u32(body, o)
                idx_flags = _u32(body, o + 4)
                idx_count = idx_flags & 0xFFFFFF
                idx_fmt = idx_flags >> 24
                vcount = _u16(body, o + 8)
                nattr = body[o + 11]
                mat_hash = _u32(body, o + 16)
                mesh_hash = _u32(body, o + 20)
                mname = hashnames.get(mesh_hash, f"{mesh_hash:08X}") if hashnames else f"{mesh_hash:08X}"
                matname = hashnames.get(mat_hash, f"{mat_hash:08X}") if hashnames else f"{mat_hash:08X}"
                print(f"  mesh[{i}] '{mname}' verts={vcount} idxCount={idx_count} "
                      f"idxFmt={idx_fmt} attrs={nattr} mat='{matname}'")
    return po


if __name__ == "__main__":
    hn = None
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(here), "formats"))
        from nlg_hash import load_hashid_bin
        hb = os.path.join(here, "..", "..", "art", "hashid.bin")
        if os.path.exists(hb):
            hn = load_hashid_bin(hb)
    except Exception as e:
        print("(hashnames unavailable:", e, ")")
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "..", "art", "characters", "glassjoe.dict")
    summarize(target, hn)
