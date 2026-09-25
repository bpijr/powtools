"""
dol_tool.py — lightweight static-analysis toolkit for Punch-Out!! Wii main.dol (PAL R7PP01).

Addresses assume main.dol SHA-256
6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137 (R7PP01 revision 0).
No NTSC equivalence established.
refs_to() and funcs() produce heuristic leads, not verified xrefs/function boundaries.

Facilities:
  - DOL section parsing, addr<->file mapping
  - PPC disassembler with Gekko paired-single support (capstone + manual ps/psq decode)
  - function boundary scan (mflr/blr heuristics)
  - immediate-halfword scanner (find code referencing chunk-type constants etc.)
  - string table + code-reference resolver (lis/addi pairs) for symbol recovery
  - CLI:  python3 dol_tool.py dis 0x80181acc 0x80181e60
          python3 dol_tool.py imm 0x5003
          python3 dol_tool.py strref AnimController
          python3 dol_tool.py funcs > symbols.txt

Known subsystem anchors:
  0x80181acc anim rotation sample+dispatch (0x7003 flag bits)
  0x801819b8 anim chunk-pointer setup (0x7101/02/03/12/15 -> obj+0x20/28/24/2c/30)
  0x80188b90/ba4/c0c rotation key loaders (16/12/8-bit quat)
  0x8018585c hinge angle sample (s16 wrapping angle * pi/32768)
  0x80185710 quat accumulate + runtime mirror negation table
"""
import struct, sys, bisect

DOL = None
SECS = []          # (foff, addr, size, name)

def load(path='DATA/sys/main.dol'):
    global DOL, SECS
    DOL = open(path, 'rb').read()
    toff = struct.unpack('>7I', DOL[0:28]);  doff = struct.unpack('>11I', DOL[28:72])
    taddr = struct.unpack('>7I', DOL[72:100]); daddr = struct.unpack('>11I', DOL[100:144])
    tsize = struct.unpack('>7I', DOL[144:172]); dsize = struct.unpack('>11I', DOL[172:216])
    SECS = ([(toff[i], taddr[i], tsize[i], f'T{i}') for i in range(7) if tsize[i]] +
            [(doff[i], daddr[i], dsize[i], f'D{i}') for i in range(11) if dsize[i]])

def a2o(addr):
    for off, a, sz, nm in SECS:
        if a <= addr < a + sz: return off + (addr - a)
    return None

def rd32(addr):
    o = a2o(addr)
    return struct.unpack('>I', DOL[o:o+4])[0] if o is not None else None

# ---- disassembly ----
_md = None
def _cap():
    global _md
    if _md is None:
        import capstone
        _md = capstone.Cs(capstone.CS_ARCH_PPC, capstone.CS_MODE_32 | capstone.CS_MODE_BIG_ENDIAN)
    return _md

def ps_dec(w):
    op = w >> 26
    if op in (56, 57, 60, 61):
        rd=(w>>21)&31; ra=(w>>16)&31; W=(w>>15)&1; g=(w>>12)&7; imm=w&0xfff
        if imm & 0x800: imm -= 0x1000
        return f'{ {56:"psq_l",57:"psq_lu",60:"psq_st",61:"psq_stu"}[op] } f{rd}, {imm}(r{ra}), W={W}, GQR{g}'
    if op == 4:
        rd=(w>>21)&31; ra=(w>>16)&31; rb=(w>>11)&31; rc=(w>>6)&31
        xo=(w>>1)&0x1f; xo10=(w>>1)&0x3ff
        n5={18:'ps_div',20:'ps_sub',21:'ps_add',23:'ps_sel',24:'ps_res',25:'ps_mul',
            26:'ps_rsqrte',28:'ps_msub',29:'ps_madd',30:'ps_nmsub',31:'ps_nmadd'}
        if xo in n5: return f'{n5[xo]} f{rd}, f{ra}, f{rb}, f{rc}'
        n10={40:'ps_neg',72:'ps_mr',136:'ps_nabs',264:'ps_abs',0:'ps_cmpu0',32:'ps_cmpo0',
             528:'ps_merge00',560:'ps_merge01',592:'ps_merge10',624:'ps_merge11',1014:'dcbz_l'}
        if xo10 in n10: return f'{n10[xo10]} f{rd}, f{ra}, f{rb}'
        return f'ps_op4 xo={xo10}'
    return None

def dis(a0, a1, out=sys.stdout):
    md = _cap()
    for a in range(a0, a1, 4):
        w = rd32(a)
        if w is None: out.write(f'{a:08x}  <unmapped>\n'); continue
        p = ps_dec(w)
        if p: out.write(f'{a:08x}  {p}\n'); continue
        ins = list(md.disasm(struct.pack('>I', w), a))
        out.write(f'{a:08x}  {ins[0].mnemonic:10s} {ins[0].op_str}\n' if ins
                  else f'{a:08x}  .word 0x{w:08x}\n')

# ---- function boundaries (text sections) ----
def funcs():
    """Return sorted list of probable function starts (stwu r1 / mflr prologues + bl targets)."""
    starts = set()
    for off, addr, size, nm in SECS:
        if not nm.startswith('T') or nm == 'T0': continue
        for i in range(0, size - 4, 4):
            w = struct.unpack('>I', DOL[off+i:off+i+4])[0]
            # stwu r1, -X(r1)
            if (w >> 16) & 0xFFFF == 0x9421: starts.add(addr + i)
            # bl targets
            if w >> 26 == 18 and not (w & 2):      # I-form b/bl
                li = w & 0x03FFFFFC
                if li & 0x02000000: li -= 0x04000000
                tgt = addr + i + li
                if w & 1: starts.add(tgt)
    return sorted(starts)

# ---- immediate scan ----
def imm_scan(value, ops=(0x2c,0x28,0x38,0x39,0x60,0x64,0x3c,0x2f,0x2b)):
    hits = []
    for off, addr, size, nm in SECS:
        if not nm.startswith('T'): continue
        for i in range(0, size, 4):
            ins = DOL[off+i:off+i+4]
            if len(ins) < 4: break
            if struct.unpack('>H', ins[2:4])[0] == value and ins[0] in ops:
                hits.append(addr + i)
    return hits

# ---- strings + refs ----
def strings(minlen=6):
    out = []
    for off, addr, size, nm in SECS:
        if nm.startswith('T'): continue
        run = bytearray(); start = None
        for i in range(size):
            c = DOL[off+i]
            if 32 <= c < 127:
                if start is None: start = addr + i
                run.append(c)
            else:
                if start is not None and len(run) >= minlen:
                    out.append((start, run.decode()))
                run = bytearray(); start = None
    return out

def refs_to(addr_target):
    """Find lis/addi (or lis/ori) pairs loading addr_target in text sections."""
    hi = (addr_target + 0x8000) >> 16
    lo = addr_target & 0xFFFF
    hits = []
    for off, addr, size, nm in SECS:
        if not nm.startswith('T'): continue
        for i in range(0, size, 4):
            w = struct.unpack('>I', DOL[off+i:off+i+4])[0]
            if w >> 26 == 15 and (w & 0xFFFF) == hi:          # lis / addis rX
                # scan a few instrs ahead for matching addi/ori/lwz with lo
                for j in range(4, 40, 4):
                    if off+i+j+4 > off+size: break
                    w2 = struct.unpack('>I', DOL[off+i+j:off+i+j+4])[0]
                    imm = w2 & 0xFFFF
                    s_lo = lo if lo < 0x8000 else lo - 0x10000
                    if imm == (s_lo & 0xFFFF) and (w2 >> 26) in (14, 24, 32, 34, 40, 48, 50):
                        hits.append(addr + i); break
    return hits

if __name__ == '__main__':
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    load(os.path.join(here, '..', '..', 'DATA', 'sys', 'main.dol'))
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'help'
    if cmd == 'dis':
        dis(int(sys.argv[2], 16), int(sys.argv[3], 16))
    elif cmd == 'imm':
        v = int(sys.argv[2], 16)
        for h in imm_scan(v): print(hex(h))
    elif cmd == 'funcs':
        for f in funcs(): print(f'{f:08x}')
    elif cmd == 'strref':
        pat = sys.argv[2]
        for a, s in strings():
            if pat in s:
                r = refs_to(a)
                print(f'{a:08x} "{s[:60]}" refs: {" ".join(hex(x) for x in r[:6])}')
    else:
        print(__doc__)
