"""
nlg_material.py — Punch-Out!! Wii MaterialData (0xB016) decoder/editor.

MATERIAL STRUCT — 204 (0xCC) bytes per material, all boxer meshes use the 'hippodiffuseskin'
shader (mesh record matHash@16 = shader name hash; materialOffset@36 = byte offset into 0xB016).

  +0x00  8 x { u32 textureHash, u32 0xFFFF0000 }   texture slots, fixed meaning:
     slot0 = DETAIL/ALBEDO overlay (skin_shadow / white / part detail)  <- multiplied w/ ramp
     slot1 = DAMAGE/MARK   (white=none, mark2/mark3, gj_eye_damage, gj_welt)
     slot2 = SPECULAR MASK (…_spec / darkgray / black)
     slot3 = DIFFUSE RAMP  (gj_skin, gj_shorts, … — the gradient that drives base color)
     slot4 = RIMLIGHT RAMP (rimlightramp / rimlightramp_thin)
     slot5 = HDR/BLOOM     (hdr_02, hdr_03, … or global/black = no bloom)   *** the "HDR" ***
     slot6 = FRESNEL RAMP  (fresnel1..5 or global/black = none)
     slot7 = SPEC RAMP     (global/specramp)
  +0x40  15 x u32 zeros (unused/runtime)
  +0x7C  u32  flag (1)
  +0x80  u32  0
  +0x84  f32  SPEC POWER (32.0 typical)
  +0x88  2 x u32 0
  +0x90  3 x u32 flags (1,1,1)
  +0x9C  f32[3] TINT RGB (multiplies the ramp — GJ skin = 0.337,0.176,0.180)
  +0xA8  f32  alpha (1.0)
  +0xAC  2 x u32 0
  +0xB4  f32[3] COLOR2 RGB (1,1,1 = neutral)
  +0xC0  3 x u32 0

Textures may be per-character (block-2 of the same archive) or GLOBAL (global/black
0x713033FC, global/specramp 0x2D53BCBA, global/white etc. — always resident, safe to reference).

WORKFLOW (materials "any way you want"):
  python nlg_material.py list  <char.dict>                # every mesh -> slots + params
  python nlg_material.py set   <char.dict> <mesh#|mat@hex> slot<N> <texname>   # retexture a slot
  python nlg_material.py tint  <char.dict> <mesh#> R G B  # recolor via tint (no texture edit!)
  python nlg_material.py spec  <char.dict> <mesh#> <power>
  python nlg_material.py addtex <char.dict> <name> <x.png> [mesh# slot<N>]   # NEW texture
  python nlg_material.py nohdr <char.dict> [mesh#]        # slot5+6 -> global/black (kill bloom),
                                                          #   all meshes if no mesh# given
All edits are in-place (fixed 204B struct -> no resize; nlg_pack round-trips byte-identical).
"""
import sys, os, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nlg_pack import Archive
import nlg_hash

MAT_SIZE = 204
SLOT_NAMES = ['detail', 'damage', 'specmask', 'ramp', 'rimramp', 'hdr', 'fresnel', 'specramp']
GLOBAL_BLACK = 0x713033FC
GLOBAL_SPECRAMP = 0x2D53BCBA


def _hashes(hn):
    """name -> hash reverse map (lazy)."""
    return {v: k for k, v in hn.items()}


class Materials:
    def __init__(self, dict_path, hashbin='../art/hashid.bin'):
        self.a = Archive(dict_path)
        self.path = dict_path
        cands = [hashbin,
                 os.path.join(os.path.dirname(dict_path), '..', 'hashid.bin'),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'art', 'hashid.bin')]
        hb = next((c for c in cands if c and os.path.exists(c)), None)
        self.hn = nlg_hash.load_hashid_bin(hb) if hb else {}
        self.rev = _hashes(self.hn)
        self.mat_ri = self.a.find_chunks(type_id=0xB016)[0]
        self.mesh_ri = self.a.find_chunks(type_id=0xB004)[0]
        # This compatibility authoring class is skin-only; generic layouts use audited patches.
        import nlg_material_layout
        records = [r for r in nlg_material_layout.records(self.a) if r.chunk == self.mat_ri]
        if not records or any(r.shader != nlg_material_layout.SKIN or r.error for r in records):
            raise ValueError("Legacy material authoring requires an owned, verified hippodiffuseskin layout. Use the generic material inspector for other shaders.")
        self.mat = bytearray(self.a.get_chunk_bytes(self.mat_ri))
        md = self.a.get_chunk_bytes(self.mesh_ri)
        self.meshes = []           # (meshIdx, shaderHash, meshHash, matOffset)
        for m in range(len(md) // 52):
            o = m * 52
            self.meshes.append((m,
                                struct.unpack_from('>I', md, o + 16)[0],
                                struct.unpack_from('>I', md, o + 20)[0],
                                struct.unpack_from('>I', md, o + 36)[0]))
        # local texture hash set (for warnings)
        th = self.a.get_chunk_bytes(self.a.find_chunks(type_id=0xB601)[0])
        self.local_tex = {struct.unpack_from('>I', th, i * 96)[0] for i in range(len(th) // 96)}

    def name(self, h):
        return self.hn.get(h, f'#{h:08x}')

    # ---- authoring: build MaterialData from scratch -------------------------------------
    # Every boxer material is the same 'hippodiffuseskin' preset -- same 8 slots, same 204B
    # layout -- so a custom character does not need a new shader, it needs N of these records
    # with the right texture hashes in them. build_record() stamps one out from a real record
    # (so the ~15 still-unidentified u32s keep whatever the game expects), and write_all()
    # rebuilds the whole 0xB016 chunk and repoints every mesh's materialOffset at it.

    def template_record(self):
        """A known-good 204B record from this archive, used as the stamp for new ones."""
        return bytes(self.mat[0:MAT_SIZE])

    def build_record(self, slots, spec_power=32.0, tint=(1.0, 1.0, 1.0), alpha=1.0,
                     color2=(1.0, 1.0, 1.0), template=None):
        """One 204B material record. `slots` = 8 texture hashes (or names, or None for
        global/black). Unspecified/short lists are padded with global/black."""
        rec = bytearray(template or self.template_record())
        s = list(slots) + [None] * (8 - len(slots))
        for i, v in enumerate(s[:8]):
            if v is None:
                h = GLOBAL_SPECRAMP if i == 7 else GLOBAL_BLACK
            elif isinstance(v, str):
                h = self.find_hash(v)
            else:
                h = int(v)
            struct.pack_into('>I', rec, i * 8, h)
            struct.pack_into('>I', rec, i * 8 + 4, 0xFFFF0000)
        struct.pack_into('>f', rec, 0x84, float(spec_power))
        for i in range(3):
            struct.pack_into('>f', rec, 0x9C + 4 * i, float(tint[i]))
            struct.pack_into('>f', rec, 0xB4 + 4 * i, float(color2[i]))
        struct.pack_into('>f', rec, 0xA8, float(alpha))
        return bytes(rec)

    def write_all(self, records, mesh_to_record):
        """Replace 0xB016 with `records` (list of 204B blobs) and point each mesh at one.
        `mesh_to_record` maps mesh index -> index into `records`. Resize-safe."""
        if any(len(r) != MAT_SIZE for r in records):
            raise ValueError("every material record must be exactly 204 bytes")
        self.mat = bytearray(b''.join(records))
        md = bytearray(self.a.get_chunk_bytes(self.mesh_ri))
        for mi in range(len(md) // 52):
            ri = mesh_to_record.get(mi)
            if ri is None:
                continue
            if not 0 <= ri < len(records):
                raise IndexError(f"mesh {mi} -> record {ri}, only {len(records)} records")
            struct.pack_into('>I', md, mi * 52 + 36, ri * MAT_SIZE)
        self.a.replace_chunk(self.mesh_ri, bytes(md))
        self.a.replace_chunk(self.mat_ri, bytes(self.mat))
        md2 = self.a.get_chunk_bytes(self.mesh_ri)
        self.meshes = [(m,
                        struct.unpack_from('>I', md2, m * 52 + 16)[0],
                        struct.unpack_from('>I', md2, m * 52 + 20)[0],
                        struct.unpack_from('>I', md2, m * 52 + 36)[0])
                       for m in range(len(md2) // 52)]
        return len(records)

    def find_hash(self, texname):
        """Accept 'gj_skin', 'glassjoe/gj_skin', 'global/black', or hex '#xxxxxxxx'."""
        if texname.startswith('#'):
            return int(texname[1:], 16)
        if texname in self.rev:
            return self.rev[texname]
        # suffix match; prefer textures local to this archive, then global/
        cands = [h for h, n in self.hn.items() if n.endswith('/' + texname) or n == texname]
        local = [h for h in cands if h in self.local_tex]
        if len(local) == 1:
            return local[0]
        glob = [h for h in cands if self.hn[h].startswith('global/')]
        if len(glob) == 1:
            return glob[0]
        if len(cands) == 1:
            return cands[0]
        raise KeyError(f"texture '{texname}': {'ambiguous ' + str([self.name(c) for c in cands]) if cands else 'not found in hashid'}")

    # ---- struct accessors ----
    def slots(self, mo):
        return [struct.unpack_from('>I', self.mat, mo + i * 8)[0] for i in range(8)]

    def set_slot(self, mo, slot, h):
        if type(slot) is not int or not 0 <= slot < 8 or mo not in self.mat_offsets():
            raise ValueError("Choose an existing skin material and texture slot 0-7")
        struct.pack_into('>I', self.mat, mo + slot * 8, h)

    def get_f(self, mo, off):
        return struct.unpack_from('>f', self.mat, mo + off)[0]

    def set_f(self, mo, off, v):
        import math
        bounds = {0x84: 256.0, 0x9C: 1.0, 0xA0: 1.0, 0xA4: 1.0, 0xA8: 1.0}
        if mo not in self.mat_offsets() or off not in bounds:
            raise ValueError("Only proven skin scalar fields may be edited")
        if type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= bounds[off]:
            raise ValueError("Material value is outside the bounded editor range")
        struct.pack_into('>f', self.mat, mo + off, v)

    def mat_offsets(self, mesh=None):
        if mesh is None:
            return sorted({mo for _, _, _, mo in self.meshes})
        return [mo for m, _, _, mo in self.meshes if m == mesh]

    def describe(self, mo):
        s = self.slots(mo)
        tint = [self.get_f(mo, 0x9C + 4 * i) for i in range(3)]
        c2 = [self.get_f(mo, 0xB4 + 4 * i) for i in range(3)]
        return {'slots': {SLOT_NAMES[i]: self.name(s[i]) for i in range(8)},
                'spec_power': self.get_f(mo, 0x84),
                'tint': tint, 'color2': c2, 'alpha': self.get_f(mo, 0xA8)}

    # ---- ops ----
    def kill_hdr(self, mesh=None):
        n = 0
        for mo in (self.mat_offsets(mesh) if mesh is not None else self.mat_offsets()):
            self.set_slot(mo, 5, GLOBAL_BLACK)
            self.set_slot(mo, 6, GLOBAL_BLACK)
            n += 1
        return n

    def save(self, out=None):
        self.a.replace_chunk(self.mat_ri, bytes(self.mat))
        self.a.write(out or self.path)


def main():
    if len(sys.argv) < 3:
        print(__doc__); return
    cmd, path = sys.argv[1], sys.argv[2]
    M = Materials(path)
    if cmd == 'list':
        for m, sh, mh, mo in M.meshes:
            d = M.describe(mo)
            hdr = d['slots']['hdr']; fres = d['slots']['fresnel']
            glow = '' if 'black' in hdr else '  **HDR**'
            print(f"mesh{m:3d} {M.name(mh):32s} mat@{mo:#06x} ramp={d['slots']['ramp']:28s} "
                  f"detail={d['slots']['detail']:28s} dmg={d['slots']['damage']:20s} "
                  f"hdr={hdr:18s} fres={fres:18s} tint=({d['tint'][0]:.3f},{d['tint'][1]:.3f},{d['tint'][2]:.3f}) "
                  f"spec={d['spec_power']:.0f}{glow}")
    elif cmd == 'set':
        target, slotarg, tex = sys.argv[3], sys.argv[4], sys.argv[5]
        slot = int(slotarg.replace('slot', '')) if slotarg.replace('slot', '').isdigit() else SLOT_NAMES.index(slotarg)
        h = M.find_hash(tex)
        mos = [int(target[4:], 16)] if target.startswith('mat@') else M.mat_offsets(int(target))
        for mo in mos:
            M.set_slot(mo, slot, h)
            if h not in M.local_tex and not M.name(h).startswith('global/'):
                print(f"  warning: {M.name(h)} is not in this archive's textures and not global/")
        M.save(); print(f"set {SLOT_NAMES[slot]} -> {M.name(h)} on {len(mos)} material(s), saved")
    elif cmd == 'tint':
        m = int(sys.argv[3]); r, g, b = map(float, sys.argv[4:7])
        for mo in M.mat_offsets(m):
            for i, v in enumerate((r, g, b)):
                M.set_f(mo, 0x9C + 4 * i, v)
        M.save(); print("tint set, saved")
    elif cmd == 'spec':
        m = int(sys.argv[3]); p = float(sys.argv[4])
        for mo in M.mat_offsets(m):
            M.set_f(mo, 0x84, p)
        M.save(); print("spec power set, saved")
    elif cmd == 'addtex':
        # addtex <char.dict> <name> <png> [mesh# slot]  -- add a NEW texture, optionally bind it
        import nlg_texture
        name, png = sys.argv[3], sys.argv[4]
        h = nlg_texture.add_texture(M.a, name, png)
        if len(sys.argv) > 6:
            mesh, slotarg = int(sys.argv[5]), sys.argv[6]
            slot = int(slotarg.replace('slot', '')) if slotarg.replace('slot', '').isdigit() else SLOT_NAMES.index(slotarg)
            M.mat = bytearray(M.a.get_chunk_bytes(M.mat_ri))
            for mo in M.mat_offsets(mesh):
                M.set_slot(mo, slot, h)
        M.save()
        print(f"added {name} ({h:08X}) and saved. NOTE: a brand-new hash is not in hashid.bin -- "
              f"boot it in Dolphin before trusting it; repaint/slot-swap are the proven paths.")
    elif cmd == 'nohdr':
        mesh = int(sys.argv[3]) if len(sys.argv) > 3 else None
        n = M.kill_hdr(mesh)
        M.save(); print(f"HDR+fresnel -> global/black on {n} material(s), saved")
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
