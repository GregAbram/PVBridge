"""
pvbridge -- run Python commands in a live ParaView GUI from outside.

A repeating VTK timer on the active view's interactor (driven by Qt, so it
runs on ParaView's main thread) runs waiting commands in a persistent
namespace that has paraview.simple loaded. Commands arrive two ways, both
run by the same timer, oldest first:

  * Network (SPEC-pvlink-voice.md 6.2-6.3): a listener thread accepts HTTP
    requests, puts them on an in-memory queue, and waits for each result.
    It never touches ParaView itself.
        POST /run     {"code": "...", "screenshot": false|true|width, "timeout": s}
                   -> {"status": "ok"|"warn"|"error", "seconds", "output", "png"?}
        GET  /status
    Every request needs "Authorization: Bearer <token>". The token is
    PVBRIDGE_TOKEN from the environment or ~/.pvbridge/keys.env (mode 600);
    with no token the listener stays off. Default address 127.0.0.1:9020.
  * File mailbox (default ~/.pvbridge, mode 700):
        in/     command files  <id>.py   (write <id>.py.tmp, then rename)
        out/    results        <id>.txt  first line "## ok|warn|error 0.12s"
        done/   commands that have been run
    Only files owned by you and not group/world-writable are run.

Output of a command includes everything printed while it runs and while the
views re-render afterwards (DMLink sends during that render), the value of a
final expression or the traceback, and VTK/ParaView errors and warnings
logged meanwhile; if any were logged the status is "warn".

Start (ParaView Python Shell):
    import importlib.util, sys
    _spec = importlib.util.spec_from_file_location("pvbridge", "/Users/gda/pvbridge/pvbridge.py")
    pvbridge = importlib.util.module_from_spec(_spec); sys.modules["pvbridge"] = pvbridge
    _spec.loader.exec_module(pvbridge); pvbridge.start()
        start(listen="0.0.0.0:9020") to accept other machines; listen=None for mailbox only.
Stop:    pvbridge.stop()

Inside commands:  everything from paraview.simple, plus
    shot(path=None, **kw)   SaveScreenshot of the active view; returns the path
    bridge                  this module

A long command blocks the GUI while it runs, as it would if typed in the shell.
"""

import ast
import base64
import contextlib
import hmac
import io
import json
import os
import queue
import stat
import sys
import threading
import time
import traceback

ROOT = os.path.expanduser("~/.pvbridge")
INTERVAL_MS = 50
GIL_YIELD_S = 0.002
DEFAULT_LISTEN = "127.0.0.1:9020"
MAX_BODY = 16 * 1024 * 1024

ns = {}            # persistent command namespace
_state = {"iren": None, "tid": None, "tag": None, "busy": False, "count": 0,
          "server": None, "server_thread": None, "listen": None, "token": None, "view": None,
          "activity": None}
_queue = queue.Queue()
_jobs = []        # background jobs: [name, generator, due_time]; see add_job()


def _dirs():
    return {k: os.path.join(ROOT, k) for k in ("in", "out", "done", "rejected", "shots")}


def _log(msg):
    try:
        print("[pvbridge] %s" % msg)
    except Exception:
        pass


def _setup_dirs():
    os.makedirs(ROOT, mode=0o700, exist_ok=True)
    os.chmod(ROOT, 0o700)
    for d in _dirs().values():
        os.makedirs(d, mode=0o700, exist_ok=True)


def _private(path):
    st = os.stat(path)
    return st.st_uid == os.getuid() and not (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def _read_token():
    tok = os.environ.get("PVBRIDGE_TOKEN", "").strip()
    if tok:
        return tok
    path = os.path.join(ROOT, "keys.env")
    try:
        if not _private(path) or os.stat(path).st_mode & 0o077:
            _log("ignoring %s: must be owned by you and mode 600" % path)
            return None
        with open(path) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == "PVBRIDGE_TOKEN" and v.strip():
                    return v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return None


def _new_namespace():
    ns.clear()
    exec("from paraview.simple import *", ns)
    ns["bridge"] = sys.modules[__name__]
    ns["shot"] = shot
    ns["__name__"] = "__pvbridge__"


def shot(path=None, **kw):
    """Screenshot of the active view; returns the file path."""
    from paraview.simple import SaveScreenshot, GetActiveView
    if path is None:
        path = os.path.join(_dirs()["shots"], time.strftime("%Y%m%d-%H%M%S") + ".png")
    SaveScreenshot(path, GetActiveView(), **kw)
    return path


def _shot_bytes(width):
    """PNG bytes of the active view, scaled to `width` pixels wide."""
    from paraview.simple import GetActiveView
    view = GetActiveView()
    w, h = view.ViewSize
    kw = {}
    if width and w > width:
        kw["ImageResolution"] = [int(width), max(1, int(round(h * width / float(w))))]
    path = os.path.join(_dirs()["shots"], "net-%s.png" % time.strftime("%Y%m%d-%H%M%S"))
    shot(path, **kw)
    with open(path, "rb") as f:
        return f.read()


def _run_source(src, filename):
    """exec src; if the last statement is an expression, return its repr."""
    tree = ast.parse(src, filename, "exec")
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = ast.Expression(tree.body.pop().value)
    exec(compile(tree, filename, "exec"), ns)
    if last is not None:
        value = eval(compile(last, filename, "eval"), ns)
        if value is not None:
            return repr(value)
    return None


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _render():
    from paraview.simple import RenderAllViews
    RenderAllViews()


@contextlib.contextmanager
def _vtk_messages(found):
    """Collects VTK/ParaView errors and warnings logged inside the block."""
    try:
        from vtkmodules.vtkCommonCore import vtkOutputWindow
        from vtkmodules.util.misc import calldata_type
        from vtkmodules.util.vtkConstants import VTK_STRING
    except ImportError:
        yield
        return
    win = vtkOutputWindow.GetInstance()

    def make(kind):
        @calldata_type(VTK_STRING)
        def cb(obj, event, text):
            found.append("%s: %s" % (kind, (text or "").strip()))
        return cb

    cbs = [make("VTK ERROR"), make("VTK WARNING")]
    tags = [win.AddObserver("ErrorEvent", cbs[0]), win.AddObserver("WarningEvent", cbs[1])]
    try:
        yield
    finally:
        for t in tags:
            win.RemoveObserver(t)


def _execute(src, name, screenshot=None):
    """Runs one command and the render after it. Returns (status, seconds, output, png)."""
    out = io.StringIO()
    found = []
    status, png = "ok", None
    t0 = time.time()
    with _vtk_messages(found), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            value = _run_source(src, name)
            if value is not None:
                out.write(value + "\n")
        except BaseException:
            status = "error"
            out.write(traceback.format_exc())
        try:
            _render()
        except Exception:
            out.write("render failed:\n" + traceback.format_exc())
        if screenshot:
            try:
                png = _shot_bytes(800 if screenshot is True else int(screenshot))
            except Exception:
                out.write("screenshot failed:\n" + traceback.format_exc())
    if found:
        out.write("".join(m + "\n" for m in found))
        if status == "ok":
            status = "warn"
    return status, time.time() - t0, out.getvalue(), png


def _first_line(src):
    return src.strip().splitlines()[0][:70] if src.strip() else ""


def run_one(path):
    """Run one mailbox command file and write its result.  Returns True if it ran."""
    d = _dirs()
    name = os.path.basename(path)
    cid = name[:-3]
    if not (_private(ROOT) and _private(path)):
        os.replace(path, os.path.join(d["rejected"], name))
        _log("REJECTED %s (not private to you)" % name)
        return False
    with open(path) as f:
        src = f.read()
    os.replace(path, os.path.join(d["done"], name))
    status, dt, text, _ = _execute(src, name)
    _write_atomic(os.path.join(d["out"], cid + ".txt"), "## %s %.2fs\n%s" % (status, dt, text))
    _state["count"] += 1
    _log("%s %s (%.2fs)  %s" % ("ran" if status != "error" else "ERROR in", cid, dt, _first_line(src)))
    return True


class _Request:
    def __init__(self, code, screenshot):
        self.code, self.screenshot = code, screenshot
        self.done = threading.Event()
        self.result = None
        self.started = False
        self.abandoned = False


def _run_request(req):
    req.started = True
    status, dt, text, png = _execute(req.code, "<net>", req.screenshot)
    res = {"status": status, "seconds": round(dt, 3), "output": text}
    if png is not None:
        res["png"] = base64.b64encode(png).decode()
    req.result = res
    req.done.set()
    _state["count"] += 1
    _log("%s net (%.2fs)  %s" % ("ran" if status != "error" else "ERROR in", dt, _first_line(req.code)))


def poll():
    """Run every waiting command: mailbox files, then queued network requests."""
    if _state["busy"]:
        return
    _state["busy"] = True
    try:
        d = _dirs()["in"]
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".py"))
        except FileNotFoundError:
            names = []
        for n in names:
            try:
                run_one(os.path.join(d, n))
            except Exception:
                _log("internal error on %s:\n%s" % (n, traceback.format_exc()))
        while True:
            try:
                req = _queue.get_nowait()
            except queue.Empty:
                break
            if req.abandoned:
                continue
            try:
                _run_request(req)
            except Exception:
                req.result = {"status": "error", "seconds": 0, "output": "pvbridge internal error:\n" + traceback.format_exc()}
                req.done.set()
        _run_jobs()
    finally:
        _state["busy"] = False


# --------------------------------------------------------------------------- background jobs

def add_job(name, gen):
    """Run a generator a step at a time on the timer, between commands, so the GUI
    repaints after every step. Each step does its work (and renders) and yields the
    seconds to wait before the next step. A job with the same name is replaced."""
    stop_job(name)
    _jobs.append([name, gen, 0.0])
    return name


def stop_job(name=None):
    """Stop the named job, or every job. Returns the names stopped."""
    stopped = []
    for j in list(_jobs):
        if name is None or j[0] == name:
            _jobs.remove(j)
            try:
                j[1].close()
            except Exception:
                pass
            stopped.append(j[0])
    return stopped


def jobs():
    return [j[0] for j in _jobs]


def _run_jobs():
    now = time.time()
    for j in list(_jobs):
        if j[2] > now:
            continue
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                wait = next(j[1])
                _render()
            j[2] = time.time() + max(0.0, float(wait or 0.0))
        except StopIteration:
            _jobs.remove(j)
        except Exception:
            _jobs.remove(j)
            _log("job %s failed:\n%s" % (j[0], traceback.format_exc()))


def _tick(obj, ev, pid):
    if pid != _state["tid"]:
        return
    # ParaView's main thread holds the GIL while its event loop is idle, so the
    # listener's threads only run when the main thread releases it. Release it
    # briefly before (to let requests arrive and be queued) and after (to let
    # replies go out) running commands.
    if _state["server"] is not None:
        time.sleep(GIL_YIELD_S)
    poll()
    if _state["server"] is not None:
        time.sleep(GIL_YIELD_S)


# --------------------------------------------------------------------------- network listener

def _make_server(host, port, token):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "pvbridge"

        def log_message(self, *args):
            pass

        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), ("Bearer " + token).encode())

        def do_GET(self):
            if not self._authorized():
                return self._reply(401, {"error": "missing or wrong token"})
            if self.path == "/status":
                return self._reply(200, status())
            self._reply(404, {"error": "unknown path"})

        def do_POST(self):
            if not self._authorized():
                return self._reply(401, {"error": "missing or wrong token"})
            if self.path != "/run":
                return self._reply(404, {"error": "unknown path"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._reply(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                code = body["code"]
                if not isinstance(code, str):
                    raise ValueError("code must be a string")
                timeout = float(body.get("timeout", 600))
            except Exception as e:
                return self._reply(400, {"error": "bad request: %s" % e})
            req = _Request(code, body.get("screenshot"))
            _queue.put(req)
            if not req.done.wait(timeout):
                req.abandoned = True
                if not req.started:
                    return self._reply(504, {"status": "timeout", "output": "not started within %gs; dropped" % timeout})
                return self._reply(504, {"status": "timeout",
                                         "output": "still running in ParaView after %gs; its result will be discarded" % timeout})
            self._reply(200, req.result)

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def _start_listener(listen, token):
    host, _, port = listen.rpartition(":")
    srv = _make_server(host or "127.0.0.1", int(port), token)
    th = threading.Thread(target=srv.serve_forever, name="pvbridge-listener", daemon=True)
    th.start()
    _state.update(server=srv, server_thread=th, listen="%s:%s" % (host, port))


def _stop_listener():
    srv = _state.get("server")
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    while True:   # fail anything still queued
        try:
            req = _queue.get_nowait()
        except queue.Empty:
            break
        req.result = {"status": "error", "seconds": 0, "output": "pvbridge stopped"}
        req.done.set()
    _state.update(server=None, server_thread=None, listen=None)


# --------------------------------------------------------------------------- macOS App Nap

def _app_nap(disable):
    """While pvbridge runs, tell macOS this app is doing user-initiated work, so App Nap
    doesn't throttle the timer when ParaView isn't the frontmost app (requests then
    waited up to seconds). Uses NSProcessInfo through the Objective-C runtime; no-op
    elsewhere."""
    if sys.platform != "darwin":
        return
    import ctypes
    import ctypes.util
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send = objc.objc_msgSend
        sel = objc.sel_registerName
        send.restype = ctypes.c_void_p
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        info = send(objc.objc_getClass(b"NSProcessInfo"), sel(b"processInfo"))
        if disable and _state.get("activity") is None:
            send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
            reason = send(objc.objc_getClass(b"NSString"), sel(b"stringWithUTF8String:"), b"pvbridge serving commands")
            # NSActivityUserInitiatedAllowingIdleSystemSleep: no App Nap; the Mac may still sleep.
            send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p]
            token = send(info, sel(b"beginActivityWithOptions:reason:"), 0x00FFFFFF & ~(1 << 20), reason)
            send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            send(token, sel(b"retain"))
            _state["activity"] = token
        elif not disable and _state.get("activity") is not None:
            send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            send(info, sel(b"endActivity:"), _state["activity"])
            send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            send(_state["activity"], sel(b"release"))
            _state["activity"] = None
    except Exception as e:
        _log("could not change App Nap: %s" % e)


# --------------------------------------------------------------------------- start / stop

def start(interval_ms=INTERVAL_MS, fresh_namespace=True, listen=DEFAULT_LISTEN, token=None):
    """Start (or restart) the bridge on the active view.

    listen: "host:port" for the network listener ("0.0.0.0:9020" for other
    machines), or None for the file mailbox only. token: defaults to
    PVBRIDGE_TOKEN (environment or ~/.pvbridge/keys.env)."""
    from paraview.simple import GetActiveView
    from vtkmodules.util.misc import calldata_type
    from vtkmodules.util.vtkConstants import VTK_INT
    stop(quiet=True)
    _setup_dirs()
    if fresh_namespace or not ns:
        _new_namespace()
    view = GetActiveView()
    if view is None:
        raise RuntimeError("no active view: click in a render view, then start() again")
    iren = view.GetRenderWindow().GetInteractor()
    if iren is None:
        raise RuntimeError("the active view has no interactor; pick a render view")
    cb = calldata_type(VTK_INT)(_tick)
    _state.update(iren=iren, cb=cb, tag=iren.AddObserver("TimerEvent", cb), view=view.GetXMLLabel())
    _state["tid"] = iren.CreateRepeatingTimer(int(interval_ms))
    where = "mailbox %s" % ROOT
    if listen:
        tok = token or _read_token()
        if not tok:
            _log("no PVBRIDGE_TOKEN (environment or %s): network listener off" % os.path.join(ROOT, "keys.env"))
        else:
            try:
                _start_listener(listen, tok)
                _state["token"] = tok
                where += " and http://%s" % _state["listen"]
            except OSError as e:
                _log("network listener on %s failed: %s" % (listen, e))
    _app_nap(disable=True)
    _log("listening on %s every %d ms (timer %d). pvbridge.stop() to stop."
         % (where, interval_ms, _state["tid"]))


def stop(quiet=False):
    iren = _state.get("iren")
    if iren is not None:
        try:
            iren.DestroyTimer(_state["tid"])
            iren.RemoveObserver(_state["tag"])
        except Exception:
            pass
        if not quiet:
            _log("stopped after %d commands" % _state["count"])
    _stop_listener()
    _app_nap(disable=False)
    _state.update(iren=None, tid=None, tag=None, busy=False, token=None)


def status():
    return {"running": _state["iren"] is not None, "timer": _state["tid"],
            "commands": _state["count"], "mailbox": ROOT, "listen": _state["listen"],
            "queue_length": _queue.qsize(), "busy": _state["busy"], "app_nap_off": _state["activity"] is not None,
            "jobs": jobs()}
