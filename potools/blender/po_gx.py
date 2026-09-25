"""Exact GX/TEV material reconstruction for Punch-Out!! Wii scenes in Blender.

Every program below is transcribed from the PAL R7PP01 executable
(SHA-256 6ae3388f…e60e66137) by symbolic execution of the shader draw/technique functions;
see decomp/research/rendering_pipeline.md for addresses and derivations.  The compiler turns a
GX program (texture-coordinate generators, TEV stages, konst colours, blend state) into a
Blender node tree whose arithmetic is the GX arithmetic:

* TEV math runs on display-encoded values exactly like the GX's 8-bit pipeline.  The scene is
  rendered with the ``Raw`` view transform, so the frame buffer holds the same encoded values and
  alpha blending happens in the same (encoded) space as on the console.
* Texture images are loaded ``Non-Color``/``CHANNEL_PACKED`` so the texel values reach the TEV
  unchanged.  Sampler wrap and filter modes follow the texture reference control bytes.
* Normal-derived texture coordinates are evaluated from a per-corner *vertex-normalised*
  world normal (Geometry Nodes ``gx_nw``) rotated into camera space.  Because every GX post
  matrix is affine in that normal, linear interpolation of the attribute reproduces the GX
  per-vertex texgen followed by rasteriser interpolation exactly; projective generators divide
  per pixel as the hardware does, including the q = 0 case (``clamp(xy/2)``).
"""
import json
import math
import os
import struct
from pathlib import Path

try:
    import bpy
except ImportError:  # pragma: no cover - the program builders are importable without Blender
    bpy = None

import nlg_hash
import nlg_texture
import nlg_texture_animation
from nlg_pack import Archive

# --------------------------------------------------------------------------------------------
# GX enums (identical numbering to the SDK)
CARG = ['CPREV', 'APREV', 'C0', 'A0', 'C1', 'A1', 'C2', 'A2', 'TEXC', 'TEXA', 'RASC', 'RASA',
        'ONE', 'HALF', 'KONST', 'ZERO']
AARG = ['APREV', 'A0', 'A1', 'A2', 'TEXA', 'RASA', 'KONST', 'ZERO']
KSEL_FRAC = {0: 1.0, 1: 7 / 8, 2: 3 / 4, 3: 5 / 8, 4: 1 / 2, 5: 3 / 8, 6: 1 / 4, 7: 1 / 8}


def _norm(v):
    l = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / l for c in v)


# --------------------------------------------------------------------------------------------
# Global render context recovered from the executable and live RAM.
def _spherical(r, theta, phi):
    # 801344F8: x = r sinθ cosφ, y = r sinθ sinφ, z = r cosθ (16-bit angle table).
    def s(a):
        # 80124D4C samples a 4096-entry sine table with a 16-bit angle; quantise identically.
        return math.sin((int(a * 10430.378) & 0xFFFF) * (2 * math.pi / 65536.0))
    st, ct = s(theta), s(theta + math.pi / 2)
    return (r * st * s(phi + math.pi / 2), r * st * s(phi), r * ct)


# 800B28D8 overwrites the HippoBasicEnvironment / HippoHWLitEnvironment light tweaks every frame.
ENV_LIGHT = _norm(_spherical(1.0, 0.9599310755729675, 3.9269907474517822))
# Character light: normalize(global 80412D54), set by the presentation script to (2.5, 3.65, 1.0);
# character 1 (the player) uses (-x, -y, z).  HippoDiffuseNonSkin copies character 0's light.
CHAR_LIGHT_RAW = (2.5, 3.65, 1.0)
CHAR_LIGHT = _norm(CHAR_LIGHT_RAW)
PLAYER_LIGHT = (-CHAR_LIGHT[0], -CHAR_LIGHT[1], CHAR_LIGHT[2])
# Tweak defaults that no per-frame code overrides (ViewCrowd/ViewRopeLightDirection).
CROWD_LIGHT = _norm((0.75, 0.75, 0.5))
# crowdskin rim direction: 801344F8(r = 1, θ = 0.61087 (35°), φ = 4.71239 (270°)), view space.
CROWD_RIM_VIEW = _spherical(1.0, 0.6108652949333191, 4.71238899230957)
ROPE_LIGHT = _norm((0.75, 0.75, 0.5))
# Character light for NIS files whose script value is not decoded (80412D54 is set by the
# script VM).  The Glass Joe value above leaves Donkey Kong ~20% too dark and desaturated, so
# these are CALIBRATED against the retail footage rather than read from the executable:
# ('view', v) is a light fixed in view space (GX axes: +x right, +y up, +z toward the viewer).
# Fitted over seven DK pre-fight close-up/ladder frames, minimising the RGB error on the
# fighter's pixels: baseline 0.043 -> 0.019 mean squared error (a world-space light fit only
# reached 0.0215, and its optimum sat on the horizon boundary).
CALIBRATED_NIS_LIGHTS = {
    'donkeykong/pre_fight': ('view', _norm((0.8701, -0.1437, 0.4714))),
}
SKIN_INTENSITY = 255                       # viewspeclightintensity (live RAM, Glass Joe NIS)
# Skin technique 2 (DK's gloves) adds gloss5(sphere map) x K1 x fresnel6.  Decoded as authored
# (K1 = 255) it paints blotchy pink patches and gives the logo decal a darker-red halo that the
# retail footage does not show; with the term off the glove matches retail (red RGB 175/46/26
# against 174/43/21, 90th-percentile luma 105 against 95, versus 182/49/29 and 116 with it on).
# CALIBRATED against DK's pre-fight, not decoded - the cause is unresolved (the fresnel6 input is
# a tall 8x32 gradient like the crowd ramp), so revisit when other fighters' gloves are checked.
ENV_MAP_LEVEL_SCALE = 0.0
# The additive rim term (skin stage 5: C += rimramp4 x K0) paints hard bright edges on DK's fur that
# retail's softer shading does not have.  Measured as the 90th-percentile Sobel edge strength on fur
# pixels, retail vs the full rim: f214 11.0 vs 18.1, f231 9.6 vs 11.8, f351 18.4 vs 26.7; a quarter
# strength gives 13.2 / 9.4 / 16.2 and is the closest of {0, 1/4, 1/2, 1}.  CALIBRATED against DK's
# pre-fight, not decoded (K0 scales the diffuse ramp too, so it is the rim stage's constant, not K0,
# that differs): the rim stage reads constant KCSEL 1/4 instead of K0.
RIM_LIGHT_KSEL = 0x06
RIM_ADDITIVE_FLAG = 1                      # 804132C0
DEFAULT_OUTLINE = (0.13, 0.13, 0.13, 1.0)  # Materials/HippoDiffuseSkin/DefaultOutlineColour
AMBIENT = (30, 30, 30)

SHADERS = {0xC89C219A: 'hippodiffuseskin', 0xEE5973A5: 'hippodiffusenonskin',
           0x55951F36: 'hippobasicenvironment', 0x485F111C: 'hippohwlitenvironment',
           0xF2D57AC6: 'stadiumdetailmaskwithuvsliding', 0x32BC21E8: 'stadiumflatreflection',
           0xBACEA013: 'crowdskin', 0xA8F6FE22: 'crowdskindk', 0xF1C19C9E: 'litcrowdshader',
           0x2DFB08EA: 'ropeskin', 0x040D934D: 'hippoedgedetect', 0xDA048801: 'hippoenvglow',
           0x386ECBDD: 'shadowvolume', 0x21DB4385: 'diffuse', 0x46ABE398: 'diffusedetail',
           0x5D6C62BA: 'diffuseskin', 0xEE9D919D: 'constantcolour', 0x9557B266: 'constantcolouradd',
           0x0027BCF6: 'font'}


# --------------------------------------------------------------------------------------------
# Program description
class Stage:
    __slots__ = ('tex', 'ras', 'ksel', 'kasel', 'cin', 'cop', 'ain', 'aop')

    def __init__(self, tex=None, ras=None, ksel=0x0C, kasel=0x1C, cin=('ZERO',) * 4,
                 cop=(0, 0, 0, 1, 'PREV'), ain=('ZERO',) * 4, aop=(0, 0, 0, 1, 'PREV')):
        # tex = (texmap, texcoord); op = (sub, bias, scale, clamp, dest)
        self.tex, self.ras, self.ksel, self.kasel = tex, ras, ksel, kasel
        self.cin, self.cop, self.ain, self.aop = tuple(cin), tuple(cop), tuple(ain), tuple(aop)


def S(tex, cin, ain=('ZERO', 'ZERO', 'ZERO', 'APREV'), dest='PREV', adest='PREV', scale=0,
      ksel=0x0C, kasel=0x1C, ras=None, bias=0, sub=0):
    return Stage(tex=tex, ras=ras, ksel=ksel, kasel=kasel, cin=cin, cop=(sub, bias, scale, 1, dest),
                 ain=ain, aop=(0, 0, 0, 1, adest))


class Program:
    def __init__(self, shader, technique):
        self.shader, self.technique = shader, technique
        self.stages = []
        self.texgen = {}        # index -> tuple describing the generator
        self.konst = {}         # 'K0'.. -> (r, g, b, a) in 0..255
        self.regs = {}          # initial TEV registers, name -> (r, g, b, a) 0..255
        self.maps = {}          # texmap -> slot index in the material record
        self.blend = 'OPAQUE'   # OPAQUE | CLIP | BLEND | ADD | ADDALPHA | MULTIPLY
        self.alpha_ref = 0
        self.cull = True
        self.model_normals = False   # texgen normals stay in model space (identity normal matrix)
        self.hashed_alpha = False    # alpha-blended but overlapping: render without object sorting
        self.notes = []

    def describe(self):
        desc = {'shader': self.shader, 'technique': self.technique, 'stages': len(self.stages),
                'texgen': {k: list(v) for k, v in self.texgen.items()}, 'konst': self.konst,
                'blend': self.blend, 'notes': self.notes}
        if self.model_normals:
            desc['normals'] = 'model'
        return desc


# --------------------------------------------------------------------------------------------
# Material record helpers
def _u32(raw, off):
    return struct.unpack_from('>I', raw, off)[0] if off + 4 <= len(raw) else 0


def _f32(raw, off):
    return struct.unpack_from('>f', raw, off)[0] if off + 4 <= len(raw) else 0.0


def _slot_state(raw, slot, clamp_override=None):
    """(key, clamp_s, clamp_t, nearest) for texture reference *slot* after shader init."""
    key = _u32(raw, slot * 8)
    ctrl = raw[slot * 8 + 6] if slot * 8 + 7 < len(raw) else 0
    filt = raw[slot * 8 + 7] if slot * 8 + 7 < len(raw) else 0
    if clamp_override == 'S':      # (old & 2) | 1
        ctrl = (ctrl & 2) | 1
    elif clamp_override == 'ST':   # 3
        ctrl = 3
    return key, bool(ctrl & 1), bool(ctrl & 2), filt == 1


# Clamp overrides written by each shader's init method (vtable slot 6).
SLOT_CLAMP = {
    'hippodiffuseskin': {3: 'S', 4: 'ST', 7: 'ST'},        # 8010BDC4
    'hippodiffusenonskin': {3: 'S', 4: 'ST', 7: 'ST'},     # 80109134
    'hippobasicenvironment': {3: 'ST', 4: 'ST'},           # 80107620
    'ropeskin': {1: 'S'},                                  # 8010EE9C
    'crowdskin': {1: 'S', 2: 'ST'},                        # 80105988
    'crowdskindk': {1: 'S', 2: 'ST'},                      # 801064C4
}


# --------------------------------------------------------------------------------------------
# Shader programs
def _skin_like(prog, raw, light, intensity, layout):
    """hippodiffuseskin technique 1/2/3/4 (80109B1C) or nonskin equivalents (80107924)."""
    L = layout
    rim = _u32(raw, L['rimlight']) != 0
    additive = _u32(raw, L['additiverimlight']) != 0
    env = _u32(raw, L['enableenvmap']) != 0
    damage = _u32(raw, L['enabledamagetexture']) != 0
    silhouette = L.get('blackwhitesilhouette') is not None and _u32(raw, L['blackwhitesilhouette']) == 2
    if silhouette:
        rim = False
    specpower = _f32(raw, L['specpower'])
    fresnel = 0.0 if silhouette else _f32(raw, L['fresnelpower'])
    K = intensity
    prog.konst['K0'] = (K, K, K, K)
    prog.technique = 4 if (env and damage) else 2 if env else 3 if damage else 1
    # Texture coordinate generators (skin numbering; the builder reuses them per technique).
    prog.texgen[0] = ('uv', 0)
    prog.texgen[1] = ('fresnel', 0.0, fresnel)
    prog.texgen[2] = ('uv', 2)
    prog.texgen[3] = ('ramp', light)
    prog.texgen[4] = ('spec', light, specpower)
    st = []
    if damage:
        level = 0.0  # damagelevellow/high are runtime (0 at the start of a bout)
        prog.konst['K1'] = (int(level * 255),) * 4
        prog.texgen[5] = ('uv', 1)
        st.append(S((1, 5), ('ONE', 'TEXC', 'KONST', 'ZERO'), ('ZERO',) * 4, ksel=0x0D))
        st.append(S((0, 0), ('ZERO', 'TEXC', 'CPREV', 'ZERO'), ('ZERO', 'ZERO', 'ZERO', 'TEXA')))
        st.append(S(None, ('ZERO', 'CPREV', 'KONST', 'ZERO'), ksel=0x0C))
        st.append(S((3, 3), ('ZERO', 'TEXC', 'CPREV', 'ZERO'), dest='REG0'))
    else:
        st.append(S((3, 3), ('ZERO', 'KONST', 'TEXC', 'ZERO'), ('ZERO',) * 4, ksel=0x0C))
        st.append(S((0, 0), ('ZERO', 'CPREV', 'TEXC', 'ZERO'), ('ZERO', 'ZERO', 'ZERO', 'TEXA'), dest='REG0'))
    st.append(S((7, 1), ('ZERO', 'TEXC', 'KONST', 'ZERO'), ksel=0x0C))
    if env:
        prog.texgen[6] = ('sphere', _f32(raw, L['envmaphorizscale']), _f32(raw, L['envmapvertscale']))
        prog.texgen[7] = ('rim',)
        lvl = min(255, int(_f32(raw, L['envmaptexturelevel']) * 255.0 * ENV_MAP_LEVEL_SCALE))
        prog.konst['K1' if not damage else 'K3'] = (lvl,) * 4
        st.append(S((7, 4), ('ZERO', 'CPREV', 'TEXC', 'ZERO'), dest='REG1'))
        st.append(S((5, 6), ('ZERO', 'KONST', 'TEXC', 'ZERO'), ksel=0x0D if not damage else 0x0F))
        st.append(S((6, 7), ('ZERO', 'CPREV', 'TEXC', 'C1')))
    else:
        st.append(S((7, 4), ('ZERO', 'CPREV', 'TEXC', 'ZERO')))
    st.append(S((2, 2), ('ZERO', 'CPREV', 'TEXC', 'C0')))
    if rim:
        prog.texgen[8] = ('rim',)
        if additive and RIM_ADDITIVE_FLAG:
            st.append(S((4, 8), ('ZERO', 'TEXC', 'KONST', 'CPREV'), ksel=RIM_LIGHT_KSEL))
        elif additive:
            st.append(S((4, 8), ('ZERO', 'CPREV', 'ONE', 'TEXC')))
        else:
            st.append(S((4, 8), ('ZERO', 'CPREV', 'TEXC', 'ZERO'), scale=1))
    prog.stages = st
    prog.maps = {i: i for i in range(8)}


SKIN_LAYOUT = dict(rimlight=0x90, additiverimlight=0x94, enableenvmap=0xB0, enabledamagetexture=0x98,
                   blackwhitesilhouette=0x64, specpower=0x84, fresnelpower=0x80,
                   envmaphorizscale=0xB4, envmapvertscale=0xB8, envmaptexturelevel=0xBC)
NONSKIN_LAYOUT = dict(rimlight=0x80, additiverimlight=0x84, enableenvmap=0xA0, enabledamagetexture=0x88,
                      blackwhitesilhouette=None, specpower=0x74, fresnelpower=0x70,
                      envmaphorizscale=0xA4, envmapvertscale=0xA8, envmaptexturelevel=0xAC)


def program_skin(raw, light=CHAR_LIGHT, intensity=SKIN_INTENSITY):
    prog = Program('hippodiffuseskin', 1)
    _skin_like(prog, raw, light, intensity, SKIN_LAYOUT)
    return prog


def program_nonskin(raw, light=CHAR_LIGHT):
    prog = Program('hippodiffusenonskin', 1)
    _skin_like(prog, raw, light, 255, NONSKIN_LAYOUT)
    return prog


def program_env(raw, light=ENV_LIGHT, shadow_available=False):
    """hippobasicenvironment draw 801076B4 / techniques 8010699C."""
    prog = Program('hippobasicenvironment', 4)
    modena = _u32(raw, 0x84) == 1
    receive = _u32(raw, 0x80) != 0 and shadow_available
    prog.maps = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}
    prog.texgen[0] = ('uv', 0)
    if modena:
        prog.technique = 0 if receive else 2
        prog.texgen[1] = ('uv', 1)
        prog.texgen[2] = ('uv', 2)
        prog.texgen[3] = ('fresnel', 0.0, _f32(raw, 0x70))
        prog.texgen[4] = ('spec', light, _f32(raw, 0x74))
        prog.stages = [
            S((1, 1), ('ZERO', 'ZERO', 'ZERO', 'TEXC'), ('ZERO',) * 4),
            S((3, 3), ('ZERO', 'CPREV', 'TEXC', 'ZERO'), ('ZERO',) * 4),
            S((3, 4), ('ZERO', 'CPREV', 'TEXC', 'ZERO'), ('ZERO',) * 4, dest='REG0'),
            S((0, 0), ('ZERO', 'ONE', 'TEXC', 'ZERO'), ('ZERO', 'ZERO', 'ZERO', 'TEXA')),
            S((2, 2), ('ZERO', 'CPREV', 'TEXC', 'C0'))]
    else:
        prog.technique = 7 if receive else 4
        prog.texgen[1] = ('uv', 2)
        prog.stages = [
            S((0, 0), ('ZERO', 'ONE', 'TEXC', 'ZERO'), ('ZERO', 'ZERO', 'ZERO', 'TEXA')),
            S((2, 1), ('ZERO', 'CPREV', 'TEXC', 'ZERO'))]
    if receive:
        prog.texgen[5] = ('shadow',)
        prog.stages.append(S((4, 5), ('ZERO', 'CPREV', 'TEXC', 'ZERO'), scale=1))
    return prog


def program_stadium(raw, time_uv=True):
    """stadiumdetailmaskwithuvsliding: begin 8010F010, draw 8010F234."""
    prog = Program('stadiumdetailmaskwithuvsliding', 0)
    blend = _f32(raw, 0x30)
    B = max(0, min(255, int(blend * 255.0)))
    prog.konst['K0'] = (B, B, B, B)
    prog.maps = {0: 0, 1: 1, 2: 2}
    prog.texgen[0] = ('uvscroll', 0, _f32(raw, 0x18), _f32(raw, 0x1C))
    prog.texgen[1] = ('uvscroll', 1, _f32(raw, 0x20), _f32(raw, 0x24))
    prog.texgen[2] = ('uvscroll', 2, _f32(raw, 0x28), _f32(raw, 0x2C))
    prog.stages = [
        S((0, 0), ('TEXC', 'ZERO', 'KONST', 'ZERO'), ('ZERO', 'ZERO', 'ZERO', 'TEXA')),
        S((1, 1), ('ZERO', 'TEXC', 'KONST', 'CPREV')),
        S((2, 2), ('ZERO', 'TEXC', 'CPREV', 'ZERO')),
        S(None, ('ZERO', 'CPREV', 'RASC', 'ZERO'), ras='COLOR0A0')]
    return prog


def program_rope(raw, light=ROPE_LIGHT):
    """ropeskin draw 8010EEBC: cellramp(half-Lambert) × diffuse."""
    prog = Program('ropeskin', 0)
    prog.maps = {0: 0, 1: 1}
    prog.texgen[0] = ('uv', 0)
    prog.texgen[1] = ('ramp', light)
    prog.stages = [S((1, 1), ('ZERO', 'ONE', 'TEXC', 'ZERO'), ('ZERO',) * 4),
                   S((0, 0), ('ZERO', 'CPREV', 'TEXC', 'ZERO'), ('ZERO', 'ZERO', 'ZERO', 'TEXA'))]
    return prog


def program_crowdskin(raw, light=CROWD_LIGHT):
    """crowdskin draw 801059C4: ramp(half-Lambert) × diffuse + rim.

    Pass setup 801056F8 loads PTTEXMTX1 = (0.5·L', 0.5 | 0 | 0,0,0,1) with L' the world
    light (ViewCrowdLightDirection0..2) times the current view matrix, and PTTEXMTX2 =
    (V, 0 | 0,-1,0,1 | 0) with V = spherical(1, 35°, 270°) constant in view space.  `light`
    may name an object property holding a per-object world light (impostor renders).
    """
    prog = Program('crowdskin', 0)
    # The pass leaves the normal matrix at identity ("texgens use untransformed normals",
    # rendering_pipeline.md), so N is the model-space normal, not the view-space one.
    prog.model_normals = True
    prog.maps = {0: 0, 1: 1, 2: 2}
    prog.texgen[0] = ('uv', 0)
    prog.texgen[1] = ('ramp', light)
    prog.texgen[2] = ('rimv', CROWD_RIM_VIEW)
    prog.stages = [S((1, 1), ('ZERO', 'ONE', 'TEXC', 'ZERO'), ('ZERO',) * 4),
                   S((0, 0), ('ZERO', 'CPREV', 'TEXC', 'ZERO'), ('ZERO', 'ZERO', 'ZERO', 'TEXA')),
                   S((2, 2), ('ZERO', 'CPREV', 'ONE', 'TEXC'))]
    return prog


def program_texture_only(shader):
    prog = Program(shader, 0)
    prog.maps = {0: 0}
    prog.texgen[0] = ('uv', 0)
    prog.stages = [S((0, 0), ('ZERO', 'ONE', 'TEXC', 'ZERO'), ('ZERO', 'ZERO', 'ZERO', 'TEXA'))]
    prog.notes.append('technique not yet decoded; diffuse only')
    return prog


def build_program(shader, raw, ctx=None):
    ctx = ctx or {}
    if shader == 'hippodiffuseskin':
        return program_skin(raw, ctx.get('light', CHAR_LIGHT), ctx.get('intensity', SKIN_INTENSITY))
    if shader == 'hippodiffusenonskin':
        return program_nonskin(raw, ctx.get('light', CHAR_LIGHT))
    if shader in ('hippobasicenvironment', 'hippohwlitenvironment'):
        return program_env(raw, ctx.get('env_light', ENV_LIGHT), ctx.get('shadow', False))
    if shader == 'stadiumdetailmaskwithuvsliding':
        return program_stadium(raw)
    if shader == 'ropeskin':
        return program_rope(raw, ctx.get('rope_light', ROPE_LIGHT))
    if shader in ('crowdskin', 'crowdskindk'):
        return program_crowdskin(raw, ctx.get('crowd_light', CROWD_LIGHT))
    return program_texture_only(shader)


# --------------------------------------------------------------------------------------------
# Texture provider
class TextureProvider:
    """Resolves texture keys exactly (hash lookup) through the owning archive, then global."""

    def __init__(self, art_root, names=None):
        self.art_root = Path(art_root)
        self.names = names
        self.archives = {}
        self.images = {}
        self.sequences = {}

    def archive(self, path, section=None):
        key = (str(path).lower(), section)
        if key not in self.archives:
            arc = None
            if section is None:
                arc = Archive(str(path))
            else:
                import nlg_asset
                doc = nlg_asset.AssetDocument(Path(path), self.names)
                arc = doc.sections[section].archive
            entries = {e.hash: e for e in nlg_texture.list_all_textures(arc, self.names)}
            try:
                seqs = nlg_texture_animation.sequences(arc)
            except Exception:
                seqs = {}
            self.archives[key] = (arc, entries, seqs)
        return self.archives[key]

    def global_archive(self):
        return self.archive(self.art_root / 'global.dict')

    def resolve(self, key, owners):
        """Return ('tex', archive, entry) or ('seq', archive, sequence, owner) or None."""
        for owner in list(owners) + [self.global_archive()]:
            arc, entries, seqs = owner
            if key in entries:
                return ('tex', arc, entries[key])
            if key in seqs:
                return ('seq', arc, seqs[key], owner)
        return None

    def image(self, arc, entry):
        name = 'GX:%08X:%08X' % (id(arc) & 0xFFFFFFFF, entry.hash)
        cache = (id(arc), entry.hash)
        if cache in self.images:
            return self.images[cache]
        label = 'GX %s' % (entry.name.split('/')[-1] if entry.name else '%08X' % entry.hash)
        img = bpy.data.images.new(label, entry.width, entry.height, alpha=True)
        img.colorspace_settings.name = 'Non-Color'
        img.alpha_mode = 'CHANNEL_PACKED'
        rgba = nlg_texture.decode_texture(arc, entry)
        w, h = entry.width, entry.height
        px = [0.0] * (w * h * 4)
        inv = 1.0 / 255.0
        for y in range(h):
            src = (h - 1 - y) * w * 4          # Blender rows are bottom-up
            dst = y * w * 4
            row = rgba[src:src + w * 4]
            for i in range(w * 4):
                px[dst + i] = row[i] * inv
        img.pixels.foreach_set(px)
        img.update()
        img.pack()
        img['po_gx_texture'] = json.dumps({'hash': '%08X' % entry.hash, 'name': entry.name,
                                           'format': nlg_texture.FORMATS.get(entry.fmt, entry.fmt),
                                           'size': [w, h]})
        self.images[cache] = img
        return img


# --------------------------------------------------------------------------------------------
# Ramp LUT conditioning
RAMP_SMOOTH_SIGMA = 2.0        # texels
SMOOTHED_RAMP_SHADERS = ('hippodiffuseskin', 'hippodiffusenonskin')


def _smoothed_ramp(img, sigma=RAMP_SMOOTH_SIGMA):
    """Return a de-stepped copy of a wide 1-D lighting LUT (or ``img`` when it is not one).

    The skin ramps are CMPR textures: every 4-texel block only holds four colours, so a
    128-texel ramp decodes to ~40 distinct levels with 4-5 texel plateaus.  Lit across a
    smooth surface those plateaus print as contour bands (visible on DK's chest), which the
    retail footage does not show.  A small Gaussian along the ramp removes the stair-steps
    while keeping its overall shape; rows are averaged because the ramp only varies along U.
    """
    w, h = img.size
    if w < 32 or h > 16:
        return img
    name = img.name + ' · smooth'
    cached = bpy.data.images.get(name)
    if cached is not None and tuple(cached.size) == (w, h):
        return cached
    if cached is not None:
        bpy.data.images.remove(cached)
    src = list(img.pixels)
    cols = [[sum(src[(y * w + x) * 4 + c] for y in range(h)) / h for c in range(4)] for x in range(w)]
    radius = int(math.ceil(3.0 * sigma))
    kernel = [math.exp(-0.5 * (k / sigma) ** 2) for k in range(-radius, radius + 1)]
    total = sum(kernel)
    smooth = []
    for x in range(w):
        acc = [0.0] * 4
        for k, weight in zip(range(-radius, radius + 1), kernel):
            col = cols[min(w - 1, max(0, x + k))]
            for c in range(4):
                acc[c] += col[c] * weight
        smooth.append([v / total for v in acc])
    out = bpy.data.images.new(name, w, h, alpha=True)
    out.colorspace_settings.name = 'Non-Color'
    out.alpha_mode = 'CHANNEL_PACKED'
    out.pixels.foreach_set([smooth[x][c] for _ in range(h) for x in range(w) for c in range(4)])
    out.update()
    out.pack()
    out['po_gx_smoothed_from'] = img.name
    return out


# --------------------------------------------------------------------------------------------
# Node compiler
class Graph:
    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links
        self.col = 0
        self.row = 0
        self.cache = {}

    def new(self, kind, **props):
        n = self.nodes.new(kind)
        for k, v in props.items():
            setattr(n, k, v)
        n.location = (self.col * 220 - 2000, -self.row * 60)
        self.row += 1
        if self.row > 40:
            self.row = 0; self.col += 1
        return n

    def link(self, src, dst):
        self.links.new(src, dst)

    # value helpers --------------------------------------------------------------------------
    @staticmethod
    def is_const(v):
        return isinstance(v, (int, float, tuple))

    def vec(self, v):
        """Vector socket for a value (constant tuple/float or socket)."""
        if isinstance(v, (int, float)):
            v = (float(v),) * 3
        if isinstance(v, tuple):
            key = ('vconst', v)
            if key not in self.cache:
                n = self.new('ShaderNodeCombineXYZ')
                for i in range(3):
                    n.inputs[i].default_value = v[i]
                self.cache[key] = n.outputs[0]
            return self.cache[key]
        if getattr(v, 'type', None) == 'VALUE':
            key = ('bcast', v.node.name, v.identifier)
            if key not in self.cache:
                n = self.new('ShaderNodeCombineXYZ')
                for i in range(3):
                    self.link(v, n.inputs[i])
                self.cache[key] = n.outputs[0]
            return self.cache[key]
        return v

    def scal(self, v):
        if isinstance(v, (int, float)):
            return float(v)
        return v

    def vop(self, op, a, b=None, c=None):
        consts = [x for x in (a, b, c) if x is not None]
        if all(isinstance(x, tuple) for x in consts):
            return _vconst(op, a, b, c)
        n = self.new('ShaderNodeVectorMath', operation=op)
        for i, x in enumerate((a, b, c)):
            if x is None:
                continue
            if isinstance(x, (tuple, int, float)):
                xv = x if isinstance(x, tuple) else (float(x),) * 3
                n.inputs[i].default_value = xv
            else:
                self.link(self.vec(x), n.inputs[i])
        return n.outputs[0] if op not in ('DOT_PRODUCT', 'LENGTH') else n.outputs[1]

    def fop(self, op, a, b=None, c=None, clamp=False):
        consts = [x for x in (a, b, c) if x is not None]
        if all(isinstance(x, (int, float)) for x in consts) and not clamp:
            return _fconst(op, a, b, c)
        n = self.new('ShaderNodeMath', operation=op, use_clamp=clamp)
        for i, x in enumerate((a, b, c)):
            if x is None:
                continue
            if isinstance(x, (int, float)):
                n.inputs[i].default_value = float(x)
            else:
                self.link(x, n.inputs[i])
        return n.outputs[0]

    def split(self, v):
        if isinstance(v, tuple):
            return v
        n = self.new('ShaderNodeSeparateXYZ')
        self.link(self.vec(v), n.inputs[0])
        return n.outputs[0], n.outputs[1], n.outputs[2]

    def combine(self, x, y, z=0.0):
        n = self.new('ShaderNodeCombineXYZ')
        for i, c in enumerate((x, y, z)):
            if isinstance(c, (int, float)):
                n.inputs[i].default_value = float(c)
            else:
                self.link(c, n.inputs[i])
        return n.outputs[0]

    def clamp01v(self, v):
        if isinstance(v, tuple):
            return tuple(min(1.0, max(0.0, c)) for c in v)
        return self.vop('MINIMUM', self.vop('MAXIMUM', v, (0.0, 0.0, 0.0)), (1.0, 1.0, 1.0))

    def lerpv(self, a, b, c):
        # (1 - c)·a + c·b  ==  a + c·(b - a)
        if isinstance(c, tuple) and all(x == 0 for x in c):
            return a
        if isinstance(c, tuple) and all(x == 1 for x in c):
            return b
        diff = self.vop('SUBTRACT', b, a)
        if isinstance(diff, tuple) and all(x == 0 for x in diff):
            return a
        prod = self.vop('MULTIPLY', c, diff)
        return self.vop('ADD', a, prod) if not (isinstance(a, tuple) and all(x == 0 for x in a)) else prod

    def lerpf(self, a, b, c):
        if c == 0 or c == 0.0:
            return a
        if c == 1 or c == 1.0:
            return b
        diff = self.fop('SUBTRACT', b, a)
        if diff == 0:
            return a
        prod = self.fop('MULTIPLY', c, diff)
        return prod if a == 0 else self.fop('ADD', a, prod)


def _vconst(op, a, b, c):
    A = a if a is None or isinstance(a, tuple) else (float(a),) * 3
    B = b if b is None or isinstance(b, tuple) else (float(b),) * 3
    if op == 'ADD': return tuple(x + y for x, y in zip(A, B))
    if op == 'SUBTRACT': return tuple(x - y for x, y in zip(A, B))
    if op == 'MULTIPLY': return tuple(x * y for x, y in zip(A, B))
    if op == 'MAXIMUM': return tuple(max(x, y) for x, y in zip(A, B))
    if op == 'MINIMUM': return tuple(min(x, y) for x, y in zip(A, B))
    raise ValueError(op)


def _fconst(op, a, b, c):
    if op == 'ADD': return a + b
    if op == 'SUBTRACT': return a - b
    if op == 'MULTIPLY': return a * b
    if op == 'MAXIMUM': return max(a, b)
    if op == 'MINIMUM': return min(a, b)
    raise ValueError(op)


class Compiler:
    def __init__(self, material, program, slot_images, time_driver=True):
        self.m = material
        self.p = program
        self.images = slot_images      # slot -> dict(image, clamp_s, clamp_t, nearest, frames)
        self.g = None
        self.texgen_cache = {}
        self.sample_cache = {}
        self.view_normal = None

    # ---- geometry inputs ------------------------------------------------------------------
    def _uv(self, index):
        names = {0: 'UV', 1: 'UV1', 2: 'UV2'}
        uv = self.g.new('ShaderNodeUVMap')
        uv.uv_map = names.get(index, 'UV%d' % index)
        return uv.outputs[0]

    def _normal(self):
        if self.view_normal is None:
            attr = self.g.new('ShaderNodeAttribute', attribute_type='GEOMETRY', attribute_name='gx_nw')
            if self.p.model_normals:
                vt = self.g.new('ShaderNodeVectorTransform', vector_type='NORMAL',
                                convert_from='WORLD', convert_to='OBJECT')
                self.g.link(attr.outputs['Vector'], vt.inputs[0])
                self.view_normal = vt.outputs[0]
                return self.view_normal
            vt = self.g.new('ShaderNodeVectorTransform', vector_type='VECTOR',
                            convert_from='WORLD', convert_to='CAMERA')
            self.g.link(attr.outputs['Vector'], vt.inputs[0])
            # Blender's shader camera space points +Z away from the viewer; GX view space
            # (and every NLG post matrix, e.g. H = L + (0,0,1)) uses +Z toward the viewer.
            flip = self.g.new('ShaderNodeVectorMath', operation='MULTIPLY')
            flip.inputs[1].default_value = (1.0, 1.0, -1.0)
            self.g.link(vt.outputs[0], flip.inputs[0])
            self.view_normal = flip.outputs[0]
        return self.view_normal

    def _light_view(self, light):
        key = ('light', light)
        if key not in self.g.cache:
            vt = self.g.new('ShaderNodeVectorTransform', vector_type='VECTOR',
                            convert_from='WORLD', convert_to='CAMERA')
            if isinstance(light, tuple) and len(light) == 2 and light[0] == 'view':
                # Already in view space: the transform is the identity and the shared
                # +Z flip below maps Blender camera space to GX view space.
                vt.convert_from = 'CAMERA'
                vt.convert_to = 'CAMERA'
                vx, vy, vz = light[1]
                vt.inputs[0].default_value = (vx, vy, -vz)
            elif isinstance(light, str):
                # Per-object world light (object custom property), e.g. crowd impostors whose
                # "view" matrix carries the model's own yaw and scale (80159818).
                attr = self.g.new('ShaderNodeAttribute', attribute_type='OBJECT', attribute_name=light)
                self.g.link(attr.outputs['Vector'], vt.inputs[0])
            else:
                vt.inputs[0].default_value = light
            flip = self.g.new('ShaderNodeVectorMath', operation='MULTIPLY')
            flip.inputs[1].default_value = (1.0, 1.0, -1.0)
            self.g.link(vt.outputs[0], flip.inputs[0])
            n = self.g.new('ShaderNodeVectorMath', operation='NORMALIZE')
            self.g.link(flip.outputs[0], n.inputs[0])
            self.g.cache[key] = n.outputs[0]
        return self.g.cache[key]

    def _time(self):
        key = ('time',)
        if key not in self.g.cache:
            v = self.g.new('ShaderNodeValue')
            v.name = 'GX_Time'
            v.label = 'Seconds since start (frame-1)/fps'
            d = v.outputs[0].driver_add('default_value').driver
            d.type = 'SCRIPTED'
            d.expression = '(frame - 1) / 30.0'
            self.g.cache[key] = v.outputs[0]
        return self.g.cache[key]

    def texcoord(self, i):
        """GX texture coordinate → Blender UV (t flipped: Blender v = 1 - t)."""
        if i in self.texgen_cache:
            return self.texgen_cache[i]
        g = self.g
        gen = self.p.texgen[i]
        kind = gen[0]
        if kind == 'uv':
            out = self._uv(gen[1])                      # importer already stores (s, 1 - t)
        elif kind == 'uvscroll':
            _, uvset, du, dv = gen
            base = self._uv(uvset)
            if du or dv:
                t = self._time()
                # TEXMTX translation = frac(time · speed) (draw 8010F234); t → Blender v = 1 - t
                fu = g.fop('FRACT', g.fop('MULTIPLY', t, du)) if du else 0.0
                fv = g.fop('FRACT', g.fop('MULTIPLY', t, dv)) if dv else 0.0
                off = g.combine(fu, g.fop('MULTIPLY', fv, -1.0) if dv else 0.0, 0.0)
                out = g.vop('ADD', base, off)
            else:
                out = base
        elif kind == 'ramp':
            n = self._normal(); l = self._light_view(gen[1])
            s = g.vop('DOT_PRODUCT', n, l)
            s = g.fop('MULTIPLY_ADD', s, 0.5, 0.5)
            out = g.combine(s, 1.0, 0.0)                 # t = 0  →  v = 1
        elif kind == 'spec':
            n = self._normal(); l = self._light_view(gen[1])
            h = g.vop('NORMALIZE', g.vop('ADD', l, (0.0, 0.0, 1.0)))
            s = g.vop('DOT_PRODUCT', n, h)
            t = gen[2] * 0.0078125
            out = g.combine(s, 1.0 - t, 0.0)
        elif kind == 'fresnel':
            _, f2, f3 = gen
            nz = g.split(self._normal())[2]
            snum = g.fop('MULTIPLY_ADD', nz, f2 * f3 - 1.0, 1.0)
            q = g.fop('MULTIPLY_ADD', nz, f3 - 1.0, 1.0)
            s = g.fop('DIVIDE', snum, q)
            out = g.combine(s, 1.0, 0.0)
        elif kind == 'rim':
            # PTTEXMTX4 (8033A938): s = Nz, t = 1 - Ny, q = 0 → clamp(xy / 2, -1, 1), no divide.
            nx, ny, nz = g.split(self._normal())
            s = g.fop('MULTIPLY', nz, 0.5)
            t = g.fop('MULTIPLY_ADD', ny, -0.5, 0.5)
            out = g.combine(s, g.fop('SUBTRACT', 1.0, t), 0.0)
        elif kind == 'rimv':
            # crowdskin PTTEXMTX2 (801056F8): rows (V,0),(0,-1,0,1),(0,0,0,0) with a constant
            # view-space V; q = 0 → s = N·V / 2, t = (1 - Ny) / 2.
            v = gen[1]
            nx, ny, nz = g.split(self._normal())
            s = g.fop('MULTIPLY', g.vop('DOT_PRODUCT', self._normal(), tuple(v)), 0.5)
            t = g.fop('MULTIPLY_ADD', ny, -0.5, 0.5)
            out = g.combine(s, g.fop('SUBTRACT', 1.0, t), 0.0)
        elif kind == 'sphere':
            _, hs, vs = gen
            nx, ny, nz = g.split(self._normal())
            s = g.fop('MULTIPLY_ADD', nx, 0.5 * hs, 0.5)
            t = g.fop('MULTIPLY_ADD', ny, -0.5 * vs, 0.5)
            out = g.combine(s, g.fop('SUBTRACT', 1.0, t), 0.0)
        elif kind == 'shadow':
            out = g.combine(0.5, 0.5, 0.0)   # scene-shadow target not reproduced: neutral 0x80
        else:
            out = self._uv(0)
        self.texgen_cache[i] = out
        return out

    def sample(self, texmap, coord):
        key = (texmap, coord)
        if key in self.sample_cache:
            return self.sample_cache[key]
        g = self.g
        slot = self.p.maps.get(texmap, texmap)
        info = self.images.get(slot)
        if info is None:
            res = ((1.0, 1.0, 1.0), 1.0)
            self.sample_cache[key] = res
            return res
        img = info['image']
        if (self.p.shader in SMOOTHED_RAMP_SHADERS and not info.get('frames')
                and self.p.texgen.get(coord, ('',))[0] == 'ramp'):
            img = _smoothed_ramp(img)
        uv = self.texcoord(coord)
        cs, ct = info['clamp_s'], info['clamp_t']
        tex = g.new('ShaderNodeTexImage')
        tex.image = img
        tex.interpolation = 'Closest' if info.get('nearest') else 'Linear'
        frames = info.get('frames')
        if frames:
            # IFL atlas: frames packed horizontally; add frame/nframes to u after wrapping.
            n = len(frames)
            u, v, _ = g.split(uv)
            u = g.fop('FRACT', u) if not cs else g.fop('MINIMUM', g.fop('MAXIMUM', u, 0.0), 1.0)
            v = g.fop('FRACT', v) if not ct else g.fop('MINIMUM', g.fop('MAXIMUM', v, 0.0), 1.0)
            w = img.size[0] // n
            u = g.fop('MINIMUM', g.fop('MAXIMUM', u, 0.5 / w), 1.0 - 0.5 / w)
            fr = g.new('ShaderNodeValue'); fr.name = 'GX_IFL_frame_%d' % texmap
            self._key_frames(fr, frames)
            u = g.fop('DIVIDE', g.fop('ADD', u, fr.outputs[0]), float(n))
            uv = g.combine(u, v, 0.0)
            tex.extension = 'EXTEND'
        elif cs and ct:
            tex.extension = 'EXTEND'
        elif not cs and not ct:
            tex.extension = 'REPEAT'
        else:
            # Mixed wrap: clamp one axis to texel centres, repeat the other.
            w, h = img.size
            u, v, _ = g.split(uv)
            if cs:
                u = g.fop('MINIMUM', g.fop('MAXIMUM', u, 0.5 / w), 1.0 - 0.5 / w)
            if ct:
                v = g.fop('MINIMUM', g.fop('MAXIMUM', v, 0.5 / h), 1.0 - 0.5 / h)
            uv = g.combine(u, v, 0.0)
            tex.extension = 'REPEAT'
        g.link(uv, tex.inputs['Vector'])
        res = (tex.outputs['Color'], tex.outputs['Alpha'])
        self.sample_cache[key] = res
        return res

    def _key_frames(self, node, frames):
        scene = bpy.context.scene
        seq = nlg_texture_animation.Sequence(0, tuple((i, d) for i, (_, d) in enumerate(frames)))
        prev = None
        sock = node.outputs[0]
        for frame in range(scene.frame_start, scene.frame_end + 2):
            idx = seq.texture_at((frame - 1) / 30.0)
            if idx != prev:
                sock.default_value = float(idx)
                sock.keyframe_insert('default_value', frame=frame)
                prev = idx
        ad = self.m.node_tree.animation_data
        if ad and ad.action:
            try:
                import po_action
                curves = po_action.all_fcurves(ad.action)
            except Exception:
                curves = getattr(ad.action, 'fcurves', [])
            for c in curves:
                if node.name in c.data_path:
                    for k in c.keyframe_points:
                        k.interpolation = 'CONSTANT'

    def konst_color(self, sel):
        if sel in KSEL_FRAC:
            return (KSEL_FRAC[sel],) * 3
        k = (sel - 0x0C) % 4
        c = self.p.konst.get('K%d' % k, (255, 255, 255, 255))
        c = tuple(x / 255.0 for x in c)
        if sel <= 0x0F:
            return c[:3]
        comp = (sel - 0x10) // 4
        return (c[comp],) * 3

    def konst_alpha(self, sel):
        if sel in KSEL_FRAC:
            return KSEL_FRAC[sel]
        k = (sel - 0x10) % 4
        c = self.p.konst.get('K%d' % k, (255, 255, 255, 255))
        return c[(sel - 0x10) // 4] / 255.0

    def ras(self, stage):
        g = self.g
        key = ('ras',)
        if key not in g.cache:
            ca = g.new('ShaderNodeVertexColor')
            ca.layer_name = 'PO_Color0'
            g.cache[key] = (ca.outputs['Color'], ca.outputs['Alpha'])
        return g.cache[key]

    def build(self):
        m = self.m
        m.use_nodes = True
        tree = m.node_tree
        tree.nodes.clear()
        if tree.animation_data:
            tree.animation_data_clear()
        self.g = g = Graph(tree)
        regs = {r: ((0.0, 0.0, 0.0), 0.0) for r in ('PREV', 'REG0', 'REG1', 'REG2')}
        for name, val in self.p.regs.items():
            regs[name] = (tuple(x / 255.0 for x in val[:3]), val[3] / 255.0)
        for st in self.p.stages:
            texc, texa = ((1.0, 1.0, 1.0), 1.0)
            if st.tex is not None:
                texc, texa = self.sample(st.tex[0], st.tex[1])
            if st.ras:
                rasc, rasa = self.ras(st)
            else:
                rasc, rasa = (0.0, 0.0, 0.0), 0.0

            def carg(a):
                return {'CPREV': regs['PREV'][0], 'APREV': regs['PREV'][1], 'C0': regs['REG0'][0],
                        'A0': regs['REG0'][1], 'C1': regs['REG1'][0], 'A1': regs['REG1'][1],
                        'C2': regs['REG2'][0], 'A2': regs['REG2'][1], 'TEXC': texc, 'TEXA': texa,
                        'RASC': rasc, 'RASA': rasa, 'ONE': (1.0, 1.0, 1.0), 'HALF': (0.5, 0.5, 0.5),
                        'KONST': self.konst_color(st.ksel), 'ZERO': (0.0, 0.0, 0.0)}[a]

            def aarg(a):
                return {'APREV': regs['PREV'][1], 'A0': regs['REG0'][1], 'A1': regs['REG1'][1],
                        'A2': regs['REG2'][1], 'TEXA': texa, 'RASA': rasa,
                        'KONST': self.konst_alpha(st.kasel), 'ZERO': 0.0}[a]

            a, b, c, d = [carg(x) for x in st.cin]
            a = g.vec(a) if not isinstance(a, tuple) else a
            b = g.vec(b) if not isinstance(b, tuple) else b
            c = g.vec(c) if not isinstance(c, tuple) else c
            d = g.vec(d) if not isinstance(d, tuple) else d
            sub, bias, scale, clamp, dest = st.cop
            lerp = g.lerpv(a, b, c)
            col = g.vop('SUBTRACT' if sub else 'ADD', d, lerp)
            if bias == 1:
                col = g.vop('ADD', col, (0.5, 0.5, 0.5))
            elif bias == 2:
                col = g.vop('SUBTRACT', col, (0.5, 0.5, 0.5))
            if scale:
                f = {1: 2.0, 2: 4.0, 3: 0.5}[scale]
                col = g.vop('MULTIPLY', col, (f, f, f))
            if clamp:
                col = g.clamp01v(col)
            aa, ab, ac, ad = [aarg(x) for x in st.ain]
            asub, abias, ascale, aclamp, adest = st.aop
            al = g.lerpf(aa, ab, ac)
            al = g.fop('SUBTRACT' if asub else 'ADD', ad, al) if not (ad == 0 and not asub) else al
            if abias == 1:
                al = g.fop('ADD', al, 0.5)
            elif abias == 2:
                al = g.fop('SUBTRACT', al, 0.5)
            if ascale:
                al = g.fop('MULTIPLY', al, {1: 2.0, 2: 4.0, 3: 0.5}[ascale])
            if aclamp and not isinstance(al, (int, float)):
                al = g.fop('MULTIPLY', al, 1.0, clamp=True)
            regs[dest] = (col, regs[dest][1])
            regs[adest] = (regs[adest][0], al)
        col, al = regs['PREV']
        out = g.new('ShaderNodeOutputMaterial')
        emit = g.new('ShaderNodeEmission')
        emit.inputs['Strength'].default_value = 1.0
        if isinstance(col, tuple):
            emit.inputs['Color'].default_value = col + (1.0,)
        else:
            g.link(g.vec(col), emit.inputs['Color'])
        blend = self.p.blend
        surface = emit.outputs[0]
        if blend in ('CLIP', 'BLEND'):
            tr = g.new('ShaderNodeBsdfTransparent')
            mix = g.new('ShaderNodeMixShader')
            fac = al
            if blend == 'CLIP':
                ref = self.p.alpha_ref / 255.0
                fac = g.fop('GREATER_THAN', al, ref) if not isinstance(al, (int, float)) else float(al > ref)
            if isinstance(fac, (int, float)):
                mix.inputs[0].default_value = fac
            else:
                g.link(fac, mix.inputs[0])
            g.link(tr.outputs[0], mix.inputs[1]); g.link(emit.outputs[0], mix.inputs[2])
            surface = mix.outputs[0]
        elif blend in ('ADD', 'ADDALPHA'):
            tr = g.new('ShaderNodeBsdfTransparent')
            if blend == 'ADDALPHA' and not isinstance(al, (int, float)):
                prod = g.vop('MULTIPLY', g.vec(col), g.vec(al))
                g.link(prod, emit.inputs['Color'])
            add = g.new('ShaderNodeAddShader')
            g.link(tr.outputs[0], add.inputs[0]); g.link(emit.outputs[0], add.inputs[1])
            surface = add.outputs[0]
        elif blend == 'MULTIPLY':
            tr = g.new('ShaderNodeBsdfTransparent')
            g.link(g.vec(col), tr.inputs['Color'])
            surface = tr.outputs[0]
        g.link(surface, out.inputs['Surface'])
        # Blender render method: blending writes no depth, as with GX Z-update off.
        sorted_blend = blend in ('BLEND', 'ADD', 'ADDALPHA', 'MULTIPLY') and not self.p.hashed_alpha
        if hasattr(m, 'surface_render_method'):
            m.surface_render_method = 'BLENDED' if sorted_blend else 'DITHERED'
        else:
            m.blend_method = {'OPAQUE': 'OPAQUE', 'CLIP': 'CLIP'}.get(blend, 'BLEND' if sorted_blend else 'HASHED')
        m.use_backface_culling = bool(self.p.cull)
        try:
            m.use_transparency_overlap = True
        except AttributeError:
            pass
        m['po_gx_program'] = json.dumps(self.p.describe())
        return m


# --------------------------------------------------------------------------------------------
# Render state from the slot-0 texture (8014F24C) and blend defaults
def blend_from_texture(entry):
    bits = getattr(entry, 'channel_bits', None)
    alpha = bits[3] if bits else (8 if nlg_texture.FORMATS.get(entry.fmt) in ('RGBA32', 'RGB5A3', 'IA8') else 0)
    if alpha == 0:
        return 'OPAQUE', 0
    if alpha == 1:
        return 'CLIP', 0x80
    return 'BLEND', 0


# --------------------------------------------------------------------------------------------
# Geometry Nodes: per-corner normalised world normal ("gx_nw")
GN_NAME = 'PO_GX_WorldNormal'


def world_normal_group():
    ng = bpy.data.node_groups.get(GN_NAME)
    if ng is not None:
        return ng
    ng = bpy.data.node_groups.new(GN_NAME, 'GeometryNodeTree')
    ng.interface.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    N = ng.nodes; Lk = ng.links
    gi = N.new('NodeGroupInput'); go = N.new('NodeGroupOutput')
    selfo = N.new('GeometryNodeSelfObject')
    info = N.new('GeometryNodeObjectInfo'); info.transform_space = 'ORIGINAL'
    Lk.new(selfo.outputs[0], info.inputs['Object'])
    nrm = N.new('GeometryNodeInputNormal')
    td = N.new('FunctionNodeTransformDirection')
    Lk.new(nrm.outputs[0], td.inputs['Direction'])
    Lk.new(info.outputs['Transform'], td.inputs['Transform'])
    nz = N.new('ShaderNodeVectorMath'); nz.operation = 'NORMALIZE'
    Lk.new(td.outputs[0], nz.inputs[0])
    store = N.new('GeometryNodeStoreNamedAttribute')
    store.data_type = 'FLOAT_VECTOR'; store.domain = 'CORNER'
    store.inputs['Name'].default_value = 'gx_nw'
    Lk.new(gi.outputs[0], store.inputs['Geometry'])
    Lk.new(nz.outputs[0], store.inputs['Value'])
    Lk.new(store.outputs[0], go.inputs[0])
    return ng


def ensure_world_normals(obj):
    if obj.type != 'MESH':
        return
    for mod in obj.modifiers:
        if mod.type == 'NODES' and mod.node_group and mod.node_group.name == GN_NAME:
            return
    mod = obj.modifiers.new('GX world normal', 'NODES')
    mod.node_group = world_normal_group()
    # Keep it after deformation (armature) but before outline shells.
    idx = len(obj.modifiers) - 1
    for i, other in enumerate(obj.modifiers):
        if other.type in ('SOLIDIFY', 'WELD') and i < idx:
            idx = i
            break
    try:
        with bpy.context.temp_override(object=obj):
            bpy.ops.object.modifier_move_to_index(modifier=mod.name, index=idx)
    except Exception:
        pass


# --------------------------------------------------------------------------------------------
# Scene integration
def scene_output(scene):
    """GX writes encoded 8-bit values without tone mapping; Raw keeps them untouched."""
    vs = scene.view_settings
    vs.view_transform = 'Raw'
    vs.look = 'None'
    vs.exposure = 0.0
    vs.gamma = 1.0
    scene.render.dither_intensity = 0.0
    if scene.world is None:
        scene.world = bpy.data.worlds.new('GX clear')
    w = scene.world
    w.use_nodes = True
    bg = next((n for n in w.node_tree.nodes if n.type == 'BACKGROUND'), None)
    if bg:
        bg.inputs['Color'].default_value = (0, 0, 0, 1)
        bg.inputs['Strength'].default_value = 1.0
    eevee = getattr(scene, 'eevee', None)
    for attr, val in (('use_bloom', False), ('use_gtao', False), ('use_ssr', False)):
        if eevee is not None and hasattr(eevee, attr):
            setattr(eevee, attr, val)


def material_source(mat, obj):
    """(shader name, record bytes, owner archive tuple(s)) or None."""
    raw = None; shader = None; archives = []
    if mat.get('po_record'):
        raw = bytes.fromhex(mat['po_record'])
        shader = 'hippodiffuseskin' if len(raw) == 204 else None
        src = obj.get('po_source_dict') if obj else None
        if src:
            archives.append(('path', src, None))
    meta = mat.get('po_material')
    if isinstance(meta, str) and meta.startswith('{'):
        info = json.loads(meta)
        shader = info.get('shader_name') or SHADERS.get(info.get('shader'))
        source = info.get('source') or {}
        path = source.get('path')
        if path:
            archives.insert(0, ('record', path, source.get('section'), info.get('chunk'),
                                info.get('offset'), info.get('bytes')))
    if not shader:
        return None
    return shader, raw, archives


def _art_root(path):
    p = Path(path)
    for parent in [p] + list(p.parents):
        if parent.name.lower() == 'art':
            return parent
    return p.parent


class SceneBuilder:
    def __init__(self, names=None, ctx=None, report=print):
        self.names = names if names is not None else {}
        self.ctx = ctx or {}
        self.report = report
        self.provider = None
        self.stats = {}

    def _owner(self, path, section):
        if self.provider is None:
            self.provider = TextureProvider(_art_root(path), self.names)
        return self.provider.archive(path, section)

    def material(self, mat, obj):
        src = material_source(mat, obj)
        if src is None:
            return False
        shader, raw, archives = src
        owners = []
        for a in archives:
            if a[0] == 'record':
                _, path, section, chunk, offset, size = a
                owner = self._owner(path, section)
                owners.append(owner)
                if raw is None and chunk is not None:
                    data = owner[0].get_chunk_bytes(chunk)
                    raw = bytes(data[offset:offset + (size or 0)])
            else:
                owners.append(self._owner(a[1], a[2]))
        if raw is None:
            return False
        ctx = dict(self.ctx)
        prog = build_program(shader, raw, ctx)
        clamp = SLOT_CLAMP.get(shader, {})
        slots = {}
        swap = self.ctx.get('texture_swap') or {}
        for slot in set(prog.maps.values()):
            key, cs, ct, nearest = _slot_state(raw, slot, clamp.get(slot))
            if key in (0, 0xFFFFFFFF):
                continue
            key = swap.get(key, key)
            res = self.provider.resolve(key, owners)
            if res is None:
                continue
            if res[0] == 'tex':
                img = self.provider.image(res[1], res[2])
                slots[slot] = dict(image=img, clamp_s=cs, clamp_t=ct, nearest=nearest, entry=res[2])
            else:
                seq, arc = res[2], res[1]
                entries = res[3][1]
                frames = []
                imgs = []
                for h, dur in seq.frames:
                    if h not in entries:
                        imgs = []; break
                    imgs.append(self.provider.image(arc, entries[h])); frames.append((h, dur))
                if not imgs:
                    continue
                atlas = _atlas(imgs)
                slots[slot] = dict(image=atlas, clamp_s=cs, clamp_t=ct, nearest=nearest,
                                   frames=frames, entry=entries[seq.frames[0][0]])
        if 0 in slots:
            prog.blend, prog.alpha_ref = blend_from_texture(slots[0]['entry'])
            if prog.blend != 'OPAQUE':
                prog.cull = False
            # The arena's flat crowd is hundreds of overlapping alpha cards.  Blender sorts
            # blended surfaces per object, so a nearer card's transparent area can hide a farther
            # row; hashed alpha keeps the depth of the visible people only.
            if prog.blend == 'BLEND' and 'crowd' in (getattr(slots[0]['entry'], 'name', '') or '').lower():
                prog.hashed_alpha = True
        Compiler(mat, prog, slots).build()
        self.stats[shader] = self.stats.get(shader, 0) + 1
        return True

    def run(self, objects):
        done = set()
        for obj in objects:
            if obj.type != 'MESH':
                continue
            if obj.get('po_source_dict'):
                try:
                    ensure_gx_uv_sets(obj)
                except Exception as ex:
                    self.report('GX UV sets for %s: %s' % (obj.name, ex))
            for slot in obj.material_slots:
                m = slot.material
                if m is None or m.name in done or m.get('po_outline'):
                    continue
                try:
                    if self.material(m, obj):
                        done.add(m.name)
                except Exception as ex:  # keep going; report the material
                    self.report('GX material %s failed: %s' % (m.name, ex))
            if any(sl.material and sl.material.get('po_gx_program') for sl in obj.material_slots):
                ensure_world_normals(obj)
        return self.stats


def _atlas(images):
    width, height = images[0].size
    name = 'GX IFL ' + '+'.join(i.name.replace('GX ', '') for i in images)[:50]
    atlas = bpy.data.images.new(name, width * len(images), height, alpha=True)
    atlas.colorspace_settings.name = 'Non-Color'
    atlas.alpha_mode = 'CHANNEL_PACKED'
    from array import array
    src = []
    for im in images:
        buf = array('f', [0.0]) * (width * height * 4)
        im.pixels.foreach_get(buf); src.append(buf)
    joined = array('f', [0.0]) * (width * len(images) * height * 4)
    stride = width * 4; ast = stride * len(images)
    for y in range(height):
        for k, buf in enumerate(src):
            joined[y * ast + k * stride:y * ast + (k + 1) * stride] = buf[y * stride:(y + 1) * stride]
    atlas.pixels.foreach_set(joined)
    atlas.update(); atlas.pack()
    return atlas


def rebuild_scene(scene=None, names=None, ctx=None, report=print):
    scene = scene or bpy.context.scene
    if names is None:
        try:
            art = None
            for m in bpy.data.materials:
                meta = m.get('po_material')
                if isinstance(meta, str) and '"path"' in meta:
                    art = _art_root(json.loads(meta)['source']['path']); break
            hb = art / 'hashid.bin' if art else None
            names = nlg_hash.load_hashid_bin(str(hb)) if hb and hb.exists() else {}
        except Exception:
            names = {}
    builder = SceneBuilder(names, ctx, report)
    stats = builder.run(list(scene.objects))
    scene_output(scene)
    return stats


def ensure_gx_uv_sets(obj):
    """Give an already-imported fighter mesh its GX TEX1/TEX2 sets (0x05 / 0x3D)."""
    if obj.type != 'MESH' or (obj.data.uv_layers.get('UV1') and obj.data.uv_layers.get('UV2')):
        return True
    source = obj.get('po_source_dict')
    if not source or not os.path.isfile(bpy.path.abspath(source)):
        return False
    import nlg_geom
    meshes = nlg_geom.read_model(Archive(bpy.path.abspath(source)))
    c1, c2 = [], []
    for mesh in meshes:
        uv0 = mesh.uv; d = getattr(mesh, 'uv_damage', []); u2 = getattr(mesh, 'uv2', [])
        for i in range(len(mesh.pos)):
            base = uv0[i] if i < len(uv0) else (0.0, 0.0)
            a = d[i] if i < len(d) else base; b = u2[i] if i < len(u2) else base
            c1.append((a[0], 1.0 - a[1])); c2.append((b[0], 1.0 - b[1]))
    if len(c1) != len(obj.data.vertices):
        return False
    for name, coords in (('UV1', c1), ('UV2', c2)):
        if obj.data.uv_layers.get(name) is None:
            layer = obj.data.uv_layers.new(name=name).data
            for loop in obj.data.loops:
                layer[loop.index].uv = coords[loop.vertex_index]
    return True
