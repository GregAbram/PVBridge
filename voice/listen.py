"""listen -- continuous microphone capture with simple voice-activity detection.

ffmpeg streams 16 kHz mono 16-bit audio from an AVFoundation device (or, for
tests, from a file at real-time speed). Each 30 ms frame's loudness is compared
with a noise floor learned from the room; speech starts after 3 loud frames and
ends after `silence_s` of quiet. Each utterance is written to a WAV file and put
on a queue. pause()/resume() drop audio (e.g. while the computer is speaking).
Standard library only.
"""
import array
import math
import os
import queue
import subprocess
import tempfile
import threading
import time
import wave

RATE = 16000
FRAME = 480                     # 30 ms
BYTES = FRAME * 2


class Listener:
    def __init__(self, mic="0", input_file=None, silence_s=0.8, max_s=20.0, min_s=0.35,
                 start_frames=3, ratio=3.0, floor_min=150.0, preroll_s=0.3, debug=False):
        self.mic, self.input_file = mic, input_file
        self.silence_frames = int(silence_s * 1000 / 30)
        self.max_frames = int(max_s * 1000 / 30)
        self.min_frames = int(min_s * 1000 / 30)
        self.start_frames = start_frames
        self.ratio, self.floor_min = ratio, floor_min
        self.preroll = int(preroll_s * 1000 / 30)
        self.debug = debug
        self.utterances = queue.Queue()
        self.paused = threading.Event()
        self.noise = None
        self.tmp = tempfile.mkdtemp(prefix="listen-")
        self.p = None
        self.thread = None
        self.error = None

    # ------------------------------------------------------------------ control
    def start(self):
        if self.input_file:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", self.input_file]
        else:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-i", ":" + self.mic]
        cmd += ["-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
        self.p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.thread = threading.Thread(target=self._run, name="listener", daemon=True)
        self.thread.start()

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()

    def pause(self):
        self.paused.set()

    def resume(self):
        self.paused.clear()

    def get(self, timeout=None):
        """Next utterance WAV path, or None at the end of input (file) / on error."""
        return self.utterances.get(timeout=timeout)

    # ------------------------------------------------------------------ capture
    @staticmethod
    def _rms(buf):
        a = array.array("h", buf)
        return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0

    def _write(self, frames):
        path = os.path.join(self.tmp, "u%d.wav" % int(time.time() * 1000))
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(frames))
        return path

    def _run(self):
        ring = []                # recent quiet frames, for pre-roll
        speech, loud_run, quiet_run = None, 0, 0
        calib = []
        try:
            while True:
                buf = self.p.stdout.read(BYTES)
                if len(buf) < BYTES:
                    break
                if self.paused.is_set():
                    speech, loud_run, quiet_run, ring = None, 0, 0, []
                    continue
                level = self._rms(buf)
                if self.noise is None:              # learn the room for the first ~0.6 s
                    calib.append(level)
                    if len(calib) >= 20:
                        self.noise = sorted(calib)[len(calib) // 2]
                        if self.debug:
                            print("[listen] noise floor %.0f" % self.noise, flush=True)
                    continue
                thr = max(self.noise * self.ratio, self.floor_min)
                if speech is None:
                    if level > thr:
                        loud_run += 1
                        ring.append(buf)
                        if loud_run >= self.start_frames:
                            speech, quiet_run = list(ring[-(self.preroll + loud_run):]), 0
                    else:
                        loud_run = 0
                        ring.append(buf)
                        self.noise = 0.98 * self.noise + 0.02 * level      # follow slow changes
                    ring = ring[-(self.preroll + self.start_frames + 2):]
                    continue
                speech.append(buf)
                quiet_run = quiet_run + 1 if level < thr * 0.6 else 0
                if quiet_run >= self.silence_frames or len(speech) >= self.max_frames:
                    voiced = len(speech) - quiet_run
                    if voiced >= self.min_frames:
                        self.utterances.put(self._write(speech))
                    elif self.debug:
                        print("[listen] dropped a %.2f s blip" % (voiced * 0.03), flush=True)
                    speech, loud_run, quiet_run, ring = None, 0, 0, []
            err = self.p.stderr.read().decode(errors="replace").strip()
            if err:
                self.error = err
        except Exception as e:     # report through the queue, don't die silently
            self.error = str(e)
        finally:
            if speech and len(speech) >= self.min_frames:
                self.utterances.put(self._write(speech))
            self.utterances.put(None)
