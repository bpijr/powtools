"""Punch-Out!! Wii arena crowd: the impostor system of main.dol (PAL R7PP01), reproduced.

Every rule below was decoded from the executable and checked against a Dolphin savestate of a
Minor Circuit fight (1006 members).  With the RNG state recovered from the first member's jitter,
``layout`` reproduces every member's position (float32), character, facing bucket and pose, and
``TintMap`` reproduces every member's vertex colour byte for byte.

Runtime pipeline
----------------
* CrowdLoader (800AC874/800AD040) creates one ImpostorCharacter per roster entry
  (803316A0: Crowd_Female_2, Crowd_Light_2, Crowd_Light_3; Crowd_DK only with the DK flag),
  each with 4 yaw buckets x 4 animation poses of 64x128 RGB5A3 render targets (8015D44C).
  Every view gets a random yaw jitter of +-10 degrees (80159600 / 80159818).
* Each ``crowd_helperNN`` record is a trapezoid (front width, back width, depth, skew).  80160F08
  fills it with rows ``Distance Between Crowd Rows`` apart and members ``Distance Between Crowd
  Members`` apart, both centred; 801615E8 jitters each member by U(+-members*hJitter) along the
  row and U(+-rows*vJitter) across it (no height jitter), picks a random character that differs
  from the previous member of the row, faces it along the helper's -Y (nearest of 4 buckets) and
  gives it a random pose (8015CA40).  All draws come from the one global RNG at 804133B0.
* Each member's vertex colour is the CPU-decoded light map ``global/litcrowd<circuit>`` sampled
  at u = (x + 30) / 60, v = 1 - (y - 30) / 60 (800C2B4C; tweaks CrowdShadowScale/X/YOffset).
* Every frame 800AC4F4 hands the live camera's forward and up vectors to the impostors.  A view's
  camera looks along that forward at (0, 0, lookat z) from ``distance`` away (80159818), with a
  38 degree vertical FOV, aspect 0.5, near 0.25, far 512; the model is drawn with
  scale(x, y, z) then the view's yaw.  crowdskin lighting follows that same matrix.
* 8015BA1C draws one camera-facing quad per member: centre = position + (0, 0, h*s/2), half
  extents 0.25*w*s along the camera's right and 0.5*h*s along its up, colour = tint x texture,
  alpha test > 0x80, blend SRCALPHA/INVSRCALPHA, Z write on, no culling.
* 800AD6D0 runs a sit / bored / clap state machine per (character, pose); reactions only come
  from gameplay (punches, stuns, knockdowns) or scripts, never from the DK pre-fight NIS.
"""
import json
import math
import os
import re
import struct
from pathlib import Path

import numpy as np

f32 = np.float32

ROSTER = ("Crowd_Female_2", "Crowd_Light_2", "Crowd_Light_3")      # 803316A0 (type index order)
ANGLES, POSES = 4, 4                                               # 8015D44C (r8 = r9 = 4)
TARGET_SIZE = (64, 128)                                            # type +0x70 / +0x74
FOVY = 0.6632251143455505                                          # 80417940
NEAR, FAR = 0.25, 512.0                                            # 804134F8 / 804134FC
VIEW_JITTER_DEG = 10.0                                             # 80417948 x U(-1, 1)
FPS_GAME = 60.0
ANIM_FPS = 30.0

# Tweak defaults (80161F88, 8015D44C) and the circuit INI files that override them.
LAYOUT_DEFAULTS = dict(rows=0.6, members=0.6, vjitter=0.4, hjitter=0.4, width=1.0, height=1.5, size=1.0)
CHARACTER_DEFAULTS = dict(scale=(1.0, 1.0, 1.0), lookat_z=1.2, distance=2.3)
CIRCUITS = {
    # arena: (ini, light map, rim ramp that replaces "<model>/crowdrimlightramp" or None)
    "worldcircuit": ("ini/Crowd.ini", "global/litcrowdworld", "global/crowdrimlightrampworld"),
    "minorcircuit": ("ini/MinorCrowd.ini", "global/litcrowdminor", "global/crowdrimlightrampminor"),
    "majorcircuit": ("ini/MajorCrowd.ini", "global/litcrowdmajor", None),
}
TINT_SCALE, TINT_X, TINT_Y = 60.0, 30.0, -30.0                     # 8011466C defaults
CROWD_LIGHT_WORLD = (0.75, 0.75, 0.5)                              # ViewCrowdLightDirection0..2

ATLAS_COLS = 8                                                     # 48 views -> 8 x 6 tiles

# World Circuit authors 11 regions; four sit at the ring corners (origin within this radius of
# the ring centre) and the other seven fill the far stands (~9,500 of 13,291 members).  In the
# DK pre-fight the far-stand impostors change no pixel next to the 2D flat-crowd cards, and the
# ring-side ones are what shows behind the ropes, so the preview lays out only the ring-side
# regions.  The full layout is still run first so the RNG stream is unchanged.
# Unverified for other cameras; Minor Circuit's savestate count (1006) uses every region.
RING_SIDE_RADIUS = {"worldcircuit": 8.0}


def ring_side_helpers(arena, helpers):
    """Names of the helper regions the preview spawns for ``arena`` (None = all of them)."""
    radius = RING_SIDE_RADIUS.get(arena)
    if radius is None:
        return None
    return {h["name"] for h in helpers if math.hypot(h["matrix"][12], h["matrix"][13]) < radius}


def cache_dir(source, arena):
    """Per-cutscene folder for baked atlases, outside the repository (game imagery)."""
    import hashlib, tempfile
    key = hashlib.sha1(("%s|%s" % (Path(source).resolve(), arena)).encode("utf-8")).hexdigest()[:12]
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return str(Path(base) / "PunchOutTools" / "crowd" / key)


# --------------------------------------------------------------------------------------------
# Global RNG (804133B0, boot seed 0x12345678)
class GameRNG:
    """fn_80134764 (integer), fn_80134824 (float range) and fn_801347A8 (float [0, hi))."""

    def __init__(self, state=0x12345678):
        self.state = state & 0xFFFFFFFF

    def _next(self):
        s = self.state
        r7 = s ^ 0x1D872B41
        r8 = r7 ^ (r7 >> 5)
        self.state = ((r7 >> 5) ^ ((r8 << 27) & 0xFFFFFFFF)) & 0xFFFFFFFF
        return s

    @staticmethod
    def _mod(s):            # s mod 0x7FFFFFFF exactly as the multiply-high sequence computes it
        hi = (3 * s) >> 32
        q = ((((s - hi) & 0xFFFFFFFF) >> 1) + hi) >> 30
        return (s - q * 0x7FFFFFFF) & 0xFFFFFFFF

    def randint(self, n):
        return 0 if n == 0 else self._next() % n

    def randf(self, lo, hi):
        v = f32(self._mod(self._next()))
        k = f32(f32(4.656612873077393e-10) * f32(f32(hi) - f32(lo)))
        return float(f32(f32(lo) + f32(k * v)))

    def rand0(self, hi):
        v = f32(self._mod(self._next()))
        return float(f32(f32(f32(4.656612873077393e-10) * f32(hi)) * v))


# --------------------------------------------------------------------------------------------
# Settings from the INI files
def read_ini(path):
    out, section = {}, ""
    try:
        text = Path(path).read_text(encoding="latin-1")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            section = line.strip("[]").strip().lower(); out.setdefault(section, {})
        elif "=" in line:
            k, v = line.split("=", 1)
            try:
                out.setdefault(section, {})[k.strip().lower()] = float(v)
            except ValueError:
                pass
    return out


def circuit_settings(files_root, arena):
    """Layout tweaks for an arena ('worldcircuit', ...) from DATA/files/ini."""
    ini, light, rim = CIRCUITS.get(arena, CIRCUITS["worldcircuit"])
    s = dict(LAYOUT_DEFAULTS, arena=arena, ini=ini, light_map=light, rim_ramp=rim)
    data = read_ini(Path(files_root) / ini)
    lay = data.get("crowd/layout", {}); vis = data.get("impostor/visual tweaks", {})
    s["rows"] = lay.get("distance between crowd rows", s["rows"])
    s["members"] = lay.get("distance between crowd members", s["members"])
    s["vjitter"] = lay.get("vertical jitter fraction", s["vjitter"])
    s["hjitter"] = lay.get("horizontal jitter fraction", s["hjitter"])
    s["size"] = vis.get("impostor size scale", s["size"])
    return s


def character_tweaks(files_root, name):
    data = read_ini(Path(files_root) / "ini" / "ImpostorCharacterTweaks.ini").get(name.lower(), {})
    d = CHARACTER_DEFAULTS
    return dict(scale=(data.get("scale x", d["scale"][0]), data.get("scale y", d["scale"][1]),
                       data.get("scale z", d["scale"][2])),
                lookat_z=data.get("camera lookat z", d["lookat_z"]),
                distance=data.get("camera distance", d["distance"]))


# --------------------------------------------------------------------------------------------
# Layout (80160F08 region fill, 801615E8 member placement)
def _corners(params):
    W0, W1, D, sk = [f32(x) for x in params[:4]]
    h = f32(f32(0.5) * f32(W0 - W1))
    return (np.array([W0, 0, 0], np.float64), np.array([0, 0, 0], np.float64),
            np.array([f32(sk + h), D, 0], np.float64), np.array([f32(W1 + f32(sk + h)), D, 0], np.float64))


def fill_region(params, rows, members):
    """Grid points of one helper trapezoid, grouped by row (local helper space)."""
    c0, c1, c2, c3 = _corners(params)
    D = f32(params[2]); rs, ms = f32(rows), f32(members)
    nr = int(math.floor(D / rs)) + 1
    t0 = f32(f32(f32(0.5) * f32(D - f32(f32(nr - 1) * rs))) / D)
    step = f32(rs / D)
    out = []
    for r in range(nr):
        t = f32(f32(r) * step + t0)
        A = [f32((1 - t) * a + t * b) for a, b in zip(c3, c0)]
        B = [f32((1 - t) * a + t * b) for a, b in zip(c2, c1)]
        L = f32(math.sqrt(sum(float(A[i] - B[i]) ** 2 for i in range(3))))
        nm = int(math.floor(L / ms)) + 1
        s0 = f32(f32(f32(0.5) * f32(L - f32(f32(nm - 1) * ms))) / L)
        sstep = f32(ms / L)
        row = []
        for m in range(nm):
            s = f32(f32(m) * sstep + s0)
            row.append([float(f32(s * A[i] + (1 - s) * B[i])) for i in range(3)])
        out.append(row)
    return out


def nearest_bucket(yaw, n=ANGLES):
    """8015C988: the bucket angle (u16) closest to ``yaw`` (u16); ties keep the first."""
    step = 65536 // n
    best, pick, a = 0x8000, 0, 0
    for _ in range(n):
        d = ((a & 0xFFFF) - yaw) & 0xFFFF
        d = abs(d - 0x10000 if d & 0x8000 else d) & 0xFFFF
        if d < best:
            best, pick = d, a & 0xFFFF
        a += step
    return pick


def layout(helpers, settings, rng, ntypes=len(ROSTER), nposes=POSES, nangles=ANGLES):
    """Members in creation order: dict(pos, type, bucket, pose, helper, row).

    ``helpers`` are nlg_cutscene.arena_crowd_helpers records; regions fill in record order."""
    rs, ms = settings["rows"], settings["members"]
    jx = float(f32(f32(ms) * f32(settings["hjitter"])))
    jy = float(f32(f32(rs) * f32(settings["vjitter"])))
    out = []
    for h in sorted(helpers, key=lambda r: (r["chunk"], r["offset"])):
        M = np.array(h["matrix"], np.float64).reshape(4, 4)
        facing = np.array([0.0, -1.0, 0.0, 0.0]) @ M
        yaw = int(math.atan2(facing[1], facing[0]) * 10430.378) & 0xFFFF
        bucket = nearest_bucket(yaw, nangles)
        for r, row in enumerate(fill_region(h["parameters"], rs, ms)):
            prev = -1
            for p in row:
                x = float(f32(f32(p[0]) + f32(rng.randf(-jx, jx))))
                y = float(f32(f32(p[1]) + f32(rng.randf(-jy, jy))))
                w = np.array([x, y, p[2], 1.0]) @ M
                t = rng.randint(ntypes)
                if t == prev:
                    t = (t + 1) % ntypes
                pose = int(math.floor(rng.randf(0.0, float(nposes))))
                out.append(dict(pos=tuple(float(f32(v)) for v in w[:3]), type=t, bucket=bucket,
                                pose=pose, helper=h["name"], row=r))
                prev = t
    return out


def view_jitters(rng, ntypes=len(ROSTER), nposes=POSES, nangles=ANGLES):
    """{(type, pose, bucket index): U(-1, 1)} for the set-0 views (80159600)."""
    out = {}
    for t in range(ntypes):
        for s in range(2):
            for p in range(nposes):
                for b in range(nangles):
                    j = rng.randf(-1.0, 1.0)
                    if s == 0:
                        out[(t, p, b)] = j
    return out


# --------------------------------------------------------------------------------------------
# Tint (800C2B4C)
class TintMap:
    def __init__(self, rgba, width, height):
        self.px = np.frombuffer(bytes(rgba), np.uint8).reshape(height, width, 4)
        self.w, self.h = width, height

    @classmethod
    def from_global(cls, art_root, name, names=None):
        import nlg_asset, nlg_hash, nlg_texture
        doc = nlg_asset.AssetDocument(Path(art_root) / "global.dict", names)
        key = nlg_hash.string_to_hash(name)
        for sec in doc.sections:
            for e in nlg_texture.list_all_textures(sec.archive):
                if e.hash == key:
                    raw = sec.archive.get_chunk_bytes(nlg_texture._pixel_chunk(sec.archive, e))
                    size = nlg_texture.texture_size(e.fmt, e.width, e.height)
                    data = raw[e.data_offset:e.data_offset + size]
                    if nlg_texture.FORMATS.get(e.fmt) == "CMPR":
                        rgba = nlg_texture.decode_cmpr_cpu(data, e.width, e.height)
                    else:
                        rgba = nlg_texture.decode_texture(sec.archive, e)
                    return cls(rgba, e.width, e.height)
        raise KeyError(name)

    def sample(self, x, y):
        u = f32(f32(f32(x) + f32(TINT_X)) / f32(TINT_SCALE))
        v = f32(f32(1.0) - f32(f32(f32(y) + f32(TINT_Y)) / f32(TINT_SCALE)))
        px = int(u * f32(self.w - 1)); py = int(v * f32(self.h - 1))
        return tuple(int(c) for c in self.px[py % self.h, px % self.w])


# --------------------------------------------------------------------------------------------
# Behaviour (800AD43C / 800AD6D0) -> clip timelines per (type, pose)
class Behaviour:
    """Per-(character, pose) crowd state machine.  ``clips`` maps clip name -> key count."""

    IDLE, CLAP_IN, CLAP, CLAP_OUT, BORED_IN, BORED, BORED_OUT = range(7)

    def __init__(self, rng, clips, female, standing=False, ntypes=len(ROSTER), nposes=POSES):
        self.rng, self.clips, self.standing = rng, clips, standing
        self.recs = []
        for t in range(ntypes):
            for p in range(nposes):
                self.recs.append(dict(type=t, pose=p, female=bool(female[t]), state=0, variant=1,
                                      timer=0.0, pending=0, delay=0.0, idle=rng.rand0(10.0),
                                      events=[]))
        self.duration = 0.0

    def _prefix(self, rec):
        return "female" if rec["female"] else "male"

    def length(self, name):
        n = self.clips.get(name)
        return 0.0 if not n else (n - 1) / ANIM_FPS

    def play(self, rec, fmt, loop, blend, rate, now):
        """800ADA10: pick the variant (falling back to lower ones), start it, return length/rate."""
        while True:
            name = fmt % (self._prefix(rec), rec["variant"])
            length = self.length(name)
            if length != 0.0:
                break
            rec["variant"] -= 1
            if rec["variant"] <= 0:
                break
        actual = self.rng.randf(0.25 * blend, blend)          # 8015D350 per pose
        rec["events"].append(dict(time=now, clip=name, loop=loop, blend=actual, rate=rate, phase=0.0))
        return length / rate

    def start(self, phases):
        """Initial clip on every pose (800AD040) with the fn_8015C1BC phase spread."""
        for rec in self.recs:
            name = "%s_idle_%s_1" % (self._prefix(rec), "stand" if self.standing else "sit")
            rec["events"].append(dict(time=0.0, clip=name, loop=True, blend=0.0, rate=1.0,
                                      phase=phases[(rec["type"], rec["pose"])]))

    def react(self, big, excite=0.0):
        """CrowdReactLarge / Small (800AE1F8 -> 800ADF64)."""
        n = len(self.recs)
        pool = list(range(n))
        keep = 10 if big else 3
        for _ in range(max(0, n - keep)):
            pool.pop(self.rng.randint(len(pool)))
        for i in pool:
            self.recs[i]["pending"] = 1
            self.recs[i]["delay"] = self.rng.rand0(1.5 if big else 1.0)
        self.duration = excite

    def _clap_variant(self, rec):
        """800ADC5C: a male clap variant 1..5 not used by other clapping poses of the type."""
        free = [1, 2, 3, 4, 5]
        for other in self.recs:
            if other is rec or other["type"] != rec["type"] or other["female"]:
                continue
            if other["state"] in (1, 2) and other["variant"] in free:
                free.remove(other["variant"])
        if not free:
            return self.rng.randint(5) + 1
        return free[0] if len(free) == 1 else free[self.rng.randint(len(free))]

    def _start_clap(self, rec, now):                           # 800ADE08
        st = rec["state"]
        if st in (1, 2):
            return
        if st == 3 or st >= 7:
            rec["state"] = 2
            rec["timer"] = self.play(rec, "%s_stand_clap_%d", True, 0.2, 1.0, now)
            return
        blend = 0.05 if st == 0 else 0.2
        rec["state"] = 1
        rec["variant"] = self.rng.randint(2) + 1 if rec["female"] else self._clap_variant(rec)
        rate = 1.0 if rec["female"] else 0.5
        fmt = "%s_stand_clap_in_%d" if self.standing else "%s_trans_sit_to_stand_clap_%d"
        rec["timer"] = self.play(rec, fmt, False, blend, 1.0 if self.standing else rate, now)

    def _stop_clap(self, rec, now):                            # 800ADB74
        if rec["state"] == 2:
            rec["state"] = 3
            if self.standing:
                rec["timer"] = self.play(rec, "%s_stand_clap_out_%d", False, 0.0, 1.0, now)
            else:
                rate = 1.0 if rec["female"] else 0.5
                rec["timer"] = self.play(rec, "%s_trans_stand_clap_to_sit_%d", False, 0.0, rate, now)
        elif rec["state"] == 1:
            rec["state"] = 0
            fmt = "%s_idle_stand_%d" if self.standing else "%s_idle_sit_%d"
            self.play(rec, fmt, True, 0.2, 1.0, now)

    def step(self, dt, now):
        if self.duration > 0.0:
            self.duration -= dt
            if self.duration <= 0.0:
                for rec in self.recs:
                    rec["pending"] = 2; rec["delay"] = self.rng.rand0(3.0)
        sit = "stand" if self.standing else "sit"
        for rec in self.recs:
            if rec["pending"]:
                rec["delay"] -= dt
                if rec["delay"] <= 0.0:
                    if rec["pending"] == 1:
                        self._start_clap(rec, now)
                    elif rec["pending"] == 2:
                        self._stop_clap(rec, now)
                    rec["pending"] = 0
            if rec["timer"] > 0.0:
                rec["timer"] -= dt
            st = rec["state"]
            if st == 0:
                rec["idle"] += dt
                if rec["idle"] >= 10.0:
                    rec["idle"] = 0.0
                    if self.rng.rand0(100.0) >= 50.0:
                        rec["state"] = 4
                        rec["variant"] = 1 if self.standing else self.rng.randint(2) + 1
                        rec["timer"] = self.play(rec, "%%s_%s_bored_in_%%d" % sit, False, 0.0, 1.0, now)
            elif st == 1 and rec["timer"] <= 0.0:
                rec["state"] = 2
                self.play(rec, "%s_stand_clap_%d", True, 0.0, 1.0, now)
            elif st in (3, 6) and rec["timer"] <= 0.0:
                rec["state"] = 0
                self.play(rec, "%%s_idle_%s_%%d" % sit, True, 0.0, 1.0, now)
            elif st == 4 and rec["timer"] <= 0.0:
                rec["state"] = 5
                self.play(rec, "%%s_%s_bored_%%d" % sit, True, 0.0, 1.0, now)
                rec["timer"] = self.rng.randf(3.0, 6.0)
            elif st == 5 and rec["timer"] <= 0.0:
                rec["state"] = 6
                rec["timer"] = self.play(rec, "%%s_%s_bored_out_%%d" % sit, False, 0.0, 1.0, now)

    def run(self, seconds, reactions=()):
        """Simulate at 60 Hz; ``reactions`` = [(time, big, excite seconds)]."""
        pending = sorted(reactions)
        steps = int(math.ceil(seconds * FPS_GAME)) + 1
        for i in range(steps):
            now = i / FPS_GAME
            while pending and pending[0][0] <= now:
                _, big, excite = pending.pop(0); self.react(big, excite)
            self.step(1.0 / FPS_GAME, now)
        return {(r["type"], r["pose"]): r["events"] for r in self.recs}


def pose_phases(ntypes=len(ROSTER), nposes=POSES):
    """8015C1BC: phase(type t, pose j) = frac(j / (4 * poses) + t / (4 * types))."""
    out = {}
    for t in range(ntypes):
        base = f32(f32(t) * f32(f32(1.0) / f32(f32(4.0) * f32(ntypes))))
        for j in range(nposes):
            ph = f32(f32(f32(j) / f32(4 * nposes)) + base)
            while ph > 1.0:
                ph = f32(ph - f32(1.0))
            out[(t, j)] = float(ph)
    return out


# --------------------------------------------------------------------------------------------
# Clip sampling on the crowdanim rig
class ClipSampler:
    def __init__(self, section):
        import nlg_cutscene as cut
        self.cut = cut
        self.rig = section.animations.rigs[0]
        self.clips = {c.name: c for c in section.animations.clips}
        self.values = {}

    def keys(self):
        return {n: c.frames for n, c in self.clips.items()}

    def _vals(self, name):
        if name not in self.values:
            self.values[name] = self.cut.clip_values(self.clips[name])
        return self.values[name]

    def local(self, name, u, loop):
        """Local (t, q xyzw, s) per node at fractional key ``u``."""
        clip = self.clips[name]; vals = self._vals(name); n = clip.frames
        if loop and n > 1:
            u = u % (n - 1)
        else:
            u = min(max(u, 0.0), n - 1)
        i = int(math.floor(u)); a = u - i; j = min(i + 1, n - 1)
        cut = self.cut
        out = []
        for node in range(self.rig.node_count):
            def get(kind, default):
                v = vals.get((node, kind))
                if v is None:
                    return default
                if len(v) == 1:
                    return tuple(v[0])
                return _mix(kind == cut.ROTATION, v[min(i, len(v) - 1)], v[min(j, len(v) - 1)], a)
            out.append((get(cut.TRANSLATION, tuple(self.rig.translations[node])),
                        get(cut.ROTATION, (0.0, 0.0, 0.0, 1.0)), get(cut.SCALE, (1.0, 1.0, 1.0))))
        return out

    def at(self, events, t):
        """Local pose of a timeline at time ``t`` (seconds), with the engine's crossfade."""
        cur = None; prev = None
        for e in events:
            if e["time"] <= t + 1e-9:
                prev, cur = cur, e
        if cur is None:
            cur = events[0]
        pose = self._event_pose(cur, t)
        if prev is not None and cur["blend"] > 0.0 and t - cur["time"] < cur["blend"]:
            w = (t - cur["time"]) / cur["blend"]
            old = self._event_pose(prev, t)
            pose = [(_mix(False, o[0], p[0], w), _mix(True, o[1], p[1], w), _mix(False, o[2], p[2], w))
                    for o, p in zip(old, pose)]
        return pose

    def _event_pose(self, e, t):
        n = self.clips[e["clip"]].frames
        u = (t - e["time"]) * e["rate"] * ANIM_FPS + e["phase"] * max(n - 1, 1)
        return self.local(e["clip"], u, e["loop"])


def _mix(rot, a, b, w):
    if not rot:
        return tuple(x + (y - x) * w for x, y in zip(a, b))
    d = sum(x * y for x, y in zip(a, b))
    if d < 0:
        b = tuple(-y for y in b); d = -d
    if d > 0.9995:
        q = tuple(x + (y - x) * w for x, y in zip(a, b))
    else:
        th = math.acos(min(1.0, d)); s = math.sin(th)
        ka, kb = math.sin((1 - w) * th) / s, math.sin(w * th) / s
        q = tuple(ka * x + kb * y for x, y in zip(a, b))
    n = math.sqrt(sum(x * x for x in q)) or 1.0
    return tuple(x / n for x in q)


# --------------------------------------------------------------------------------------------
# RGB5A3 render-target copy (EFB -> texture, Dolphin's > 0.878 alpha rule)
def rgb5a3_quantize(rgba):
    a = rgba[..., 3].astype(np.int32)
    c = rgba[..., :3].astype(np.int32)
    opaque = a >= 224
    c5 = c >> 3; c5 = (c5 << 3) | (c5 >> 2)
    c4 = (c >> 4) * 17
    a3 = a >> 5; a3 = (a3 << 5) | (a3 << 2) | (a3 >> 1)
    out = np.empty_like(rgba)
    out[..., :3] = np.where(opaque[..., None], c5, c4)
    out[..., 3] = np.where(opaque, 255, a3)
    return out


def view_index(t, pose, bucket_index):
    return (t * POSES + pose) * ANGLES + bucket_index


def atlas_shape(ntypes=len(ROSTER)):
    n = ntypes * POSES * ANGLES
    rows = (n + ATLAS_COLS - 1) // ATLAS_COLS
    return ATLAS_COLS, rows


# ============================================================================================
# Blender
try:
    import bpy
    from mathutils import Matrix, Vector
except ImportError:          # pure-python use (tests, analysis)
    bpy = None

BILLBOARD_GN = "PO_Crowd_Billboard"
BILLBOARD_MAT = "PO Crowd impostor (litcrowdshader)"


def _billboard_group():
    ng = bpy.data.node_groups.get(BILLBOARD_GN)
    if ng is not None:
        return ng
    ng = bpy.data.node_groups.new(BILLBOARD_GN, "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Camera", in_out="INPUT", socket_type="NodeSocketObject")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    N, L = ng.nodes, ng.links
    gi, go = N.new("NodeGroupInput"), N.new("NodeGroupOutput")
    info = N.new("GeometryNodeObjectInfo"); info.transform_space = "ORIGINAL"
    L.new(gi.outputs["Camera"], info.inputs["Object"])

    def attr(name):
        n = N.new("GeometryNodeInputNamedAttribute"); n.data_type = "FLOAT_VECTOR"
        n.inputs["Name"].default_value = name
        return n.outputs["Attribute"]

    def axis(vec):
        td = N.new("FunctionNodeTransformDirection")
        td.inputs["Direction"].default_value = vec
        L.new(info.outputs["Transform"], td.inputs["Transform"])
        nz = N.new("ShaderNodeVectorMath"); nz.operation = "NORMALIZE"
        L.new(td.outputs[0], nz.inputs[0])
        return nz.outputs[0]

    def vmath(op, a, b):
        n = N.new("ShaderNodeVectorMath"); n.operation = op
        L.new(a, n.inputs[0]); L.new(b, n.inputs[1])
        return n.outputs[0]

    def scale(v, s):
        n = N.new("ShaderNodeVectorMath"); n.operation = "SCALE"
        L.new(v, n.inputs[0]); L.new(s, n.inputs["Scale"])
        return n.outputs[0]

    ext = vmath("MULTIPLY", attr("po_corner"), attr("po_half"))
    sep = N.new("ShaderNodeSeparateXYZ"); L.new(ext, sep.inputs[0])
    right = scale(axis((1.0, 0.0, 0.0)), sep.outputs[0])
    up = scale(axis((0.0, 1.0, 0.0)), sep.outputs[1])
    pos = vmath("ADD", attr("po_center"), vmath("ADD", right, up))
    sp = N.new("GeometryNodeSetPosition")
    L.new(gi.outputs["Geometry"], sp.inputs["Geometry"]); L.new(pos, sp.inputs["Position"])
    L.new(sp.outputs[0], go.inputs[0])
    return ng


def _billboard_material(image, cols, rows):
    """litcrowdshader: C = RASC x TEXC, A = RASA x TEXA; alpha test GREATER 0x80, then blend."""
    mat = bpy.data.materials.get(BILLBOARD_MAT) or bpy.data.materials.new(BILLBOARD_MAT)
    mat.use_nodes = True
    nt = mat.node_tree; nt.nodes.clear(); N, L = nt.nodes, nt.links

    def math_node(op, a, b=None, clamp=False):
        n = N.new("ShaderNodeMath"); n.operation = op; n.use_clamp = clamp
        for i, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, (int, float)):
                n.inputs[i].default_value = float(v)
            else:
                L.new(v, n.inputs[i])
        return n.outputs[0]

    uv = N.new("ShaderNodeUVMap"); uv.uv_map = "UV"
    tile = N.new("ShaderNodeAttribute"); tile.attribute_type = "GEOMETRY"; tile.attribute_name = "po_tile"
    sp = N.new("ShaderNodeSeparateXYZ"); L.new(uv.outputs[0], sp.inputs[0])
    tl = N.new("ShaderNodeSeparateXYZ"); L.new(tile.outputs["Vector"], tl.inputs[0])
    w, h = TARGET_SIZE
    # GX CLAMP on a 64x128 target: sample at most half a texel from its edge.
    s = math_node("MINIMUM", math_node("MAXIMUM", sp.outputs[0], 0.5 / w), 1.0 - 0.5 / w)
    t = math_node("MINIMUM", math_node("MAXIMUM", sp.outputs[1], 0.5 / h), 1.0 - 0.5 / h)
    u = math_node("DIVIDE", math_node("ADD", tl.outputs[0], s), float(cols))
    # tile rows count from the top of the atlas; Blender v runs bottom-up
    v = math_node("DIVIDE", math_node("ADD", math_node("SUBTRACT", float(rows - 1), tl.outputs[1]), t), float(rows))
    co = N.new("ShaderNodeCombineXYZ"); L.new(u, co.inputs[0]); L.new(v, co.inputs[1])
    tex = N.new("ShaderNodeTexImage"); tex.image = image; tex.interpolation = "Linear"
    tex.extension = "EXTEND"
    L.new(co.outputs[0], tex.inputs["Vector"])
    if image is not None and image.source == "SEQUENCE":
        iu = tex.image_user
        iu.frame_start = 1; iu.frame_offset = 0; iu.use_auto_refresh = True
        iu.frame_duration = int(image.get("po_frames", 1))
    vc = N.new("ShaderNodeVertexColor"); vc.layer_name = "PO_Color0"
    col = N.new("ShaderNodeVectorMath"); col.operation = "MULTIPLY"
    L.new(vc.outputs["Color"], col.inputs[0]); L.new(tex.outputs["Color"], col.inputs[1])
    alpha = math_node("MULTIPLY", vc.outputs["Alpha"], tex.outputs["Alpha"])
    passed = math_node("GREATER_THAN", alpha, 128.0 / 255.0)
    fac = math_node("MULTIPLY", alpha, passed)
    emit = N.new("ShaderNodeEmission"); L.new(col.outputs[0], emit.inputs["Color"])
    tr = N.new("ShaderNodeBsdfTransparent")
    mix = N.new("ShaderNodeMixShader")
    L.new(fac, mix.inputs[0]); L.new(tr.outputs[0], mix.inputs[1]); L.new(emit.outputs[0], mix.inputs[2])
    out = N.new("ShaderNodeOutputMaterial"); L.new(mix.outputs[0], out.inputs["Surface"])
    if hasattr(mat, "surface_render_method"):
        # Z write is on in GX (8015BA1C): dithered keeps depth; blending converges with samples.
        mat.surface_render_method = "DITHERED"
    else:
        mat.blend_method = "HASHED"
    mat.use_backface_culling = False
    mat["po_gx_program"] = json.dumps({"shader": "litcrowdshader", "C": "RASC*TEXC", "A": "RASA*TEXA",
                                       "alpha_compare": "GREATER 0x80", "blend": "SRCALPHA, INVSRCALPHA",
                                       "zwrite": True, "cull": "NONE", "draw": "8015BA1C / 80159F18"})
    return mat


def add_facing_modifier(obj, camera):
    """Geometry-nodes modifier that turns po_center/po_corner/po_half quads toward ``camera``."""
    mod = obj.modifiers.new("Face camera (8015A4C8)", "NODES"); mod.node_group = _billboard_group()
    for item in mod.node_group.interface.items_tree:
        if getattr(item, "name", "") == "Camera" and item.in_out == "INPUT":
            try:
                mod[item.identifier] = camera
            except TypeError:               # Blender 5.x moved modifier inputs to .properties
                mod.properties.inputs[item.identifier]["value"] = camera
    return mod


def build_billboards(members, settings, tint, collection, camera, image=None, name="PO Crowd impostors"):
    """One mesh: four corners per member, oriented to ``camera`` by Geometry Nodes."""
    s = settings["size"]; hw, hh = 0.25 * settings["width"] * s, 0.5 * settings["height"] * s
    verts, faces = [], []
    centers, corners, halves, tiles, colors, uvs = [], [], [], [], [], []
    cols, rows = atlas_shape()
    corner_uv = ((1, 1, 1, 0), (-1, 1, 0, 0), (-1, -1, 0, 1), (1, -1, 1, 1))     # 8015A4C8
    for m in members:
        x, y, z = m["pos"]
        c = (x, y, z + hh)
        k = view_index(m["type"], m["pose"], m["bucket"] // (65536 // ANGLES))
        tile = (k % cols, k // cols, 0.0)
        rgba = tuple(v / 255.0 for v in tint.sample(x, y)) if tint else (1.0, 1.0, 1.0, 1.0)
        base = len(verts)
        for sx, sy, su, st in corner_uv:
            verts.append(c); centers.append(c); corners.append((sx, sy, 0.0)); halves.append((hw, hh, 0.0))
            uvs.append((su, 1.0 - st)); colors.append(rgba); tiles.append(tile)
        faces.append((base, base + 1, base + 2, base + 3))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces); me.update()
    for attr, data in (("po_center", centers), ("po_corner", corners), ("po_half", halves)):
        a = me.attributes.new(attr, "FLOAT_VECTOR", "POINT")
        a.data.foreach_set("vector", [v for p in data for v in p])
    uvl = me.uv_layers.new(name="UV")
    loops = me.loops
    uvl.data.foreach_set("uv", [v for li in range(len(loops)) for v in uvs[loops[li].vertex_index]])
    ca = me.color_attributes.new("PO_Color0", "FLOAT_COLOR", "CORNER")
    ca.data.foreach_set("color", [v for li in range(len(loops)) for v in colors[loops[li].vertex_index]])
    ta = me.attributes.new("po_tile", "FLOAT_VECTOR", "CORNER")
    ta.data.foreach_set("vector", [v for li in range(len(loops)) for v in tiles[loops[li].vertex_index]])
    obj = bpy.data.objects.new(name, me); collection.objects.link(obj)
    add_facing_modifier(obj, camera)
    me.materials.append(_billboard_material(image, cols, rows))
    obj["po_crowd"] = json.dumps({"members": len(members), "settings": {k: v for k, v in settings.items()},
                                  "atlas": [cols, rows], "tile": list(TARGET_SIZE)})
    return obj


def crowd_image(directory, frames):
    """Image-sequence datablock for baked atlases ``impostors_####.png`` (frame = Blender frame)."""
    first = os.path.join(directory, "impostors_0001.png")
    img = bpy.data.images.get("PO crowd impostors")
    if img is None:
        if not os.path.isfile(first):
            _placeholder(first)
        img = bpy.data.images.load(first, check_existing=True)
        img.name = "PO crowd impostors"
    img.source = "SEQUENCE"
    img.colorspace_settings.name = "Non-Color"
    img.alpha_mode = "CHANNEL_PACKED"
    img["po_frames"] = int(frames)
    return img


def _placeholder(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols, rows = atlas_shape(); w, h = TARGET_SIZE
    arr = np.zeros((rows * h, cols * w, 4), np.uint8)
    _write_png(path, arr)


def _write_png(path, arr):
    import zlib
    h, w = arr.shape[:2]
    raw = b"".join(b"\x00" + arr[y].tobytes() for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def build(scene, helpers, files_root, arena, collection, camera, seed=0x12345678, frames=None,
          image_dir=None, names=None, keep_helpers=None):
    """Lay out the crowd, build the billboards and store what the impostor bake needs.

    ``keep_helpers`` (a set of region names) drops the other regions' members after the whole
    layout has run, so the RNG stream and the kept members are identical to the full layout."""
    settings = circuit_settings(files_root, arena)
    art = Path(files_root) / "art"
    rng = GameRNG(seed)
    members = layout(helpers, settings, rng)
    if keep_helpers is not None:
        members = [m for m in members if m["helper"] in keep_helpers]
    tint = TintMap.from_global(art, settings["light_map"], names)
    frames = frames or (scene.frame_end - scene.frame_start + 1)
    img = crowd_image(image_dir, frames) if image_dir else None
    obj = build_billboards(members, settings, tint, collection, camera, img)
    jit = view_jitters(rng)
    obj["po_crowd_bake"] = json.dumps({"files_root": str(files_root), "arena": arena, "seed": seed,
                                       "rng_after_layout": rng.state, "image_dir": image_dir,
                                       "jitter": {"%d,%d,%d" % k: v for k, v in jit.items()},
                                       "used_views": sorted({view_index(m["type"], m["pose"],
                                                             m["bucket"] // (65536 // ANGLES)) for m in members}),
                                       "camera": camera.name if camera else None})
    return obj, members


# --------------------------------------------------------------------------------------------
# Impostor bake
def _look_at(eye, target, up):
    f = (target - eye).normalized()
    r = f.cross(up).normalized()
    u = r.cross(f)
    m = Matrix.Identity(4)
    m.col[0][:3] = r; m.col[1][:3] = u; m.col[2][:3] = -f; m.col[3][:3] = eye
    return m


def _load_type(doc, section, collection, label, mats, imgs, load_textures=True):
    import io_punchout_asset as asset
    from io_punchout_animation import bind_armature
    ms = section.model_sets[0]
    arm, bone_names = bind_armature(doc, ms, label + " rig", collection)
    meshes = []
    for mesh in ms.meshes:
        meta = doc.mesh_metadata(section.index, ms, mesh)
        md = asset._build_mesh(doc, section.index, ms, mesh, meta, "%s mesh %d" % (label, mesh.index))
        md.materials.append(asset._material(doc, section.index, ms, mesh, meta, mats, imgs, load_textures))
        obj = bpy.data.objects.new("%s mesh %d" % (label, mesh.index), md); collection.objects.link(obj)
        obj.matrix_world = asset._blender_matrix(ms.transforms[mesh.transform])
        obj.parent = arm; obj.matrix_parent_inverse = Matrix.Identity(4)
        if mesh.palette:
            asset._skin(obj, mesh, bone_names, arm)
        meshes.append(obj)
    return arm, bone_names, meshes


def _key_timeline(arm, bone_nodes, sampler, events, frames, name):
    """Key an armature from a behaviour timeline: bone pose = node world (rest-relative)."""
    import po_action
    from mathutils import Quaternion
    rig = sampler.rig
    order = sampler.cut.order(list(rig.parents))
    rest = {b: arm.data.bones[b].matrix_local.copy() for b in bone_nodes}
    parent = {b: (arm.data.bones[b].parent.name if arm.data.bones[b].parent else None) for b in bone_nodes}
    rows = {b: ([], []) for b in bone_nodes}
    for f in frames:
        t = (f - 1) / 30.0
        local = sampler.at(events, t)
        world = [None] * rig.node_count
        for n in order:
            tt, q, s = local[n]
            m = Matrix.LocRotScale(Vector(tt), Quaternion((q[3], q[0], q[1], q[2])), Vector(s))
            world[n] = m if rig.parents[n] < 0 else world[rig.parents[n]] @ m
        for b, n in bone_nodes.items():
            p = parent[b]
            if p is None or p not in bone_nodes:
                basis = rest[b].inverted() @ world[n]
            else:
                basis = rest[b].inverted() @ rest[p] @ world[bone_nodes[p]].inverted() @ world[n]
            loc, rot, _ = basis.decompose()
            rows[b][0].append(tuple(loc)); rows[b][1].append((rot.w, rot.x, rot.y, rot.z))
    action, slot = po_action.new_action(arm, name)
    for b in bone_nodes:
        pb = arm.pose.bones[b]; pb.rotation_mode = "QUATERNION"
        base = pb.path_from_id()
        quats, prev = [], None
        for q in rows[b][1]:
            if prev and sum(x * y for x, y in zip(prev, q)) < 0:
                q = tuple(-x for x in q)
            quats.append(q); prev = q
        for path, data in ((base + ".location", rows[b][0]), (base + ".rotation_quaternion", quats)):
            for k in range(len(data[0])):
                fc = po_action.new_fcurve(action, path, index=k, group=b, slot=slot)
                fc.keyframe_points.add(len(frames))
                fc.keyframe_points.foreach_set("co", [v for f, r in zip(frames, data) for v in (f, r[k])])
                for kp in fc.keyframe_points:
                    kp.interpolation = "LINEAR"
                fc.update()
    return action, slot


def setup_bake_scene(main_scene, crowd_obj, frames, names=None, reactions=(), load_textures=True):
    """Create the hidden impostor scene: 3 characters x 4 poses x 4 yaw buckets with cameras."""
    sc = bpy.data.scenes.get("PO crowd impostor bake") or bpy.data.scenes.new("PO crowd impostor bake")
    # The armature/mesh importers work on the active view layer, so build inside the bake scene.
    with bpy.context.temp_override(scene=sc, view_layer=sc.view_layers[0]):
        return _setup_bake_scene(sc, main_scene, crowd_obj, frames, names, reactions, load_textures)


def _setup_bake_scene(sc, main_scene, crowd_obj, frames, names, reactions, load_textures):
    import nlg_asset, nlg_hash, po_action, po_gx
    info = json.loads(crowd_obj["po_crowd_bake"])
    files_root = Path(info["files_root"]); art = files_root / "art"
    names = names if names is not None else nlg_hash.load_hashid_bin(str(art / "hashid.bin"))
    settings = circuit_settings(files_root, info["arena"])
    for o in list(sc.collection.all_objects):
        bpy.data.objects.remove(o)
    sc.frame_start, sc.frame_end = main_scene.frame_start, main_scene.frame_end
    col = sc.collection
    anim_doc = nlg_asset.AssetDocument(art / "characters" / "crowdanim.dict", names)
    sampler = ClipSampler(anim_doc.sections[0])
    rng = GameRNG(int(info["rng_after_layout"]))
    view_jitters(rng)                                          # consume the view draws again
    female = [n.lower().startswith("crowd_female") for n in ROSTER]
    beh = Behaviour(rng, sampler.keys(), female)
    beh.start(pose_phases())
    seconds = (max(frames) - 1) / 30.0 + 0.1
    timelines = beh.run(seconds, reactions)
    jitter = {tuple(int(x) for x in k.split(",")): v for k, v in info["jitter"].items()}
    used = set(info["used_views"])
    rim_to = settings["rim_ramp"]
    views = []
    for t, cname in enumerate(ROSTER):
        doc = nlg_asset.AssetDocument(art / "characters" / (cname.lower() + ".dict"), names)
        tw = character_tweaks(files_root, cname)
        swap = {}
        if rim_to:
            swap[nlg_hash.string_to_hash("%s/crowdrimlightramp" % cname.lower())] = nlg_hash.string_to_hash(rim_to)
        mats, imgs, built = {}, {}, set()
        builder = po_gx.SceneBuilder(names, {"crowd_light": "po_crowd_light", "texture_swap": swap})
        for p in range(POSES):
            if not any(view_index(t, p, b) in used for b in range(ANGLES)):
                continue
            template = None
            action = None
            for b in range(ANGLES):
                k = view_index(t, p, b)
                if k not in used:
                    continue
                label = "%s p%d b%d" % (cname, p, b)
                arm, bone_names, meshes = _load_type(doc, doc.sections[0], col, label, mats, imgs, load_textures)
                nodes = {bn: sampler.rig.hashes.index(h) for h, bn in bone_names.items() if h in sampler.rig.hashes}
                if action is None:
                    action, slot = _key_timeline(arm, nodes, sampler, timelines[(t, p)], frames, "%s pose %d" % (cname, p))
                else:
                    po_action.assign_action(arm, action, slot)
                yaw = (b * (65536 // ANGLES)) * 2 * math.pi / 65536 + math.radians(VIEW_JITTER_DEG * jitter[(t, p, b)])
                offset = Vector((k * 40.0, 0.0, 0.0))
                R = Matrix.Rotation(yaw, 4, "Z"); S = Matrix.Diagonal((*tw["scale"], 1.0))
                arm.matrix_world = Matrix.Translation(offset) @ R @ S
                light = (R @ S).to_3x3() @ Vector(CROWD_LIGHT_WORLD)
                light.normalize()
                for m in meshes:
                    m["po_crowd_light"] = tuple(light)
                for m in meshes:
                    for ms_ in m.material_slots:
                        if ms_.material and ms_.material.name not in built:
                            builder.material(ms_.material, m); built.add(ms_.material.name)
                    po_gx.ensure_world_normals(m)
                cam_data = bpy.data.cameras.new("PO impostor cam %d" % k)
                cam_data.sensor_fit = "VERTICAL"; cam_data.angle_y = FOVY
                cam_data.clip_start, cam_data.clip_end = NEAR, 20.0
                cam = bpy.data.objects.new("PO impostor cam %02d" % k, cam_data); col.objects.link(cam)
                views.append(dict(index=k, camera=cam, offset=offset, tweaks=tw))
    r = sc.render
    r.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in r.bl_rna.properties["engine"].enum_items.keys() else "BLENDER_EEVEE"
    r.resolution_x, r.resolution_y = TARGET_SIZE; r.resolution_percentage = 100
    r.film_transparent = True; r.filter_size = 0.0
    r.image_settings.file_format = "PNG"; r.image_settings.color_mode = "RGBA"; r.image_settings.color_depth = "8"
    sc.eevee.taa_render_samples = 1
    po_gx.scene_output(sc)
    return sc, views


def bake(main_scene, crowd_obj, frames=None, out_dir=None, reactions=(), log=print):
    """Render every used impostor view at ``frames`` and write RGB5A3 atlases.

    Every scene frame gets an atlas file: a rendered frame's atlas is held for the frames up to
    the next rendered one (and before the first), so any subset of frames gives a full sequence.
    The views depend on the camera direction, so hold spans should not cross a hard camera cut."""
    import shutil
    first, last = main_scene.frame_start, main_scene.frame_end
    frames = sorted(set(frames or range(first, last + 1)))
    info = json.loads(crowd_obj["po_crowd_bake"])
    out_dir = out_dir or info.get("image_dir")
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, "_views"); os.makedirs(tmp, exist_ok=True)
    sc, views = setup_bake_scene(main_scene, crowd_obj, frames, reactions=reactions)
    cam_main = main_scene.camera
    cols, rows = atlas_shape(); w, h = TARGET_SIZE
    import time
    t0 = time.time()
    for i, f in enumerate(frames):
        main_scene.frame_set(f)
        cm = cam_main.matrix_world
        fwd = -(cm.col[2].to_3d()).normalized(); up = cm.col[1].to_3d().normalized()
        sc.frame_set(f)
        atlas = np.zeros((rows * h, cols * w, 4), np.uint8)
        for v in views:
            tw = v["tweaks"]
            target = v["offset"] + Vector((0.0, 0.0, tw["lookat_z"]))
            eye = target - fwd * tw["distance"]
            v["camera"].matrix_world = _look_at(eye, target, up)
            sc.camera = v["camera"]
            path = os.path.join(tmp, "v%02d.png" % v["index"])
            sc.render.filepath = path
            bpy.ops.render.render(write_still=True, scene=sc.name)
            img = _read_png(path)
            k = v["index"]; cx, cy = k % cols, k // cols
            atlas[cy * h:(cy + 1) * h, cx * w:(cx + 1) * w] = img
        target_file = os.path.join(out_dir, "impostors_%04d.png" % f)
        _write_png(target_file, rgb5a3_quantize(atlas))
        hold = list(range(first, f)) if i == 0 else []                  # frames before the first bake
        hold += range(f + 1, frames[i + 1] if i + 1 < len(frames) else last + 1)
        for g in hold:
            shutil.copyfile(target_file, os.path.join(out_dir, "impostors_%04d.png" % g))
        if i % 10 == 0:
            log("crowd impostors: frame %d (%d/%d) %.1fs" % (f, i + 1, len(frames), time.time() - t0))
    main_scene.frame_set(first)
    refresh_image(crowd_obj)
    return out_dir


def refresh_image(crowd_obj):
    """Point the billboard material at the baked sequence (one atlas file per scene frame)."""
    mat = crowd_obj.data.materials[0] if crowd_obj.data.materials else None
    if mat is None:
        return
    for node in mat.node_tree.nodes:
        if node.type != "TEX_IMAGE" or node.image is None:
            continue
        img = node.image
        img.source = "SEQUENCE"
        img.reload()
        scene = bpy.context.scene
        iu = node.image_user
        iu.frame_start = 1; iu.frame_offset = scene.frame_start - 1
        iu.frame_duration = scene.frame_end - scene.frame_start + 1
        iu.use_auto_refresh = True; iu.use_cyclic = False
        img["po_frames"] = iu.frame_duration
    crowd_obj["po_crowd_baked"] = True


def _read_png(path):
    img = bpy.data.images.load(path, check_existing=False)
    img.colorspace_settings.name = "Non-Color"
    try:
        w, h = img.size
        px = np.empty(w * h * 4, np.float32); img.pixels.foreach_get(px)
        arr = np.round(px.reshape(h, w, 4)[::-1] * 255.0).astype(np.uint8)
    finally:
        bpy.data.images.remove(img)
    return arr
