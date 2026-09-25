"""Nintendo GameCube/Wii DSP-ADPCM codec in pure Python (no numpy, no external tools).

Frame: 8 bytes = header byte (predictor index << 4 | scale) + 14 signed 4-bit samples.
Decode: s = clamp16(((nibble << scale) << 11) + c1*yn1 + c2*yn2 + 1024 >> 11).

`correlate_coefs` is the standard DSPADPCM coefficient search (the reverse-engineered Nintendo
algorithm also used by VGAudio / gc-dspadpcm-encode): per 14-sample block an order-2 linear
predictor is solved, the records are merged into one average filter, then split into 8 by three
rounds of refinement. `encode_frame` tries every coefficient pair and keeps the smallest squared
error. C integer semantics (truncating division) are reproduced.
"""
import math
import struct

SAMPLES_PER_FRAME = 14
BYTES_PER_FRAME = 8
_HALF = 0.4999999                     # the reference encoder rounds with 0.4999999f
_HALF = struct.unpack("<f", struct.pack("<f", _HALF))[0]


def frames_for(samples):
    return -(-samples // SAMPLES_PER_FRAME)


def bytes_for(samples):
    return frames_for(samples) * BYTES_PER_FRAME


def _tdiv(a, b):
    """C99 integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def _clamp16(v):
    return 32767 if v > 32767 else (-32768 if v < -32768 else v)


# --- coefficient search -----------------------------------------------------------------------

def _inner_product(buf, base):
    out = [0.0, 0.0, 0.0]
    for i in range(3):
        acc = 0.0
        for x in range(14):
            acc -= buf[base + x - i] * buf[base + x]
        out[i] = acc
    return out


def _outer_product(buf, base):
    mtx = [[0.0] * 3 for _ in range(3)]
    for x in (1, 2):
        for y in (1, 2):
            acc = 0.0
            for z in range(14):
                acc += buf[base + z - x] * buf[base + z - y]
            mtx[x][y] = acc
    return mtx


def _analyze_ranges(mtx, idx):
    recips = [0.0] * 3
    for x in (1, 2):
        val = max(abs(mtx[x][1]), abs(mtx[x][2]))
        if val < 2.220446049250313e-16:
            return True
        recips[x] = 1.0 / val
    max_index = 0
    for i in (1, 2):
        for x in range(1, i):
            tmp = mtx[x][i]
            for y in range(1, x):
                tmp -= mtx[x][y] * mtx[y][i]
            mtx[x][i] = tmp
        val = 0.0
        for x in range(i, 3):
            tmp = mtx[x][i]
            for y in range(1, i):
                tmp -= mtx[x][y] * mtx[y][i]
            mtx[x][i] = tmp
            tmp = abs(tmp) * recips[x]
            if tmp >= val:
                val = tmp
                max_index = x
        if max_index != i:
            for y in (1, 2):
                mtx[max_index][y], mtx[i][y] = mtx[i][y], mtx[max_index][y]
            recips[max_index] = recips[i]
        idx[i] = max_index
        if mtx[i][i] == 0.0:
            return True
        if i != 2:
            tmp = 1.0 / mtx[i][i]
            for x in range(i + 1, 3):
                mtx[x][i] *= tmp
    lo, hi = 1.0e10, 0.0
    for i in (1, 2):
        tmp = abs(mtx[i][i])
        lo = min(lo, tmp); hi = max(hi, tmp)
    return lo / hi < 1.0e-10


def _bidirectional_filter(mtx, idx, vec):
    x = 0
    for i in (1, 2):
        index = idx[i]
        tmp = vec[index]
        vec[index] = vec[i]
        if x != 0:
            for y in range(x, i):
                tmp -= vec[y] * mtx[i][y]
        elif tmp != 0.0:
            x = i
        vec[i] = tmp
    for i in (2, 1):
        tmp = vec[i]
        for y in range(i + 1, 3):
            tmp -= vec[y] * mtx[i][y]
        vec[i] = tmp / mtx[i][i]
    vec[0] = 1.0


def _quadratic_merge(vec):
    v2 = vec[2]
    tmp = 1.0 - v2 * v2
    if tmp == 0.0:
        return True
    v0 = (vec[0] - v2 * v2) / tmp
    v1 = (vec[1] - vec[1] * v2) / tmp
    vec[0], vec[1] = v0, v1
    return abs(v1) > 1.0


def _finish_record(src, out):
    for z in (1, 2):
        if src[z] >= 1.0: src[z] = 0.9999999999
        elif src[z] <= -1.0: src[z] = -0.9999999999
    out[0] = 1.0
    out[1] = src[2] * src[1] + src[1]
    out[2] = src[2]


def _matrix_filter(src, dst):
    mtx = [[0.0] * 3 for _ in range(3)]
    mtx[2][0] = 1.0
    for i in (1, 2):
        mtx[2][i] = -src[i]
    for i in (2, 1):
        val = 1.0 - mtx[i][i] * mtx[i][i]
        for y in range(1, i + 1):
            mtx[i - 1][y] = (mtx[i][i] * mtx[i][y] + mtx[i][y]) / val
    dst[0] = 1.0
    for i in (1, 2):
        dst[i] = 0.0
        for y in range(1, i + 1):
            dst[i] += mtx[i][y] * dst[i - y]


def _merge_finish_record(src, dst):
    tmp = [0.0] * 3
    val = src[0]
    dst[0] = 1.0
    for i in (1, 2):
        v2 = 0.0
        for y in range(1, i):
            v2 += dst[y] * src[i - y]
        dst[i] = -(v2 + src[i]) / val if val > 0.0 else 0.0
        tmp[i] = dst[i]
        for y in range(1, i):
            dst[y] += dst[i] * dst[i - y]
        val *= 1.0 - dst[i] * dst[i]
    _finish_record(tmp, dst)


def _contrast(s1, s2):
    val = (s2[2] * s2[1] + -s2[1]) / (1.0 - s2[2] * s2[2])
    val1 = s1[0] * s1[0] + s1[1] * s1[1] + s1[2] * s1[2]
    val2 = s1[0] * s1[1] + s1[1] * s1[2]
    val3 = s1[0] * s1[2]
    return val1 + 2.0 * val * val2 + 2.0 * (-s2[1] * val + -s2[2]) * val3


def _filter_records(best, exp, records):
    for _ in range(2):
        counts = [0] * exp
        sums = [[0.0, 0.0, 0.0] for _ in range(exp)]
        tmp = [0.0, 0.0, 0.0]
        for rec in records:
            index, value = 0, 1.0e30
            for i in range(exp):
                t = _contrast(best[i], rec)
                if t < value:
                    value, index = t, i
            counts[index] += 1
            _matrix_filter(rec, tmp)
            for i in range(3):
                sums[index][i] += tmp[i]
        for i in range(exp):
            if counts[i] > 0:
                for y in range(3):
                    sums[i][y] /= counts[i]
        for i in range(exp):
            _merge_finish_record(sums[i], best[i])


def correlate_coefs(pcm):
    """Return 8 (c1, c2) signed 16-bit coefficient pairs for one channel of s16 samples."""
    samples = len(pcm)
    records = []
    # 28-sample window: previous 14 then current 14 (negative offsets reach the previous block).
    window = [0] * 28
    block = 0x3800
    pos = 0
    while pos < samples:
        chunk = list(pcm[pos:pos + block]); pos += block
        frame_samples = len(chunk)
        if frame_samples < block:
            chunk += [0] * min(14, block - frame_samples)
        i = 0
        while i < frame_samples:
            window[:14] = window[14:]
            part = chunk[i:i + 14]
            window[14:] = part + [0] * (14 - len(part))
            i += 14
            vec = _inner_product(window, 14)
            if abs(vec[0]) > 10.0:
                mtx = _outer_product(window, 14)
                idx = [0, 0, 0]
                if not _analyze_ranges(mtx, idx):
                    _bidirectional_filter(mtx, idx, vec)
                    if not _quadratic_merge(vec):
                        rec = [0.0, 0.0, 0.0]
                        _finish_record(vec, rec)
                        records.append(rec)
    best = [[0.0, 0.0, 0.0] for _ in range(8)]
    vec = [1.0, 0.0, 0.0]
    tmp = [0.0, 0.0, 0.0]
    for rec in records:
        _matrix_filter(rec, tmp)
        for y in (1, 2):
            vec[y] += tmp[y]
    if records:
        for y in (1, 2):
            vec[y] /= len(records)
    _merge_finish_record(vec, best[0])
    exp = 1
    for w in range(3):
        for i in range(exp):
            best[exp + i] = [0.01 * d + b for d, b in zip((0.0, -1.0, 0.0), best[i])]
        exp = 1 << (w + 1)
        _filter_records(best, exp, records)
    coefs = []
    for z in range(8):
        pair = []
        for k in (1, 2):
            d = -best[z][k] * 2048.0
            if d > 0.0: pair.append(32767 if d > 32767.0 else int(math.floor(d + 0.5)))
            else: pair.append(-32768 if d < -32768.0 else -int(math.floor(-d + 0.5)))
        coefs.append(tuple(pair))
    return coefs


# --- frames -----------------------------------------------------------------------------------

def encode_frame(hist, samples, coefs):
    """Encode up to 14 samples. hist = [yn2, yn1] decoded history; returns (8 bytes, new hist)."""
    count = len(samples)
    pcm = [hist[0], hist[1]] + list(samples)
    best = None
    for ci in range(8):
        c1, c2 = coefs[ci]
        distance = 0
        for s in range(count):
            v2 = pcm[s + 2] - _tdiv(pcm[s] * c2 + pcm[s + 1] * c1, 2048)
            v3 = _clamp16(v2)
            if abs(v3) > abs(distance):
                distance = v3
        scale = 0
        while scale <= 12 and (distance > 7 or distance < -8):
            scale += 1; distance = _tdiv(distance, 2)
        scale = -1 if scale <= 1 else scale - 2
        while True:
            scale += 1
            err = 0.0; index = 0
            out = [0] * 14
            rec = [pcm[0], pcm[1]] + [0] * count
            div = float(1 << scale) * 2048.0
            for s in range(count):
                v1 = rec[s] * c2 + rec[s + 1] * c1
                v2 = (pcm[s + 2] << 11) - v1
                v3 = int(v2 / div + _HALF) if v2 > 0 else int(v2 / div - _HALF)
                if v3 < -8:
                    if index < -8 - v3: index = -8 - v3
                    v3 = -8
                elif v3 > 7:
                    if index < v3 - 7: index = v3 - 7
                    v3 = 7
                out[s] = v3
                v = _clamp16((v1 + ((v3 * (1 << scale)) << 11) + 1024) >> 11)
                rec[s + 2] = v
                d = pcm[s + 2] - v
                err += d * d
            x = index + 8
            while x > 256:
                scale += 1
                if scale >= 12: scale = 11
                x >>= 1
            if not (scale < 12 and index > 1):
                break
        if best is None or err < best[0]:
            best = (err, ci, scale, out, rec)
    _, ci, scale, out, rec = best
    frame = bytearray(8)
    frame[0] = (ci << 4) | (scale & 0xF)
    for y in range(7):
        frame[y + 1] = ((out[2 * y] & 0xF) << 4) | (out[2 * y + 1] & 0xF)
    return bytes(frame), [rec[count], rec[count + 1]]


def encode(pcm, coefs=None, progress=None, cancel=None):
    """Encode one channel. Returns (adpcm bytes, coefs, first header byte)."""
    if coefs is None:
        coefs = correlate_coefs(pcm)
    out = bytearray()
    hist = [0, 0]
    total = len(pcm)
    for start in range(0, total, 14):
        frame, hist = encode_frame(hist, pcm[start:start + 14], coefs)
        out += frame
        if start % (14 * 4096) == 0:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("DSP encoding cancelled")
            if progress: progress(start, total)
    return bytes(out), coefs


def decode(adpcm, coefs, samples, hist=(0, 0)):
    """Decode one channel of `samples` samples; hist = (yn1, yn2)."""
    out = []
    yn1, yn2 = hist
    for f in range(frames_for(samples)):
        base = f * 8
        header = adpcm[base]
        c1, c2 = coefs[header >> 4]
        scale = 1 << (header & 0xF)
        for i in range(14):
            if len(out) == samples: break
            byte = adpcm[base + 1 + i // 2]
            nib = (byte >> 4) if i % 2 == 0 else (byte & 0xF)
            if nib >= 8: nib -= 16
            v = _clamp16(((nib * scale) << 11) + c1 * yn1 + c2 * yn2 + 1024 >> 11)
            yn2, yn1 = yn1, v
            out.append(v)
    return out


def context(coefs, first_header, yn1=0, yn2=0):
    """46-byte DSP ADPCM context as stored in .dsp headers and old Wii Wwise `WiiH`: 16 coefs,
    gain, predictor/scale, history, then the loop predictor/scale and loop history."""
    flat = [c for pair in coefs for c in pair]
    return struct.pack(">16h7H", *flat, 0, first_header, yn1 & 0xFFFF, yn2 & 0xFFFF, first_header, 0, 0)


def parse_context(raw):
    values = struct.unpack(">16h", raw[:32])
    gain, ps, yn1, yn2, lps, lyn1, lyn2 = struct.unpack(">7H", raw[32:46])
    to_s = lambda v: v - 0x10000 if v >= 0x8000 else v
    return {"coefs": [(values[2 * i], values[2 * i + 1]) for i in range(8)], "gain": gain, "ps": ps,
            "yn1": to_s(yn1), "yn2": to_s(yn2), "loop_ps": lps, "loop_yn1": to_s(lyn1), "loop_yn2": to_s(lyn2)}
