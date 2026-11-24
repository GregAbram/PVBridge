#!/usr/bin/env python3
"""talk -- speak to the natural-language PVLink interface (Part 2, desktop voice front end).

    talk.py -m sn-gptoss120 --dm 10.0.0.49
    talk.py --stt http://WINDOWS-BOX-IP:8000/v1 ...     # Whisper on another machine
    talk.py --no-speak --mic "MacBook Pro Microphone"

Hands-free (default): it listens all the time and acts on what follows the
trigger word: "Please, contour the wavelet at 123". Saying just "Please"
gives a tone, and your next sentence is the request. After the computer asks a
question, your next sentence (within 15 s) is the answer, no trigger word needed.
"Please stop" cuts a spoken reply short; "Please quit" exits. --wake picks another word. Tones: Tink
= heard you, Pop = working, Basso = something failed, Funk = cancelled.
An input containing the word "cancel" anywhere is discarded, not sent.

--ptt: push to talk instead (Enter to start, Enter to stop, or type a request).

Before each recording the current object and variable names are fetched from
ParaView and given to Whisper as hints, so "wavelet contour" or "RTData" are
recognized.

By default the Part 1 session runs in this process (nli.Session). With
--server it sends what it hears to a running nli_server.py instead, so this
and the headset (PVAR) share one conversation and take turns:
    talk.py --server                       # http://127.0.0.1:9030
    talk.py --server http://HOST:9030      # NLI_TOKEN from ~/.pvbridge/keys.env
Speech recognition and spoken replies stay on this machine either way.
"""
import argparse
import os
import subprocess
import sys
import re
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nli  # noqa: E402
from pvvoice import Recorder, LocalWhisper, HttpWhisper  # noqa: E402
from listen import Listener  # noqa: E402

BASE_VOCAB = ["ParaView", "Hey ParaView", "contour", "isosurface", "isovalue", "slice", "clip", "outline", "threshold",
              "colormap", "viridis", "headset", "sweep", "wavelet"]


class RemoteSession:
    """The parts of nli.Session that talk.py uses, over nli_server.py's HTTP API."""

    def __init__(self, url, token):
        import urllib.parse
        self.url, self.token = url.rstrip("/"), token
        self.cfg = {"pvlink": {"dm_host": "(server)", "dm_port": urllib.parse.urlparse(self.url).netloc}}
        st = self._call("GET", "/status")
        dm = st.get("dm", "?")
        host, _, port = dm.rpartition(":")
        self.cfg["pvlink"].update(dm_host=host, dm_port=port)
        self.model = st.get("model")

    def _call(self, method, path, body=None, timeout=30):
        import json
        import urllib.error
        import urllib.request
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}").get("error", "")
            except ValueError:
                err = ""
            if e.code == 409:
                raise RuntimeError("busy: the headset (or another client) has a request running") from None
            if e.code == 401:
                raise RuntimeError("the voice service refused the token (NLI_TOKEN)") from None
            raise RuntimeError("voice service HTTP %d: %s" % (e.code, err)) from None

    def ask(self, text):
        r = self._call("POST", "/ask", {"text": text}, timeout=600)
        for k, v in (("seconds", 0), ("tool_calls", 0), ("items", []), ("changed", False),
                     ("pending_question", False), ("false_success", False)):
            r.setdefault(k, v)
        if r.get("error") and not r.get("reply"):
            raise RuntimeError(r["error"])
        return r

    def vocabulary(self):
        return self._call("GET", "/vocabulary")

    def reset(self):
        self._call("POST", "/reset")


class Speaker:
    """`say` in the background, so the next push-to-talk can interrupt it."""

    def __init__(self, voice=None, enabled=True):
        self.voice, self.enabled, self.p = voice, enabled, None

    def say(self, text):
        self.stop()
        if not self.enabled or not text:
            return
        cmd = ["say"] + (["-v", self.voice] if self.voice else []) + [text.replace("*", "")[:600]]
        self.p = subprocess.Popen(cmd)

    def stop(self):
        if self.p is not None and self.p.poll() is None:
            self.p.terminate()
        self.p = None

    def speaking(self):
        return self.p is not None and self.p.poll() is None

    def wait(self):
        if self.p is not None:
            self.p.wait()

    @staticmethod
    def estimate(text):
        return len((text or "").split()) / 2.8      # seconds, at about 170 words a minute


def earcon(name):
    path = "/System/Library/Sounds/%s.aiff" % name
    if os.path.exists(path):
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fuzzy_wake(text, word, threshold=0.72):
    """Match the trigger word loosely in the first one to three words ("SuperRaview").
    Returns the rest of the text, or None."""
    import difflib
    target = re.sub(r"[^a-z]", "", word.lower())
    tokens = re.findall(r"[A-Za-z']+|[^A-Za-z']+", text)
    words_idx = [k for k, t in enumerate(tokens) if re.match(r"[A-Za-z']", t)]
    for n in (1, 2, 3):
        if len(words_idx) < n:
            break
        lead = "".join(tokens[k] for k in words_idx[:n]).lower().replace("'", "")
        for skip in ("hey", "ok", "okay", "hi"):
            if lead.startswith(skip) and len(lead) > len(skip) + 3:
                lead = lead[len(skip):]
        if difflib.SequenceMatcher(None, lead, target).ratio() >= threshold:
            rest = "".join(tokens[words_idx[n - 1] + 1:])
            # A short trigger word ("please") only counts loosely when a pause
            # follows it, so "Pleased to meet you" doesn't trigger.
            if len(target) < 8 and lead != target and not re.match(r"^\s*([,.!?:;]|$)", rest):
                return None
            return rest
    return None


def wake_regex(word):
    if word.lower().replace(" ", "") == "paraview":
        core = r"(?:para\W*view|pair\W*(?:of|a)\W*view|parra\W*view|para\W*vue|paraview)"
    else:
        core = r"\W*".join(re.escape(w) for w in word.split())
    return re.compile(r"^\W*(?:(?:hey|ok|okay|hi)\W+)?" + core + r"\b\W*", re.I)


CANCEL_WORD = re.compile(r"\bcancel(?:led|ed)?\b", re.I)   # anywhere in an input: discard it
STOP_WORDS = re.compile(r"^\W*(stop|be quiet|quiet|shut up|enough|cancel|never ?mind)\W*$", re.I)
QUIT_WORDS = re.compile(r"^\W*(quit|exit|goodbye|stop listening|go to sleep)\W*$", re.I)


def show(r):
    flags = [f for f in ("changed", "pending_question", "false_success") if r.get(f)]
    print("pv> %s\n    [%.1fs, %d calls%s]  headset: %s" % (
        r["reply"], r.get("seconds") or 0, r.get("tool_calls") or 0, ", " + ", ".join(flags) if flags else "",
        ", ".join("%s%s%s" % (i["name"], "" if i["shown"] else " (hidden)",
                               " by " + i["colour_by"] if i["colour_by"] else "") for i in r.get("items") or []) or "empty"), flush=True)


def ask(session, text):
    try:
        return session.ask(text)
    except Exception as e:
        print("  (error: %s)" % e, flush=True)
        busy = str(e).startswith("busy")
        return {"reply": "I'm still working on another request. Try again in a moment." if busy
                else "Sorry, that failed. The details are on the screen.", "seconds": 0, "tool_calls": 0,
                "items": [], "changed": False, "pending_question": False, "false_success": False, "failed": True}


def hands_free(a, session, stt, spk):
    wake = wake_regex(a.wake)
    lis = Listener(mic=a.mic, input_file=a.input_file, debug=a.debug)
    lis.start()
    print("Listening (trigger word: %s). Say \"%s, quit\" or press Ctrl-C to stop." % (a.wake, a.wake), flush=True)
    follow_until, hints, hints_at = 0.0, "", 0.0
    try:
        while True:
            wav = lis.get()
            if wav is None:
                if lis.error:
                    print("  (microphone stopped: %s)" % lis.error)
                break
            if time.time() - hints_at > 10:         # names change as the pipeline does
                hints, hints_at = vocabulary_prompt(session), time.time()
            text = stt(wav, hints).strip()
            if not text:
                continue
            now = time.time()
            m = wake.match(text)
            loose = None if m else fuzzy_wake(text, a.wake)
            if m or loose is not None:
                rest = (text[m.end():] if m else loose).strip(" ,.!?")
            elif now < follow_until:
                rest = text.strip(" ,.!?") if not spk.speaking() else ""
                if not rest:
                    continue
            else:
                if a.debug:
                    print("  (ignored: %s)" % text, flush=True)
                continue
            print("\n  heard: %s" % text, flush=True)
            if CANCEL_WORD.search(text):             # "cancel" anywhere: drop this input
                spk.stop()
                follow_until = 0
                print("  (cancelled; nothing sent)", flush=True)
                earcon("Funk")
                continue
            if not rest:                             # just the trigger word
                spk.stop()
                earcon("Tink")
                follow_until = time.time() + 8
                continue
            if STOP_WORDS.match(rest):
                spk.stop()
                follow_until = 0
                continue
            if QUIT_WORDS.match(rest):
                spk.stop()
                spk.say("Goodbye.")
                break
            spk.stop()
            earcon("Pop")
            lis.pause()                              # don't queue speech while working
            r = ask(session, rest)
            lis.resume()
            show(r)
            if r.get("failed"):
                earcon("Basso")
            spk.say(r["reply"])
            follow_until = time.time() + spk.estimate(r["reply"]) + 15 if r.get("pending_question") else 0
    except KeyboardInterrupt:
        print()
    finally:
        lis.stop()
        spk.wait() if a.input_file else spk.stop()


def vocabulary_prompt(session):
    """Whisper hint as a natural sentence (a bare word list makes Whisper drop or mangle
    the first word, e.g. the trigger word)."""
    try:
        words = session.vocabulary()
    except Exception:
        words = []
    presets = {"Viridis", "Inferno", "Plasma", "Cool to Warm", "Turbo"}
    generic = {"contour", "slice", "outline", "isosurface"}
    names = [w for w in words if w not in presets and w not in generic][:25]
    sentence = "The user talks to ParaView about"
    sentence += (" the " + ", ".join(names) if names else " the data")
    sentence += ", contours, isosurfaces, isovalues, slices, outlines and colormaps like Viridis."
    return sentence[:600]

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--model", default=os.environ.get("PVVOICE_MODEL", "sn-gptoss120"))
    ap.add_argument("--dm", help="DataManager host[:port] that DMLink sends to (default 127.0.0.1:9010)")
    ap.add_argument("--stt", default="local", help='"local" or the /v1 URL of a Whisper server')
    ap.add_argument("--stt-model", default="base.en", help="local: base.en, small.en, ...; http: the server's model id")
    ap.add_argument("--whisper-python", default="/usr/bin/python3")
    ap.add_argument("--mic", default="0", help="AVFoundation audio device index or name")
    ap.add_argument("--voice", default=None, help="macOS `say` voice")
    ap.add_argument("--no-speak", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the code the model runs")
    ap.add_argument("--ptt", action="store_true", help="push to talk with Enter instead of a trigger word")
    ap.add_argument("--wake", default="please", help="trigger word (default: please)")
    ap.add_argument("--input-file", help="test: listen to this audio file instead of the microphone")
    ap.add_argument("--debug", action="store_true", help="show ignored speech and the noise floor")
    ap.add_argument("--server", nargs="?", const="http://127.0.0.1:9030", metavar="URL",
                    help="send requests to a running nli_server.py (shared with the headset) "
                         "instead of running the session here; -m, --dm and -v are then the server's")
    a = ap.parse_args()

    if a.server:
        token = os.environ.get("NLI_TOKEN", "").strip()
        if not token:
            sys.exit("--server needs NLI_TOKEN (environment or ~/.pvbridge/keys.env)")
        try:
            session = RemoteSession(a.server, token)
        except Exception as e:
            sys.exit("can't reach the voice service at %s: %s" % (a.server, e))
        a.model = "%s via %s" % (session.model, a.server)
    else:
        cfg = {"pvlink": {}}
        if a.dm:
            host, _, port = a.dm.partition(":")
            cfg["pvlink"].update(dm_host=host, dm_port=int(port or 9010))
        session = nli.Session(a.model, config=cfg, verbose=a.verbose)
    print("Starting speech-to-text (%s)..." % a.stt, flush=True)
    stt = LocalWhisper(a.whisper_python, a.stt_model) if a.stt == "local" else HttpWhisper(a.stt, a.stt_model)
    spk = Speaker(a.voice, not a.no_speak)
    print("Ready: model %s, DMLink -> %s:%s." % (a.model, session.cfg["pvlink"]["dm_host"], session.cfg["pvlink"]["dm_port"]))
    if not a.ptt:
        hands_free(a, session, stt, spk)
        return
    rec = Recorder(a.mic)
    tmp = tempfile.mkdtemp(prefix="talk-")
    print("Push to talk: Enter = talk, or type a request. Ctrl-D quits.")

    while True:
        try:
            typed = input("\n[Enter to talk] ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        spk.stop()
        if typed:
            text = typed
        else:
            hints = vocabulary_prompt(session)
            wav = os.path.join(tmp, "%d.wav" % time.time())
            rec.start(wav)
            try:
                input("  listening... Enter to stop ")
            except (EOFError, KeyboardInterrupt):
                pass
            ok, err = rec.stop()
            if not ok:
                print("  (no audio recorded%s)" % (": " + err.strip() if err.strip() else
                      "; does this app have microphone access? System Settings > Privacy & Security > Microphone"))
                continue
            t0 = time.time()
            text = stt(wav, hints).strip()
            print("  heard (%.1fs): %s" % (time.time() - t0, text or "(nothing)"))
            if not text:
                continue
        if CANCEL_WORD.search(text):
            print("  (cancelled; nothing sent)")
            earcon("Funk")
            continue
        low = text.lower().strip(" .!?")
        if low in ("quit", "exit", "goodbye"):
            break
        if low in ("new conversation", "forget that conversation"):
            session.reset()
            print("  (conversation cleared; ParaView is unchanged)")
            spk.say("Okay, fresh conversation.")
            continue
        r = ask(session, text)
        show(r)
        spk.say(r["reply"])
    spk.stop()


if __name__ == "__main__":
    main()
