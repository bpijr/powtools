"""Wii DSP-ADPCM encode/decode. Synthetic samples only."""
import math
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent)); import paths  # noqa: E402
import nlg_dsp


def representative_pcm(count=56):
    return [int(16000 * math.sin(i * 0.2)) for i in range(count)]


class DspCodecTests(unittest.TestCase):
    def test_encode_decode_roundtrip_length(self):
        pcm = representative_pcm(56)
        result = nlg_dsp.encode(pcm)
        self.assertGreaterEqual(len(result), 2)
        adpcm, coefs = result[0], result[1]
        self.assertIsInstance(adpcm, (bytes, bytearray))
        self.assertEqual(len(adpcm) % nlg_dsp.BYTES_PER_FRAME, 0)
        self.assertEqual(len(adpcm), nlg_dsp.bytes_for(len(pcm)))
        decoded = nlg_dsp.decode(adpcm, coefs, len(pcm))
        self.assertIsInstance(decoded, (list, tuple))
        self.assertEqual(len(decoded), len(pcm))
        self.assertTrue(decoded)
        self.assertTrue(all(type(s) is int for s in decoded))
        self.assertTrue(all(-32768 <= s <= 32767 for s in decoded))
        self.assertEqual(len(nlg_dsp.context(coefs, adpcm[0])), 46)
        parsed = nlg_dsp.parse_context(nlg_dsp.context(coefs, adpcm[0]))
        self.assertEqual(parsed["coefs"], list(coefs))


if __name__ == "__main__":
    unittest.main()
