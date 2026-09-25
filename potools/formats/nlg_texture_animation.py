"""Read-only B602 IFL texture sequences; unknown header fields remain opaque."""
from dataclasses import dataclass
import math
import struct


@dataclass(frozen=True)
class Sequence:
    hash: int
    frames: tuple

    def texture_at(self, seconds):
        duration = sum(t for _, t in self.frames)
        time = max(0.0, seconds) % duration
        for texture, hold in self.frames:
            if time < hold - 1e-7:
                return texture
            time -= hold
        return self.frames[0][0]


def sequences(archive):
    result = {}
    for i, chunk in enumerate(archive.chunks):
        if chunk[2] != 0xB602:
            continue
        raw = archive.get_chunk_bytes(i)
        offset = 0
        while offset < len(raw):
            if len(raw) - offset < 36:
                raise ValueError("Truncated B602 sequence header")
            magic, name, count = struct.unpack_from(">III", raw, offset)
            if magic != 0x5F6C6669 or not count or count > (len(raw)-offset-36)//8:
                raise ValueError("Unsupported B602 sequence")
            frames = tuple(struct.unpack_from(">If", raw, offset+36+8*j) for j in range(count))
            if any(not math.isfinite(t) or t <= 0 for _, t in frames):
                raise ValueError("Invalid B602 frame duration")
            if name in result:
                raise ValueError("Duplicate B602 alias")
            result[name] = Sequence(name, frames)
            offset += 36 + 8*count
    return result
