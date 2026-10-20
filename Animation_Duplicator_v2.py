bl_info = {
    "name": "Animation Duplicator",
    "author": "You",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "Graph Editor / Dope Sheet > Sidebar (N) > Animation Duplicator",
    "description": "Analyzes the value difference between 2 selected keyframes and fills "
                   "a frame range by adding or subtracting the difference every frame.",
    "category": "Animation",
}

import bpy
from bpy.props import EnumProperty, IntProperty, PointerProperty

PRECISION = 10  # decimals used for the analysis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_fcurves(id_data):
    """Return the F-Curves of an ID's active action (works with slotted actions)."""
    ad = getattr(id_data, "animation_data", None)
    if not ad or not ad.action:
        return []
    action = ad.action

    # Legacy API (Blender < 5.0)
    if hasattr(action, "fcurves"):
        return list(action.fcurves)

    # Slotted / layered actions (Blender 4.4+, required in 5.x)
    try:
        from bpy_extras import anim_utils
        slot = ad.action_slot
        if slot is None:
            return []
        channelbag = anim_utils.action_get_channelbag_for_slot(action, slot)
        return list(channelbag.fcurves) if channelbag else []
    except Exception:
        return []


def target_ids(context):
    ids = list(context.selected_objects)
    if context.active_object and context.active_object not in ids:
        ids.append(context.active_object)
    return ids


def selected_pairs(context):
    """Yield (id_data, fcurve, key_a, key_b, delta) for every channel that has
    exactly 2 selected keyframes."""
    seen = set()
    results = []
    for idb in target_ids(context):
        for fc in get_fcurves(idb):
            ptr = fc.as_pointer()
            if ptr in seen or fc.lock or fc.hide:
                continue
            seen.add(ptr)
            keys = sorted(
                (k for k in fc.keyframe_points if k.select_control_point),
                key=lambda k: k.co.x,
            )
            if len(keys) != 2:
                continue
            delta = round(keys[1].co.y - keys[0].co.y, PRECISION)
            results.append((idb, fc, keys[0], keys[1], delta))
    return results


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------
class AnimDupSettings(bpy.types.PropertyGroup):
    mode: EnumProperty(
        name="Operation",
        description="Add or subtract the analyzed difference on each new frame",
        items=[
            ('ADD', "Add", "Add the difference each frame"),
            ('SUBTRACT', "Subtract", "Subtract the difference each frame"),
        ],
        default='ADD',
    )
    frame_start: IntProperty(
        name="Start Frame",
        description="First frame to receive a generated keyframe. Frames at or before "
                    "the last selected keyframe are never overwritten",
        default=1, min=-1048574, max=1048574,
    )
    frame_end: IntProperty(
        name="End Frame",
        description="Last frame to receive a generated keyframe",
        default=100, min=-1048574, max=1048574,
    )


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------
class ANIMDUP_OT_calculate(bpy.types.Operator):
    bl_idname = "animdup.calculate_keyframes"
    bl_label = "Calculate Keyframes"
    bl_description = "Generate keyframes using the difference between the 2 selected keyframes"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects or context.active_object)

    def execute(self, context):
        s = context.scene.animdup
        pairs = selected_pairs(context)
        if not pairs:
            self.report({'WARNING'}, "Select exactly 2 keyframes in a channel first")
            return {'CANCELLED'}

        if s.frame_end < s.frame_start:
            self.report({'WARNING'}, "End Frame must be greater than or equal to Start Frame")
            return {'CANCELLED'}

        sign = 1.0 if s.mode == 'ADD' else -1.0
        channels = 0
        total = 0

        for _idb, fc, _ka, kb, delta in pairs:
            # Capture values before inserting (inserting may invalidate references)
            last_frame = int(round(kb.co.x))
            value = kb.co.y
            interp = kb.interpolation

            # Step frame by frame from the last selected key so the sequence stays
            # continuous, but only write keys inside the chosen range.
            for frame in range(last_frame + 1, s.frame_end + 1):
                value += sign * delta
                if frame < s.frame_start:
                    continue
                kp = fc.keyframe_points.insert(frame, value, options={'FAST'})
                kp.interpolation = interp
                total += 1
            fc.update()
            channels += 1

        if total == 0:
            self.report({'WARNING'}, "Nothing to add: the range is at or before the last selected keyframe")
            return {'CANCELLED'}

        for area in context.screen.areas:
            area.tag_redraw()
        self.report({'INFO'}, f"Added {total} keyframes on {channels} channel(s)")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class ANIMDUP_PT_main:
    bl_region_type = 'UI'
    bl_category = "Animation Duplicator"
    bl_label = "Animation Duplicator"

    def draw(self, context):
        layout = self.layout
        s = context.scene.animdup

        box = layout.box()
        box.label(text="Analysis (2 selected keys)")
        pairs = selected_pairs(context)
        if not pairs:
            box.label(text="Select 2 keyframes in a channel", icon='INFO')
        else:
            for _idb, fc, _ka, _kb, delta in pairs[:8]:
                row = box.row()
                row.label(text=f"{fc.data_path}[{fc.array_index}]")
                row.label(text=f"{delta:.{PRECISION}f}")
            if len(pairs) > 8:
                box.label(text=f"... and {len(pairs) - 8} more channels")

        layout.prop(s, "mode", expand=True)
        col = layout.column(align=True)
        col.prop(s, "frame_start")
        col.prop(s, "frame_end")
        layout.operator("animdup.calculate_keyframes", icon='KEYFRAME_HLT')


class ANIMDUP_PT_graph(ANIMDUP_PT_main, bpy.types.Panel):
    bl_idname = "ANIMDUP_PT_graph"
    bl_space_type = 'GRAPH_EDITOR'


class ANIMDUP_PT_dopesheet(ANIMDUP_PT_main, bpy.types.Panel):
    bl_idname = "ANIMDUP_PT_dopesheet"
    bl_space_type = 'DOPESHEET_EDITOR'


classes = (
    AnimDupSettings,
    ANIMDUP_OT_calculate,
    ANIMDUP_PT_graph,
    ANIMDUP_PT_dopesheet,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.animdup = PointerProperty(type=AnimDupSettings)


def unregister():
    del bpy.types.Scene.animdup
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
