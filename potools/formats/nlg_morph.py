"""
nlg_morph.py — Punch-Out!! Wii facial vertex-morph (blend shape) decoder.

0xB00C GRAMMAR (field order is delta-FIRST, index-LAST):
  u32 A = total shape count (Σ nShapes over channels)
  u32 B = channel count
  A x f32 = per-shape weight BREAKPOINTS, channel-major (2-breakpoint channels = in-between
            shapes, e.g. DK eyelids (0.5, 1.0); single 1.0 = plain target)
  u32 16 (record size), u32 S = number of mesh-lists (model mesh order)
  S x meshList: B x channel: { u32 nShapes, nShapes x { u32 nRecs,
        nRecs x { f32 dx, f32 dy, f32 dz, u32 LOCAL vertexIndex } } }

WEIGHTS (per anim; from main.dol 0x80182710/0x80188cc0):
  0x7009 = per-channel key counts, 0x700A = per-channel target-id hashes,
  0x700B = channel-major u8 keys / 255.0 (lerp between adjacent keys by phase),
  0x7007 = 1 s16/frame master weight, 0x7008 = 6 s16/frame (blend params; constant per anim).
  Engine: out[ch] += master * w, clamped to 1.0.

RECONSTRUCTION (channel ch at weight w, channel-major shape list):
  1 breakpoint b0:      vert += (w/b0) * delta            (w usually <= b0 = 1.0)
  2 breakpoints b0,b1:  w<=b0: vert += (w/b0)*shape0
                        w> b0: vert += shape0 + (w-b0)/(b1-b0) * (shape1 - shape0)
Usage:
  from nlg_morph import Morphs
  M = Morphs('../art/characters/donkeykong.dict')
  M.channels()                  -> per-channel info (breakpoints, affected meshes/verts)
  M.anim_weights('blink')       -> frames x channels float array
  M.apply(meshes, weights_row)  -> displaced copies of mesh positions
"""
import sys, os, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nlg_pack import Archive
import nlg_geom, nlg_hash


# Morph target names. Archives store only each target's 32-bit name hash (the per-clip 0x700A
# lists); none of these strings exist in the game files. They were recovered by hashing candidate
# names and keeping readable matches. Unlisted hashes stay unnamed rather than guessed.
TARGET_NAMES = {nlg_hash.string_to_hash(n): n for n in (
    "blink_l_top", "blink_l_bottom", "blink_r_top", "blink_r_bottom",
    "default_face", "default_brows", "default_mouth", "eye_left", "eye_right", "eyeleft", "eyeright",
    "damage_brow", "damage_cheek", "damage_chin", "damage_ear", "damage_eye", "damage_l_eye",
    "damage_r_eye", "damage_hair", "damage_head", "damage_lip", "damage_lips", "damage_lips_frown",
    "damage_nose", "damage_teeth", "damage_stache", "damage_bandage", "beard_damage", "belly_damage",
    "eye_damage", "head_damage", "lips_damage", "teeth_damage",
    "dk_angry_top", "dk_angry_bottom", "dk_sad_top", "dk_sad_bottom", "dk_surprise_top",
    "dk_surprise_bottom", "dk_smile", "dk_laugh", "dk_cheek_l", "dk_cheek_r",
    "pantsdrop", "helmet", "boots_fix")}


# Every target hash that some retail clip animates (a full scan of art/, all 0x700A lists with a
# nonzero 0x700B key). A target no clip ever drives is a static state: in practice the hurt shapes.
ANIMATED_TARGETS = frozenset((
    0x002572A3, 0x053CD14A, 0x0BB099E8, 0x0C1D2CEE, 0x1650568C, 0x17A68BC0,
    0x240F6604, 0x4E05706B, 0x5901AE06, 0x61A78090, 0x6D4CF60E, 0x6FA56590,
    0x706AE710, 0x742C43D9, 0x763340C2, 0x792F1076, 0x7B91994C, 0x7BDF7CBE,
    0x82F8A34F, 0x8334E7DC, 0x868788B4, 0x93C810A0, 0x93D77A29, 0x93D77A2A,
    0x9418F66D, 0x96EDD653, 0xA1CED7FA, 0xACFF059E, 0xB5D09D12, 0xC02B8A60,
    0xC8B6938A, 0xC8B6938B, 0xC8B6938C, 0xD81C834A, 0xE0C9EFBF, 0xE745B5DE,
    0xE799043C, 0xE7CAC387, 0xE92A8CD7, 0xEC604F40, 0xEE5CF48C, 0xEEC98792,
    0xF048FCBB, 0xFCFC8B6E,
))


def target_name(h):
    """Readable name for a target hash, 'target_XXXXXXXX' when unknown, None for no hash."""
    if h is None:
        return None
    return TARGET_NAMES.get(h, "target_%08X" % h)


def canonical_targets(clip_lists, channels):
    """Model channel -> target hash, from the clips' 0x700A lists.

    The model's 0xB00C channels carry no names; each clip lists the targets it animates, and
    most list every channel in model order. A few list a subset, swap two entries, or name a
    channel differently (King Hippo), so each channel takes the hash most clips put at that
    position, never reusing a hash. Channels no clip names stay None.
    """
    votes = [{} for _ in range(channels)]
    for hs in clip_lists:
        for k, h in enumerate(hs[:channels]):
            votes[k][h] = votes[k].get(h, 0) + 1
    names, used = [], set()
    for v in votes:
        pick = next((h for h, _ in sorted(v.items(), key=lambda kv: (-kv[1], kv[0])) if h not in used), None)
        names.append(pick)
        if pick is not None: used.add(pick)
    return names


def parse_b00c(d):
    """Return (breakpoints_per_channel, S, lists) where lists[mesh][channel] = [shape,...],
    shape = list of (localVertIdx, dx, dy, dz)."""
    A, B = struct.unpack_from('>2I', d, 0)
    o = 8
    bps = list(struct.unpack_from(f'>{A}f', d, o)); o += 4 * A
    recSize, S = struct.unpack_from('>2I', d, o); o += 8
    assert recSize == 16, f"recSize {recSize}"
    lists = []
    chan_ns = None
    for li in range(S):
        chans = []
        for ch in range(B):
            n = struct.unpack_from('>I', d, o)[0]; o += 4
            assert n <= 16, f"list{li} ch{ch} nShapes={n} @{o-4:#x}"
            shapes = []
            for si in range(n):
                nr = struct.unpack_from('>I', d, o)[0]; o += 4
                assert nr * 16 <= len(d) - o, f"nRecs {nr} @{o-4:#x}"
                recs = []
                for k in range(nr):
                    dx, dy, dz = struct.unpack_from('>3f', d, o)
                    idx = struct.unpack_from('>I', d, o + 12)[0]
                    recs.append((idx, dx, dy, dz)); o += 16
                shapes.append(recs)
            chans.append(shapes)
        ns = tuple(len(s) for s in chans)
        if chan_ns is None: chan_ns = ns
        else: assert ns == chan_ns, f"list{li} channel shape-counts differ"
        lists.append(chans)
    assert o == len(d), f"consumed {o}/{len(d)}"
    assert sum(chan_ns) == A
    # split breakpoints channel-major
    bp = []; i = 0
    for n in chan_ns:
        bp.append(bps[i:i + n]); i += n
    return bp, S, lists


class Morphs:
    def __init__(self, dict_path, hashbin=None):
        self.a = Archive(dict_path)
        hb = hashbin or os.path.join(os.path.dirname(dict_path), '..', 'hashid.bin')
        self.hn = nlg_hash.load_hashid_bin(hb) if os.path.exists(hb) else {}
        d = self.a.get_chunk_bytes(self.a.find_chunks(type_id=0xB00C)[0])
        self.bp, self.S, self.lists = parse_b00c(d)
        self.B = len(self.bp)
        self.meshes = nlg_geom.read_model(self.a, self.hn)
        # anim runs (for weights)
        self._runs = self._index_anims()
        # model channel -> target hash, from every clip's 0x700A list
        self.chan_hashes = canonical_targets([self._clip_hashes(r) for r in self._runs.values()], self.B)

    def _clip_hashes(self, run):
        if 0x700A not in run:
            return []
        d = self.a.get_chunk_bytes(run[0x700A])
        return list(struct.unpack('>%dI' % (len(d) // 4), d))

    def key_names(self, ch):
        """Shape key names for channel ch, one per breakpoint: '04_blink_r_top', '06_x_50'."""
        base = "%02d_%s" % (ch, target_name(self.chan_hashes[ch]) or "morph%02d" % ch)
        bp = self.bp[ch]
        return [base] if len(bp) == 1 else ["%s_%d" % (base, int(round(b * 100))) for b in bp]

    def animated_channels(self):
        """Model channels that any clip in this archive drives with a nonzero weight."""
        if getattr(self, "_animated", None) is None:
            self._animated = set()
            for anim in self.anim_names():
                try: rows = self.anim_weights(anim)
                except (KeyError, ValueError, struct.error): continue
                for row in rows:
                    self._animated.update(ch for ch, w in enumerate(row) if w > 0)
        return self._animated

    def hurt_channels(self):
        """Channels that belong to the hurt state: named damage targets, and static targets no
        retail clip animates. Archives whose clips animate no morphs at all (the referee) have
        nothing to compare against, so only named damage targets count there."""
        animated = self.animated_channels()
        out = []
        for ch in range(self.B):
            h = self.chan_hashes[ch]
            name = TARGET_NAMES.get(h, "")
            if "damage" in name or "bandage" in name:
                out.append(ch)
            elif animated and ch not in animated and h not in ANIMATED_TARGETS:
                out.append(ch)
        return out

    def model_channel(self, clip_hashes, k):
        """Model channel animated by a clip's k-th channel: matched by target hash, or by
        position when the clip carries no usable name list. None if the target is not on this model."""
        if clip_hashes and k < len(clip_hashes) and any(self.chan_hashes):
            h = clip_hashes[k]
            return self.chan_hashes.index(h) if h in self.chan_hashes else None
        return k if k < self.B else None

    def _index_anims(self):
        runs = {}
        idxs = sorted(range(self.a.num_file_entries, len(self.a.chunks)),
                      key=lambda i: (self.a._chunk_block(i), self.a.chunks[i][4]))
        cur = None
        for i in idxs:
            t = self.a.chunks[i][2]
            if t == 0x7001:
                cur = {}
            if cur is not None and 0x7000 <= t <= 0x71FF:
                cur.setdefault(t, i)
                if t == 0x7002:
                    name = self.a.get_chunk_bytes(i).split(b'\0')[0].decode()
                    runs[name] = cur
        return runs

    def anim_names(self):
        return sorted(self._runs)

    def anim_weights(self, anim):
        """frames x B model-channel weights (u8/255 keys). Clip channels are matched to model
        channels by target hash; model channels the clip does not animate stay 0."""
        r = self._runs[anim]
        clip_hashes = self._clip_hashes(r)
        counts = struct.unpack('>%dI' % (len(self.a.get_chunk_bytes(r[0x7009])) // 4),
                               self.a.get_chunk_bytes(r[0x7009]))
        stream = self.a.get_chunk_bytes(r[0x700B])
        m7007 = struct.unpack('>%dh' % (len(self.a.get_chunk_bytes(r[0x7007])) // 2),
                              self.a.get_chunk_bytes(r[0x7007]))
        nf = max(counts)
        out = []
        target = [self.model_channel(clip_hashes, k) for k in range(len(counts))]
        for f in range(nf):
            row = [0.0] * self.B; o = 0
            for k, cnt in enumerate(counts):
                keys = stream[o:o + cnt]; o += cnt
                if not cnt or target[k] is None:
                    continue
                # phase-sample (frame f of nf maps to key f when counts==frames)
                row[target[k]] = min(keys[min(f, cnt - 1)] / 255.0, 1.0)
            out.append(row)
        return out

    def channel_deltas(self, ch):
        """[(meshIdx, shapeIdx, [(localVert, dx,dy,dz)...])] for channel ch."""
        out = []
        for mi, chans in enumerate(self.lists):
            for si, recs in enumerate(chans[ch]):
                if recs:
                    out.append((mi, si, recs))
        return out

    def channels(self):
        info = []
        for ch in range(self.B):
            per = self.channel_deltas(ch)
            meshes = sorted({mi for mi, _, _ in per})
            nrec = sum(len(r) for _, _, r in per)
            h = self.chan_hashes[ch] or 0
            info.append({'ch': ch, 'hash': f'{h:08x}', 'name': target_name(self.chan_hashes[ch]),
                         'breakpoints': self.bp[ch],
                         'meshes': meshes, 'records': nrec,
                         'mesh_names': [getattr(self.meshes[m], 'name', '?') if m < len(self.meshes) else '?'
                                        for m in meshes]})
        return info

    def apply(self, weights_row):
        """Return {meshIdx: displaced position list} for one frame's channel weights."""
        disp = {}
        for ch, w in enumerate(weights_row):
            if w <= 0:
                continue
            bps = self.bp[ch]
            for mi, chans in enumerate(self.lists):
                shapes = chans[ch]
                if not shapes or all(not s for s in shapes):
                    continue
                if mi not in disp:
                    disp[mi] = [list(p) for p in self.meshes[mi].pos] if mi < len(self.meshes) else None
                if disp[mi] is None:
                    continue
                P = disp[mi]
                if len(bps) == 1 or w <= bps[0] + 1e-9:
                    t = w / bps[0]
                    for idx, dx, dy, dz in shapes[0]:
                        if idx < len(P):
                            P[idx][0] += t * dx; P[idx][1] += t * dy; P[idx][2] += t * dz
                else:
                    t = (w - bps[0]) / (bps[1] - bps[0])
                    s0 = {i: (x, y, z) for i, x, y, z in shapes[0]}
                    s1 = {i: (x, y, z) for i, x, y, z in (shapes[1] if len(shapes) > 1 else [])}
                    for idx in set(s0) | set(s1):
                        a = s0.get(idx, (0, 0, 0)); b = s1.get(idx, (0, 0, 0))
                        if idx < len(P):
                            P[idx][0] += a[0] + t * (b[0] - a[0])
                            P[idx][1] += a[1] + t * (b[1] - a[1])
                            P[idx][2] += a[2] + t * (b[2] - a[2])
        return disp


if __name__ == '__main__':
    dp = sys.argv[1] if len(sys.argv) > 1 else '../art/characters/donkeykong.dict'
    M = Morphs(dp)
    print(f"{os.path.basename(dp)}: {M.B} channels, {M.S} mesh-lists, meshes={len(M.meshes)}")
    for c in M.channels():
        if c['records']:
            print(f"  ch{c['ch']:2d} {c['hash']} bp={c['breakpoints']} recs={c['records']:5d} "
                  f"meshes={[n.split('/')[-1] for n in c['mesh_names']]}")
