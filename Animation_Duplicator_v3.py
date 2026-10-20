bl_info = {
    "name": "Animation Duplicator",
    "author": "You",
    "version": (1, 1, 0),
    "blender": (5, 2, 0),
    "location": "Graph Editor / Dope Sheet > Sidebar (N) > Animation Duplicator",
    "description": "Fills a frame range from the difference between 2 selected keyframes, "
                   "or repeats the whole curve shape of any selection of keyframes.",
    "category": "Animation",
}

import bpy
from bpy.props import EnumProperty, IntProperty, PointerProperty

PRECISION = 10  # decimals used for the analysis
EPS = 1e-4
MAX_CYCLES = 100000


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


def selected_keys_per_channel(context, min_keys, exact=False):
    """Return [(id_data, fcurve, [selected keys sorted by frame])] for channels
    with at least min_keys selected keyframes (or exactly min_keys if exact)."""
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
            if (len(keys) == min_keys) if exact else (len(keys) >= min_keys):
                results.append((idb, fc, keys))
    return results


def selected_pairs(context):
    """Yield (id_data, fcurve, key_a, key_b, delta) for every channel that has
    exactly 2 selected keyframes."""
    results = []
    for idb, fc, keys in selected_keys_per_channel(context, 2, exact=True):
        delta = round(keys[1].co.y - keys[0].co.y, PRECISION)
        results.append((idb, fc, keys[0], keys[1], delta))
    return results


def selected_sets(context):
    """Channels with 2+ selected keys, with span analysis for the repeat feature.
    Returns [(id_data, fcurve, keys, period, net_change)]."""
    results = []
    for idb, fc, keys in selected_keys_per_channel(context, 2):
        period = keys[-1].co.x - keys[0].co.x
        net = round(keys[-1].co.y - keys[0].co.y, PRECISION)
        results.append((idb, fc, keys, period, net))
    return results


def capture_pattern(keys):
    """Snapshot the selected keys relative to the first one, so the data stays
    valid after new keyframes are inserted."""
    fx, fy = keys[0].co.x, keys[0].co.y
    pattern = []
    for k in keys:
        pattern.append({
            "dx": k.co.x - fx,
            "dy": k.co.y - fy,
            "interp": k.interpolation,
            "easing": k.easing,
            "ktype": k.type,
            "hl_type": k.handle_left_type,
            "hr_type": k.handle_right_type,
            "hl": (k.handle_left.x - k.co.x, k.handle_left.y - k.co.y),
            "hr": (k.handle_right.x - k.co.x, k.handle_right.y - k.co.y),
        })
    return pattern


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
    repeat_mode: EnumProperty(
        name="Each Repeat",
        description="How values change from one repetition of the selected curve to the next",
        items=[
            ('EXACT', "Same Values",
             "Repeat the curve with identical values (a loop)"),
            ('ADD', "Add Change",
             "Add the curve's net change (last key - first key) on every repetition"),
            ('SUBTRACT', "Subtract Change",
             "Subtract the curve's net change on every repetition"),
        ],
        default='EXACT',
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
# Operators
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


class ANIMDUP_OT_repeat(bpy.types.Operator):
    bl_idname = "animdup.repeat_selection"
    bl_label = "Repeat Selected Curve"
    bl_description = ("Analyze all selected keyframes in each channel and reproduce that "
                      "curve shape (values, interpolation, handles) over the frame range")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects or context.active_object)

    def execute(self, context):
        s = context.scene.animdup
        sets = selected_sets(context)
        if not sets:
            self.report({'WARNING'}, "Select 2 or more keyframes in a channel first")
            return {'CANCELLED'}

        if s.frame_end < s.frame_start:
            self.report({'WARNING'}, "End Frame must be greater than or equal to Start Frame")
            return {'CANCELLED'}

        sign = {'EXACT': 0.0, 'ADD': 1.0, 'SUBTRACT': -1.0}[s.repeat_mode]
        channels = 0
        total = 0
        skipped = 0

        for _idb, fc, keys, period, net in sets:
            if period < EPS:
                skipped += 1
                continue

            # Snapshot everything first: inserting keys can invalidate references.
            fx, fy = keys[0].co.x, keys[0].co.y
            last_x = keys[-1].co.x
            pattern = capture_pattern(keys)
            # The first key of each cycle coincides with the end of the previous
            # one, so it is never rewritten.
            body = [p for p in pattern if p["dx"] > EPS]
            if not body:
                skipped += 1
                continue
            first_dx = body[0]["dx"]

            wrote = False
            for n in range(1, MAX_CYCLES + 1):
                shift = n * period
                if fx + shift + first_dx > s.frame_end:
                    break
                y_off = sign * net * n
                for p in body:
                    x = fx + shift + p["dx"]
                    if x > s.frame_end:
                        break
                    if x < s.frame_start or x <= last_x + EPS:
                        continue
                    y = fy + y_off + p["dy"]
                    kp = fc.keyframe_points.insert(x, y, options={'FAST'})
                    kp.interpolation = p["interp"]
                    kp.easing = p["easing"]
                    kp.type = p["ktype"]
                    kp.handle_left_type = p["hl_type"]
                    kp.handle_right_type = p["hr_type"]
                    kp.handle_left = (x + p["hl"][0], y + p["hl"][1])
                    kp.handle_right = (x + p["hr"][0], y + p["hr"][1])
                    total += 1
                    wrote = True
            fc.update()
            if wrote:
                channels += 1

        if total == 0:
            msg = "Nothing to add: the range is at or before the last selected keyframe"
            if skipped:
                msg += " (selected keys must span more than 0 frames)"
            self.report({'WARNING'}, msg)
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

        # --- Frame range (shared by both tools) ---
        col = layout.column(align=True)
        col.prop(s, "frame_start")
        col.prop(s, "frame_end")

        # --- Tool 1: difference between 2 keys ---
        box = layout.box()
        box.label(text="Difference (exactly 2 keys)")
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
        box.prop(s, "mode", expand=True)
        box.operator("animdup.calculate_keyframes", icon='KEYFRAME_HLT')

        # --- Tool 2: repeat the whole selected curve ---
        box = layout.box()
        box.label(text="Repeat Curve (2+ keys)")
        sets = selected_sets(context)
        if not sets:
            box.label(text="Select 2+ keyframes in a channel", icon='INFO')
        else:
            for _idb, fc, keys, period, net in sets[:8]:
                row = box.row()
                row.label(text=f"{fc.data_path}[{fc.array_index}]")
                row.label(text=f"{len(keys)} keys / {period:g}f / {net:+.{4}f}")
            if len(sets) > 8:
                box.label(text=f"... and {len(sets) - 8} more channels")
        box.prop(s, "repeat_mode", text="")
        box.operator("animdup.repeat_selection", icon='FILE_REFRESH')


class ANIMDUP_PT_graph(ANIMDUP_PT_main, bpy.types.Panel):
    bl_idname = "ANIMDUP_PT_graph"
    bl_space_type = 'GRAPH_EDITOR'


class ANIMDUP_PT_dopesheet(ANIMDUP_PT_main, bpy.types.Panel):
    bl_idname = "ANIMDUP_PT_dopesheet"
    bl_space_type = 'DOPESHEET_EDITOR'


classes = (
    AnimDupSettings,
    ANIMDUP_OT_calculate,
    ANIMDUP_OT_repeat,
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
