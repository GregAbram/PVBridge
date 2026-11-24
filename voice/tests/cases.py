"""Test set for pvagent: realistic spoken requests with automatic checks.

Each case:
  id, cat      name and category (basic / intermediate / hard / answer / multi)
  setup        state ParaView is reset to first (see _t_setup in HELPERS)
  turns        list of user requests, sent in order in one conversation
  check        Python run inside ParaView afterwards; body of a function that
               returns (ok, detail). Helpers from HELPERS are available.
  reply_re     regex the final spoken reply must match (case-insensitive)
  expect       Python expression evaluated in ParaView *before* the request (setup
               state); a number in the reply must be within 2% of it
  reference    known-good code; the 'reference' backend runs it instead of a model,
               which proves the setup and check are right
"""

HELPERS = r'''
import os, re, math
from paraview import servermanager as _t_sm
_T_PLUME = os.path.expanduser("~/ParaViewPlugins/out/plume/particles.pvd")
_T_OUT = os.path.expanduser("~/.pvbridge/testout")
os.makedirs(_T_OUT, exist_ok=True)

def _t_view():
    return GetActiveViewOrCreate("RenderView")

def _t_id(x):
    if x is None:
        return None
    if not hasattr(x, "GetGlobalIDAsString") and hasattr(x, "Proxy"):
        x = x.Proxy
    return x.GetGlobalIDAsString()

def _t_inputs(p):
    if "Input" not in p.ListProperties():
        return []
    i = p.Input
    if i is None:
        return []
    return list(i) if isinstance(i, (list, tuple)) else [i]

def _t_is_input(p, src):
    return any(_t_id(i) == _t_id(src) for i in _t_inputs(p))

def _t_rep(p, v=None):
    return _t_sm.GetRepresentation(p, v or _t_view())

def _t_visible(p, v=None):
    r = _t_rep(p, v)
    return bool(r is not None and r.Visibility)

def _t_sources():
    return list(GetSources().values())

def _t_find(xml_names=None, visible=None, input_of=None):
    out = []
    for s in _t_sources():
        if xml_names and s.GetXMLName() not in xml_names:
            continue
        if visible is not None and _t_visible(s) != visible:
            continue
        if input_of is not None and not _t_is_input(s, input_of):
            continue
        out.append(s)
    return out

def _t_lut_range(lut):
    p = lut.RGBPoints
    return p[0], p[-4]

def _t_cam_dir(v=None):
    v = v or _t_view()
    d = [f - p for f, p in zip(v.CameraFocalPoint, v.CameraPosition)]
    n = math.sqrt(sum(c * c for c in d)) or 1.0
    return [c / n for c in d]

def _t_max_over_time(src, name):
    t0 = GetTimeKeeper().Time
    mx = -1e300
    for t in src.TimestepValues:
        src.UpdatePipeline(t)
        mx = max(mx, src.PointData[name].GetRange()[1])
    src.UpdatePipeline(t0)
    return mx

def _t_remove_cues(sc):
    """Remove every animation track except the time track, and unregister them.
    Never drop the time track: ParaView's timeline widget crashes without it."""
    tt = GetTimeTrack()
    tid = tt.GetGlobalIDAsString()
    seen, uniq = set(), []
    for c in sc.Cues:                 # first drop duplicate entries (a model may append a track twice)
        if c.GetGlobalIDAsString() not in seen:
            seen.add(c.GetGlobalIDAsString()); uniq.append(c)
    if len(uniq) != len(sc.Cues):
        sc.Cues = uniq
    extra = [c for c in uniq if c.GetGlobalIDAsString() != tid]
    for c in extra:                   # then remove the others one at a time
        sc.Cues = [q for q in sc.Cues if q.GetGlobalIDAsString() != c.GetGlobalIDAsString()]
    pxm = _t_sm.ProxyManager()
    for c in extra:
        name = pxm.GetProxyName("animation", c)
        if name:
            pxm.UnRegisterProxy("animation", name, c)

def _t_clear():
    v = _t_view()
    _t_remove_cues(GetAnimationScene())   # tracks first, while the proxies they animate still exist
    for _ in range(50):
        srcs = _t_sources()
        if not srcs:
            break
        used = set()
        for s in srcs:
            for i in _t_inputs(s):
                used.add(_t_id(i))
        for s in srcs:
            if _t_id(s) not in used:
                Delete(s)
    sc = GetAnimationScene()
    sc.PlayMode = "Snap To TimeSteps"
    pxm = _t_sm.ProxyManager()
    for grp in ("lookup_tables", "piecewise_functions"):
        for (name, _id), p in list(pxm.GetProxiesInGroup(grp).items()):
            pxm.UnRegisterProxy(grp, name, p)
    for r in v.Representations:
        if r.GetXMLName().endswith("ScalarBarWidgetRepresentation"):
            r.Visibility = 0
    v.UseColorPaletteForBackground = 1
    v.OrientationAxesVisibility = 1
    v.CameraParallelProjection = 0

def _t_side_camera(v):
    v.CameraFocalPoint = [0, 0, 0]; v.CameraPosition = [0, -1, 0]; v.CameraViewUp = [0, 0, 1]
    v.ResetCamera()

def _t_setup(name):
    _t_clear()
    v = _t_view()
    if name in ("plume", "plume_smoke"):
        r = PVDReader(registrationName="particles.pvd", FileName=_T_PLUME)
        d = Show(r, v)
        d.ColorArrayName = ["POINTS", ""]
        d.PointSize = 2
        sc = GetAnimationScene()
        sc.UpdateAnimationUsingDataTimeSteps()
        sc.AnimationTime = 0.0
        r.UpdatePipeline(0.0)          # otherwise the data stays at the previous case's time
        _t_side_camera(v)
        if name == "plume_smoke":
            ColorBy(d, ("POINTS", "smoke"))
            lut = GetColorTransferFunction("smoke")
            lut.RescaleTransferFunction(0.0, 2.46)
            lut.AutomaticRescaleRangeMode = "Never"
            d.SetScalarBarVisibility(v, False)
        SetActiveSource(r)
    elif name == "wavelet":
        w = Wavelet(registrationName="Wavelet1")
        Show(w, v)
        sc = GetAnimationScene()
        sc.AnimationTime = 0.0
        v.CameraFocalPoint = [0, 0, 0]; v.CameraPosition = [30, -40, 25]; v.CameraViewUp = [0, 0, 1]
        v.ResetCamera()
        SetActiveSource(w)
    for f in ("view.png", "state.pvsm"):
        p = os.path.join(_T_OUT, f)
        if os.path.exists(p):
            os.remove(p)
    Render(v)
    return "setup %s: %d sources" % (name, len(_t_sources()))
'''

# shorthand used in checks
R = 'r = FindSource("particles.pvd"); v = _t_view(); d = _t_rep(r, v)\n'
W = 'w = FindSource("Wavelet1"); v = _t_view()\n'

CASES = [
    # ------------------------------------------------------------------ basic
    dict(id="bg_black", cat="basic", setup="plume",
         turns=["make the background black"],
         check='v = _t_view()\nreturn (v.UseColorPaletteForBackground == 0 and list(v.Background) == [0, 0, 0]), (v.UseColorPaletteForBackground, list(v.Background))',
         reference='v = GetActiveView(); v.UseColorPaletteForBackground = 0; v.BackgroundColorMode = "Single Color"; v.Background = [0, 0, 0]'),
    dict(id="bg_white", cat="basic", setup="plume",
         turns=["can you set the background to white"],
         check='v = _t_view()\nreturn (v.UseColorPaletteForBackground == 0 and list(v.Background) == [1, 1, 1]), (v.UseColorPaletteForBackground, list(v.Background))',
         reference='v = GetActiveView(); v.UseColorPaletteForBackground = 0; v.BackgroundColorMode = "Single Color"; v.Background = [1, 1, 1]'),
    dict(id="color_smoke", cat="basic", setup="plume",
         turns=["color the particles by smoke"],
         check=R + 'return d.ColorArrayName[1] == "smoke", list(d.ColorArrayName)',
         reference='r = FindSource("particles.pvd"); ColorBy(GetDisplayProperties(r), ("POINTS", "smoke"))'),
    dict(id="points3", cat="basic", setup="plume",
         turns=["make the points three pixels"],
         check=R + 'return d.PointSize == 3, d.PointSize',
         reference='GetDisplayProperties(FindSource("particles.pvd")).PointSize = 3'),
    dict(id="red", cat="basic", setup="plume",
         turns=["make the particles red"],
         check=R + 'c = list(d.DiffuseColor); solid = not d.ColorArrayName[1]\nreturn solid and c[0] > 0.8 and c[1] < 0.3 and c[2] < 0.3, (list(d.ColorArrayName), c)',
         reference='d = GetDisplayProperties(FindSource("particles.pvd")); d.ColorArrayName = ["POINTS", ""]; d.AmbientColor = d.DiffuseColor = [1, 0, 0]'),
    dict(id="hide_axes", cat="basic", setup="plume",
         turns=["hide the orientation axes"],
         check='v = _t_view()\nreturn v.OrientationAxesVisibility == 0, v.OrientationAxesVisibility',
         reference='GetActiveView().OrientationAxesVisibility = 0'),
    dict(id="hide_particles", cat="basic", setup="plume",
         turns=["hide the particles"],
         check=R + 'return not _t_visible(r), _t_visible(r)',
         reference='Hide(FindSource("particles.pvd"))'),
    dict(id="last_step", cat="basic", setup="plume",
         turns=["go to the last time step"],
         check='t = GetTimeKeeper().Time\nreturn abs(t - 200) < 1e-6, t',
         reference='GetAnimationScene().GoToLast()'),
    dict(id="time100", cat="basic", setup="plume",
         turns=["jump to time one hundred"],
         check='t = GetTimeKeeper().Time\nreturn abs(t - 100) <= 1.25, t',
         reference='GetAnimationScene().AnimationTime = 100.0'),
    dict(id="parallel", cat="basic", setup="plume",
         turns=["switch to a parallel projection"],
         check='v = _t_view()\nreturn v.CameraParallelProjection == 1, v.CameraParallelProjection',
         reference='GetActiveView().CameraParallelProjection = 1'),
    dict(id="play", cat="basic", setup="plume",
         turns=["play the animation"],
         check='t = GetTimeKeeper().Time\nreturn abs(t - 200) < 1e-6, t',
         reference='GetAnimationScene().Play()'),

    # ----------------------------------------------------------- intermediate
    dict(id="top_view", cat="intermediate", setup="plume",
         turns=["look straight down on the plume from above"],
         check='dd = _t_cam_dir()\nreturn dd[2] < -0.98, [round(c, 3) for c in dd]',
         reference='v = GetActiveView(); v.CameraFocalPoint = [0, 0, 0]; v.CameraPosition = [0, 0, 1]; v.CameraViewUp = [0, 1, 0]; v.ResetCamera()'),
    dict(id="vel_mag", cat="intermediate", setup="plume",
         turns=["color by the magnitude of the velocity"],
         check=R + 'lut = GetColorTransferFunction("Velocity")\nreturn d.ColorArrayName[1] == "Velocity" and lut.VectorMode == "Magnitude", (list(d.ColorArrayName), lut.VectorMode)',
         reference='ColorBy(GetDisplayProperties(FindSource("particles.pvd")), ("POINTS", "Velocity", "Magnitude"))'),
    dict(id="legend", cat="intermediate", setup="plume_smoke",
         turns=["show the color legend"],
         check=R + 'lut = GetColorTransferFunction("smoke"); sb = GetScalarBar(lut, v)\nreturn bool(sb.Visibility), sb.Visibility',
         reference='r = FindSource("particles.pvd"); GetDisplayProperties(r).SetScalarBarVisibility(GetActiveView(), True)'),
    dict(id="log_scale", cat="intermediate", setup="plume_smoke",
         turns=["use a log scale for the smoke colors"],
         check='lut = GetColorTransferFunction("smoke"); lo, hi = _t_lut_range(lut)\nreturn lut.UseLogScale == 1 and lo > 0, (lut.UseLogScale, lo, hi)',
         reference='lut = GetColorTransferFunction("smoke"); lut.RescaleTransferFunction(1e-4, 2.46); lut.UseLogScale = 1'),
    dict(id="viridis", cat="intermediate", setup="plume_smoke",
         turns=["switch the color map to viridis"],
         check='lut = GetColorTransferFunction("smoke"); p = list(lut.RGBPoints)\n'
               'first = p[1:4]; last = p[-3:]\n'
               'ok = abs(first[0] - 0.267) < 0.03 and first[1] < 0.05 and abs(first[2] - 0.329) < 0.03 and last[0] > 0.9 and last[1] > 0.85 and last[2] < 0.2\n'
               'return ok, ([round(c, 3) for c in first], [round(c, 3) for c in last])',
         reference='GetColorTransferFunction("smoke").ApplyPreset("Viridis", True)'),
    dict(id="range01", cat="intermediate", setup="plume_smoke",
         turns=["set the smoke color range from zero to one"],
         check='lut = GetColorTransferFunction("smoke"); lo, hi = _t_lut_range(lut)\nreturn abs(lo) < 1e-6 and abs(hi - 1) < 1e-6, (lo, hi)',
         reference='lut = GetColorTransferFunction("smoke"); lut.RescaleTransferFunction(0.0, 1.0); lut.AutomaticRescaleRangeMode = "Never"'),
    dict(id="rescale_time", cat="intermediate", setup="plume",
         turns=["color by smoke and set the range to cover all the time steps"],
         check=R + 'lut = GetColorTransferFunction("smoke"); lo, hi = _t_lut_range(lut); mx = _t_max_over_time(r, "smoke")\nreturn d.ColorArrayName[1] == "smoke" and abs(hi - mx) / mx < 0.01, (list(d.ColorArrayName), lo, hi, mx)',
         reference='d = GetDisplayProperties(FindSource("particles.pvd")); ColorBy(d, ("POINTS", "smoke")); d.RescaleTransferFunctionToDataRangeOverTime()'),
    dict(id="semi_transparent", cat="intermediate", setup="plume",
         turns=["make the particles semi transparent"],
         check=R + 'return 0.05 < d.Opacity < 0.95, d.Opacity',
         reference='GetDisplayProperties(FindSource("particles.pvd")).Opacity = 0.5'),
    dict(id="spheres", cat="intermediate", setup="plume",
         turns=["draw the particles as little spheres with radius half a unit"],
         check=R + 'g = [s for s in _t_find(["Glyph", "GlyphWithCustomSource"], visible=True) if _t_is_input(s, r)]\n'
               'pg = d.Representation == "Point Gaussian" and 0.4 <= d.GaussianRadius <= 0.6 and _t_visible(r)\n'
               'return pg or bool(g), (d.Representation, d.GaussianRadius, len(g))',
         reference='d = GetDisplayProperties(FindSource("particles.pvd")); d.SetRepresentationType("Point Gaussian"); d.GaussianRadius = 0.5; d.ShaderPreset = "Sphere"'),
    dict(id="threshold", cat="intermediate", setup="plume",
         turns=["only show the particles where smoke is above zero point five"],
         check=R + 'hits = []\n'
               'for s in _t_find(visible=True):\n'
               '    if _t_id(s) == _t_id(r): continue\n'
               '    a = s.PointData["smoke"] if "smoke" in s.PointData.keys() else None\n'
               '    n = s.GetDataInformation().GetNumberOfPoints()\n'
               '    if a is not None and n > 0: hits.append((s.GetXMLName(), n, a.GetRange()[0]))\n'
               'ok = (not _t_visible(r)) and any(lo >= 0.5 - 1e-6 for _, _, lo in hits)\n'
               'return ok, (_t_visible(r), hits)',
         reference='r = FindSource("particles.pvd"); t = Threshold(Input=r, Scalars=["POINTS", "smoke"], LowerThreshold=0.5, UpperThreshold=1e9, ThresholdMethod="Between"); Show(t); Hide(r)'),
    dict(id="outline", cat="intermediate", setup="plume",
         turns=["put a bounding box outline around the particles"],
         check=R + 'o = _t_find(["OutlineFilter", "OutlineCorners"], visible=True, input_of=r)\n'
               'return bool(o) or d.Representation == "Outline", (len(o), d.Representation)',
         reference='o = Outline(Input=FindSource("particles.pvd")); Show(o)'),
    dict(id="text_label", cat="intermediate", setup="plume",
         turns=["add a title at the top that says Plume Dispersion"],
         check='t = [s for s in _t_find(["TextSource"], visible=True) if "plume" in s.Text.lower()]\nreturn bool(t), [s.Text for s in _t_find(["TextSource"])]',
         reference='t = Text(Text="Plume Dispersion"); td = Show(t); td.WindowLocation = "Upper Center"'),
    dict(id="time_label", cat="intermediate", setup="plume",
         turns=["show the current time in the top left corner"],
         check='hits = [(s.GetXMLName(), str(_t_rep(s).WindowLocation)) for s in _t_find(visible=True) if "Time" in s.GetXMLName() and "WindowLocation" in _t_rep(s).ListProperties()]\n'
               'return any("Upper Left" in str(loc) for _, loc in hits), hits',
         reference='a = AnnotateTimeFilter(Input=FindSource("particles.pvd")); ad = Show(a); ad.WindowLocation = "Upper Left Corner"'),
    dict(id="contour150", cat="intermediate", setup="wavelet",
         turns=["add a contour at one fifty"],
         check=W + 'c = _t_find(["Contour"], visible=True, input_of=w)\nreturn bool(c) and list(c[0].Isosurfaces) == [150.0], [list(x.Isosurfaces) for x in c]',
         reference='c = Contour(Input=FindSource("Wavelet1"), ContourBy=["POINTS", "RTData"], Isosurfaces=[150.0]); Show(c)'),
    dict(id="slice_x", cat="intermediate", setup="wavelet",
         turns=["slice it through the middle with a plane facing the x direction"],
         check=W + 's = _t_find(["Cut"], visible=True, input_of=w)\n'
               'ok = bool(s) and abs(s[0].SliceType.Normal[0]) > 0.99 and abs(s[0].SliceType.Origin[0]) < 1\n'
               'return ok, [(list(x.SliceType.Normal), list(x.SliceType.Origin)) for x in s]',
         reference='s = Slice(Input=FindSource("Wavelet1")); s.SliceType.Normal = [1, 0, 0]; s.SliceType.Origin = [0, 0, 0]; Show(s)'),
    dict(id="volume", cat="intermediate", setup="wavelet",
         turns=["show the wavelet as a volume rendering"],
         check=W + 'return _t_rep(w, v).Representation == "Volume", _t_rep(w, v).Representation',
         reference='d = GetDisplayProperties(FindSource("Wavelet1")); d.SetRepresentationType("Volume")'),
    dict(id="clip_xpos", cat="intermediate", setup="wavelet",
         turns=["clip away the half where x is positive"],
         check=W + 'c = _t_find(["Clip"], visible=True, input_of=w)\n'
               'b = c[0].GetDataInformation().GetBounds() if c else None\n'
               'return bool(c) and b[1] <= 0.6 and b[0] < -9, b',
         reference='c = Clip(Input=FindSource("Wavelet1")); c.ClipType = "Plane"; c.ClipType.Normal = [1, 0, 0]; c.ClipType.Origin = [0, 0, 0]; c.Invert = 1; Show(c); Hide(FindSource("Wavelet1"))'),

    # ------------------------------------------------------------------- hard
    dict(id="animate_contour", cat="hard", setup="wavelet",
         turns=["make a contour and animate its value from fifty to two fifty over fifty frames, but don't play it yet"],
         check=W + 'sc = GetAnimationScene()\n'
               'tr = [q for q in sc.Cues if getattr(q, "AnimatedPropertyName", None) == "ContourValues"]\n'
               'vals = sorted(k.KeyValues[0] for q in tr for k in q.KeyFrames if len(k.KeyValues))\n'
               'ok = bool(tr) and vals and abs(vals[0] - 50) < 1e-6 and abs(vals[-1] - 250) < 1e-6 and sc.NumberOfFrames == 50 and sc.PlayMode == "Sequence"\n'
               'return ok, (len(tr), vals, sc.NumberOfFrames, sc.PlayMode)',
         reference='w = FindSource("Wavelet1"); c = Contour(Input=w, ContourBy=["POINTS", "RTData"], Isosurfaces=[50.0]); Show(c)\n'
                   'sc = GetAnimationScene(); sc.PlayMode = "Sequence"; sc.NumberOfFrames = 50\n'
                   'tr = GetAnimationTrack("ContourValues", index=0, proxy=c)\n'
                   'tr.KeyFrames = [CompositeKeyFrame(KeyTime=0, KeyValues=[50.0]), CompositeKeyFrame(KeyTime=1, KeyValues=[250.0])]'),
    dict(id="orbit", cat="hard", setup="wavelet",
         turns=["set up the animation so the camera does one full orbit around the data"],
         check='sc = GetAnimationScene()\ncams = [q for q in sc.Cues if q.GetXMLName() == "CameraAnimationCue"]\n'
               'return any(q.Mode == "Path-based" for q in cams), [q.Mode for q in cams]',
         reference='import math\nv = GetActiveView(); cam = GetCameraTrack(view=v); cam.Mode = "Path-based"\n'
                   'pts = []\nfor i in range(24):\n    a = 2 * math.pi * i / 24; pts += [60 * math.cos(a), 60 * math.sin(a), 20]\n'
                   'k0 = CameraKeyFrame(KeyTime=0, FocalPoint=[0, 0, 0], ViewUp=[0, 0, 1], PositionPathPoints=pts, FocalPathPoints=[0, 0, 0], ClosedPositionPath=1)\n'
                   'k1 = CameraKeyFrame(KeyTime=1, FocalPoint=[0, 0, 0], ViewUp=[0, 0, 1])\ncam.KeyFrames = [k0, k1]'),
    dict(id="save_shot", cat="hard", setup="plume",
         turns=["save a screenshot of the view to ~/.pvbridge/testout/view.png"],
         check='p = os.path.join(_T_OUT, "view.png")\nok = os.path.exists(p) and open(p, "rb").read(8) == b"\\x89PNG\\r\\n\\x1a\\n"\nreturn ok, p',
         reference='SaveScreenshot(os.path.expanduser("~/.pvbridge/testout/view.png"), GetActiveView())'),
    dict(id="save_state", cat="hard", setup="plume",
         turns=["save the paraview state to ~/.pvbridge/testout/state.pvsm"],
         check='p = os.path.join(_T_OUT, "state.pvsm")\nok = os.path.exists(p) and "ServerManagerState" in open(p).read(4000)\nreturn ok, p',
         reference='SaveState(os.path.expanduser("~/.pvbridge/testout/state.pvsm"))'),

    # ----------------------------------------------------------------- answer
    dict(id="count_last", cat="answer", setup="plume",
         turns=["how many particles are there at the last time step"],
         expect='r = FindSource("particles.pvd"); r.UpdatePipeline(r.TimestepValues[-1]); n = r.GetDataInformation().GetNumberOfPoints(); r.UpdatePipeline(GetTimeKeeper().Time); n',
         check='return True, ""',
         reference='sc = GetAnimationScene(); sc.GoToLast(); FindSource("particles.pvd").GetDataInformation().GetNumberOfPoints()'),
    dict(id="max_smoke", cat="answer", setup="plume",
         turns=["what's the biggest smoke value right now"],
         expect='r = FindSource("particles.pvd"); r.UpdatePipeline(GetTimeKeeper().Time); r.PointData["smoke"].GetRange()[1]',
         check='return True, ""',
         reference='FindSource("particles.pvd").PointData["smoke"].GetRange()[1]'),
    dict(id="arrays", cat="answer", setup="plume",
         turns=["what variables does this data have"],
         reply_re=r"(?=.*smoke)(?=.*weight)(?=.*velocity)",
         check='return True, ""',
         reference='FindSource("particles.pvd").PointData.keys()'),

    # ------------------------------------------------------------------ multi
    dict(id="multi_log", cat="multi", setup="plume",
         turns=["color the particles by smoke", "now make that a log scale"],
         check=R + 'lut = GetColorTransferFunction("smoke"); lo, hi = _t_lut_range(lut)\n'
               'return d.ColorArrayName[1] == "smoke" and lut.UseLogScale == 1 and lo > 0, (list(d.ColorArrayName), lut.UseLogScale, lo)',
         reference='d = GetDisplayProperties(FindSource("particles.pvd")); ColorBy(d, ("POINTS", "smoke")); lut = GetColorTransferFunction("smoke"); lut.RescaleTransferFunction(1e-4, 2.46); lut.UseLogScale = 1'),
    dict(id="multi_undo_color", cat="multi", setup="plume",
         turns=["make the particles red", "actually go back to coloring them by smoke"],
         check=R + 'return d.ColorArrayName[1] == "smoke", list(d.ColorArrayName)',
         reference='d = GetDisplayProperties(FindSource("particles.pvd")); ColorBy(d, ("POINTS", "smoke"))'),
]
