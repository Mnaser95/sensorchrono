# -*- mode: python ; coding: utf-8 -*-
# PyInstaller ONE-FOLDER spec for SensorChrono.
#
# One-FILE mode breaks PySide6's Qt plugins, so we build ONE-FOLDER and wrap it
# in an Inno Setup installer (build/installer.iss) so it still feels like a
# single double-click. See build/PACKAGING.md.
#
# Build (from the repo root, in the project venv):
#   set LIBLSL_PATH=...\liblsl.dll          (the native LSL library)
#   set LABRECORDER_DIR=...\LabRecorder      (folder containing LabRecorder.exe)
#   pyinstaller --noconfirm build/sensorchrono.spec
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(os.environ.get("SENSORCHRONO_ROOT", os.path.abspath(os.path.join(SPECPATH, "..")))).resolve()

# --- data files --------------------------------------------------------------
datas = []
# committed device profiles (profiles.py resolves them relative to the package)
datas += [(str(p), "profiles") for p in (ROOT / "profiles").glob("*.yaml")]
# the capture bridges now live in sensorchrono/bridges/ and are pulled in as
# package submodules by collect_submodules("sensorchrono") below — the frozen
# exe runs them via ``--run-bridge``, so there are no loose files to bundle.
# the analysis package source (post-process subprocess imports analysis.*)
if (ROOT / "analysis").exists():
    for py in (ROOT / "analysis").glob("*.py"):
        datas.append((str(py), "analysis"))

# --- native binaries ---------------------------------------------------------
binaries = []
liblsl = os.environ.get("LIBLSL_PATH")
if liblsl and Path(liblsl).exists():
    binaries.append((liblsl, "."))
else:
    print("WARNING [spec]: LIBLSL_PATH not set/found — liblsl will NOT be bundled.")

# --- bundle a LabRecorder build (optional; located at runtime via _MEIPASS) ---
labrecorder_dir = os.environ.get("LABRECORDER_DIR")
if labrecorder_dir and Path(labrecorder_dir).exists():
    src = Path(labrecorder_dir)
    for f in src.rglob("*"):
        if f.is_file():
            rel = f.relative_to(src).parent
            datas.append((str(f), str(Path("LabRecorder") / rel)))
else:
    print("WARNING [spec]: LABRECORDER_DIR not set/found — LabRecorder will NOT be bundled.")

# --- hidden imports (the app imports many submodules lazily) -----------------
# imageio-ffmpeg ships its own ffmpeg executable; collect_all keeps audio muxing
# available in the frozen desktop build without a system FFmpeg installation.
ffmpeg_datas, ffmpeg_binaries, ffmpeg_hidden = collect_all("imageio_ffmpeg")
datas += ffmpeg_datas
binaries += ffmpeg_binaries

hiddenimports = collect_submodules("sensorchrono") + ffmpeg_hidden
if (ROOT / "analysis").exists():
    hiddenimports += collect_submodules("analysis")

a = Analysis(
    [str(ROOT / "build" / "sensorchrono_main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[str(ROOT / "build" / "rthook_pylsl.py")],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SensorChrono",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # GUI app: no console window
    icon=os.environ.get("SENSORCHRONO_ICON") or None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SensorChrono",
)
