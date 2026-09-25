import sys
import struct
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nlg_texture_animation import sequences


class Archive:
    chunks = [(0, 0, 0xB602)]
    def __init__(self, raw): self.raw = raw
    def get_chunk_bytes(self, index): return self.raw


def record(name=123):
    return struct.pack(">III", 0x5F6C6669, name, 2) + bytes(24) + struct.pack(">IfIf", 10, .1, 11, .2)


class TextureAnimationTests(unittest.TestCase):
    def test_don_flamenco_major_circuit_default(self):
        from nlg_cutscene import Cutscene
        cs = Cutscene.__new__(Cutscene)
        cs.path = Path('NIS/DonFlamenco2/pre_fight.dict')
        self.assertEqual(cs.arena(), 'majorcircuit')
        cs.path = Path('NIS/DonFlamenco/pre_fight.dict')
        self.assertEqual(cs.arena(), 'majorcircuit')

    def test_duration_boundaries_and_loop(self):
        seq = sequences(Archive(record()))[123]
        self.assertEqual([seq.texture_at(t) for t in (0, .099, .1, .299, .3, .4)], [10, 10, 11, 11, 10, 11])

    def test_concatenated_records(self):
        self.assertEqual(set(sequences(Archive(record()+record(456)))), {123,456})

    def test_invalid_records(self):
        for raw in (record()[:-1], record()+b'bad', record()+record(),
                    bytes(4)+record()[4:], record()[:-4]+struct.pack('>f',float('nan'))):
            with self.assertRaises(ValueError): sequences(Archive(raw))
