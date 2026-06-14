# gt4_camera_import.py
# Blender add-on: import Gran Turismo 4 "Mot1" CAMERA (.mot) animation.
#
# Companion to the character importer (gt4_mot_import_fixed.py). A GT4 camera mot
# packs many camera "shots"; each shot is a node of 18 channels with this layout
# (reverse-engineered from pitMans.part1.camera.mot - see gt4_camera.py):
#
#     slot  0,1,2  -> camera EYE   position X, Y, Z
#     slot  3,4,5  -> look-at TARGET position X, Y, Z
#     slot  6,7,8  -> camera UP vector X, Y, Z  (unit; gives roll)
#     slot  9      -> FOV (radians)
#     slot 10,11   -> CameraCoord / TargetCoord  *integer links, NOT coords*
#     slot 12..17  -> LOD levels (ignored here)
#   global channel index = shot * 18 + slot
#
# This add-on builds, per shot, a real Blender Camera with clean LOCATION +
# ROTATION_QUATERNION + LENS keyframes. The rotation is baked from a look-at
# matrix computed from eye/target/up (no Track-To constraint, no target Empty),
# so the curves are standard and easy to edit / re-export.
#
# Install: Edit > Preferences > Add-ons > Install... pick this file, enable it.
# Use:     File > Import > Gran Turismo 4 Camera (.mot)
# CLI:     python gt4_camera_import.py file.mot   (prints a parse report)

# bl_info kept for reference only; the package __init__.py owns the real one.
_bl_info = {
    "name": "Gran Turismo 4 Camera (.mot)",
    "author": "reverse-engineered with Claude",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > Gran Turismo 4 Camera (.mot)",
    "description": "Import GT4 Mot1 camera shots as Blender cameras (baked look-at)",
    "category": "Import-Export",
}

import struct
import math

# ----------------------------------------------------------------------------
# Pure parser (no Blender dependency) ----------------------------------------
# ----------------------------------------------------------------------------

SLOTS = 18
EYE    = (0, 1, 2)
TARGET = (3, 4, 5)
UP     = (6, 7, 8)
FOV    = 9


def _u16(d, o): return struct.unpack_from("<H", d, o)[0]
def _u32(d, o): return struct.unpack_from("<I", d, o)[0]


def _is_ptr_record(d, o):
    return (0 <= o and o + 16 <= len(d)
            and _u32(d, o) == 1 and _u32(d, o + 4) == 0 and _u32(d, o + 8) == 0
            and 0x20 <= _u32(d, o + 12) < len(d))


def _find_ptr_table(d):
    """Locate the contiguous run of (1,0,0,off) channel-block pointer records."""
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


def _parse_block(d, start, end):
    """Read keyframes until time stops strictly increasing (= zero padding)."""
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


def eval_channel(keys, t):
    """Linear sample of one channel at time t (keys are time-sorted (t,v,..))."""
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


def parse_camera_nodes(data):
    """Return (nodes, slot_names).
    nodes[i] = list of 18 channels; channel = list of (t, v0, v1, v2).
    slot_names = {slot_index: name} from the camera name table."""
    if data[:4] != b"Mot1":
        raise ValueError("not a Mot1 file (bad magic)")
    footer = _u32(data, 0x10)
    fr = [_u32(data, footer + 4 * i) for i in range(4)]
    dur_off, name_off = fr[1], fr[3]

    offs, cap = _find_ptr_table(data)
    bounds = offs + [cap]
    end_of = {offs[i]: bounds[i + 1] for i in range(len(offs))}

    rec0 = dur_off + 0x10
    idx_off = _u32(data, rec0 + 8)            # idx_ptr of the first node record
    base_block = offs[0]

    def channel_at(logical_idx):
        ptr_rec = _u32(data, idx_off + 4 * logical_idx)
        blk = _u32(data, ptr_rec + 12)
        return _parse_block(data, blk, end_of[blk])

    # node records (32B): (fieldA, count, idx_ptr, base==0x310, scale, 0,0,0)
    nodes = []
    o = rec0
    while o < name_off:
        count, idx_ptr, base = _u32(data, o + 4), _u32(data, o + 8), _u32(data, o + 12)
        if base != base_block or not (0 < count <= 64):
            break
        first = (idx_ptr - idx_off) // 4
        nodes.append([channel_at(first + s) for s in range(count)])
        o += 0x20

    # name table: 0x10 0x01 <name> 0x00 <u16 slot>
    slot_names = {}
    o = name_off
    while o + 4 < footer:
        if data[o] == 0x10 and data[o + 1] == 0x01:
            o += 2
            e = data.index(0, o)
            slot_names[_u16(data, e + 1)] = data[o:e].decode("ascii", "replace")
            o = e + 3
        else:
            break
    return nodes, slot_names


# ----------------------------------------------------------------------------
# Node-tree TOKEN STREAM (shared by static AND animated menu cameras) ---------
# ----------------------------------------------------------------------------
# The "name table" is really a token stream declaring the node's channels.
# Each group token is followed by a mode byte: 0 = INLINE float32 values
# (static/menu files, no pointer tables) or nonzero = u16 CHANNEL INDICES
# (animated files). Verified on camera.txt (inline) and gtauto_entrance_cam
# (indexed: 0B FF 01 <ch 0..8>, 0A 01 <ch 9>, 0C/0D/0E/0F groups, 10 01 names).
#   0x0B <m> <m2>  camera group: 9 elements = eye XYZ, target XYZ, up XYZ
#   0x0A <m>       fov: 1 element
#   0x0C/0D/0E/0F <inst> <mask>  other scene groups: popcount(mask) u16 indices
#   0x10 0x01 <ascii name> 0x00 <u16 ch>   named scalar channel
#   0x01 (then zeros)   end record


def _cstr(data, o):
    e = data.index(0, o)
    return data[o:e].decode("ascii", "replace")


def parse_token_stream(data, o, end):
    """Parse the node token stream. Returns a dict with camera declarations:
    cam (9 channel indices or None), cam_vals (9 inline floats or None),
    fov / fov_val, names {ch: name}, groups [(token, inst, mask, indices)]."""
    r = {"cam": None, "cam_vals": None, "fov": None, "fov_val": None,
         "names": {}, "groups": []}
    while o < end:
        t = data[o]
        if t == 0x0B:
            mode = data[o + 1]; o += 3                  # 2 mode/flag bytes
            if mode == 0:
                r["cam_vals"] = list(struct.unpack_from("<9f", data, o)); o += 36
            else:
                r["cam"] = [_u16(data, o + 2 * i) for i in range(9)]; o += 18
        elif t == 0x0A:
            mode = data[o + 1]; o += 2                  # 1 mode byte
            if mode == 0:
                r["fov_val"] = struct.unpack_from("<f", data, o)[0]; o += 4
            else:
                r["fov"] = _u16(data, o); o += 2
        elif t in (0x0C, 0x0D, 0x0E, 0x0F):
            inst, mask = data[o + 1], data[o + 2]; o += 3
            n = bin(mask).count("1")
            idx = [_u16(data, o + 2 * i) for i in range(n)]; o += 2 * n
            r["groups"].append((t, inst, mask, idx))
        elif t == 0x10 and data[o + 1] == 0x01:
            o += 2
            e = data.index(0, o)
            nm = data[o:e].decode("ascii", "replace")
            r["names"][_u16(data, e + 1)] = nm
            o = e + 3
        elif t == 0x01:                                  # end record
            break
        else:                                            # unknown token: stop
            break
    return r


def is_static_camera(data):
    """Static files have no (1,0,0,off) channel-block pointer table."""
    try:
        offs, _ = _find_ptr_table(data)
        return not offs
    except ValueError:
        return True


def parse_static_camera(data):
    """Token-parse a static (menu) camera: footer field-3 points at the token
    stream whose 0x0B/0x0A groups carry INLINE values. Returns the same
    (nodes, slot_names) shape as the animated parser (1-key channels)."""
    footer = _u32(data, 0x10)
    base = _u32(data, footer + 12)              # footer field-3 -> token stream
    r = parse_token_stream(data, base, footer)
    if r["cam_vals"] is None:
        raise ValueError("static camera: no inline 0x0B camera group found")
    fov = r["fov_val"] if r["fov_val"] is not None else 0.6
    name = _cstr(data, 0x20)
    vals = r["cam_vals"] + [fov] + [0.0] * 8
    node = [[(0.0, v)] for v in vals]          # one static keyframe per channel
    return [node], {10: r["names"].get(10, name)}


def load_any(path):
    """Raw .mot, or a whitespace hex-dump .txt (like the emulator dumps)."""
    import re
    raw = open(path, "rb").read()
    if raw[:4] == b"Mot1":
        return raw
    txt = raw.decode("latin1")
    return bytes(int(h, 16) for h in re.findall(r"\b[0-9A-Fa-f]{2}\b", txt))


def parse_camera(data):
    """Auto-route: animated (pointer/index tables) vs static (packed struct)."""
    if is_static_camera(data):
        return parse_static_camera(data)
    return parse_camera_nodes(data)


# ----------------------------------------------------------------------------
# Math helpers (need mathutils; only used inside Blender) ---------------------
# ----------------------------------------------------------------------------

def _yup_to_zup(Vector, v):
    """GT4 Y-up -> Blender Z-up. Pure rotation (det=+1), valid for points & dirs.
       (x, y, z)  ->  (x, -z, y)"""
    return Vector((v[0], -v[2], v[1]))


def _look_quat(Vector, Matrix, eye, target, up):
    """Exact camera look-at as a quaternion (Blender camera looks down -Z, up +Y).
       All inputs must already be in Blender (Z-up) space."""
    fwd = target - eye
    if fwd.length < 1e-9:
        fwd = Vector((0.0, 1.0, 0.0))
    fwd.normalize()
    u = up.normalized() if up.length > 1e-9 else Vector((0.0, 0.0, 1.0))
    x = fwd.cross(u)                                   # camera local +X (right)
    if x.length < 1e-9:                                # up parallel to view dir
        u = Vector((0.0, 0.0, 1.0)) if abs(fwd.z) < 0.99 else Vector((0.0, 1.0, 0.0))
        x = fwd.cross(u)
    x.normalize()
    z = -fwd                                           # camera local +Z (back)
    y = z.cross(x)                                     # camera local +Y (true up)
    # columns are the camera's local axes expressed in world space
    R = Matrix(((x.x, y.x, z.x),
                (x.y, y.y, z.y),
                (x.z, y.z, z.z)))
    return R.to_quaternion()


# ----------------------------------------------------------------------------
# Blender import -------------------------------------------------------------
# ----------------------------------------------------------------------------

def _import_cameras(nodes, slot_names, opts):
    import bpy
    from mathutils import Vector, Matrix

    fps        = opts["fps"]
    fov_scale  = opts["fov_scale"]
    interp     = opts["interpolation"]
    rebase     = opts["rebase_each_shot"]
    per_frame  = opts["bake_every_frame"]

    scene = bpy.context.scene
    scene.render.fps = max(1, int(round(fps)))

    # dedicated collection
    coll = bpy.data.collections.new(opts["collection_name"])
    scene.collection.children.link(coll)

    def sample(ch, t):
        return eval_channel(ch, t)

    all_frames = []
    first_cam = None

    for i, node in enumerate(nodes):
        nm = "Shot_%02d" % i
        cam_data = bpy.data.cameras.new(nm)
        cam_data.lens_unit = "FOV"
        cam_data.sensor_fit = "HORIZONTAL"        # so .angle is the horizontal FOV
        cam_obj = bpy.data.objects.new(nm, cam_data)
        coll.objects.link(cam_obj)
        cam_obj.rotation_mode = "QUATERNION"
        if first_cam is None:
            first_cam = cam_obj

        # ---- choose sample times ----
        tset = set()
        for s in EYE + TARGET + UP + (FOV,):
            for k in node[s]:
                tset.add(k[0])
        times = sorted(tset) if tset else [0.0]
        if per_frame and len(times) > 1:
            t0, t1 = times[0], times[-1]
            nfr = max(1, int(round((t1 - t0) * fps)))
            times = [t0 + (t1 - t0) * j / nfr for j in range(nfr + 1)]

        t_origin = times[0] if rebase else 0.0

        prev_q = None
        for t in times:
            # 1) convert eye / target / up to Blender space FIRST
            eye = _yup_to_zup(Vector, (sample(node[0], t), sample(node[1], t), sample(node[2], t)))
            tgt = _yup_to_zup(Vector, (sample(node[3], t), sample(node[4], t), sample(node[5], t)))
            up  = _yup_to_zup(Vector, (sample(node[6], t), sample(node[7], t), sample(node[8], t)))
            fov = max(1e-4, min(math.pi - 1e-4, sample(node[9], t) * fov_scale))

            # 2) bake the exact look-at rotation
            q = _look_quat(Vector, Matrix, eye, tgt, up)
            if prev_q is not None and q.dot(prev_q) < 0.0:   # keep curve continuous
                q.negate()
            prev_q = q

            frame = int(round((t - t_origin) * fps)) + (1 if rebase else 0)
            all_frames.append(frame)

            # 3) keyframe location, rotation_quaternion, lens
            cam_obj.location = eye
            cam_obj.rotation_quaternion = q
            cam_obj.keyframe_insert("location", frame=frame)
            cam_obj.keyframe_insert("rotation_quaternion", frame=frame)

            cam_data.angle = fov                  # Blender camera FOV is in radians
            cam_data.keyframe_insert("lens", frame=frame)

        _set_interp(cam_obj, interp)
        _set_interp(cam_data, interp)

    if all_frames:
        scene.frame_start = min(all_frames)
        scene.frame_end = max(all_frames)
        scene.frame_set(scene.frame_start)
    if first_cam is not None and opts["set_active"]:
        scene.camera = first_cam
    return len(nodes)


def _set_interp(id_block, interp):
    ad = getattr(id_block, "animation_data", None)
    if not ad or not ad.action:
        return
    for fc in ad.action.fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = interp
            if interp == "BEZIER":
                kp.handle_left_type = kp.handle_right_type = "AUTO_CLAMPED"
        fc.update()


# ----------------------------------------------------------------------------
# Operator / menu ------------------------------------------------------------
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# Package entry point used by the unified GT4 Motion Suite import operator.
# ----------------------------------------------------------------------------

def do_import(filepath, opts):
    """Load a GT4 camera .mot (or hex .txt) and build Blender cameras.

    opts keys: collection_name, fps, fov_scale, interpolation,
               bake_every_frame, rebase_each_shot, set_active
    Returns (shot_count, kind) where kind is "static" or "animated".
    """
    data = load_any(filepath)
    kind = "static" if is_static_camera(data) else "animated"
    nodes, slot_names = parse_camera(data)
    n = _import_cameras(nodes, slot_names, opts)
    return n, kind
