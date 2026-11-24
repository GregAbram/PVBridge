"""pvlink -- helpers for natural-language PVLink authoring (SPEC-pvlink-voice.md section 7).

Runs inside the ParaView GUI: Part 1 executes this file in pvbridge's command
namespace (paraview.simple is already imported there), then calls
pvl_config(...). Everything here works on ParaView's own state; nothing is
kept elsewhere except the configuration.

A *named object* is a pipeline item the person named. It is *shown* when it
has a DMLink Sender whose display is visible (it is then in the headset).
Senders and the DMLink Domain are plumbing and are never named objects.
"""
import fnmatch
import re
import os

_PVL = {
    "institution": "GDA", "project": "voice", "dm_host": "127.0.0.1", "dm_port": 9010,
    "data_roots": [], "domain_scan": "all", "sample_every": 10, "max_find": 40,
    "dm_probe": os.path.expanduser("~/Documents/GitHub/DataManager/build/dm_probe"),
    # Save and bake (SPEC-pvlink-voice.md 10): all paths on the ParaView machine.
    "vis_dir": os.path.expanduser("~/pvbridge/visualizations"),
    "pvbatch": "/Applications/ParaView-6.1.1.app/Contents/bin/pvbatch",
    "bake_script": os.path.expanduser("~/pvbridge/voice/bake.py"),
    "dmlink_plugin": os.path.expanduser("~/Documents/GitHub/DataManager/paraview/DMLink.py"),
    "plugins": [],
}
_PLUMBING = ("DMLinkSender", "DMLinkDomain")
_BAD_NAME_CHARS = "/\\:"
_ID_LIKE = ("id", "ids", "index", "vtkoriginalpointids", "vtkoriginalcellids", "vtkoriginalindices")
_OP_NAMES = {"Contour": "contour", "Slice": "slice", "Outline": "outline", "OutlineCorners": "corners",
             "Clip": "clip", "Threshold": "threshold", "Glyph": "glyphs", "StreamTracer": "streamlines", "StreamTracerWithCustomSource": "streamlines",
             "ExtractSurface": "surface", "Calculator": "calculator", "Tube": "tubes",
             "WarpByScalar": "warp", "WarpByVector": "warp", "Delaunay2D": "mesh", "Decimate": "decimated"}


class PVLError(Exception):
    """A problem the model can act on; the message says what to do."""


# --------------------------------------------------------------------------- configuration

def pvl_config(**kw):
    unknown = set(kw) - set(_PVL)
    if unknown:
        raise PVLError("unknown settings %s; known: %s" % (sorted(unknown), sorted(_PVL)))
    _PVL.update(kw)
    dom = _domain()
    if dom is not None:        # point an existing Domain at the configured DataManager and project
        want = (_PVL["dm_host"], int(_PVL["dm_port"]), _PVL["institution"], _PVL["project"])
        have = (dom.Host, int(dom.TCPPort), dom.Institution, dom.Project)
        if want != have:
            dom.Host, dom.TCPPort, dom.Institution, dom.Project = want
            dom.UpdatePipeline(GetTimeKeeper().Time)   # DMLink reconnects and resends what is shown
    return dict(_PVL)


# --------------------------------------------------------------------------- lookups

def _view():
    return GetActiveViewOrCreate("RenderView")


def _all():
    return {k[0]: p for k, p in GetSources().items()}


def _is_plumbing(p):
    return p.GetXMLName() in _PLUMBING


# Pipeline Browser names carry the filter: "wavelet contour [Contour]". The part before
# the bracket is the object's name for voice, the model and the headset.
_TAG = re.compile(r"\s*\[[^\[\]]*\]$")


def _base(reg_name):
    return _TAG.sub("", reg_name)


def _filter_label(p):
    try:
        return p.SMProxy.GetXMLLabel() or p.GetXMLName()
    except Exception:
        return p.GetXMLName()


def _tag(p):
    """Give an object its Pipeline Browser name, "<name> [<filter>]", if it lacks one."""
    reg = _registered_name(p)
    if reg is None or _is_plumbing(p):
        return
    want = "%s [%s]" % (_base(reg), _filter_label(p))
    if reg != want:
        RenameSource(want, p)


def _hide_unused_legends(view=None):
    """Turn off colour legends whose colormap no visible display uses (ParaView can
    leave them on after the objects using them are deleted)."""
    view = view or _view()
    used = set()
    for r in view.Representations:
        if r.GetXMLName().endswith("ScalarBarWidgetRepresentation"):
            continue
        if "Visibility" in r.ListProperties() and r.Visibility and "LookupTable" in r.ListProperties() \
                and "ColorArrayName" in r.ListProperties() and r.ColorArrayName[1] and r.LookupTable is not None:
            used.add(r.LookupTable.GetGlobalIDAsString())
    hidden = []
    for b in view.Representations:
        if b.GetXMLName().endswith("ScalarBarWidgetRepresentation") and b.Visibility:
            lut = b.LookupTable
            if lut is None or lut.GetGlobalIDAsString() not in used:
                b.Visibility = 0
                hidden.append(b.Title)
    return hidden


def pvl_label_all():
    """Label every pipeline object with its filter in the Pipeline Browser (objects
    added by hand in the GUI get labelled too)."""
    for p in list(_all().values()):
        _tag(p)


def _registered_name(proxy):
    for n, p in _all().items():
        if p == proxy:
            return n
    return None


def _objects():
    return {_base(n): p for n, p in _all().items() if not _is_plumbing(p)}


def _name_of(proxy, missing=None):
    n = _registered_name(proxy)
    return _base(n) if n is not None else missing


def _label(proxy):
    """Name for display; inputs that are no longer pipeline objects are marked."""
    return _name_of(proxy, "(deleted %s)" % proxy.GetXMLName())


def _obj(name_or_proxy):
    if not isinstance(name_or_proxy, str):
        return name_or_proxy
    objs = _objects()
    if name_or_proxy in objs:
        return objs[name_or_proxy]
    low = {n.lower(): p for n, p in objs.items()}
    if name_or_proxy.lower() in low:
        return low[name_or_proxy.lower()]
    raise PVLError("no object named %r; objects are: %s" % (name_or_proxy, ", ".join(sorted(objs)) or "(none)"))


def _inputs(p):
    if "Input" not in p.ListProperties():
        return []
    i = p.Input
    if i is None:
        return []
    out = []
    for x in (i if isinstance(i, (list, tuple)) else [i]):
        out.append(getattr(x, "Proxy", x) if not hasattr(x, "GetXMLName") else x)
    return out


def _consumers(p):
    return [q for q in _all().values() if any(i == p for i in _inputs(q))]


def _sender_of(obj):
    for q in _consumers(obj):
        if q.GetXMLName() == "DMLinkSender":
            return q
    return None


def _domain():
    for p in _all().values():
        if p.GetXMLName() == "DMLinkDomain":
            return p
    return None


def _rep(p, view=None):
    from paraview import servermanager as sm
    return sm.GetRepresentation(p, view or _view())


def _shown(sender):
    r = _rep(sender)
    return bool(r is not None and r.Visibility)


def _check_name(name):
    if not name or not name.strip():
        raise PVLError("a name is needed")
    name = name.strip()
    bad = [c for c in _BAD_NAME_CHARS if c in name]
    if bad:
        raise PVLError("names can't contain %s (they become DataManager keys); try %r"
                       % (" ".join(bad), "".join("-" if c in _BAD_NAME_CHARS else c for c in name)))
    if "[" in name or "]" in name:
        raise PVLError("names can't contain [ or ] (the Pipeline Browser adds the filter in brackets)")
    if name in {_base(n) for n in _all()}:
        raise PVLError("there is already an object named %r" % name)
    return name


def _unique(base):
    names = {_base(n) for n in _all()}
    if base not in names:
        return base
    k = 2
    while "%s %d" % (base, k) in names:
        k += 1
    return "%s %d" % (base, k)


# --------------------------------------------------------------------------- data description

def _arrays(attrs):
    out = {}
    for name in attrs.keys():
        a = attrs[name]
        try:
            rng = a.GetRange(-1) if a.GetNumberOfComponents() > 1 else a.GetRange()
        except Exception:
            rng = None
        out[name] = {"components": a.GetNumberOfComponents(),
                     "range": [round(rng[0], 6), round(rng[1], 6)] if rng else None}
    return out


def _topology(p):
    """'triangles', 'lines', 'points', 'volume', 'mixed' or 'empty' for a proxy's output."""
    di = p.GetDataInformation()
    if di.GetNumberOfPoints() == 0:
        return "empty"
    cls = di.GetDataSetTypeAsString() if hasattr(di, "GetDataSetTypeAsString") else di.DataInformation.GetDataSetTypeAsString()
    if cls in ("vtkImageData", "vtkUniformGrid", "vtkRectilinearGrid", "vtkStructuredGrid", "vtkUnstructuredGrid"):
        return "volume"
    # Built-in session: the output is in this process (as DMLink also assumes).
    try:
        data = p.GetClientSideObject().GetOutputDataObject(0)
        blocks = [data]
        if hasattr(data, "NewIterator"):
            blocks, it = [], data.NewIterator()
            it.InitTraversal()
            while not it.IsDoneWithTraversal():
                blocks.append(it.GetCurrentDataObject())
                it.GoToNextItem()
        kinds = set()
        for b in blocks:
            if hasattr(b, "GetNumberOfPolys"):
                if b.GetNumberOfPolys() or b.GetNumberOfStrips():
                    kinds.add("triangles")
                if b.GetNumberOfLines():
                    kinds.add("lines")
                if b.GetNumberOfVerts():
                    kinds.add("points")
            elif b is not None and b.GetNumberOfPoints():
                kinds.add("volume")
        if kinds:
            return kinds.pop() if len(kinds) == 1 else "mixed (%s)" % ", ".join(sorted(kinds))
    except Exception:
        pass
    return "surface"


def pvl_describe(name):
    """What an object holds: type, size, bounds, arrays with ranges (current time), timesteps."""
    p = _obj(name)
    di = p.GetDataInformation()
    d = {
        "name": _name_of(p), "kind": p.GetXMLName(),
        "inputs": [_label(i) for i in _inputs(p)],
        "data": di.DataInformation.GetDataSetTypeAsString(),
        "points": di.GetNumberOfPoints(), "cells": di.GetNumberOfCells(),
        "topology": _topology(p),
        "bounds": [round(b, 6) for b in di.GetBounds()],
        "point_arrays": _arrays(p.PointData), "cell_arrays": _arrays(p.CellData),
    }
    ts = list(getattr(p, "TimestepValues", []) or [])
    if ts:
        d["timesteps"] = {"count": len(ts), "first": ts[0], "last": ts[-1]}
    s = _sender_of(p)
    d["headset"] = None if s is None else {"item": s.ItemName, "shown": _shown(s), "colour_by": _colour_var(s)}
    return d


def _default_colour_var(p):
    for attrs in (p.PointData, p.CellData):
        for name in attrs.keys():
            if name.lower() in _ID_LIKE or name.lower().endswith("id") or name == "Normals":
                continue
            if attrs[name].GetNumberOfComponents() == 1:
                return name
    return None


def _colour_var(sender):
    r = _rep(sender)
    if r is None:
        return None
    arr = r.ColorArrayName
    return arr[1] if arr and len(arr) > 1 and arr[1] else None


# --------------------------------------------------------------------------- domain

def _valid(b):
    return b and b[0] <= b[1] and b[2] <= b[3] and b[4] <= b[5]


def _union(a, b):
    if not _valid(a):
        return list(b)
    if not _valid(b):
        return list(a)
    return [min(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3]), min(a[4], b[4]), max(a[5], b[5])]


def _source_bounds(p):
    """Bounds of a source object, over all its timesteps for a time series (7.3)."""
    ts = list(getattr(p, "TimestepValues", []) or [])
    if len(ts) < 2:
        p.UpdatePipeline()
        return list(p.GetDataInformation().GetBounds())
    if _PVL["domain_scan"] == "sample":
        step = max(1, int(_PVL["sample_every"]))
        ts = ts[::step] + ([ts[-1]] if (len(ts) - 1) % step else [])
    b = None
    t_now = GetTimeKeeper().Time
    for t in ts:
        p.UpdatePipeline(t)
        b = _union(b, list(p.GetDataInformation().GetBounds()))
    p.UpdatePipeline(t_now)
    return b


def _ensure_domain(first_source):
    """The session's DMLink Domain, configured from pvl_config; created on first use."""
    dom = _domain()
    if dom is None:
        dom = DMLinkDomain(registrationName="DMLink Domain", Input=first_source)
        dom.Host = _PVL["dm_host"]
        dom.TCPPort = int(_PVL["dm_port"])
        dom.Institution = _PVL["institution"]
        dom.Project = _PVL["project"]
        dom.OverrideBounds = 1
        dom.XRange = dom.YRange = dom.ZRange = [1.0, -1.0]   # empty: grows from the first source
        Hide(dom, _view())
    return dom


def _grow_domain(source):
    dom = _ensure_domain(source)
    cur = list(dom.XRange) + list(dom.YRange) + list(dom.ZRange)
    new = _union(cur, _source_bounds(source))
    if new != cur:
        dom.XRange, dom.YRange, dom.ZRange = new[0:2], new[2:4], new[4:6]
    dom.UpdatePipeline(GetTimeKeeper().Time)   # configures DMLink and sends the bounds
    return new


def pvl_reset_domain():
    """Recompute the domain from the current source objects only (it otherwise only grows)."""
    dom = _domain()
    if dom is None:
        return None
    b = None
    for p in _objects().values():
        if not _inputs(p) and p.GetDataInformation().GetNumberOfPoints() > 0:
            b = _union(b, _source_bounds(p))
    if b:
        dom.XRange, dom.YRange, dom.ZRange = b[0:2], b[2:4], b[4:6]
        dom.UpdatePipeline(GetTimeKeeper().Time)
    return b


# --------------------------------------------------------------------------- showing and colouring

def _displays_of(p, view):
    """Every display of p in the view (normally one)."""
    out = []
    for r in view.Representations:
        if "Input" not in r.ListProperties() or r.Input is None:
            continue
        i = r.Input
        i = getattr(i, "Proxy", i) if not hasattr(i, "GetGlobalIDAsString") else i
        if i.GetGlobalIDAsString() == p.GetGlobalIDAsString():
            out.append(r)
    return out


def _drop_extra_displays(p, view):
    """Keep only the display ParaView's GUI and DMLink both use (the first one
    GetRepresentation finds); delete any others. Returns how many were deleted."""
    from paraview import servermanager as sm
    keep = sm.GetRepresentation(p, view)
    extra = [r for r in _displays_of(p, view) if keep is None or r.GetGlobalIDAsString() != keep.GetGlobalIDAsString()]
    for r in extra:
        r.Visibility = 0
        view.Representations = [x for x in view.Representations if x.GetGlobalIDAsString() != r.GetGlobalIDAsString()]
        try:
            sm.UnRegister(r)
        except Exception:
            pass
    return len(extra)


def _match_display(obj, sender, view):
    """Make the Sender's display in ParaView look like what DMLink sends: a volume goes
    to the headset as its outer surface, but ParaView draws volumes as an outline by
    default, so the two would differ."""
    if _topology(obj) == "volume":
        d = GetDisplayProperties(sender, view)
        if "Representation" in d.ListProperties() and d.Representation != "Surface":
            d.SetRepresentationType("Surface")


def pvl_show(name, colour_by=None):
    """Put an object in the headset: a Static DMLink Sender named after it, shown; its own
    display hidden so the ParaView view mirrors the headset."""
    p = _obj(name)
    oname = _name_of(p)
    view = _view()
    if _domain() is None:
        root = p
        while _inputs(root):
            root = _inputs(root)[0]
        _grow_domain(root)
    s = _sender_of(p)
    new = s is None
    if new:
        s = DMLinkSender(registrationName=_unique(oname + " (headset)"), Input=p)
        s.ItemName = oname
        s.Time = "Static"
        # Run the Sender once before Show(): on its first run DMLink looks up its
        # display with GetDisplayProperties, which creates one if none exists. If
        # that happens inside Show(), the Sender ends up with two displays: the
        # GUI's eye icon controls one, DMLink follows the other.
        s.UpdatePipeline(GetTimeKeeper().Time)
    Show(s, view)
    Hide(p, view)
    _drop_extra_displays(s, view)
    _match_display(p, s, view)
    if colour_by is not None or new:
        var = colour_by if colour_by is not None else _default_colour_var(p)
        pvl_colour(oname, var=var or "")
    SetActiveSource(p)
    return "%s is in the headset%s" % (oname, (", coloured by %s" % _colour_var(s)) if _colour_var(s) else "")


def pvl_hide(name):
    """Hide in the headset but keep it in the DataManager (quick to show again)."""
    p = _obj(name)
    s = _sender_of(p)
    if s is None:
        return "%s is not in the headset" % _name_of(p)
    Hide(s, _view())
    _hide_unused_legends()
    return "%s hidden" % _name_of(p)


def pvl_remove(name):
    """Take out of the headset and the DataManager. The ParaView object stays."""
    p = _obj(name)
    s = _sender_of(p)
    if s is None:
        return "%s was not in the headset" % _name_of(p)
    Delete(s)
    _hide_unused_legends()
    SetActiveSource(p)
    return "%s removed from the headset" % _name_of(p)


def pvl_colour(name, var=None, preset=None, log=None, range=None):
    """Colour a shown object (its Sender's display, which is what DMLink sends).
    var: array name, "" for solid colour, None to keep. range: [lo, hi] or "data" to rescale.
    Colormaps are shared per variable: changing one changes it for every object coloured by it."""
    p = _obj(name)
    s = _sender_of(p)
    if s is None:
        raise PVLError("%s is not in the headset; show it first (pvl_show)" % _name_of(p))
    d = GetDisplayProperties(s, _view())
    notes = []
    if var is not None:
        if var == "":
            d.ColorArrayName = ["POINTS", ""]
        else:
            if var in p.PointData.keys():
                assoc, comps = "POINTS", p.PointData[var].GetNumberOfComponents()
            elif var in p.CellData.keys():
                assoc, comps = "CELLS", p.CellData[var].GetNumberOfComponents()
            else:
                raise PVLError("%s has no variable %r; it has: %s" % (
                    _name_of(p), var, ", ".join(list(p.PointData.keys()) + list(p.CellData.keys()))))
            ColorBy(d, (assoc, var, "Magnitude") if comps > 1 else (assoc, var))
            if range is None:
                range = "data"
    cur = _colour_var(s)
    if cur is None:
        return "%s is a solid colour" % _name_of(p)
    lut = GetColorTransferFunction(cur)
    if preset:
        if not lut.ApplyPreset(preset, True):
            raise PVLError("no colormap preset %r (try Viridis, Inferno, Plasma, Cool to Warm, Turbo, Jet, Grayscale)" % preset)
    if range == "data":
        # The object's own range at the current time (the Sender may not have run yet).
        p.UpdatePipeline(GetTimeKeeper().Time)
        attrs = p.PointData if cur in p.PointData.keys() else p.CellData
        a = attrs[cur]
        lo, hi = a.GetRange(-1) if a.GetNumberOfComponents() > 1 else a.GetRange()
        if hi <= lo:
            hi = lo + 1e-6
        lut.RescaleTransferFunction(lo, hi)
    elif range is not None:
        lut.RescaleTransferFunction(float(range[0]), float(range[1]))
    if range is not None:
        lut.AutomaticRescaleRangeMode = "Never"
    if log is not None:
        if log:
            lo, hi = lut.RGBPoints[0], lut.RGBPoints[-4]
            if lo <= 0:
                lo = hi * 1e-4 if hi > 0 else 1e-6
                lut.RescaleTransferFunction(lo, hi)
                notes.append("log scale needs a positive minimum; using %g" % lo)
        lut.UseLogScale = 1 if log else 0
    others = [n for n, q in _objects().items() if q != p and _sender_of(q) is not None and _colour_var(_sender_of(q)) == cur]
    if others and (preset or log is not None or range not in (None, "data")):
        notes.append("the %s colormap is shared, so this also changes %s" % (cur, ", ".join(others)))
    lo, hi = lut.RGBPoints[0], lut.RGBPoints[-4]
    return "%s coloured by %s (%g to %g%s)%s" % (_name_of(p), cur, lo, hi, ", log" if lut.UseLogScale else "",
                                                  "; " + "; ".join(notes) if notes else "")


# --------------------------------------------------------------------------- creating

def _active_name():
    a = GetActiveSource()
    if a is None:
        return None
    if _is_plumbing(a):
        ins = _inputs(a)
        return _name_of(ins[0]) if ins else None
    return _name_of(a)


def pvl_create(kind_or_path, name):
    """A new named object: a ParaView source by name ("Wavelet", "Sphere") or a data file path.
    Shown in the headset as its native geometry; extends the domain."""
    name = _check_name(name)
    if os.path.sep in kind_or_path or os.path.splitext(kind_or_path)[1]:
        path = os.path.expanduser(kind_or_path)
        if not os.path.exists(path):
            raise PVLError("no file %s (pvl_find_data can search the data folders)" % path)
        p = OpenDataFile(path, registrationName=name)
        if p is None:
            raise PVLError("ParaView has no reader for %s" % path)
        _tag(p)
    else:
        import paraview.simple as _simple
        ctor = getattr(_simple, kind_or_path, None)
        if ctor is None or not callable(ctor):
            raise PVLError("ParaView has no source called %r" % kind_or_path)
        p = ctor(registrationName=name)
        _tag(p)
    p.UpdatePipeline()
    if list(getattr(p, "TimestepValues", []) or []):
        GetAnimationScene().UpdateAnimationUsingDataTimeSteps()
    _grow_domain(p)
    return pvl_show(name)


def pvl_add_variable(name, expression, var_name, attribute="Point Data"):
    """Add a variable to an object with a Calculator expression (e.g. "coordsX").
    The object keeps its name (the name follows the data); its previous version is
    renamed "<name> source"; its headset item gains the variable and is coloured by it."""
    p = _obj(name)
    oname = _name_of(p)
    if not var_name or any(c in var_name for c in _BAD_NAME_CHARS):
        raise PVLError("bad variable name %r" % var_name)
    # Redefining a variable this object already got from a Calculator: change that
    # Calculator's expression rather than adding another one on top.
    q = p
    while q is not None and q.GetXMLName() == "Calculator":
        if q.ResultArrayName == var_name:
            q.Function = expression
            p.UpdatePipeline(GetTimeKeeper().Time)
            if var_name not in p.PointData.keys() and var_name not in p.CellData.keys():
                raise PVLError("the expression %r produced no variable; check it" % expression)
            SetActiveSource(p)
            if _sender_of(p) is not None:
                return pvl_colour(oname, var=var_name) + " (%s redefined as %s)" % (var_name, expression)
            return "%s redefined as %s" % (var_name, expression)
        ins = _inputs(q)
        q = ins[0] if ins else None
    s = _sender_of(p)
    RenameSource(_unique(oname + " source"), p)
    _tag(p)
    calc = Calculator(registrationName=oname, Input=p)
    _tag(calc)
    calc.ResultArrayName = var_name
    calc.Function = expression
    if "AttributeType" in calc.ListProperties():
        calc.AttributeType = attribute
    calc.UpdatePipeline()
    if var_name not in calc.PointData.keys() and var_name not in calc.CellData.keys():
        raise PVLError("the expression %r produced no variable; check it" % expression)
    if s is not None:
        s.Input = calc
        Hide(p, _view())
        out = pvl_colour(oname, var=var_name)
    else:
        out = "%s now has %s (not in the headset)" % (oname, var_name)
    SetActiveSource(calc)
    return out


def pvl_derive(input_name, filter, name=None, replace=True, **props):
    """New geometry from an object: filter is a ParaView filter name ("Contour", "Slice",
    "Outline", "Clip", "Threshold", ...), props its properties. Named "<input> <operation>"
    unless given. With replace=True its input leaves the headset ("add"/"also" requests:
    replace=False). Coloured like its input when that variable survives."""
    import paraview.simple as _simple
    src = _obj(input_name)
    sname = _name_of(src)
    ctor = getattr(_simple, filter, None)
    if ctor is None or not callable(ctor):
        raise PVLError("ParaView has no filter called %r" % filter)
    name = _check_name(name) if name else _unique("%s %s" % (sname, _OP_NAMES.get(filter, filter.lower())))
    f = ctor(registrationName=name, Input=src)
    _tag(f)
    for k, v in props.items():
        try:
            setattr(f, k, v)
        except AttributeError:
            Delete(f)
            raise PVLError("%s has no property %r; it has: %s" % (filter, k, ", ".join(f.ListProperties())))
    f.UpdatePipeline()
    if f.GetDataInformation().GetNumberOfPoints() == 0:
        note = " (it is empty: nothing to show with these settings)"
    else:
        note = ""
    ssrc = _sender_of(src)
    inherit = _colour_var(ssrc) if ssrc is not None else None
    if inherit and inherit not in f.PointData.keys() and inherit not in f.CellData.keys():
        inherit = None
    out = pvl_show(name, colour_by=inherit)
    if replace and ssrc is not None:
        pvl_remove(sname)
        out += "; %s removed from the headset" % sname
    SetActiveSource(f)
    return out + note

_MANY_SEEDS = 20000


def pvl_streamlines(field_name, seeds_name, vectors=None, name=None, replace=False, **props):
    """Streamlines through a vector field, seeded from the points of another object
    (StreamTracerWithCustomSource). vectors: the vector variable; found by itself when the
    field has only one. Integrates both ways, up to the field's diagonal, unless props say
    otherwise (IntegrationDirection, MaximumStreamlineLength, ...). Named "<field> streamlines"
    unless given. replace=False keeps the field in the headset; the seed object is left as is."""
    import paraview.simple as _simple
    field = _obj(field_name)
    seeds = _obj(seeds_name)
    fname, sname = _name_of(field), _name_of(seeds)
    if field is seeds:
        raise PVLError("the seeds have to come from a different object than the field %s "
                       "(a slice, a line, a point cloud, ...)" % fname)
    vecs = [("POINTS", k) for k in field.PointData.keys() if field.PointData[k].GetNumberOfComponents() == 3]
    vecs += [("CELLS", k) for k in field.CellData.keys() if field.CellData[k].GetNumberOfComponents() == 3]
    vecs = [v for v in vecs if v[1].lower() not in ("normals", "texturecoordinates")]
    if vectors is None:
        if not vecs:
            raise PVLError("%s has no vector variables to trace; make one first (e.g. a gradient, "
                           "or a Calculator combining three components)" % fname)
        if len(vecs) > 1:
            raise PVLError("%s has several vector variables: %s; say which one to trace" % (
                fname, ", ".join(v[1] for v in vecs)))
        assoc, vectors = vecs[0]
    else:
        match = [v for v in vecs if v[1] == vectors] or [v for v in vecs if v[1].lower() == vectors.lower()]
        if not match:
            raise PVLError("%s has no vector variable %r; its vectors: %s" % (
                fname, vectors, ", ".join(v[1] for v in vecs) or "none"))
        assoc, vectors = match[0]
    n_seeds = seeds.GetDataInformation().GetNumberOfPoints()
    if n_seeds == 0:
        raise PVLError("%s has no points to seed from" % sname)
    if n_seeds > _MANY_SEEDS:
        raise PVLError("%s has %d points; that many streamlines would take very long. Seed from "
                       "something smaller (a coarser slice, a line, or a sample of points)" % (sname, n_seeds))
    name = _check_name(name) if name else _unique("%s streamlines" % fname)
    f = _simple.StreamTracerWithCustomSource(registrationName=name, Input=field, SeedSource=seeds)
    _tag(f)
    f.Vectors = [assoc, vectors]
    f.IntegrationDirection = "BOTH"
    b = field.GetDataInformation().GetBounds()
    diag = ((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2 + (b[5] - b[4]) ** 2) ** 0.5
    if diag > 0:
        f.MaximumStreamlineLength = diag
    for k, v in props.items():
        try:
            setattr(f, k, v)
        except AttributeError:
            Delete(f)
            raise PVLError("StreamTracerWithCustomSource has no property %r; it has: %s" % (k, ", ".join(f.ListProperties())))
    f.UpdatePipeline()
    n_pts = f.GetDataInformation().GetNumberOfPoints()
    out = pvl_show(name, colour_by=vectors if (vectors in f.PointData.keys()) else None)
    out += " (traced along %s from %d seed points of %s)" % (vectors, n_seeds, sname)
    if n_pts == 0:
        out += "; they are empty: the seeds may lie outside the field, or the flow is zero there"
    ssrc = _sender_of(field)
    if replace and ssrc is not None:
        pvl_remove(fname)
        out += "; %s removed from the headset" % fname
    SetActiveSource(f)
    return out


def pvl_set(name, **props):
    """Change properties of an object (e.g. Isosurfaces=[150], or SliceType.Normal via
    slice_normal=[1,0,0], slice_origin=[0,0,0])."""
    p = _obj(name)
    for k, v in props.items():
        if k in ("slice_normal", "slice_origin", "clip_normal", "clip_origin"):
            sub = p.SliceType if k.startswith("slice") else p.ClipType
            setattr(sub, "Normal" if k.endswith("normal") else "Origin", v)
            continue
        if k not in p.ListProperties():
            raise PVLError("%s has no property %r; it has: %s" % (_name_of(p), k, ", ".join(p.ListProperties())))
        setattr(p, k, v)
    p.UpdatePipeline()
    SetActiveSource(p)
    return "%s updated" % _name_of(p)


def pvl_rename(name, new_name):
    p = _obj(name)
    new_name = _check_name(new_name)
    old = _name_of(p)
    RenameSource(new_name, p)
    _tag(p)
    s = _sender_of(p)
    if s is not None:
        s.ItemName = new_name     # the headset item is renamed too (old one removed)
        RenameSource(_unique(new_name + " (headset)"), s)
    return "%s is now called %s" % (old, new_name)


def pvl_clear_headset():
    """Start over, step 1: take everything out of the headset (the pipeline stays)."""
    gone = []
    for n, p in list(_objects().items()):
        if _sender_of(p) is not None:
            pvl_remove(n)
            gone.append(n)
    try:
        Render(_view())                     # DMLink sends the removals
        dm = pvl_clear_project()
    except Exception as e:
        dm = "could not empty the DataManager project: %s" % e
    return "removed from the headset: %s; %s" % (", ".join(gone) or "nothing", dm)


def _value_range(p, prop):
    """(lo, hi) a setting can usefully take on object p: a contour's variable range on
    its input, or a slice/clip offset's span of the input's box along the plane normal."""
    import itertools
    ins = _inputs(p)
    src = ins[0] if ins else p
    if prop in ("Isosurfaces", "ContourValues"):
        var = p.ContourBy[1] if len(p.ContourBy) > 1 else p.ContourBy[0]
        attrs = src.PointData if var in src.PointData.keys() else src.CellData
        lo, hi = attrs[var].GetRange()
        return lo, hi
    if prop in ("SliceOffsetValues", "OffsetValues"):
        plane = p.SliceType if "SliceType" in p.ListProperties() else p.ClipType
        n, o = list(plane.Normal), list(plane.Origin)
        b = src.GetDataInformation().GetBounds()
        # ParaView puts the plane through origin + offset * normal, with the normal as
        # given (not normalized), so the offset of a box corner c is n.(c-o) / |n|^2.
        nn = sum(x * x for x in n) or 1.0
        vals = [sum(n[a] * (c[a] - o[a]) for a in range(3)) / nn
                for c in itertools.product(b[0:2], b[2:4], b[4:6])]
        return min(vals), max(vals)
    raise PVLError("I don't know the range of %s.%s; give the values" % (_name_of(p), prop))


def _track(name, prop, values, steps):
    p = _obj(name)
    if prop not in p.ListProperties():
        raise PVLError("%s has no property %r; it has: %s" % (_name_of(p), prop, ", ".join(p.ListProperties())))
    if isinstance(values, str):
        import re as _re
        m = _re.match(r"^\s*(-?[\d.]+(?:e-?\d+)?)\s*(?:to|\.\.|-|:)\s*(-?[\d.]+(?:e-?\d+)?)\s*$", values)
        if values.strip() == "range":
            lo, hi = _value_range(p, prop)
        elif m:
            lo, hi = float(m.group(1)), float(m.group(2))
        else:
            raise PVLError('values must be a list, "range", or "lo to hi" (e.g. "60 to 260")')
        n = max(2, int(steps))
        values = [lo + (hi - lo) * k / (n - 1) for k in range(n)]
    values = list(values)
    if not values:
        raise PVLError("no values to animate for %s.%s" % (_name_of(p), prop))
    old = getattr(p, prop)
    old = list(old) if hasattr(old, "__len__") and not isinstance(old, str) else old
    return {"p": p, "name": _name_of(p), "prop": prop, "values": values, "old": old, "list": isinstance(old, list)}


def _apply(tr, v):
    setattr(tr["p"], tr["prop"], [v] if tr["list"] and not isinstance(v, (list, tuple)) else v)
    tr["p"].UpdatePipeline(GetTimeKeeper().Time)


def pvl_animate(tracks, steps=10, seconds_per_step=0.5, loops=1, end="last", job="animation"):
    """Animate one or more settings together, a step at a time in the background (the GUI
    repaints after each step; the headset gets every step). tracks: list of
    (object_name, property, values), where values is a list, "range" (the setting's
    useful range on that object) or "lo to hi", the last two split into `steps` values. Tracks with fewer values
    hold their last one. end: "last", "first" or "original". Returns at once;
    pvl_stop_animation() stops it."""
    trs = [_track(n, pr, v, steps) for (n, pr, v) in tracks]
    if not trs:
        raise PVLError("nothing to animate")
    loops = max(1, int(loops))
    step = max(0.05, float(seconds_per_step))
    count = max(len(t["values"]) for t in trs)
    total = count * loops * step
    if total > 600:
        raise PVLError("that animation would take over 10 minutes; use fewer steps or loops")

    def frame(k):
        for t in trs:
            _apply(t, t["values"][min(k, len(t["values"]) - 1)])

    def run():
        try:
            for loop in range(loops):
                for k in range(count):
                    if loop == 0 and k == 0:
                        yield step        # frame 0 was applied before returning
                        continue
                    frame(k)
                    yield step
            for t in trs:
                if end == "first":
                    _apply(t, t["values"][0])
                elif end == "original":
                    setattr(t["p"], t["prop"], t["old"])
                    t["p"].UpdatePipeline(GetTimeKeeper().Time)
            yield 0
        except Exception:
            pass   # an object went away while animating

    frame(0)
    Render(_view())
    bridge.add_job(job, run())
    SetActiveSource(trs[0]["p"])
    desc = "; ".join("%s %s from %.4g to %.4g" % (t["name"], t["prop"], t["values"][0], t["values"][-1]) for t in trs)
    return "animating %s in %d steps, %d loop%s, about %.0f s; it runs in the background" % (
        desc, count, loops, "" if loops == 1 else "s", total)


def pvl_sweep(name, prop, values, seconds_per_step=0.5, loops=1, end="last", steps=10):
    """One setting: pvl_animate([(name, prop, values)], ...). values may be "range" or
    "lo to hi". Each setting gets its own job, so sweeps of different settings run side
    by side; sweeping the same setting again replaces its previous sweep."""
    return pvl_animate([(name, prop, values)], steps=steps, seconds_per_step=seconds_per_step, loops=loops,
                       end=end, job="sweep:%s:%s" % (_name_of(_obj(name)), prop))


# Stored animation ranges: kept in ParaView as annotations on the objects
# ("pvl.anim.<property>" = values), so they are part of the GUI's state and of
# saved state files.
_ANIM_KEY = "pvl.anim."


def pvl_set_animation_range(name, prop, values):
    """Remember the range to animate `prop` of `name` over: "lo to hi", "range", or a
    list. pvl_run_animation() animates every remembered range together."""
    import json
    tr = _track(name, prop, values, 10)          # validates the object, property and values
    text = values if isinstance(values, str) else json.dumps(list(values))
    tr["p"].SMProxy.SetAnnotation(_ANIM_KEY + prop, text)
    return "%s %s will animate over %s" % (tr["name"], prop, text)


def pvl_animation_ranges():
    """[(object, property, values)] remembered by pvl_set_animation_range."""
    import json
    out = []
    for n, p in sorted(_objects().items()):
        sp = p.SMProxy
        for i in range(sp.GetNumberOfAnnotations()):
            k = sp.GetAnnotationKeyAt(i)
            if k.startswith(_ANIM_KEY):
                v = sp.GetAnnotation(k)
                if v.strip().startswith("["):
                    v = json.loads(v)
                out.append((n, k[len(_ANIM_KEY):], v))
    return out


def pvl_clear_animation_range(name=None, prop=None):
    """Forget remembered ranges: all, all of one object, or one setting."""
    gone = []
    for n, pr, _ in pvl_animation_ranges():
        if (name is None or n == _name_of(_obj(name))) and (prop is None or pr == prop):
            _obj(n).SMProxy.RemoveAnnotation(_ANIM_KEY + pr)
            gone.append("%s %s" % (n, pr))
    return "forgot the animation range of %s" % (", ".join(gone) or "nothing")


def pvl_run_animation(steps=10, seconds_per_step=0.5, loops=1, end="last"):
    """Animate every remembered range together, in `steps` steps."""
    tracks = pvl_animation_ranges()
    if not tracks:
        raise PVLError("no animation ranges are set; use pvl_set_animation_range first")
    return pvl_animate(tracks, steps=steps, seconds_per_step=seconds_per_step, loops=loops, end=end)


def pvl_stop_animation():
    """Stop a running animation where it is."""
    stopped = []
    for j in bridge.jobs():
        if j == "animation" or j.startswith("sweep"):
            stopped += bridge.stop_job(j)
    return "stopped the animation" if stopped else "no animation was running"

def _dmlink_manager():
    import sys as _sys
    for m in list(_sys.modules.values()):
        if getattr(m, "DMLinkManager", None) is not None and hasattr(m, "get_manager"):
            return m.get_manager()
    return None


def _project_keys(project=None, host=None, port=None):
    """Every item key the DataManager holds for a project (default: the scratch
    project), via dm_probe. Includes items left by earlier ParaView sessions."""
    import subprocess as _sp
    project = project or "%s/%s" % (_PVL["institution"], _PVL["project"])
    host, port = host or _PVL["dm_host"], int(port or _PVL["dm_port"])
    if not os.path.exists(_PVL["dm_probe"]):
        raise PVLError("dm_probe not found at %s; set pvl_config(dm_probe=...)" % _PVL["dm_probe"])
    out = _sp.run([_PVL["dm_probe"], "--host", host, "--port", str(port),
                   "--state", project.replace("/", "-"), "--project", project,
                   "--seconds", "1.5"], capture_output=True, text=True, timeout=20).stdout
    keys = set()
    prefix = project + "/"
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("MANIFEST"):
            for k in line.split(":", 1)[1].split(" " + prefix):
                k = k.strip()
                if k.startswith(prefix):
                    k = k[len(prefix):]
                if k:
                    keys.add(k)
    return sorted(keys)


def _wipe(src, keys):
    """Remove keys over a DMSource connection. Each is first rewritten as untimed,
    which drops any timeline it has (SPEC.md 5.8), then removed. Returns (removed, failed)."""
    import struct as _struct
    removed, failed = [], []
    for k in keys:
        try:
            src.put(k, {}, _struct.pack("<6f", 0, 0, 0, 0, 0, 0) if k.startswith("Bounds/") else b"")
            src.remove_untimed(k)
            removed.append(k)
        except Exception as e:
            failed.append("%s (%s)" % (k, e))
    return removed, failed


def pvl_clear_project():
    """Empty the scratch project in the DataManager, including items left by earlier
    ParaView sessions, timed or untimed."""
    mgr = _dmlink_manager()
    if mgr is None or not mgr.project:
        return "the DataManager project isn't connected yet; nothing to clear"
    keys = _project_keys()
    if not keys:
        return "the DataManager project is already empty"
    if mgr._src is None and not mgr._connect():
        raise PVLError("can't reach the DataManager at %s:%s" % (mgr.host, mgr.port))
    removed, failed = _wipe(mgr._src, keys)
    for attr in ("_sent", "_sent_untimed"):
        getattr(mgr, attr).clear()
    for attr in ("_last", "_last_untimed", "_untimed", "_staged"):
        getattr(mgr, attr).clear()
    left = _project_keys()
    msg = "emptied the DataManager project (%d items removed)" % len(removed)
    if failed or left:
        msg += "; could not remove %s" % ", ".join(failed + left)
    return msg


# --------------------------------------------------------------------------- save and bake

_BAKES = {}   # name -> subprocess, for bakes started in this ParaView session


def _vis_name(name):
    name = (name or "").strip()
    bad = [c for c in '/\\:*?"<>|' if c in name]
    if not name or bad or name.startswith(".") or name.endswith("."):
        raise PVLError("a visualization name can't be empty, start or end with a dot, or contain %s"
                       % " ".join('/\\:*?"<>|'))
    return name


def pvl_save(name):
    """Save the current visualization (the whole ParaView state, data paths absolute)
    as <vis_dir>/<name>.pvsm, with a <name>.json note of what was in the headset."""
    import json
    import time as _time
    name = _vis_name(name)
    os.makedirs(_PVL["vis_dir"], exist_ok=True)
    path = os.path.join(_PVL["vis_dir"], name + ".pvsm")
    SaveState(path)
    note = {"name": name, "saved": _time.strftime("%Y-%m-%d %H:%M:%S"), "state": path,
            "items": pvl_items(), "animation_ranges": pvl_animation_ranges(),
            "timesteps": len(GetTimeKeeper().TimestepValues or []), "plugins": _PVL["plugins"]}
    with open(os.path.join(_PVL["vis_dir"], name + ".json"), "w") as f:
        json.dump(note, f, indent=1, default=str)
    shown = [i["name"] for i in note["items"] if i["shown"]]
    return "saved the visualization %r (%s) to %s" % (name, ", ".join(shown) or "nothing shown", path)


def pvl_bake(name, steps=10, times="all", stride=1, replace=False, save=True):
    """Bake a visualization into its own DataManager project <institution>/<name>, every
    frame timed, so headsets can play it by themselves (state "<institution>-<name>").
    Saves the current state as <name> first (save=False bakes an earlier save). Frames:
    the data's timesteps (times "t0:t1", every `stride`th), or `steps` frames when the data
    has none; stored animation ranges are spread over the frames. Runs in the background;
    pvl_bake_status(name) reports progress. replace=True overwrites an earlier bake of
    the same name (otherwise that is an error)."""
    import subprocess as _sp
    name = _vis_name(name)
    if name in _BAKES and _BAKES[name].poll() is None:
        raise PVLError("a bake of %r is already running" % name)
    if save:
        pvl_save(name)
    state = os.path.join(_PVL["vis_dir"], name + ".pvsm")
    if not os.path.exists(state):
        raise PVLError("no saved visualization %r (expected %s)" % (name, state))
    project = "%s/%s" % (_PVL["institution"], name)
    if project == "%s/%s" % (_PVL["institution"], _PVL["project"]):
        raise PVLError("a bake can't use the scratch project's name, %r" % _PVL["project"])
    if not replace and _project_keys(project):
        raise PVLError("there is already a bake called %r; say to replace it, or choose another name" % name)
    for path, what in ((_PVL["pvbatch"], "pvbatch"), (_PVL["bake_script"], "bake.py")):
        if not os.path.exists(path):
            raise PVLError("%s not found at %s" % (what, path))
    prog = os.path.join(_PVL["vis_dir"], name + ".bake.json")
    log = os.path.join(_PVL["vis_dir"], name + ".bake.log")
    for f in (prog,):
        if os.path.exists(f):
            os.remove(f)
    cmd = [_PVL["pvbatch"], _PVL["bake_script"], state, "--project", name,
           "--institution", _PVL["institution"], "--host", _PVL["dm_host"], "--port", str(_PVL["dm_port"]),
           "--steps", str(int(steps)), "--times", str(times), "--stride", str(int(stride)),
           "--dmlink", _PVL["dmlink_plugin"], "--dm-probe", _PVL["dm_probe"], "--progress", prog]
    for p in _PVL["plugins"]:
        cmd += ["--plugin", p]
    if replace:
        cmd.append("--replace")
    with open(log, "w") as lf:
        _BAKES[name] = _sp.Popen(cmd, stdout=lf, stderr=_sp.STDOUT, stdin=_sp.DEVNULL, start_new_session=True)
    n = len(GetTimeKeeper().TimestepValues or []) or int(steps)
    return ("baking %r into DataManager project %s (state %s-%s), about %d frames, in the background; "
            "pvl_bake_status(%r) reports progress" % (name, project, _PVL["institution"], name, n, name))


def pvl_bake_status(name=None):
    """Progress of a bake: running (frame k of n), done, or failed with the reason.
    With no name: every bake started in this session."""
    import json
    names = [_vis_name(name)] if name else sorted(_BAKES)
    if not names:
        return "no bakes have been started"
    out = []
    for n in names:
        prog = os.path.join(_PVL["vis_dir"], n + ".bake.json")
        st = None
        if os.path.exists(prog):
            try:
                st = json.load(open(prog))
            except Exception:
                st = None
        proc = _BAKES.get(n)
        alive = proc is not None and proc.poll() is None
        if st is None:
            out.append("%s: %s" % (n, "starting" if alive else "no progress recorded"))
        elif st.get("state") == "done":
            out.append("%s: done, %s, in %s s" % (n, st.get("message", ""), st.get("elapsed")))
        elif st.get("state") == "failed" or (not alive and proc is not None and proc.poll() != 0):
            tail = ""
            log = os.path.join(_PVL["vis_dir"], n + ".bake.log")
            if os.path.exists(log):
                tail = " | log: " + " / ".join(open(log).read().strip().splitlines()[-3:])
            out.append("%s: failed: %s%s" % (n, st.get("message", "exited early"), tail))
        else:
            out.append("%s: %s frame %s of %s (t=%s), %s s so far" % (
                n, st.get("state"), st.get("frame", 0), st.get("frames", "?"), st.get("t", "-"), st.get("elapsed")))
    return "; ".join(out)


def pvl_delete_all():
    """Start over completely: empty the headset, then delete every pipeline object
    (end of the pipeline first). Uses ParaView's Delete, which also removes animation
    tracks on those objects. The render view stays, so pvbridge keeps running."""
    pvl_clear_headset()
    deleted = []
    for _ in range(100):
        srcs = _all()
        if not srcs:
            break
        used = set()
        for p in srcs.values():
            for i in _inputs(p):
                used.add(i.GetGlobalIDAsString())
        leaves = [(n, p) for n, p in srcs.items() if p.GetGlobalIDAsString() not in used]
        for n, p in leaves:
            Delete(p)
            if not _is_plumbing(p):
                deleted.append(n)
    left = list(_all())
    _hide_unused_legends()
    try:
        GetAnimationScene().AnimationTime = GetAnimationScene().StartTime
    except Exception:
        pass
    try:
        dm = pvl_clear_project()
    except Exception as e:
        dm = "could not empty the DataManager project: %s" % e
    return "deleted %s%s; %s" % (", ".join(deleted) or "nothing", ("; could not delete %s" % left) if left else "", dm)


def pvl_undo():
    """Not available: undoing ParaView's undo stack from a script crashed ParaView
    (2026-11-20, vtkSMSessionProxyManagerInternals::ComputeDelta). Reverse a change
    directly instead."""
    raise PVLError("undo is not available (it can crash ParaView). Reverse the last change directly: "
                   "pvl_remove or Delete what was created, or set the changed values back, and say what you did.")


# --------------------------------------------------------------------------- state for the model

def pvl_items():
    """What the headset has: one entry per Sender."""
    out = []
    for n, p in sorted(_objects().items()):
        s = _sender_of(p)
        if s is None:
            continue
        var = _colour_var(s)
        e = {"name": s.ItemName, "object": n, "shown": _shown(s), "colour_by": var,
             "points": p.GetDataInformation().GetNumberOfPoints(), "topology": _topology(p)}
        if var:
            lut = GetColorTransferFunction(var)
            e["range"] = [round(lut.RGBPoints[0], 6), round(lut.RGBPoints[-4], 6)]
            if lut.UseLogScale:
                e["log"] = True
        out.append(e)
    return out


def _key_params(p):
    x = p.GetXMLName()
    try:
        if x == "Contour":
            return "contour by %s at %s" % (p.ContourBy[1] if len(p.ContourBy) > 1 else p.ContourBy, list(p.Isosurfaces))
        if x in ("Cut",):
            return "normal %s origin %s" % (list(p.SliceType.Normal), [round(v, 4) for v in p.SliceType.Origin])
        if x == "Clip":
            return "normal %s origin %s" % (list(p.ClipType.Normal), [round(v, 4) for v in p.ClipType.Origin])
        if x == "Calculator":
            return "%s = %s" % (p.ResultArrayName, p.Function)
        if x == "Threshold":
            return "%s from %s to %s" % (p.Scalars[1] if len(p.Scalars) > 1 else p.Scalars, p.LowerThreshold, p.UpperThreshold)
        if "FileName" in p.ListProperties():
            f = p.FileName
            return "file %s" % (f[0] if isinstance(f, (list, tuple)) and f else f)
    except Exception:
        pass
    return ""


def pvl_snapshot():
    """Compact description of the current state, given to the model before each request."""
    try:
        pvl_label_all()
        _hide_unused_legends()
    except Exception:
        pass
    lines = []
    t = GetTimeKeeper().Time
    times = list(GetTimeKeeper().TimestepValues or [])
    lines.append("ParaView time: %g%s" % (t, (" (timesteps %g to %g, %d of them)" % (times[0], times[-1], len(times))) if len(times) > 1 else ""))
    lines.append("Active object (\"it\"): %s" % (_active_name() or "none"))
    objs = _objects()
    if not objs:
        lines.append("Objects: none")
    else:
        lines.append("Objects:")
        for n, p in sorted(objs.items()):
            ins = [_label(i) for i in _inputs(p)]
            arrays = ["%s%s" % (a, "" if p.PointData[a].GetNumberOfComponents() == 1 else "(%d)" % p.PointData[a].GetNumberOfComponents())
                      for a in p.PointData.keys()]
            arrays += ["%s(cell)" % a for a in p.CellData.keys()]
            s = _sender_of(p)
            if s is None:
                head = "not in headset"
            else:
                var = _colour_var(s)
                head = "%s in headset, coloured by %s" % ("SHOWN" if _shown(s) else "hidden", var or "solid colour")
                if var:
                    lut = GetColorTransferFunction(var)
                    pts = list(lut.RGBPoints)
                    head += " (range %g to %g%s, colours %s to %s)" % (
                        pts[0], pts[-4], ", log" if lut.UseLogScale else "",
                        [round(c, 2) for c in pts[1:4]], [round(c, 2) for c in pts[-3:]])
            kp = _key_params(p)
            lines.append("- %s: %s%s; %d points, %s; variables: %s; %s%s" % (
                n, p.GetXMLName(), (" of " + ", ".join(ins)) if ins else "",
                p.GetDataInformation().GetNumberOfPoints(), _topology(p),
                ", ".join(arrays) or "none", head, ("; " + kp) if kp else ""))
    try:
        sc = GetAnimationScene()
        tracks = []
        for q in sc.Cues:
            prop = getattr(q, "AnimatedPropertyName", None)
            if q.GetXMLName() == "CameraAnimationCue":
                tracks.append("camera (%s)" % q.Mode)
            elif prop:
                tgt = _name_of(q.AnimatedProxy) if getattr(q, "AnimatedProxy", None) is not None else "?"
                vals = [round(v, 4) for k in q.KeyFrames for v in list(k.KeyValues)[:1]]
                tracks.append("%s.%s keyframes %s" % (tgt, prop, vals))
        if tracks:
            lines.append("Animation: %s; mode %s, %d frames" % ("; ".join(tracks), sc.PlayMode, sc.NumberOfFrames))
    except Exception:
        pass
    try:
        ranges = pvl_animation_ranges()
        if ranges:
            lines.append("Animation ranges set: " + "; ".join("%s %s over %s" % r for r in ranges))
        running = [j for j in bridge.jobs() if j == "animation" or j.startswith("sweep")]
        if running:
            lines.append("Animation running now")
    except Exception:
        pass
    dom = _domain()
    if dom is not None:
        lines.append("Domain: project %s/%s, bounds x %s y %s z %s" % (dom.Institution, dom.Project,
                     [round(v, 4) for v in dom.XRange], [round(v, 4) for v in dom.YRange], [round(v, 4) for v in dom.ZRange]))
    return "\n".join(lines)


def pvl_vocabulary():
    """Names for speech recognition hints: objects, variables, presets."""
    words = set()
    for n, p in _objects().items():
        words.add(n)
        words.update(p.PointData.keys())
        words.update(p.CellData.keys())
    words.update(["Viridis", "Inferno", "Plasma", "Cool to Warm", "Turbo", "contour", "slice", "outline", "isosurface"])
    return sorted(words)


def pvl_filters(match=None):
    """Names of the filters ParaView has loaded (built-in and from plugins), usable with
    pvl_derive. match: a case-insensitive substring to filter by."""
    from paraview import servermanager as sm
    names = sorted(n for n in dir(sm.filters) if not n.startswith("_"))
    if match:
        names = [n for n in names if match.lower().replace(" ", "") in n.lower()]
    return names


def pvl_filter_help(name):
    """What a filter does and what each of its properties means, from its own
    documentation (built-in or plugin)."""
    from paraview import servermanager as sm
    ctor = getattr(sm.filters, name, None) or getattr(sm.sources, name, None)
    if ctor is None:
        close = pvl_filters(name[:5])
        raise PVLError("no filter %r%s" % (name, ("; similar: " + ", ".join(close[:8])) if close else ""))
    p = ctor()
    d = p.SMProxy.GetDocumentation()
    out = {"filter": name, "about": (d.GetDescription() or d.GetShortHelp() or "") if d else "", "properties": {}}
    for n in p.ListProperties():
        if n in ("Input", "ReloadPythonModule"):
            continue
        prop = p.GetProperty(n)
        sp = getattr(prop, "SMProperty", None) or p.SMProxy.GetProperty(n)
        doc = sp.GetDocumentation() if sp is not None else None
        try:
            default = getattr(p, n)
            default = list(default) if hasattr(default, "__len__") and not isinstance(default, str) else default
        except Exception:
            default = None
        out["properties"][n] = {"default": default, "about": (doc.GetDescription() if doc else "").strip()}
    return out


def pvl_integrate(name, var):
    """Integral of a variable over an object (volume or area weighted), at the current
    time, in mesh units: e.g. the total amount from a concentration field."""
    p = _obj(name)
    integ = IntegrateVariables(Input=p)
    try:
        integ.UpdatePipeline(GetTimeKeeper().Time)
        attrs = integ.PointData if var in integ.PointData.keys() else integ.CellData
        if var not in attrs.keys():
            raise PVLError("%s has no variable %r" % (_name_of(p), var))
        return attrs[var].GetRange()[0]
    finally:
        Delete(integ)


def pvl_find_data(pattern):
    """Data files under the configured data folders whose names match (glob, case-insensitive)."""
    pat = pattern if any(c in pattern for c in "*?[") else "*%s*" % pattern
    hits = []
    for root in _PVL["data_roots"]:
        root = os.path.expanduser(root)
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in files:
                if fnmatch.fnmatch(f.lower(), pat.lower()):
                    hits.append(os.path.join(d, f))
                    if len(hits) >= _PVL["max_find"]:
                        return hits
    return hits


# --------------------------------------------------------------------------- undo grouping (used by Part 1)

def _pvl_begin(label):
    from paraview import servermanager as sm
    b = sm.vtkSMProxyManager.GetProxyManager().GetUndoStackBuilder()
    if b is not None:
        b.Begin(label[:80])


def _pvl_end():
    from paraview import servermanager as sm
    b = sm.vtkSMProxyManager.GetProxyManager().GetUndoStackBuilder()
    if b is not None:
        b.End()
        b.PushToStack()
