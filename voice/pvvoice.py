#!/usr/bin/env python3
"""pvvoice -- talk to a live ParaView GUI.

    pvvoice.py                      # default model "opus", local Whisper, speaks replies
    pvvoice.py -m local             # any model name from models.json
    pvvoice.py --stt http://WINDOWS-BOX-IP:8000/v1   # Whisper on another machine
    pvvoice.py --no-speak --mic "MacBook Pro Microphone"

Push to talk: press Enter to start recording, Enter again to stop.
Or type a request instead and press Enter.
Say or type "start over" to clear the conversation; Ctrl-D or "quit" to exit.

Speech-to-text:
  local (default)  openai-whisper in a persistent worker (stt_whisper.py) run by
                   --whisper-python (default /usr/bin/python3, which has it here)
  http://...       an OpenAI-compatible /v1/audio/transcriptions server, e.g.
                   speaches / faster-whisper-server on the GPU box
Text-to-speech: macOS `say`.
Recording: ffmpeg with AVFoundation (the terminal app needs microphone access).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pvagent  # noqa: E402

VOCAB = ("ParaView, colour map, colormap, isosurface, contour, slice, clip, threshold, "
         "smoke, Weight, Velocity, ParticleId, RTData, viridis, opacity, legend, "
         "orientation axes, timestep, Point Gaussian")


# --------------------------------------------------------------------------- recording

class Recorder:
    def __init__(self, mic):
        self.mic = mic
        self.p = None

    def start(self, path):
        self.path = path
        self.p = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
             "-i", ":" + self.mic, "-ac", "1", "-ar", "16000", "-y", path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def stop(self):
        try:
            _, err = self.p.communicate(b"q", timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            _, err = self.p.communicate()
        err = (err or b"").decode(errors="replace")
        ok = os.path.exists(self.path) and os.path.getsize(self.path) > 2000
        return ok, err


# --------------------------------------------------------------------------- speech to text

class LocalWhisper:
    def __init__(self, python, model):
        self.p = subprocess.Popen([python, os.path.join(HERE, "stt_whisper.py"), model],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        ready = self.p.stdout.readline()
        if not ready:
            raise SystemExit("Whisper worker failed to start (is `whisper` installed for %s?)" % python)

    def __call__(self, wav, prompt):
        self.p.stdin.write(json.dumps({"wav": wav, "prompt": prompt}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())["text"]


class HttpWhisper:
    """OpenAI-compatible /audio/transcriptions (multipart/form-data)."""

    def __init__(self, base_url, model):
        self.url = base_url.rstrip("/") + "/audio/transcriptions"
        self.model = model

    def __call__(self, wav, prompt):
        b = uuid.uuid4().hex
        parts = []
        for k, v in (("model", self.model), ("language", "en"), ("prompt", prompt)):
            parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode())
        with open(wav, "rb") as f:
            audio = f.read()
        parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                      'Content-Type: audio/wav\r\n\r\n' % b).encode() + audio + b"\r\n")
        parts.append(("--%s--\r\n" % b).encode())
        req = urllib.request.Request(self.url, data=b"".join(parts),
                                     headers={"content-type": "multipart/form-data; boundary=" + b})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read()).get("text", "").strip()


# --------------------------------------------------------------------------- speaking

def speak(text, voice=None, enabled=True):
    if not enabled or not text:
        return
    text = re.sub(r"[`*_#>]", "", text)[:600]
    cmd = ["say"] + (["-v", voice] if voice else []) + [text]
    subprocess.run(cmd)


# --------------------------------------------------------------------------- main loop

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--model", default=os.environ.get("PVVOICE_MODEL", "opus"))
    ap.add_argument("--stt", default="local", help='"local" or the /v1 URL of a Whisper server')
    ap.add_argument("--stt-model", default="base.en",
                    help="Whisper model (local: base.en, small.en, ...; http: the server's model id)")
    ap.add_argument("--whisper-python", default="/usr/bin/python3")
    ap.add_argument("--mic", default="0", help="AVFoundation audio device index or name")
    ap.add_argument("--voice", default=None, help="macOS `say` voice")
    ap.add_argument("--no-speak", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true", help="don't show the code being run")
    a = ap.parse_args()

    agent = pvagent.Agent(a.model, verbose=not a.quiet)
    print("Starting speech-to-text...", flush=True)
    stt = (LocalWhisper(a.whisper_python, a.stt_model) if a.stt == "local"
           else HttpWhisper(a.stt, a.stt_model))
    rec = Recorder(a.mic)
    tmpdir = tempfile.mkdtemp(prefix="pvvoice-")
    print("pvvoice ready (model %s). Enter = talk, or type a request. Ctrl-D quits." % a.model)

    while True:
        try:
            typed = input("\n[Enter to talk] ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if typed:
            text = typed
        else:
            wav = os.path.join(tmpdir, "%d.wav" % time.time())
            rec.start(wav)
            try:
                input("  recording... Enter to stop ")
            except (EOFError, KeyboardInterrupt):
                pass
            ok, err = rec.stop()
            if not ok:
                print("  (no audio recorded%s)" % (": " + err.strip() if err.strip() else
                      "; does this terminal have microphone access?"))
                continue
            t0 = time.time()
            text = stt(wav, VOCAB)
            print("  heard (%.1fs): %s" % (time.time() - t0, text or "(nothing)"))
            if not text:
                continue
        low = text.lower().strip(" .!?")
        if low in ("quit", "exit", "goodbye"):
            break
        if low in ("start over", "reset", "new conversation"):
            agent.reset()
            print("  (conversation cleared)")
            speak("Starting over.", a.voice, not a.no_speak)
            continue
        try:
            reply, stats = agent.ask(text)
        except Exception as e:
            reply, stats = "Sorry, that failed: %s" % e, {"seconds": 0, "tool_calls": 0}
        print("pv> %s   [%.1fs, %d calls]" % (reply, stats["seconds"], stats["tool_calls"]))
        speak(reply, a.voice, not a.no_speak)


if __name__ == "__main__":
    main()
