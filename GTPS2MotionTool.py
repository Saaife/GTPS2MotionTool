# OLD VERSION OF THE TOOL/ADDON
bl_info = {
    "name": "Gran Turismo 4 MotionTool",
    "author": "Saif/Claude",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "File > Import/Export > Gran Turismo 4 Motion (.mot)",
    "description": "Imports and Exports GT4 character Motion",
    "category": "Import-Export",
}

import struct
import math
import os

class Bone:
    __slots__ = ("name", "parent", "trans_idx", "rot_idx")
    def __init__(self, name, parent, trans_idx, rot_idx):
        self.name = name
        self.parent = parent
        self.trans_idx = trans_idx
        self.rot_idx = rot_idx

CHUNK = b"NODE_MOTION\x00\x00\x00\x00\x00"

def _gen_tokens(bones):
    n = len(bones)
    children = [[] for _ in bones]
    for i, b in enumerate(bones):
        if b.parent >= 0:
            children[b.parent].append(i)
    tokens = [[] for _ in bones]
    tokens[0] = [0x02]
    branch, prev = [], 0
    for i in range(1, n):
        p = bones[i].parent
        if p == prev:
            if len(children[p]) > 1:
                tokens[i] = [0x02]; branch.append(p)
            else:
                tokens[i] = []
        else:
            pops = 0
            while branch and branch[-1] != p:
                branch.pop(); pops += 1
            tokens[i] = [0x03] * (pops + 1) + [0x02]
        prev = i
    c02 = sum(t.count(0x02) for t in tokens)
    c03 = sum(t.count(0x03) for t in tokens)
    return tokens, [0x03] * (c02 - c03)

def _name_table(bones):
    tokens, trailing = _gen_tokens(bones)
    out = bytearray()
    for i, b in enumerate(bones):
        out += bytes(tokens[i])
        out += bytes([0x05, 0x07]) + struct.pack("<HHH", *b.trans_idx)
        out += bytes([0x13, 0x07]) + struct.pack("<HHH", *b.rot_idx)
        out += bytes([0x04]) + b.name.encode("ascii", "replace") + b"\x00"
    out += bytes(trailing)
    return bytes(out)

def build_mot(bones, channels, dur=None):
    out = bytearray()
    out += b"Mot1" + b"\x00" * 12
    out += struct.pack("<I", 0)
    out += struct.pack("<HH", 1, 1)
    out += struct.pack("<II", 0, 0)
    out += CHUNK
    block_offsets = []
    for keys in channels:
        block_offsets.append(len(out))
        N = len(keys)
        size = 16 * (N + 1)
        out += struct.pack("<III", size // 4 - 2, 0, 0)
        for (t, v0, v1, v2) in keys:
            out += struct.pack("<ffff", v0, v1, v2, t)
        out += b"\x00" * 4
    ptr_off = len(out)
    for off in block_offsets:
        out += struct.pack("<IIII", 1, 0, 0, off)
    idx_off = len(out)
    for k in range(len(block_offsets)):
        out += struct.pack("<I", ptr_off + 16 * k)
    dur_off = len(out)
    if dur is None:
        maxt = max((k[-1][0] for k in channels if k), default=0.0)
        dur = (maxt + 1.0 / 60.0, maxt)
    out += struct.pack("<ff", *dur) + b"\x00" * 8
    out += struct.pack("<IIII", 0x20, len(channels), idx_off, 0x30)
    name_off = len(out)
    out += _name_table(bones)
    out += struct.pack("<IIII", 1, 0, 0, 0)
    footer_off = len(out)
    out += struct.pack("<IIII", 1, dur_off, footer_off, name_off)
    out += struct.pack("<IIII", 0x20, 0, 0xFFFF, 0)
    out += b"\x00" * 0x20
    struct.pack_into("<I", out, 0x10, footer_off)
    struct.pack_into("<I", out, 0x18, footer_off + 0x20)
    struct.pack_into("<I", out, 0x1C, footer_off + 0x10)
    return bytes(out)

def _armature_to_motion(obj, opts):
    import bpy

    rscale = (math.pi / 180.0) if opts["rot_degrees"] else 1.0
    pscale = opts["translation_scale"]
    fps    = opts["fps_source"]
    order  = opts["euler_order"]
    arm    = obj.data

    # DFS pre-order -> sequential channel ids + clean tree encoding
    order_names, parent_idx = [], {}
    def rec(bone, pidx):
        idx = len(order_names)
        order_names.append(bone.name); parent_idx[bone.name] = pidx
        for c in bone.children:
            rec(c, idx)
    for r in [b for b in arm.bones if b.parent is None]:
        rec(r, -1)

    bones = [Bone(nm, parent_idx[nm],
                   [6 * k, 6 * k + 1, 6 * k + 2],
                   [6 * k + 3, 6 * k + 4, 6 * k + 5])
             for k, nm in enumerate(order_names)]

    scene = bpy.context.scene
    f0, f1 = int(scene.frame_start), int(scene.frame_end)
    if f1 < f0:
        f1 = f0
    frames = list(range(f0, f1 + 1))

    acc = {nm: {"loc": ([], [], []), "eu": ([], [], []), "prev": None} for nm in order_names}
    saved = scene.frame_current
    try:
        for f in frames:
            scene.frame_set(f)                       # physically advance the scene
            t = f / fps
            for nm in order_names:
                pb = obj.pose.bones[nm]
                # native local transform = parent_pose^-1 @ pose  (frame-independent)
                L = (pb.parent.matrix.inverted() @ pb.matrix) if pb.parent else pb.matrix.copy()
                tr = L.to_translation()
                prev = acc[nm]["prev"]
                eu = L.to_euler(order, prev) if prev else L.to_euler(order)
                acc[nm]["prev"] = eu
                for a in range(3):
                    acc[nm]["loc"][a].append((t, tr[a] / pscale))
                    acc[nm]["eu"][a].append((t, eu[a] / rscale))
    finally:
        scene.frame_set(saved)

    def finish(seq):
        if not seq:
            return [(0.0, 0.0, 0.0, 0.0)]
        return [(t, v, 0.0, 0.0) for (t, v) in seq]

    channels = [None] * (6 * len(bones))
    for k, nm in enumerate(order_names):
        for a in range(3):
            channels[6 * k + a]     = finish(acc[nm]["loc"][a])
            channels[6 * k + 3 + a] = finish(acc[nm]["eu"][a])
    return bones, channels


class MotFile:
    def __init__(self):
        self.bones = []
        self.channels = []
        self.duration = 0.0

def _u16(d, o): return struct.unpack_from("<H", d, o)[0]
def _u32(d, o): return struct.unpack_from("<I", d, o)[0]

def _is_ptr_record(d, o):
    return (0 <= o and o + 16 <= len(d)
            and _u32(d, o) == 1 and _u32(d, o + 4) == 0 and _u32(d, o + 8) == 0
            and 0x20 <= _u32(d, o + 12) < len(d))

def _find_ptr_table(d):
    seed = None
    for o in range(0x20, len(d) - 16, 4):
        if _is_ptr_record(d, o) and _is_ptr_record(d, o + 16):
            seed = o
            break
    if seed is None:
        raise ValueError("block pointer table not found")
    while _is_ptr_record(d, seed - 16):
        seed -= 16
    offs = []
    o = seed
    while _is_ptr_record(d, o):
        offs.append(_u32(d, o + 12))
        o += 16
    return offs, seed

def _parse_nodes(d, name_off):
    o = name_off
    while o < len(d) and not (d[o] == 0x05 and d[o + 1] == 0x07):
        o += 1
    raw = []
    tok = []
    while o < len(d) - 4:
        b = d[o]
        if b in (0x02, 0x03):
            tok.append(b); o += 1; continue
        if b == 0x05 and d[o + 1] == 0x07:
            tr = [_u16(d, o + 2 + i * 2) for i in range(3)]; o += 8
            if d[o] == 0x13 and d[o + 1] == 0x07:
                rot = [_u16(d, o + 2 + i * 2) for i in range(3)]; o += 8
            else:
                rot = [0, 0, 0]
            if d[o] == 0x04:
                o += 1
                e = d.index(b"\x00", o)
                name = d[o:e].decode("ascii", "replace"); o = e + 1
                raw.append((name, tr, rot, tok)); tok = []
            else:
                break
        else:
            break
    bones = []
    branch = []
    prev = -1
    for i, (name, tr, rot, tk) in enumerate(raw):
        n03 = tk.count(3); n02 = tk.count(2)
        if i == 0:
            parent = -1
        elif n03 == 0 and n02 == 0:
            parent = prev
        elif n03 == 0 and n02 == 1:
            parent = prev; branch.append(prev)
        else:
            for _ in range(n03 - 1):
                if branch: branch.pop()
            parent = branch[-1] if branch else -1
        bones.append(Bone(name, parent, tr, rot))
        prev = i
    return bones

def _parse_block(d, start, end):
    nmax = (end - start - 12) // 16
    keys = []
    prev_t = None
    base = start + 12
    for k in range(nmax):
        o = base + k * 16
        if o + 16 > end:
            break
        v0, v1, v2, t = struct.unpack_from("<ffff", d, o)
        if not (math.isfinite(v0) and math.isfinite(v1)
                and math.isfinite(v2) and math.isfinite(t)):
            break
        if prev_t is not None and not (t > prev_t + 1e-9):
            break
        keys.append((t, v0, v1, v2))
        prev_t = t
    return keys

def parse_mot(data):
    if data[:4] != b"Mot1":
        raise ValueError("not a Mot1 file (bad magic)")
    mot = MotFile()
    try:
        footer = _u32(data, 0x10)
        name_off = _u32(data, footer + 12)
        if not (0 < name_off < len(data)):
            raise ValueError
    except Exception:
        name_off = data.find(b"\x05\x07")
    mot.bones = _parse_nodes(data, name_off)
    offs, cap = _find_ptr_table(data)
    bounds = offs + [cap]
    mot.channels = [_parse_block(data, bo, bounds[i + 1]) for i, bo in enumerate(offs)]
    mot.duration = max((ch[-1][0] for ch in mot.channels if ch), default=0.0)
    return mot

def eval_channel(keys, t):
    if not keys:
        return 0.0
    if t <= keys[0][0]:
        return keys[0][1]
    if t >= keys[-1][0]:
        return keys[-1][1]
    lo, hi = 0, len(keys) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if keys[mid][0] <= t: lo = mid
        else: hi = mid
    t0, v0 = keys[lo][0], keys[lo][1]
    t1, v1 = keys[hi][0], keys[hi][1]
    f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
    return v0 + (v1 - v0) * f

def _import_into_blender(mot, name, opts):
    import bpy
    from mathutils import Vector, Matrix, Euler

    # --- FIX: Sanitize bone names to prevent empty/duplicate crashing ---
    seen_names = {}
    for b in mot.bones:
        bname = b.name.strip() if b.name else "Bone"
        if bname not in seen_names:
            seen_names[bname] = 1
            b.name = bname
        else:
            b.name = f"{bname}.{seen_names[bname]:03d}"
            seen_names[bname] += 1
    # --------------------------------------------------------------------

    rscale = (math.pi / 180.0) if opts["rot_degrees"] else 1.0
    pscale = opts["translation_scale"]
    fps    = opts["fps_source"]
    order  = opts["euler_order"]
    interp = opts["interpolation"]
    bones  = mot.bones

    def chan(idx):
        return mot.channels[idx] if 0 <= idx < len(mot.channels) else []

    def local_at(b, t):
        tr = Vector((eval_channel(chan(b.trans_idx[0]), t),
                     eval_channel(chan(b.trans_idx[1]), t),
                     eval_channel(chan(b.trans_idx[2]), t))) * pscale
        eu = Euler((eval_channel(chan(b.rot_idx[0]), t) * rscale,
                    eval_channel(chan(b.rot_idx[1]), t) * rscale,
                    eval_channel(chan(b.rot_idx[2]), t) * rscale), order)
        return Matrix.Translation(tr) @ eu.to_matrix().to_4x4()

    children = [[] for _ in bones]
    for i, b in enumerate(bones):
        if b.parent >= 0:
            children[b.parent].append(i)

    t0 = 0.0
    Lrest = [local_at(b, t0) for b in bones]
    world_rest = [None] * len(bones)
    for i, b in enumerate(bones):
        world_rest[i] = Lrest[i] if b.parent < 0 else world_rest[b.parent] @ Lrest[i]
    pos = [w.to_translation() for w in world_rest]

    def disp_len(i):
        if children[i]:
            return max(1e-3, (pos[children[i][0]] - pos[i]).length)
        if bones[i].parent >= 0:
            return max(1e-3, (pos[i] - pos[bones[i].parent]).length * 0.5)
        return 0.1

    ctx = bpy.context
    if ctx.view_layer.objects.active and ctx.object and ctx.object.mode != "OBJECT":
        try: bpy.ops.object.mode_set(mode="OBJECT")
        except Exception: pass
    try: bpy.ops.object.select_all(action="DESELECT")
    except Exception: pass

    arm_data = bpy.data.armatures.new(name)
    arm_obj = bpy.data.objects.new(name, arm_data)
    (ctx.collection or ctx.scene.collection).objects.link(arm_obj)
    arm_obj.select_set(True)
    ctx.view_layer.objects.active = arm_obj
    ctx.view_layer.update()

    try:
        bpy.ops.object.mode_set(mode="EDIT")
        ebs = []
        for i, b in enumerate(bones):
            eb = arm_data.edit_bones.new(b.name)
            eb.head = Vector((0, 0, 0))
            eb.tail = Vector((0, disp_len(i), 0))
            eb.matrix = world_rest[i]
            ebs.append(eb)
        for i, b in enumerate(bones):
            if b.parent >= 0:
                ebs[i].parent = ebs[b.parent]
    finally:
        try: bpy.ops.object.mode_set(mode="OBJECT")
        except Exception: pass

    if len(arm_obj.pose.bones) != len(bones):
        raise RuntimeError("armature build incomplete (%d/%d bones)"
                           % (len(arm_obj.pose.bones), len(bones)))

    ml = [arm_data.bones[b.name].matrix_local.copy() for b in bones]
    rest_local_inv = []
    for i, b in enumerate(bones):
        rl = ml[i] if b.parent < 0 else ml[b.parent].inverted() @ ml[i]
        rest_local_inv.append(rl.inverted())

    for pb in arm_obj.pose.bones:
        pb.rotation_mode = "QUATERNION"

    if opts["limb_shapes"]:
        try:
            shp = bpy.data.objects.get("GT4_LimbShape")
            if shp is None:
                me = bpy.data.meshes.new("GT4_LimbShape")
                v = [(0, 0, 0), (0.07, 0.1, 0.07), (-0.07, 0.1, 0.07),
                     (-0.07, 0.1, -0.07), (0.07, 0.1, -0.07), (0, 1, 0)]
                f = [(0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 1),
                     (5, 2, 1), (5, 3, 2), (5, 4, 3), (5, 1, 4)]
                me.from_pydata(v, [], f); me.update()
                shp = bpy.data.objects.new("GT4_LimbShape", me)
                shp.use_fake_user = True
            pbs = {pb.name: pb for pb in arm_obj.pose.bones}
            up = Vector((0, 1, 0))
            for i, b in enumerate(bones):
                pb = pbs[b.name]
                if children[i]:
                    dloc = (ml[i].inverted() @ ml[children[i][0]]).to_translation()
                elif b.parent >= 0:
                    dloc = ml[i].inverted() @ (pos[i] + (pos[i] - pos[b.parent]) * 0.5)
                else:
                    dloc = Vector((0, disp_len(i), 0))
                if dloc.length < 1e-6:
                    continue
                pb.custom_shape = shp
                pb.custom_shape_rotation_euler = up.rotation_difference(dloc.normalized()).to_euler()
        except Exception:
            pass

    action = bpy.data.actions.new(name + "_Action")
    ad = arm_obj.animation_data_create()
    ad.action = action
    try:
        if hasattr(action, "slots"):
            slot = action.slots.new(id_type="OBJECT", name=name) if len(action.slots) == 0 \
                   else action.slots[0]
            ad.action_slot = slot
    except Exception:
        pass

    def make_fcurves(path, count, group, frames, comps):
        for a in range(count):
            fc = action.fcurves.new(path, index=a, action_group=group)
            fc.keyframe_points.add(len(frames))
            for j, kp in enumerate(fc.keyframe_points):
                kp.co = (frames[j], comps[j][a])
                kp.interpolation = interp
                kp.handle_left_type = kp.handle_right_type = "AUTO_CLAMPED"
            fc.update()

    EPS = 1e-6
    for i, b in enumerate(bones):
        tset = set()
        for ci in (b.trans_idx + b.rot_idx):
            for k in chan(ci):
                tset.add(k[0])
        times = sorted(tset) if tset else [0.0]
        frames = [t * fps for t in times]

        locs, quats = [], []
        prev_q = None
        for t in times:
            basis = rest_local_inv[i] @ local_at(b, t)
            q = basis.to_quaternion()
            if prev_q is not None and q.dot(prev_q) < 0.0:
                q.negate()
            prev_q = q
            locs.append(basis.to_translation())
            quats.append(q)

        loc_anim = any(l.length > 1e-5 for l in locs)
        quat_anim = any(abs(1.0 - q.w) > EPS or q.x * q.x + q.y * q.y + q.z * q.z > EPS * EPS
                        for q in quats)
        if loc_anim:
            make_fcurves('pose.bones["%s"].location' % b.name, 3, b.name, frames,
                         [(l.x, l.y, l.z) for l in locs])
        if quat_anim:
            make_fcurves('pose.bones["%s"].rotation_quaternion' % b.name, 4, b.name, frames,
                         [(q.w, q.x, q.y, q.z) for q in quats])

    scene = bpy.context.scene
    scene.render.fps = max(1, int(round(fps)))
    scene.frame_start = 0
    scene.frame_end = max(1, int(math.ceil(mot.duration * fps)))
    if opts["convert_yup"]:
        arm_obj.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
    scene.frame_set(0)
    return arm_obj

try:
    import bpy
    from bpy.props import StringProperty, BoolProperty, FloatProperty, EnumProperty
    from bpy_extras.io_utils import ExportHelper, ImportHelper
    from bpy.types import Operator

    class EXPORT_OT_gt4_mot(Operator, ExportHelper):
        bl_idname = "export_scene.gt4_mot"
        bl_label = "Export GT4 Motion"
        bl_options = {"REGISTER"}
        filename_ext = ".mot"
        filter_glob: StringProperty(default="*.mot", options={"HIDDEN"})

        translation_scale: FloatProperty(name="Translation scale", default=1.0, min=1e-6, max=1000.0)
        fps_source: FloatProperty(name="Source FPS (frame->time)", default=60.0, min=1.0, max=240.0)
        rot_degrees: BoolProperty(name="Rotations in degrees", default=False)
        euler_order: EnumProperty(
            name="Euler order",
            items=[(o, o, "") for o in ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")],
            default="XYZ")

        @classmethod
        def poll(cls, context):
            return context.active_object is not None and context.active_object.type == "ARMATURE"

        def execute(self, context):
            obj = context.active_object
            if obj is None or obj.type != "ARMATURE":
                self.report({"ERROR"}, "Select an armature first")
                return {"CANCELLED"}
            opts = {k: getattr(self, k) for k in
                    ("translation_scale", "fps_source", "rot_degrees", "euler_order")}
            try:
                bones, channels = _armature_to_motion(obj, opts)
                data = build_mot(bones, channels)
                with open(self.filepath, "wb") as fh:
                    fh.write(data)
            except Exception as e:
                self.report({"ERROR"}, "Export failed: %s" % e)
                return {"CANCELLED"}
            self.report({"INFO"}, "Wrote %d bones, %d bytes -> %s"
                        % (len(bones), len(data), self.filepath))
            return {"FINISHED"}

    class IMPORT_OT_gt4_mot(Operator, ImportHelper):
        bl_idname = "import_scene.gt4_mot"
        bl_label = "Import GT4 Motion"
        bl_options = {"REGISTER", "UNDO"}
        filename_ext = ".mot"
        filter_glob: StringProperty(default="*.mot", options={"HIDDEN"})

        translation_scale: FloatProperty(
            name="Translation scale", default=1.0, min=1e-6, max=1000.0,
            description="Rig is ~1.6 units tall at 1.0 (already ~meters)")
        fps_source: FloatProperty(
            name="Source FPS (time->frame)", default=60.0, min=1.0, max=240.0,
            description="Timestamps are seconds, authored at 60 fps; also sets scene fps")
        rot_degrees: BoolProperty(name="Rotations in degrees", default=False)
        euler_order: EnumProperty(
            name="Euler order",
            items=[(o, o, "") for o in ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")],
            default="XYZ")
        interpolation: EnumProperty(
            name="Interpolation",
            items=[("BEZIER", "Bezier (smooth)", ""), ("LINEAR", "Linear", "")],
            default="BEZIER")
        limb_shapes: BoolProperty(
            name="Limb-shaped bones",
            description="Display each bone with a custom shape aimed at its child "
                        "so the rig looks like a normal skeleton. Purely visual - "
                        "the animation stays rotation-only and never explodes",
            default=True)
        convert_yup: BoolProperty(name="Convert Y-up to Z-up", default=True)

        def execute(self, context):
            try:
                data = open(self.filepath, "rb").read()
                mot = parse_mot(data)
            except Exception as e:
                self.report({"ERROR"}, "Parse failed: %s" % e)
                return {"CANCELLED"}
            opts = {k: getattr(self, k) for k in (
                "translation_scale", "fps_source", "rot_degrees", "euler_order",
                "interpolation", "limb_shapes", "convert_yup")}
            name = os.path.splitext(os.path.basename(self.filepath))[0]
            try:
                _import_into_blender(mot, name, opts)
            except Exception as e:
                self.report({"ERROR"}, "Import failed: %s" % e)
                return {"CANCELLED"}
            self.report({"INFO"}, "Imported %d bones, %.2fs (%d frames)" %
                        (len(mot.bones), mot.duration, int(mot.duration * self.fps_source)))
            return {"FINISHED"}

    def menu_func_export(self, context):
        self.layout.operator(EXPORT_OT_gt4_mot.bl_idname, text="Gran Turismo 4 Motion (.mot)")

    def menu_func_import(self, context):
        self.layout.operator(IMPORT_OT_gt4_mot.bl_idname, text="Gran Turismo 4 Motion (.mot)")

    classes = (
        EXPORT_OT_gt4_mot,
        IMPORT_OT_gt4_mot
    )

    def register():
        for cls in classes:
            bpy.utils.register_class(cls)
        bpy.types.TOPBAR_MT_file_export.append(menu_func_export)
        bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

    def unregister():
        bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
        for cls in reversed(classes):
            bpy.utils.unregister_class(cls)

except ImportError:
    def register(): pass
    def unregister(): pass


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2:
        # CLI Parser behavior if run standalone
        mot = parse_mot(open(sys.argv[1], "rb").read())
        print("bones: %d   duration: %.3fs (%.0f frames @60)   channels: %d"
              % (len(mot.bones), mot.duration, mot.duration * 60, len(mot.channels)))
    else:
        print("Install as a Blender add-on, then File > Import/Export > GT4 Motion (.mot)")
