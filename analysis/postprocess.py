"""End-to-end post-processing pipeline.

Five stages:

  Stage 1 - Dejitter regular-rate streams (pyxdf dejitter_timestamps=True)
  Stage 2 - Apply Shimmer clock model to ShimmerECG timestamps
  Stage 3 - Subtract per-modality absolute lag (from in-situ calibration or profile)
  Stage 4 - Build a unified Parquet table + a frames table for the MP4
  Stage 5 - Re-detect fiducials and certify residuals

Inputs:
  XDF file (mandatory)
  MP4 file (optional, but most useful runs have one)

Outputs (under --out-dir):
  unified.parquet      - sample-level table of all streams on a common LSL timeline
  frames.csv           - mp4 frame_idx -> corrected LSL timestamp
  pipeline_report.json - everything the pipeline did, what it found, verdict
  pipeline_report.md   - human-readable summary

CLI
---
    python -m analysis.postprocess recording.xdf
    python -m analysis.postprocess recording.xdf --mp4 recording.mp4
    python -m analysis.postprocess recording.xdf --out-dir OUT/ --skip-parquet
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Sequence

import numpy as np
import pyxdf

from analysis.shimmer_clock_model import fit_from_xdf, apply as apply_clock_model
from analysis.insitu_lag_calibration import calibrate_xdf
from analysis.recording_audit import audit as audit_recording


# ---------- data ----------

@dataclass
class StageResult:
    name: str
    status: str           # ok | warn | skipped | error
    detail: str = ""
    artifacts: list = field(default_factory=list)


@dataclass
class PipelineReport:
    xdf: str
    mp4: str | None
    out_dir: str
    stages: list           # list[StageResult]
    audit_verdict: str
    audit_summary_issues: list
    overall_status: str    # ok | warn | error

    def to_dict(self) -> dict:
        return {
            "xdf": self.xdf,
            "mp4": self.mp4,
            "out_dir": self.out_dir,
            "stages": [asdict(s) for s in self.stages],
            "audit_verdict": self.audit_verdict,
            "audit_summary_issues": self.audit_summary_issues,
            "overall_status": self.overall_status,
        }


# ---------- pipeline ----------

def run(xdf_path: Path, *,
        mp4_path: Path | None = None,
        out_dir: Path,
        profile_lag_ms: dict | None = None,
        skip_parquet: bool = False) -> PipelineReport:
    """Run all five stages. Returns a PipelineReport."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stages: list[StageResult] = []

    # --- Stage 0: audit (informational; gates "fail-loud" rather than blocking)
    audit_report = audit_recording(xdf_path)
    stages.append(StageResult(
        name="0_audit",
        status="ok" if audit_report.overall_verdict == "PASS" else
               ("warn" if audit_report.overall_verdict == "WARN" else "error"),
        detail=f"audit verdict {audit_report.overall_verdict}; "
               f"{len(audit_report.summary_issues)} issues flagged",
        artifacts=[],
    ))

    # --- Stage 1: dejitter
    # pyxdf's dejitter applies linear regression to per-stream timestamps,
    # collapsing burst noise on regular-rate streams.
    streams_dejittered, _ = pyxdf.load_xdf(
        str(xdf_path),
        dejitter_timestamps=True,
        jitter_break_threshold_seconds=1.0,
        synchronize_clocks=True,
    )
    by_name = {s["info"]["name"][0]: s for s in streams_dejittered}
    n_regular = sum(1 for s in streams_dejittered
                    if float(s["info"]["nominal_srate"][0]) > 0)
    stages.append(StageResult(
        name="1_dejitter",
        status="ok",
        detail=f"dejittered {n_regular} regular-rate streams via pyxdf",
        artifacts=[],
    ))

    # --- Stage 2: clock model for ShimmerECG
    cm = None
    ecg_corrected_ts = None
    try:
        cm = fit_from_xdf(xdf_path)
        if "ShimmerECG" in by_name:
            ecg = by_name["ShimmerECG"]
            dev_ts = np.asarray([v[0] for v in ecg["time_series"]])
            ecg_corrected_ts = apply_clock_model(cm, dev_ts)
        status = "ok" if cm.verdict in ("PASS", "WARN") else "warn"
        detail = (f"drift {cm.b_ppm:+.2f} ppm, residual {cm.residual_std_ms:.2f} ms, "
                  f"verdict {cm.verdict}")
        if cm.anomalies:
            detail += f"; anomalies: {list(cm.anomalies)}"
    except Exception as exc:
        status = "error"
        detail = f"clock model fit failed: {exc}"
    stages.append(StageResult(name="2_clock_model", status=status, detail=detail))

    # --- Stage 3: per-modality lag subtraction
    # Try in-situ first; fall back to provided profile_lag_ms; otherwise null.
    lag_ms = {"ShimmerECG": None, "Audio": None, "VideoFrames": None}
    lag_source = {}
    try:
        cal = calibrate_xdf(xdf_path)
        if cal.audio:
            lag_ms["Audio"] = cal.audio.median_ms
            lag_source["Audio"] = "in_situ"
        if cal.video:
            lag_ms["VideoFrames"] = cal.video.median_ms
            lag_source["VideoFrames"] = "in_situ"
        if cal.shimmer_ecg_min_bt_lag_ms is not None:
            # Note: this is a LOWER BOUND, not the full lag (missing Shimmer ADC chain).
            lag_ms["ShimmerECG"] = cal.shimmer_ecg_min_bt_lag_ms
            lag_source["ShimmerECG"] = "in_situ_min_bt_only"
        stages.append(StageResult(
            name="3_lag_calibration",
            status="ok",
            detail=(f"in-situ lag: audio={lag_ms['Audio']}, "
                    f"video={lag_ms['VideoFrames']}, "
                    f"ecg-bt-min={lag_ms['ShimmerECG']}"),
        ))
    except Exception as exc:
        if profile_lag_ms:
            for k in lag_ms:
                if k in profile_lag_ms:
                    lag_ms[k] = profile_lag_ms[k]
                    lag_source[k] = "profile"
            stages.append(StageResult(
                name="3_lag_calibration",
                status="warn",
                detail=f"in-situ failed ({exc}); fell back to profile lag",
            ))
        else:
            stages.append(StageResult(
                name="3_lag_calibration",
                status="warn",
                detail=f"no lag available: {exc}",
            ))

    # --- Stage 4: build unified table + frames.csv
    if skip_parquet:
        stages.append(StageResult(name="4_unified_table", status="skipped",
                                  detail="--skip-parquet"))
    else:
        artifacts = []
        try:
            written = _write_unified_outputs(
                out_dir, by_name, ecg_corrected_ts, lag_ms,
            )
            artifacts = written
            stages.append(StageResult(
                name="4_unified_table",
                status="ok",
                detail=f"wrote {len(written)} artifact(s)",
                artifacts=[str(a) for a in artifacts],
            ))
        except Exception as exc:
            stages.append(StageResult(
                name="4_unified_table",
                status="error",
                detail=f"could not build unified table: {exc}",
            ))

    # --- Stage 5: residual check (re-detect fiducials post-correction)
    try:
        residuals = _residual_check(by_name, ecg_corrected_ts, lag_ms)
        ok_residuals = (
            residuals.get("audio_post_lag_median_ms") is not None
            and abs(residuals["audio_post_lag_median_ms"]) < 5.0
            and residuals.get("video_post_lag_median_ms") is not None
            and abs(residuals["video_post_lag_median_ms"]) < 5.0
        )
        stages.append(StageResult(
            name="5_residual_check",
            status="ok" if ok_residuals else "warn",
            detail=json.dumps(residuals),
        ))
    except Exception as exc:
        stages.append(StageResult(
            name="5_residual_check",
            status="warn",
            detail=f"could not certify residuals: {exc}",
        ))

    # --- Stage 6: sync validation video
    try:
        from analysis.sync_validation import generate_validation_video as _gen_video
        vid_path = _gen_video(by_name, ecg_corrected_ts, lag_ms, out_dir)
        if vid_path:
            stages.append(StageResult(
                name="6_sync_validation",
                status="ok",
                detail=f"wrote {vid_path.name}",
                artifacts=[str(vid_path)],
            ))
        else:
            stages.append(StageResult(
                name="6_sync_validation",
                status="skipped",
                detail="no KeyboardFiducial stream, video file, or frames.csv",
            ))
    except Exception as exc:
        stages.append(StageResult(
            name="6_sync_validation",
            status="warn",
            detail=f"sync validation video failed: {exc}",
        ))

    # Overall
    statuses = [s.status for s in stages]
    if "error" in statuses:
        overall = "error"
    elif "warn" in statuses or audit_report.overall_verdict == "WARN":
        overall = "warn"
    else:
        overall = "ok"

    report = PipelineReport(
        xdf=str(xdf_path.resolve()),
        mp4=str(mp4_path.resolve()) if mp4_path else None,
        out_dir=str(out_dir.resolve()),
        stages=stages,
        audit_verdict=audit_report.overall_verdict,
        audit_summary_issues=audit_report.summary_issues,
        overall_status=overall,
    )

    # Write report artifacts
    (out_dir / "pipeline_report.json").write_text(
        json.dumps(report.to_dict(), indent=2), encoding="utf-8",
    )
    (out_dir / "pipeline_report.md").write_text(
        _render_markdown(report), encoding="utf-8",
    )

    return report


def _write_unified_outputs(out_dir: Path,
                           by_name: dict,
                           ecg_corrected_ts: np.ndarray | None,
                           lag_ms: dict) -> list[Path]:
    """Write per-stream CSVs with corrected timestamps and a frames.csv."""
    artifacts = []

    # ShimmerECG with corrected ts
    if "ShimmerECG" in by_name:
        ecg = by_name["ShimmerECG"]
        ts_corr = ecg_corrected_ts if ecg_corrected_ts is not None else ecg["time_stamps"]
        ts_corr = np.asarray(ts_corr)
        # Subtract per-modality lag if available
        lag = lag_ms.get("ShimmerECG")
        ts_final = ts_corr - (lag / 1000.0 if lag is not None else 0.0)
        X = np.asarray(ecg["time_series"])
        out_path = out_dir / "shimmer_ecg.csv"
        with open(out_path, "w") as f:
            f.write("lsl_corrected_s,lsl_lag_applied_s,dev_ts_s,lead1,lead2,lead2m1\n")
            for i in range(len(ts_final)):
                row = X[i]
                f.write(f"{ts_final[i]:.6f},{ts_corr[i]:.6f},"
                        f"{row[0]:.6f},{row[1]:.4f},{row[2]:.4f},{row[3]:.4f}\n")
        artifacts.append(out_path)

    # Video frame index -> corrected LSL ts
    if "VideoFrames" in by_name:
        vid = by_name["VideoFrames"]
        ts = np.asarray(vid["time_stamps"])
        # VideoFrames carries [frame_idx, ?] in time_series ch0 (see video_lsl_bridge)
        ts_series = np.asarray(vid["time_series"])
        lag = lag_ms.get("VideoFrames")
        ts_final = ts - (lag / 1000.0 if lag is not None else 0.0)
        out_path = out_dir / "frames.csv"
        with open(out_path, "w") as f:
            f.write("frame_idx,lsl_corrected_s,lsl_raw_s\n")
            for i in range(len(ts)):
                fidx = int(ts_series[i][0]) if ts_series.ndim == 2 else i
                f.write(f"{fidx},{ts_final[i]:.6f},{ts[i]:.6f}\n")
        artifacts.append(out_path)

    # Audio (just metadata; full samples stay in WAV)
    if "Audio" in by_name:
        audio = by_name["Audio"]
        ts = np.asarray(audio["time_stamps"])
        lag = lag_ms.get("Audio")
        ts0 = float(ts[0] - (lag / 1000.0 if lag is not None else 0.0))
        ts1 = float(ts[-1] - (lag / 1000.0 if lag is not None else 0.0))
        out_path = out_dir / "audio_meta.json"
        out_path.write_text(json.dumps({
            "n_samples": int(len(ts)),
            "duration_s": float(ts[-1] - ts[0]),
            "lsl_start_corrected_s": ts0,
            "lsl_end_corrected_s": ts1,
            "lag_applied_ms": lag,
        }, indent=2))
        artifacts.append(out_path)

    # KeyboardFiducial (just write through; system clock is the reference)
    if "KeyboardFiducial" in by_name:
        kb = by_name["KeyboardFiducial"]
        ts = np.asarray(kb["time_stamps"])
        ev = [v[0] for v in kb["time_series"]]
        out_path = out_dir / "keyboard_fiducial.csv"
        with open(out_path, "w") as f:
            f.write("lsl_s,event_json\n")
            for t, e in zip(ts, ev):
                e_safe = str(e).replace("\"", "\\\"")
                f.write(f"{t:.6f},\"{e_safe}\"\n")
        artifacts.append(out_path)

    return artifacts


def _residual_check(by_name: dict,
                    ecg_corrected_ts: np.ndarray | None,
                    lag_ms: dict) -> dict:
    """Verify that subtracting the calibrated lag actually zeros the median delta.

    Approach: re-measure raw deltas on the ORIGINAL timestamps (so the
    detector windows are unchanged), then subtract the calibrated lag
    from the per-event delta. Median residual should be ~0; std reveals
    per-event jitter.
    """
    from analysis.insitu_lag_calibration import detect_audio_lag, detect_video_lag
    if "KeyboardFiducial" not in by_name:
        return {"note": "no keyboard fiducial; cannot check residuals"}
    kb = by_name["KeyboardFiducial"]
    kb_ts = np.asarray(kb["time_stamps"])
    kb_ev = [v[0] for v in kb["time_series"]]
    press_ts = np.array([t for t, e in zip(kb_ts, kb_ev) if "press" in e])

    out: dict = {"n_keystrokes": int(len(press_ts))}

    if "Audio" in by_name and lag_ms.get("Audio") is not None:
        audio = by_name["Audio"]
        a_ts = np.asarray(audio["time_stamps"])
        a_v = np.asarray([v[0] for v in audio["time_series"]], dtype=np.float32)
        a_fs = float(audio["info"]["nominal_srate"][0])
        m = detect_audio_lag(press_ts, a_ts, a_v, a_fs)
        if m:
            out["audio_post_lag_median_ms"] = m.median_ms - lag_ms["Audio"]
            out["audio_post_lag_std_ms"] = m.std_ms

    if "VideoFrames" in by_name and lag_ms.get("VideoFrames") is not None:
        v_ts = np.asarray(by_name["VideoFrames"]["time_stamps"])
        m = detect_video_lag(press_ts, v_ts)
        if m:
            out["video_post_lag_median_ms"] = m.median_ms - lag_ms["VideoFrames"]
            out["video_post_lag_std_ms"] = m.std_ms

    return out


# ---------- markdown rendering ----------

def _render_markdown(r: PipelineReport) -> str:
    lines = ["# Post-processing pipeline report", ""]
    lines.append(f"- XDF: `{r.xdf}`")
    if r.mp4:
        lines.append(f"- MP4: `{r.mp4}`")
    lines.append(f"- Out dir: `{r.out_dir}`")
    lines.append(f"- Audit verdict: **{r.audit_verdict}**")
    lines.append(f"- Overall: **{r.overall_status.upper()}**")
    lines.append("")
    lines.append("## Stages")
    for s in r.stages:
        icon = {"ok": "[OK]", "warn": "[WARN]", "skipped": "[skip]", "error": "[ERR]"}.get(s.status, "-")
        lines.append(f"- {icon} **{s.name}** — {s.status}")
        if s.detail:
            lines.append(f"  - {s.detail}")
        for a in s.artifacts:
            lines.append(f"  - artifact: `{a}`")
    if r.audit_summary_issues:
        lines.append("")
        lines.append("## Audit issues")
        for i in r.audit_summary_issues:
            lines.append(f"- {i}")
    return "\n".join(lines) + "\n"


# ---------- CLI ----------

def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="End-to-end post-processing pipeline.")
    ap.add_argument("xdf", type=Path)
    ap.add_argument("--mp4", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--skip-parquet", action="store_true",
                    help="Skip the unified-table stage (faster).")
    args = ap.parse_args(argv)

    if not args.xdf.exists():
        print(f"ERROR: {args.xdf} not found", file=sys.stderr)
        return 2

    report = run(args.xdf, mp4_path=args.mp4, out_dir=args.out_dir,
                 skip_parquet=args.skip_parquet)
    # Print a brief summary
    print(_render_markdown(report))
    return {"ok": 0, "warn": 1, "error": 2}[report.overall_status]


if __name__ == "__main__":
    sys.exit(main())
