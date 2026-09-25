# Punch-Out!! Wii — Animation Format

> Executable addresses below refer to PAL `R7PP01`, revision 0 (`main.dol` SHA-256
> `6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137`). NTSC addresses differ and
> were not tested.

Reference for the character animation format. Rotation compression is **precision-based**, not a
component subset; sections 4 and 10 describe it. The canonical implementation is **`potools/formats/nlg_anim2.py`** (`Rig` class). Everything below was
derived from the raw bytes and validated by coherent skinned playback across the roster (0 unit-norm
violations in 191k frames across 12 characters) and by in-game probes in Dolphin.

All values are **big-endian** (Wii / PowerPC).

---

## 1. The trailing `0x8xxx` node table

Each character archive ends (after the last animation run) with a block of `0x8xxx` chunks that is a
**global animation-set node table** — the game's own rig description. It removes all the guesswork
that earlier probing tried to reconstruct:

| Chunk | Contents |
|-------|----------|
| `0x8003` | `N × u32` node **name hashes** → de-hash via `hashid.bin` = the complete node→bone map, for free. |
| `0x8009` | `N × i32` **parent node index** (node0 `root` = −1) = the exact hierarchy. |
| `0x8010` | `N × 3×f32` bind **local translation** per node (rest offsets). |
| `0x8011` | `N × u8`: `1` = translation is constant (use `0x8010`); `0` = node has a `0x7102` track. |
| `0x8008` | left↔right **mirror** node remap. |
| `0x8007` | eval order. `0x8004` = child count. `0x8005`/`0x8006` = identity. |

`N` is the node count (74 for glassjoe, 93 for `doc`, etc. — it differs per character). **Always use
the `0x8009` parent table and the `0x8011` flag order — never guess hierarchy from bone names.**
(Example gotcha: `doc`'s `shoulder_fix_l/r` parent to node1 `bip01`, not the upperarms, and sit at
the end of the node list.)

---

## 2. Animation run structure

Each of a character's animations (glassjoe has 38) is an ordered chunk run delimited by `0x7001`:

- `0x7001` — header (88 B). `u32[2]` = frame count, `u32[3]` = node count.
- `0x7002` — name (ASCII: `idle`, `jab_l`, `uppercut_r`, `knockdown_high_left`, …).
- Side tables `0x7110/7113/7004/7005/7006/7111/7114` (all-zero in idle; reserved: scale/events).
- `0x7003` — per-node **runtime flags** (bit families 24/28/29/30/31/76/78…). This is the game's own
  per-node track-format descriptor. **The bits select the quantization FORMAT** (see §4): `0x01` hinge-angle, `0x10` s16 quat, `0x20` 12-bit quat, `0x02` static, none-set = s8 quat. (Earlier drafts wrongly said the kind came
  from track *size* alone (see §4). `bit1 (=2)` marks a static node; the 76/78 family marks
  from chunk size alone.)
- `0x7007` = `21 × 0x3FFF` per frame (blend weights?). `0x7009` = frame-count-related. `0x7100` tiny per-node chunks.
- Per-node **rotation tracks** (`0x7101`) and **translation tracks** (`0x7102`).
- Morph runs use `0x5xxx` (MorphAnimControllerNode) — see §7.

---

## 3. Track → node alignment (this was subtly off-by-one; get it exactly right)

- **Rotation `0x7101` tracks** map to nodes **in order, skipping only node0 `root`.**
  (`0x7003[node0] = 0` literally means "root has no tracks.") So there are `N−1` rotation tracks.
  - **`bip01 footsteps` DOES get a rotation track.** It is the 3ds-Max biped gizmo — it carries
    per-anim `Rz(180)` flips, drives no skinning, and is harmless. Misassigning it to `bip01` made
    half the animations face backwards (the old ±90° body-yaw split) — a classic trap.
- **Translation `0x7102` tracks** map to nodes with `0x8011 == 0`, **in order, skipping node0.**
  (35 translation tracks for glassjoe.)
- `node1 'bip01'` translation is **WORLD-absolute**; all other translations are **local** offsets.
  Static (12 B) translations match `0x8010` to ~4 decimals.

---

## 4. Rotation tracks — PRECISION-based quantization (from `main.dol`)

Every rotation key is a **full quaternion** (or a single hinge angle) — never a component subset.
The earlier `{z}`/`{x,z}` "component-subset + derived-w" model was an artifact of reading 8-bit
quaternions as int16 pairs; the real formats were read from the game's own decode path.

The per-node `0x7003` flag bits select the quantization format:

| `0x7003` | key format | decode |
|---|---|---|
| `0x01` | 1 × s16 **wrapping angle** about local Z | `radians = raw * pi / 32768` (knees/elbows/finger hinges) |
| `0x10` | 4 × s16 quaternion | `/ 32768` (loaded via `psq_l` GQR6) |
| `0x20` | 4 × **12-bit** quaternion | `/ 2048`, nibble-packed `[b0 b1hi][b2 b1lo][b3 b4hi][b5 b4lo]` |
| _(none set)_ | 4 × s8 quaternion | `/ 128` (`psq_l` GQR7) — the tracks the old model misread as "2-comp" |
| `0x02` | static: a single key in one of the above formats | — |

Track byte size = `keyBytes × frameCount` (static = one key), keyBytes ∈ {8 (s16), 6 (12-bit), 4 (s8), 2 (hinge)}.
Game decode anchors (in `main.dol`): dispatch `0x80181acc`, chunk-ptr setup `0x801819b8`, key loaders
`0x80188b90` (s16) / `0x80188ba4` (12-bit) / `0x80188c0c` (s8), hinge angle `0x8018585c`, quaternion
accumulate/blend + runtime L/R mirror negation table `0x80185710`.

FK, root/`bip01` world semantics, and foot-planting (§6, §7) are unchanged.


## 5. Translation tracks

`0x7102` = `12 B / frame` = `XYZ f32` big-endian.

- `node1 'bip01'` translation is **WORLD-absolute** and is applied **RAW** — never rotate it by the
  root's animated rotation. (Root spins during knockdowns; rotating the world translation turned a
  correct "slide back / wobble / fall to the mat" into "lifted vertically + slid sideways".)
- All other translations are **local** offsets. Static nodes (`0x8011 == 1`) use `0x8010`.

---

## 6. Forward kinematics & world frame

Standard Hamilton product, **no conjugation**, quats decoded as `xyzw int16 BE / 32767`:

```
wq[n] = wq[parent] · q[n]
wp[n] = wp[parent] + rot(wq[parent], trans[n])
```

- **node0 `root` is an engine placement transform** (ring facing / mirroring) — NOT part of the
  skeleton chain. Do not compose it into `bip01`.
- **`bip01` (node1, parent = root) is WORLD-absolute** in both rotation and translation. FK special
  case: `wp[1] = trans_track[f]`; rotation still composes `wq[1] = wq[0] · q[1]`.
- **World frame:** anim space has the character facing **+X** (backward −X, left/right ±Y, up +Z).
  The bind mesh faces **−Y**, so `nlg_anim2` applies `WORLD_YAW = Rz(−90)` to `bip01` so animations
  land in the rest-mesh frame.

### 6.1 Skinning uses BoneData; FK does not

Animation quats compose in the **node hierarchy** (root → bip01 → pelvis …), whose local frames
differ from the `BoneData` bind frames. **Do not mix the two.** `BoneData` bind matrices are needed
**only for skinning**: `skin = W_node · inv(BindWorld_bone)`.

> **BoneData rotation is COLUMN-major** (transpose it). Reading it row-major conjugates every bind
> quat — the "twisted arms / cooked face" bug. Proof: with the transpose, static rotation tracks
> equal the `BoneData` bind-local quats to 0.0000 (bicep, eyes, jaw, lips, tongue, nose…).

---

## 7. Foot planting

The raw tracks leave every character's feet slightly tilted/hovering (gj +8°, hondo +11°, kinghippo
+4°, DK +25°) — **the game levels feet at runtime with foot-plant IK.** `nlg_anim2.pose()` runs a
planting pass: when a foot/toe is near the ground (`z < 0.30`) and its sole is within 50° of flat,
the foot's world rotation is leveled (sole normal → +z) and the toe re-seated. Knockdown/lying anims
are skipped by the tilt gate. Result: 16/16 feet across 8 characters at 0.0° sole tilt. Disable with
`Rig.ground_feet = False`.

---

## 8. Facial animation: skeletal face bones **plus** a vertex-morph system

Faces use **two** mechanisms. Fighters like Glass Joe and Von Kaiser have skeletal `face_*` bones
(brow/cheek/lip/jaw) that the decoder above handles. But there is **also a vertex-morph (blend-shape)
system** — and it is what drives characters with little or no face rig. Donkey Kong is the proof: he
has only `face_eye`/`face_pupil` bones, his mouth/eyelid meshes (`dk_mouth_top`, `dk_mouth_bottom`,
`dk_eyelids`) are weighted **only to the head bone**, yet in-game he opens his mouth wide and quivers
his lip — smooth geometry motion no texture swap or head bone could produce.

The morph data lives **inside the `0x7xxx` animation block** (which is why it was easy to miss among
the skeletal tracks), per animation, and its size scales with facial expressiveness:

| chunk | idle | taunt_kiss | stun | blink | role |
|-------|------|-----------|------|-------|------|
| `0x7007` | 50 | 154 | 74 | 30 | 1 × int16 per frame — master/blend weight |
| `0x7008` | 300 | 924 | 444 | 180 | 12 bytes/frame — per-frame morph payload (packed) |
| `0x700B` | 350 | 1078 | 518 | 210 | 14 bytes/frame — per-frame morph payload |

**Target names.** `0x700A` is the list of target-name hashes a clip animates, in that clip's channel order. The model's `0xB00C` channels carry no names, so a channel takes the hash most clips list at its position (`nlg_morph.canonical_targets`) and clip channels are matched to model channels by hash. None of the names exist as strings in the game files; the readable ones in `nlg_morph.TARGET_NAMES` (`blink_r_top`, `damage_lip`, `dk_smile`, `pantsdrop`, ...) were recovered by hashing candidate names. Blender shape keys are named `<channel>_<target>`, e.g. `04_blink_r_top`; unknown hashes show as `target_XXXXXXXX`.

`0x7009` holds per-channel frame counts. The blend-shape **target**
geometry is stored once (not per-anim) in `0xB00C` (~69 KB) and/or `0xD002` (`0xD001` = channel count,
e.g. 7 for DK). **Status: decoded; imported as Blender shape keys**
(exact `0x7008`/`0x700B` field layout and target→vertex mapping). This system is roster-wide (every
fighter carries `0x7007/0x7008/0x700B` per animation).

*(Texture swaps also exist — DK has `donkey_mouth1..4` and `donkey_eye_0/2` textures — but those
complement the morph shapes; they are not what produces the lip quiver.)*

The `0x5xxx` chunks — present in every real fighter (34/95 archives) — are the game's **keyframed
camera system**: the cinematic/gameplay cameras for that fighter's star-punch, knockdown, intro, and
VS shots. Every `0x5002` chunk is a camera *name*; across all archives they read as
`Camera01` (×166), `zoomcam_<fighter>`, `ZoomOutCam_*`, `gp_camera_<fighter>_1/2/3`, `Knockdown_Cam`,
`Major_VS`/`Minor_VS`/`World_VS`, `JumbotronIntro`, `camera_rotate_2_to_3`, `CameraMainMenu`, … Little
Mac has 17 because he appears in every cutscene; Glass Joe has 2.

### Camera track layout (per set — plain big-endian floats, no compression)

| chunk | contents |
|-------|----------|
| `0x5002` | name (ASCII) |
| `0x5031` | `u32` frame count |
| `0x5003` | position track `N × 3 f32` (`0x5022` = duplicate/tangent) |
| `0x5004` | orientation track `N × 4 f32`, unit quaternion per frame (`0x5024` = dup) |
| `0x5023` | look-at/target track `N × 3 f32` (`0x5025` = dup) |
| `0x5028` | `N × f32` keyframe times |
| `0x5026` | `N × f32` FOV per frame |
| `0x5033` / `0x5034` | `f32` params (~26.0 near; ~6.58 FOV base) |
| `0x5021` / `0x5032` | 64 B = 4×4 f32 initial transform |

This is trivially decodable and, via the repacker, enables **custom cutscene cameras**. It is not
needed for character faces. (Real hash-named `MorphAnimControllerNode` targets do exist, but in
environment/menu archives, where `0x5002` holds a 4-byte hash rather than an ASCII name.)

---

## 9. Blender importer checklist (the settled rules)

1. Hierarchy + rest offsets from `0x8009` / `0x8010` (node space).
2. Rotation tracks = nodes in order **skipping node0 only**; node0 = identity, no tracks.
3. `bip01` rot + trans are **WORLD-absolute** (compose nothing above it).
4. `footsteps` track: import or ignore — it drives no skinning.
5. Rotation decode: per-node `0x7003` selects the quant format (§4): hinge angle `×π/32768`, or a full
   quaternion at s16/`32768`, 12-bit/`2048`, or s8/`128`.
6. Translation tracks = `0x8011 == 0` nodes in order skipping node0; `node1` = WORLD.
7. Optional `Rz(−90)` world yaw to match the −Y-facing rest mesh.
8. `BoneData` 4×4: rotation column-major (**transpose**), translation row 3 — for skin binding only, not FK.

`io_import_punchout.py` calls `rig.pose()` directly, so improvements to the decoder flow through with
just a re-import (no plugin change).

---

## 10. `vonkaiser`-class compact rigs

Von Kaiser and similar fighters store rotation tracks as **4 × s8 quaternions** (`/128`), which the
an earlier decoder misread as 2-component `{x,z}` pairs — that misread collapsed the body into a heap.
Reading them as full s8 quaternions (§4) fixes the whole class: the body assembles, the head sits at
z ≈ 2.0, and the toes reach the ground. (An earlier "bind-fill" hack in this repo is **obsolete** —
the precision-based decode is the real fix.) Residuals to check against the game: donflamenco's idle
feet sit ~30° (likely an authored flamenco stance), and a few VK mid-anim frames exceed the
foot-plant gates.


## 11. Known remaining

Rotation, morph (facial shape keys), the animation encoder, and materials are all decoded. Open items:

- **Damage-state toggling** — the model archive has no per-mesh damage flag; which meshes/marks show
  when a fighter is hurt lives outside the archive (character definition / `.bun` / code). Not yet found.
- **Adding a genuinely new material or a 9th texture slot** — the 204 B material record is fixed-size,
  and the shader (`hippodiffuseskin`) lives in `main.dol`; only slot retargeting / repaint / tint work.
- **A true new roster slot** (31st selectable) — replacing a fighter works; adding one likely needs
  `main.dol` patching.
- **Foot-plant residuals** — donflamenco idle feet ~30° (maybe an authored stance) and a few VK
  mid-anim frames; verify against the game before "fixing".
