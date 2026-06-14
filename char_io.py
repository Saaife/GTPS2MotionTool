# bl_info kept for reference only; the package __init__.py owns the real one.
_bl_info = {
    "name": "Gran Turismo 4 MotionTool",
    "author": "Saif/Claude",
    "version": (2, 8, 0),
    "blender": (4, 5, 0),
    "location": "File > Import/Export > Gran Turismo 4 Motion (.mot)",
    "description": "Imports/exports GT4/TT character, single-node prop, generic/light MOT files",
    "category": "Import-Export",
}

import struct
import math
import os
import base64
import hashlib

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
            scene.frame_set(f)
            t = f / fps
            for nm in order_names:
                pb = obj.pose.bones[nm]
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


def _collect_pose_key_frames(action, f0, f1):
    """Map bone name -> set of keyframe times (in frames) found on the
    action's pose-bone F-curves, clamped to [f0, f1]."""
    per = {}
    fcurves = _legacy_action_fcurves(action)
    if not fcurves:
        return per
    pre = 'pose.bones["'
    for fc in fcurves:
        dp = str(getattr(fc, "data_path", "") or "")
        if not dp.startswith(pre):
            continue
        end = dp.find('"]', len(pre))
        if end < 0:
            continue
        nm = dp[len(pre):end]
        s = per.setdefault(nm, set())
        for kp in fc.keyframe_points:
            f = float(kp.co[0])
            if f0 - 1e-6 <= f <= f1 + 1e-6:
                s.add(f)
    return per

def _armature_to_motion_sparse(obj, opts):
    """Key-based variant of _armature_to_motion: emits MOT keys only at the
    action's keyframe times (clamped to the scene frame range) instead of
    sampling every frame, so edited re-exports stay close to the original
    file size and trimming the frame range shrinks the file. Returns
    (None, None) when the armature has no pose-bone keyframes at all, in
    which case the caller should fall back to dense per-frame sampling."""
    import bpy

    rscale = (math.pi / 180.0) if opts["rot_degrees"] else 1.0
    pscale = opts["translation_scale"]
    fps    = opts["fps_source"]
    order  = opts["euler_order"]
    arm    = obj.data

    order_names, parent_idx = [], {}
    def rec(bone, pidx):
        idx = len(order_names)
        order_names.append(bone.name); parent_idx[bone.name] = pidx
        for c in bone.children:
            rec(c, idx)
    for r in [b for b in arm.bones if b.parent is None]:
        rec(r, -1)

    scene = bpy.context.scene
    f0, f1 = int(scene.frame_start), int(scene.frame_end)
    if f1 < f0:
        f1 = f0
    action = obj.animation_data.action if obj.animation_data else None
    per = _collect_pose_key_frames(action, float(f0), float(f1))
    if not any(per.get(nm) for nm in order_names):
        return None, None

    bone_frames, bone_fsets, needed = {}, {}, set()
    for nm in order_names:
        s = set(per.get(nm) or ())
        if s:
            s.add(float(f1))  # capture the pose at the cut/end point
        else:
            s = {float(f0)}
        bone_frames[nm] = sorted(s)
        bone_fsets[nm] = s
        needed |= s

    samples = {nm: {} for nm in order_names}
    saved = scene.frame_current
    try:
        for f in sorted(needed):
            fr = int(math.floor(f))
            scene.frame_set(fr, subframe=max(0.0, min(0.999999, f - fr)))
            for nm in order_names:
                if f not in bone_fsets[nm]:
                    continue
                pb = obj.pose.bones[nm]
                L = (pb.parent.matrix.inverted() @ pb.matrix) if pb.parent else pb.matrix.copy()
                samples[nm][f] = L
    finally:
        scene.frame_set(saved)

    bones = [Bone(nm, parent_idx[nm],
                  [6 * k, 6 * k + 1, 6 * k + 2],
                  [6 * k + 3, 6 * k + 4, 6 * k + 5])
             for k, nm in enumerate(order_names)]
    channels = [None] * (6 * len(bones))
    for k, nm in enumerate(order_names):
        loc = ([], [], [])
        eu3 = ([], [], [])
        prev = None
        for f in bone_frames[nm]:
            L = samples[nm][f]
            t = (f - f0) / fps
            tr = L.to_translation()
            eu = L.to_euler(order, prev) if prev else L.to_euler(order)
            prev = eu
            for a in range(3):
                loc[a].append((t, tr[a] / pscale))
                eu3[a].append((t, eu[a] / rscale))
        for a in range(3):
            channels[6 * k + a]     = [(t, v, 0.0, 0.0) for (t, v) in loc[a]]
            channels[6 * k + 3 + a] = [(t, v, 0.0, 0.0) for (t, v) in eu3[a]]
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
    if name_off < 0:
        raise ValueError("node name table not found")
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

def _legacy_action_fcurves(action):
    try:
        return action.fcurves if action is not None and hasattr(action, "fcurves") else None
    except Exception:
        return None

def _set_owner_component(owner, data_path, index, value):
    if data_path.startswith('["') and data_path.endswith('"]'):
        prop = data_path[2:-2]
        owner[prop] = value
        return
    cur = getattr(owner, data_path)
    try:
        cur[index] = value
    except TypeError:
        cur = list(cur)
        cur[index] = value
        setattr(owner, data_path, cur)

def _insert_component_keys(action, owner, data_path, index, frames, values, group=None, interpolation="BEZIER"):
    fcurves = _legacy_action_fcurves(action)
    if fcurves is not None:
        try:
            fc = fcurves.find(data_path, index=index) if index is not None else fcurves.find(data_path)
        except TypeError:
            fc = fcurves.find(data_path)
        if fc is None:
            if index is None:
                fc = fcurves.new(data_path, action_group=group)
            else:
                fc = fcurves.new(data_path, index=index, action_group=group)
        try:
            fc.keyframe_points.clear()
        except Exception:
            try:
                while len(fc.keyframe_points):
                    fc.keyframe_points.remove(fc.keyframe_points[-1])
            except Exception:
                pass
        fc.keyframe_points.add(len(frames))
        for j, kp in enumerate(fc.keyframe_points):
            kp.co = (frames[j], values[j])
            kp.interpolation = interpolation
            try:
                kp.handle_left_type = kp.handle_right_type = "AUTO_CLAMPED"
            except Exception:
                pass
        fc.update()
        return fc

    for frame, value in zip(frames, values):
        _set_owner_component(owner, data_path, index, value)
        try:
            if index is None or data_path.startswith('["'):
                owner.keyframe_insert(data_path=data_path, frame=frame, group=group)
            else:
                owner.keyframe_insert(data_path=data_path, index=index, frame=frame, group=group)
        except TypeError:
            if index is None or data_path.startswith('["'):
                owner.keyframe_insert(data_path=data_path, frame=frame)
            else:
                owner.keyframe_insert(data_path=data_path, index=index, frame=frame)
    return None

def _find_fcurve_compat(action, data_path, index=None):
    fcurves = _legacy_action_fcurves(action)
    if fcurves is None:
        return None
    try:
        return fcurves.find(data_path, index=index) if index is not None else fcurves.find(data_path)
    except TypeError:
        return fcurves.find(data_path)

def _animation_fingerprint(obj):
    """Stable hash of the object's keyframe data (paths, indices and key
    frame/value pairs). This captures exactly what the exporters sample;
    handle or interpolation tweaks that don't move key values are
    deliberately ignored because they don't change the exported samples."""
    h = hashlib.sha256()
    try:
        ad = getattr(obj, "animation_data", None)
        action = ad.action if ad else None
        fcurves = _legacy_action_fcurves(action)
        if not fcurves:
            h.update(b"no-animation")
            return h.hexdigest()
        items = []
        for fc in fcurves:
            kps = [(float(kp.co[0]), float(kp.co[1])) for kp in fc.keyframe_points]
            items.append((str(getattr(fc, "data_path", "") or ""),
                          int(getattr(fc, "array_index", 0) or 0), kps))
        items.sort(key=lambda it: (it[0], it[1]))
        for dp, idx, kps in items:
            h.update(dp.encode("utf-8", "replace"))
            h.update(struct.pack("<iI", idx, len(kps)))
            for f, v in kps:
                h.update(struct.pack("<ff", f, v))
    except Exception:
        h.update(b"fingerprint-error")
    return h.hexdigest()

def _mot_animation_edited(obj, scene=None):
    """True when the object's animation (or the scene frame range, which is
    how trims are expressed) differs from what the importer stamped."""
    fp = obj.get("gtps2_mot_anim_fingerprint")
    if not fp:
        return False  # imported by an older version; keep exact-bytes behavior
    if str(fp) != _animation_fingerprint(obj):
        return True
    if scene is not None:
        try:
            fs = obj.get("gtps2_mot_import_frame_start")
            fe = obj.get("gtps2_mot_import_frame_end")
            if fs is not None and int(scene.frame_start) != int(fs):
                return True
            if fe is not None and int(scene.frame_end) != int(fe):
                return True
        except Exception:
            pass
    return False

def _import_into_blender(mot, name, opts):
    import bpy
    from mathutils import Vector, Matrix, Euler

    seen_names = {}
    for b in mot.bones:
        bname = b.name.strip() if b.name else "Bone"
        if bname not in seen_names:
            seen_names[bname] = 1
            b.name = bname
        else:
            b.name = f"{bname}.{seen_names[bname]:03d}"
            seen_names[bname] += 1

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
            vals = [comps[j][a] for j in range(len(frames))]
            _insert_component_keys(action, arm_obj, path, a, frames, vals, group, interp)

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
    scene.frame_end = max(1, int(math.ceil((getattr(mot, "duration_end", 0.0) or mot.duration) * fps)))
    if opts["convert_yup"]:
        arm_obj.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
    scene.frame_set(0)
    return arm_obj


def _mot_chunk_label(data):
    if len(data) < 0x30:
        return ""
    raw = data[0x20:0x30]
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace")

def _looks_like_mot(data):
    return len(data) >= 0x20 and data[:4] == b"Mot1"

def _block_key_count_from_header(data, start, end):
    if start + 12 > end:
        return 0
    h0, h1, h2 = struct.unpack_from("<III", data, start)
    if h1 != 0 or h2 != 0 or h0 < 2 or (h0 - 2) % 4 != 0:
        return 0
    n = (h0 - 2) // 4
    if start + 12 + 16 * n + 4 > end:
        return 0
    return n

def _parse_block_tail(d, start, end):
    n = _block_key_count_from_header(d, start, end)
    keys = []
    base = start + 12
    prev_t = None
    for k in range(n):
        o = base + k * 16
        v0, v1, v2, t = struct.unpack_from("<ffff", d, o)
        if not (math.isfinite(v0) and math.isfinite(v1) and math.isfinite(v2) and math.isfinite(t)):
            break
        if prev_t is not None and not (t > prev_t + 1e-9):
            break
        keys.append((t, v0, v1, v2))
        prev_t = t
    tail = 0.0
    tail_off = base + n * 16
    if tail_off + 4 <= end:
        try:
            tail = struct.unpack_from("<f", d, tail_off)[0]
        except Exception:
            tail = keys[-1][1] if keys else 0.0
    return keys, tail

def parse_mot_any(data):
    if not _looks_like_mot(data):
        raise ValueError("not a Mot1 file (bad magic)")
    label = _mot_chunk_label(data)
    mot = MotFile()
    mot.kind = "RAW"
    mot.label = label
    mot.channel_tails = []
    mot.duration_end = 0.0

    try:
        old = parse_mot(data)
        if old.bones:
            old.kind = "SINGLE_NODE" if label == "NODE_MOTION" and len(old.bones) == 1 else "CHARACTER"
        else:
            old.kind = "GENERIC_CHANNEL"
        old.label = label
        old.channel_tails = []
        old.duration_end = old.duration
        old.generic_names = []
        old.generic_metadata_strings = []
        if old.kind == "GENERIC_CHANNEL":
            old.generic_names, old.generic_metadata_strings = _generic_channel_names_from_table(data, len(old.channels), label)
        try:
            offs, cap = _find_ptr_table(data)
            bounds = offs + [cap]
            tails = []
            for i, bo in enumerate(offs):
                _keys, tail = _parse_block_tail(data, bo, bounds[i + 1])
                tails.append(tail)
            old.channel_tails = tails
        except Exception:
            pass
        return old
    except Exception:
        pass

    try:
        footer = _u32(data, 0x10)
        mot.duration = 0.0
        mot.duration_end = 0.0
        mot.footer_off = footer
    except Exception:
        mot.footer_off = 0
    return mot

def _store_original_mot_on_object(obj, data, filepath, kind, label):
    digest = hashlib.sha256(data).hexdigest()
    obj["gtps2_mot_kind"] = str(kind)
    obj["gtps2_mot_label"] = str(label)
    obj["gtps2_mot_original_path"] = str(filepath)
    obj["gtps2_mot_original_sha256"] = digest
    obj["gtps2_mot_original_size"] = int(len(data))
    b64 = base64.b64encode(data).decode("ascii")
    chunk_size = 24000
    chunks = [b64[i:i + chunk_size] for i in range(0, len(b64), chunk_size)]
    obj["gtps2_mot_original_b64_chunks"] = len(chunks)
    for i, chunk in enumerate(chunks):
        obj[f"gtps2_mot_original_b64_{i:03d}"] = chunk

def _recover_original_mot_from_object(obj):
    expected_hash = obj.get("gtps2_mot_original_sha256")
    expected_size = obj.get("gtps2_mot_original_size")
    path = obj.get("gtps2_mot_original_path")
    if path:
        try:
            data = open(path, "rb").read()
            if (expected_size is None or len(data) == int(expected_size)) and \
               (not expected_hash or hashlib.sha256(data).hexdigest() == expected_hash):
                return data
        except Exception:
            pass
    try:
        n = int(obj.get("gtps2_mot_original_b64_chunks", 0))
        if n > 0:
            b64 = "".join(str(obj.get(f"gtps2_mot_original_b64_{i:03d}", "")) for i in range(n))
            data = base64.b64decode(b64.encode("ascii"), validate=True)
            if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
                return None
            return data
    except Exception:
        return None
    return None

def _sanitize_prop_name(name):
    out = []
    for ch in str(name):
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out).strip("_")
    return s or "unnamed"

def _unique_names(names):
    seen = {}
    out = []
    for n in names:
        base = _sanitize_prop_name(n)
        if base not in seen:
            seen[base] = 1
            out.append(base)
        else:
            out.append(f"{base}_{seen[base]:02d}")
            seen[base] += 1
    return out

def _generic_name_table_range(data):
    try:
        footer = _u32(data, 0x10)
        name_off = _u32(data, footer + 12)
        if 0 <= name_off <= footer <= len(data):
            return name_off, footer
    except Exception:
        pass
    return 0, 0

def _metadata_string_channel_map(data):
    out = {}
    metadata = []
    name_off, footer = _generic_name_table_range(data)
    if not name_off or footer <= name_off:
        return out, metadata
    seg = data[name_off:footer]
    o = 0
    while o + 4 <= len(seg):
        if seg[o] == 0x10 and seg[o + 1] in (0x00, 0x01):
            flags = seg[o + 1]
            e = o + 2
            while e < len(seg) and seg[e] != 0:
                e += 1
            if e < len(seg):
                raw = seg[o + 2:e]
                ok = (len(raw) >= 2 and
                      ((65 <= raw[0] <= 90) or (97 <= raw[0] <= 122) or raw[0] == 95) and
                      all((65 <= b <= 90) or (97 <= b <= 122) or (48 <= b <= 57) or b == 95 for b in raw))
                try:
                    name = raw.decode("ascii", "replace") if ok else ""
                except Exception:
                    name = ""
                idx_off = e + 1
                if name:
                    metadata.append(name)
                    if flags == 0x01 and idx_off + 2 <= len(seg):
                        idx = struct.unpack_from("<H", seg, idx_off)[0]
                        out[idx] = name
                        o = idx_off + 2
                        continue
                    o = idx_off
                    continue
        o += 1
    return out, metadata

def _generic_channel_names_from_table(data, count, label=""):
    names = [f"mot_ch_{i:03d}" for i in range(count)]
    fmap, metadata = _metadata_string_channel_map(data)
    for idx, raw in fmap.items():
        if 0 <= idx < count:
            names[idx] = f"mot_ch_{idx:03d}_{raw}"
    return _unique_names(names), metadata

def _generic_channel_names(count, label="", mot=None):
    if mot is not None and getattr(mot, "generic_names", None) and len(mot.generic_names) == count:
        return list(mot.generic_names)
    return _unique_names([f"mot_ch_{i:03d}" for i in range(count)])

try:
    import bpy
    from bpy.props import StringProperty, BoolProperty, FloatProperty, EnumProperty
    from bpy_extras.io_utils import ExportHelper, ImportHelper
    from bpy.types import Operator

    def _import_generic_channel_mot(mot, name, opts):
        import bpy
        fps = opts["fps_source"]
        interp = opts["interpolation"]
        label = getattr(mot, "label", "")
        obj = bpy.data.objects.new(name + ("_" + label if label else "_MOT"), None)
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = 1.0
        (bpy.context.collection or bpy.context.scene.collection).objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        obj["gtps2_mot_generic_channel_count"] = len(mot.channels)
        obj["gtps2_mot_note"] = "Generic/channel-only MOT import. Values are stored as custom animated properties."

        action = bpy.data.actions.new(name + "_GenericMOTChannels")
        obj.animation_data_create()
        obj.animation_data.action = action
        try:
            if hasattr(action, "slots"):
                slot = action.slots.new(id_type="OBJECT", name=obj.name) if len(action.slots) == 0 \
                       else action.slots[0]
                obj.animation_data.action_slot = slot
        except Exception:
            pass
        names = _generic_channel_names(len(mot.channels), label, mot)
        meta = list(getattr(mot, "generic_metadata_strings", []) or [])
        obj["gtps2_mot_generic_names"] = ", ".join(names)
        if meta:
            obj["gtps2_mot_metadata_strings"] = ", ".join(meta[:64])
        for ci, ch in enumerate(mot.channels):
            prop = names[ci]
            obj[prop] = ch[0][1] if ch else 0.0
            path = f'["{prop}"]'
            if ch:
                frames = [k[0] * fps for k in ch]
                vals = [k[1] for k in ch]
                _insert_component_keys(action, obj, path, None, frames, vals, "MOT Channels", interp)
        scene = bpy.context.scene
        scene.render.fps = max(1, int(round(fps)))
        scene.frame_start = 0
        scene.frame_end = max(1, int(math.ceil(mot.duration * fps)))
        scene.frame_set(0)
        return obj

    def _import_single_node_mot(mot, name, opts):
        import bpy
        fps = opts["fps_source"]
        interp = opts["interpolation"]
        order = opts["euler_order"]
        pscale = opts["translation_scale"]
        rscale = (math.pi / 180.0) if opts["rot_degrees"] else 1.0
        b = mot.bones[0]
        obj = bpy.data.objects.new(name + "_SingleNodeMOT", None)
        obj.empty_display_type = "ARROWS"
        obj.empty_display_size = 1.0
        (bpy.context.collection or bpy.context.scene.collection).objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        obj["gtps2_mot_note"] = "Single-node NODE_MOTION imported as an Empty object. Useful for props/carts/camera rigs."
        obj["gtps2_mot_single_node_name"] = b.name
        obj.rotation_mode = order

        action = bpy.data.actions.new(name + "_SingleNodeMOT")
        obj.animation_data_create()
        obj.animation_data.action = action
        try:
            if hasattr(action, "slots"):
                slot = action.slots.new(id_type="OBJECT", name=obj.name) if len(action.slots) == 0 \
                       else action.slots[0]
                obj.animation_data.action_slot = slot
        except Exception:
            pass

        def chan(idx):
            return mot.channels[idx] if 0 <= idx < len(mot.channels) else []
        tset = set()
        for ci in b.trans_idx + b.rot_idx:
            for k in chan(ci):
                tset.add(k[0])
        times = sorted(tset) if tset else [0.0]
        frames = [t * fps for t in times]
        locs = []
        rots = []
        for t in times:
            locs.append(tuple(eval_channel(chan(ci), t) * pscale for ci in b.trans_idx))
            rots.append(tuple(eval_channel(chan(ci), t) * rscale for ci in b.rot_idx))
        def make(path, count, comps):
            for a in range(count):
                vals = [comps[j][a] for j in range(len(frames))]
                _insert_component_keys(action, obj, path, a, frames, vals, "Single Node", interp)
        make("location", 3, locs)
        make("rotation_euler", 3, rots)
        scene = bpy.context.scene
        scene.render.fps = max(1, int(round(fps)))
        scene.frame_start = 0
        scene.frame_end = max(1, int(math.ceil(mot.duration * fps)))
        scene.frame_set(0)
        return obj

    def _export_single_node_inplace(obj, filepath, fps, pscale=1.0, rscale=1.0):
        data = _recover_original_mot_from_object(obj)
        if data is None:
            raise RuntimeError("Original single-node MOT bytes are not available; exact/raw patch export cannot proceed")
        mot = parse_mot_any(data)
        if getattr(mot, "kind", "") != "SINGLE_NODE" or not mot.bones:
            raise RuntimeError("This is not a single-node MOT")
        out = bytearray(data)
        offs, cap = _find_ptr_table(data)
        bounds = offs + [cap]
        b = mot.bones[0]
        action = obj.animation_data.action if obj.animation_data else None
        duration = max((ch[-1][0] for ch in mot.channels if ch), default=0.0)
        def fc_value(path, index, frame, fallback):
            if action:
                fc = _find_fcurve_compat(action, path, index)
                if fc is not None:
                    return float(fc.evaluate(frame))
            return fallback
        def val_for_channel(ci, t):
            frame = t * fps
            if ci in b.trans_idx:
                a = b.trans_idx.index(ci)
                return fc_value("location", a, frame, float(getattr(obj, "location", (0,0,0))[a])) / pscale
            if ci in b.rot_idx:
                a = b.rot_idx.index(ci)
                return fc_value("rotation_euler", a, frame, float(getattr(obj, "rotation_euler", (0,0,0))[a])) / rscale
            return 0.0
        for ci, bo in enumerate(offs):
            if ci not in b.trans_idx + b.rot_idx:
                continue
            end = bounds[ci + 1]
            n = _block_key_count_from_header(data, bo, end)
            if n <= 0:
                continue
            base = bo + 12
            for k in range(n):
                rec = base + k * 16
                _v0, _v1, _v2, t = struct.unpack_from("<ffff", data, rec)
                struct.pack_into("<f", out, rec, float(val_for_channel(ci, t)))
        with open(filepath, "wb") as f:
            f.write(bytes(out))
        return len(out)

    _EULER_ITEMS = [(o, o, "") for o in ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")]
    _INTERP_ITEMS = [
        ("LINEAR", "Linear", "Matches the engine's linear key interpolation"),
        ("BEZIER", "Bezier", "Smoothed with auto-clamped handles"),
        ("CONSTANT", "Constant", "Stepped keys"),
    ]

    # ------------------------------------------------------------------------
    # Package entry points used by the unified GT4 Motion Suite operators.
    # These mirror the original GT4MOT_OT_import / GT4MOT_OT_export execute()
    # routing exactly (character / single-node / generic / raw auto-detect, and
    # exact-bytes-when-unedited export).
    # ------------------------------------------------------------------------

    def do_import(context, filepath, opts):
        with open(filepath, "rb") as f:
            data = f.read()
        mot = parse_mot_any(data)
        name = os.path.splitext(os.path.basename(filepath))[0]
        kind = getattr(mot, "kind", "RAW")
        if kind == "CHARACTER":
            obj = _import_into_blender(mot, name, opts)
        elif kind == "SINGLE_NODE":
            obj = _import_single_node_mot(mot, name, opts)
        else:
            obj = _import_generic_channel_mot(mot, name, opts)
        try:
            _store_original_mot_on_object(obj, data, filepath, kind,
                                          getattr(mot, "label", ""))
            obj["gtps2_mot_import_fps"] = float(opts["fps_source"])
            obj["gtps2_mot_anim_fingerprint"] = _animation_fingerprint(obj)
            scn = getattr(context, "scene", None)
            if scn is not None:
                obj["gtps2_mot_import_frame_start"] = int(scn.frame_start)
                obj["gtps2_mot_import_frame_end"] = int(scn.frame_end)
        except Exception:
            pass
        return name, kind, len(mot.channels)

    def do_export(context, obj, filepath, fps_source, translation_scale,
                  rot_degrees, euler_order):
        """Returns (report_level, message). report_level in {INFO,WARNING,ERROR}."""
        if obj is None:
            return "ERROR", "No active object"
        rscale = (math.pi / 180.0) if rot_degrees else 1.0
        original = _recover_original_mot_from_object(obj)
        edited = _mot_animation_edited(obj, getattr(context, "scene", None))
        if original is not None and not edited:
            with open(filepath, "wb") as f:
                f.write(original)
            return "INFO", ("No edits detected; wrote exact imported MOT bytes "
                            "(%d bytes)" % len(original))
        if obj.type == "ARMATURE":
            opts = {
                "fps_source": float(fps_source),
                "translation_scale": float(translation_scale),
                "rot_degrees": bool(rot_degrees),
                "euler_order": euler_order,
            }
            bones, channels = _armature_to_motion_sparse(obj, opts)
            dense = bones is None
            if dense:
                bones, channels = _armature_to_motion(obj, opts)
            data = build_mot(bones, channels)
            with open(filepath, "wb") as f:
                f.write(data)
            nkeys = sum(len(c) for c in channels if c)
            if dense:
                return "INFO", ("Wrote %d bones, dense per-frame sampling "
                                "(no keyframes found), %d bytes"
                                % (len(bones), len(data)))
            return "INFO", ("Wrote edited animation: %d bones, %d keys, %d bytes"
                            % (len(bones), nkeys, len(data)))
        if str(obj.get("gtps2_mot_kind", "")) == "SINGLE_NODE":
            n = _export_single_node_inplace(obj, filepath, float(fps_source),
                                            pscale=float(translation_scale),
                                            rscale=rscale)
            return "INFO", ("Patched single-node MOT in place with current "
                            "values (%d bytes)" % n)
        if original is not None:
            with open(filepath, "wb") as f:
                f.write(original)
            return "WARNING", ("Edits detected, but this MOT kind has no edit "
                               "exporter yet; wrote the original imported bytes "
                               "(%d bytes)" % len(original))
        return "ERROR", ("Active object must be an armature or an imported "
                         "single-node MOT empty")

except ImportError:
    # bpy unavailable (module used as a plain MOT parser/builder library).
    bpy = None
