# Natural-language PVLink authoring: specification

Draft 0.2, 2026-11-20. Restructures draft 0.1 into two parts that meet at a text API, with every component reachable over IP. Status: design agreed in conversation. Built so far: section 17 step 1 (networked pvbridge, 2026-11-20; see HANDOFF.md 7b). Everything else is not built except what section 2 lists as existing.

- **Part 1, the natural-language interface** (sections 5–10): text in, reply and status out. It turns requests into changes to a live ParaView GUI session, which DMLink sends through the DataManager (DM) to headsets.
- **Part 2, input/output components** (sections 11–12): typed or spoken input, shown or spoken replies: terminal, desktop voice, headset client, speech-to-text and text-to-speech services.
- **Shared** (sections 13–17): security, changes to other repos, tests, open questions, order of work.

## 1. Purpose and scope

A person, typically wearing a headset running a PVLink app (PVAR on Quest), develops a visualization by talking or typing. A language model turns each request into changes to a live ParaView GUI session; DMLink sends the result through the DM to the headset. When the visualization is right, it is saved as a ParaView state and **baked**: run offline through every time into a DM project that headsets can play on their own.

In scope: the authoring model, pvbridge's network interface, the ParaView helper layer, the agent, the Part 1 API, save and bake, the front ends and speech services, the DM and DMLink changes they need, and tests.

Out of scope: on-headset language models; ABR styling; multi-user arbitration beyond "one request at a time"; volumes in PVLink (deferred in the DM spec).

## 2. What exists

| Piece | Where | State |
|---|---|---|
| pvbridge: run Python in the live ParaView GUI through a private file mailbox | `~/pvbridge/pvbridge.py`, `pvsend` | Working on Mac ParaView 6.1.1 |
| Model-agnostic agent: one tool (`run_paraview`), backends for Claude, OpenAI-compatible servers (TACC SambaNova endpoint, Ollama), reference | `~/pvbridge/voice/pvagent.py`, `models.json`, `system_prompt.md` | Working; 37-case desktop test set passes with the reference backend; DeepSeek-V3.2 passed 28 of its first 29 |
| Desktop voice loop: push-to-talk, Whisper, `say` | `~/pvbridge/voice/pvvoice.py` | Built; microphone not yet tested |
| DMLink plugin: DMLink Domain (address, institution, project, state, override bounds) and DMLink Sender (one KeyData item per Sender, explicit or automatic `ItemName`; colour-by and colormap from the Sender's display; visibility follows the Sender's display; `TimeMode` Auto/Timed/Static; one batch per render; removes the item when the Sender is deleted) | `DataManager/paraview/DMLink.py` | Tested headless in pvpython; GUI use to be confirmed (15.1) |
| DM, Unity package, PVAR with on-device cache | DataManager, PVLink, PVAR repos | Working on Quest |

## 3. Principles

1. **The ParaView GUI holds all the state.** Pipeline, names, parameters, colouring, visibility and time live in ParaView, visible in the Pipeline Browser and Properties panel. No other component holds pipeline state; Part 1 reads it over the bridge for every request (8.2). Edits made by hand in the GUI are therefore picked up automatically.
2. **The DM is a passthrough while authoring.** It carries geometry to headsets and holds only what is currently shown. Requests and replies never go through the DM.
3. **ParaView owns time while authoring.** Senders created by Part 1 are Static (untimed): the DM holds one current version of each item, replaced when ParaView's time or the pipeline changes. Headsets show ParaView's current time.
4. **Only what is shown is sent.** Intermediate objects stay in ParaView. Removing something from the headset deletes its Sender, which deletes the item from the DM.
5. **Names are shared.** The name a person gives an object is its ParaView registration name and its headset item name.
6. **Time-varying output comes from baking,** not from authoring (section 10).
7. **Components meet over IP.** Each can run on any machine; only text, small control messages and the occasional screenshot cross between them. Geometry goes through the DM.

## 4. Architecture and deployment

```
 front end (terminal, desktop voice, headset)                     [Part 2]
   │ audio            ▲ text, status
   ▼                  │
 speech-to-text service (Whisper)      text-to-speech (front end or service)
   │ text
   ▼
 NL interface: agent + Part 1 API ────HTTP────▶ model (TACC, Claude, 4090)   [Part 1]
   │ code, ▲ results                                                    
   ▼       │  TCP, token
 pvbridge listener ─▶ in-memory queue ─▶ main-thread timer in the ParaView GUI
                                            └─▶ DMLink ─▶ DM ─▶ headsets (geometry)
```

| Component | Runs on | Talks to |
|---|---|---|
| ParaView GUI + pvbridge + DMLink | Mac, or an ls6 DCV node | DM (geometry); accepts commands from Part 1 |
| NL interface (Part 1) | Anywhere | Model endpoint; pvbridge; serves front ends |
| Model | TACC endpoint, Anthropic, the 4090 box | — |
| Speech-to-text, text-to-speech | Mac, the 4090 box, or inside a front end | Serves front ends |
| Front ends | Terminal, desktop, headset | Part 1; speech services |
| DM | As today | DMLink; headsets |

- Every component's address and token are configuration; moving one is a configuration change.
- Crossing networks (lab to an ls6 compute node) uses an ssh tunnel or VPN.
- Latency per request: speech-to-text about 1 s; model 2–5 s per call; each bridge round trip a few milliseconds on a LAN; then ParaView's compute and the DM transfer.

---

# Part 1: Natural-language interface

## 5. Authoring model

### 5.1 Objects

A **named object** is a ParaView pipeline item with a person-given or agent-given name. An object is **shown** when it has a DMLink Sender whose display is visible, which means it is in the headset.

### 5.2 Kinds of request

| Kind | Examples | Effect |
|---|---|---|
| Create | "open the Wavelet source", "load the plume particles" | New named object, shown as its native geometry (5.4) |
| Add a variable | "add a scalar for the X coordinates" | Same object, new variable: the new filter takes the object's name (5.3). The object is coloured by the new variable |
| Derive geometry | "contour wavelet at 123", "slice it through the middle", "outline of wavelet" | New named object from an existing one. **Replaces** its input in the headset (the input's Sender is deleted, so its item leaves the DM; the ParaView object stays), unless the request says "add", "also" or "back in", which keeps the input shown |
| Style | "colour it by X", "use viridis", "log scale", "make it semi-transparent" | Changes the shown object's Sender display; DMLink sends the colour-by array and colormap |
| Show / hide / remove | "hide the outline", "show wavelet again", "remove the contour" | Hide: Sender display invisible (kept in the DM). Show: re-show or re-add the Sender. Remove: delete the Sender (deleted from the DM); the ParaView object stays |
| Time | "next step", "go to 100", "play" | Changes ParaView's time; shown objects are re-sent |
| Ask | "what variables does wavelet have", "what's the range of X" | Answer only, no changes |
| Undo | "undo that" | Reverses the last request's changes (8.5) |
| Session | "save this visualization as hilat-bloom", "bake hilat-bloom", "start over" | Section 10 and 5.9 |

### 5.3 The name follows the data

When a variable is added to object *N*, the new filter (Calculator, Gradient, ...) is named *N*, and the previous holder of the name is renamed *N* `source` (then *N* `source 2`, ...). *N*'s Sender moves to the new filter and keeps its item name. So *N* always means the newest version of that data, to the agent, in the Pipeline Browser, and in the headset.

Senders always set `ItemName` explicitly, so renaming a pipeline item never renames, and therefore never deletes, a headset item.

### 5.4 Default representations

| Data | Sent as |
|---|---|
| Polygonal surface | As is |
| Points / particles (vertices) | Points |
| Lines, streamlines | Line strips |
| Volume (image data, structured or unstructured grid) | Its outer surface (what DMLink sends for a volume by default) |

Colouring of a new object: the variable the person names; otherwise the first non-ID scalar array; otherwise solid. The colormap range is the variable's range on that object at the current time, and is then held (`AutomaticRescaleRangeMode = "Never"`) until the person asks to rescale.

### 5.5 Colormaps are shared per variable

ParaView has one colormap per variable name, and DMLink sends one `Colormap/<variable>` item. Every object coloured by `X` uses the same colormap, so changing it for one changes it for all. This is intended; the agent says so when a change affects other shown objects ("that also changes the wavelet's colours").

### 5.6 Names

- Created objects (sources, loaded files): the agent proposes a name and waits for a yes or a different name: "Call it wavelet?". If the request already gave one ("open the wavelet source as wavelet"), no question.
- Derived objects: automatic name *input* + *operation* ("wavelet contour", "wavelet outline", "wavelet slice"), said in the reply, changeable ("call it the box").
- Variables the agent creates get the name the person said ("X").
- Names must be unique among ParaView sources; a clash is resolved by asking.
- Names may contain spaces but not `/`, `\` or `:` (they become DM keys, `KeyData/<name>`); the agent replaces those characters and says so.

### 5.7 Questions and confirmations

- Ask only when there is a real choice the request doesn't settle, and propose the likely answer so a "yes" completes it: "Contour on RTData?".
- A request that supplies everything gets no question.
- One question at a time; the pending question is part of the recent conversation (8.2).
- Confirm before: deleting objects from the pipeline, "start over", loading data larger than a configured size, baking.

### 5.8 References

"It" and "that" mean ParaView's active source. Every helper that creates or changes an object makes it active. So "colour it by X" after a contour applies to the contour.

### 5.9 Start over

Deletes every Sender, then asks whether to also delete the ParaView pipeline. Either way, the scratch project in the DM is then emptied completely (`pvl_clear_project`): its keys are listed with `dm_probe` (including items left by earlier ParaView sessions, which DMLink doesn't know about), untimed items are removed with `REMOVE_UNTIMED`, and timed ones (the domain bounds) are first rewritten untimed, which drops their timeline, then removed, all over DMLink's own connection. This stands in for `DELETE_PROJECT` (14, row 2).

## 6. pvbridge

### 6.1 Execution model (unchanged)

All ParaView calls run on ParaView's main thread, from a repeating VTK timer (200 ms) on the active view's interactor. Commands run in a persistent namespace with `paraview.simple`, `shot()` and the `pvlink` helpers (7). Results are captured output, the repr of a final expression, or the traceback.

### 6.2 In-memory message buffer

- A **listener thread** inside pvbridge accepts commands over TCP on one configured address and port. It only moves bytes: it checks the token, reads the request, puts it on an in-memory `queue.Queue`, waits for that request's result, and sends it back.
- The **timer** takes requests off the queue in order, runs each, renders, and hands the result back to the waiting listener.
- ParaView's main thread holds Python's GIL while idle, so listener threads run only when it releases it: the timer runs every 50 ms and sleeps 2 ms before and after running commands. Round trip for a trivial command is about 0.05 s.
- If the client has disconnected, the result is discarded. Queued requests are lost if ParaView exits; clients see the connection close.
- `pvbridge.stop()` closes the listening socket as well as the timer, so `start()` can be called again without the port being in use.
- The **file mailbox** stays for now as a second way in, feeding the same queue (local scripts, the shared home directory on ls6).

### 6.3 Protocol

HTTP/1.1 on the listener (so it can be tested with `curl`), `Authorization: Bearer <token>` on every request.

| Method and path | Body | Response |
|---|---|---|
| `POST /run` | JSON `{code, screenshot?: bool, timeout?: s}` | JSON `{status: "ok"|"error", seconds, output, png?: base64}` |
| `GET /status` | | JSON `{running, commands, queue_length, view}` |

The screenshot is returned in the response, because Part 1 may not share a filesystem with ParaView.

### 6.4 Everything reported

`output` includes, for the duration of the command **and the render after it**:
- printed output and the traceback, as now;
- DMLink's messages (it sends during the render, so the render is now inside the capture);
- VTK and ParaView errors and warnings (an observer on `vtkOutputWindow`); if any were logged, `status` is `"warn"`.

### 6.5 Starting

On macOS, pvbridge declares user-initiated activity (`NSProcessInfo beginActivityWithOptions`) while it runs, so App Nap doesn't throttle its timer when ParaView isn't the frontmost app.


pvbridge starts itself: an auto-loaded ParaView plugin file (like DMLink) or a startup script, which waits until a render view exists and then calls `start()` with the configured address, port and token. It restarts on the new active view if its view is closed.

## 7. ParaView helper layer (`pvlink`)

A Python module loaded into pvbridge's namespace by Part 1 before its first request, and again if pvbridge restarts. The model is told to use it; plain `paraview.simple` remains available for anything it doesn't cover.

### 7.1 Configuration

`pvl_config(institution="GDA", project="voice", dm_host="127.0.0.1", dm_port=9010, data_roots=[...], domain_scan="all")`, called by Part 1 from its configuration. `project` is the **scratch project** for authoring. Paths are on the ParaView machine.

### 7.2 Functions

| Function | Does | Returns |
|---|---|---|
| `pvl_describe(obj)` | Data type, number of points and cells, cell types, bounds, arrays with components and ranges at the current time, timesteps | dict |
| `pvl_create(kind_or_path, name)` | Creates a source (by ParaView source name) or opens a file (reader chosen by ParaView); names it; updates the domain (7.3); shows it | proxy |
| `pvl_add_variable(obj_name, expression, var_name)` | Calculator (or a given filter) on the object; applies 5.3; colours by the new variable | proxy |
| `pvl_derive(input_name, filter, name=None, replace=True, **props)` | Creates the filter on the named input, names it (5.6), shows it; if `replace`, `pvl_remove`s the input | proxy |
| `pvl_show(obj, color_by=None)` | Ensures the Domain; adds a Sender with `Time = "Static"` and explicit `ItemName`, or re-shows an existing one; hides `obj`'s own display so the view mirrors the headset (7.4) | Sender |
| `pvl_hide(name)` / `pvl_remove(name)` | Hide: Sender display invisible. Remove: delete the Sender | None |
| `pvl_color(name, var=None, preset=None, log=None, range=None)` | Colours the Sender's display (which is what DMLink sends) | None |
| `pvl_items()` | What the headset has: each item's name, input object, shown or hidden, colour-by, colormap preset and range, points, topology; DMLink's connection status | list of dicts |
| `pvl_snapshot()` | Compact text of the pipeline (names, types, inputs, key parameters), the shown items, the active source and ParaView's time (8.2) | str |
| `pvl_vocabulary()` | Object, variable and preset names, for speech recognition hints (9) | list |
| `pvl_find_data(pattern)` | Files under the configured data roots whose names match | list of paths |
| `pvl_save(name)` / `pvl_bake(name, ...)` | Section 10 | path / job id |

All functions raise with a plain message the model can act on ("no object named 'wavlet'; objects are: wavelet, wavelet contour").

### 7.3 Domain policy

- One DMLink Domain per session, configured from `pvl_config`. If the pipeline already has a Domain (a state loaded by hand), it is used, and its project is reported in the snapshot.
- **The domain is the union of the bounding boxes of the pipeline's source objects:** real sources (Wavelet, Sphere, ...) and readers, meaning pipeline items with no input whose output is a dataset (not text or tables). Filters don't count: a slice or contour lies inside its source's box, and a clip shouldn't shrink the domain.
- The union **only grows** during a session: it is recomputed when a source is created and when a source's bounds grow. Removing a source doesn't shrink it until "start over" or "reset the domain", so the headset's container doesn't jump while the person works.
- **A source that is an existing time series** contributes the union of its bounds over **all** its timesteps, computed once when it is created. VTK XML files (`.pvd`, `.vtp`, `.vtu`) don't record bounds, so this reads every timestep once; readers that report bounds without reading (image data's whole extent, origin and spacing) are used as is. `domain_scan="sample"` reads every *n*th timestep plus the last, for very long series; the domain then still grows if a visited step exceeds it.
- The result is kept on the Domain (its override ranges), so it is saved with the state and not recomputed.
- Sources whose bounds change over time without timesteps (an animated source parameter) grow the domain as they are seen; a bake runs through all times first (10.2).
- Interim implementation: `pvlink` sets the Domain's `OverrideBounds` and X/Y/Z ranges. Final: DMLink Domain computes it itself (14, row 6).

### 7.4 Display mirroring

The ParaView view shows the Senders' displays and hides their inputs, so the GUI view shows what the headset shows. Hiding or showing a Sender in the Pipeline Browser does the same in the headset.

## 8. Agent

### 8.1 Model and prompt

Built on `pvagent.Agent`: one tool, `run_paraview(code, screenshot)`, now calling pvbridge's `/run` (6.3). Any backend in `models.json`. The system prompt adds a PVLink section: the rules of section 5 and the helper reference of 7.2.

### 8.2 Per request

1. `pvl_snapshot()` over the bridge.
2. The model is called with the system prompt, the last few turns of conversation (default 6, which carries any pending question), then the snapshot followed by the request. Earlier snapshots are dropped from the history.
3. Tool loop, at most 10 calls.
4. `pvl_snapshot()` again; the difference is the request's **change record** (8.4, 8.5).
5. The reply.

Requests are handled one at a time; a request arriving while one runs is refused as busy.

### 8.3 Cancel

Cancel stops the tool loop before its next call. A ParaView command already running finishes (it can't be interrupted); the reply says what was done before stopping.

### 8.4 Checking for false success

If the request wasn't a question and the change record is empty, the reply says nothing changed ("I didn't change anything; ..."), whatever the model claimed. If any tool result had `status: "warn"` or `"error"` the model hasn't dealt with, the reply says so.

### 8.5 Undo

**Not available yet.** Driving ParaView's undo stack from a script works for a single property change (15.1) but crashed ParaView on 2026-11-20 when undoing a request that had created objects and animation tracks (`vtkSMUndoStack::Undo` → `vtkSMSessionProxyManagerInternals::ComputeDelta`, null pointer). An earlier crash came from assigning `AnimationScene.Cues` from Python while the timeline widget was open. So `pvl_undo` refuses, and the model reverses its last change directly (deletes what it created, sets values back). Requests are still grouped as one labelled step on the GUI's stack.

The intended replacement is the agent reversing a request from its change record (8.2, step 4): record each helper call's inverse (created → delete, property → old value, Sender removed → re-add) and replay them in reverse. One level of undo is required.

## 9. Part 1 API

HTTP, `Authorization: Bearer <token>`, on a configured address.

| Method and path | Body | Response |
|---|---|---|
| `POST /ask` | JSON `{text, request_id?}` | Returns when done: JSON `{request_id, reply, changed: bool, pending_question: bool, items, warnings, seconds, tool_calls}`; `409` if busy |
| `GET /progress/<request_id>` | | JSON `{state: "thinking"|"running"|"done"|"failed"|"cancelled", step, of, detail}` |
| `POST /cancel` | JSON `{request_id}` | `{ok}` |
| `GET /vocabulary` | | JSON list from `pvl_vocabulary()` |
| `POST /reset` | | Clears the conversation; the pipeline is untouched |
| `GET /status` | | Model, bridge reachable, DMLink connected, busy |

`items` is `pvl_items()` after the request, so a front end can show what the headset should now contain. Front ends poll `/progress` while `/ask` is outstanding.

## 10. Save and bake

### 10.1 Save

"Save this visualization as *name*" → `pvl_save(name)`: `SaveState` to `<visualizations dir>/<name>.pvsm` on the ParaView machine, with absolute data paths, and a sidecar `<name>.json` (date, model, the conversation's requests).

### 10.2 Bake

`bake.py`, run by `pvbatch` on a machine with the data, by hand or started by "bake *name*" as a background job on the ParaView machine:

```
pvbatch bake.py --state hilat-bloom.pvsm --institution GDA --project hilat-bloom
                [--host H --port P] [--times all|t0:t1|stride N] [--replace]
```

1. Loads the state (DMLink plugin auto-loaded).
2. Sets every DMLink Domain's project and DM state name to the targets, and the domain to the union of the sources' bounds over all times (7.3); sets every Sender's `Time` to Auto, so items with timesteps or animation tracks are timed and the rest are sent once.
3. For each selected time: set the animation time, render (DMLink flushes one batch), and wait until the DM confirms the batch is stored.
4. Reports progress; exits nonzero on any refused or failed batch.

The target project must be new or empty; `--replace` deletes it first (14, row 2). Part 1 reports a started bake's progress and completion in later replies.

### 10.2a As built (2026-11-23)

- `pvl_bake(name, steps=10, times="all", stride=1, replace=False, save=True)` saves the state as `<vis_dir>/<name>.pvsm` (plus `<name>.json`), then runs `pvbatch bake.py` detached on the ParaView machine; progress goes to `<name>.bake.json`, output to `<name>.bake.log`; `pvl_bake_status(name)` reads them.
- `bake.py` loads DMLink, the configured plugins (e.g. the Molar Point Sampler) and the voice helpers, empties the target project only with `--replace` (every key rewritten untimed, then removed), loads the state, points every DMLink Domain at `<institution>/<name>` (state `<institution>-<name>`), sets every shown Sender to Timed, deletes hidden ones, and steps the frames. Frames are the data's timesteps (with `--times`, `--stride`) or `--steps` frames at t = 0, 1, ...; stored animation ranges are spread over them. Each frame is a render, so DMLink sends one batch, and `DMLink.flush()` returns once the DataManager has stored it.
- Static items in a time series are stored once (the DataManager ignores an unchanged version), so baking costs only what changes.

### 10.3 What bakes

Anything that varies over time must be in the pipeline (data with timesteps) or in an Animation View track (for example an isovalue sweep), because only those are saved in the state. A setting changed by voice at one time is a fixed setting in the bake. "Sweep the isovalue from 50 to 250 over the run" creates an animation track.

### 10.4 Later

Bake on an ls6 compute node, next to large data, to a DM at TACC; headset DMs pull it through the upstream link (DM spec section 15).

---

# Part 2: Input/output components

## 11. Front ends

Every front end:
- sends text to Part 1's `/ask` and shows (or speaks) the reply;
- shows what was heard (for speech) before or with the reply, so misrecognitions are obvious;
- shows progress from `/progress` (thinking, running step *k* of *n*, done, failed);
- offers cancel (a button, or saying "stop" or "cancel", which is sent to `/cancel`, not as a request);
- holds no pipeline state.

| Front end | Input | Output | Notes |
|---|---|---|---|
| Terminal | Typing | Text | Successor of `pvagent.py`'s interactive mode, calling the API |
| Desktop voice | Push-to-talk (Enter), typing | Text and speech | Successor of `pvvoice.py`, calling the API and the speech services |
| Headset (Unity) | Push-to-talk on a controller button | World-space panel; reply audio | Component in the PVLink Unity package (or PVAR); Android `RECORD_AUDIO`; service URLs and tokens from the inspector, overridable by a file on the device |

**Headset time controls.** While authoring, time belongs to ParaView. Headset time controls (PVAR's thumbstick) send requests like "next step" through `/ask`, or a dedicated endpoint (16, item 1). They must not drive the DM clock in this mode.

## 12. Speech services

### 12.1 Speech-to-text

- An OpenAI-compatible `POST /v1/audio/transcriptions` server, so any implementation works: Whisper on the Mac (`stt_whisper.py` behind a small server) or faster-whisper on the 4090 box. A front end may instead use its own recognizer.
- The front end passes `prompt` = Part 1's `/vocabulary`, fetched before each recording, so names like "wavelet contour" and "RTData" are recognized.
- The system prompt tells the model to expect spoken forms: number words ("one twenty three"), spelled-out names ("R T data").

### 12.2 Text-to-speech

A front end's job: macOS `say` on the desktop; on the headset, Android's speech engine through Unity, or an OpenAI-compatible `POST /v1/audio/speech` service if that is simpler. Part 1 returns text only.

---

# Shared

## 13. Security

pvbridge runs arbitrary Python inside the user's ParaView for anyone who can reach it, and Part 1 does so on behalf of anyone who can reach *it*. So:
- Every listening component binds to one configured interface (the lab LAN or localhost), never a public address.
- Every request carries a token (`Authorization: Bearer`), separate per component (bridge, Part 1, speech services).
- Across networks, use an ssh tunnel or VPN; the token isn't encryption.
- Tokens and model keys live in `~/.pvbridge/keys.env` (mode 600) on each machine that needs them.

## 14. Changes elsewhere

| Row | Where | Change | Needed for |
|---|---|---|---|
| 1 | DM | `RESET_KEY`: drop every entry of a timed key | Re-baking a key; DMLink when a timed Sender's upstream changes (independent of this work) |
| 2 | DM | `DELETE_PROJECT` (with the writer lock), refused while a source writes it | "start over" on the scratch project; `bake --replace` |
| 3 | DMLink | Remove `Colormap/<var>` when no remaining item uses it | Clean scratch project |
| 4 | DMLink | Confirm Static Senders re-send on ParaView time changes in the GUI | Principle 3 (check; may need nothing) |
| 5 | PVAR / PVLink | Headset front end (11); time controls routed to Part 1 while authoring | Section 11 |
| 6 | DMLink | DMLink Domain sends the union of the bounding boxes of all source objects (7.3): over all timesteps for time-series sources, growing only otherwise; computed over all times when baking | Stable domain for hand-built pipelines and bakes |

## 15. Tests

### 15.1 First checks

In the GUI with DMLink loaded and a local `dm`:
- a pipeline change made through pvbridge reaches the DM as a new version (`DMLINK_VERBOSE=1`, `dm_probe`);
- a Static Sender re-sends when ParaView's time changes (14, row 4);
- whether commands run through pvbridge appear on ParaView's undo stack (8.5).

**Results 2026-11-20** (Mac, ParaView 6.1.1, `build/dm` of Nov 11, DMLink.py working copy):
- DMLink loads through the bridge (`LoadPlugin(path, ns=globals())`) and its messages ("connected", "batch", "untimed: n item(s)") come back in the command output (6.4).
- Wavelet extent change, a new Contour + Sender, and a new isovalue each produced a new item version in the DM. Static Senders are stored untimed (`KeyData/<name>.u.*`); `dm ls` lists untimed items under each time.
- A Static Sender on the plume reader re-sent at each time change (3024, 3296, 3261, 3508 points) and the store kept only one untimed version: row 4 needs no change.
- Deleting Senders removed their KeyData and Visibility items; `Colormap/RTData` stayed (row 3 confirmed).
- Undo: plain property changes are recorded one property per undo set ("Changed 'vtkSMSourceProxy'"). Wrapping changes in `GetUndoStackBuilder().Begin(label)` / `End()` / `PushToStack()` makes one labelled set; `Undo()`/`Redo()` restore it and DMLink re-sends. The GUI stack depth is 10.
- The Sender's time property is `Time` in Python (values "Auto", "Timed", "Static"), not `TimeMode`.

### 15.2 pvbridge

`/run` from another machine with and without the token; ordering under concurrent requests; a client that disconnects mid-command; `stop()`/`start()` reusing the port; screenshot in the response; DMLink and VTK messages captured (6.4).

### 15.3 Scenario tests (Part 1, typed)

Multi-turn scripted conversations against Part 1's API, checked after each turn with `pvl_items()` (names, shown or hidden, colour-by, topology) and, at the end, against the DM with `dm_probe`. The first scenario:

| Turn | Request (with scripted answers to questions) | Check after the turn |
|---|---|---|
| 1 | "Open the Wavelet source" → "wavelet" | Item `wavelet` shown, triangles |
| 2 | "Add a scalar corresponding to the X coordinates" | `wavelet` has array `X`, coloured by `X`; the pipeline has `wavelet source` and `wavelet` |
| 3 | "Now contour wavelet at 123" → "RTData. Call it wavelet contour" | `wavelet contour` shown (isosurface 123 of RTData); no `wavelet` item in the headset; `wavelet` still in the pipeline |
| 4 | "Colour it by X" | `wavelet contour` coloured by `X` |
| 5 | "Add the outline of the wavelet back in" | `wavelet outline` shown, lines; `wavelet contour` still shown |

Scripted answers are given only if the model asks; an unnecessary question doesn't fail the test but is counted. Also: false-success detection (8.4) with a model forced to reply without calling the tool; undo after each turn.

### 15.4 Model comparison

The same scenarios across models (TACC endpoint models, Claude, a 4090-class local model): pass rate, questions asked, tool calls, seconds per turn.

### 15.5 Part 2

Each front end against a mock Part 1 that echoes requests and fakes progress; speech-to-text accuracy on recorded requests, with and without the vocabulary prompt.

### 15.6 Bake

Bake the scenario and a time-varying one (the plume particles), then check with `dm_probe` that timed items have one entry per timestep, static items one untimed version, and the domain is fixed.

## 16. Open questions

1. **Headset time controls** while authoring: route through `/ask` as text, or a dedicated time endpoint on Part 1?
2. **Large data while authoring.** Recomputing and re-sending on every time change may be slow for large meshes; authoring could use a decimated or single-timestep version, with the bake using the full data.
3. **More than one speaker** (several headsets): one request at a time is enforced; whose question a pending question belongs to is not.
4. **Undo depth** beyond one level (8.5).

## 17. Order of work

**Part 1**
1. pvbridge: in-memory buffer and `/run` (6.2–6.3), output capture including the render, DMLink and VTK messages (6.4); 15.2. **Done 2026-11-20**, except checking DMLink messages and access from another machine.
2. First checks 15.1. **Done 2026-11-20** (results in 15.1).
3. `pvlink` helpers (7) and the PVLink prompt section; agent changes (8): snapshot, change record, false-success check, undo, cancel. **Built 2026-11-20**: `voice/pvlink.py`, `voice/system_prompt_pvlink.md`, `voice/nli.py` (typed CLI); the wavelet scenario (15.3) passes with gpt-oss-120b and DeepSeek-V3.2 at 1-4 s per turn, and nearly with gpt-oss-20b. pvbridge turns off App Nap while it runs (6.5).
4. Part 1 API (9); scenario tests 15.3 with the reference backend, then models (15.4). **API built 2026-11-23** as `voice/nli_server.py` (port 9030, token `NLI_TOKEN`): `/ask` takes JSON text or a raw WAV body (speech-to-text on the server) and can return the reply as speech (`/audio/<id>.wav`, made with `say`); plus `/progress`, `/cancel`, `/vocabulary`, `/reset`, `/status`. Tested from the Mac and reachable from the Windows box. Scenario tests still to do.
5. Save and bake (10) on the Mac to a local `dm`; 15.6. **Built 2026-11-23**: `pvl_save`, `pvl_bake`, `pvl_bake_status` in `voice/pvlink.py`, `voice/bake.py` (pvbatch). Tested: a no-time visualization with stored animation ranges (5 frames), the fire plume's sampled points (every 4th of 41 timesteps), refusing then replacing an existing bake, and a bake started from ParaView by the helpers.
6. pvbridge self-start (6.5).

**Part 2** (can start once 9's API is fixed, against a mock)
7. Terminal and desktop voice front ends on the API; speech-to-text service with the vocabulary prompt.
8. Headset front end (Unity), built on the Windows host.

**Elsewhere**
9. DM and DMLink changes, section 14 rows 1–3 and 6.
