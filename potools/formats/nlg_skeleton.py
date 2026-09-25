"""
nlg_skeleton.py — Punch-Out!! Wii skeleton (BoneData 0xB00A) reader/editor.

BoneData = 68 bytes/bone: u32 nameHash + 4x4 f32 BE WORLD bind matrix (row-major, translation
in row 3). Skinning uses v' = W_anim * W_bind^-1 * v, so editing bind matrices reshapes the
character without touching geometry:
  - scale_bone(name, s): scales the rotation part of the bind by 1/s -> the skin bound to that
    bone renders s x bigger around the bone origin (bighead mods, giant fists, ...).
  - move_bone(name, dx,dy,dz): shifts the bind origin -> shifts that body part.
  - set_matrix(name, 16 floats): raw write (full custom skeletons from Blender: paste each
    edit-bone's world matrix; node count/order/hierarchy must stay the same — anims are
    node-indexed).
All edits in-place (fixed 68B records, no resize).

CLI:
  python nlg_skeleton.py list  <char.dict>
  python nlg_skeleton.py scale <char.dict> <bone-substring> <factor>   # e.g. head 1.5
  python nlg_skeleton.py move  <char.dict> <bone-substring> dx dy dz
Writes back to the same .dict/.data.
"""
import sys, os, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nlg_pack import Archive
import nlg_hash


class Skeleton:
    def __init__(self, dict_path, hashbin=None):
        self.a = Archive(dict_path)
        self.path = dict_path
        cands = [hashbin,
                 os.path.join(os.path.dirname(dict_path), '..', 'hashid.bin'),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'art', 'hashid.bin')]
        hb = next((c for c in cands if c and os.path.exists(c)), None)
        self.hn = nlg_hash.load_hashid_bin(hb) if hb else {}
        self.ri = self.a.find_chunks(type_id=0xB00A)[0]     # first model's bones
        self.data = bytearray(self.a.get_chunk_bytes(self.ri))
        self.n = len(self.data) // 68

    def name(self, i):
        h = struct.unpack_from('>I', self.data, i * 68)[0]
        return self.hn.get(h, f'#{h:08x}')

    def matrix(self, i):
        return list(struct.unpack_from('>16f', self.data, i * 68 + 4))

    def set_matrix(self, i, m):
        struct.pack_into('>16f', self.data, i * 68 + 4, *m)

    def find(self, sub):
        hits = [i for i in range(self.n) if sub.lower() in self.name(i).lower()]
        if not hits:
            raise KeyError(f"no bone matching '{sub}'")
        return hits

    def scale_bone(self, sub, s):
        """Render skin bound to bone(s) s x bigger: bind rotation part *= 1/s."""
        for i in self.find(sub):
            m = self.matrix(i)
            for r in range(3):              # rows 0-2 = rotation*scale
                for c in range(3):
                    m[r * 4 + c] /= s
            self.set_matrix(i, m)

    def move_bone(self, sub, dx, dy, dz):
        for i in self.find(sub):
            m = self.matrix(i)
            m[12] += dx; m[13] += dy; m[14] += dz   # row 3 = translation
            self.set_matrix(i, m)

    def save(self, out=None):
        self.a.replace_chunk(self.ri, bytes(self.data))
        self.a.write(out or self.path)


def main():
    if len(sys.argv) < 3:
        print(__doc__); return
    cmd, path = sys.argv[1], sys.argv[2]
    S = Skeleton(path)
    if cmd == 'list':
        for i in range(S.n):
            m = S.matrix(i)
            print(f"{i:3d} {S.name(i):28s} pos=({m[12]:+.3f},{m[13]:+.3f},{m[14]:+.3f})")
    elif cmd == 'scale':
        sub, s = sys.argv[3], float(sys.argv[4])
        S.scale_bone(sub, s)
        S.save(); print(f"scaled {[S.name(i) for i in S.find(sub)]} x{s}, saved")
    elif cmd == 'move':
        sub = sys.argv[3]; d = [float(x) for x in sys.argv[4:7]]
        S.move_bone(sub, *d)
        S.save(); print(f"moved {[S.name(i) for i in S.find(sub)]} by {d}, saved")
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
