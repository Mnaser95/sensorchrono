# SensorChrono Technical Summary
**Date:** 2026-06-18

---

## 1. What is SensorChrono?

SensorChrono is a guided desktop application for time-aligned, multi-modal physiological data capture and post-processing. It walks an operator through a single safe workflow — select equipment → liveness check → calibrate → record → auto post-process — and produces a **drift-corrected, lag-calibrated, audit-certified** dataset from:

- Shimmer3 ECG/EMG unit (Bluetooth)
- Any UVC webcam (operator-selected)
- Any input microphone (operator-selected)
- USB keyboard (fiducial)

All streams are recorded to a single `.xdf` file via a bundled LabRecorder, then post-processed into a corrected dataset.

---

## 2. How LSL Clock Sync Works

### The protocol

LSL (Lab Streaming Layer) runs a clock synchronization protocol across machines on a network. When an **inlet** (reader/recorder) connects to an **outlet** (stream source), LSL periodically runs a sync exchange:

```
Inlet sends:    sync request → outlet          (records send time T1)
Outlet replies: here is my clock time T_out
Inlet receives:                                (records receive time T4)
```

- `T1` — inlet's local clock when it sends the sync request
- `T_out` — outlet's local clock when it receives the sync request (on the outlet's machine)
- `T4` — inlet's local clock when it receives the outlet's reply

From these three values:
```
RTT    = T4 - T1
offset = T_out - (T1 + RTT/2)
```

The offset answers: "the outlet's clock reads X — what does the inlet's clock read at that same moment?" LSL runs this exchange continuously throughout the recording and uses minimum-RTT observations for stability (same idea as NTP).

### What LSL does NOT use

LSL's sync protocol operates entirely on timing of control packets. It never looks at stream data (ECG values, audio samples, etc.).

### Applying the offset

For every incoming sample:
```
inlet_time = outlet_timestamp - offset
```

This converts all stream timestamps to the inlet's (recorder's) local clock — a common time reference.

---

## 3. What LabRecorder Does

LabRecorder's responsibilities:

- Calls `pull_sample()` on each inlet in a loop
- Writes samples + corrected timestamps into the XDF file
- Optional **`dejitter_timestamps`** pass: fits a linear model per stream to smooth residual per-sample jitter caused by OS scheduling (the `pull_sample()` loop doesn't fire at perfectly even intervals)

**Key point:** dejitter smooths OS scheduling noise but does not correct for crystal drift in embedded devices.

---

## 4. The Division of Responsibility

| Layer | Who handles it |
|---|---|
| Multi-PC clock alignment | LSL |
| Writing to XDF | LabRecorder |
| Per-sample OS scheduling jitter | LabRecorder (dejitter) |
| Starting and managing capture bridges | SensorChrono |
| Shimmer crystal drift | SensorChrono |
| Bluetooth jitter | SensorChrono |
| Absolute audio/video acquisition chain lag | SensorChrono |

---

## 5. The Bridge

A **bridge** is a Python script that sits between a hardware device and LSL. The Shimmer speaks its own binary protocol over Bluetooth/serial — it sends raw packets containing tick counters and ADC values. LSL has no way to communicate with the Shimmer directly.

The bridge:
1. Opens the serial port and speaks the Shimmer protocol
2. Extracts values from each packet (tick counter, ECG/EMG voltages)
3. Calls `outlet.push_sample()` to hand values to LSL

```
Shimmer hardware
      ↓  (Bluetooth serial packets)
bridge script (shimmer_lsl_bridge.py)
      ↓  (push_sample calls)
LSL network
      ↓  (pull_sample calls)
LabRecorder → XDF file
```

SensorChrono has four bridges: Shimmer, video, audio, keyboard — one per modality.

---

## 6. dev_ts vs arrival_lsl

Both are timestamps for the same physical sample, measured by different clocks:

**`dev_ts`** — the Shimmer's internal tick counter at the moment the ADC sampled the signal. Stamped by the Shimmer's crystal oscillator inside the hardware, before data ever leaves the device. This is the closest thing to "when the physical event actually happened."

**`arrival_lsl`** — `pylsl.local_clock()` called on PC-B at the moment the Bluetooth packet landed in the bridge's serial buffer. Stamped by the PC's clock, after data traveled over Bluetooth.

The gap between them:

```
dev_ts                    arrival_lsl
  |                            |
  |←──── Bluetooth delay ─────→|
  |←──── crystal drift   ─────→|
```

- **Bluetooth delay**: variable, noisy, always positive (packets can only arrive late)
- **Crystal drift**: Shimmer's crystal ticks at a slightly different rate than the PC clock, so the gap grows linearly over time

`dev_ts` is what you want — it reflects when the sample was taken. `arrival_lsl` is what you have — it reflects when the PC noticed.

---

## 7. Crystal Drift

A crystal oscillator vibrates at a specific resonant frequency. The Shimmer uses a 32,768 Hz crystal — supposed to vibrate exactly 32,768 times per second. The actual frequency depends on manufacturing tolerances, temperature, and aging.

Instead of ticking at exactly 32,768 Hz, the Shimmer's crystal might tick at 32,769.147 Hz. That difference in parts per million (ppm):

```
drift = (32769.147 - 32768) / 32768 × 1,000,000 ≈ 35 ppm
```

Accumulated error over time:
```
after 1 second:   0.035 ms
after 1 minute:   2.1 ms
after 1 hour:     126 ms
after 2 hours:    252 ms
```

This is perfectly linear and predictable — which is why a linear model can correct it completely.

---

## 8. Numeric Example: dev_ts, arrival_lsl, Crystal Drift, and Bluetooth Jitter

**Setup:**
- Shimmer crystal: 35 ppm fast (actual rate 32,769.147 Hz)
- Bluetooth jitter: random 0–20 ms
- Recording starts at LSL time = 1000.000 s

**At t = 1 hour true time:**

```
True time:      4600.000 s   (what we want)
dev_ts:         4600.126 s   (Shimmer crystal is 126ms fast)
arrival_lsl:    4600.141 s   (dev_ts + 15ms Bluetooth jitter)
```

XDF records `4600.141` for a sample that truly occurred at `4600.000`. Total error = **141 ms**.

**What the diagnostics sidecar captures (per 10s window):**

| Window | min(arrival_lsl - dev_ts) |
|---|---|
| 0–10s | 0.006 s |
| 10–20s | 0.006 s |
| ... | ... |
| 3590–3600s | 0.006 s |

Theil-Sen fits:
```
lsl_time = a + b × dev_ts
b ≈ 0.999965    (corrects 35ppm crystal)
```

**Corrected timestamp:**
```
corrected = a + b × dev_ts
          = a + 0.999965 × 4600.126
          ≈ 4600.000 s    ← recovers true time
```

Bluetooth jitter (the 15ms) is bypassed entirely — the corrected timestamp comes from `dev_ts`, not `arrival_lsl`.

---

## 9. Shimmer Crystal Drift Correction (Post-hoc)

### What SensorChrono records in the XDF

**ECG/EMG stream** — each sample carries `arrival_lsl` as its LSL timestamp (raw, uncorrected).

**`ShimmerDiagnostics_ECG` sidecar stream** — each sample carries `arrival_lsl - dev_ts` (the raw observed offset between the two clocks).

### The correction algorithm

1. **Bin** diagnostic samples into 10-second windows
2. **Per window**, keep only the minimum observed offset (lowest-latency packet = least Bluetooth jitter = cleanest clock pair)
3. **Theil-Sen fit** on the resulting `(dev_ts, lsl_time)` bin pairs:
   ```
   lsl_time = a + b × dev_ts
   ```
4. **Apply** to every ECG/EMG sample:
   ```
   corrected_lsl_ts = a + b × dev_ts
   ```

The slope `b` captures crystal drift; `(b-1) × 1e6` is drift in ppm. The intercept `a` is the LSL time when `dev_ts = 0`.

### Why post-hoc is better than online

| | Online (old code) | Post-hoc (SensorChrono) |
|---|---|---|
| When | Per-sample, real time | After recording, from XDF |
| Method | Running minimum + EMA | Theil-Sen on full recording |
| Early sample quality | Noisy estimate | Benefits from full-recording data |
| Stored in XDF | Already-corrected timestamps | Raw + diagnostics sidecar |

The online estimate is causal (only past data). The post-hoc fit is non-causal (full recording at once) — early samples benefit from information not available until later in the recording.

---

## 10. Types of Jitter

| Type | Source | Affects |
|---|---|---|
| Bluetooth transport jitter | Radio protocol batching/retransmission | `arrival_lsl` — one-sided, always positive |
| OS scheduling jitter | OS wakes threads irregularly | `local_clock()` call timing in bridge |
| USB jitter | USB polled bus (1ms/125μs frames) | Audio and video arrival timing |
| Buffer jitter | OS audio ring buffers | Audio callback timing |
| Crystal phase noise | Microscopic cycle-to-cycle variation | Negligible in practice |
| Network jitter | Variable network latency | LSL multi-PC sync RTT measurements |

In SensorChrono's single-PC setup the relevant ones are Bluetooth, OS, USB, and buffer jitter.

---

## 11. Absolute Acquisition Chain Lag

### The problem

After LSL sync and drift correction, all timestamps reflect "when PC-B's software received the data." The physical event happened earlier:

```
Key physically pressed           ← true event time
      ↓
Sound wave travels through air   ← ~3ms per meter
      ↓
Mic diaphragm vibrates
      ↓
ADC converts to digital
      ↓
USB driver buffer
      ↓
OS audio callback fires
      ↓
bridge calls local_clock()       ← THIS is the LSL timestamp
```

Typical lag values:
- **Audio**: ~40–60 ms
- **Video**: ~30–60 ms (exposure + USB + frame buffer + decode)
- **Keyboard HID**: ~1 ms (hardware interrupt, taken as ground truth)

### How SensorChrono measures it

Each spacebar press creates a simultaneous event in three streams:

```
delta_audio_i = t_audio_click_i - t_keyboard_i
delta_video_i = t_video_frame_i - t_keyboard_i
```

Across 10–20 presses in the calibration block, the median gives:
```
audio_lag = median(delta_audio_i)   →  e.g. 48 ms
video_lag = median(delta_video_i)   →  e.g. 37 ms
```

### How SensorChrono corrects it

```
corrected_audio_ts = raw_audio_ts - audio_lag
corrected_video_ts = raw_video_ts - video_lag
```

After subtraction, audio clicks and video frames align with keyboard HID timestamps. Stage 5 residual should be ~0 ms.

---

## 12. What SensorChrono Adds Over Traditional LSL + LabRecorder

| Capability | LSL + LabRecorder | SensorChrono |
|---|---|---|
| Multi-PC clock alignment | Yes | Yes (via LSL) |
| Writing to XDF | Yes | Yes (via LabRecorder) |
| OS scheduling jitter | Partial (dejitter) | Yes (dejitter in Stage 1) |
| Shimmer crystal drift | No | Yes (post-hoc linear model) |
| Bluetooth jitter | No | Yes (bypassed via dev_ts) |
| Absolute audio/video lag | No | Yes (in-situ fiducial measurement) |
| Stream management | No | Yes (starts/monitors/stops bridges) |
| Liveness check | No | Yes (blocks until all streams live) |
| Calibration enforcement | No | Yes (wizard enforces spacebar block) |
| Quality audit | No | Yes (PASS/WARN/FAIL verdict) |
| Corrected output | No | Yes (CSVs + frame map + report) |

---

## 13. The 5-Stage Post-Processing Pipeline

1. **Dejitter** — smooth OS scheduling jitter on all stream timestamps
2. **Apply clock model** — correct Shimmer timestamps using `a + b × dev_ts`
3. **Subtract per-modality lag** — remove measured audio and video acquisition chain lag
4. **Build unified table + frame map** — align all streams to a common timeline
5. **Re-detect fiducials and certify residuals** — verify post-correction alignment is ~0 ms; emit PASS/WARN/FAIL

---

## 14. Novelty

### What exists already

- LSL + LabRecorder — multi-PC clock alignment + XDF recording (widely used)
- BrainFlow — biosensor SDK with online clock correction, no video/audio
- MNE-LSL — EEG-focused LSL wrapper, no video/audio
- iMotions — commercial multimodal platform, closed source, expensive
- Ad-hoc lab scripts — scattered, undocumented, not reusable

### What is novel about SensorChrono

1. **Diagnostics sidecar + post-hoc clock model** — deliberately recording `arrival_lsl - dev_ts` as a sidecar stream for global Theil-Sen fit post-hoc
2. **In-situ lag calibration using keyboard as free triple-modal fiducial** — no external calibration hardware required
3. **Complete integrated pipeline** — drift + lag + dejitter + audit in one reproducible automated tool applied to a standard XDF
4. **Protocol enforcement as correctness mechanism** — wizard makes a valid synchronized recording the only possible output

### What is NOT novel

- Underlying math (OWD minimum filtering, Theil-Sen, EMA) — standard techniques
- LSL and LabRecorder — existing tools
- The fact that clocks drift — well known

### Honest framing

The novelty is in the **combination and operationalization** — known techniques packaged into a complete, reproducible, hardware-agnostic workflow producing certified synchronized datasets from commodity hardware with no external calibration equipment.

---

## 15. Benchmarking and Validation

### Internal validation (built in)

- Post-correction residuals should be ~0 ms median, < 5 ms std (PASS threshold)
- Leave-one-out cross-validation on calibration presses

### External benchmarks

| Solution | Crystal drift | BT jitter | Acq. chain lag | Hardware |
|---|---|---|---|---|
| Raw LSL + LabRecorder | No | No | No | No |
| LSL + LabRecorder + dejitter | No | Partial | No | No |
| Old `LslTimestampMapper` bridge | Partial (online) | Partial (online) | No | No |
| BrainFlow | Partial | Partial | No | OpenBCI |
| MNE-LSL | Partial | Partial | No | No |
| LSL `time_correction()` | No | No | No | No |
| **SensorChrono** | **Yes** | **Yes** | **Yes** | **No** |
| Hardware trigger box | Yes | Yes | Yes | Yes |

### Physical ground truth experiment

**LED flash + photodiode:**
- Connect photodiode to Shimmer analog input
- Flash LED visible to camera
- Both streams capture same physical event
- Measure alignment error before vs after correction

**Recommended experiment design:**
1. Record 3+ sessions with LED flash setup
2. Measure alignment error in each condition (raw LSL, online bridge, SensorChrono)
3. Show error grows linearly over time in uncorrected condition (drift)
4. Show error stays flat in corrected condition
5. Report residual as the bound on remaining missynchronization

### Worst-case missynchronization bounds after correction

| Source | Bound |
|---|---|
| Crystal drift residual | < 20 ms (WARN threshold) |
| Audio lag estimation error | ± bootstrap CI from calibration presses |
| Video lag quantization | ± 16.5 ms at 30 fps (half frame) |
| Keyboard HID ground truth | ~1 ms |
| Shimmer ECG absolute lag | Unknown lower bound only |

---

## 16. No Competing Solution Does Physio + Video + Audio

None of the alternative tools handle the full combination:

- **BrainFlow** — biosensors only, no video/audio
- **MNE-LSL** — EEG only, no video/audio
- **LabRecorder + dejitter** — records any streams but no lag calibration between modalities
- **Hardware trigger boxes** — synchronize start time only, don't measure ongoing acquisition chain lag
- **iMotions** — commercial, closed source, expensive, not LSL-based

SensorChrono is the only open-source tool providing drift-corrected, lag-calibrated synchronization across physiological, video, and audio streams from commodity hardware with no external calibration equipment.

---

*SensorChrono is developed at Kennesaw State University.*
