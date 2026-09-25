"""
nlg_texture.py — Punch-Out!! Wii texture codec (GameCube/Wii GX formats).

Textures live as two chunks: TextureHeaders (0xB601, 96 bytes each) in block1, and TextureData
(0xB603) pixel bytes in block2. Header (big-endian):
    u32 hashID, u16 width, u16 height, u16 0, u8,u8,u8, u8 format, u16,u16,u16,
    u32 dataOffset (into the TextureData chunk), u32 (0x00412BA1), ... pad to 96.
format byte: 8=RGBA32 7=RGB565 6=CMPR 5=RGB5A3 4=IA4 3=I8 2=I4.
Character skins are CMPR. The dump also uses RGB5A3 (menu/HUD bundles, effects, rings) and
RGBA32 (the 128x4 toon ramps of a few fighters, ring and global lookup maps); no other format
byte occurs in the retail archives.

This module implements CMPR, RGB5A3 and RGBA32 decode + encode, plus PNG import/export via Pillow.
CMPR = GameCube S3TC/DXT1: image in 8x8 super-tiles, each = four 4x4 DXT1 sub-blocks (row-major).
Each 4x4 block: 2x RGB565 endpoints (BIG-endian) + 4 bytes of 2-bit indices (MSB-first per row).
RGB5A3 = 4x4 tiles of big-endian u16: top bit set -> opaque RGB555, clear -> A3 RGB444.
RGBA32 = 4x4 tiles of 64 bytes: sixteen A,R pairs then sixteen G,B pairs.
"""
import struct

# Pillow is only needed for PNG file I/O. Blender ships without it, and the addon has to be able
# to WRITE textures back, so every codec path below works on raw RGBA bytes and PIL is imported
# lazily -- import nlg_texture inside Blender and only the *_png helpers are unavailable.
def _pil():
    from PIL import Image
    return Image


FORMATS = {0x8: "RGBA32", 0x7: "RGB565", 0x6: "CMPR", 0x5: "RGB5A3", 0x4: "IA4", 0x3: "I8", 0x2: "I4",
           0x9: "IA8", 0xA: "CI8"}
# The authoritative format is the u32 runtime enum at header +16. main.dol (801BBE9C) maps it to
# per-channel bit depths when header bytes +12..+15 are 0xFF: 0 RGB565, 1 RGB5A3, 3 RGBA8, 4 I4,
# 5 I8, 7 IA8; 2 is CMPR and 8 is C8 (256-entry palette appended after the pixels). Bytes
# +12..+15 are the R,G,B,A bit depths (the alpha depth selects opaque / alpha-test / blend in
# 8014F24C), so byte +13 is only the GREEN depth and is not a format code.
ENUM_TO_FMT = {0: 0x7, 1: 0x5, 2: 0x6, 3: 0x8, 4: 0x2, 5: 0x3, 7: 0x9, 8: 0xA}


# ---------- RGB565 helpers ----------
def _rgb565_to_rgb(c):
    r = (c >> 11) & 0x1F
    g = (c >> 5) & 0x3F
    b = c & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def _rgb_to_565(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


# ---------- CMPR decode ----------
def decode_cmpr(data, width, height):
    """Return an RGBA bytearray (width*height*4) from GameCube CMPR data."""
    out = bytearray(width * height * 4)
    off = 0
    bw = (width + 7) // 8
    bh = (height + 7) // 8
    for by in range(bh):
        for bx in range(bw):
            for sub in range(4):           # four 4x4 sub-blocks: (0,0)(1,0)(0,1)(1,1)
                sx = (sub & 1) * 4
                sy = (sub >> 1) * 4
                c0 = struct.unpack_from(">H", data, off)[0]
                c1 = struct.unpack_from(">H", data, off + 2)[0]
                idxbytes = data[off + 4:off + 8]
                off += 8
                r0, g0, b0 = _rgb565_to_rgb(c0)
                r1, g1, b1 = _rgb565_to_rgb(c1)
                pal = [(r0, g0, b0, 255), (r1, g1, b1, 255)]
                if c0 > c1:
                    # GX interpolates 5/8 : 3/8, not DXT1's 2/3 : 1/3 (Dolphin DXTBlend).
                    pal.append(((5*r0+3*r1) >> 3, (5*g0+3*g1) >> 3, (5*b0+3*b1) >> 3, 255))
                    pal.append(((3*r0+5*r1) >> 3, (3*g0+5*g1) >> 3, (3*b0+5*b1) >> 3, 255))
                else:
                    # Index 3 is the SAME average as index 2 with alpha 0 (not transparent black).
                    avg = ((r0+r1)//2, (g0+g1)//2, (b0+b1)//2)
                    pal.append(avg + (255,))
                    pal.append(avg + (0,))
                for py in range(4):
                    row = idxbytes[py]
                    for px in range(4):
                        idx = (row >> (6 - px*2)) & 3
                        X = bx*8 + sx + px
                        Y = by*8 + sy + py
                        if X < width and Y < height:
                            p = (Y*width + X) * 4
                            out[p:p+4] = bytes(pal[idx])
    return out


def decode_cmpr_cpu(data, width, height):
    """The game's own CPU CMPR decoder (main.dol 800C2EE0 / 800C311C).

    Used where the engine samples a texture on the CPU (the crowd light map read by
    800C2B4C).  It differs from the GX hardware decoder above: channels expand as
    v*255//31 and v*255//63, the blend is DXT1's 2/3 : 1/3, index 3 of a three-colour
    block is transparent black, and indices are read in PC DXT1 order (byte ``y ^ 1`` of
    the index word, pixel x at bits 2x) which mirrors every 4x4 block and swaps its row
    pairs.  Verified byte-exact against the decoded copy in a Dolphin savestate.
    """
    out = bytearray(width * height * 4)
    off = 0

    def expand(c):
        return ((c >> 11) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31, 255)

    for by in range(0, height, 8):
        for bx in range(0, width, 8):
            for sy, sx in ((0, 0), (0, 4), (4, 0), (4, 4)):
                c0, c1 = struct.unpack_from(">HH", data, off)
                idx = data[off + 4:off + 8]
                off += 8
                a, b = expand(c0), expand(c1)
                if c0 > c1:
                    pal = [a, b, tuple((2 * a[i] + b[i]) // 3 for i in range(3)) + (255,),
                           tuple((a[i] + 2 * b[i]) // 3 for i in range(3)) + (255,)]
                else:
                    pal = [a, b, tuple((a[i] + b[i]) // 2 for i in range(3)) + (255,), (0, 0, 0, 0)]
                for y in range(4):
                    row = idx[y ^ 1]
                    for x in range(4):
                        X, Y = bx + sx + x, by + sy + y
                        if X < width and Y < height:
                            p = (Y * width + X) * 4
                            out[p:p + 4] = bytes(pal[(row >> (2 * x)) & 3])
    return out


# ---------- CMPR encode ----------
def _nearest(pal, px):
    best = 0; bd = 1 << 30
    for i, c in enumerate(pal):
        d = (c[0]-px[0])**2 + (c[1]-px[1])**2 + (c[2]-px[2])**2
        if d < bd:
            bd = d; best = i
    return best


def encode_cmpr(rgba, width, height):
    """Encode an RGBA bytes buffer (width*height*4) to GameCube CMPR. Simple min/max-endpoint
    DXT1 compressor: good enough for skins. Alpha is treated as opaque (character maps are opaque)."""
    out = bytearray()
    bw = (width + 7) // 8
    bh = (height + 7) // 8
    for by in range(bh):
        for bx in range(bw):
            for sub in range(4):
                sx = (sub & 1) * 4
                sy = (sub >> 1) * 4
                # gather 4x4 texels (clamp at edges)
                texels = []
                for py in range(4):
                    for px in range(4):
                        X = min(bx*8 + sx + px, width - 1)
                        Y = min(by*8 + sy + py, height - 1)
                        p = (Y*width + X) * 4
                        texels.append((rgba[p], rgba[p+1], rgba[p+2]))
                # pick endpoints = the two most extreme along luminance
                lo = min(texels, key=lambda c: c[0]+c[1]+c[2])
                hi = max(texels, key=lambda c: c[0]+c[1]+c[2])
                c0 = _rgb_to_565(*hi)
                c1 = _rgb_to_565(*lo)
                if c0 == c1:
                    # Uniform block. It MUST still be emitted in 4-colour OPAQUE mode, which on
                    # GX means strictly c0 > c1. Writing c0 == c1 puts the block in 3-colour
                    # PUNCH-THROUGH mode, where index 3 is transparent black -- our own decoder
                    # shrugs at that (index 0 is the right colour either way) but the hardware
                    # does not, and a mostly-flat texture is nearly ALL uniform blocks, so it
                    # shows up as banding/stripes on console while looking perfect here.
                    #
                    # Do NOT decrement c0 to make the pair -- that borrows across the 565 channel
                    # boundaries and yields a wrong second endpoint (the old coloured-stripe bug).
                    # Instead pair the colour with 0x0000 and pick the index that names it.
                    if c0:
                        # colour at the high endpoint, every index 0 -> palette[0] is exact
                        out += struct.pack(">HH", c0, 0x0000)
                        out += b"\x00\x00\x00\x00"
                    else:
                        # Pure black: nothing sorts BELOW 0, so c0 > c1 is unreachable with the
                        # colour at the high end. Put black at the LOW endpoint instead and
                        # select index 1 (0x55 = 01 01 01 01 per row). c0 is a don't-care that
                        # only has to satisfy c0 > c1, so the block stays 4-colour opaque.
                        out += struct.pack(">HH", 0x0001, 0x0000)
                        out += b"\x55\x55\x55\x55"
                    continue
                if c0 < c1:
                    c0, c1 = c1, c0
                r0, g0, b0 = _rgb565_to_rgb(c0)
                r1, g1, b1 = _rgb565_to_rgb(c1)
                pal = [(r0, g0, b0), (r1, g1, b1),
                       ((5*r0+3*r1) >> 3, (5*g0+3*g1) >> 3, (5*b0+3*b1) >> 3),
                       ((3*r0+5*r1) >> 3, (3*g0+5*g1) >> 3, (3*b0+5*b1) >> 3)]
                out += struct.pack(">HH", c0, c1)
                for py in range(4):
                    row = 0
                    for px in range(4):
                        idx = _nearest(pal, texels[py*4 + px])
                        row |= idx << (6 - px*2)
                    out.append(row)
    return bytes(out)


# ---------- RGB5A3 / RGBA32 (4x4 tiles) ----------
def _tile_pixels(width, height):
    """(x, y) of every texel in GX 4x4 tile order; positions past the edge are padding."""
    for ty in range(0, height, 4):
        for tx in range(0, width, 4):
            for y in range(ty, ty + 4):
                for x in range(tx, tx + 4):
                    yield x, y


def decode_rgb5a3(data, width, height):
    out = bytearray(width * height * 4)
    for n, (x, y) in enumerate(_tile_pixels(width, height)):
        if x >= width or y >= height: continue
        v = struct.unpack_from(">H", data, n * 2)[0]
        if v & 0x8000:
            r, g, b = (v >> 10) & 31, (v >> 5) & 31, v & 31
            px = ((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2), 255)
        else:
            a = (v >> 12) & 7
            px = (((v >> 8) & 15) * 17, ((v >> 4) & 15) * 17, (v & 15) * 17, (a << 5) | (a << 2) | (a >> 1))
        p = (y * width + x) * 4
        out[p:p + 4] = bytes(px)
    return out


def encode_rgb5a3(rgba, width, height):
    """Opaque texels use RGB555; anything with alpha below ~7/7 uses 3-bit alpha + RGB444.
    Re-encoding decoded RGB5A3 reproduces the original texel values."""
    out = bytearray()
    for x, y in _tile_pixels(width, height):
        p = (min(y, height - 1) * width + min(x, width - 1)) * 4
        r, g, b, a = rgba[p:p + 4]
        a3 = (a * 7 + 127) // 255
        if a3 == 7:
            v = 0x8000 | ((r * 31 + 127) // 255) << 10 | ((g * 31 + 127) // 255) << 5 | (b * 31 + 127) // 255
        else:
            v = a3 << 12 | ((r * 15 + 127) // 255) << 8 | ((g * 15 + 127) // 255) << 4 | (b * 15 + 127) // 255
        out += struct.pack(">H", v)
    return bytes(out)


def encode_rgb565(rgba, width, height):
    """Opaque 4x4-tiled RGB565, the inverse of decode_rgb565 (alpha is dropped)."""
    out = bytearray()
    for x, y in _tile_pixels(width, height):
        p = (min(y, height - 1) * width + min(x, width - 1)) * 4
        r, g, b = rgba[p:p + 3]
        out += struct.pack(">H", ((r * 31 + 127) // 255) << 11 | ((g * 63 + 127) // 255) << 5 | (b * 31 + 127) // 255)
    return bytes(out)


def decode_rgba32(data, width, height):
    out = bytearray(width * height * 4)
    positions = list(_tile_pixels(width, height))
    for tile in range(len(positions) // 16):
        base = tile * 64
        for k in range(16):
            x, y = positions[tile * 16 + k]
            if x >= width or y >= height: continue
            p = (y * width + x) * 4
            a, r = data[base + k * 2], data[base + k * 2 + 1]
            g, b = data[base + 32 + k * 2], data[base + 33 + k * 2]
            out[p:p + 4] = bytes((r, g, b, a))
    return out


def encode_rgba32(rgba, width, height):
    out = bytearray()
    positions = list(_tile_pixels(width, height))
    for tile in range(len(positions) // 16):
        ar = bytearray(); gb = bytearray()
        for x, y in positions[tile * 16:tile * 16 + 16]:
            p = (min(y, height - 1) * width + min(x, width - 1)) * 4
            ar += bytes((rgba[p + 3], rgba[p])); gb += bytes((rgba[p + 1], rgba[p + 2]))
        out += ar + gb
    return bytes(out)


# ---------- RGB565 / intensity / palette formats (decode only) ----------
def _tile_order(width, height, tw, th):
    for ty in range(0, height, th):
        for tx in range(0, width, tw):
            for y in range(ty, ty + th):
                for x in range(tx, tx + tw):
                    yield x, y


def decode_rgb565(data, width, height):
    out = bytearray(width * height * 4)
    for n, (x, y) in enumerate(_tile_order(width, height, 4, 4)):
        if x >= width or y >= height: continue
        r, g, b = _rgb565_to_rgb(struct.unpack_from(">H", data, n * 2)[0])
        p = (y * width + x) * 4
        out[p:p + 4] = bytes((r, g, b, 255))
    return out


def decode_i8(data, width, height):
    """GX I8: R, G, B and A all equal the intensity."""
    out = bytearray(width * height * 4)
    for n, (x, y) in enumerate(_tile_order(width, height, 8, 4)):
        if x >= width or y >= height: continue
        v = data[n]
        p = (y * width + x) * 4
        out[p:p + 4] = bytes((v, v, v, v))
    return out


def decode_i4(data, width, height):
    out = bytearray(width * height * 4)
    for n, (x, y) in enumerate(_tile_order(width, height, 8, 8)):
        if x >= width or y >= height: continue
        v = ((data[n >> 1] >> (0 if n & 1 else 4)) & 15) * 17
        p = (y * width + x) * 4
        out[p:p + 4] = bytes((v, v, v, v))
    return out


def decode_ia8(data, width, height):
    out = bytearray(width * height * 4)
    for n, (x, y) in enumerate(_tile_order(width, height, 4, 4)):
        if x >= width or y >= height: continue
        a, i = data[n * 2], data[n * 2 + 1]
        p = (y * width + x) * 4
        out[p:p + 4] = bytes((i, i, i, a))
    return out


def decode_ci8(data, width, height, palette=None, alpha_bits=3):
    """C8 indices in 8x4 tiles. The 256-entry big-endian u16 palette follows the pixels; its
    entries are RGB5A3 when the header declares alpha bits, otherwise RGB565."""
    if palette is None:
        return decode_i8(data, width, height)
    out = bytearray(width * height * 4)
    for n, (x, y) in enumerate(_tile_order(width, height, 8, 4)):
        if x >= width or y >= height: continue
        v = struct.unpack_from(">H", palette, data[n] * 2)[0]
        if alpha_bits and not v & 0x8000:
            a = (v >> 12) & 7
            px = (((v >> 8) & 15) * 17, ((v >> 4) & 15) * 17, (v & 15) * 17, (a << 5) | (a << 2) | (a >> 1))
        elif alpha_bits:
            r, g, b = (v >> 10) & 31, (v >> 5) & 31, v & 31
            px = ((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2), 255)
        else:
            px = _rgb565_to_rgb(v) + (255,)
        p = (y * width + x) * 4
        out[p:p + 4] = bytes(px)
    return out


DECODERS = {"CMPR": decode_cmpr, "RGB5A3": decode_rgb5a3, "RGBA32": decode_rgba32, "RGB565": decode_rgb565,
            "I8": decode_i8, "I4": decode_i4, "IA8": decode_ia8, "CI8": decode_ci8}
ENCODERS = {"CMPR": encode_cmpr, "RGB5A3": encode_rgb5a3, "RGBA32": encode_rgba32, "RGB565": encode_rgb565}
# (tile width, tile height, bytes per tile) for every documented format byte.
_TILES = {"CMPR": (8, 8, 32), "RGB5A3": (4, 4, 32), "RGB565": (4, 4, 32), "RGBA32": (4, 4, 64),
          "I8": (8, 4, 32), "IA4": (8, 4, 32), "I4": (8, 8, 32), "IA8": (4, 4, 32), "CI8": (8, 4, 32)}


# ---------- header parsing over an Archive ----------
class TexEntry:
    # `size` is the BASE level only -- that is what you decode to look at the image. `chain_size`
    # is every level the record declares, which is what you must respect when WRITING.
    # table/local_index/header_chunk/data_chunk are set by list_all_textures only.
    __slots__ = ("index", "hash", "name", "width", "height", "fmt", "data_offset", "size",
                 "levels", "chain_size", "table", "local_index", "header_chunk", "data_chunk",
                 "format_enum", "channel_bits")


def _cmpr_size(w, h):
    return ((w + 7)//8) * ((h + 7)//8) * 32


def chain_size(w, h, levels):
    """Bytes for base + every mip level, the way the archive packs them."""
    return sum(_cmpr_size(max(1, w >> k), max(1, h >> k)) for k in range(max(1, levels)))


def texture_size(fmt, w, h):
    """Base-level bytes for a format byte; unknown formats fall back to the CMPR size."""
    tw, th, tb = _TILES.get(FORMATS.get(fmt), _TILES["CMPR"])
    return ((w + tw - 1) // tw) * ((h + th - 1) // th) * tb


def format_chain_size(fmt, w, h, levels):
    return sum(texture_size(fmt, max(1, w >> k), max(1, h >> k)) for k in range(max(1, levels)))


def _entry(hdr, i, hashnames):
    o = i*96
    e = TexEntry()
    e.index = i
    e.hash = struct.unpack_from(">I", hdr, o)[0]
    e.width = struct.unpack_from(">H", hdr, o+4)[0]
    e.height = struct.unpack_from(">H", hdr, o+6)[0]
    e.format_enum = struct.unpack_from(">I", hdr, o+16)[0]
    e.channel_bits = tuple(hdr[o+12:o+16])
    # Every retail header carries a nonzero red depth; records without one (synthetic/legacy
    # test fixtures that only set byte +13) keep the historical byte +13 interpretation.
    e.fmt = ENUM_TO_FMT.get(e.format_enum, hdr[o+13]) if e.channel_bits[0] else hdr[o+13]
    e.data_offset = struct.unpack_from(">I", hdr, o+20)[0]
    e.name = (hashnames or {}).get(e.hash, f"{e.hash:08X}")
    e.levels = hdr[o+11] or 1
    e.size = texture_size(e.fmt, e.width, e.height)
    e.chain_size = format_chain_size(e.fmt, e.width, e.height, e.levels)
    return e


def list_textures(archive, hashnames=None):
    """Return TexEntry list for a loaded nlg_pack.Archive (first texture table only, CMPR sizes)."""
    th = archive.find_chunks(type_id=0xB601)[0]
    hdr = archive.get_chunk_bytes(th)
    return [_entry(hdr, i, hashnames) for i in range(len(hdr)//96)]


def list_all_textures(archive, hashnames=None):
    """Every texture in every header/pixel table pair, numbered continuously.

    Most archives hold one 0xB601/0xB603 pair. Big Mac, Little Mac, Glass Joe 2, Great Tiger (both),
    effects and the frontend bundles hold two or three; each header table indexes its own pixel
    chunk, and in every retail archive the pixel chunk directly follows its header chunk. With one
    pair the numbering equals list_textures(). Sizes follow each record's own format.
    """
    heads = archive.find_chunks(type_id=0xB601)
    pixels = archive.find_chunks(type_id=0xB603)
    if len(heads) != len(pixels) or any(not h < p for h, p in zip(heads, pixels)):
        raise ValueError("texture header and pixel tables do not pair up")
    ents = []
    for table, (hr, dr) in enumerate(zip(heads, pixels)):
        hdr = archive.get_chunk_bytes(hr)
        if len(hdr) % 96:
            raise ValueError("texture headers have an unsupported size")
        for i in range(len(hdr) // 96):
            e = _entry(hdr, i, hashnames)
            e.index, e.local_index, e.table, e.header_chunk, e.data_chunk = len(ents), i, table, hr, dr
            e.size = texture_size(e.fmt, e.width, e.height)
            e.chain_size = format_chain_size(e.fmt, e.width, e.height, e.levels)
            ents.append(e)
    return ents


def _pixel_chunk(archive, ent):
    dr = getattr(ent, "data_chunk", None)
    return archive.find_chunks(type_id=0xB603)[0] if dr is None else dr


def decode_texture(archive, ent):
    """Base level of any supported format as RGBA bytes (width*height*4, top-down)."""
    decoder = DECODERS.get(FORMATS.get(ent.fmt))
    if decoder is None:
        raise NotImplementedError(f"format {FORMATS.get(ent.fmt, ent.fmt)} not yet supported")
    size = texture_size(ent.fmt, ent.width, ent.height)
    pixels = archive.get_chunk_bytes(_pixel_chunk(archive, ent))
    raw = pixels[ent.data_offset:ent.data_offset + size]
    if len(raw) != size:
        raise ValueError("texture pixels are truncated")
    if FORMATS.get(ent.fmt) == "CI8":
        pal = pixels[ent.data_offset + size:ent.data_offset + size + 512]
        bits = getattr(ent, "channel_bits", (5, 5, 5, 3))
        return decoder(raw, ent.width, ent.height, pal if len(pal) == 512 else None, bits[3])
    return decoder(raw, ent.width, ent.height)


def export_png(archive, ent, path):
    Image = _pil()
    rgba = decode_texture(archive, ent)
    Image.frombytes("RGBA", (ent.width, ent.height), bytes(rgba)).save(path)


def import_png_to_texture(archive, ent, png_path):
    """Encode a PNG back into the archive's TextureData chunk for texture `ent` (CMPR, same size).
    Returns the modified TextureData chunk bytes (call archive.replace_chunk to commit)."""
    Image = _pil()
    im = Image.open(png_path).convert("RGBA").resize((ent.width, ent.height))
    return replace_texture_rgba(archive, ent, im.tobytes())


def replace_texture_rgba(archive, ent, rgba):
    """Same, from raw RGBA bytes (w*h*4, top-down). No Pillow needed.

    Rewrites the WHOLE mip chain, not just the base level. The record's level count at +0x0B
    tells the game how many levels to read from this offset, and it keeps reading them whether
    we refreshed them or not -- so repainting only level 0 leaves every smaller level holding
    the ORIGINAL art, and the old image comes back as the camera pulls away. Harmless on the
    128x4 ramps (1 level, which is why this hid for so long) and wrong on every detail map,
    which are 32x32 and up and all mipped.

    Every format is fixed-size per dimensions, so the rebuilt chain is byte-for-byte the same
    length and cannot shift the texture that follows. The chain is written in the record's own
    format: an RGBA32 ramp stays RGBA32 (writing CMPR there would be read back as garbage).
    """
    levels = getattr(ent, "levels", 0) or 1
    if FORMATS.get(ent.fmt) not in ENCODERS:
        raise NotImplementedError(f"format {FORMATS.get(ent.fmt, ent.fmt)} not yet supported")
    enc, _ = build_mip_chain(rgba, ent.width, ent.height, levels, ent.fmt)
    want = format_chain_size(ent.fmt, ent.width, ent.height, levels)
    if len(enc) != want:
        raise RuntimeError("mip chain for %dx%d x%d encoded to %d bytes, expected %d"
                           % (ent.width, ent.height, levels, len(enc), want))
    td = _pixel_chunk(archive, ent)
    tdata = bytearray(archive.get_chunk_bytes(td))
    tdata[ent.data_offset:ent.data_offset + len(enc)] = enc   # fixed-size -> in-place
    return td, bytes(tdata)


def mip_levels(w, h):
    """How many mip levels this engine stores for a given size.

    Derived from the shipped textures, and it matches every dimension class in the referee
    archive: 32x32->3, 32x64->3, 64x64->4, 64x128->4, 128x128->5, 256x8->1, 128x4->1.
    That is max(1, log2(min(w,h)) - 2) -- it halves down to an 8x8 floor.
    """
    m = min(w, h)
    lv = 0
    while m > 8:
        m //= 2
        lv += 1
    return max(1, lv + 1 if min(w, h) >= 8 else 1)


def downsample_rgba(rgba, w, h):
    """2x2 box filter. Returns (bytes, w2, h2)."""
    w2, h2 = max(1, w // 2), max(1, h // 2)
    out = bytearray(w2 * h2 * 4)
    for y in range(h2):
        r0 = (min(h - 1, y * 2) * w) * 4
        r1 = (min(h - 1, y * 2 + 1) * w) * 4
        for x in range(w2):
            c0 = min(w - 1, x * 2) * 4
            c1 = min(w - 1, x * 2 + 1) * 4
            d = (y * w2 + x) * 4
            for k in range(4):
                out[d + k] = (rgba[r0 + c0 + k] + rgba[r0 + c1 + k] +
                              rgba[r1 + c0 + k] + rgba[r1 + c1 + k]) // 4
    return bytes(out), w2, h2


def build_mip_chain(rgba, w, h, levels=None, fmt=6):
    """Base level plus every mip the record will claim, concatenated, encoded as `fmt` (CMPR).

    THIS IS NOT OPTIONAL. The 96-byte texture record stores a mip COUNT at +0x0B and the
    game reads that many levels contiguously from the data offset. Writing only the base
    level while the record claims 4 makes it read past the end of the pixel data -- which
    hangs the game on load, not glitches quietly.

    Pass `levels` to match an EXISTING record when overwriting in place; leave it None for a
    new texture and the engine's own rule (mip_levels) decides.
    """
    levels = mip_levels(w, h) if levels is None else max(1, levels)
    encode = ENCODERS[FORMATS[fmt]]
    out = bytearray(encode(rgba, w, h))
    cur, cw, ch = rgba, w, h
    for _ in range(levels - 1):
        cur, cw, ch = downsample_rgba(cur, cw, ch)
        out += encode(cur, cw, ch)
    return bytes(out), levels


def add_texture_rgba(archive, name, rgba, w, h, template_index=1):
    """Add a new texture from raw RGBA bytes. No Pillow needed. Returns the new hash."""
    import nlg_hash
    if w % 8 or h % 8:
        raise ValueError(f"CMPR needs multiples of 8, got {w}x{h}")
    hh = nlg_hash.string_to_hash(name)
    hr = archive.find_chunks(type_id=0xB601)[0]
    dr = archive.find_chunks(type_id=0xB603)[0]
    hdr = bytearray(archive.get_chunk_bytes(hr))
    tdata = bytearray(archive.get_chunk_bytes(dr))
    for i in range(len(hdr) // 96):
        if struct.unpack_from(">I", hdr, i * 96)[0] == hh:
            raise ValueError(f"'{name}' ({hh:08X}) is already in this archive")
    data_off = len(tdata)
    pixels, levels = build_mip_chain(rgba, w, h)
    tdata += pixels
    rec = bytearray(hdr[template_index * 96:(template_index + 1) * 96])
    struct.pack_into(">I", rec, 0, hh)
    struct.pack_into(">H", rec, 4, w)
    struct.pack_into(">H", rec, 6, h)
    # +0x0A/+0x0B carry the mip count (both bytes, always equal in the shipped records).
    # The template's count belongs to the template's SIZE -- copying it is what broke this.
    rec[10] = levels
    rec[11] = levels
    # New pixels are always CMPR: runtime format enum 2 with the retail CMPR channel depths
    # (5,6,5,0). A template can be an RGB565 ramp (enum 0), whose format must not carry over.
    rec[12:16] = bytes((5, 6, 5, 0))
    struct.pack_into(">I", rec, 16, 2)
    struct.pack_into(">I", rec, 20, data_off)
    hdr += rec
    archive.replace_chunk(dr, bytes(tdata))
    archive.replace_chunk(hr, bytes(hdr))
    return hh


def add_texture(archive, name, png_path, width=None, height=None, template_index=1):
    """Add a BRAND NEW texture to a character archive and commit it.

    Until now materials could only point at textures that already existed in the archive (or at
    global/*), so "any material you want" really meant "any repaint of the 39 textures this
    fighter shipped with". This appends a 40th: a new 96B TextureHeader record on 0xB601 and new
    CMPR pixels on the end of 0xB603, both resize-injected via Archive.replace_chunk.

    `name` is hashed with the engine's own hash (nlg_hash.string_to_hash), so materials can
    reference it by hash exactly like a stock texture. Dimensions must be multiples of 8 (CMPR
    super-tile); they default to the PNG's own size rounded up.

    Returns the new texture's hash. Caller still needs archive.write().

    CAVEAT, do not skip this: resource hashes are also listed in art/hashid.bin, the global
    registry. A hash that is not in hashid.bin has never been tested in-game here. Repainting an
    existing texture, or pointing a slot at a different existing/global texture, are the two
    PROVEN paths. Treat a fresh hash as needing a Dolphin boot before you trust it.
    """
    Image = _pil()
    im = Image.open(png_path).convert("RGBA")
    w = width or ((im.width + 7) // 8) * 8
    h = height or ((im.height + 7) // 8) * 8
    if im.size != (w, h):
        im = im.resize((w, h))
    return add_texture_rgba(archive, name, im.tobytes(), w, h, template_index)
