# SensorChrono

**A guided desktop app for time-aligned, multi-modal recording.** SensorChrono
walks an operator through a single safe workflow — *select equipment → liveness
check → calibrate → record → auto post-process* — and produces a
**drift-corrected, lag-calibrated, audit-certified** dataset from a Shimmer3
ECG/EMG unit, **any UVC webcam and any input microphone** (operator-selected), and
a USB keyboard, all recorded to one `.xdf` via a bundled LabRecorder.

It wraps a proven Lab Streaming Layer (LSL) capture + analysis core (the
`sensorchrono/bridges/` capture bridges and the `analysis/` pipeline) in a
wizard so the timing math is correct *and* the operator can't mis-record.

<!-- Drop a screenshot at docs/img/sensorchrono.png to show it here. -->
<!-- ![SensorChrono wizard](docs/img/sensorchrono.png) -->

---

## Download for Windows

Grab the latest installer from the **[latest release](https://github.com/ksu-oor/sensorchrono/releases/latest)** —
that page always points at the newest build and is updated automatically on every
merge to `main`:

> **`SensorChrono-<version>-setup.exe`** — a single download that includes the app
> **and a bundled LabRecorder**. No separate LabRecorder install needed.

The asset filename carries the version (e.g. `SensorChrono-1.0.3-setup.exe`), so the
[latest-release page](https://github.com/ksu-oor/sensorchrono/releases/latest) is the
stable place to grab the current installer; a specific version lives on its own
release under **[Releases](https://github.com/ksu-oor/sensorchrono/releases)**.

**SmartScreen note:** the installer isn't code-signed yet, so Windows SmartScreen
may warn on first run. Click **More info → Run anyway**. (It's a normal Inno Setup
installer; the source and build pipeline are in this repo under `build/` and
`.github/workflows/release.yml`.)

## Quick start (installed app)

1. Launch **SensorChrono** from the Start menu / desktop shortcut.
2. **Set up recording** — enter participant / session / task / duration. Leave
   *dry run* unticked for a real capture, then under **Hardware bindings** pick the
   auto-detected **Shimmer COM port**, **camera index**, and **microphone**
   (**Rescan devices** re-scans, including probing for cameras). The choices persist
   to `~/.sensorchrono/config.yaml`, so you bind the rig once.
3. **Liveness check** — every selected stream must show live traffic before you
   can proceed (this is the structural fix for "forgot to tick a stream").
4. **Calibrate** — perform the 30-second calibration block: **10–20 firm spacebar
   presses ~2 s apart**. This is what makes audio/video lag measurable in-situ.
5. **Record** — SensorChrono drives the bundled LabRecorder over its Remote
   Control Server, so you never touch LabRecorder by hand.
6. **Done** — it auto-runs the 5-stage post-processing pipeline and shows a single
   **PASS / WARN / FAIL** verdict with the corrected dataset.

See **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** (operators) and
**[docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)** (admins) for the full walkthrough,
and **[docs/HARDWARE.md](docs/HARDWARE.md)** for device wiring (Shimmer pairing,
electrode placement, packet/timing reference).

---

### Optional EMOTIV headset

SensorChrono can launch an EMOTIV Cortex bridge alongside the Shimmer,
camera, microphone, and keyboard bridges:

1. Install and start **EMOTIV Launcher**, then connect the headset.
2. Register a Cortex application in the EMOTIV developer portal.
3. Create a text file containing:

   ```text
   CLIENT_ID=your_client_id
   CLIENT_SECRET=your_client_secret
   ```

4. On **Set up recording**, enable **EMOTIV Cortex**, optionally enter the
   headset ID, and select that credentials file. Environment variables
   `EMOTIV_CLIENT_ID` and `EMOTIV_CLIENT_SECRET` can be used instead.
5. Accept the Cortex access request in EMOTIV Launcher on first use.

The bridge publishes `EmotivEEG` and `EmotivMotion`; Cortex determines the
actual channel layout, so Insight and other Cortex-supported models are not
hard-coded. EMOTIV samples use the local LSL clock at WebSocket receipt time.
SensorChrono exports them but does not claim Shimmer-style device-clock or
acquisition-lag correction for these streams.

## Run from source (developers)

SensorChrono is a normal Python package. The GUI and live bridges need real
hardware + native libs (Windows in practice), but the **app shell, analysis
pipeline, and full test suite run anywhere**.

```bash
pip install -r requirements.txt          # runtime deps (PySide6, numpy, pylsl, …)

python -m sensorchrono                    # launch the GUI
python -m sensorchrono --info             # environment + profiles summary (no GUI)
```

Develop + test:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                                 # hardware-free; GUI/LSL tests self-skip if deps absent
```

Build the Windows installer locally (Windows + the project venv):

```powershell
build\build_windows.ps1                   # PyInstaller one-folder -> dist\SensorChrono\
# then compile build\installer.iss with Inno Setup
```

See **[build/PACKAGING.md](build/PACKAGING.md)** for the packaging strategy and
**`.github/workflows/release.yml`** for the canonical CI build.

### Releases are automatic

Every merge to `main` builds the Windows installer and publishes it to
**[Releases](https://github.com/ksu-oor/sensorchrono/releases)** with the **patch
number auto-incremented** (`1.0.0 → 1.0.1 → …`). To cut a **minor/major** release,
bump `__version__` in `sensorchrono/__init__.py` (e.g. to `1.1.0`) and merge — the
pipeline releases exactly that and resumes auto-patching from there. The version
authority is `build/next_version.py` (the latest git tag + that floor); it is
unit-tested in `tests/test_next_version.py`. Add `[skip release]` to the merge
commit **subject** (first line) to skip the build for a trivial change. See
[the design note](docs/superpowers/specs/2026-06-05-auto-release-versioning-design.md)
for the full rule.

## Post-processing on its own

The analysis pipeline is independent of the GUI — point it at any recorded `.xdf`:

```bash
python -m analysis.postprocess PATH/TO/recording.xdf --out-dir OUT/
python -m analysis.postprocess recording.xdf --mp4 recording.mp4
```

It emits `OUT/pipeline_report.md`, per-stream CSVs with corrected timestamps, a
frame-index↔LSL-time map for the MP4, and a PASS/WARN/FAIL verdict. Each module
also runs standalone: `analysis.recording_audit`, `analysis.shimmer_clock_model`,
`analysis.insitu_lag_calibration`.

> **Timing honesty:** audio and video absolute lag are fully *measured* in-situ.
> `ShimmerECG` absolute lag is only a **lower bound** (Bluetooth one-way minimum) —
> it excludes internal ADC/filter delay, so don't claim sub-ms ECG-to-physical sync.

---

## Hardware support

| Modality | Device | LSL stream(s) |
|---|---|---|
| EEG / motion | EMOTIV Cortex-supported headset | `EmotivEEG`, `EmotivMotion` |
| ECG / EMG | Shimmer3 EXG (Bluetooth) | `ShimmerECG` / `ShimmerEMG`, `ShimmerMarkers`, `ShimmerDiagnostics_ECG` |
| Video | Any UVC webcam (operator-selected index) | `VideoFrames` (+ `.mp4` + `frames.csv`) |
| Audio | Any input device (operator-selected, or system default) | `Audio` (48 kHz) |
| Fiducial | USB HID keyboard | `KeyboardFiducial` |

Per-device calibration lives in `profiles/*.yaml`. Wiring details, electrode
placement, and the Shimmer packet/timing reference are in
**[docs/HARDWARE.md](docs/HARDWARE.md)**.

## Project layout

```
sensorchrono/            the app
  bridges/               the 4 capture bridges (one LSL stream per modality)
  devices/               DeviceAdapter ABC + real bridge drivers + simulated (dry-run)
  orchestration/         supervisor, lsl_monitor, preflight, labrecorder (+ launcher), session FSM
  ui/                    PySide6 wizard pages + live video/waveform widgets
analysis/                post-processing pipeline (drift + lag + audit) — library + CLI
build/                   PyInstaller spec, frozen entry, Inno Setup installer
profiles/                per-device YAML calibration
docs/                    USER_GUIDE, SETUP_GUIDE, HARDWARE
tests/                   pytest suite (runs without hardware)
.github/workflows/       ci.yml (Linux tests) · release.yml (Windows installer)
```

## Licensing

SensorChrono is developed at Kennesaw State University; see the repository for its
license. The Windows installer bundles **[LabRecorder](https://github.com/labstreaminglayer/App-LabRecorder)**
(© the LabStreamingLayer authors, **MIT License**) under `LabRecorder/`; its
license travels inside the bundle.
