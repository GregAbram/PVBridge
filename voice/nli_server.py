#!/usr/bin/env python3
"""nli_server -- the natural-language interface as a network service (SPEC-pvlink-voice.md 9),
with speech in and out, for front ends that aren't on this machine (the PVAR headset).

    nli_server.py -m sn-gptoss120 --dm 10.0.0.49 [--listen 0.0.0.0:9030]
                  [--stt local|http://HOST:PORT/v1] [--voice Samantha]

Every request needs "Authorization: Bearer <NLI_TOKEN>" (environment or ~/.pvbridge/keys.env;
created on first start if missing).

    POST /ask                 JSON {"text": "...", "request_id"?: "...", "speak"?: true}
                              or a raw WAV body (Content-Type audio/wav), with ?speak=1&request_id=...
                           -> JSON {request_id, heard, reply, changed, pending_question, items, warnings,
                                    seconds, stt_seconds, audio_url?, cancelled?, ignored?}
    GET  /audio/<id>.wav      the reply as speech (when speak was asked for)
    GET  /progress/<request_id>   {state: thinking|running|done|cancelled|failed|unknown, step}
    POST /cancel              stop the running request after its current step
    GET  /vocabulary          object and variable names (for speech recognition hints)
    POST /reset               forget the conversation (ParaView is untouched)
    GET  /status              model, DataManager, busy

Spoken input: an utterance containing the word "cancel" is discarded; "new conversation"
clears the model's memory. Standard library only (plus the Whisper worker for local STT).
"""
import argparse
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nli          # noqa: E402  (loads ~/.pvbridge/keys.env into the environment)
import talk         # noqa: E402  (vocabulary hints, cancel word)
from pvvoice import LocalWhisper, HttpWhisper  # noqa: E402

KEYS = os.path.expanduser("~/.pvbridge/keys.env")
MAX_BODY = 20 * 1024 * 1024


def nli_token():
    tok = os.environ.get("NLI_TOKEN", "").strip()
    if tok:
        return tok
    tok = secrets.token_urlsafe(32)
    with open(KEYS, "a") as f:
        f.write("NLI_TOKEN=%s\n" % tok)
    os.chmod(KEYS, 0o600)
    os.environ["NLI_TOKEN"] = tok
    print("created NLI_TOKEN in %s" % KEYS, flush=True)
    return tok


class Service:
    def __init__(self, a):
        cfg = {"pvlink": {}}
        if a.config:
            # ParaView-side settings for a ParaView on another machine (paths there, plugins, ...).
            with open(os.path.expanduser(a.config)) as f:
                cfg = json.load(f)
            cfg.setdefault("pvlink", {})
            print("ParaView settings from %s" % a.config, flush=True)
        if a.dm:
            host, _, port = a.dm.partition(":")
            cfg["pvlink"].update(dm_host=host, dm_port=int(port or 9010))
        self.session = nli.Session(a.model, config=cfg, verbose=a.verbose)
        self.model = a.model
        self.voice = a.voice
        print("starting speech-to-text (%s)..." % a.stt, flush=True)
        self.stt = LocalWhisper(a.whisper_python, a.stt_model) if a.stt == "local" else HttpWhisper(a.stt, a.stt_model)
        self.stt_lock = threading.Lock()          # the local worker handles one at a time
        self.audio_dir = tempfile.mkdtemp(prefix="nli-audio-")
        self.hints, self.hints_at = "", 0.0
        self.last = {}                            # request_id -> final progress

    # ------------------------------------------------------------------ speech
    def transcribe(self, wav_bytes):
        path = os.path.join(self.audio_dir, "in-%d.wav" % (time.time() * 1000))
        with open(path, "wb") as f:
            f.write(wav_bytes)
        if time.time() - self.hints_at > 10:
            self.hints, self.hints_at = talk.vocabulary_prompt(self.session), time.time()
        t0 = time.time()
        with self.stt_lock:
            text = self.stt(path, self.hints).strip()
        try:
            os.remove(path)
        except OSError:
            pass
        return text, round(time.time() - t0, 2)

    def speak(self, text):
        """Render text as a WAV (16-bit mono 22.05 kHz) with macOS `say`; returns its id."""
        if not text:
            return None
        aid = "%d%s" % (time.time() * 1000, secrets.token_hex(3))
        path = os.path.join(self.audio_dir, aid + ".wav")
        cmd = ["say", "-o", path, "--data-format=LEI16@22050"] + (["-v", self.voice] if self.voice else [])
        subprocess.run(cmd + [re.sub(r"[`*_#>]", "", text)[:1000]], check=True, timeout=60)
        files = sorted(f for f in os.listdir(self.audio_dir) if f.endswith(".wav") and not f.startswith("in-"))
        for old in files[:-30]:                    # keep the last 30 replies
            try:
                os.remove(os.path.join(self.audio_dir, old))
            except OSError:
                pass
        return aid

    # ------------------------------------------------------------------ requests
    def ask(self, text, heard, stt_seconds, request_id, speak):
        out = {"request_id": request_id, "heard": heard, "stt_seconds": stt_seconds}
        if heard is not None and not text:
            out.update(ignored=True, reply="")
            return 200, out
        if talk.CANCEL_WORD.search(text):
            out.update(cancelled=True, reply="")
            return 200, out
        if text.lower().strip(" .!?") in ("new conversation", "forget that conversation"):
            self.session.reset()
            out.update(reply="Okay, fresh conversation.", changed=False)
        else:
            try:
                r = self.session.ask(text, request_id=request_id)
            except nli.Busy:
                return 409, {"error": "busy: another request is running", "request_id": request_id}
            except Exception as e:
                r = {"reply": "Sorry, that failed.", "error": str(e)[:500], "changed": False,
                     "pending_question": False, "items": [], "warnings": [], "seconds": 0}
            out.update(r)
        self.last[request_id] = dict(self.session.progress)
        if speak and out.get("reply"):
            try:
                out["audio_url"] = "/audio/%s.wav" % self.speak(out["reply"])
            except Exception as e:
                out["audio_error"] = str(e)[:200]
        return 200, out

    def progress(self, request_id):
        p = self.session.progress
        if p.get("request_id") == request_id:
            return p
        return self.last.get(request_id, {"state": "unknown"})


def make_handler(svc, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "nli"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _ok(self):
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), ("Bearer " + token).encode()):
                return True
            self._send(401, {"error": "missing or wrong token"})
            return False

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request too large")
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            if not self._ok():
                return
            url = urllib.parse.urlparse(self.path)
            if url.path == "/status":
                return self._send(200, {"model": svc.model, "busy": svc.session.lock.locked(),
                                        "dm": "%s:%s" % (svc.session.cfg["pvlink"]["dm_host"], svc.session.cfg["pvlink"]["dm_port"]),
                                        "progress": svc.session.progress})
            if url.path == "/vocabulary":
                try:
                    return self._send(200, svc.session.vocabulary())
                except Exception as e:
                    return self._send(502, {"error": str(e)[:300]})
            m = re.match(r"^/progress/(.+)$", url.path)
            if m:
                return self._send(200, svc.progress(urllib.parse.unquote(m.group(1))))
            m = re.match(r"^/audio/([0-9a-f]+)\.wav$", url.path)
            if m:
                path = os.path.join(svc.audio_dir, m.group(1) + ".wav")
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        return self._send(200, f.read(), "audio/wav")
                return self._send(404, {"error": "no such audio"})
            self._send(404, {"error": "unknown path"})

        def do_POST(self):
            if not self._ok():
                return
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            try:
                body = self._body()
            except ValueError as e:
                return self._send(413, {"error": str(e)})
            if url.path == "/ask":
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype in ("audio/wav", "audio/x-wav", "audio/wave", "application/octet-stream"):
                    if not body:
                        return self._send(400, {"error": "empty audio"})
                    rid = (q.get("request_id") or [secrets.token_hex(4)])[0]
                    speak = (q.get("speak") or ["0"])[0] in ("1", "true", "yes")
                    try:
                        heard, st = svc.transcribe(body)
                    except Exception as e:
                        return self._send(500, {"error": "speech-to-text failed: %s" % e, "request_id": rid})
                    code, out = svc.ask(heard, heard, st, rid, speak)
                    return self._send(code, out)
                try:
                    req = json.loads(body or b"{}")
                    text = str(req["text"])
                except Exception as e:
                    return self._send(400, {"error": "bad request: %s" % e})
                rid = str(req.get("request_id") or secrets.token_hex(4))
                code, out = svc.ask(text, None, None, rid, bool(req.get("speak")))
                return self._send(code, out)
            if url.path == "/cancel":
                svc.session.cancel()
                return self._send(200, {"ok": True})
            if url.path == "/reset":
                svc.session.reset()
                return self._send(200, {"ok": True})
            self._send(404, {"error": "unknown path"})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--model", default=os.environ.get("PVVOICE_MODEL", "sn-gptoss120"))
    ap.add_argument("--dm", help="DataManager host[:port] that DMLink sends to, as ParaView's machine sees it")
    ap.add_argument("--config", help="JSON of ParaView-side settings merged over the defaults "
                    "(dmlink_plugin, plugins, pvlink: {pvbatch, bake_script, dm_probe, vis_dir, data_roots, ...}); "
                    "for a ParaView on another machine, e.g. remote-ls6.json")
    ap.add_argument("--listen", default="0.0.0.0:9030", help="address:port to serve on")
    ap.add_argument("--stt", default="local", help='"local" or the /v1 URL of a Whisper server')
    ap.add_argument("--stt-model", default="base.en")
    ap.add_argument("--whisper-python", default="/usr/bin/python3")
    ap.add_argument("--voice", default=None, help="macOS `say` voice for spoken replies")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    token = nli_token()
    svc = Service(a)
    host, _, port = a.listen.rpartition(":")
    srv = ThreadingHTTPServer((host or "0.0.0.0", int(port)), make_handler(svc, token))
    srv.daemon_threads = True
    print("nli service on http://%s:%s, model %s, DMLink -> %s:%s" % (
        host or "0.0.0.0", port, a.model, svc.session.cfg["pvlink"]["dm_host"], svc.session.cfg["pvlink"]["dm_port"]), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
