"""Texture format identity: RGB565 round-trips and new CMPR records never inherit a template's format."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
import nlg_texture


def ramp_header(key, width, height, enum, offset=0):
    """Retail-style record: channel depths at +12..+15 and the runtime format enum at +16."""
    rec = bytearray(96)
    struct.pack_into(">IHH", rec, 0, key, width, height)
    rec[10] = rec[11] = 1
    rec[12:16] = bytes((5, 6, 5, 0))
    struct.pack_into(">II", rec, 16, enum, offset)
    return bytes(rec)


class TextureFormatTests(unittest.TestCase):
    def test_rgb565_reencode_reproduces_the_stored_bytes(self):
        data = b"".join(struct.pack(">H", (i * 2654435761) & 0xFFFF) for i in range(128 * 4))
        self.assertEqual(len(data), nlg_texture.texture_size(0x7, 128, 4))
        rgba = nlg_texture.decode_rgb565(data, 128, 4)
        self.assertEqual(nlg_texture.encode_rgb565(rgba, 128, 4), data)

    def test_entry_sizes_follow_the_record_format(self):
        e = nlg_texture._entry(ramp_header(1, 128, 4, 0), 0, None)
        self.assertEqual(nlg_texture.FORMATS[e.fmt], "RGB565")
        self.assertEqual((e.size, e.chain_size), (1024, 1024))

    def test_new_cmpr_texture_does_not_inherit_rgb565_template(self):
        from helpers import archive_bytes
        from nlg_pack import Archive
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dd, da = archive_bytes([(0xB601, ramp_header(1, 128, 4, 0)), (0xB603, bytes(1024))])
            path = Path(tmp) / "a.dict"; path.write_bytes(dd); path.with_suffix(".data").write_bytes(da)
            a = Archive(str(path))
            nlg_texture.add_texture_rgba(a, "test/new", bytes([200, 100, 50, 255]) * 64, 8, 8, template_index=0)
            new = nlg_texture.list_all_textures(a)[-1]
            self.assertEqual((nlg_texture.FORMATS[new.fmt], new.format_enum), ("CMPR", 2))
            self.assertEqual(len(nlg_texture.decode_texture(a, new)), 8 * 8 * 4)


if __name__ == "__main__":
    unittest.main()
