# Handoff: ParaView voice + AR work, now run from the Windows machine

Start Claude Code in `C:\Users\gda` and say: *"Read HANDOFF-windows.md and continue."*
Written 2026-11-25 by the Claude Code session that drove this machine from the user's Mac over ssh.

Until now, Claude ran on the Mac (10.0.0.171) and drove this machine (DESKTOP-JCDB4VN, 10.0.0.49) over ssh. The user wants the work to continue here, and the first job (section 6) is to move the voice server off the Mac, so the system runs without it and gets GPU speech-to-text.

---

## 1. Rules from the user (standing)

- **Never print, echo or commit keys or tokens:** `SAMBANOVA_API_KEY` (the TACC LLM key), `PVBRIDGE_TOKEN`, `NLI_TOKEN`. On the Mac they're in `~/.pvbridge/keys.env` (mode 600). Copy them here without displaying them.
- **Commit or push only when the user asks.** "Commit" doesn't mean "push": ask before pushing.
- **Never commit the user's own uncommitted work** (section 4 lists it). Commit only the files your change touched.
- **Back up user files before overwriting them**, and confirm before deleting data or killing processes you didn't start.
- **On production servers, show the exact commands before any write;** the user runs or approves them.
- End commit messages with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

## 2. The system

Voice control of ParaView, with the results in augmented reality on a Quest 3 and on the Windows Unity editor.

```
 Quest 3 (PVAR app)                       Windows (this machine)                    TACC
 ───────────────────                      ──────────────────────                    ────
 hold X → records speech ──audio──►  voice server (nli_server.py, :9030) ──► LLM: gpt-oss-120b
 plays the spoken reply   ◄──WAV───   Whisper speech-to-text                     ai.tejas.tacc.utexas.edu/v1
                                       │  runs Python in ParaView via
                                       ▼  pvbridge (HTTP :9020, token)
                                     ParaView GUI + pvbridge  (local, or on an ls6 compute node)
                                       │  DMLink plugin writes the geometry
                                       ▼
 PVAR's own dm cache  ◄── TCP :9010 ── dm.exe (DataManager)  ◄── also Unity PVLink in the editor
```

**Today the voice server and Whisper run on the Mac**, which also holds the ssh tunnel to the ls6 node. Everything else is already here.

### Pieces
- **DataManager (dm):** a C++ server that stores visualization items (meshes, colormaps, states over time) and streams them to clients. A dm can be a **root** (owns the store) or a **cache** (`--upstream host:port`, holds what its clients asked for). The Quest's PVAR runs a cache in-process, store `/sdcard/DataManager/store`.
- **PVAR:** the Quest/iPhone app. AprilTags colocation puts the data at a fixed room position; PVLink draws what the dm sends. Thumbstick up/down cycles states ("GDA-bake 2 of 5" = 2nd of 5 states, sorted by name); left/right steps time. X = push-to-talk.
- **pvbridge** (`pvbridge.py`): started inside a ParaView GUI; serves `POST /run` and `GET /status` (Bearer `PVBRIDGE_TOKEN`) and a file mailbox at `~/.pvbridge/in`; a Qt timer runs each request on ParaView's main thread.
- **Voice server** (`voice/nli_server.py`): takes the headset's audio (or text), transcribes it with Whisper, asks the LLM, which has one tool, `run_paraview(code, screenshot)`. The `pvl_*` helpers in `voice/pvlink.py` (show, derive, streamlines, bake, …) are sent into ParaView when their hash changes; the system prompt (`system_prompt_pvlink.md`) is reread on each request. `--config file.json` sets ParaView-side paths for a ParaView on another machine.
- **talk.py:** Mac mic/speaker client (trigger word "please"). Not needed when the headset is the mic.
- **Bakes:** `pvl_bake` saves the state and runs `pvbatch voice/bake.py`, writing every time step as its own project/state (`GDA/<name>`, `GDA-<name>`) to the dm.

---

## 3. This machine

- Windows 11, RTX 4090 Laptop (16 GB, driver 591.86), Python 3.13 (`C:\Users\gda\AppData\Local\Programs\Python\Python313`), ParaView 6.1.1 (`C:\Program Files\ParaView 6.1.1\bin`: paraview, pvpython, pvbatch), ffmpeg (`C:\ffmpeg\bin`), VS 2022 Build Tools, Unity 6000.3.10f1 (and 6000.4.11f1).
- **adb:** `C:\Program Files\Unity\Hub\Editor\6000.3.10f1\Editor\Data\PlaybackEngines\AndroidPlayer\SDK\platform-tools\adb.exe`; the Quest 3 is on USB (serial 2G97C5ZH8601MX). The headset must be awake and worn for apps to run; PVAR pauses when it's off the head.
- **Firewall:** inbound rules "DataManager" (dm.exe) exist. Port 9030 (voice server) has no rule yet.
- **ssh config** (`~/.ssh/config`) has hosts f1, vista, s3 but **not ls6** (section 6.4).
- Git over https uses the Windows credential manager, which works in your interactive session. (It failed only for the old ssh-driven sessions; that's why earlier work moved commits as bundles via the Mac.)

### Unity Quest builds
Each app has `Tools > <App> > Build Quest APK`, or in batch mode:
```
"C:\Program Files\Unity\Hub\Editor\6000.3.10f1\Editor\Unity.exe" -batchmode -quit -projectPath C:\Users\gda\Unity\PVAR -buildTarget Android -executeMethod PVARApp.BuildQuest -logFile C:\Users\gda\pvar-build.log
```
(`SurveyApp.BuildQuest`, `ABRARApp.BuildQuest`, …). The APK goes to `<project>\Builds\Quest\<App>.apk`; install with `adb install -r`. **Check the log for `Quest build Succeeded` before installing.** A batch build can't run while the project is open in the editor. Packages from the private DataManager repo download only when git can authenticate.

### DataManager build
`C:\Users\gda\DataManager-cache` (branch main, a784dd2; a git worktree of `C:\Users\gda\DataManager`) builds with `build-win.bat` (vcvars64 + CMake/Ninja). The installed server is `C:\Users\gda\DataManager\build-win\dm.exe` (previous one kept as `dm.exe.afc7239`). Android builds need the junction `C:\Users\gda\ndk-r27c` → Unity's NDK (the spaced path breaks CMake).

### dm stores here
| Store | Role | Run |
|---|---|---|
| `C:\Users\gda\dmvoice` | root, id b361acfe…; **the Quest's cache belongs to this root**. Holds states GDA-AAA, GDA-bake (the fire-plume bake from ls6), GDA-Plume 1, GDA-plume1, GDA-voice | `dm.exe --store C:\Users\gda\dmvoice` |
| `C:\Users\gda\dmcache-ls6` | cache of the ls6 node's root, via the Mac tunnel | `dm.exe --store C:\Users\gda\dmcache-ls6 --upstream 10.0.0.171:9010` |

No dm is running right now. Processes started from ssh die with the session; from your own session that isn't an issue.

---

## 4. Repositories (all GregAbram on GitHub)

| Repo | Here | State |
|---|---|---|
| **DataManager** (private) | `C:\Users\gda\DataManager` (still on old local branch `pvlink-points`, with the user's PVLink example edits: OpenXR settings, packages-lock) and its worktree `DataManager-cache` (main) | `main` = a784dd2, the only branch. Its `origin/*` refs here are stale; `git fetch` once |
| **PVBridge** | not cloned here | `main` = 1eb425c. Clone it for section 6 |
| **PVPlugins** | not cloned here | ParaView plugins MolarPointSampler, AdvectionSampler, the fire-plume generator |
| **PVAR** | `C:\Users\gda\Unity\PVAR`, branch `voice` | `main` = `voice` = fd526de: AprilTags 0.8.0, DataManager a784dd2, data at (0, 1.4, −2) in the room_base frame. Uncommitted: Unity/Oculus settings churn (user's) |
| **AprilTags** | `C:\Users\gda\Unity\AprilTags-dev` is an old uncommitted 0.8.0 copy, no longer used by any project | `main` = 0.8.0, f0b051e, tag v0.8.0; `release-0.7.1` kept |
| **Survey** | `C:\Users\gda\Unity\Survey` (main 8393a75, pins AprilTags 0.8.0) | its old `file:` pointer to AprilTags-dev is backed up in `C:\Users\gda\Unity\Survey-packages-backup-20261125` |
| **ABRAR** | `C:\Users\gda\Unity\ABRAR` (main 13e362c) | created 2026-11-25; uses Git LFS (174 files). Pins DataManager a784dd2 + AprilTags 0.8.0 but **not built yet**: the batch build couldn't download the private DataManager packages over ssh. The user is opening it in the Unity editor to resolve them; then build and install |
| Locator, SharedAR | `C:\Users\gda\Unity\…` | not repinned: Locator AprilTags 0.7.0, SharedAR a5f898f |

PVAR, Survey, ABRAR and PVAR's dependencies are pinned by full commit hash, all on GitHub. Both PVAR and Survey were rebuilt with AprilTags 0.8.0 and installed on the Quest on 2026-11-25 (not yet tried on the headset).

**AprilTags 0.8.0 in one line:** a new survey starts from `/sdcard/AprilTags/room_base.json` (on the Quest: tag 7 at y 1.4224, yaw 0, so the origin is on the floor below tag 7, +Z into its wall), and snap to walls turns learned tags about the anchor instead of the whole room about the origin. PVAR and ABRAR place data at (0, 1.4, −2) in that frame.

---

## 5. TACC ls6 (ParaView on a compute node)

- `$WORK` = `/work/01249/gda/ls6`. Login host `staff.ls6.tacc.utexas.edu` (MFA). Compute nodes are reachable only through the login node (`ssh -J ls6 <node>`). Use the `normal` queue, not `development`.
- On ls6: `$WORK/DataManager` (main a784dd2, built with gcc 13 + static libstdc++), store `$WORK/dmstore` (the bake `GDA/bake`, 288 MB), `$WORK/pvbridge` (hand-copied files; could be a clone now), `$WORK/ParaViewPlugins/MolarPointSampler.py`, data in `/work/01249/gda/data/fire_plume`. ParaView 6.1.1 at `/opt/apps/intel24/paraview/6.1.1/bin`.
- **Last run (c311-011, DCV, job now ended):** on the node, `dm --store $WORK/dmstore --host 127.0.0.1 --no-discovery` (log `$WORK/dm-node.log`), and in ParaView's Python shell:
  ```
  import importlib.util, sys; _spec = importlib.util.spec_from_file_location("pvbridge", "/work/01249/gda/ls6/pvbridge/pvbridge.py"); pvbridge = importlib.util.module_from_spec(_spec); sys.modules["pvbridge"] = pvbridge; _spec.loader.exec_module(pvbridge); pvbridge.start()
  ```
  The Mac then held `ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -J ls6 -L 9120:127.0.0.1:9020 -L 0.0.0.0:9010:127.0.0.1:9010 <node>`, and the voice server ran with `PVBRIDGE_URL=http://127.0.0.1:9120 … --dm 127.0.0.1:9010 --config remote-ls6.json` (`voice/remote-ls6.json` in PVBridge holds the ls6 paths).

---

## 6. Next job: run the voice side here instead of on the Mac

Nothing below has been tried. Suggested order:

1. **Clone PVBridge** (and PVPlugins if local ParaView should load the samplers) under `C:\Users\gda`. The voice server is standard-library Python apart from the Whisper worker.
2. **GPU Whisper.** `voice/pvvoice.py` `LocalWhisper` starts `stt_whisper.py <model>` and speaks one JSON line per request (`{"wav", "prompt"}` → `{"text", "seconds"}`); `HttpWhisper` posts to any OpenAI-compatible `/v1/audio/transcriptions`. Either add a faster-whisper (CUDA) worker with the same JSON protocol, or run a local OpenAI-compatible Whisper server and pass `--stt http://127.0.0.1:8000/v1`. Try `large-v3` or `turbo`. Keep passing the vocabulary prompt (pipeline names) as `initial_prompt`. The Mac used `base.en` on CPU.
3. **Spoken replies.** `nli_server.py` line ~106 makes the reply WAV with macOS `say -o … --data-format=LEI16@22050`. Replace it on Windows (Piper for a good local neural voice, or System.Speech) and keep a 16-bit PCM WAV that PVAR can play.
4. **Keys and config.** Copy `keys.env` from the Mac (`~/.pvbridge/keys.env`) to `%USERPROFILE%\.pvbridge\keys.env` without printing it; check how `nli_server.py` and `pvagent.py` locate it on Windows. The LLM entry is `sn-gptoss120` in `voice/models.json`. Make a `--config` JSON with Windows paths for local ParaView (pvbatch, bake.py, dm_probe, DMLink plugin, vis_dir), like `remote-ls6.json`.
5. **Run it:** `python voice\nli_server.py -m sn-gptoss120 --dm 127.0.0.1:9010 [--config …]`, plus a firewall rule allowing inbound TCP 9030. Start a dm root (`dmvoice`) and ParaView with pvbridge, locally or on ls6.
6. **Point the headset here:** PVAR reads `pvar_voice.json` from its app files folder (on the Quest `/sdcard/Android/data/edu.utexas.tacc.pvar/files/`): `{"service": "http://10.0.0.171:9030", "token": …, "button": "X"}` → change the service to `http://10.0.0.49:9030` (pull it with adb, edit, push back; don't print the token).
7. **ls6 from here:** add an `ls6` host to `~/.ssh/config`. Windows OpenSSH has no ControlMaster, so each tunnel needs the MFA login. With the tunnel ending here, `dm.exe --upstream 127.0.0.1:9010` can use the node directly.

## 7. Open issues

- **Headset not storing data from a cache-of-a-cache (unresolved).** With Windows as a cache of the ls6 root (`dmcache-ls6`, root 2f51…), PVAR logged "linked (current state)" but wrote nothing, while Unity PVLink on Windows got the data. Likely cause: the Quest's cache records root b361… (dmvoice) and its upstream then came from another root. Serving the bake from `dmvoice` as root worked (the Quest cached all 290 MB).
- **Node dm resend loop:** `$WORK/dm-node.log` shows "client 127.0.0.1 dropped 1 item(s)" ~10/s for minutes, and "cannot read GDA/voice/Bounds/domain … domain.u.json". Not investigated.
- **Untried shortcut for bakes:** with PVAR stopped, `adb push` a project dir and its `.states/<state>.json` into `/sdcard/DataManager/store`; with no `.dm-cache.json` entry it's never pruned.
- **Deferred designs:** headless pvbridge (a `serve()` mode in pvbatch, so a node job needs no GUI); DataManager SPEC 15.9 steps 3–5 (cache index, fetch, pin) so a cache can hold a whole bake.
