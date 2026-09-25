"""Blender action adapter shared by every Punch-Out Blender module.

Blender 3.x-4.3 actions are flat: `Action.fcurves`. Blender 4.4 introduced slotted/layered
actions (slot -> layer -> keyframe strip -> channelbag -> fcurves) and 5.0 removed the flat
`Action.fcurves`. Code creates, assigns, reads and NLA-stacks actions only through these
helpers, so one code path behaves the same in 3.6 and 5.x. A 4.4+ action made here holds one
slot per animated datablock, one layer and one keyframe strip, which is what Blender itself
creates when keying and what files saved by 3.x are versioned into on load.
"""
import bpy

LAYERED = bpy.app.version >= (4, 4, 0)


class ActionLayoutError(ValueError):
    pass


def new_action(datablock, name, assign=True):
    """(action, slot) for one datablock (object, shape-key block, ...). slot is None before 4.4."""
    action = bpy.data.actions.new(name)
    slot = None
    if LAYERED:
        slot = action.slots.new(id_type=datablock.id_type, name=datablock.name)
        action.layers.new(name="Layer").strips.new(type="KEYFRAME").channelbags.new(slot)
    if assign:
        assign_action(datablock, action, slot)
    return action, slot


def only_slot(action):
    if not LAYERED:
        return None
    if len(action.slots) != 1:
        raise ActionLayoutError("Action %r has %d slots; choose one explicitly" % (action.name, len(action.slots)))
    return action.slots[0]


def assign_action(datablock, action, slot=None):
    """Make `action` (or None) the datablock's active action, selecting its slot on 4.4+."""
    ad = datablock.animation_data or datablock.animation_data_create()
    ad.action = action
    if LAYERED and action is not None:
        slot = slot or only_slot(action)
        if ad.action_slot != slot:
            ad.action_slot = slot
    return ad


def slot_target(slot):
    """ID type a slot animates ('OBJECT', 'KEY', ...); None for legacy actions."""
    return None if slot is None else getattr(slot, "target_id_type", None) or getattr(slot, "id_root", None)


def active_slot(datablock):
    ad = datablock.animation_data
    return ad.action_slot if LAYERED and ad is not None else None


def _channelbag(action, slot, ensure):
    if len(action.layers) == 0:
        if not ensure:
            return None
        action.layers.new(name="Layer")
    layer = action.layers[0]
    if len(layer.strips) == 0:
        if not ensure:
            return None
        layer.strips.new(type="KEYFRAME")
    return layer.strips[0].channelbag(slot, ensure=ensure)


def fcurves(action, slot=None, ensure=False):
    """The F-curve collection of one slot (legacy: Action.fcurves). With `slot` None the action
    must have exactly one slot. Returns () for a slot that has no curves and ensure=False."""
    if not LAYERED:
        return action.fcurves
    slot = slot or only_slot(action)
    bag = _channelbag(action, slot, ensure)
    return bag.fcurves if bag is not None else ()


def all_fcurves(action):
    """Every F-curve of every slot, layer and strip (read-only inspection)."""
    if not LAYERED:
        return list(action.fcurves)
    return [fc for layer in action.layers for strip in layer.strips
            for bag in strip.channelbags for fc in bag.fcurves]


def layout(action):
    """(layers, strips, channelbags, slots); legacy actions report (0, 0, 0, 0)."""
    if not LAYERED:
        return (0, 0, 0, 0)
    strips = [s for layer in action.layers for s in layer.strips]
    return (len(action.layers), len(strips), sum(len(s.channelbags) for s in strips), len(action.slots))


def new_fcurve(action, data_path, index=0, group=None, slot=None):
    curves = fcurves(action, slot, ensure=True)
    fc = curves.new(data_path, index=index)
    if group:
        groups = action.groups if not LAYERED else _channelbag(action, slot or only_slot(action), True).groups
        fc.group = groups.get(group) or groups.new(group)
    return fc


def has_curves(action):
    return bool(all_fcurves(action))


def add_strip(track, name, start, action, slot=None):
    """NLA strip bound to the action's slot for this track's datablock (4.4+)."""
    strip = track.strips.new(name, int(start), action)
    if LAYERED:
        slot = slot or only_slot(action)
        if strip.action_slot != slot:
            strip.action_slot = slot
    return strip
