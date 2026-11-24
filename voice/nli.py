#!/usr/bin/env python3
"""nli -- Part 1 of SPEC-pvlink-voice.md: the natural-language interface to ParaView and the DM.

Text in, reply and status out. ParaView is reached through pvbridge's network
listener (PVBRIDGE_URL, default http://127.0.0.1:9020; token PVBRIDGE_TOKEN from
the environment or ~/.pvbridge/keys.env). The model is any entry in models.json.

    nli.py -m sn-deepseek                 # typed conversation
    nli.py -m sn-deepseek --script turns.txt   # one request per line

Each request: snapshot of ParaView's state -> model with the snapshot and the
request -> tool calls -> snapshot again. The request is one labelled step on
ParaView's undo stack. Standard library only.
"""
import argparse
import ast
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pvagent  # noqa: E402  (also loads ~/.pvbridge/keys.env into the environment)

PVLINK_PY = os.path.join(HERE, "pvlink.py")
SYSTEM_PROMPT = os.path.join(HERE, "system_prompt_pvlink.md")
DEFAULT_CONFIG = {
    "bridge_url": os.environ.get("PVBRIDGE_URL", "http://127.0.0.1:9020"),
    "dmlink_plugin": os.path.expanduser("~/Documents/GitHub/DataManager/paraview/DMLink.py"),
    "pvlink": {"institution": "GDA", "project": "voice", "dm_host": "127.0.0.1", "dm_port": 9010,
               "data_roots": [os.path.expanduser("~/ParaViewPlugins/out"), os.path.expanduser("~/ParaViewPlugins/data")]},
    "history_turns": 6,
    # ParaView plugins to load with the helpers (their filters become usable by name),
    # and notes about them added to the model's instructions.
    "plugins": [os.path.expanduser("~/ParaViewPlugins/MolarPointSampler.py")],
    "filter_notes": os.path.join(HERE, "filters.md"),
}
ACTION_WORDS = re.compile(r"\b(added|created|coloured|colored|set|changed|removed|hid|hidden|showed|shown|moved|made|"
                          r"switched|renamed|updated|contoured|sliced|clipped|undid|undone|loaded|opened|applied|"
                          r"replaced|put|swept|repeated|extended|played|cycled|animated|generated|computed|rescaled|now (?:shows|has|is))\b", re.I)
QUESTION_START = re.compile(r"\s*(what|which|where|when|why|how|who|is|are|does|do|can|could|show me|tell me|list|describe)\b", re.I)
NO_CHANGE_WORDS = re.compile(r"\b(already|didn't|did not|no change|nothing|not need|unchanged|wasn't|isn't|couldn't|"
                             r"could not|can't|cannot|unable|failed|no such|doesn't|does not)\b", re.I)


class Busy(Exception):
    pass


class Cancelled(Exception):
    pass


class BridgeError(Exception):
    pass


class Bridge:
    """Client for pvbridge's HTTP listener (SPEC 6.3)."""

    def __init__(self, url, token=None):
        self.url = url.rstrip("/")
        self.token = token or os.environ.get("PVBRIDGE_TOKEN", "")
        if not self.token:
            raise BridgeError("no PVBRIDGE_TOKEN (environment or ~/.pvbridge/keys.env)")

    def run(self, code, screenshot=False, timeout=300):
        body = {"code": code, "timeout": timeout}
        if screenshot:
            body["screenshot"] = True
        req = urllib.request.Request(self.url + "/run", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(req, timeout=timeout + 15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                res = json.loads(e.read())
            except Exception:
                res = {}
            if e.code == 504:
                return {"status": "error", "output": "TIMEOUT: " + res.get("output", "")}
            raise BridgeError("pvbridge HTTP %d: %s" % (e.code, res.get("error") or res.get("output") or ""))
        except (urllib.error.URLError, OSError) as e:
            raise BridgeError("cannot reach pvbridge at %s: %s" % (self.url, e))

    def value(self, expr, timeout=120):
        """Evaluate an expression in ParaView and return the Python value (literal)."""
        res = self.run(expr, timeout=timeout)
        if res["status"] == "error":
            raise BridgeError(res["output"])
        out = res["output"].strip().splitlines()
        # DMLink/VTK messages may follow the value; the value is the first line that parses.
        for line in out:
            try:
                return ast.literal_eval(line)
            except Exception:
                continue
        raise BridgeError("no value in: %r" % res["output"][:300])

    def text(self, code, timeout=120):
        """Run code that prints; return its output (status must not be error)."""
        res = self.run(code, timeout=timeout)
        if res["status"] == "error":
            raise BridgeError(res["output"])
        return res["output"]


class Session:
    """One conversation with ParaView (SPEC 8)."""

    def __init__(self, model_name, config=None, verbose=False):
        self.cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        for k, v in (config or {}).items():
            if isinstance(v, dict) and isinstance(self.cfg.get(k), dict):
                self.cfg[k].update(v)
            else:
                self.cfg[k] = v
        models = pvagent.load_models()
        if model_name not in models:
            raise SystemExit("unknown model %r; models.json has: %s" % (model_name, ", ".join(models)))
        self.system, self._prompt_sig = self._read_system()
        self.model_name = model_name
        self.backend = pvagent.BACKENDS[models[model_name]["backend"]](models[model_name], self.system)
        self.bridge = Bridge(self.cfg["bridge_url"])
        self.verbose = verbose
        self.lock = threading.Lock()
        self.cancel_requested = False
        self.progress = {"state": "idle"}
        self.trace = []

    # ------------------------------------------------------------------ instructions
    def _prompt_files(self):
        files = [SYSTEM_PROMPT]
        notes = self.cfg.get("filter_notes")
        if notes and os.path.exists(notes):
            files.append(notes)
        return files

    def _read_system(self):
        parts, sig = [], []
        for path in self._prompt_files():
            with open(path) as f:
                parts.append(f.read())
            sig.append((path, os.path.getmtime(path)))
        return "\n\n".join(parts), sig

    def refresh_instructions(self):
        """Re-read the instruction files if they changed since the session started,
        so edits take effect without restarting talk.py / nli.py."""
        sig = [(p, os.path.getmtime(p)) for p in self._prompt_files()]
        if sig == self._prompt_sig:
            return False
        self.system, self._prompt_sig = self._read_system()
        b = self.backend
        if isinstance(getattr(b, "system", None), list):          # Anthropic: cached system block
            b.system[0]["text"] = self.system
        elif getattr(b, "messages", None) and b.messages[0].get("role") == "system":   # OpenAI-compatible
            b.messages[0]["content"] = self.system
            b.system = self.system
        return True

    # ------------------------------------------------------------------ helpers in ParaView
    def ensure_helpers(self):
        import hashlib
        with open(PVLINK_PY, "rb") as f:
            digest = hashlib.sha1(f.read()).hexdigest()[:12]
        ok = self.bridge.value("'pvl_snapshot' in globals() and 'DMLinkSender' in globals() "
                               "and globals().get('_PVLINK_HASH') == %r" % digest)
        if ok:
            if not getattr(self, "_configured", False):
                self.cfg["pvlink"].setdefault("plugins", list(self.cfg.get("plugins", [])))
                self.cfg["pvlink"].setdefault("dmlink_plugin", self.cfg["dmlink_plugin"])
                self.bridge.text("pvl_config(**%r)" % self.cfg["pvlink"])
                self._configured = True
            return
        plugin = self.cfg["dmlink_plugin"]
        code = ("import paraview.simple as _s\n"
                "if 'DMLinkSender' not in dir(_s):\n"
                "    LoadPlugin(%r, ns=globals())\n"
                "elif 'DMLinkSender' not in globals():\n"
                "    DMLinkSender, DMLinkDomain = _s.DMLinkSender, _s.DMLinkDomain\n" % plugin)
        with open(PVLINK_PY) as f:
            code += f.read()
        self.cfg["pvlink"].setdefault("plugins", list(self.cfg.get("plugins", [])))
        self.cfg["pvlink"].setdefault("dmlink_plugin", self.cfg["dmlink_plugin"])
        for plugin in self.cfg.get("plugins", []):
            code += ("\ntry:\n    LoadPlugin(%r, ns=globals())\nexcept Exception as _e:\n"
                     "    print('could not load plugin %s: %%s' %% _e)\n" % (plugin, os.path.basename(plugin)))
        code += "\npvl_config(**%r)\n_PVLINK_HASH = %r\n'helpers loaded'" % (self.cfg["pvlink"], digest)
        res = self.bridge.run(code)
        if res["status"] == "error":
            raise BridgeError("loading the pvlink helpers failed:\n" + res["output"])
        self._configured = True

    def snapshot(self):
        return self.bridge.text("print(pvl_snapshot())").strip()

    @staticmethod
    def _comparable(snap):
        # The active object changes with every helper call; it isn't a change by itself.
        return "\n".join(l for l in snap.splitlines() if not l.startswith("Active object"))

    # ------------------------------------------------------------------ the tool
    def _tool(self, args):
        if self.cancel_requested:
            raise Cancelled()
        self.progress.update(state="running", step=self.progress.get("step", 0) + 1)
        t0 = time.time()
        try:
            res = self.bridge.run(args.get("code", ""), screenshot=bool(args.get("screenshot")))
            self.tool_seconds += time.time() - t0
        except BridgeError as e:
            return False, "ERROR: %s" % e, None
        text = res.get("output", "").strip()
        if res["status"] == "error":
            text = "ERROR\n" + text
        elif res["status"] == "warn":
            text = "WARNINGS were logged (see below)\n" + text
        if not text:
            text = ("(no output: nothing was printed and the last line was not a bare expression, "
                    "so no value came back)")
        if len(text) > 6000:
            text = text[:3000] + "\n...[%d chars cut]...\n" % (len(text) - 6000) + text[-3000:]
        png = None
        if res.get("png"):
            import base64
            png = base64.b64decode(res["png"])
        return res["status"] != "error", text, png

    def _log(self, kind, text):
        self.trace.append((kind, text))
        if self.verbose:
            tag = {"code": ">>> ", "result": "<<< "}.get(kind, "")
            sys.stderr.write("".join(tag + l + "\n" for l in text.splitlines()) or tag + "\n")

    # ------------------------------------------------------------------ history
    def _request_indices(self):
        msgs = self.backend.messages
        return [i for i, m in enumerate(msgs) if m["role"] == "user" and isinstance(m["content"], str)
                and m["content"].startswith(("Current state:", "Request:"))]

    def _trim_history(self, full_message, request_text):
        msgs = self.backend.messages
        for m in msgs:   # drop the snapshot from the stored copy of this request
            if m["role"] == "user" and m["content"] == full_message:
                m["content"] = "Request: " + request_text
        idx = self._request_indices()
        keep = self.cfg["history_turns"]
        if len(idx) > keep:
            cut = idx[-keep]
            head = msgs[:1] if msgs and msgs[0]["role"] == "system" else []
            self.backend.messages = head + msgs[cut:]

    # ------------------------------------------------------------------ API
    def ask(self, text, request_id=None):
        if not self.lock.acquire(blocking=False):
            raise Busy("a request is already running")
        try:
            return self._ask(text, request_id)
        finally:
            self.lock.release()

    def _ask(self, text, request_id):
        t0 = time.time()
        self.cancel_requested = False
        self.trace = []
        self.tool_seconds = 0.0
        self.progress = {"state": "thinking", "request_id": request_id, "step": 0}
        self.refresh_instructions()
        self.ensure_helpers()
        before = self.snapshot()
        t_model0 = time.time()
        message = "Current state:\n%s\n\nRequest: %s" % (before, text)
        saved = list(self.backend.messages)
        self.bridge.text("_pvl_begin(%r)" % ("voice: " + text))
        cancelled = False
        try:
            reply, stats = self.backend.turn(message, self._tool, self._log)
        except Cancelled:
            cancelled = True
            self.backend.messages = saved
            reply, stats = None, {"tool_calls": self.progress.get("step", 0), "tool_errors": 0, "usage": {}}
        except Exception as e:   # model or endpoint failure: report it, keep the session usable
            self.backend.messages = saved
            failed = str(e)
            if len(failed) > 300:
                failed = failed[:300] + "..."
            reply, stats = None, {"tool_calls": self.progress.get("step", 0), "tool_errors": 1, "usage": {},
                                  "model_error": failed}
        finally:
            model_and_tools = time.time() - t_model0
            self.bridge.text("_pvl_end()")
        after = self.snapshot()
        changed = self._comparable(after) != self._comparable(before)
        warnings = [t for k, t in self.trace if k == "result" and ("VTK ERROR" in t or "VTK WARNING" in t)]
        false_success = False
        if cancelled:
            reply = "Stopped." + (" Some changes were already made." if changed else " Nothing was changed.")
        elif reply is None:
            reply = "Sorry, the language model failed%s." % (", after making some changes" if changed else "; nothing was changed")
        else:
            self._trim_history(message, text)
            asked = reply.rstrip().endswith("?")
            question = text.rstrip().endswith("?") or bool(QUESTION_START.match(text))
            if (not changed and not asked and not question and ACTION_WORDS.search(reply or "")
                    and not NO_CHANGE_WORDS.search(reply or "")):
                false_success = True
                reply = "I didn't actually change anything. (The model said: %s)" % reply
        items = self.bridge.value("pvl_items()")
        self.progress = {"state": "cancelled" if cancelled else "done", "request_id": request_id,
                         "step": stats.get("tool_calls", 0)}
        return {"request_id": request_id, "reply": reply, "changed": changed,
                "pending_question": (reply or "").rstrip().endswith("?"), "items": items,
                "warnings": warnings, "false_success": false_success,
                "seconds": round(time.time() - t0, 2),
                "model_seconds": round(model_and_tools - self.tool_seconds, 2),
                "paraview_seconds": round(self.tool_seconds, 2), "tool_calls": stats.get("tool_calls", 0),
                "tool_errors": stats.get("tool_errors", 0), "usage": stats.get("usage", {}),
                "model_error": stats.get("model_error")}

    def cancel(self):
        self.cancel_requested = True

    def reset(self):
        self.backend.reset()

    def vocabulary(self):
        self.ensure_helpers()
        return self.bridge.value("pvl_vocabulary()")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--model", default=os.environ.get("PVVOICE_MODEL", "sn-deepseek"))
    ap.add_argument("--script", help="file with one request per line")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the code the model runs")
    ap.add_argument("--json", action="store_true", help="print each full result as JSON")
    ap.add_argument("--dm", help="DataManager host[:port] that DMLink sends to (default 127.0.0.1:9010)")
    ap.add_argument("--project", help="Institution/Project for the scratch project (default GDA/voice)")
    a = ap.parse_args()
    cfg = {"pvlink": {}}
    if a.dm:
        host, _, port = a.dm.partition(":")
        cfg["pvlink"].update(dm_host=host, dm_port=int(port or 9010))
    if a.project:
        inst, _, proj = a.project.partition("/")
        cfg["pvlink"].update(institution=inst, project=proj)
    s = Session(a.model, config=cfg, verbose=a.verbose)
    print("model %s; DMLink -> %s:%s, project %s/%s. Blank line or Ctrl-D quits." % (
        a.model, s.cfg["pvlink"]["dm_host"], s.cfg["pvlink"]["dm_port"], s.cfg["pvlink"]["institution"], s.cfg["pvlink"]["project"]))
    lines = open(a.script).read().splitlines() if a.script else None
    while True:
        if lines is not None:
            if not lines:
                break
            text = lines.pop(0).strip()
            if not text:
                continue
            print("you> " + text)
        else:
            try:
                text = input("you> ").strip()
            except EOFError:
                break
            if not text:
                break
        r = s.ask(text)
        if a.json:
            print(json.dumps(r, indent=1))
        else:
            flags = [f for f in ("changed", "pending_question", "false_success") if r[f]]
            print("pv>  %s\n     [%.1fs, %d calls%s]  headset: %s" % (
                r["reply"], r["seconds"], r["tool_calls"], ", " + ", ".join(flags) if flags else "",
                ", ".join("%s%s%s" % (i["name"], "" if i["shown"] else " (hidden)",
                                       " by " + i["colour_by"] if i["colour_by"] else "") for i in r["items"]) or "empty"))


if __name__ == "__main__":
    main()
