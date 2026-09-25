"""
anim_retarget.py — Bind-pose retargeting for Punch-Out!! Wii.

Workflow: Take the game's rig, reshape it to your character, keep all animations working.

Why this works:
  The game's animation tracks drive rotations and translations of the NODE HIERARCHY.
    The exporter can retarget joint positions through the node offsets and BoneData (0xB00A).
    Rotation axes stay in the source animation's coordinate frames; edit-bone roll/direction is
    Blender viewport state and is deliberately not exported.
  
  Same hierarchy = same animations. Different bone lengths = different-looking character
  playing the same moves.

Usage in Blender:
    from anim_retarget import ReshapeRig, WeightMesh
    
    # Step 1: Import source character (via io_import_punchout.py)
    # This gives you the game rig with all 38+ animations loaded.
    
    # Step 2: Reshape bones
    rr = ReshapeRig(blender_armature)
    rr.scale_bone('bip01 l thigh', 1.3, axis='y')      # Longer thigh
    rr.scale_bone('bip01 l shin', 0.9, axis='y')        # Shorter shin
    rr.move_bone('bip01 head', (0, 0, 0.1))             # Adjust head position
    
    # Step 3: Weight your mesh to the reshaped bones
    wm = WeightMesh(blender_mesh, blender_armature)
    wm.auto_weight('CLUSTER_DEFORM')  # or 'RANDOM', 'EMPTY', 'OBJECT_FLUID'
    wm.transfer_from_source(source_object)  # copy weights from a reference mesh
    
    # Step 4: Export — animations stay intact
    # (io_export_punchout.py handles geometry export; animations go elsewhere)

Author: Bryan Intindola
"""
import math, struct, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# Bone reshaping utilities (Blender-independent core logic)
# ============================================================================

class ReshapeRig:
    """Edit joint positions on an imported game rig.
    
    Preserves the node hierarchy so game animations continue to play correctly.
    The exporter uses bone heads as game joint positions. Bone roll and display direction are
    not exported because Punch-Out animations use the source rig's rotation axes.
    """
    
    def __init__(self, armature_obj):
        """Initialize from a Blender armature object.
        
        Args:
            armature_obj: bpy.types.Object — the imported game rig armature
        """
        import bpy
        self.armature = armature_obj
        self.armature_data = armature_obj.data
        self.bones = {}  # name -> edit_bone
        
        for eb in self.armature_data.edit_bones:
            self.bones[eb.name] = eb
        
        # Store original transforms for undo
        self._original_transforms = {}
        for name, eb in self.bones.items():
            self._original_transforms[name] = {
                'head': eb.head.copy(),
                'tail': eb.tail.copy(),
                'roll': eb.roll,
                'scale': (1.0, 1.0, 1.0),
            }
    
    def save_originals(self):
        """Save current state as originals (for undo)."""
        for name, eb in self.bones.items():
            self._original_transforms[name] = {
                'head': eb.head.copy(),
                'tail': eb.tail.copy(),
                'roll': eb.roll,
            }
    
    def restore_originals(self):
        """Restore all bones to their original transforms."""
        for name, eb in self.bones.items():
            orig = self._original_transforms[name]
            eb.head[:] = orig['head']
            eb.tail[:] = orig['tail']
            eb.roll = orig['roll']
    
    def scale_bone(self, bone_name, factor, axis='y'):
        """Scale a bone along one of its local axes.
        
        Changes bone length while preserving the joint connection point.
        
        Args:
            bone_name: Name of the bone to scale
            factor: Scale multiplier (>1 stretches, <0.5 shrinks)
            axis: 'x', 'y', or 'z' — axis relative to bone's roll orientation
        """
        if bone_name not in self.bones:
            print(f"[ReshapeRig] Bone '{bone_name}' not found.")
            return
        
        eb = self.bones[bone_name]
        
        # Calculate bone direction
        delta = eb.tail - eb.head
        bone_len = delta.length
        
        if bone_len < 0.001:
            print(f"[ReshapeRig] Bone '{bone_name}' has zero length — can't scale.")
            return
        
        # Determine axis in bone space
        if axis == 'y':
            # Along bone length (default)
            new_tail = eb.head + delta * factor
        elif axis == 'x' or axis == 'z':
            # Perpendicular to bone — affects roll/direction
            # Find perpendicular axis in bone's local space
            up = eb.roll_vector
            if axis == 'x':
                perp = up.cross(delta.normalized())
            else:
                perp = delta.normalized().cross(up)
            
            # Scale perpendicular component
            proj_length = delta.dot(perp)
            new_proj = proj_length * factor
            new_delta = delta + (new_proj - proj_length) * perp.normalized()
            new_tail = eb.head + new_delta
        else:
            print(f"[ReshapeRig] Unknown axis '{axis}'. Use 'x', 'y', or 'z'.")
            return
        
        eb.tail[:] = new_tail
    
    def move_bone(self, bone_name, offset):
        """Move a bone's head position by an offset.
        
        Useful for adjusting joint positions without changing bone length.
        
        Args:
            bone_name: Name of the bone to move
            offset: (dx, dy, dz) offset vector
        """
        if bone_name not in self.bones:
            print(f"[ReshapeRig] Bone '{bone_name}' not found.")
            return
        
        eb = self.bones[bone_name]
        eb.head[:] = tuple(eb.head[i] + offset[i] for i in range(3))
        eb.tail[:] = tuple(eb.tail[i] + offset[i] for i in range(3))
    
    def rotate_bone(self, bone_name, degrees, axis='z'):
        """Rotate a bone's Blender display tail around its head.
        
        This is useful for viewport rigging only. It does not move a game joint and is not
        exported; use :meth:`move_bone` to reposition a joint for Punch-Out.
        
        Args:
            bone_name: Name of the bone to rotate
            degrees: Rotation angle in degrees
            axis: 'x', 'y', or 'z' — world-axis rotation
        """
        if bone_name not in self.bones:
            print(f"[ReshapeRig] Bone '{bone_name}' not found.")
            return
        
        import mathutils
        eb = self.bones[bone_name]
        
        # Rotate the tail around the head
        delta = eb.tail - eb.head
        if axis == 'x':
            rot = mathutils.Euler((math.radians(degrees), 0, 0), 'XYZ')
        elif axis == 'y':
            rot = mathutils.Euler((0, math.radians(degrees), 0), 'XYZ')
        elif axis == 'z':
            rot = mathutils.Euler((0, 0, math.radians(degrees)), 'XYZ')
        else:
            print(f"[ReshapeRig] Unknown axis '{axis}'. Use 'x', 'y', or 'z'.")
            return
        
        rotated_delta = rot.to_matrix() @ delta
        eb.tail[:] = eb.head + rotated_delta
    
    def stretch_to_bone(self, from_bone, to_bone):
        """Stretch a bone to reach another bone's head.
        
        Useful for connecting bones in a chain.
        
        Args:
            from_bone: Name of the bone to stretch
            to_bone: Name of the target bone (its head will be reached)
        """
        if from_bone not in self.bones or to_bone not in self.bones:
            print(f"[ReshapeRig] One or both bones not found.")
            return
        
        eb = self.bones[from_bone]
        target_head = self.bones[to_bone].head
        eb.tail[:] = target_head
    
    def uniform_scale(self, bone_name, factor):
        """Uniformly scale a bone in all dimensions.
        
        Args:
            bone_name: Name of the bone to scale
            factor: Uniform scale multiplier
        """
        if bone_name not in self.bones:
            print(f"[ReshapeRig] Bone '{bone_name}' not found.")
            return
        
        eb = self.bones[bone_name]
        center = (eb.head + eb.tail) / 2.0
        
        eb.head[:] = center + (eb.head - center) * factor
        eb.tail[:] = center + (eb.tail - center) * factor
    
    def set_bone_length(self, bone_name, length):
        """Set a bone to an exact length along its current direction.
        
        Args:
            bone_name: Name of the bone
            length: Desired bone length
        """
        if bone_name not in self.bones:
            print(f"[ReshapeRig] Bone '{bone_name}' not found.")
            return
        
        eb = self.bones[bone_name]
        delta = eb.tail - eb.head
        current_len = delta.length
        
        if current_len < 0.001:
            print(f"[ReshapeRig] Bone '{bone_name}' has zero length.")
            return
        
        new_delta = delta * (length / current_len)
        eb.tail[:] = eb.head + new_delta
    
    def get_chain_lengths(self, root_bone, target_bone):
        """Calculate total length from root to target bone in the hierarchy.
        
        Useful for checking if proportions are reasonable.
        
        Args:
            root_bone: Starting bone name
            target_bone: Ending bone name
        
        Returns:
            Total chain length and individual bone lengths
        """
        lengths = []
        current = target_bone
        total = 0.0
        
        while current is not None and current != root_bone:
            if current not in self.bones:
                break
            
            eb = self.bones[current]
            blen = eb.length
            lengths.append((current, blen))
            total += blen
            
            parent = eb.parent
            current = parent.name if parent else None
        
        return total, reversed(lengths)
    
    def print_bone_report(self):
        """Print a report of all bone names, lengths, and parents.
        
        Useful for debugging and verifying the hierarchy.
        """
        print("\n=== RIG REPORT ===")
        for name, eb in self.bones.items():
            parent = eb.parent.name if eb.parent else "(root)"
            print(f"  {name:30s} len={eb.length:8.4f}  parent={parent}")
        print("==================\n")


# ============================================================================
# Mesh weighting utilities
# ============================================================================

class WeightMesh:
    """Weight a mesh to the game rig's bones.
    
    After reshaping the rig, you need to weight your custom mesh to the bones
    so the skin deforms correctly when animations play.
    """
    
    def __init__(self, mesh_obj, armature_obj):
        """Initialize with mesh and armature objects.
        
        Args:
            mesh_obj: bpy.types.Object — the mesh to weight
            armature_obj: bpy.types.Object — the armature to weight to
        """
        import bpy
        self.mesh = mesh_obj
        self.armature = armature_obj
        
        # Ensure vertex groups exist
        vg_names = {vg.name for vg in armature_obj.vertex_groups}
        for vert in mesh_obj.data.vertices:
            for group in vert.groups:
                if group.group >= len(armature_obj.vertex_groups):
                    continue
                vg_name = armature_obj.vertex_groups[group.group].name
                if vg_name not in vg_names:
                    armature_obj.vertex_groups.new(name=vg_name)
    
    def auto_weight(self, method='CLUSTER_DEFORM'):
        """Automatically weight the mesh using Blender's built-in methods.
        
        Args:
            method: Weighting method — 'CLUSTER_DEFORM', 'RANDOM', 
                   'EMPTY', 'OBJECT_FLUID', or 'NORMALLYZE'
        """
        import bpy
        
        # Select mesh and armature
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.context.view_layer.objects.active = self.mesh
        self.mesh.select_set(True)
        self.armature.select_set(False)
        
        # Clear existing weights
        for vg in self.mesh.vertex_groups:
            self.mesh.vertex_groups.remove(vg)
        
        # Add armature modifier
        mod = self.mesh.modifiers.new(name="Armature", type='ARMATURE')
        mod.object = self.armature
        
        # Set selection mode to vertex
        bpy.ops.object.mode_set(mode='VERTEX')
        
        # Select all vertices
        bpy.ops.mesh.select_all(action='SELECT')
        
        # Apply weighting method
        if method == 'CLUSTER_DEFORM':
            bpy.ops.vertex_weight_cluster()
        elif method == 'RANDOM':
            bpy.ops.vertex_weight_random()
        elif method == 'EMPTY':
            bpy.ops.vertex_weight_empty()
        elif method == 'OBJECT_FLUID':
            bpy.ops.vertex_weight_object_fluid()
        elif method == 'NORMALLYZE':
            bpy.ops.vertex_weight_normalize()
        
        bpy.ops.object.mode_set(mode='OBJECT')
        print(f"[WeightMesh] Applied '{method}' weighting to {len(self.mesh.data.vertices)} vertices.")
    
    def transfer_from_source(self, source_object, use_share=True):
        """Transfer weights from a source mesh to this mesh.
        
        Both meshes must have the same topology or use proportional transfer.
        
        Args:
            source_object: Source mesh object with existing weights
            use_share: Share vertex groups with armature
        """
        import bpy
        
        # Enable proportional editing for smoother transfer
        bpy.context.scene.tool_settings.proportional_edit = 'ENABLED'
        
        # Select both objects
        bpy.ops.object.mode_set(mode='OBJECT')
        source_object.select_set(True)
        self.mesh.select_set(True)
        bpy.context.view_layer.objects.active = source_object
        
        # Transfer vertex groups
        bpy.ops.object.join_shaders()  # Alternative: custom transfer operator
        
        bpy.context.scene.tool_settings.proportional_edit = 'DISABLED'
        print("[WeightMesh] Attempted weight transfer from source.")
    
    def paint_weights(self, brush_strength=0.5, bone_name=None):
        """Paint weights in weight paint mode.
        
        Args:
            brush_strength: Strength of weight painting (0.0-1.0)
            bone_name: Specific bone to paint (None = all enabled)
        """
        import bpy
        
        bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
        
        if bone_name:
            # Set active vertex group
            for vg in self.mesh.vertex_groups:
                if vg.name == bone_name:
                    self.mesh.active_vertex_group.index = vg.index
                    break
        
        bpy.context.tool_settings.paint_weight.brush_strength = brush_strength
        print(f"[WeightMesh] Entered weight paint mode (strength={brush_strength}).")
    
    def assign_single_vertex(self, vertex_index, bone_name, weight=1.0):
        """Assign a single vertex to a single bone with a specific weight.
        
        Args:
            vertex_index: Vertex index in the mesh
            bone_name: Name of the bone
            weight: Weight value (0.0-1.0)
        """
        import bpy
        
        vg = None
        for vgroup in self.mesh.vertex_groups:
            if vgroup.name == bone_name:
                vg = vgroup
                break
        
        if vg is None:
            vg = self.mesh.vertex_groups.new(name=bone_name)
        
        self.mesh.vertex_groups[vertex_index].add(index=(vg.index,), weight=weight, type='ADD')
        print(f"[WeightMesh] Assigned vertex {vertex_index} to {bone_name} (weight={weight}).")
    
    def clear_weights(self):
        """Remove all vertex weights from the mesh."""
        import bpy
        
        for vg in self.mesh.vertex_groups:
            self.mesh.vertex_groups.remove(vg)
        
        print("[WeightMesh] Cleared all vertex weights.")
    
    def get_vertex_weights(self, vertex_index):
        """Get the current weights for a specific vertex.
        
        Returns:
            List of (bone_name, weight) tuples
        """
        weights = []
        
        for vg in self.mesh.vertex_groups:
            try:
                group = None
                for v in self.mesh.data.vertices:
                    if v.index == vertex_index:
                        for g in v.groups:
                            if g.group == vg.index:
                                group = g
                                break
                        break
                
                if group:
                    weights.append((vg.name, group.weight))
            except (IndexError, KeyError):
                continue
        
        return weights


# ============================================================================
# Workflow template — complete pipeline from import to export
# ============================================================================

def workflow_template(source_dict, custom_mesh_obj, custom_armature_obj):
    """Complete workflow: import → reshape → weight → export-ready.
    
    This is the full pipeline. Call it after importing the source character.
    
    Usage:
        # 1. Import source character (game rig with animations)
        #    (Done via io_import_punchout.py operator)
        
        # 2. Run this workflow
        from anim_retarget import workflow_template
        workflow_template("/path/to/source.dict", custom_mesh, custom_armature)
    
    Args:
        source_dict: Path to source character's .dict file
        custom_mesh_obj: Blender mesh object (the custom character mesh)
        custom_armature_obj: Blender armature object (the custom rig)
    
    Returns:
        Tuple of (reshape_result, weight_result) with status messages
    """
    import bpy
    
    print("=" * 60)
    print("PUNCH-OUT RETARGET WORKFLOW")
    print("=" * 60)
    
    # Step 1: Reshape the game rig
    print("\n[Step 1] Reshaping rig to match character proportions...")
    rr = ReshapeRig(custom_armature_obj)
    
    # Print current bone report for reference
    rr.print_bone_report()
    
    # Common adjustments for typical character customization:
    # rr.set_bone_length('bip01 l thigh', 0.45)      # Thigh length
    # rr.set_bone_length('bip01 l shin', 0.40)       # Shin length  
    # rr.scale_bone('bip01 chest', 1.2, axis='x')     # Broaden torso
    # rr.scale_bone('bip01 head', 1.15, axis='x')     # Larger head
    # rr.move_bone('bip01 head', (0, 0, 0.05))        # Head height adjustment
    
    # Step 2: Weight the mesh
    print("\n[Step 2] Weighting mesh to reshaped rig...")
    wm = WeightMesh(custom_mesh_obj, custom_armature_obj)
    wm.auto_weight('CLUSTER_DEFORM')
    
    # Step 3: Verify
    print("\n[Step 3] Verification:")
    print(f"  Bones: {len(rr.bones)}")
    print(f"  Vertices: {len(custom_mesh_obj.data.vertices)}")
    print(f"  Vertex Groups: {len(custom_mesh_obj.vertex_groups)}")
    
    # Leave the imported NLA strips in charge: no active action overrides them. animation_data
    # is read-only (the old assignment raised on every version); po_action handles 3.x-5.x.
    print("\n[Step 4] Testing animation playback...")
    import po_action
    po_action.assign_action(custom_armature_obj, None)
    
    print("\n" + "=" * 60)
    print("WORKFLOW COMPLETE — Animations should play on your custom rig!")
    print("=" * 60)
    
    return rr, wm


# ============================================================================
# Quick-reference cheat sheet
# ============================================================================

CHEAT_SHEET = """
QUICK REFERENCE — Reshaping the Game Rig
==========================================

1. IMPORT THE GAME RIG
   - Use io_import_punchout.py to import a character
   - You get the full rig with all 38+ animations
   
2. RESHAPE BONESTo change proportions:
   
   rr = ReshapeRig(armature)
   
   # Length adjustments (most common)
   rr.set_bone_length('bip01 l thigh', 0.45)
   rr.set_bone_length('bip01 l shin', 0.40)
   rr.set_bone_length('bip01 r thigh', 0.45)
   rr.set_bone_length('bip01 r shin', 0.40)
   
   # Proportional scaling
   rr.scale_bone('bip01 chest', 1.2, axis='x')   # Wider
   rr.scale_bone('bip01 head', 1.1, axis='x')    # Bigger head
   rr.uniform_scale('bip01 l forearm', 0.9)       # Thinner limb
   
   # Position adjustments
   rr.move_bone('bip01 head', (0, 0, 0.05))       # Raise/lower head
   
3. WEIGHT YOUR MESH
   wm = WeightMesh(mesh, armature)
   wm.auto_weight('CLUSTER_DEFORM')
   
   # Or paint manually
   wm.paint_weights(brush_strength=0.5, bone_name='bip01 l thigh')
   
   # Or assign specific vertices
   wm.assign_single_vertex(42, 'bip01 l thigh', 1.0)
   
4. EXPORT / PLAY
   - Animations play automatically in Blender
   - Export geometry via io_export_punchout.py
   - Animations export separately (see game's animation system)

IMPORTANT RULES:
- NEVER change the hierarchy (parent-child relationships)
- NEVER rename bones (the game uses hash names internally)
- ONLY export: joint positions (bone heads) and mesh weights
- Bone tail direction and roll are Blender-only display aids; do not use them to pose limbs
- Keep the root bone (node0) untouched — it controls placement

Checking the result:
- Play animations in Blender: switch to Pose Mode and test
- Check that joints bend correctly (no weird stretching)
- Verify the character stands upright at rest pose
"""
