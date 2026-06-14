# bl_info kept for reference only; the package __init__.py owns the real one.
_bl_info = {
    "name": "GT4 Light Motion (.mot)",
    "author": "Saif/Claude",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "File > Import/Export, View3D > Sidebar > GT4 Light",
    "description": "Import/export Gran Turismo 4 Mot1 light.mot files with slider UI",
    "category": "Import-Export",
}

import struct

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

# ============================================================================
#  Mot1 format core (embedded copy of gt4mot.py, byte-perfect round-trip)
# ============================================================================

MAGIC = b"Mot1"

PARAM_POSITION = 0x0F
PARAM_COLOR0 = 0x0C
PARAM_COLOR1 = 0x0D
PARAM_COLOR2 = 0x0E
PARAM_NAMED = 0x10
PARAM_END = 0x01

PARAM_NCOMP = {PARAM_POSITION: 3, PARAM_COLOR0: 4, PARAM_COLOR1: 4, PARAM_COLOR2: 4}


class Curve:
    """1-D piecewise cubic Bezier. keys=[(t,v),...]; ctrl=[(c1,c2),...]."""

    def __init__(self, keys, ctrl):
        assert len(ctrl) == len(keys) - 1
        self.keys = [(float(t), float(v)) for t, v in keys]
        self.ctrl = [(float(a), float(b)) for a, b in ctrl]

    @classmethod
    def from_bytes(cls, data, off):
        count, z0, z1 = struct.unpack_from("<III", data, off)
        if z0 or z1 or (count - 2) % 4:
            raise ValueError(f"bad curve block at {off:#x}")
        nseg = (count - 2) // 4
        vals = struct.unpack_from(f"<{count - 1}f", data, off + 12)
        keys = [(0.0, vals[0])]
        ctrl = []
        for k in range(nseg):
            c1, c2, t, p = vals[1 + 4 * k : 5 + 4 * k]
            ctrl.append((c1, c2))
            keys.append((t, p))
        return cls(keys, ctrl)

    def to_bytes(self):
        vals = [self.keys[0][1]]
        for i, (c1, c2) in enumerate(self.ctrl):
            t, p = self.keys[i + 1]
            vals += [c1, c2, t, p]
        return struct.pack("<III", 4 * len(self.ctrl) + 2, 0, 0) + struct.pack(
            f"<{len(vals)}f", *vals
        )


class Channel:
    def __init__(self, curve=None, const=None):
        self.curve = curve
        self.const = const


class LightBinding:
    def __init__(self, param, light_index, channels):
        self.param = param
        self.light_index = light_index
        self.channels = channels


class NamedBinding:
    def __init__(self, name, channel):
        self.name = name
        self.channel = channel


class MotFile:
    def __init__(self):
        self.name = "bottomShadow"
        self.duration = 24.15
        self.unk_time = 1.0666667
        self.curves = []
        self.bindings = []

    # ----------------------------------------------------------- parse
    @classmethod
    def parse(cls, data):
        if data[:4] != MAGIC:
            raise ValueError("not a Mot1 file (bad magic)")
        self = cls()
        p_motion, _nm, _nn, _z, _p_node = struct.unpack_from("<IHHII", data, 0x10)
        self.name = data[0x20 : data.index(b"\x00", 0x20)].decode("ascii")

        _one, p_hdrA, _p_self, p_bind = struct.unpack_from("<IIII", data, p_motion)
        self.duration, self.unk_time = struct.unpack_from("<ff", data, p_hdrA)
        _name_off, n_curves, p_table, _ = struct.unpack_from("<IIII", data, p_hdrA + 16)

        for i in range(n_curves):
            rec_off = struct.unpack_from("<I", data, p_table + 4 * i)[0]
            c_off = struct.unpack_from("<IIII", data, rec_off)[3]
            self.curves.append(Curve.from_bytes(data, c_off))

        pos = p_bind
        while True:
            t = data[pos]
            if t == PARAM_END:
                break
            if t in PARAM_NCOMP:
                idx, mask = data[pos + 1], data[pos + 2]
                pos += 3
                chans = []
                for ci in range(PARAM_NCOMP[t]):
                    if mask & (1 << ci):
                        chans.append(Channel(curve=struct.unpack_from("<H", data, pos)[0]))
                        pos += 2
                    else:
                        chans.append(Channel(const=struct.unpack_from("<f", data, pos)[0]))
                        pos += 4
                self.bindings.append(LightBinding(t, idx, chans))
            elif t == PARAM_NAMED:
                mode = data[pos + 1]
                pos += 2
                end = data.index(b"\x00", pos)
                nm = data[pos:end].decode("ascii")
                pos = end + 1
                if mode & 1:
                    ch = Channel(curve=struct.unpack_from("<H", data, pos)[0])
                    pos += 2
                else:
                    ch = Channel(const=struct.unpack_from("<f", data, pos)[0])
                    pos += 4
                self.bindings.append(NamedBinding(nm, ch))
            else:
                raise ValueError(f"unknown binding type {t:#x} at {pos:#x}")
        return self

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return cls.parse(f.read())

    # ----------------------------------------------------------- write
    def build(self):
        def align16(b):
            return b + b"\x00" * (-len(b) % 16)

        name_bytes = align16(self.name.encode("ascii") + b"\x00")
        name_off = 0x20
        cursor = name_off + len(name_bytes)

        curve_blobs, curve_offs = [], []
        for c in self.curves:
            blob = c.to_bytes()
            curve_offs.append(cursor)
            curve_blobs.append(blob)
            cursor += len(blob)

        rec_offs, recs = [], b""
        for off in curve_offs:
            rec_offs.append(cursor + len(recs))
            recs += struct.pack("<IIII", 1, 0, 0, off)
        cursor += len(recs)

        table_off = cursor
        table = b"".join(struct.pack("<I", o) for o in rec_offs)
        cursor += len(table)

        hdrA_off = cursor
        hdrA = struct.pack("<ff8x", self.duration, self.unk_time)
        hdrB = struct.pack("<IIII", name_off, len(self.curves), table_off, 0)
        cursor += 32

        bind_off = cursor
        bind = b""
        for b in self.bindings:
            if isinstance(b, LightBinding):
                mask, payload = 0, b""
                for ci, ch in enumerate(b.channels):
                    if ch.curve is not None:
                        mask |= 1 << ci
                        payload += struct.pack("<H", ch.curve)
                    else:
                        payload += struct.pack("<f", ch.const)
                bind += bytes([b.param, b.light_index, mask]) + payload
            else:
                animated = b.channel.curve is not None
                bind += bytes([PARAM_NAMED, 1 if animated else 0])
                bind += b.name.encode("ascii") + b"\x00"
                if animated:
                    bind += struct.pack("<H", b.channel.curve)
                else:
                    bind += struct.pack("<f", b.channel.const)
        bind += bytes([PARAM_END])
        bind = align16(bind)
        cursor += len(bind)

        motion_off = cursor
        motion = struct.pack("<IIII", 1, hdrA_off, motion_off, bind_off)
        node_off = motion_off + 16
        node = struct.pack("<IIHHI", name_off, 0, 0xFFFF, 0, 0)
        header = MAGIC + b"\x00" * 12 + struct.pack("<IHHII", motion_off, 1, 1, 0, node_off)
        return (
            header + name_bytes + b"".join(curve_blobs) + recs + table + hdrA + hdrB
            + bind + motion + node
        )

    def save(self, path):
        with open(path, "wb") as f:
            f.write(self.build())


# ============================================================================
#  Blender helpers
# ============================================================================

RIG_TAG = "gt4mot"
LIGHT_TAG = "gt4_light_index"

# soft slider ranges for the named parameters
NAMED_RANGES = {
    "Alpha0": (0.0, 1.0), "Alpha1": (0.0, 1.0), "Alpha2": (0.0, 1.0), "Alpha3": (0.0, 1.0),
    "BlackLevel": (0.0, 1.0), "WhiteLevel": (0.0, 2.0), "ColorScale": (0.0, 2.0),
    "shadowDarkness0": (0.0, 1.0), "shadowDarkness1": (0.0, 1.0),
    "shadowBlur0": (0.0, 30.0), "shadowBlur1": (0.0, 30.0),
    "bottomShadow": (0.0, 1.0),
}
NAMED_DESC = {
    "Alpha0": "Morning lighting-state blend weight",
    "Alpha1": "Midday lighting-state blend weight",
    "Alpha2": "Evening lighting-state blend weight",
    "Alpha3": "Night lighting-state blend weight",
    "BlackLevel": "Tone mapping floor",
    "WhiteLevel": "Tone mapping ceiling",
    "ColorScale": "Global color multiplier",
    "shadowDarkness0": "Shadow layer 0 darkness",
    "shadowDarkness1": "Shadow layer 1 darkness",
    "shadowBlur0": "Shadow layer 0 blur radius",
    "shadowBlur1": "Shadow layer 1 blur radius",
    "bottomShadow": "Car ground/contact shadow strength",
}


def _prop_ui(idb, name, soft):
    """Attach slider min/max metadata to a custom property (best effort)."""
    try:
        ui = idb.id_properties_ui(name)
        ui.update(min=-1.0e6, max=1.0e6, soft_min=soft[0], soft_max=soft[1],
                  description=NAMED_DESC.get(name, ""))
    except Exception:
        pass


def _new_action(id_block, name):
    act = bpy.data.actions.new(name)
    ad = id_block.animation_data_create()
    ad.action = act
    return act


def _finalize_anim(id_block):
    """Blender 4.4+ slotted actions: make sure a slot is assigned."""
    ad = getattr(id_block, "animation_data", None)
    if not ad or not ad.action:
        return
    if hasattr(ad, "action_slot"):
        try:
            slots = ad.action.slots
            if len(slots) and ad.action_slot is None:
                ad.action_slot = slots[0]
        except Exception:
            pass


def _action_fcurves(id_block):
    ad = getattr(id_block, "animation_data", None)
    if not ad or not ad.action:
        return []
    try:
        return list(ad.action.fcurves)
    except Exception:
        return []


def _fcurve_lookup(id_block):
    return {(fc.data_path, fc.array_index): fc for fc in _action_fcurves(id_block)}


def _curve_to_fcurve(fc, curve, fph, negate=False):
    """Write a Mot1 Curve into a Blender F-curve, losslessly."""
    sgn = -1.0 if negate else 1.0
    keys, ctrl = curve.keys, curve.ctrl
    n = len(keys)
    fc.keyframe_points.add(n)
    kps = fc.keyframe_points
    for i, (t, v) in enumerate(keys):
        kp = kps[i]
        kp.co = (t * fph, sgn * v)
        kp.interpolation = "BEZIER"
        kp.handle_left_type = "FREE"
        kp.handle_right_type = "FREE"
    for i in range(n - 1):
        f0 = keys[i][0] * fph
        f1 = keys[i + 1][0] * fph
        df = f1 - f0
        c1, c2 = ctrl[i]
        kps[i].handle_right = (f0 + df / 3.0, sgn * c1)
        kps[i + 1].handle_left = (f0 + 2.0 * df / 3.0, sgn * c2)
    # boundary handles (cosmetic, flat)
    d0 = (kps[1].co[0] - kps[0].co[0]) / 3.0 if n > 1 else 1.0
    dn = (kps[-1].co[0] - kps[-2].co[0]) / 3.0 if n > 1 else 1.0
    kps[0].handle_left = (kps[0].co[0] - d0, kps[0].co[1])
    kps[-1].handle_right = (kps[-1].co[0] + dn, kps[-1].co[1])
    fc.extrapolation = "CONSTANT"
    fc.update()


def _fcurve_to_curve(fc, fph, negate=False):
    """Refit a Blender F-curve into the file's value-only Bezier form.

    Works for ANY edits (moved handles, added keys, linear/constant interp)
    by sampling the F-curve at the 1/3 and 2/3 points of every segment and
    solving for the two control values that reproduce those samples exactly.
    """
    sgn = -1.0 if negate else 1.0
    frames = sorted({kp.co[0] for kp in fc.keyframe_points})
    if not frames:
        return None
    if frames[0] > 1e-6:
        frames.insert(0, 0.0)  # format requires the first key at t=0
    keys = [(f / fph, sgn * fc.evaluate(f)) for f in frames]
    ctrl = []
    for i in range(len(frames) - 1):
        f0, f1 = frames[i], frames[i + 1]
        p0, p1 = keys[i][1], keys[i + 1][1]
        df = f1 - f0
        v13 = sgn * fc.evaluate(f0 + df / 3.0)
        v23 = sgn * fc.evaluate(f0 + 2.0 * df / 3.0)
        a = 27.0 * v13 - 8.0 * p0 - p1
        b = 27.0 * v23 - p0 - 8.0 * p1
        ctrl.append(((2.0 * a - b) / 18.0, (2.0 * b - a) / 18.0))
    return Curve(keys, ctrl)


def find_rig(context):
    ob = context.active_object
    if ob and RIG_TAG in ob:
        return ob
    for ob in context.scene.objects:
        if RIG_TAG in ob:
            return ob
    return None


# ============================================================================
#  Import
# ============================================================================

def do_import(context, filepath, fph, axis_convert, set_range):
    mot = MotFile.load(filepath)

    coll = bpy.data.collections.new(f"GT4 Light Motion ({mot.name})")
    context.scene.collection.children.link(coll)

    rig = bpy.data.objects.new("GT4_LightRig", None)
    rig.empty_display_type = "PLAIN_AXES"
    rig.empty_display_size = 0.5
    coll.objects.link(rig)
    rig[RIG_TAG] = 1
    rig["gt4_name"] = mot.name
    rig["gt4_duration"] = float(mot.duration)
    rig["gt4_unk_time"] = float(mot.unk_time)
    rig["gt4_frames_per_hour"] = float(fph)
    rig["gt4_axis_convert"] = 1 if axis_convert else 0
    _prop_ui(rig, "gt4_duration", (0.0, 48.0))
    _prop_ui(rig, "gt4_unk_time", (0.0, 24.0))

    # group bindings
    light_binds = {}
    named = []
    for b in mot.bindings:
        if isinstance(b, LightBinding):
            light_binds.setdefault(b.light_index, {})[b.param] = b.channels
        else:
            named.append(b)

    # ---- lights ------------------------------------------------------------
    for idx in sorted(light_binds):
        params = light_binds[idx]
        ldata = bpy.data.lights.new(f"GT4_Light{idx}", type="POINT")
        ldata.energy = 1000.0  # viewport only; not stored in the file
        obj = bpy.data.objects.new(f"GT4_Light{idx}", ldata)
        coll.objects.link(obj)
        obj.parent = rig
        obj[LIGHT_TAG] = idx

        obj_action = None
        data_action = None

        def need_obj_action():
            nonlocal obj_action
            if obj_action is None:
                obj_action = _new_action(obj, f"GT4_Light{idx}_obj")
            return obj_action

        def need_data_action():
            nonlocal data_action
            if data_action is None:
                data_action = _new_action(ldata, f"GT4_Light{idx}_data")
            return data_action

        # position -> object location (optionally Y-up -> Z-up)
        pos = params.get(PARAM_POSITION)
        if pos:
            if axis_convert:
                mapping = [(0, pos[0], False), (1, pos[2], True), (2, pos[1], False)]
            else:
                mapping = [(i, pos[i], False) for i in range(3)]
            for bl_idx, ch, neg in mapping:
                if ch.curve is not None:
                    fc = need_obj_action().fcurves.new("location", index=bl_idx)
                    _curve_to_fcurve(fc, mot.curves[ch.curve], fph, negate=neg)
                else:
                    obj.location[bl_idx] = -ch.const if neg else ch.const

        # color0 RGB -> light color (visible in viewport), A -> custom prop
        c0 = params.get(PARAM_COLOR0)
        if c0:
            for ci in range(3):
                ch = c0[ci]
                if ch.curve is not None:
                    fc = need_data_action().fcurves.new("color", index=ci)
                    _curve_to_fcurve(fc, mot.curves[ch.curve], fph)
                else:
                    ldata.color[ci] = max(0.0, ch.const)
            cha = c0[3]
            obj["gt4_color0_a"] = float(
                cha.const if cha.const is not None else mot.curves[cha.curve].keys[0][1]
            )
            _prop_ui(obj, "gt4_color0_a", (0.0, 1.0))
            if cha.curve is not None:
                fc = need_obj_action().fcurves.new('["gt4_color0_a"]')
                _curve_to_fcurve(fc, mot.curves[cha.curve], fph)

        # color1 / color2 -> custom prop float[4] arrays
        for param, pname in ((PARAM_COLOR1, "gt4_color1"), (PARAM_COLOR2, "gt4_color2")):
            cc = params.get(param)
            if not cc:
                continue
            init = [
                ch.const if ch.const is not None else mot.curves[ch.curve].keys[0][1]
                for ch in cc
            ]
            obj[pname] = [float(v) for v in init]
            _prop_ui(obj, pname, (0.0, 1.0))
            for ci, ch in enumerate(cc):
                if ch.curve is not None:
                    fc = need_obj_action().fcurves.new(f'["{pname}"]', index=ci)
                    _curve_to_fcurve(fc, mot.curves[ch.curve], fph)

        _finalize_anim(obj)
        _finalize_anim(ldata)

    # ---- named parameters ("special sliders") -------------------------------
    rig_action = None
    order = []
    for b in named:
        order.append(b.name)
        ch = b.channel
        rig[b.name] = float(
            ch.const if ch.const is not None else mot.curves[ch.curve].keys[0][1]
        )
        _prop_ui(rig, b.name, NAMED_RANGES.get(b.name, (0.0, 1.0)))
        if ch.curve is not None:
            if rig_action is None:
                rig_action = _new_action(rig, "GT4_LightRig")
            fc = rig_action.fcurves.new(f'["{b.name}"]')
            _curve_to_fcurve(fc, mot.curves[ch.curve], fph)
    rig["gt4_named_order"] = ";".join(order)
    _finalize_anim(rig)

    if set_range:
        context.scene.frame_start = 0
        context.scene.frame_end = int(round(mot.duration * fph))

    n_lights = len(light_binds)
    return mot.name, n_lights, len(named), len(mot.curves)


class GT4MOT_OT_import(bpy.types.Operator, ImportHelper):
    """Import a Gran Turismo 4 light.mot file"""
    bl_idname = "import_scene.gt4_light_mot"
    bl_label = "Import GT4 Light Motion"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".mot"
    filter_glob: StringProperty(default="*.mot", options={"HIDDEN"})

    frames_per_hour: FloatProperty(
        name="Frames per Hour",
        description="Timeline mapping: 60 means 1 frame = 1 in-game minute",
        default=60.0, min=1.0, soft_max=600.0,
    )
    axis_convert: BoolProperty(
        name="Convert Y-up to Z-up",
        description="Convert GT4's Y-up positions to Blender's Z-up",
        default=True,
    )
    set_range: BoolProperty(
        name="Set Scene Frame Range",
        description="Set the timeline to cover the whole day cycle",
        default=True,
    )

    def execute(self, context):
        try:
            name, nl, nn, nc = do_import(
                context, self.filepath, self.frames_per_hour,
                self.axis_convert, self.set_range,
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Import failed: {exc}")
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"Imported '{name}': {nl} lights, {nn} named params, {nc} curves",
        )
        return {"FINISHED"}


# ============================================================================
#  Export
# ============================================================================

def _channel_from_fcurve(fcurves, key, fallback, fph, neg=False, *, curve_pool):
    """Build a Channel from a Blender fcurve (or a constant fallback)."""
    fc = fcurves.get(key)
    if fc and len(fc.keyframe_points) >= 2:
        curve_pool.append(_fcurve_to_curve(fc, fph, negate=neg))
        return Channel(curve=len(curve_pool) - 1)
    if fc and len(fc.keyframe_points) == 1:
        v = fc.keyframe_points[0].co[1]
        return Channel(const=(-v if neg else v))
    return Channel(const=(-fallback if neg else fallback))


def do_export(context, rig, filepath):
    mot = MotFile()
    mot.name = str(rig.get("gt4_name", "bottomShadow"))
    mot.duration = float(rig.get("gt4_duration", 24.15))
    mot.unk_time = float(rig.get("gt4_unk_time", 1.0666667))
    fph = float(rig.get("gt4_frames_per_hour", 60.0))
    axis_convert = bool(rig.get("gt4_axis_convert", 1))

    pool = mot.curves  # curves get appended here as channels are built

    # ---- lights, in slot order ----------------------------------------------
    lights = sorted(
        (ob for ob in context.scene.objects if LIGHT_TAG in ob),
        key=lambda o: int(o[LIGHT_TAG]),
    )
    for obj in lights:
        idx = int(obj[LIGHT_TAG])
        ofc = _fcurve_lookup(obj)
        dfc = _fcurve_lookup(obj.data) if obj.data else {}

        # position: file (x, y, z) <- blender (x, z, -y) when converted
        if axis_convert:
            src = [(("location", 0), obj.location[0], False),
                   (("location", 2), obj.location[2], False),
                   (("location", 1), obj.location[1], True)]
        else:
            src = [(("location", i), obj.location[i], False) for i in range(3)]
        chans = [
            _channel_from_fcurve(ofc, key, fb, fph, neg, curve_pool=pool)
            for key, fb, neg in src
        ]
        mot.bindings.append(LightBinding(PARAM_POSITION, idx, chans))

        # color0: RGB from light data, A from custom prop
        ld = obj.data
        chans = [
            _channel_from_fcurve(dfc, ("color", ci), float(ld.color[ci]), fph,
                                 curve_pool=pool)
            for ci in range(3)
        ]
        chans.append(
            _channel_from_fcurve(ofc, ('["gt4_color0_a"]', 0),
                                 float(obj.get("gt4_color0_a", 1.0)), fph,
                                 curve_pool=pool)
        )
        mot.bindings.append(LightBinding(PARAM_COLOR0, idx, chans))

        # color1 / color2 from custom prop arrays
        for param, pname in ((PARAM_COLOR1, "gt4_color1"), (PARAM_COLOR2, "gt4_color2")):
            vals = list(obj.get(pname, [0.0, 0.0, 0.0, 1.0]))
            while len(vals) < 4:
                vals.append(1.0)
            chans = [
                _channel_from_fcurve(ofc, (f'["{pname}"]', ci), float(vals[ci]), fph,
                                     curve_pool=pool)
                for ci in range(4)
            ]
            mot.bindings.append(LightBinding(param, idx, chans))

    # ---- named parameters ----------------------------------------------------
    rfc = _fcurve_lookup(rig)
    order = [n for n in str(rig.get("gt4_named_order", "")).split(";") if n]
    if not order:  # fall back to whatever props look like params
        order = [k for k in rig.keys() if k in NAMED_RANGES]
    for name in order:
        ch = _channel_from_fcurve(rfc, (f'["{name}"]', 0),
                                  float(rig.get(name, 0.0)), fph, curve_pool=pool)
        mot.bindings.append(NamedBinding(name, ch))

    mot.save(filepath)
    return len(lights), len(order), len(pool), len(mot.build())


class GT4MOT_OT_export(bpy.types.Operator, ExportHelper):
    """Export the GT4 light rig back to a game-ready light.mot"""
    bl_idname = "export_scene.gt4_light_mot"
    bl_label = "Export GT4 Light Motion"

    filename_ext = ".mot"
    filter_glob: StringProperty(default="*.mot", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return find_rig(context) is not None

    def execute(self, context):
        rig = find_rig(context)
        if rig is None:
            self.report({"ERROR"}, "No GT4 light rig in the scene (import one first)")
            return {"CANCELLED"}
        try:
            nl, nn, nc, nbytes = do_export(context, rig, self.filepath)
        except Exception as exc:
            self.report({"ERROR"}, f"Export failed: {exc}")
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"Wrote {nbytes} bytes: {nl} lights, {nn} named params, {nc} curves",
        )
        return {"FINISHED"}


# ============================================================================
#  Sidebar panel ("special sliders")
# ============================================================================

class GT4MOT_PT_panel(bpy.types.Panel):
    bl_label = "GT4 Light Motion"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GT4 Light"

    def draw(self, context):
        layout = self.layout
        rig = find_rig(context)

        if rig is None:
            layout.label(text="No GT4 light rig in scene", icon="INFO")
            layout.operator(GT4MOT_OT_import.bl_idname, icon="IMPORT")
            return

        fph = float(rig.get("gt4_frames_per_hour", 60.0))
        t = context.scene.frame_current / fph
        hh = int(t) % 24
        mm = int(round((t - int(t)) * 60.0))
        if mm == 60:
            hh, mm = (hh + 1) % 24, 0
        layout.label(text=f"Time of day:  {hh:02d}:{mm:02d}", icon="TIME")
        layout.label(text=f"(frame {context.scene.frame_current}, {fph:g} frames/hour)")

        box = layout.box()
        box.label(text="Game sliders", icon="PREFERENCES")
        order = [n for n in str(rig.get("gt4_named_order", "")).split(";") if n]
        if not order:
            order = [k for k in rig.keys() if k in NAMED_RANGES]
        for name in order:
            if name in rig:
                box.prop(rig, f'["{name}"]', text=name, slider=True)
        box.label(text="Right-click a slider > Insert Keyframe", icon="KEYFRAME")

        obj = context.active_object
        if obj and LIGHT_TAG in obj:
            box = layout.box()
            box.label(text=f"Light {int(obj[LIGHT_TAG])}", icon="LIGHT")
            if obj.data:
                box.prop(obj.data, "color", text="color0 (RGB)")
            for pname, label in (("gt4_color0_a", "color0 A"),
                                 ("gt4_color1", "color1"),
                                 ("gt4_color2", "color2")):
                if pname in obj:
                    box.prop(obj, f'["{pname}"]', text=label)

        box = layout.box()
        box.label(text="File settings", icon="FILE")
        for pname in ("gt4_duration", "gt4_unk_time", "gt4_frames_per_hour"):
            if pname in rig:
                box.prop(rig, f'["{pname}"]', text=pname.replace("gt4_", ""))

        layout.operator(GT4MOT_OT_export.bl_idname, icon="EXPORT")


# NOTE: registration (menu items + register/unregister) is handled centrally by
# the package __init__.py. The two operators above stay registered so the
# sidebar panel's buttons keep working; the File > Import/Export entries are
# provided by the unified GT4 Motion Suite operators.
