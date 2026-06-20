"""Session configuration: what the operator picks on the SETUP page, plus the
device bindings an admin configures once. Includes the *validation* that makes
the app idiot-proof (the whole reason this product exists) and a YAML
round-trip so a saved ``config.yaml`` reproduces a session exactly.

Validation philosophy (these are deliberate choices — see the brief):
  * Identity labels (participant/session/task) are **required and filename-safe**
    because they flow into the LabRecorder filename template and output paths.
  * ``duration_s`` has hard min/max bounds; the max mirrors the profile's
    ``safety.max_continuous_minutes`` (240 min) so a fat-fingered "36000" can't
    start a 10-hour run.
  * The chosen ``profile_id`` must actually exist on disk.
  * For a **real** (non-dry-run) capture, the required device bindings must be
    set — you cannot start a hardware session with no COM port / camera. In
    dry-run, bindings are optional (synthetic adapters need none).
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from sensorchrono.profiles import list_profiles

DEFAULT_PROFILE_ID = "shimmer3_exg_sr47-5-1"

#: Env override for where the app remembers the last session + device bindings.
_CONFIG_PATH_ENV = "SENSORCHRONO_CONFIG"


def user_config_path() -> Path:
    """Where the app persists the last session config (incl. device bindings) so
    an admin binds the hardware once and every later launch reloads it.

    Honours ``$SENSORCHRONO_CONFIG`` (used by tests + power users); otherwise
    ``~/.sensorchrono/config.yaml``."""
    override = os.environ.get(_CONFIG_PATH_ENV)
    if override:
        return Path(override)
    return Path.home() / ".sensorchrono" / "config.yaml"

MIN_DURATION_S = 5
MAX_DURATION_S = 240 * 60  # 4 h — matches profiles' safety.max_continuous_minutes

# Labels become path/filename components, so keep them conservative.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConfigError(ValueError):
    """Raised by :meth:`SessionConfig.validate` with all problems collected."""


def default_dry_run() -> bool:
    """Dry-run (synthetic streams) is the default everywhere except Windows,
    where the real hardware lives."""
    return not sys.platform.startswith("win")


def _creatable(target: Path) -> tuple[bool, str]:
    """Can ``target`` be created as a directory, *without* creating anything?

    Walks up to the nearest existing ancestor and checks it's a writable
    directory. Keeps :meth:`SessionConfig.validate` a pure predicate — a
    wizard can call it on every keystroke without littering the disk."""
    for ancestor in (target, *target.parents):
        if ancestor.exists():
            if not ancestor.is_dir():
                return False, f"{ancestor} exists and is not a directory"
            if not os.access(ancestor, os.W_OK):
                return False, f"{ancestor} is not writable"
            return True, ""
    return False, f"{target} has no existing ancestor"


@dataclass(slots=True)
class DeviceBindings:
    """Machine-specific hardware bindings — configured once by the admin, saved in
    ``config.yaml``. All device lists support N devices of each type.

    Backward compat: v1 configs used single-device fields
    (``shimmer_com_port``, ``camera_index``, ``mic_device``).  These are
    automatically migrated to lists in :meth:`SessionConfig.__post_init__`.
    """

    shimmer_com_ports: list = field(default_factory=list)   # list[str]  — COM ports
    camera_indices: list = field(default_factory=list)      # list[int]  — cv2 device indices
    mic_devices: list = field(default_factory=list)         # list[str | int] — sounddevice ids
    display_grid: list = field(default_factory=list)        # list[list[str]] — 2-D panel layout


@dataclass(slots=True)
class SessionConfig:
    """A single recording session's configuration."""

    participant: str
    session: str
    task: str
    duration_s: int
    out_dir: Path
    profile_id: str = DEFAULT_PROFILE_ID
    bindings: DeviceBindings = field(default_factory=DeviceBindings)
    dry_run: bool = field(default_factory=default_dry_run)
    root_label: str = "sensorchrono"  # LabRecorder filename {root:...}

    def __post_init__(self) -> None:
        # Accept str paths from YAML / callers; normalise to Path.
        if not isinstance(self.out_dir, Path):
            self.out_dir = Path(self.out_dir)
        if isinstance(self.bindings, dict):
            d = dict(self.bindings)
            # Migrate v1 single-device keys → v2 list-based keys
            if "shimmer_com_port" in d or "shimmer_ecg_port" in d:
                port = d.pop("shimmer_com_port", None) or d.pop("shimmer_ecg_port", None)
                d.pop("shimmer_com_port", None)
                d.pop("shimmer_ecg_port", None)
                if "shimmer_com_ports" not in d:
                    d["shimmer_com_ports"] = [port] if port else []
            if "camera_index" in d:
                cam = d.pop("camera_index")
                if "camera_indices" not in d:
                    d["camera_indices"] = [cam] if cam is not None else []
            if "mic_device" in d:
                mic = d.pop("mic_device")
                if "mic_devices" not in d:
                    d["mic_devices"] = [mic] if mic is not None else []
            self.bindings = DeviceBindings(**d)

    # -- validation ---------------------------------------------------------
    def validate(self) -> None:
        """Raise :class:`ConfigError` listing *all* problems, or return None."""
        errs: list[str] = []

        for fname in ("participant", "session", "task"):
            value = str(getattr(self, fname)).strip()
            if not value:
                errs.append(f"{fname} must not be empty")
            elif not _LABEL_RE.match(value):
                errs.append(
                    f"{fname}={value!r} is not filename-safe "
                    "(use letters, digits, '.', '_' or '-'; no spaces/slashes)"
                )

        if not isinstance(self.duration_s, int) or isinstance(self.duration_s, bool):
            errs.append(f"duration_s must be an int (got {self.duration_s!r})")
        elif not (MIN_DURATION_S <= self.duration_s <= MAX_DURATION_S):
            errs.append(
                f"duration_s={self.duration_s} out of range "
                f"[{MIN_DURATION_S}, {MAX_DURATION_S}] seconds"
            )

        if self.profile_id not in list_profiles():
            errs.append(
                f"profile_id={self.profile_id!r} not found "
                f"(available: {', '.join(list_profiles()) or 'none'})"
            )

        creatable, why = _creatable(self.out_dir)
        if not creatable:
            errs.append(f"out_dir={self.out_dir} is not creatable: {why}")

        if not self.dry_run:
            if not self.bindings.shimmer_com_ports:
                errs.append("real capture requires at least one Shimmer COM port")
            if not self.bindings.camera_indices:
                errs.append("real capture requires at least one camera")

        if errs:
            raise ConfigError("Invalid session config:\n  - " + "\n  - ".join(errs))

    # -- serialization ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["out_dir"] = str(self.out_dir)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionConfig":
        """Build a config from a parsed dict (e.g. a loaded ``config.yaml``).

        Hardens the operator-editable round-trip: unknown keys and malformed
        bodies raise a *catchable* :class:`ConfigError` rather than a raw
        ``TypeError``, and a missing ``dry_run`` is rejected — re-deriving it
        from the platform default would silently flip a real-capture config to
        dry-run (or vice-versa) when moved between machines, breaking the
        promise that a saved config reproduces a session exactly."""
        d = dict(d)
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ConfigError(f"config has unknown keys: {sorted(unknown)}")
        if "dry_run" not in d:
            raise ConfigError(
                "config is missing 'dry_run' — cannot safely infer real-vs-simulated capture"
            )
        try:
            # __post_init__ coerces a dict `bindings` and a str `out_dir`.
            return cls(**d)
        except TypeError as exc:
            raise ConfigError(f"config is malformed: {exc}") from exc

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False))
        return path

    @classmethod
    def load(cls, path: str | Path) -> "SessionConfig":
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.from_dict(data)
