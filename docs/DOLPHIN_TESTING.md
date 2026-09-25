# Testing mods in Dolphin

A fast loop for loading modified archives and inspecting them in game.

## 1. Riivolution: no ISO rebuilds

Dolphin can patch modified files in at boot, so you never need to rebuild the disc image.

- Copy `punchout_mods.xml` to `Documents\Dolphin Emulator\Load\Riivolution\`.
- Create `Documents\Dolphin Emulator\Load\Riivolution\po_mod\art\characters\` and put the
  modified `<fighter>.dict` + `<fighter>.data` there.
- Right-click Punch-Out!! -> **Start With Riivolution Patches...** -> enable "Character Files" ->
  Save as Preset -> launch.
- To test a new build, overwrite those two files and restart the game.

The sample XML targets PAL `R7PP01`. Change the `<id game="...">` value for other regions.

## 2. Free Look camera

Dolphin can detach the camera so you can orbit a fighter and inspect any bone.
Bind the Free Look toggle and movement keys under **Hotkeys** (Free Look section), then hold the
toggle key in game and use WASD/mouse to move the camera.

## 3. Save states

Start a fight, let the opponent reach idle, then **Save State** (Shift+F1 by default). After each
reload, **Load State** (F1) returns straight to the fight without menus.

## 4. Gecko codes (PAL R7PP01)

Enable cheats (Config -> General -> "Enable Cheats"), then right-click Punch-Out!! ->
Properties -> **Gecko Codes** -> Add New Code. These codes (from GameHacking.org) are for PAL
`R7PP01` only; NTSC codes differ.

### Infinite Round Time

```
48000000 804148A8
DE000000 80008180
1400008C 426C0000
E0000000 80008000
```

### Infinite Health

```
C20181D8 00000009
2C150000 40820038
2C190000 40820030
3F20746C 6339654D
82A3FF08 7C15C800
40820014 3F204300
93230010 93230014
93230018 3B200000
3AA00000 C0430010
60000000 00000000
```

### All Enemies Unlocked

```
C20A7544 00000002
3980001A 91830000
80630000 00000000
```

## Suggested loop

1. Export a modified archive pair.
2. Copy it into `Load\Riivolution\po_mod\art\characters\`.
3. Start an Exhibition match against that fighter with Infinite Round Time on, or load a save state.
4. Use Free Look to inspect the result.
