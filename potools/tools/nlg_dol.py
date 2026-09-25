"""
nlg_dol.py — main.dol (Wii PowerPC / Gekko) static analysis toolkit for Punch-Out!! Wii.

Built to locate the animation rotation-track decode function. Handles what stock Capstone
can't: it recovers SDA bases (r2/r13), tracks lis+addi/ori register values to resolve
small-data and pointer loads, and recognizes Gekko paired-single ops (ps_*, psq_l/st) that
Capstone silently skips (which is why a naive linear sweep desyncs in this engine).

Addresses assume PAL R7PP01 revision 0, main.dol SHA-256
6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137. No NTSC image was compared.
The following are static-analysis notes, not validated runtime observations.

Findings (PAL main.dol, R7PP01):
  - Sections: main text 0x80007280..0x80311520 (file off 0x25e0). entry 0x80006124.
  - SDA: _SDA2_BASE_ (r2)=0x8041e2c0  _SDA_BASE_ (r13)=0x80419260
    (__init_registers starts at 0x80006290; 0x80006304 is inside it).
  - 32767.0f constant at 0x80417d58 / 0x80418170 (data7 = .sdata2). Reached as lfs -0x6568(r2) etc.
  - Animation math cluster ~0x80184000..0x80186000; VERY paired-single heavy (3316 psq_l/st total).
  - Virtual sampler at 0x8018597c (no bl callers -> vtable method, i.e. AnimControllerNode-style):
    uses 32767 for a frame-index->weight LERP and CONDITIONALLY NEGATES vec components
    (branchy: store f0,-f1,f2  vs  f0,f1,-f2) = quaternion sign handling.
  - Helper at 0x80185aa0 (2 bl callers: 0x80181df0, 0x8018374c) accumulates into node slots.
  - Correction: the old "GQR table at 0x8041e5bc" calculation was wrong.
    DTK identifies 0x801c8d58 as TRKRestoreExtended1Block; its first lis/ori pair reloads
    r2 with gTRKCPUState=0x803acca8. Thus lmw 0x2fc(r2) at 0x801c8d94 reads 0x803acfa4
    before restoring GQR0-7 at 0x801c8d98. This is saved debugger context, not proof of
    animation quantization initialization. Actual runtime GQR settings were not checked
    here. The old component-subset interpretation is superseded by nlg_anim2.py.

Usage: python nlg_dol.py <path/to/main.dol>
"""
import struct, sys
try: from capstone import Cs, CS_ARCH_PPC, CS_MODE_BIG_ENDIAN, CS_MODE_32
except ImportError: Cs=None

class Dol:
    def __init__(self, path):
        self.d=open(path,'rb').read()
        u=lambda o: struct.unpack_from('>I',self.d,o)[0]
        self.segs=[]  # (name, fileoff, addr, size)
        for i in range(7):
            o,a,s=u(4*i),u(0x48+4*i),u(0x90+4*i)
            if s: self.segs.append(('text%d'%i,o,a,s))
        for i in range(11):
            o,a,s=u(0x1c+4*i),u(0x64+4*i),u(0xac+4*i)
            if s: self.segs.append(('data%d'%i,o,a,s))
        self.bss_addr=u(0xd8); self.bss_size=u(0xdc); self.entry=u(0xe0)
        self.sda2=self.sda=None
    def read(self, addr, n):
        for nm,o,a,s in self.segs:
            if a<=addr<a+s: return self.d[o+addr-a:o+addr-a+n]
        return None
    def word(self, addr):
        b=self.read(addr,4); return struct.unpack('>I',b)[0] if b else None
    def resolve_sda(self):
        # __init_registers: lis/ori into r2 (_SDA2_BASE_) and r13 (_SDA_BASE_)
        for nm,o,a,s in self.segs:
            if not nm.startswith('text'): continue
            reg={}; addr=a
            for off in range(0,s,4):
                w=struct.unpack_from('>I',self.d,o+off)[0]; op=w>>26
                rD=(w>>21)&31; rA=(w>>16)&31; imm=w&0xffff
                if op==15: reg[rD]=((reg.get(rA,0) if rA else 0)+(imm<<16))&0xffffffff
                elif op==14 and rA in reg: reg[rD]=(reg[rA]+((imm-0x10000 if imm>=0x8000 else imm)))&0xffffffff
                elif op==24 and rA in reg: reg[rD]=reg[rA]|imm
                if op==19 and 2 in reg and 13 in reg:  # blr ending __init_registers
                    self.sda2, self.sda=reg[2],reg[13]; return self.sda2,self.sda
        return None,None
    def find_const_loads(self, const_addrs):
        """Every lfs/lfd/psq_l whose effective address hits one of const_addrs (tracks lis+addi)."""
        if self.sda2 is None: self.resolve_sda()
        s16=lambda v:(v&0xffff)-0x10000 if (v&0xffff)>=0x8000 else (v&0xffff)
        s12=lambda v:(v&0xfff)-0x1000 if (v&0xfff)>=0x800 else (v&0xfff)
        C=set(const_addrs); hits=[]
        for nm,o,a,s in self.segs:
            if not nm.startswith('text'): continue
            reg={2:self.sda2,13:self.sda}
            for off in range(0,s,4):
                w=struct.unpack_from('>I',self.d,o+off)[0]; ad=a+off
                op=w>>26; rD=(w>>21)&31; rA=(w>>16)&31; imm=w&0xffff
                if op==15:
                    if rA==0: reg[rD]=(imm<<16)&0xffffffff
                    elif rA in reg: reg[rD]=(reg[rA]+(imm<<16))&0xffffffff
                    else: reg.pop(rD,None)
                elif op==14:
                    if rA==0: reg[rD]=s16(imm)&0xffffffff
                    elif rA in reg: reg[rD]=(reg[rA]+s16(imm))&0xffffffff
                    else: reg.pop(rD,None)
                elif op==24 and rA in reg: reg[rD]=reg[rA]|imm
                elif op in (48,49,50) and rA in reg:
                    if (reg[rA]+s16(imm))&0xffffffff in C: hits.append((ad,'lfs/lfd',rA,s16(imm)))
                elif op==56 and rA in reg:
                    if (reg[rA]+s12(w&0xfff))&0xffffffff in C: hits.append((ad,'psq_l',rA,s12(w&0xfff)))
                if op in (16,18,19): reg={2:self.sda2,13:self.sda}
        return hits

if __name__=='__main__':
    dp=sys.argv[1] if len(sys.argv)>1 else '../DATA/sys/main.dol'
    d=Dol(dp); d.resolve_sda()
    print(f"entry={d.entry:#x}  _SDA2_(r2)={d.sda2:#x}  _SDA_(r13)={d.sda:#x}")
    print("32767.0f loads:", [f"{a:#x} {m} r{r}{o:+#x}" for a,m,r,o in d.find_const_loads([0x80417d58,0x80418170])])
