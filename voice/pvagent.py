#!/usr/bin/env python3
"""pvagent -- a minimal, model-agnostic agent that drives a live ParaView GUI.

One tool, run_paraview(code, screenshot), sends Python to ParaView through
pvbridge (pvsend) and returns the output, plus a screenshot image if asked.
The same loop runs against:

    anthropic   Claude via the Messages API          (needs ANTHROPIC_API_KEY)
    openai      any OpenAI-compatible chat endpoint  (Ollama, LM Studio, vLLM, ...)
    reference   no model; replays a test case's reference code (harness self-check)

Only the Python standard library is used.

    pvagent.py --model opus "colour the particles by smoke"
    pvagent.py --model local            # interactive: type requests, blank line quits
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PVSEND = os.path.join(os.path.dirname(HERE), "pvsend")
SHOT_WIDTH = 800          # screenshots sent to the model are scaled to this width
MAX_STEPS = 10            # tool calls per user request
TOOL_TIMEOUT = 300        # seconds a single ParaView command may take

TOOL_DESC = (
    "Run Python code inside the user's live ParaView GUI (paraview.simple is "
    "imported; the namespace persists between calls). Returns printed output and "
    "the repr of a final expression, or the traceback. The views re-render after "
    "each call. Set screenshot=true to also get an image of the active view after "
    "the code runs.")
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "Python source to execute."},
        "screenshot": {"type": "boolean",
                       "description": "Return a screenshot of the active view afterwards."},
    },
    "required": ["code"],
}


# --------------------------------------------------------------------------- config

KEYS_FILE = os.path.expanduser("~/.pvbridge/keys.env")


def load_keys(path=KEYS_FILE):
    """Fill missing env vars from ~/.pvbridge/keys.env (NAME=value lines), if it is private."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        sys.stderr.write("ignoring %s: must be owned by you and mode 600\n" % path)
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


load_keys()


def load_models(path=None):
    path = path or os.path.join(HERE, "models.json")
    with open(path) as f:
        cfg = json.load(f)
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def load_system_prompt(path=None):
    with open(path or os.path.join(HERE, "system_prompt.md")) as f:
        return f.read()


# --------------------------------------------------------------------------- the tool

def pvsend(code, timeout=TOOL_TIMEOUT):
    """Run code in ParaView. Returns (ok, text)."""
    try:
        p = subprocess.run([PVSEND, "-t", str(timeout)], input=code, text=True,
                           capture_output=True, timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        return False, "pvsend itself hung"
    if p.returncode == 2:
        return False, "TIMEOUT: ParaView did not answer within %ds (still busy, or pvbridge not running)." % timeout
    text = p.stdout
    if p.returncode == 1:
        text = "ERROR\n" + text
    return p.returncode == 0, text.strip() or (
        "(no output: the code printed nothing, and its last line was not a bare expression, so NO value "
        "came back. A value computed inside if/else or a loop is not returned. To get a value, print it "
        "or put it alone on the last line.)")


def screenshot_png():
    """Screenshot of the active view, scaled to SHOT_WIDTH, as PNG bytes (or None)."""
    ok, out = pvsend("shot()", timeout=60)
    if not ok:
        return None
    m = re.search(r"'([^']+\.png)'", out)
    if not m:
        return None
    src = m.group(1)
    small = src[:-4] + "_small.png"
    try:   # macOS sips; fall back to the full-size image
        subprocess.run(["sips", "-Z", str(SHOT_WIDTH), src, "--out", small],
                       capture_output=True, timeout=30, check=True)
        src = small
    except Exception:
        pass
    with open(src, "rb") as f:
        return f.read()


class ParaViewTool:
    def __init__(self, timeout=TOOL_TIMEOUT):
        self.timeout = timeout

    def __call__(self, args):
        code = args.get("code", "")
        ok, text = pvsend(code, self.timeout)
        png = screenshot_png() if args.get("screenshot") else None
        if len(text) > 6000:
            text = text[:3000] + "\n...[%d chars cut]...\n" % (len(text) - 6000) + text[-3000:]
        return ok, text, png


# --------------------------------------------------------------------------- HTTP

def post_json(url, body, headers, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=dict(headers, **{"content-type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %d from %s: %s" % (e.code, url, e.read().decode(errors="replace")[:2000]))


# --------------------------------------------------------------------------- backends

class AnthropicBackend:
    """Claude Messages API with prompt caching; old screenshots are dropped."""

    def __init__(self, cfg, system):
        self.model = cfg["model"]
        self.url = cfg.get("base_url", "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        key = os.environ.get(cfg.get("api_key_env", "ANTHROPIC_API_KEY"), "")
        if not key:
            raise RuntimeError("set %s for model %s" % (cfg.get("api_key_env", "ANTHROPIC_API_KEY"), self.model))
        self.headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        self.max_tokens = cfg.get("max_tokens", 4096)
        self.system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self.tools = [{"name": "run_paraview", "description": TOOL_DESC,
                       "input_schema": TOOL_SCHEMA, "cache_control": {"type": "ephemeral"}}]
        self.messages = []

    def reset(self):
        self.messages = []

    def _drop_old_images(self, keep=1):
        seen = 0
        for m in reversed(self.messages):
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            for blk in m["content"]:
                if blk.get("type") != "tool_result" or not isinstance(blk.get("content"), list):
                    continue
                imgs = [c for c in blk["content"] if c.get("type") == "image"]
                if imgs:
                    seen += 1
                    if seen > keep:
                        blk["content"] = [c for c in blk["content"] if c.get("type") != "image"] + \
                                         [{"type": "text", "text": "(older screenshot removed)"}]

    def turn(self, user_text, tool, log):
        self.messages.append({"role": "user", "content": user_text})
        usage = {"in": 0, "out": 0, "cache_read": 0}
        calls = errors = 0
        for _ in range(MAX_STEPS + 1):
            self._drop_old_images()
            r = post_json(self.url, {"model": self.model, "max_tokens": self.max_tokens,
                                     "system": self.system, "tools": self.tools,
                                     "messages": self.messages}, self.headers)
            u = r.get("usage", {})
            usage["in"] += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            usage["cache_read"] += u.get("cache_read_input_tokens", 0)
            usage["out"] += u.get("output_tokens", 0)
            self.messages.append({"role": "assistant", "content": r["content"]})
            uses = [b for b in r["content"] if b["type"] == "tool_use"]
            if not uses or calls >= MAX_STEPS:
                text = " ".join(b["text"] for b in r["content"] if b["type"] == "text").strip()
                return text, {"tool_calls": calls, "tool_errors": errors, "usage": usage}
            results = []
            for b in uses:
                calls += 1
                log("code", b["input"].get("code", ""))
                ok, out, png = tool(b["input"])
                errors += not ok
                log("result", out)
                content = [{"type": "text", "text": out}]
                if png:
                    content.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": base64.b64encode(png).decode()}})
                results.append({"type": "tool_result", "tool_use_id": b["id"],
                                "content": content, "is_error": not ok})
            self.messages.append({"role": "user", "content": results})
        return "(stopped after %d tool calls)" % calls, {"tool_calls": calls, "tool_errors": errors, "usage": usage}


class OpenAIBackend:
    """OpenAI-compatible /v1/chat/completions (Ollama, LM Studio, vLLM, llama.cpp server)."""

    def __init__(self, cfg, system):
        self.model = cfg["model"]
        self.url = cfg["base_url"].rstrip("/") + "/chat/completions"
        key = "none"
        if cfg.get("api_key_env"):
            key = os.environ.get(cfg["api_key_env"], "")
            if not key:
                raise RuntimeError("set %s for model %s" % (cfg["api_key_env"], cfg["model"]))
        self.headers = {"authorization": "Bearer " + key}
        self.vision = cfg.get("vision", False)
        self.extra = cfg.get("extra", {})          # e.g. {"temperature": 0.2, "options": {...}}
        self.system = system
        self.tools = [{"type": "function", "function": {
            "name": "run_paraview", "description": TOOL_DESC, "parameters": TOOL_SCHEMA}}]
        self.reset()

    def reset(self):
        self.messages = [{"role": "system", "content": self.system}]

    def _drop_old_images(self, keep=1):
        seen = 0
        for m in reversed(self.messages):
            if m["role"] == "user" and isinstance(m["content"], list):
                if any(c.get("type") == "image_url" for c in m["content"]):
                    seen += 1
                    if seen > keep:
                        m["content"] = "(older screenshot removed)"

    def turn(self, user_text, tool, log):
        self.messages.append({"role": "user", "content": user_text})
        usage = {"in": 0, "out": 0, "cache_read": 0}
        calls = errors = 0
        for _ in range(MAX_STEPS + 1):
            self._drop_old_images()
            body = dict({"model": self.model, "messages": self.messages, "tools": self.tools}, **self.extra)
            for attempt in range(3):
                try:
                    r = post_json(self.url, body, self.headers)
                    break
                except RuntimeError as e:
                    # the server could not parse the model's tool call (malformed JSON): sample again
                    if attempt < 2 and "HTTP 400" in str(e) and "tool call" in str(e).lower():
                        log("retry", "server could not parse the tool call; retrying")
                        continue
                    raise
            u = r.get("usage") or {}
            usage["in"] += u.get("prompt_tokens", 0)
            usage["out"] += u.get("completion_tokens", 0)
            msg = r["choices"][0]["message"]
            tcs = msg.get("tool_calls") or []
            self.messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not tcs or calls >= MAX_STEPS:
                # reasoning models may put their thinking inline; don't speak it
                text = re.sub(r"(?s)<think>.*?(</think>|$)", "", msg.get("content") or "")
                # Gemma 4 thinking-channel markers: drop everything up to the last <channel|>
                text = re.sub(r"(?s)^.*<channel\|>", "", text) if "<channel|>" in text else text
                text = re.sub(r"<\|?/?channel\|?>|<\|[a-z_]+\|>", "", text).strip()
                return text, {"tool_calls": calls, "tool_errors": errors, "usage": usage}
            pngs = []
            for tc in tcs:
                calls += 1
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError as e:
                    args, out, ok, png = None, "ERROR: tool arguments were not valid JSON: %s" % e, False, None
                if args is not None:
                    log("code", args.get("code", ""))
                    ok, out, png = tool(args)
                errors += not ok
                log("result", out)
                self.messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": out})
                if png and self.vision:
                    pngs.append(png)
            for png in pngs:   # tool messages can't carry images on most servers
                self.messages.append({"role": "user", "content": [
                    {"type": "text", "text": "Screenshot of the view after that call:"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]})
        return "(stopped after %d tool calls)" % calls, {"tool_calls": calls, "tool_errors": errors, "usage": usage}


class ReferenceBackend:
    """No model: run the reference code a test case supplies (checks the harness and the checks)."""

    def __init__(self, cfg, system):
        self.reference = None

    def reset(self):
        pass

    def turn(self, user_text, tool, log):
        if not self.reference:
            return "(no reference for this request)", {"tool_calls": 0, "tool_errors": 0, "usage": {}}
        log("code", self.reference)
        ok, out, _ = tool({"code": self.reference})
        log("result", out)
        return out, {"tool_calls": 1, "tool_errors": int(not ok), "usage": {}}


BACKENDS = {"anthropic": AnthropicBackend, "openai": OpenAIBackend, "reference": ReferenceBackend}


class Agent:
    def __init__(self, model_name, models=None, system=None, verbose=False, tool_timeout=TOOL_TIMEOUT):
        models = models or load_models()
        if model_name not in models:
            raise SystemExit("unknown model %r; models.json has: %s" % (model_name, ", ".join(models)))
        cfg = models[model_name]
        self.name = model_name
        self.backend = BACKENDS[cfg["backend"]](cfg, system or load_system_prompt())
        self.tool = ParaViewTool(tool_timeout)
        self.verbose = verbose
        self.trace = []

    def _log(self, kind, text):
        self.trace.append((kind, text))
        if self.verbose:
            tag = {"code": ">>> ", "result": "<<< "}.get(kind, "")
            sys.stderr.write("".join(tag + l + "\n" for l in text.splitlines()) or tag + "\n")

    def reset(self):
        self.backend.reset()

    def ask(self, text):
        """One user request -> (spoken reply, stats)."""
        self.trace = []
        t0 = time.time()
        reply, stats = self.backend.turn(text, self.tool, self._log)
        stats["seconds"] = round(time.time() - t0, 2)
        return reply, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("request", nargs="*", help="request text (omit for interactive)")
    ap.add_argument("-m", "--model", default=os.environ.get("PVVOICE_MODEL", "opus"))
    ap.add_argument("-q", "--quiet", action="store_true", help="don't show the code being run")
    a = ap.parse_args()
    agent = Agent(a.model, verbose=not a.quiet)
    if a.request:
        reply, stats = agent.ask(" ".join(a.request))
        print(reply)
        sys.stderr.write("[%s: %.1fs, %d tool calls]\n" % (a.model, stats["seconds"], stats["tool_calls"]))
        return
    while True:
        try:
            line = input("you> ").strip()
        except EOFError:
            break
        if not line:
            break
        reply, stats = agent.ask(line)
        print("pv>  " + reply)


if __name__ == "__main__":
    main()
