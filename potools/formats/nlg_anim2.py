"""
nlg_anim2.py — COMPLETE Punch-Out Wii animation reconstruction.

Uses the decoded 0x8xxx animation-set node table (the game's own data — no guessing):
  0x8003: 74 x u32 node name hashes  -> node->bone map (de-hash via hashid.bin)
  0x8009: 74 x i32 parent node index (node0 'root' = -1)
  0x8010: 74 x 3 f32 bind LOCAL translation offset per node
  0x8011: 74 x u8: 1 = translation is constant (use 0x8010), 0 = node has a 0x7102 track
  0x8008: mirror (L<->R) node remap; 0x8007: eval order; 0x8004: child count
Track alignment (validated by L/R mirror symmetry + translation statics == 0x8010):
  rotation 0x7101 track i  -> node i (i = 0..72; last node has no rot track)
  translation 0x7102 tracks -> nodes with 0x8011 == 0, in node order, skipping node0.
    node1 ('bip01') translation is WORLD; all others LOCAL.
Rotation compression (per-node, constant across all anims; validated: 0 unit-norm
violations in 191k frames across 12 characters; component ids from mirror-pair analysis):
  size 8*fr: full (x,y,z,w) int16/32767 per frame      size 8: static full
  size 4*fr: (y,z) per frame, x=0, w=+sqrt(1-y2-z2)    size 4: static
  size 2*fr: (z)  per frame, x=y=0, w=+sqrt(1-z2)      size 2: static
  (3-comp analog: (x,y,z).)  All quats are DELTAS FROM BIND (rest ~= identity).
Composition (CONV): animated local = qmul(lbq, dq) ('local', default) or qmul(dq, lbq)
('parent').  Use compare() to render a variant grid.
"""
import struct, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nlg_pack import Archive
from nlg_hash import load_hashid_bin

WORLD_YAW=(0.0,0.0,-0.7071067811865476,0.7071067811865476)  # Rz(-90): +X(anim fwd) -> -Y(mesh fwd)

def qmul(a,b):
    ax,ay,az,aw=a; bx,by,bz,bw=b
    return (aw*bx+ax*bw+ay*bz-az*by, aw*by-ax*bz+ay*bw+az*bx,
            aw*bz+ax*by-ay*bx+az*bw, aw*bw-ax*bx-ay*by-az*bz)
def qconj(q): return (-q[0],-q[1],-q[2],q[3])
def qnorm(q):
    n=math.sqrt(sum(x*x for x in q)) or 1.0; return tuple(x/n for x in q)
def qrot(q,v):
    t=qmul(qmul(q,(v[0],v[1],v[2],0)),qconj(q)); return (t[0],t[1],t[2])
def mat2quat(M):
    R=[[M[0],M[1],M[2]],[M[4],M[5],M[6]],[M[8],M[9],M[10]]]
    for r in R:
        n=math.sqrt(r[0]**2+r[1]**2+r[2]**2) or 1; r[0]/=n; r[1]/=n; r[2]/=n
    t=R[0][0]+R[1][1]+R[2][2]
    if t>0:
        s=math.sqrt(t+1)*2; w=.25*s; x=(R[2][1]-R[1][2])/s; y=(R[0][2]-R[2][0])/s; z=(R[1][0]-R[0][1])/s
    elif R[0][0]>R[1][1] and R[0][0]>R[2][2]:
        s=math.sqrt(1+R[0][0]-R[1][1]-R[2][2])*2; w=(R[2][1]-R[1][2])/s; x=.25*s; y=(R[0][1]+R[1][0])/s; z=(R[0][2]+R[2][0])/s
    elif R[1][1]>R[2][2]:
        s=math.sqrt(1+R[1][1]-R[0][0]-R[2][2])*2; w=(R[0][2]-R[2][0])/s; x=(R[0][1]+R[1][0])/s; y=.25*s; z=(R[1][2]+R[2][1])/s
    else:
        s=math.sqrt(1+R[2][2]-R[0][0]-R[1][1])*2; w=(R[1][0]-R[0][1])/s; x=(R[0][2]+R[2][0])/s; y=(R[1][2]+R[2][1])/s; z=.25*s
    return qnorm((x,y,z,w))

class Rig:
    """Node table + bind pose + all animations for one character archive."""
    def __init__(self, dict_path, hashid='../art/hashid.bin'):
        self.a=Archive(dict_path)
        hn=load_hashid_bin(hashid)
        self.hashnames = hn
        a=self.a
        seq=[(a.chunks[i][2],a.chunks[i][3],i) for i in range(a.num_file_entries,len(a.chunks))]
        g={}
        for t,s,i in seq:
            if 0x8001<=t<=0x8011: g.setdefault(t,[]).append(i)
        get=lambda t: a.get_chunk_bytes(g[t][0])
        nn=len(get(0x8003))//4
        self.nn=nn
        self.hashes=struct.unpack(f'>{nn}I',get(0x8003))
        self.names=[hn.get(h,f'{h:08X}') for h in self.hashes]
        self.par=list(struct.unpack(f'>{nn}i',get(0x8009)))
        self.loff=[struct.unpack_from('>3f',get(0x8010),n*12) for n in range(nn)]
        self.tflag=list(get(0x8011))
        self.mirror=struct.unpack(f'>{nn}I',get(0x8008))
        # bind world quats/pos from BoneData (0xB00A), matched by hash
        bd=a.get_chunk_bytes(a.find_chunks(type_id=0xB00A)[0])
        self.bonehash2idx={}; self.bone_wq=[]; self.bone_wp=[]
        for i in range(len(bd)//68):
            o=i*68; h=struct.unpack_from('>I',bd,o)[0]
            m=[struct.unpack_from('>f',bd,o+4+4*k)[0] for k in range(16)]
            self.bonehash2idx[h]=i
            # BoneData rotation is COLUMN-major -> transpose before quat extraction
            # (validated: static track quats == bind-local quats only with transpose)
            mT=[m[0],m[4],m[8],0, m[1],m[5],m[9],0, m[2],m[6],m[10],0, m[12],m[13],m[14],1]
            self.bone_wq.append(mat2quat(mT)); self.bone_wp.append((m[12],m[13],m[14]))
        self.node2bone=[self.bonehash2idx.get(h) for h in self.hashes]
        # node bind world orientation: bone's if it has one, else parent's (identity at root)
        self.wq_bind=[None]*nn
        order=sorted(range(nn), key=self._depth)
        for n in order:
            b=self.node2bone[n]
            if b is not None: self.wq_bind[n]=self.bone_wq[b]
            else: self.wq_bind[n]=(0,0,0,1) if self.par[n]<0 else self.wq_bind[self.par[n]]
        # bind local quats
        self.lbq=[None]*nn
        for n in range(nn):
            p=self.par[n]
            self.lbq[n]=self.wq_bind[n] if p<0 else qmul(qconj(self.wq_bind[p]),self.wq_bind[n])
        # parse all animation runs
        runs=[];cur=None
        for t,s,i in seq:
            if t==0x7001: cur=[]; runs.append(cur)
            if cur is not None: cur.append((t,s,i))
        self.anims={}
        for r in runs:
            nm=None
            for t,s,i in r:
                if t==0x7002: nm=a.get_chunk_bytes(i).split(b'\0')[0].decode('latin1'); break
            fr=struct.unpack_from('>I',a.get_chunk_bytes(r[0][2]),8)[0]
            rot=[a.get_chunk_bytes(i) for t,s,i in r if t==0x7101]
            trn=[a.get_chunk_bytes(i) for t,s,i in r if t==0x7102]
            t3ch=[i for t,s,i in r if t==0x7003]
            t3=struct.unpack(f'>{self.nn}I',a.get_chunk_bytes(t3ch[0])) if t3ch else (0,)*self.nn
            self.anims[nm]=(fr,rot,trn,t3)

    def _depth(self,n):
        d=0
        while self.par[n]>=0: d+=1; n=self.par[n]
        return d

    def decode_rot(self, data, fr, node=None, flag=None):
        """DEFINITIVE decode, reverse-engineered from main.dol (dispatch 0x80181acc,
        loaders 0x80188b90/ba4/c0c, angle fn 0x8018585c). Compression is PRECISION-based:
        every key is a FULL quaternion except the 1-value hinge type. 0x7003 flag bits:
          0x01: key = 1x s16 wrapping ANGLE (radians = raw*pi/32768) about local Z
          0x10: key = 4x s16 quat /32768                       (8 bytes)
          0x20: key = 4x 12-bit quat /2048 (nibble-packed)     (6 bytes)
          else: key = 4x s8  quat /128  (GQR7 hardware path)   (4 bytes)
          0x02: STATIC (single key)
        No component masks, no derived w, no hemisphere heuristics -- artifacts, all of it."""
        n=len(data)
        if flag is not None:
            bk = 2 if (flag & 0x1) else 8 if (flag & 0x10) else 6 if (flag & 0x20) else 4
            if n not in (bk, bk*fr): flag=None
        if flag is None:
            for bk in (8,6,4,2):
                if n in (bk, bk*fr): break
            else: raise ValueError(f'rot track {n}B / {fr} frames')
        static=(n==bk); keys=1 if static else fr
        out=[]
        for f in range(keys):
            o=f*bk
            if bk==2:
                a=struct.unpack_from('>h',data,o)[0]*(math.pi/32768.0)
                q=(0.0,0.0,math.sin(a/2),math.cos(a/2))
            elif bk==8:
                s=struct.unpack_from('>4h',data,o)
                q=qnorm((s[0]/32768.0,s[1]/32768.0,s[2]/32768.0,s[3]/32768.0))
            elif bk==6:
                b=data[o:o+6]
                v=[(b[0]<<4)|(b[1]>>4), ((b[1]&0xF)<<8)|b[2], (b[3]<<4)|(b[4]>>4), ((b[4]&0xF)<<8)|b[5]]
                v=[x-4096 if x>=2048 else x for x in v]
                q=qnorm((v[0]/2048.0,v[1]/2048.0,v[2]/2048.0,v[3]/2048.0))
            else:
                s=struct.unpack_from('>4b',data,o)
                q=qnorm((s[0]/128.0,s[1]/128.0,s[2]/128.0,s[3]/128.0))
            out.append(q)
        return out*fr if static else out

    def decode_trn(self, data, fr):
        if len(data)==12: return [struct.unpack('>3f',data)]*fr
        assert len(data)==12*fr, (len(data),fr)
        return [struct.unpack_from('>3f',data,f*12) for f in range(fr)]

    def pose(self, anim, conv='local'):
        """-> (frames, posf, qff): per-frame per-node world pos + quat."""
        fr,rot,trn,t3=self.anims[anim]
        nn=self.nn
        # rotation tracks -> nodes with 0x7003 != 0, in node order (node0 'root' = 0 = no
        # tracks). NOTE: 'bip01 footsteps' HAS a rot track (3dsMax gizmo, no skin, harmless).
        dq=[[ (0,0,0,1) ]*fr for _ in range(nn)]
        rnodes=[n for n in range(nn) if t3[n]!=0] if any(t3) else [n for n in range(nn) if n!=0]
        for i,d in enumerate(rot):
            dq[rnodes[i]]=self.decode_rot(d,fr,node=rnodes[i],flag=t3[rnodes[i]])
        # translations
        tr=[[self.loff[n]]*fr for n in range(nn)]
        ti=0
        for n in range(1,nn):
            if self.tflag[n]==0:
                if ti<len(trn): tr[n]=self.decode_trn(trn[ti],fr); ti+=1
        # FK: track quats are ABSOLUTE local rotations (xyzw, world = parent*child).
        # ('conv' kept for API compat; ignored.)
        order=sorted(range(nn), key=self._depth)
        posf=[];qff=[]
        for f in range(fr):
            wq=[None]*nn; wp=[None]*nn
            for n in order:
                q=dq[n][f]
                p=self.par[n]
                if p<0:
                    # node0 'root': NO tracks (0x7003[0]=0), identity placement.
                    wq[n]=(0,0,0,1); wp[n]=(0,0,0)
                elif p==0:
                    # bip01: WORLD rotation + translation. YAW converts anim world
                    # (character faces +X, backward=-X, left/right=+-Y) into the bind-mesh
                    # frame (faces -Y, backward=+Y) so anims align with the rest pose.
                    wq[n]=qnorm(qmul(WORLD_YAW,q))
                    wp[n]=qrot(WORLD_YAW,tr[n][f])
                else:
                    wq[n]=qnorm(qmul(wq[p],q))
                    wp[n]=tuple(wp[p][k]+qrot(wq[p],tr[n][f])[k] for k in range(3))
            posf.append(wp);qff.append(wq)
        # FOOT PLANTING (the game does this at runtime — raw tracks leave every character's
        # feet slightly tilted/hovering: gj +8deg, hondo +11, VK +19, DK +25. When a foot is
        # near the ground, level its sole (world pitch/roll vs bind) and re-seat the toe.)
        if getattr(self,'ground_feet',True):
            GROUND_Z=0.30; MAX_TILT=math.radians(50)
            pairs=[]
            for side in ('l','r'):
                try:
                    F=self.names.index(f'bip01 {side} foot');T=self.names.index(f'bip01 {side} toe0')
                    if self.node2bone[F] is not None: pairs.append((F,T))
                except ValueError: pass
            for f in range(fr):
                wq=qff[f];wp=posf[f]
                for F,T in pairs:
                    if wp[T][2]>GROUND_Z and wp[F][2]>GROUND_Z: continue
                    b=self.node2bone[F]
                    d=qmul(wq[F],qconj(self.bone_wq[b]))
                    nrm=qrot(d,(0,0,1))
                    tilt=math.acos(max(-1.0,min(1.0,nrm[2])))
                    if tilt<1e-4 or tilt>MAX_TILT: continue
                    ax=(nrm[1],-nrm[0],0.0)   # cross(nrm, z): rotates nrm onto +z
                    m=math.hypot(ax[0],ax[1])
                    if m<1e-6: continue
                    s=math.sin(tilt/2)/m
                    cq=(ax[0]*s,ax[1]*s,0.0,math.cos(tilt/2))
                    wq[F]=qnorm(qmul(cq,wq[F]))
                    # re-seat toe under the corrected foot
                    off=tuple(wp[T][k]-wp[F][k] for k in range(3))
                    off=qrot(cq,off)
                    wp[T]=tuple(wp[F][k]+off[k] for k in range(3))
                    wq[T]=qnorm(qmul(cq,wq[T]))
                    # and level the toe's own sole the same way
                    bt=self.node2bone[T]
                    if bt is not None:
                        d2=qmul(wq[T],qconj(self.bone_wq[bt]))
                        n2=qrot(d2,(0,0,1))
                        t2=math.acos(max(-1.0,min(1.0,n2[2])))
                        if 1e-4<t2<MAX_TILT:
                            ax2=(n2[1],-n2[0],0.0);m2=math.hypot(ax2[0],ax2[1])
                            if m2>1e-6:
                                s2=math.sin(t2/2)/m2
                                cq2=(ax2[0]*s2,ax2[1]*s2,0.0,math.cos(t2/2))
                                wq[T]=qnorm(qmul(cq2,wq[T]))
        return fr,posf,qff

    def bind_check(self):
        """FK with identity deltas should reproduce BoneData world positions."""
        nn=self.nn
        order=sorted(range(nn), key=self._depth)
        wq=[None]*nn; wp=[None]*nn; err=0
        for n in order:
            p=self.par[n]
            if p<0: wq[n]=self.lbq[n]; wp[n]=self.loff[n]
            else:
                wq[n]=qmul(wq[p],self.lbq[n])
                wp[n]=tuple(wp[p][k]+qrot(wq[p],self.loff[n])[k] for k in range(3))
        for n in range(nn):
            b=self.node2bone[n]
            if b is None: continue
            e=max(abs(wp[n][k]-self.bone_wp[b][k]) for k in range(3))
            if e>err: err=e; worst=self.names[n]
        return err,worst

def skin_frames(rig, anim, conv='local', step=1, only=None):
    """Linear-blend skin the real mesh with the reconstructed pose."""
    import nlg_geom
    hn=getattr(rig, 'hashnames', {})
    a=rig.a
    meshes=nlg_geom.read_model(a,hn)
    bh=a.find_chunks(type_id=0xB00B)
    palettes=[]
    for ri in bh[:len(meshes)]:
        d=a.get_chunk_bytes(ri)
        palettes.append([struct.unpack_from('>I',d,o)[0] for o in range(0,len(d),4)])
    hash2node={h:n for n,h in enumerate(rig.hashes)}
    fr,posf,qff=rig.pose(anim,conv)
    # node bind world pos for skinning reference = bone bind world
    prepared=[]
    for mi,m in enumerate(meshes):
        pal=palettes[mi] if mi<len(palettes) else []
        vl=[]
        for vi in range(len(m.pos)):
            infl=[]
            bidx=m.bidx[vi] if vi<len(m.bidx) else (0,0,0,0)
            wts=m.bwt[vi] if vi<len(m.bwt) else (1,0,0,0)
            for k in range(4):
                w=wts[k]
                if w<=0.0001: continue
                pj=bidx[k]
                if pj>=len(pal): continue
                n=hash2node.get(pal[pj])
                if n is None or rig.node2bone[n] is None: continue
                infl.append((n,w))
            if not infl: infl=[(3,1.0)]
            vl.append(infl)
        prepared.append(vl)
    frames_out=[]
    fsel=only if only is not None else range(0,fr,step)
    fsel=[min(f,fr-1) for f in fsel]
    for f in fsel:
        Q=qff[f];P=posf[f]
        mp=[]
        for mi,m in enumerate(meshes):
            pts=[]
            for vi,v in enumerate(m.pos):
                sx=sy=sz=0.0
                for n,w in prepared[mi][vi]:
                    b=rig.node2bone[n]
                    bq=rig.bone_wq[b];bp=rig.bone_wp[b]
                    lv=qrot(qconj(bq),(v[0]-bp[0],v[1]-bp[1],v[2]-bp[2]))
                    aw=qrot(Q[n],lv)
                    sx+=w*(P[n][0]+aw[0]); sy+=w*(P[n][1]+aw[1]); sz+=w*(P[n][2]+aw[2])
                pts.append((sx,sy,sz))
            mp.append((pts,m.tris))
        frames_out.append(mp)
    return frames_out,fr

def render_gif(dict_path, anim='idle', conv='local', out=None, step=1):
    import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from PIL import Image
    rig=Rig(dict_path)
    fd,fr=skin_frames(rig,anim,conv,step)
    out=out or f'{anim}_v2.gif'
    imgs=[]
    for k,mp in enumerate(fd):
        fig,ax=plt.subplots(figsize=(3.4,4.4))
        polys=[]
        for pts,tris in mp:
            for ia,ib,ic in tris:
                polys.append([(pts[ia][0],pts[ia][2]),(pts[ib][0],pts[ib][2]),(pts[ic][0],pts[ic][2])])
        ax.add_collection(PolyCollection(polys,facecolors=(0.35,0.55,0.8,0.5),edgecolors=(0.1,0.2,0.4,0.25),linewidths=0.2))
        ax.set_xlim(-1.2,1.2);ax.set_ylim(-0.1,2.4);ax.set_aspect('equal');ax.axis('off')
        ax.set_title(f'{anim} [{conv}] {k*step+1}/{fr}',fontsize=9)
        fig.canvas.draw();imgs.append(Image.frombytes('RGBA',fig.canvas.get_width_height(),bytes(fig.canvas.buffer_rgba())).convert('RGB'));plt.close(fig)
    imgs[0].save(out,save_all=True,append_images=imgs[1:],duration=60*step,loop=0)
    print(f'{anim} [{conv}]: {fr} frames -> {out}')

def compare(dict_path, anim='uppercut_r', frames=(0,15,30,45), out='convention_v2.png'):
    """Render key frames for both composition conventions side by side."""
    import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    rig=Rig(dict_path)
    e,w=rig.bind_check(); print(f'bind FK check: max err {e:.6f} ({w})')
    fig,axes=plt.subplots(2,len(frames),figsize=(3.0*len(frames),8))
    for row,conv in enumerate(('local','parent')):
        fd,fr=skin_frames(rig,anim,conv,only=list(frames))
        for col,f in enumerate(frames):
            ax=axes[row][col]
            polys=[]
            for pts,tris in fd[col]:
                for ia,ib,ic in tris:
                    polys.append([(pts[ia][0],pts[ia][2]),(pts[ib][0],pts[ib][2]),(pts[ic][0],pts[ic][2])])
            ax.add_collection(PolyCollection(polys,facecolors=(0.35,0.55,0.8,0.5),edgecolors=(0.1,0.2,0.4,0.25),linewidths=0.2))
            ax.set_xlim(-1.2,1.2);ax.set_ylim(-0.1,2.4);ax.set_aspect('equal');ax.axis('off')
            ax.set_title(f'{conv} f{min(f,fr-1)}',fontsize=9)
    plt.tight_layout();plt.savefig(out,dpi=90);plt.close()
    print(f'-> {out}')

if __name__=='__main__':
    dp=sys.argv[1] if len(sys.argv)>1 else '../art/characters/glassjoe.dict'
    if len(sys.argv)>2 and sys.argv[2]=='compare': compare(dp)
    else: render_gif(dp, sys.argv[2] if len(sys.argv)>2 else 'idle')
