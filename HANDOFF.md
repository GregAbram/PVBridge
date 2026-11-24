# pvbridge: driving a live ParaView GUI from outside — handoff

Start a new conversation with: *"Read ~/pvbridge/HANDOFF.md and continue the pvbridge work."*
Status as of 2026-11-19.

---

## 1. What it is

pvbridge lets an outside process, such as Claude in a terminal, run Python inside a **running ParaView GUI**. The changes show up live: in the render views, the Pipeline Browser, the Properties panel, the Animation View and the undo history. It was built to let Claude inspect and change the user's live visualization pipeline, so the user doesn't have to click through GUI steps Claude describes.

**Status:**
- Working from start to finish with ParaView 6.1.1 on the user's Mac.
- Not yet tried in the GUI on ls6, where ParaView runs in the user's DCV desktop on a compute node.

---

## 2. Files

| File | Mac | ls6 |
|---|---|---|
| `pvbridge.py`: the module run inside ParaView | `~/pvbridge/pvbridge.py` | `/home1/01249/gda/pvbridge/pvbridge.py` |
| `pvsend`: the client that sends code and waits for the reply | `~/pvbridge/pvsend` | `/home1/01249/gda/pvbridge/pvsend` |
| `test_bridge.py`: headless test of the command handling | `~/pvbridge/test_bridge.py` | `/home1/01249/gda/pvbridge/test_bridge.py` |
| Mailbox | `~/.pvbridge/` (mode 700) | `~/.pvbridge/` in the ls6 home, created on first start |

The Mac and ls6 copies of `pvbridge.py` and `pvsend` are identical (md5 `4b853715…` and `9e2aa9e8…`).

**Mailbox layout:**

| Directory | Holds |
|---|---|
| `in/` | Commands waiting to run |
| `out/` | Results |
| `done/` | Commands that have run |
| `rejected/` | Command files refused for security reasons |
| `shots/` | Screenshots |

---

## 3. How it works

```
pvsend (Claude)                         ParaView GUI (pvbridge)
  write in/<id>.py.tmp                    VTK repeating timer on the active view's
  rename -> in/<id>.py   ───────────────▶  interactor, every 200 ms, on the Qt main thread
                                            → for each in/*.py, oldest first:
                                              check ownership/permissions
                                              move to done/
                                              exec in persistent namespace
                                              RenderAllViews()
  poll out/<id>.txt      ◀───────────────   write out/<id>.txt (tmp + rename)
  print body; exit 0/1/2                    one-line log in the Python Shell
```

- **Main thread:** `paraview.simple` must only be called from ParaView's main (Qt) thread. A background thread with a socket would crash the GUI or leave its state inconsistent.
  - ParaView's Python has **no PySide or PyQt**, on ls6 or in the Mac app, so a `QTimer` isn't available.
  - Installing PySide with pip would bring a second Qt and risk crashes.
  - Instead, `iren.CreateRepeatingTimer(200)` on the render view's `vtkRenderWindowInteractor` gives a timer driven by Qt, whose callbacks run on the main thread.
- **Timer ID quirk:** the `TimerEvent` callback must use `@calldata_type(VTK_INT)` and compare the event's call data with the ID `CreateRepeatingTimer` returned.
  - `obj.GetTimerEventId()` is never set by the Qt interactor, so a check against it never matches.
  - `GetVTKTimerId(pid)` is also wrong: the call data already *is* the VTK timer ID.
- **No network port:** the mailbox is a private directory, so Unix permissions control access. On ls6 the home directory is shared, so `pvsend` works from the login node with no ssh to the compute node.
- **Results:** the first line of each result is `## ok 0.12s` or `## error 0.12s`. The rest is the captured stdout/stderr, plus the value of a final expression (REPL style), or the traceback.
- **Security:**
  - The mailbox root must be private to the user.
  - A command file is run only if the user owns it and no one else can write to it. Otherwise it's moved to `rejected/` and the rejection is reported in the Python Shell.
- **Re-entry guard:** a `busy` flag stops a long command from being re-entered if Qt processes events while it runs.

---

## 4. Using it

### Start (in the ParaView Python Shell, with a render view active)
Import by file path. A plain `sys.path.insert` followed by `import pvbridge` failed once. The cause turned out to be that the path pointed to ls6 while ParaView was running on the Mac, but loading by file path avoids any such problem.

```python
import importlib.util, sys
_spec = importlib.util.spec_from_file_location("pvbridge", "/Users/gda/pvbridge/pvbridge.py")   # ls6: /home1/01249/gda/pvbridge/pvbridge.py
pvbridge = importlib.util.module_from_spec(_spec); sys.modules["pvbridge"] = pvbridge
_spec.loader.exec_module(pvbridge); pvbridge.start()
```
It prints `[pvbridge] listening on … every 200 ms (timer N)`.

**Other calls:**
- Stop: `pvbridge.stop()`. Status: `pvbridge.status()`.
- Calling `start()` again stops the previous timer first, so pasting it twice is safe.
- **After editing `pvbridge.py`:** run the four lines above again. `importlib.reload(pvbridge); pvbridge.start()` should also work, because the module is registered in `sys.modules`, but it hasn't been tested.
- **The timer belongs to the view that was active at `start()`.** If that view is closed, run `start()` again.

### Send code (Claude, from a shell)
```bash
~/pvbridge/pvsend -t 60 <<'EOF'
c = FindSource("Contour1")
c.Isosurfaces = [100.0]
c.UpdatePipeline()
c.GetDataInformation().GetNumberOfPoints()   # final expression -> returned
EOF
```
You can also pass the code as an argument: `pvsend 'GetSources()'`.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | The command raised; the traceback goes to stdout and the `## error` line to stderr |
| 2 | Timed out. The command may still run later; its result will land in `~/.pvbridge/out/<id>.txt` |

**In the command namespace:** everything from `paraview.simple`, plus `shot(path=None, **kw)` (saves a screenshot of the active view to `~/.pvbridge/shots/<time>.png` and returns the path) and `bridge` (the module). The namespace persists between commands and is separate from the Python Shell's own variables; from the shell, see it as `pvbridge.ns`.

**Looking at the result:** read the PNG that `shot()` returns.

### Practical rules
- **Long commands freeze the GUI** until they finish, the same as typing them into the Python Shell. Set `pvsend -t` long enough.
- **Animations:** `scene.Play()` inside a command blocks until the animation ends, and the view does redraw each frame.
- **Shared control:** the user and Claude both change the same state. Let one act at a time, and report what changed.
- **Check before acting:** list sources with `GetSources()` and look at a `shot()` before making assumptions about the pipeline.

---

## 5. What was tested

**Headless,** on ls6 node c311-072 with `pvpython --force-offscreen-rendering test_bridge.py`. This calls `poll()` directly and doesn't test the GUI timer.
- Passed:
  - An expression returns its value.
  - Printed output comes back.
  - The namespace persists between commands.
  - A pipeline change works: Sphere resolution 16 → 32, giving 194 points.
  - An error comes back with its traceback.
  - A group-writable command file is rejected and not run.
  - The mailbox is mode 700.
- **The screenshot test crashed** (segfault) because ParaView on ls6 needs an X display and the ssh session had none. It was removed from the test. In the DCV desktop there is a display, so `shot()` is expected to work there, but this is untested.

**Live, in the Mac ParaView 6.1.1 GUI.** All of these worked:
- Listing the pipeline and taking a screenshot.
- Adding a Wavelet, deleting all sources, adding a Contour.
- Setting the isovalue to 100.
- Rescaling the RTData colour map to the Wavelet's range (37.35–276.83).
- A keyframe animation of the isovalue from 40 to 270 in steps of 10.
- That same animation combined with a full camera orbit: 720 frames in 21.7 s, about 33 frames per second.

---

## 6. ParaView lessons (6.1.1)

- **Animation tracks need the XML property name,** not the Python name. For Contour: `GetAnimationTrack("ContourValues", index=0, proxy=c)`, not `"Isosurfaces"`. The wrong name gives no Python error; ParaView's Output Messages fill with `vtkPVKeyFrameAnimationCueForProxies … Cue does not have a property set. Cannot animate.` and nothing animates. To remove a bad track: `s.Cues = [q for q in s.Cues if getattr(q, "AnimatedPropertyName", None) != "Isosurfaces"]`.
- **No "Real Time" play mode in 6.1.** `PlayMode` accepts only `Sequence` and `Snap To TimeSteps`. To make an animation slower, give it more frames (`NumberOfFrames`); the speed in seconds then depends on how fast the view draws.
- **Stepped values:** use `CompositeKeyFrame(..., Interpolation="Boolean")` at each step so the value holds, rather than `Ramp`, which interpolates.
- **Camera orbit:** use `GetCameraTrack(view)` with `Mode = "Path-based"`. The first `CameraKeyFrame` needs `PositionPathPoints` (points on the circle around the focal point; 24 was used), `FocalPathPoints` and `ClosedPositionPath=1`.
- **Contour colour:** a contour coloured by its own array has a single value, so the colour map collapses to it. Rescale to the source's range with `lut.RescaleTransferFunction(lo, hi)`, and set `lut.AutomaticRescaleRangeMode = "Never"` so it stays.
- **Testing the timer** while developing: if you paste timer code repeatedly, the old timers keep running (IDs 10–14 were seen cycling). Restart ParaView for a clean slate, or keep timer and observer handles so they can be removed (`start()` does this).

---

## 7. Open items

1. **Errors from ParaView itself aren't reported back.** VTK and ParaView errors and warnings go to the GUI's Output Messages window, not to Python, so a command can "succeed" while ParaView logged errors. That's how the bad animation track was missed. Proposed fix: during each command, add an observer on `vtkOutputWindow.GetInstance()` for `ErrorEvent` and `WarningEvent` (call data is the message text, via `@calldata_type(VTK_STRING)`). Append what it collects to the result, and mark the result `## warn` if anything was logged. The user was offered this but hasn't asked for it yet.
2. **Test on ls6** in the DCV desktop GUI (ParaView 6.1.1 on the compute node): start from `/home1/01249/gda/pvbridge/pvbridge.py` and send from the login node with `/home1/01249/gda/pvbridge/pvsend`. Confirm the timer fires and `shot()` works.
3. **Possible additions:**
   - A `pvsend --shot` option that returns the screenshot path.
   - A `pvbridge.share_namespace()` option to share variables with the Python Shell.
   - Pruning `done/`, `out/` and `shots/`, which grow without limit; Claude has written 7 screenshots and about 16 commands so far.
4. **Alternative considered but not used:** ParaView's multi-client collaboration mode (`pvserver --multi-clients`). It's unclear whether pvpython can join as a full collaborator.

---

## 7a. Voice front end and model test set (`~/pvbridge/voice/`, 2026-11-20)

Talk to ParaView instead of typing to Claude Code, and compare models (Opus vs an open-weights model on the user's Windows 4090 box). Details in `~/pvbridge/voice/README.md`.
- `pvagent.py`: a model-agnostic agent with one tool (`run_paraview` → pvsend, optional screenshot). Backends: Claude Messages API, any OpenAI-compatible server (Ollama etc.), `reference`. Configs in `models.json`; prompt in `system_prompt.md`.
- `pvvoice.py`: push-to-talk (Enter/Enter), ffmpeg recording, Whisper (local worker under `/usr/bin/python3`, or an HTTP server), `say` for replies.
- `tests/cases.py` (37 cases) and `run_tests.py`; results in `voice/results/`. **Running the tests wipes the ParaView pipeline** (state saved first to `~/.pvbridge/testout/backup-*.pvsm`).
- **Verified:** Whisper `base.en` transcribes a `say` sample exactly (~1.2 s on this Intel Mac); both backends' tool loops pass against a mock server; all cases compile.
- **TACC SambaNova models** are in `models.json` as `sn-*` (endpoint `https://ai.tejas.tacc.utexas.edu/v1`, key in `SAMBANOVA_API_KEY`, OpenAI-compatible). `sn-qwen32`, `sn-gemma31`, `sn-gptoss20` are 4090-sized and stand in for the Windows box. The endpoint answers from the Mac (401 without a key); no model call made yet.
- **Reference run: 37/37 pass** (2026-11-20) after fixes: `ColorBy(d, None)` raises in 6.1 when the display isn't coloured yet (use `d.ColorArrayName = ["POINTS", ""]`); the preset is `"Viridis"`, not `"Viridis (matplotlib)"`; data-dependent answers are computed live (`expect`), because the plume data was regenerated 2026-11-19 15:02. System prompt now asks for numbers as digits.
- **Tool-calling probe:** 10 of 11 SambaNova models made a correct tool call for "make the background black"; Qwen3-235B-Instruct replied as if done without calling the tool.
- **Key file:** `~/.pvbridge/keys.env` (mode 600, `NAME=value` lines) is read by pvagent, so Claude can run models without seeing the key.
- **ParaView crash, 2026-11-20 10:27, during the DeepSeek run** (it had passed 33 of its first 35 cases). DeepSeek's orbit attempt ran `scene.Cues = []`, which also removes the time track; the next scene property change (the harness reset) segfaulted in `pqTimelineWidget::setAdvancedMode`. Fixes, not yet tested in a live GUI: pvbridge now restores the time track after any command that removes it (`_ensure_time_track`, so the Mac pvbridge.py no longer matches the ls6 copy); the harness removes tracks without touching the time track (`_t_remove_cues`); the system prompt forbids replacing `scene.Cues` wholesale.
- **Second crash, 2026-11-20 10:43,** in the harness reset after Qwen3-32B's `animate_contour`: the model did `scene.Cues.append(track)` after `GetAnimationTrack` had already added it, so the track was listed twice; removing it later segfaulted in `pqTimelineModel::deleteRow`. Fixes, not yet tested live: pvbridge drops duplicate track entries after each command (`_dedupe_cues`); the reset removes tracks one at a time, duplicates first, before deleting sources; the prompt says not to append tracks again.
- **Duplicate-track fix verified live** (Qwen's exact sequence, then the reset: no crash). Reference 37/37 again.
- **Results, 2026-11-20, clean rerun with the final prompt and agent** (`voice/results/run-20261120-clean.log`):

  | | DeepSeek-V3.2 | gpt-oss-120b | Qwen3-32B | gemma-4-31B | gpt-oss-20b |
  |---|---|---|---|---|---|
  | passed | 36/37 | 35/37 | 35/37 | 36/37 | 35/37 |
  | median s/request | 3.6 | 2.3 | 5.0 | 2.8 | 2.4 |
  | failed | volume | clip_xpos, orbit | threshold, volume | clip_xpos | rescale_time, clip_xpos |

  `clip_xpos` is the hardest case (3 of 5 fail; gpt-oss-20b kept the wrong half while saying the right thing). `volume` failures: a second Wavelet made instead of using "Wavelet1". gpt-oss-120b's `orbit` failure: the server could not parse its tool call 3 times in a row (HTTP 400). Failures vary between runs, so one run separates these models poorly. No crash; no time-track or duplicate-track repairs were needed.
- **Test fixes found by the models:** the reset left the reader at the previous case's time (now `UpdatePipeline(0)`); `expect` values computed at the view time; numbers like "3 508" parsed; the prompt's number example ("3508") was being copied, so removed.
- **Agent fixes:** Gemma `<|channel>` markers stripped; HTTP 400 "could not parse tool call" (Qwen3-32B, malformed JSON) retried twice.
- **Not yet done:** a real mic recording; any Opus run (needs `ANTHROPIC_API_KEY` in keys.env); the Windows 4090 server.


**Next direction (2026-11-20):** voice-driven authoring for the Unity/PVLink headset, with a bake step for standalone playback. Spec: `~/pvbridge/SPEC-pvlink-voice.md` (draft 0.2: Part 1 natural-language interface, Part 2 input/output components, all over IP; not yet built).

## 7b. Networked pvbridge (2026-11-20, SPEC-pvlink-voice.md 6.2-6.4)

- `pvbridge.py` now also listens on HTTP (default `127.0.0.1:9020`; `start(listen="0.0.0.0:9020")` for other machines, `listen=None` for mailbox only). `POST /run {code, screenshot, timeout}` → `{status ok|warn|error, seconds, output, png?}`; `GET /status`. Token `PVBRIDGE_TOKEN` from the environment or `~/.pvbridge/keys.env` (generated 2026-11-20; with no token the listener stays off). A listener thread only queues requests; the main-thread timer runs them.
- Output now includes what's printed during the render after each command (DMLink sends then) and VTK/ParaView errors and warnings (`vtkOutputWindow` observer → status `warn`). Applies to the mailbox too (`## warn` first line).
- **GIL finding:** ParaView's main thread holds the GIL while idle in Qt, so listener threads only ran during timer ticks (0.4–0.6 s per trivial call at 200 ms). Fix: timer every 50 ms, and each tick sleeps 2 ms before and after `poll()` when the listener is on. Round trip now ~0.05 s.
- `pvsend --url http://host:9020 [--screenshot file.png]` (or `PVBRIDGE_URL`) uses the network; without it, the mailbox.
- **Hot reload without restarting ParaView:** send, by mailbox or network:
  `import importlib.util, sys; _old = bridge; _spec = importlib.util.spec_from_file_location("pvbridge", "/Users/gda/pvbridge/pvbridge.py"); _m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m); sys.modules["pvbridge"] = _m; _old.stop(quiet=True); _m.start()`
  (the Python Shell's own `pvbridge` variable still points at the old module).
- Tested on the Mac: auth, 20 concurrent requests run once each in order, `/status` answers in 2 ms during a long command, client timeout (504) then recovery, VTK error capture, tracebacks, screenshot in the response, mailbox and `pvsend --url`, restart on the same port. Not yet: DMLink messages during the render (needs DMLink loaded; spec 15.1), access from another machine.
- Previous versions kept as `pvbridge.py.v1`, `pvsend.v1`.

## 7c. Part 1 helpers and session (2026-11-20, SPEC 7-8)

- `voice/pvlink.py`: helpers executed in pvbridge's namespace (`pvl_create`, `pvl_add_variable`, `pvl_derive`, `pvl_set`, `pvl_show/hide/remove`, `pvl_colour`, `pvl_rename`, `pvl_items`, `pvl_snapshot`, `pvl_vocabulary`, `pvl_find_data`, `pvl_undo`, `pvl_clear_headset`, `pvl_reset_domain`; `_pvl_begin/_pvl_end` group a request into one undo step). Senders are named "<object> (headset)", Time=Static, ItemName = object name; one "DMLink Domain" with OverrideBounds = union of source bounds.
- `voice/system_prompt_pvlink.md`: the PVLink-mode prompt. `voice/nli.py`: `Session.ask(text)` (snapshot, undo group, model, snapshot, false-success check, items) and a typed CLI (`nli.py -m sn-deepseek`). Loads DMLink and the helpers into ParaView on first use.
- ParaView 6.1 notes: `GetUniqueCellTypes()` returns nothing here, so topology comes from the in-process output (built-in session only); rescale colours from the object's own array range, not the Sender display (it may not have run yet).
- **App Nap**: when ParaView isn't the frontmost app, macOS throttled the bridge's timer (no-op round trips 0.24-4.6 s). pvbridge now begins an NSProcessInfo user-initiated activity while running (ctypes, no system setting); `status()` shows `app_nap_off`. Round trips are a steady 0.05 s in the background.
- **Wavelet scenario, 2026-11-20, after prompt fixes** (time per turn including model, ParaView and snapshots):
  - gpt-oss-120b: all turns right, asks for the name, 0.9-2.9 s per turn. Best so far.
  - DeepSeek-V3.2: all turns right, 1.1-3.7 s.
  - gpt-oss-20b (4090-sized): right end state, asked for the name and the variable; once claimed a colour change that was already true (caught by the false-success check). 1.0-4.0 s.
  - Qwen3-32B: empty replies (thinking mode), skipped turns. Llama-4-Maverick: the TACC endpoint rejects its tool calls (HTTP 400); session now reports a model error instead of crashing.

- **First live run with Unity on Windows (2026-11-20):** Mac ParaView + nli.py (gpt-oss-120b) → dm.exe on Windows (store C:\Users\gda\dmvoice) → DataManager examples PVLink scene in the Unity editor. Wavelet, colouring, colormaps, contour and an isovalue sweep reached Unity. One live update (adding X) only showed after restarting Play mode; colormap changes updated live. Then ParaView **crashed** when the model called undo (see SPEC 8.5); undo is now disabled and the model is told never to remove animation cues. The snapshot now includes colormap range/colours and animation tracks, so those changes count for the false-success check.

- **Voice front end** `voice/talk.py` (2026-11-20): hands-free with trigger word "ParaView" (`listen.py`: ffmpeg stream + energy voice detection; loose match for near misses), follow-up answers without the trigger word after a question, earcons (Tink/Pop/Basso), `say` replies; `--ptt` for push-to-talk. Whisper hints must be a sentence, not a word list (a list made Whisper drop the trigger word). Mac input volume was 8/100.
- **Start over empties the DM project** (`pvl_clear_project`, SPEC 5.9), via dm_probe + DMLink's connection. Verified on the Windows dm (6 leftover items → 0).

- **Save and bake (2026-11-23):** "save this as X" / "bake X" → `pvl_save` / `pvl_bake` (voice/pvlink.py) run `voice/bake.py` under pvbatch in the background into project `GDA/X`, state `GDA-X`, every frame timed; `pvl_bake_status`. Saved visualizations in `~/pvbridge/visualizations`. Tested on a scratch dm and from the GUI against the Windows dm (test project emptied afterwards; its empty state `GDA-t_bake gui` remains, since states can only be deleted with DELETE_STATE).

- **Voice service for the headset (2026-11-23):** `voice/nli_server.py`, run with **Apple's `/usr/bin/python3`** (`/usr/bin/python3 nli_server.py -m sn-gptoss120 --dm 10.0.0.49`): the Mac firewall doesn't recognise Homebrew's Python.app, so with it LAN connections were reset even after allowing it; `/usr/bin/python3` is already allowed. Port 9030, token `NLI_TOKEN` in keys.env. `POST /ask` with JSON `{text, speak}` or a raw WAV body (`?speak=1&request_id=`); reply audio at `/audio/<id>.wav` (16-bit mono 22.05 kHz from `say`).
- **PVAR voice (2026-11-23):** `C:\Users\gda\Unity\PVAR\Assets\Scripts\PVARVoice.cs` (new, untracked). Hold X (left), speak, release; label below the line of sight; reply played on the Quest speakers (X again cuts it short). Off unless `/sdcard/Android/data/edu.utexas.tacc.pvar/files/pvar_voice.json` exists (`service`, `token`, `button`, `maxSeconds`; pushed). Uses HttpClient (UnityWebRequest refuses http here). Not yet built or tested in the headset.
- **Earbuds alongside the headset:** `talk.py --server [URL]` (default http://127.0.0.1:9030) sends what it hears to nli_server instead of running its own session, so earbuds and headset share one conversation and take turns (a request while the other is running gets "still working"). Speech recognition and `say` stay on the Mac. Without --server, talk.py and nli_server are separate sessions on the same ParaView: separate memory, no coordination.

## 8. Related

**The HiLat visualization project**, which this was built to support: `~/hilat/HANDOFF.md`. Its state files are in `/scratch/01249/gda/north55_vtk/States/` on ls6.

**ls6 access:**
- `ssh staff.ls6.tacc.utexas.edu`, then `ssh <DCV node>`. The node changes daily, so ask the user.
- Modules: `module load intel/24.1 paraview/6.1.1`.

## 9. Where the code lives (2026-11-23)

- **pvbridge** (`~/pvbridge`): git repo since 2026-11-23, `main`, no remote. visualizations/ and voice/results/ are ignored.
- **AprilTags**: `release-0.7.1` + tag `v0.7.1` (5bcb587) pushed = 0.7.0 + `QuestRoomControls.ForgetLearnedOnX`/`XHint`. Local `main` still has the user's uncommitted 0.8.0 work; merge release-0.7.1 into it when 0.8.0 is committed.
- **DataManager**: `cache-removals` (9b3755f) and `pvlink-points` (46e2a71: points without cells, sprites, radius, Z-up) pushed. `cache-deletes` (85221bb, on pvlink-points) pushed: caches drop projects/states their root no longer lists (SPEC 15.2b, `.dm-cache.json`), Android lib rebuilt. The Windows clone `C:\Users\gda\DataManager` is on pvlink-points.
- **PVAR**: branch `voice` pushed (PVARVoice.cs; manifest pins DataManager 85221bb and AprilTags 5bcb587). Not merged into main/datamanager-client.
- **ParaViewPlugins**: `main` aadca1d, not pushed.
- The dm on Windows runs `C:\Users\gda\DataManager\build-win\dm.exe --store C:\Users\gda\dmvoice`: `.states\` holds one JSON per state (Institution-Project), `GDA\<project>\<type>\` holds the items (`KeyData\<name>-<t>.bin` + `.tstep`, Bounds, Colormap, Visibility).
