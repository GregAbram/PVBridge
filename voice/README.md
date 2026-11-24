# pvbridge voice: talk to a live ParaView GUI

Built 2026-11-20. Standard-library Python only (3.9+). Needs pvbridge running in the ParaView GUI (see `../HANDOFF.md`).

| File | What it is |
|---|---|
| `pvagent.py` | The agent: one tool (`run_paraview` → `pvsend`, optional screenshot), the tool-use loop, and backends for Claude (`anthropic`), any OpenAI-compatible server (`openai`: Ollama, LM Studio, vLLM), and `reference` (no model, used to self-check the tests). Also a typed interface. |
| `models.json` | Named model configs. `local` points at the Windows 4090 box: set its IP and model. |
| `system_prompt.md` | What every model is told: how to work, how to reply (spoken, short), and verified ParaView 6.1 snippets and quirks. The same for all models, so the comparison is fair. |
| `pvvoice.py` | Voice front end: push-to-talk in the terminal (Enter / Enter), ffmpeg recording, Whisper speech-to-text (local worker or HTTP server), macOS `say` for replies. |
| `stt_whisper.py` | Persistent local Whisper worker (runs under `/usr/bin/python3`, which has `openai-whisper`). |
| `tests/cases.py` | 37 test cases: spoken-style requests with automatic checks run inside ParaView, or regexes on the spoken answer. |
| `run_tests.py` | Runs the cases against one or more models and prints a comparison table. |
| `results/` | One JSON per run: every request, the code each model ran, its reply, and the check outcome. |

## Use

```bash
export ANTHROPIC_API_KEY=...            # for opus / sonnet / haiku
~/pvbridge/voice/pvvoice.py             # talk (model opus by default)
~/pvbridge/voice/pvvoice.py -m local    # talk to the 4090 model
~/pvbridge/voice/pvagent.py -m opus "colour by smoke"   # typed, one request
```

The first time you record, macOS asks for microphone access for the terminal app.

## Test and compare models

```bash
~/pvbridge/voice/run_tests.py -m reference          # should be all PASS: proves the cases
~/pvbridge/voice/run_tests.py -m opus -m local      # compare
~/pvbridge/voice/run_tests.py --report              # table of all saved runs
```

**The test run takes over ParaView**: before every case it deletes the whole pipeline, colour maps and animation tracks, and loads the case's data (the plume particles or a Wavelet). Your state is saved first to `~/.pvbridge/testout/backup-<time>.pvsm`; get it back with File > Load State.

Categories: `basic` (one property), `intermediate` (colour maps, filters, camera, annotations), `hard` (animation tracks, camera orbit, saving files), `answer` (questions about the data, checked against the spoken reply), `multi` (two-turn conversations that depend on context).

## The Windows 4090 box

Run the model server there and point `models.json` → `local` at it:
- **Ollama for Windows**: `ollama pull <model>`, set `OLLAMA_HOST=0.0.0.0` so it listens on the LAN, allow port 11434 in the Windows firewall for the LAN only. `base_url` is `http://<ip>:11434/v1`.
- **Models that fit 24 GB**: about 30B parameters at 4-bit (e.g. Qwen2.5-Coder-32B), or mixture-of-experts models of similar total size, which answer faster. Set `"vision": true` only for a model that accepts images.
- **Speech-to-text on the GPU too** (optional): an OpenAI-compatible Whisper server (e.g. speaches / faster-whisper-server), then `pvvoice.py --stt http://<ip>:8000/v1 --stt-model <its model id>`.
- These servers have no authentication: keep them on the LAN or use an ssh tunnel (`ssh -L 11434:localhost:11434 windows`, then `base_url` `http://localhost:11434/v1`).

## Design notes

- Old screenshots are dropped from the conversation (only the latest is kept), and the Claude system prompt and tool are prompt-cached, to keep each turn fast.
- At most 10 tool calls per request.
- The system prompt asks the model to confirm before deleting, loading data, or slow operations. That is a prompt rule, not enforced in code.
- ParaView's own errors and warnings (Output Messages) are still not reported back to the model; see open item 1 in `../HANDOFF.md`. This hurts weaker models most.
