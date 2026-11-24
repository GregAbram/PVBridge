"""bake -- write a saved visualization, every frame, into its own DataManager project
(SPEC-pvlink-voice.md 10.2). Run with pvbatch; no GUI.

    pvbatch bake.py STATE.pvsm --project "fire smoke" [--institution GDA]
                    [--host 10.0.0.49 --port 9010] [--steps 10] [--times 0:200] [--stride 2]
                    [--plugin PLUGIN.py ...] [--replace] [--progress FILE.json]

Frames: the data's timesteps (optionally a range and a stride); with no timesteps,
--steps frames at times 0, 1, 2, .... Animation ranges stored on objects (pvl_set_animation_range)
are spread over the frames. Every shown DMLink Sender is sent timed, so headsets can play the
project by themselves; hidden ones are left out. The project must be new or empty, or --replace
empties it first. Each frame is confirmed stored by the DataManager before the next.
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("state", help="saved ParaView state (.pvsm)")
ap.add_argument("--project", required=True, help="project name to bake into (Institution/<this>)")
ap.add_argument("--institution", default="GDA")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=9010)
ap.add_argument("--steps", type=int, default=10, help="frames when the data has no timesteps")
ap.add_argument("--times", default="all", help='"all" or "t0:t1" (data times to include)')
ap.add_argument("--stride", type=int, default=1, help="use every Nth timestep")
ap.add_argument("--plugin", action="append", default=[], help="ParaView plugin to load first (repeatable)")
ap.add_argument("--dmlink", default=os.path.expanduser("~/Documents/GitHub/DataManager/paraview/DMLink.py"))
ap.add_argument("--dm-probe", default=os.path.expanduser("~/Documents/GitHub/DataManager/build/dm_probe"))
ap.add_argument("--replace", action="store_true", help="empty the project first if it has data")
ap.add_argument("--progress", help="write progress here as JSON")
a = ap.parse_args()

t_start = time.time()


def progress(**kw):
    kw.setdefault("project", "%s/%s" % (a.institution, a.project))
    kw["elapsed"] = round(time.time() - t_start, 1)
    print("[bake] %s" % json.dumps(kw), flush=True)
    if a.progress:
        tmp = a.progress + ".tmp"
        with open(tmp, "w") as f:
            json.dump(kw, f)
        os.replace(tmp, a.progress)


def fail(msg):
    progress(state="failed", message=msg)
    sys.exit(1)


progress(state="starting", message="loading plugins and state")
from paraview.simple import *   # noqa: E402,F401,F403

LoadPlugin(a.dmlink, ns=globals())
for p in a.plugin:
    try:
        LoadPlugin(p, ns=globals())
    except Exception as e:
        print("[bake] could not load plugin %s: %s" % (p, e), flush=True)
dml = [m for m in list(sys.modules.values()) if hasattr(m, "DMSource") and hasattr(m, "build_keydata")][0]

# The voice helpers: stored animation ranges, their values, and emptying a project.
exec(open(os.path.join(HERE, "pvlink.py")).read(), globals())
pvl_config(institution=a.institution, project=a.project, dm_host=a.host, dm_port=a.port,   # noqa: F821
           dm_probe=a.dm_probe)

project = "%s/%s" % (a.institution, a.project)
try:
    existing = _project_keys(project)   # noqa: F821
except Exception as e:
    fail("can't reach the DataManager at %s:%d to check the project: %s" % (a.host, a.port, e))
if existing:
    if not a.replace:
        fail("project %s already has %d items; use --replace or another name" % (project, len(existing)))
    src = dml.DMSource(a.host, a.port, project, peer_name="bake.py (clearing)")
    removed, failed = _wipe(src, existing)   # noqa: F821
    src.close()
    if failed:
        fail("could not empty project %s: %s" % (project, ", ".join(failed)))
    progress(state="starting", message="emptied %d old items" % len(removed))

LoadState(a.state)
views = GetViews()
if not views:
    fail("the state has no view")
view = GetActiveView() or views[0]

# Point every DMLink Domain at this bake's project and state.
domains = [p for p in GetSources().values() if p.GetXMLName() == "DMLinkDomain"]
if not domains:
    fail("the state has no DMLink Domain: nothing was in the headset when it was saved")
for d in domains:
    d.Host, d.TCPPort, d.Institution, d.Project, d.State = a.host, a.port, a.institution, a.project, ""

# Shown Senders are baked timed; hidden ones are left out.
from paraview import servermanager as sm   # noqa: E402
senders = [p for p in GetSources().values() if p.GetXMLName() == "DMLinkSender"]
shown = []
for s in senders:
    r = sm.GetRepresentation(s, view)
    if r is not None and r.Visibility:
        s.Time = "Timed"
        shown.append(s.ItemName)
    else:
        Delete(s)
if not shown:
    fail("nothing is shown in the headset in this state")

# Frames.
data_times = list(GetTimeKeeper().TimestepValues or [])
if data_times:
    frames = data_times
    if a.times != "all":
        lo, _, hi = a.times.partition(":")
        frames = [t for t in frames if float(lo or "-inf") <= t <= float(hi or "inf")]
    frames = frames[::max(1, a.stride)]
else:
    frames = [float(k) for k in range(max(1, a.steps))]
if not frames:
    fail("no frames in the requested time range")
tracks = [_track(n, pr, v, len(frames)) for (n, pr, v) in pvl_animation_ranges()]   # noqa: F821

progress(state="baking", frame=0, frames=len(frames), items=shown,
         animated=["%s %s" % (t["name"], t["prop"]) for t in tracks])
scene = GetAnimationScene()
for k, t in enumerate(frames):
    if data_times:
        scene.AnimationTime = t
    view.ViewTime = t
    for tr in tracks:
        _apply(tr, tr["values"][min(k, len(tr["values"]) - 1)])   # noqa: F821
    for d in domains:
        d.UpdatePipeline(t)        # configures DMLink and sends the domain at this time
    Render(view)                   # DMLink sends the changed items as one batch at time t ...
    dml.flush()                    # ... and waits until the DataManager has stored it
    progress(state="baking", frame=k + 1, frames=len(frames), t=t)

dml.flush()
progress(state="done", frames=len(frames), items=shown, t0=frames[0], t1=frames[-1],
         message="baked %d frames of %s into %s (state %s-%s)" % (
             len(frames), ", ".join(shown), project, a.institution, a.project))
