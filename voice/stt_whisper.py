"""Persistent local speech-to-text worker using openai-whisper.

Run with an interpreter that has the `whisper` package (here: /usr/bin/python3, 3.9).
Reads one JSON request per line on stdin: {"wav": path, "prompt": "..."}
Writes one JSON line per request:          {"text": "...", "seconds": 1.2}
The model is loaded once, so each request costs only the transcription itself.
"""
import json
import sys
import time

import whisper

model_name = sys.argv[1] if len(sys.argv) > 1 else "base.en"
model = whisper.load_model(model_name)
print(json.dumps({"ready": model_name}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    t0 = time.time()
    try:
        r = model.transcribe(req["wav"], language="en", fp16=False,
                             initial_prompt=req.get("prompt") or None)
        out = {"text": r["text"].strip()}
    except Exception as e:  # report and keep serving
        out = {"text": "", "error": str(e)}
    out["seconds"] = round(time.time() - t0, 2)
    print(json.dumps(out), flush=True)
