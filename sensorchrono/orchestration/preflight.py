"""Preflight device scan — confirm each selected device actually responds
*before* staging, structurally killing the "wrong COM port / dead camera"
foot-guns.

Each check returns a :class:`CheckResult` (pass / warn / fail, required or
not). A required failure is a red-X blocker; a warning is a yellow-! the
operator can proceed past. All hardware libraries are imported lazily and a
missing library degrades the check to a warning (it can't be *proved* good,
but it shouldn't hard-block on the dev box). In dry-run the hardware checks are
skipped entirely.
"""
from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# status constants
PASS = "pass"
WARN = "warn"
FAIL = "fail"

DEFAULT_RCS_HOST = "localhost"
DEFAULT_RCS_PORT = 22345


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str = ""
    required: bool = True


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff no *required* check failed (warnings don't block)."""
        return not self.blockers()

    def blockers(self) -> list[CheckResult]:
        return [c for c in self.checks if c.required and c.status == FAIL]

    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == WARN]


# -- individual checks (each importable + independently testable) ----------
def check_labrecorder(
    host: str = DEFAULT_RCS_HOST,
    port: int = DEFAULT_RCS_PORT,
    *,
    timeout: float = 1.0,
    dry_run: bool = False,
) -> CheckResult:
    """Is LabRecorder's Remote Control Server reachable on its TCP port?
    A warning (not a blocker): the recorder layer falls back to CLI / manual.

    ``dry_run`` only changes the wording when RCS is *not* reachable: preflight
    probes the port before LabRecorder is launched, so "not reachable" is the
    expected, benign state — the old "will use CLI/manual fallback" text read as
    a fault in the field logs. A real session launches LabRecorder automatically."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return CheckResult("labrecorder_rcs", PASS, f"RCS reachable at {host}:{port}", required=False)
    except OSError as exc:
        if dry_run:
            detail = (
                "LabRecorder not running — expected in dry run; it is launched "
                "automatically when a real session starts (optional)"
            )
        else:
            detail = f"RCS not reachable ({exc}); will use CLI/manual fallback"
        return CheckResult("labrecorder_rcs", WARN, detail, required=False)


def enumerate_serial_ports() -> list | None:
    """All serial ports the OS sees, or ``None`` if pyserial is unavailable.

    Each item is a ``serial.tools.list_ports_common.ListPortInfo`` exposing
    ``device`` (e.g. ``"COM3"``), ``description``, ``hwid``, ``vid``, ``pid`` —
    enough to answer "is COM3 actually the Shimmer?" from a field log."""
    try:
        from serial.tools import list_ports
    except Exception:
        return None
    try:
        return list(list_ports.comports())
    except Exception:  # pragma: no cover - defensive (driver enumeration glitch)
        return []


def _format_available(ports: list) -> str:
    """One-line human summary of available ports, e.g. ``COM4 (Shimmer3-1234)``."""
    parts = []
    for p in ports:
        desc = (getattr(p, "description", "") or "").strip()
        parts.append(f"{p.device} ({desc})" if desc and desc.lower() != "n/a" else str(p.device))
    return ", ".join(parts)


def _log_serial_ports(ports: list | None) -> None:
    """Persist the full COM enumeration to the log so a field failure is
    answerable after the fact ("which ports existed, and what were they?")."""
    if ports is None:
        logger.info("serial ports: pyserial unavailable — cannot enumerate")
        return
    if not ports:
        logger.info("serial ports: none enumerated")
        return
    for p in ports:
        logger.info(
            "serial port: device=%s description=%s hwid=%s vid=%s pid=%s",
            p.device, getattr(p, "description", None), getattr(p, "hwid", None),
            getattr(p, "vid", None), getattr(p, "pid", None),
        )


def classify_serial_error(exc: Exception, port: str, available_ports: list) -> str:
    """Map a serial-open failure to operator-facing advice. **Contribution point.**

    You've seen the real COM3 failures on the bench; the message text + which
    cases to distinguish are yours to shape. The baseline below covers the three
    Windows failure modes we know about. The test contract (so refinements stay
    green) is intentionally loose: keep an available-ports list in the *absent*
    case and the words "in use" in the *access-denied* case.

    Args:
        exc: the exception raised by ``serial.Serial(port, ...)``.
        port: the bound COM port that failed to open (e.g. ``"COM3"``).
        available_ports: ``enumerate_serial_ports()`` output (possibly empty).

    Cases worth distinguishing on Windows:
        * ``PermissionError`` / access-denied  → port held by another app
          (a terminal, a prior SensorChrono, Shimmer Connect).
        * ``FileNotFoundError`` / port not in the enumeration → wrong COM port
          (the Shimmer is a *different* port, or not paired/powered).
        * semaphore-timeout / generic ``OSError`` → Bluetooth RFCOMM paired but
          the device isn't actually connected (off, out of range, dead battery).
    """
    available = _format_available(available_ports) or "none"
    devices = [getattr(p, "device", None) for p in available_ports]

    # Port isn't even in the OS enumeration → almost certainly the wrong port.
    if devices and port not in devices:
        return f"{port} not present — available ports: {available}"

    # Port exists but the open was refused: another app is holding it.
    if isinstance(exc, PermissionError):
        return (
            f"{port} exists but is in use by another application "
            f"(close other Shimmer/terminal software, then rescan) — {exc}"
        )

    # Port name resolved to nothing the driver could open.
    if isinstance(exc, FileNotFoundError):
        return (
            f"{port} not found — confirm the Shimmer is paired and powered on; "
            f"available ports: {available}"
        )

    # Anything else (e.g. Windows semaphore-timeout on a paired-but-disconnected
    # RFCOMM device): report verbatim so the raw OSError reaches the log.
    return f"{port} did not open: {exc}"


def check_serial_port(port: str | None) -> CheckResult:
    if not port:
        return CheckResult("shimmer_serial", FAIL, "no COM port bound")
    try:
        import serial
    except Exception:
        return CheckResult("shimmer_serial", WARN, "pyserial not installed; cannot probe port", required=True)
    available = enumerate_serial_ports()
    _log_serial_ports(available)
    try:
        sp = serial.Serial(port, timeout=0.5)
        sp.close()
        return CheckResult("shimmer_serial", PASS, f"{port} opened")
    except Exception as exc:
        return CheckResult("shimmer_serial", FAIL, classify_serial_error(exc, port, available or []))


def check_camera(index: int | None) -> CheckResult:
    if index is None:
        return CheckResult("camera", FAIL, "no camera index bound")
    try:
        import cv2
    except Exception:
        return CheckResult("camera", WARN, "opencv not installed; cannot probe camera", required=True)
    cap = None
    try:
        cap = cv2.VideoCapture(index)
        if cap.isOpened():
            return CheckResult("camera", PASS, f"camera {index} opened")
        return CheckResult("camera", FAIL, f"camera {index} did not open")
    finally:
        if cap is not None:
            cap.release()


def check_microphone(device: str | int | None) -> CheckResult:
    try:
        import sounddevice as sd
    except Exception:
        return CheckResult("microphone", WARN, "sounddevice not installed; cannot probe mic", required=True)
    try:
        sd.query_devices(device, "input")
        return CheckResult("microphone", PASS, f"input device {device!r} present")
    except Exception as exc:
        return CheckResult("microphone", FAIL, f"input device {device!r} not found: {exc}")


def _log_report(report: PreflightReport) -> None:
    """Persist every CheckResult so a field log records exactly what preflight
    found (which device passed, which blocked, and the forensic detail)."""
    for c in report.checks:
        level = logging.ERROR if c.status == FAIL and c.required else logging.INFO
        logger.log(level, "preflight %s: %s — %s", c.name, c.status, c.detail)


def check_all(session, *, rcs_host: str = DEFAULT_RCS_HOST, rcs_port: int = DEFAULT_RCS_PORT) -> PreflightReport:
    """Run every check appropriate to ``session``. In dry-run, hardware checks
    are skipped (synthetic adapters need no hardware) and only LabRecorder
    reachability is probed informationally."""
    dry_run = bool(getattr(session, "dry_run", False))
    if dry_run:
        report = PreflightReport(
            checks=[
                CheckResult("dry_run", PASS, "dry-run: hardware checks skipped (synthetic streams)", required=False),
                check_labrecorder(rcs_host, rcs_port, dry_run=True),
            ]
        )
    else:
        b = session.bindings
        report = PreflightReport(
            checks=[
                check_serial_port(b.shimmer_com_ports[0] if b.shimmer_com_ports else None),
                check_camera(b.camera_indices[0] if b.camera_indices else None),
                check_microphone(b.mic_devices[0] if b.mic_devices else None),
                check_labrecorder(rcs_host, rcs_port, dry_run=False),
            ]
        )
    _log_report(report)
    return report
