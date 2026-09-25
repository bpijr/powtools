"""Old Wii Wwise (bank version 35) media, banks and object links. Pure Python, big-endian.

Evidence (full private scan: tests/corpus_audio_scan.py, AUDIO_FORMAT.md):

Media are RIFX/WAVE with chunks `fmt ` (40 bytes, codec 0xFFF0 = Nintendo DSP-ADPCM),
`smpl` (60), `WiiH` (46 bytes per channel, one DSP context each), `JUNK` (zeros padding the
data start to a 32-byte file offset) and `data`; two retail stereo streams add `cue `/`LIST`
markers. Only 1 and 2 channels occur. fmt+8 is the sample count and always equals the smpl
loop end; the loop start is always 0 (a whole-sound loop marker); data holds exactly
ceil(samples / 14) frames per channel. Each channel's WiiH predictor/scale equals the
header byte of that channel's first frame, and the loop predictor/scale equals it too.
The RIFX size field is not len-8: it differs from len-8 by a constant per chunk layout,
so replacements keep the template's field and total length.

Stereo channel layout depends on storage (WiiH predictor/scale test on every file):
streamed files and bank prefetch prefixes interleave 8-byte frames (L, R, L, R...);
media held whole in a bank store each channel contiguously (all L frames, then all R).

Banks: chunk list (BKHD version 35 / bank id, DATA, DIDX, HIRC, STID ...). DIDX rows are
(media id, offset in DATA, size). A DIDX entry smaller than its RIFX data declares is a
prefetch prefix of the streamed `<id>.wav` (byte-identical prefix in retail). HIRC v35
objects are {u32 type, u32 size, u32 id, body}; the sound source prefix is {plugin,
stream type (0 in memory, 1 streamed, 2 prefetch), rate, format bits, source id, file id,
offset, size} and for in-memory sounds offset/size equal the DIDX row. Replacements are
therefore spliced in place at the same size so DIDX, HIRC and prefetch sizes never move.

Event, state and state-group IDs are FNV-1 32-bit hashes of the lowercase name
(`fnv1_32`), checked against every bank's .txt listing. Media IDs are not name hashes.
"""
import math
import struct

import nlg_dsp


class WwiseFormatError(ValueError):
    pass


def _require(ok, message):
    if not ok: raise WwiseFormatError(message)


def fnv1_32(name):
    h = 2166136261
    for b in name.lower().encode("utf-8"):
        h = (h * 16777619) & 0xFFFFFFFF
        h ^= b
    return h


DSP_CODEC = 0xFFF0
CHANNEL_MASKS = {1: 0x4, 2: 0x3}          # retail WAVEFORMATEXTENSIBLE masks: FC / FL|FR
STORAGE_LAYOUT = {"streamed": "interleaved", "prefetch": "interleaved", "in-memory": "sequential"}
CONTEXT_BYTES = 46


class Media:
    """Strict read-only view of one RIFX DSP media file.

    `truncated=True` accepts a bank prefetch prefix whose data chunk is cut short.
    """
    def __init__(self, raw, truncated=False):
        raw = bytes(raw)
        _require(len(raw) >= 12 and raw[:4] == b"RIFX" and raw[8:12] == b"WAVE", "Not Wii Wwise RIFX media.")
        self.raw = raw; self.size_field = struct.unpack_from(">I", raw, 4)[0]
        self.chunks = []; self.offsets = {}; self.truncated = False; off = 12
        while off < len(raw):
            _require(off + 8 <= len(raw), "Truncated chunk header.")
            tag = raw[off:off + 4]; size = struct.unpack_from(">I", raw, off + 4)[0]
            end = off + 8 + size
            if end > len(raw):
                _require(truncated and tag == b"data", f"{tag!r} chunk overruns the media.")
                self.truncated = True; self.declared_data = size; end = len(raw)
            _require(tag not in self.offsets, f"Duplicate {tag!r} chunk.")
            self.offsets[tag] = off + 8; self.chunks.append((tag, raw[off + 8:end])); off = end
        m = dict(self.chunks)
        for tag in (b"fmt ", b"smpl", b"WiiH", b"data"):
            _require(tag in m, f"Missing {tag.decode()} chunk.")
        fmt = m[b"fmt "]
        _require(len(fmt) == 40, "Unsupported fmt size (retail is 40 bytes).")
        (self.codec, self.channels, self.rate, self.samples, self.block_align, self.bits,
         self.extra, self.valid_bits, self.channel_mask) = struct.unpack_from(">HHIIHHHHI", fmt)
        _require(self.codec == DSP_CODEC, "Only Nintendo DSP-ADPCM media (fmt 0xFFF0) are supported.")
        _require(self.channels in CHANNEL_MASKS, "Only mono and stereo media are supported.")
        _require(self.block_align == 8 and self.bits == 4 and self.extra == 22, "Unsupported DSP fmt fields.")
        _require(0 < self.rate <= 96000, "Invalid sample rate.")
        smpl = m[b"smpl"]
        _require(len(smpl) == 60, "Unsupported smpl size (retail is 60 bytes).")
        words = struct.unpack(">15I", smpl)
        self.loop_count = words[7]
        self.loop = (words[11], words[12]) if words[7] == 1 else None
        wiih = m[b"WiiH"]
        _require(len(wiih) == CONTEXT_BYTES * self.channels, "WiiH must hold one 46-byte DSP context per channel.")
        self.contexts = [nlg_dsp.parse_context(wiih[CONTEXT_BYTES * c:CONTEXT_BYTES * (c + 1)])
                         for c in range(self.channels)]
        self.data = m[b"data"]
        if not self.truncated:
            _require(len(self.data) % (8 * self.channels) == 0, "Data is not whole DSP frames per channel.")
            self.frames = len(self.data) // (8 * self.channels)
            self.size_delta = len(raw) - 8 - self.size_field
        else:
            _require(self.declared_data % (8 * self.channels) == 0, "Declared data is not whole DSP frames.")
            self.frames = self.declared_data // (8 * self.channels)
            self.size_delta = None
        self.capacity = self.frames * nlg_dsp.SAMPLES_PER_FRAME

    @property
    def retail_consistent(self):
        """The invariants every complete retail media file satisfies."""
        return (not self.truncated and self.loop == (0, self.samples) and
                -(-self.samples // 14) == self.frames and self.channel_mask == CHANNEL_MASKS[self.channels] and
                all(c["loop_ps"] == c["ps"] and not (c["gain"] or c["yn1"] or c["yn2"] or c["loop_yn1"] or c["loop_yn2"])
                    for c in self.contexts))

    def tags(self): return [t for t, _ in self.chunks]

    def seconds(self): return self.samples / self.rate


def layout_evidence(media):
    """Layouts whose first frame per channel agrees with every WiiH predictor/scale byte."""
    if media.channels == 1: return {"mono"} if media.data[:1] == bytes([media.contexts[0]["ps"]]) else set()
    result = set(); n = media.channels
    for layout in ("interleaved", "sequential"):
        ok = True
        for c in range(n):
            pos = 8 * c if layout == "interleaved" else c * media.frames * 8
            if pos >= len(media.data) or media.data[pos] != media.contexts[c]["ps"]: ok = False
        if ok: result.add(layout)
    return result


def layout_for(media, storage):
    if media.channels == 1: return "mono"
    _require(storage in STORAGE_LAYOUT, "Unknown media storage.")
    return STORAGE_LAYOUT[storage]


def split_channels(data, channels, frames, layout):
    if channels == 1: return [bytes(data[:frames * 8])]
    if layout == "interleaved":
        return [b"".join(data[(f * channels + c) * 8:(f * channels + c) * 8 + 8] for f in range(frames))
                for c in range(channels)]
    _require(layout == "sequential", "Unknown stereo layout.")
    return [bytes(data[c * frames * 8:(c + 1) * frames * 8]) for c in range(channels)]


def join_channels(parts, layout):
    if len(parts) == 1: return parts[0]
    _require(len({len(p) for p in parts}) == 1, "Channels must have equal frame counts.")
    if layout == "sequential": return b"".join(parts)
    _require(layout == "interleaved", "Unknown stereo layout.")
    out = bytearray()
    for f in range(len(parts[0]) // 8):
        for p in parts: out += p[f * 8:f * 8 + 8]
    return bytes(out)


def decode(media, layout, limit=None):
    """Per-channel s16 lists for the playable samples (fmt+8), or the whole prefix if truncated."""
    frames = media.frames if not media.truncated else len(media.data) // (8 * media.channels)
    count = min(media.samples, frames * 14) if limit is None else min(limit, frames * 14)
    frames = min(frames, -(-count // 14))
    parts = split_channels(media.data, media.channels, frames, layout)
    return [nlg_dsp.decode(p, ctx["coefs"], count, (ctx["yn1"], ctx["yn2"]))
            for p, ctx in zip(parts, media.contexts)]


def _loop_context(adpcm, decoded, start):
    if start == 0: return adpcm[0], 0, 0
    _require(start % 14 == 0, "Loop start must fall on a DSP frame boundary (a multiple of 14 samples).")
    return adpcm[(start // 14) * 8], decoded[start - 1], decoded[start - 2] if start >= 2 else 0


def encode_like(template, channels, layout, loop=None, progress=None, cancel=None):
    """Encode per-channel s16 PCM into the template's exact container (see `encode_channels`)."""
    return assemble(template, encode_channels(template, channels, loop, progress, cancel), layout)


class Encoded:
    def __init__(self, parts, contexts, smpl):
        self.parts, self.contexts, self.smpl = parts, contexts, smpl


def assemble(template, encoded, layout):
    """Media bytes of the template's length. Every chunk except WiiH, data and the smpl loop words
    is byte-identical, including the RIFX size field, fmt sample count and JUNK padding."""
    if template.channels > 1: _require(layout in ("interleaved", "sequential"), "Unknown stereo layout.")
    data = join_channels(encoded.parts, layout)
    body = bytearray(b"RIFX" + struct.pack(">I", template.size_field) + b"WAVE")
    for tag, contents in template.chunks:
        new = {b"WiiH": encoded.contexts, b"data": data, b"smpl": encoded.smpl}.get(tag, contents)
        _require(len(new) == len(contents), f"{tag.decode()} must keep its size.")
        body += tag + struct.pack(">I", len(new)) + new
    out = bytes(body)
    _require(len(out) == len(template.raw), "Replacement length differs from the original.")
    return out


def encode_channels(template, channels, loop=None, progress=None, cancel=None):
    """DSP-encode per-channel s16 PCM for the template; layout-independent until `assemble`.

    Shorter input is padded with digital silence up to the template capacity; the playable
    length (fmt+8) stays the retail length. `loop=None` keeps the template's loop record
    (retail: the whole sound); an explicit (start, end) must lie inside the replacement and
    start on a frame boundary; its predictor/scale and history go into the loop context.
    """
    _require(isinstance(template, Media) and not template.truncated, "A complete template is required.")
    _require(isinstance(channels, (list, tuple)) and len(channels) == template.channels,
             f"The original sound has {template.channels} channel(s).")
    lengths = {len(c) for c in channels}
    _require(len(lengths) == 1 and 0 < min(lengths), "Every channel needs the same non-zero sample count.")
    count = lengths.pop()
    _require(count <= template.samples, "Replacement is longer than the original sound.")
    for c in channels:
        _require(all(type(s) is int and -32768 <= s <= 32767 for s in c), "PCM samples must be 16-bit integers.")
    if loop is None:
        loop = template.loop
        _require(loop is not None, "The template has no loop record to preserve.")
    else:
        _require(isinstance(loop, (list, tuple)) and len(loop) == 2 and all(type(v) is int for v in loop),
                 "Loop must be (start, end) sample indexes.")
        _require(0 <= loop[0] < loop[1] <= min(count, template.samples) and loop[1] - loop[0] >= 14,
                 "Loop must lie inside the replacement and span at least one DSP frame.")
    capacity = template.capacity
    contexts = []; parts = []
    total = capacity * len(channels); done = [0]
    for c, pcm in enumerate(channels):
        padded = list(pcm) + [0] * (capacity - count)
        coefs = nlg_dsp.correlate_coefs(padded)
        def step(pos, _t, base=c * capacity):
            if progress: progress(base + pos, total)
        adpcm, _ = nlg_dsp.encode(padded, coefs, progress=step, cancel=cancel)
        _require(len(adpcm) == template.frames * 8, "DSP encoding produced an unexpected frame count.")
        decoded = nlg_dsp.decode(adpcm, coefs, capacity)
        lps, lyn1, lyn2 = _loop_context(adpcm, decoded, loop[0])
        gain = template.contexts[c]["gain"]
        flat = [v for pair in coefs for v in pair]
        contexts.append(struct.pack(">16h7H", *flat, gain, adpcm[0], 0, 0, lps, lyn1 & 0xFFFF, lyn2 & 0xFFFF))
        parts.append(adpcm)
    smpl = bytearray(dict(template.chunks)[b"smpl"])
    struct.pack_into(">II", smpl, 44, loop[0], loop[1])
    return Encoded(parts, b"".join(contexts), bytes(smpl))


def pad_encoded(template, replacement, layout):
    """Fit an already-encoded DSP media file into the template's container.

    Channel count, rate and chunk list must match and it may not hold more frames. Missing
    frames are encoded from silence continuing each channel's own decoder history, so the
    supplied audio is kept and the file, DIDX and HIRC sizes stay the original's. Every non-audio
    chunk (fmt sample count, smpl loop, JUNK) is taken from the template.
    """
    r = Media(replacement)
    _require(r.channels == template.channels and r.rate == template.rate and r.tags() == template.tags(),
             "Pre-encoded replacement must match the original channel count, rate and chunk layout.")
    _require(r.frames <= template.frames, "Replacement is longer than the original sound.")
    parts = split_channels(r.data, r.channels, r.frames, layout)
    extra = template.frames - r.frames; padded = []
    for p, ctx in zip(parts, r.contexts):
        pcm = nlg_dsp.decode(p, ctx["coefs"], r.frames * 14, (ctx["yn1"], ctx["yn2"]))
        hist = [pcm[-2] if len(pcm) > 1 else ctx["yn2"], pcm[-1] if pcm else ctx["yn1"]]
        tail = bytearray()
        for _ in range(extra):
            frame, hist = nlg_dsp.encode_frame(hist, [0] * 14, ctx["coefs"]); tail += frame
        padded.append(p + bytes(tail))
    return Encoded(padded, dict(r.chunks)[b"WiiH"], dict(template.chunks)[b"smpl"])


def structural_diff(original, replacement):
    """Chunks/fields that differ between two media of one container (empty = only audio changed).

    WiiH coefficients/predictor, data bytes and smpl loop words are the audio payload and are
    reported only when their sizes differ.
    """
    a, b = Media(original), Media(replacement); diffs = []
    if len(original) != len(replacement): diffs.append("file length")
    if a.size_field != b.size_field: diffs.append("RIFX size field")
    if a.tags() != b.tags(): diffs.append("chunk order")
    for (tag, x), (_, y) in zip(a.chunks, b.chunks):
        if len(x) != len(y): diffs.append(f"{tag.decode()} size")
        elif tag == b"smpl":
            if x[:44] != y[:44] or x[52:] != y[52:]: diffs.append("smpl header")
        elif tag not in (b"WiiH", b"data") and x != y: diffs.append(f"{tag.decode()} bytes")
    for ca, cb in zip(a.contexts, b.contexts):
        if ca["gain"] != cb["gain"] or cb["yn1"] or cb["yn2"]: diffs.append("WiiH gain/history")
    return diffs


# --- banks ------------------------------------------------------------------------------------

class Bank:
    """Strict chunk view of a big-endian bank; HIRC objects are indexed for version 35 only."""
    def __init__(self, raw):
        raw = bytes(raw); self.raw = raw; self.chunks = []; off = 0
        while off < len(raw):
            _require(off + 8 <= len(raw), "Truncated bank chunk header.")
            tag = raw[off:off + 4]; size = struct.unpack_from(">I", raw, off + 4)[0]
            _require(off + 8 + size <= len(raw), f"Bank {tag!r} chunk overruns the file.")
            self.chunks.append((tag, off + 8, size)); off += 8 + size
        tags = [t for t, _, _ in self.chunks]
        _require(tags and tags[0] == b"BKHD" and len(set(tags)) == len(tags), "Unsupported bank chunk list.")
        bkhd = self.chunk(b"BKHD")
        _require(len(bkhd) >= 8, "Truncated BKHD.")
        self.version, self.bank_id = struct.unpack_from(">II", bkhd)
        self.didx = {}
        if b"DIDX" in tags:
            _require(b"DATA" in tags, "DIDX without DATA.")
            didx = self.chunk(b"DIDX"); _require(len(didx) % 12 == 0, "Malformed DIDX.")
            data_off, data_size = self.span(b"DATA")
            for o in range(0, len(didx), 12):
                mid, offset, size = struct.unpack_from(">III", didx, o)
                _require(mid not in self.didx and offset + size <= data_size, f"Media {mid} overruns DATA.")
                self.didx[mid] = (data_off + offset, size)
        self.objects = []
        if b"HIRC" in tags and self.version == 35:
            h_off, h_size = self.span(b"HIRC"); end = h_off + h_size
            _require(h_size >= 4, "Truncated HIRC.")
            count = struct.unpack_from(">I", raw, h_off)[0]; o = h_off + 4
            for _ in range(count):
                _require(o + 12 <= end, "Truncated HIRC object.")
                kind, size, oid = struct.unpack_from(">III", raw, o)
                _require(size >= 4 and o + 8 + size <= end, "HIRC object overruns its chunk.")
                self.objects.append((kind, oid, o + 12, size - 4)); o += 8 + size
            _require(o == end, "Trailing HIRC bytes.")

    def span(self, tag):
        for t, off, size in self.chunks:
            if t == tag: return off, size
        raise WwiseFormatError(f"Missing {tag.decode()} chunk.")

    def chunk(self, tag):
        off, size = self.span(tag); return self.raw[off:off + size]

    def media(self, mid):
        off, size = self.didx[mid]; return self.raw[off:off + size]

    def media_kind(self, mid):
        """'in-memory' when the DIDX entry is a whole media file, 'prefetch' when it is a prefix."""
        raw = self.media(mid)
        try: Media(raw); return "in-memory"
        except WwiseFormatError:
            Media(raw, truncated=True); return "prefetch"

    def splice(self, mid, payload):
        off, size = self.didx[mid]
        _require(len(payload) == size, "Bank media must keep its original size.")
        return self.raw[:off] + bytes(payload) + self.raw[off + size:]

    def body(self, index):
        _, _, off, size = self.objects[index]; return self.raw[off:off + size]

    def sounds(self):
        out = {}
        for i, (kind, oid, off, size) in enumerate(self.objects):
            if kind == 2 and size >= 32:
                out[oid] = struct.unpack_from(">8I", self.raw, off)
        return out

    def music_sources(self):
        out = {}
        for kind, oid, off, size in self.objects:
            if kind == 11 and size >= 4:
                n = struct.unpack_from(">I", self.raw, off)[0]
                if 4 + 32 * n <= size:
                    out[oid] = [struct.unpack_from(">8I", self.raw, off + 4 + 32 * k) for k in range(n)]
        return out

    def events(self):
        out = {}
        for kind, oid, off, size in self.objects:
            if kind == 4 and size >= 4:
                n = struct.unpack_from(">I", self.raw, off)[0]
                if 4 + 4 * n == size: out[oid] = list(struct.unpack_from(">%dI" % n, self.raw, off + 4))
        return out

    def actions(self):
        return {oid: struct.unpack_from(">II", self.raw, off) for kind, oid, off, size in self.objects
                if kind == 3 and size >= 8}


HIRC_TYPES = {2: "sound", 3: "action", 4: "event", 5: "random/sequence container", 7: "actor-mixer",
              10: "music segment", 11: "music track", 12: "music switch", 13: "music playlist"}


def parse_listing(text, bank):
    """Rows (section, id, name, bank) from a bank .txt listing (Event / State / media sections)."""
    rows = []; section = None
    for line in text.splitlines():
        if line and not line.startswith("\t"):
            section = line.split("\t")[0]; continue
        parts = line.split("\t")
        if section and len(parts) > 2 and parts[1].isdigit():
            rows.append((section, int(parts[1]), parts[2], bank))
    return rows


def snr_db(reference, decoded):
    signal = sum(s * s for s in reference); noise = sum((a - b) ** 2 for a, b in zip(reference, decoded))
    if noise == 0: return math.inf
    return 10 * math.log10(max(signal, 1) / noise)
