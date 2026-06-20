"""Central diagnostic logging — persist enough detail that a Windows field
failure can be debugged from a log folder after the fact.

The frozen GUI has no console, bridges only ``print()`` to stdout, and the
supervisor keeps just a 200-line ring buffer of which only the last few lines
ever reach the GUI error box. So when a session dies in the field (the canonical
case: ``shimmer_exg not ready … (COM3)``) there is nothing left to inspect.
This module gives the **main/GUI process** a rotating log file plus a one-shot
environment snapshot; the per-bridge subprocess output is persisted separately
by the supervisor (it tees each bridge's stdout to ``<out_dir>/logs/``), because
a :class:`~logging.handlers.RotatingFileHandler` is **not** safe to share across
processes — five bridges + the GUI rotating the same file would race.

Stdlib-only by design (it is imported at startup, before heavy deps, and runs
in the frozen exe). The log dir lives next to the existing config under
``~/.sensorchrono`` (see :func:`sensorchrono.config.user_config_path`).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
from pathlib import Path

#: the package-wide logger every module logs under (``logging.getLogger(__name__)``
#: in a ``sensorchrono.*`` module is a child of this and inherits its handlers).
LOGGER_NAME = "sensorchrono"

#: Env overrides — ``SENSORCHRONO_LOG_DIR`` relocates the folder (tests, power
#: users); ``SENSORCHRONO_DEBUG`` forces DEBUG file verbosity without a flag.
_LOG_DIR_ENV = "SENSORCHRONO_LOG_DIR"
_DEBUG_ENV = "SENSORCHRONO_DEBUG"

#: marks handlers this module installed, so setup is idempotent + re-pointable.
_OWNED = "_sensorchrono_handler"

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB per file
_BACKUP_COUNT = 5  # ~10 MB ceiling across rotations


def log_dir() -> Path:
    """Folder the rotating log lives in. Honours ``$SENSORCHRONO_LOG_DIR``;
    otherwise ``~/.sensorchrono/logs`` (alongside the saved ``config.yaml``)."""
    override = os.environ.get(_LOG_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".sensorchrono" / "logs"


def _debug_enabled(debug: bool) -> bool:
    if debug:
        return True
    return os.environ.get(_DEBUG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def setup_logging(*, debug: bool = False) -> Path:
    """Configure the ``"sensorchrono"`` logger and return the log directory.

    * File handler at ``<log_dir>/sensorchrono.log`` (rotating, INFO always,
      DEBUG when ``debug`` or ``$SENSORCHRONO_DEBUG``).
    * stderr handler too, *except* in a frozen GUI build (no console attached).

    Idempotent and re-pointable: a second call (e.g. a test pointing
    ``$SENSORCHRONO_LOG_DIR`` at a ``tmp_path``) swaps the handlers rather than
    stacking duplicates. Returns the directory so the GUI can offer
    "Open log folder"."""
    level = logging.DEBUG if _debug_enabled(debug) else logging.INFO
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)  # let handlers gate level; capture everything here
    logger.propagate = False  # don't double-log via the root logger

    # Drop any handlers we previously installed (so the dir/level can change).
    for h in [h for h in logger.handlers if getattr(h, _OWNED, False)]:
        logger.removeHandler(h)
        h.close()

    fmt = logging.Formatter(_LOG_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        directory / "sensorchrono.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    setattr(file_handler, _OWNED, True)
    logger.addHandler(file_handler)

    if not _is_frozen():
        stream_handler = logging.StreamHandler(stream=sys.stderr)
        stream_handler.setLevel(level)
        stream_handler.setFormatter(fmt)
        setattr(stream_handler, _OWNED, True)
        logger.addHandler(stream_handler)

    return directory


def log_environment_snapshot(config=None) -> None:
    """Emit one INFO block describing the runtime + the session's device
    bindings — the first thing to read when triaging a field log. Best-effort:
    never raises (a snapshot must not be able to break startup)."""
    logger = logging.getLogger(LOGGER_NAME)
    try:
        from sensorchrono import __version__
    except Exception:  # pragma: no cover - import cycle / partial install
        __version__ = "unknown"

    lines = [
        "=== SensorChrono environment snapshot ===",
        f"version={__version__}",
        f"platform={platform.platform()}",
        f"python={platform.python_version()} ({sys.executable})",
        f"frozen={_is_frozen()}",
        f"debug_env={_debug_enabled(False)}",  # whether SENSORCHRONO_DEBUG is set
    ]
    if config is not None:
        try:
            b = getattr(config, "bindings", None)
            lines += [
                f"profile_id={getattr(config, 'profile_id', '?')}",
                f"dry_run={getattr(config, 'dry_run', '?')}",
                f"out_dir={getattr(config, 'out_dir', '?')}",
                f"bindings.shimmer_com_ports={getattr(b, 'shimmer_com_ports', [])}",
                f"bindings.camera_indices={getattr(b, 'camera_indices', [])}",
                f"bindings.mic_devices={getattr(b, 'mic_devices', [])!r}",
            ]
        except Exception:  # pragma: no cover - defensive
            lines.append("bindings=<unreadable>")
    logger.info("\n  ".join(lines))
