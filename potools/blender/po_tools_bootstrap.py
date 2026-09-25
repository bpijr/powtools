bl_info = {
    "name": "Punch-Out!! Wii Tools (bootstrap)",
    "author": "Bryan Intindola",
    "version": (1, 0),
    "blender": (3, 0, 0),
    "location": "File > Import/Export > Punch-Out!! Asset ; View3D sidebar > PO Tools",
    "description": "Loads the Punch-Out!! importer/exporter in place from the potools folder.",
    "category": "Import-Export",
}

# ---------------------------------------------------------------------------------------
# WHY THIS FILE EXISTS
#
# Blender's "Install from Disk" copies the ONE .py you picked into its addons folder. The
# importer and exporter both do `import po_shader` (and reach for anim_retarget and the nlg_*
# modules), and those stay behind in potools -- hence "No module named 'po_shader'".
#
# So: install THIS file instead. It is the only file Blender ever copies. It finds the real
# potools folder, puts its formats/, tools/ and blender/ folders on sys.path, and registers
# the addons from where they actually live -- so your edits are the ones that run, with no copies
# to keep in sync. Hit F3 > "Reload Scripts" after editing and the changes are picked up.
#
# Resolution order for the folder: PO_POTOOLS env var -> the addon preference below ->
# this file's own folder. Set it in
# Edit > Preferences > Add-ons > Punch-Out!! Wii Tools if auto-detect misses.
# ---------------------------------------------------------------------------------------

import bpy
import os
import sys
import importlib

SUBMODULES = ("io_punchout_texture_animation", "io_import_punchout", "io_export_punchout", "io_punchout_asset", "io_punchout_material",
              "io_punchout_animation", "io_punchout_cinematic", "io_punchout_effects", "io_punchout_ui")
MARKER = os.path.join("blender", "po_shader.py")   # proves a folder really is potools
FOLDERS = ("formats", "tools", "blender")          # put on sys.path, last one first

_loaded = []
_last_error = ""


def _is_potools(path):
    return bool(path) and os.path.isfile(os.path.join(path, MARKER))


def _resolve_dir():
    """Find potools. Returns the path, or "" if every candidate missed."""
    candidates = [os.environ.get("PO_POTOOLS", "")]

    try:                                        # the addon preference, if one is set
        prefs = bpy.context.preferences.addons[__name__].preferences
        candidates.append(bpy.path.abspath(prefs.potools_dir))
    except Exception:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.dirname(here))    # running in place from potools/blender
    candidates.append(os.path.join(here, "potools"))

    for c in candidates:
        if _is_potools(c):
            return os.path.normpath(c)
    return ""


class PO_BOOT_Preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    potools_dir: bpy.props.StringProperty(
        name="potools folder",
        description="The potools folder (holds blender/, formats/ and tools/)",
        subtype="DIR_PATH",
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "potools_dir")
        found = _resolve_dir()
        if found:
            layout.label(text="Using: %s" % found, icon="CHECKMARK")
            layout.label(text="Loaded: %s" % (", ".join(_loaded) or "nothing yet"),
                         icon="SCRIPT")
        else:
            layout.label(text="Could not find %s -- set the folder above" % MARKER,
                         icon="ERROR")
        if _last_error:
            layout.label(text=_last_error, icon="ERROR")
        layout.label(text="Blender %s; minimum 3.0, tested in 3.6 and 5.2. Export to a separate folder."
                     % bpy.app.version_string)
        layout.label(text="Experimental: validate exports and test them in game.")
        layout.operator("po.boot_reload", icon="FILE_REFRESH")


class PO_BOOT_OT_reload(bpy.types.Operator):
    """Re-read the addon files from potools without restarting Blender"""
    bl_idname = "po.boot_reload"
    bl_label = "Reload PO Tools"

    def execute(self, context):
        _unload()
        ok, msg = _load()
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        return {"FINISHED"} if ok else {"CANCELLED"}


# The unified export and every PO Tools panel live in
# io_punchout_ui (loaded last, so it can fold the per-format File menu entries into one).


def _load():
    """Put the potools folders on sys.path, then import and register each submodule."""
    global _last_error
    _last_error = ""

    if bpy.app.version < (3, 0, 0):
        _last_error = "Blender 3.0 or newer is required. Newer versions still need local testing."
        return False, _last_error

    d = _resolve_dir()
    if not d:
        _last_error = ("Could not locate potools. Set it in Preferences > Add-ons > "
                       "Punch-Out!! Wii Tools, or set the PO_POTOOLS environment variable.")
        return False, _last_error

    for folder in FOLDERS:       # ahead of everything, so our modules win any name clash
        path = os.path.join(d, folder)
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)

    problems = []
    # These modules have no register() function, but are cached by the importers.
    # Reloading just the UI previously left the old cutscene and shader code active.
    for name in ("nlg_texture_animation", "nlg_cutscene", "po_shader", "io_punchout_cutscene"):
        if name in sys.modules:
            try:
                importlib.reload(sys.modules[name])
            except Exception as ex:
                problems.append("%s: %s" % (name, ex))
    for name in SUBMODULES:
        if not os.path.isfile(os.path.join(d, "blender", name + ".py")):
            continue             # exporter is optional; skip quietly if it is not there
        try:
            mod = importlib.import_module(name)
            importlib.reload(mod)            # pick up edits on F3 > Reload Scripts
            if hasattr(mod, "register"):
                mod.register()
            _loaded.append(name)
        except Exception as ex:
            problems.append("%s: %s" % (name, ex))

    if problems:
        _last_error = " | ".join(problems)
        # partial success still counts -- one broken module should not hide a working one
        return bool(_loaded), _last_error
    if not _loaded:
        _last_error = "Found %s but registered nothing." % d
        return False, _last_error
    return True, "PO Tools loaded from %s (%s)" % (d, ", ".join(_loaded))


def _unload():
    for name in reversed(_loaded):
        mod = sys.modules.get(name)
        try:
            if mod is not None and hasattr(mod, "unregister"):
                mod.unregister()
        except Exception as ex:
            print("PO bootstrap: unregister %s failed: %s" % (name, ex))
    _loaded.clear()


def register():
    bpy.utils.register_class(PO_BOOT_Preferences)
    bpy.utils.register_class(PO_BOOT_OT_reload)
    ok, msg = _load()
    # Never raise here. A hard failure would make "enable addon" fail outright and there
    # would be no preferences panel left to point at the right folder.
    print("PO bootstrap:", msg)


def unregister():
    _unload()
    bpy.utils.unregister_class(PO_BOOT_OT_reload)
    bpy.utils.unregister_class(PO_BOOT_Preferences)


if __name__ == "__main__":       # allows Run Script straight from the Text Editor
    register()
