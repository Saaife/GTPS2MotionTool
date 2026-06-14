# gt4_camera_export.py
# Blender add-on: export a single animated Blender camera as a GT4 "Mot1"
# single-shot CAMERA (.mot) file. Inverse of gt4_camera_import.py.
#
# Per-shot 18-channel layout that is written (see gt4_camera.py for the map):
#     slot  0,1,2  -> camera EYE   position X, Y, Z
#     slot  3,4,5  -> look-at TARGET position X, Y, Z
#     slot  6,7,8  -> camera UP vector X, Y, Z
#     slot  9      -> FOV (radians)
#     slot 10      -> CameraCoord link (int, hardcoded)
#     slot 11      -> TargetCoord link (int, hardcoded)
#     slot 12..17  -> LOD levels (int, hardcoded 0)
#
# Math (per frame, from camera.matrix_world, Blender Z-up):
#     eye    = matrix_world.translation
#     target = eye + (local -Z)           (point straight ahead of the lens)
#     up     = local +Y
#     fov    = camera.data.angle
# then each vector is converted Blender Z-up -> GT4 Y-up before writing.
#
# Smart compression: any channel whose value is constant across the whole
# timeline is written as exactly two keyframes (frame_start, frame_end), matching
# how native static PS2 cameras are authored.
#
# Install: Edit > Preferences > Add-ons > Install... pick this file, enable it.
# Use:     select a camera, File > Export > Gran Turismo 4 Camera (.mot)
# CLI:     python gt4_camera_export.py    (runs a build+reparse self-test)

# bl_info kept for reference only; the package __init__.py owns the real one.
_bl_info = {
    "name": "Gran Turismo 4 Camera Export (.mot)",
    "author": "reverse-engineered with Claude",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "File > Export > Gran Turismo 4 Camera (.mot)",
    "description": "Export a Blender camera as a single-shot GT4 Mot1 camera file",
    "category": "Import-Export",
}

import struct
import math

SLOTS = 18
EPS = 1e-6


# ----------------------------------------------------------------------------
# Coordinate conversion + compression (pure python, no Blender) ---------------
# ----------------------------------------------------------------------------

def zup_to_yup(v):
    """Blender Z-up -> GT4 Y-up. Exact inverse of the importer's (x,-z,y).
       (X, Y, Z) -> (X, Z, -Y)"""
    return (v[0], v[2], -v[1])


def compress_channel(times, values):
    """Return a list of (t, value) keys. If the value never changes across the
    whole timeline, collapse to two keys (start, end); else keep every frame."""
    if not times:
        return [(0.0, 0.0)]
    if len(times) == 1:
        return [(times[0], values[0])]
    v0 = values[0]
    if all(abs(v - v0) <= EPS for v in values):
        return [(times[0], v0), (times[-1], v0)]          # static -> 2 keys
    return list(zip(times, values))                        # animated -> all keys


# ----------------------------------------------------------------------------
# Mot1 camera writer (pure python) -------------------------------------------
# ----------------------------------------------------------------------------

def _build_block(keys):
    """One channel block: header(count,0,0) + N*(v0,v1,v2,t) + 4B pad.
       count field = size//4 - 2 = 4*N + 2 (v0=v1=v2=value, like static cameras)."""
    n = len(keys)
    out = bytearray()
    out += struct.pack("<III", 4 * n + 2, 0, 0)
    for (t, v) in keys:
        out += struct.pack("<ffff", v, v, v, t)
    out += b"\x00" * 4
    return bytes(out)


def build_camera_mot(channels, name="TEST_CAMERA", cam_link=0, tgt_link=1):
    """channels: list of 18 lists of (t, value).  Returns the .mot bytes."""
    assert len(channels) == SLOTS, "need exactly %d channels" % SLOTS
    name_b = name.encode("ascii")

    out = bytearray()
    out += b"Mot1" + b"\x00" * 12            # 0x00  magic + 12 nulls
    out += struct.pack("<I", 0)              # 0x10  footer offset (patched)
    out += struct.pack("<HH", 1, 1)          # 0x14  (1, node_count=1)
    out += struct.pack("<I", 0)              # 0x18  tail ptr (patched)
    out += struct.pack("<I", 0)              # 0x1C  footer+0x10 (patched)

    # 0x20: small string pool (mirrors the real file; keeps blocks off 0x20)
    out += name_b + b"\x00"
    while len(out) % 16 != 0:
        out += b"\x00"

    # channel blocks
    block_offsets = []
    for ch in channels:
        block_offsets.append(len(out))
        out += _build_block(ch)

    # pointer table: (1,0,0,block_off) per block
    ptr_off = len(out)
    for bo in block_offsets:
        out += struct.pack("<IIII", 1, 0, 0, bo)

    # index table: address of each pointer record
    idx_off = len(out)
    for k in range(SLOTS):
        out += struct.pack("<I", ptr_off + 16 * k)
    while len(out) % 16 != 0:        # PS2 needs 16-byte (128-bit) section alignment;
        out += b"\x00"               # SLOTS*4 is not a multiple of 16 -> pad it.

    # duration (16B): (max_t, 0, 0, 0)
    max_t = max((ch[-1][0] for ch in channels if ch), default=0.0)
    dur_off = len(out)
    out += struct.pack("<ffff", max_t, 0.0, 0.0, 0.0)

    # single node record (32B): (fieldA, count, idx_ptr, base, scale, 0,0,0)
    out += struct.pack("<IIII", 0x20, SLOTS, idx_off, block_offsets[0])
    out += struct.pack("<f", max_t) + b"\x00" * 12

    # name table (camera dialect): 0x10 0x01 <name> 0x00 <u16 slot=0x0A>
    name_off = len(out)
    out += bytes([0x10, 0x01]) + name_b + b"\x00" + struct.pack("<H", 0x0A)
    while len(out) % 16 != 0:        # keep the footer on a 16-byte boundary too
        out += b"\x00"

    # footer
    footer_off = len(out)
    num_named, num_nodes = 1, 1
    out += struct.pack("<IIII", (num_named << 16) | num_nodes,
                       dur_off, footer_off, name_off)
    out += struct.pack("<IHHII", 0, 0, 0xFFFF, num_nodes, 0)        # sentinel header
    out += struct.pack("<IHHH", 0x20, 0, 0, 0xFFFF) + b"\x00" * 6   # node entry

    tail_off = len(out)
    out += b"\x00" * 0x10                                           # trailing pad
    while len(out) % 32 != 0:        # pad EOF to a 32-byte boundary (PS2 DMA)
        out += b"\x00"

    # patch header pointers
    struct.pack_into("<I", out, 0x10, footer_off)
    struct.pack_into("<I", out, 0x18, tail_off)
    struct.pack_into("<I", out, 0x1C, footer_off + 0x10)
    return bytes(out)


# ----------------------------------------------------------------------------
# ANIMATED MENU export (gtauto-style, template-based) --------------------------
# ----------------------------------------------------------------------------
# Menu/GTAuto animated cams are ONE node whose channels are DECLARED by the
# token stream (0B FF 01 <ch0..8> = eye/tgt/up, 0A 01 <ch9> = fov, plus scene
# groups and named channels like fade/shadowOpacity). The engine needs all of
# it, so we clone a native file and swap only the declared camera channels.
# Differences vs the pit-style builder (all verified on gtauto_entrance_cam):
# duration = (maxt+1/60, maxt); 16-byte node record (no scale row); block pad
# byte = last key's value; v1/v2 are spline tangents (we write flat: v1=v2=v0).

def parse_container(data):
    """Full-fidelity parse of a single-node animated camera for rebuilding."""
    from . import cam_import as I
    offs, ptr_off = I._find_ptr_table(data)
    bounds = offs + [ptr_off]
    foot = struct.unpack_from("<I", data, 0x10)[0]
    fr = [struct.unpack_from("<I", data, foot + 4 * i) for i in range(4)]
    fr = [x[0] for x in fr]
    dur_off, name_off = fr[1], fr[3]
    rec0 = dur_off + 0x10
    blocks = []
    for i, bo in enumerate(offs):
        end = bounds[i + 1]
        keys = I._parse_block(data, bo, end)             # (t, v0, v1, v2)
        blocks.append({"keys": keys, "pad": data[end - 4:end]})
    decl = I.parse_token_stream(data, name_off, foot)
    return {
        "h14": data[0x14:0x18],
        "pool": data[0x20:offs[0]],
        "blocks": blocks,
        "node_rec": data[rec0:rec0 + 4],                  # fieldA only
        "names_raw": data[name_off:foot],                 # token stream verbatim
        "f0": fr[0],
        "dur": data[dur_off:dur_off + 16],
        "decl": decl,
    }


def rebuild_container(tpl, replace=None):
    """Rebuild the container; replace = {channel_index: [(t, v), ...]}."""
    replace = replace or {}
    out = bytearray()
    out += b"Mot1" + b"\x00" * 12
    out += struct.pack("<I", 0) + tpl["h14"]             # @0x10 patched, @0x14 raw
    out += struct.pack("<II", 0, 0)                      # @0x18/@0x1C patched
    out += tpl["pool"]

    block_offsets = []
    for ci, blk in enumerate(tpl["blocks"]):
        block_offsets.append(len(out))
        if ci in replace:
            keys = [(t, v, v, v) for (t, v) in replace[ci]]
            pad = struct.pack("<f", keys[-1][1])
        else:
            keys, pad = blk["keys"], blk["pad"]
        out += struct.pack("<III", 4 * len(keys) + 2, 0, 0)
        for (t, v0, v1, v2) in keys:
            out += struct.pack("<ffff", v0, v1, v2, t)
        out += pad

    ptr_off = len(out)
    for bo in block_offsets:
        out += struct.pack("<IIII", 1, 0, 0, bo)
    idx_off = len(out)
    for k in range(len(block_offsets)):
        out += struct.pack("<I", ptr_off + 16 * k)
    while len(out) % 16 != 0:
        out += b"\x00"

    dur_off = len(out)
    if replace:
        maxt = max(k[-1][0] for b in tpl["blocks"] for k in [b["keys"]] if k)
        for ch in replace.values():
            maxt = max(maxt, ch[-1][0])
        out += struct.pack("<ffff", maxt + 1.0 / 60.0, maxt, 0.0, 0.0)
    else:
        out += tpl["dur"]

    out += tpl["node_rec"]                                # 16-byte node record
    out += struct.pack("<III", len(block_offsets), idx_off, block_offsets[0])

    name_off = len(out)
    out += tpl["names_raw"]

    footer_off = len(out)
    out += struct.pack("<IIII", tpl["f0"], dur_off, footer_off, name_off)
    out += struct.pack("<IIHHI", 0x20, 0, 0xFFFF, 0, 0)
    tail_off = len(out)
    out += b"\x00" * 0x30
    while len(out) % 32 != 0:
        out += b"\x00"

    struct.pack_into("<I", out, 0x10, footer_off)
    struct.pack_into("<I", out, 0x18, tail_off)
    struct.pack_into("<I", out, 0x1C, footer_off + 0x10)
    return bytes(out)


def build_static_camera_mot(eye, target, up, fov, name="TEST_CAMERA", link=1):
    """STATIC (menu) format: no pointer/index tables - one dense 256-byte struct.
    Byte-for-byte layout matches the native menu camera (camera.txt):
      0x00 Mot1+12nulls | 0x10 hdr | 0x20 name | 0x30/0x50 node descs |
      0x70 packed eye/target/up/fov | 0xB0 footer."""
    # truncate to 15 chars so name + null terminator fit the 16-byte 0x20..0x2F
    # block; the zero-filled buffer null-pads the remainder automatically.
    name_b = name.encode("ascii", "replace")[:15]
    out = bytearray(0x100)                            # 256 bytes, zero-filled
    out[0:4] = b"Mot1"
    struct.pack_into("<I", out, 0x10, 0xB0)          # footer offset
    struct.pack_into("<HH", out, 0x14, 1, 2)         # @0x14=1, @0x16=2 (2 nodes)
    struct.pack_into("<I", out, 0x1C, 0xC0)          # footer + 0x10

    out[0x20:0x20 + len(name_b)] = name_b            # node0 name; 0x20..0x2F null-padded
    n0 = 0x20
    # node1 (target) is unnamed -> point at a null byte; clamp to 0x2F so a long
    # name can never push this into the node-descriptor block at 0x30.
    n1 = min(0x20 + len(name_b) + 1, 0x2F)

    # node descriptors (camera + target). The 0.15 / 0.16667 pair is preserved
    # from the native file (clip/range constants the menu engine expects).
    struct.pack_into("<ffII", out, 0x30, 0.15, 1.0 / 6.0, 0, 0)
    struct.pack_into("<IIII", out, 0x40, n0, 0, 0x30, 0)
    struct.pack_into("<ffII", out, 0x50, 0.15, 1.0 / 6.0, 0, 0)
    struct.pack_into("<IIII", out, 0x60, n1, 0, 0x30, 0)

    # packed transform struct @0x70 (markers + 10 floats + link)
    b = 0x70
    out[b:b + 3] = bytes([0x0B, 0x00, 0x00])         # lead marker
    struct.pack_into("<fff", out, b + 3,  eye[0],    eye[1],    eye[2])
    struct.pack_into("<fff", out, b + 15, target[0], target[1], target[2])
    struct.pack_into("<fff", out, b + 27, up[0],     up[1],     up[2])
    out[b + 39:b + 41] = bytes([0x0A, 0x00])         # mid marker
    struct.pack_into("<f", out, b + 41, fov)
    struct.pack_into("<I", out, b + 45, link)

    # footer @0xB0
    struct.pack_into("<IIII", out, 0xB0, 2, 0x30, 0xB0, 0x70)
    struct.pack_into("<IIII", out, 0xC0, n1, 0x00010000, 0x0000FFFF, 0)
    struct.pack_into("<IIII", out, 0xD0, n0, 0x00000000, 0x0000FFFF, 0)
    return bytes(out)


# ----------------------------------------------------------------------------
# Blender data extraction ----------------------------------------------------
# ----------------------------------------------------------------------------

def _extract_channels(cam_obj, scene, context, fps):
    """Sample the camera every frame -> 18 compressed channels (GT4 Y-up)."""
    from mathutils import Vector

    f0, f1 = scene.frame_start, scene.frame_end
    cur = scene.frame_current
    frames = list(range(f0, f1 + 1))

    series = [[] for _ in range(SLOTS)]      # per-slot list of values
    times = []
    try:
        for f in frames:
            scene.frame_set(f)
            deps = context.evaluated_depsgraph_get()
            ev = cam_obj.evaluated_get(deps)
            mw = ev.matrix_world
            rot = mw.to_3x3()
            eye = mw.translation
            fwd = (rot @ Vector((0.0, 0.0, -1.0))).normalized()   # local -Z
            up  = (rot @ Vector((0.0, 1.0, 0.0))).normalized()    # local +Y
            tgt = eye + fwd                                        # point ahead
            fov = ev.data.angle

            e = zup_to_yup(eye); t = zup_to_yup(tgt); u = zup_to_yup(up)
            vals = [e[0], e[1], e[2], t[0], t[1], t[2], u[0], u[1], u[2], fov,
                    0, 0, 0, 0, 0, 0, 0, 0]   # slots 10-17 filled below
            for s in range(SLOTS):
                series[s].append(vals[s])
            times.append(f / fps)
    finally:
        scene.frame_set(cur)

    # hardcode the static link / LOD slots
    n = len(times)
    consts = {10: 0, 11: 1, 12: 0, 13: 0, 14: 0, 15: 0, 16: 0, 17: 0}
    for s, c in consts.items():
        series[s] = [float(c)] * n

    return [compress_channel(times, series[s]) for s in range(SLOTS)]


def _export(cam_obj, filepath, context):
    scene = context.scene
    fps = max(1.0, scene.render.fps / max(1, scene.render.fps_base))
    channels = _extract_channels(cam_obj, scene, context, fps)
    data = build_camera_mot(channels, name="TEST_CAMERA")
    with open(filepath, "wb") as fh:
        fh.write(data)
    n_static = sum(1 for ch in channels if len(ch) <= 2)
    return len(data), n_static


def _sample_frame(cam_obj, context):
    """eye / target / up / fov of the camera at the CURRENT frame, in GT4 Y-up."""
    from mathutils import Vector
    ev = cam_obj.evaluated_get(context.evaluated_depsgraph_get())
    mw = ev.matrix_world
    rot = mw.to_3x3()
    eye = mw.translation
    fwd = (rot @ Vector((0.0, 0.0, -1.0))).normalized()   # local -Z
    up  = (rot @ Vector((0.0, 1.0, 0.0))).normalized()    # local +Y
    tgt = eye + fwd
    return (zup_to_yup(eye), zup_to_yup(tgt), zup_to_yup(up), ev.data.angle)


def _export_static(cam_obj, filepath, context):
    """STATIC (menu) export: bypass the block/pointer builder entirely - just pack
    the current frame into the dense 256-byte struct."""
    eye, tgt, up, fov = _sample_frame(cam_obj, context)
    data = build_static_camera_mot(eye, tgt, up, fov, name=cam_obj.name)
    with open(filepath, "wb") as fh:
        fh.write(data)
    return len(data)


def _export_menu_animated(cam_obj, filepath, context, template_path):
    """ANIMATED MENU export: clone a native menu cam (template) and swap the
    channels its token stream declares as eye/target/up/fov."""
    from . import cam_import as I
    tpl_data = I.load_any(template_path)
    tpl = parse_container(tpl_data)
    decl = tpl["decl"]
    if not decl["cam"]:
        raise ValueError("template has no indexed 0x0B camera declaration")

    scene = context.scene
    fps = max(1.0, scene.render.fps / max(1, scene.render.fps_base))
    channels = _extract_channels(cam_obj, scene, context, fps)  # slots 0-9 + links
    replace = {}
    for k in range(9):                       # eye/target/up -> declared indices
        replace[decl["cam"][k]] = channels[k]
    if decl["fov"] is not None:
        replace[decl["fov"]] = channels[9]
    data = rebuild_container(tpl, replace)
    with open(filepath, "wb") as fh:
        fh.write(data)
    return len(data), len(replace)


# ----------------------------------------------------------------------------
# Operator / menu ------------------------------------------------------------
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# Package entry points used by the unified GT4 Motion Suite export operator.
# ----------------------------------------------------------------------------

def pick_camera(context):
    """Return the camera to export: active object if it's a camera, else the
    first selected camera, else None."""
    o = context.active_object
    if o and o.type == "CAMERA":
        return o
    for o in context.selected_objects:
        if o.type == "CAMERA":
            return o
    return None


def do_export(context, cam, filepath, export_type, template_path=""):
    """Write a GT4 camera .mot. export_type in {ANIMATED, MENU_ANIM, STATIC}.
    Returns a human-readable status message; raises on hard errors."""
    import bpy
    if export_type == "STATIC":
        size = _export_static(cam, filepath, context)
        return "Exported STATIC '%s' -> %d bytes (current frame)" % (cam.name, size)
    if export_type == "MENU_ANIM":
        if not template_path:
            raise ValueError("Pick a native menu cam as Template .mot")
        size, nrep = _export_menu_animated(cam, filepath, context,
                                           bpy.path.abspath(template_path))
        return ("Exported MENU '%s' -> %d bytes (%d channels swapped)"
                % (cam.name, size, nrep))
    size, n_static = _export(cam, filepath, context)
    return ("Exported ANIMATED '%s' -> %d bytes (%d static channels)"
            % (cam.name, size, n_static))
