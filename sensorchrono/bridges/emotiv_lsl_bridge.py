"""Publish EMOTIV Cortex EEG and motion data to LSL.

EMOTIV Launcher must be running. Samples are timestamped with the local LSL
clock when Cortex delivers them; this is receipt timing, not a recovered
headset device clock.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CORTEX_URI = "wss://localhost:6868"


class CortexError(RuntimeError):
    pass


@dataclass
class Subscription:
    cortex_name: str
    columns: list[str]
    indices: list[int]
    outlet: Any


def read_credentials(path: str | None) -> tuple[str | None, str | None]:
    client_id = os.environ.get("EMOTIV_CLIENT_ID")
    client_secret = os.environ.get("EMOTIV_CLIENT_SECRET")
    if not path:
        return client_id, client_secret
    values: dict[str, str] = {}
    plain: list[str] = []
    for raw in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sep = "=" if "=" in line else ":" if ":" in line else None
        if sep:
            key, value = line.split(sep, 1)
            key = key.strip().upper().replace(" ", "_").replace("-", "_")
            values[key] = value.strip().strip(chr(34) + chr(39))
        else:
            plain.append(line.strip(chr(34) + chr(39)))
    return (
        client_id or values.get("CLIENT_ID") or values.get("EMOTIV_CLIENT_ID")
        or (plain[0] if plain else None),
        client_secret or values.get("CLIENT_SECRET")
        or values.get("EMOTIV_CLIENT_SECRET")
        or (plain[1] if len(plain) > 1 else None),
    )


class CortexClient:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.ws = None
        self.request_id = 0

    async def connect(self) -> None:
        import websockets
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.ws = await websockets.connect(CORTEX_URI, ssl=context, max_size=None)

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None

    async def call(self, method: str, params: dict) -> Any:
        assert self.ws is not None
        self.request_id += 1
        request_id = self.request_id
        await self.ws.send(json.dumps({
            "jsonrpc": "2.0", "id": request_id,
            "method": method, "params": params,
        }))
        deadline = time.monotonic() + 20.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Cortex {method} timed out")
            message = json.loads(await asyncio.wait_for(
                self.ws.recv(), timeout=remaining))
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise CortexError(f"{method}: {message['error']}")
            return message.get("result")

    async def authorize(self) -> str:
        result = await self.call("authorize", {
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
        })
        token = result.get("cortexToken")
        if not token:
            raise CortexError("Cortex authorize returned no token")
        return token


async def discover_headset(client: CortexClient, headset_id: str | None) -> dict:
    await client.call("controlDevice", {"command": "refresh"})
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        found = await client.call(
            "queryHeadsets", {"id": headset_id} if headset_id else {})
        for headset in found:
            if not headset_id or headset.get("id") == headset_id:
                return headset
        await asyncio.sleep(1.0)
    raise CortexError("No EMOTIV headset discovered by Launcher")


async def ensure_connected(client: CortexClient, headset: dict) -> dict:
    headset_id = headset["id"]
    if headset.get("status") != "connected":
        await client.call("controlDevice", {
            "command": "connect", "headset": headset_id,
        })
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        found = await client.call("queryHeadsets", {"id": headset_id})
        for current in found:
            if current.get("id") == headset_id and current.get("status") == "connected":
                return current
        await asyncio.sleep(0.75)
    raise CortexError(f"EMOTIV headset {headset_id} did not connect")


def selected_indices(stream: str, columns: list[str]) -> list[int]:
    if stream != "eeg":
        return list(range(len(columns)))
    auxiliary = {
        "COUNTER", "INTERPOLATED", "RAW_CQ", "MARKERS", "MARKER_HARDWARE",
        "GYROX", "GYROY", "GYROZ", "Q0", "Q1", "Q2", "Q3",
    }
    return [i for i, name in enumerate(columns) if name.upper() not in auxiliary]


def sampling_rate(stream: str, headset: dict) -> float:
    if stream == "eeg":
        settings = headset.get("settings") or {}
        value = settings.get("eegRate") or settings.get("eegSamplingRate")
        return float(value) if isinstance(value, (int, float)) and value > 0 else 128.0
    return 0.0


async def create_subscriptions(
    client: CortexClient, token: str, session_id: str, headset: dict,
    requested: list[str], names: dict[str, str],
) -> dict[str, Subscription]:
    from pylsl import StreamInfo, StreamOutlet, cf_double64

    result = await client.call("subscribe", {
        "cortexToken": token, "session": session_id, "streams": requested,
    })
    failures = result.get("failure") or []
    if failures:
        print(f"[emotiv] Cortex subscriptions failed: {failures}", flush=True)
    bundles: dict[str, Subscription] = {}
    for item in result.get("success") or []:
        cortex_name = item["streamName"]
        columns = list(item.get("cols") or [])
        indices = selected_indices(cortex_name, columns)
        if not indices:
            raise CortexError(f"Cortex {cortex_name} returned no usable channels")
        stream_name = names[cortex_name]
        stream_type = "EEG" if cortex_name == "eeg" else "Motion"
        info = StreamInfo(
            stream_name, stream_type, len(indices),
            sampling_rate(cortex_name, headset), cf_double64,
            f"sensorchrono_emotiv_{headset['id']}_{cortex_name}",
        )
        desc = info.desc()
        desc.append_child_value("manufacturer", "EMOTIV")
        desc.append_child_value("headset_id", str(headset["id"]))
        desc.append_child_value("cortex_stream", cortex_name)
        desc.append_child_value(
            "timestamp_semantics",
            "receipt_lsl: local_clock when Cortex websocket message arrived",
        )
        channels = desc.append_child("channels")
        for index in indices:
            channel = channels.append_child("channel")
            channel.append_child_value("label", str(columns[index]))
            channel.append_child_value("type", stream_type)
            if cortex_name == "eeg":
                channel.append_child_value("unit", "microvolts")
        outlet = StreamOutlet(info)
        bundles[cortex_name] = Subscription(cortex_name, columns, indices, outlet)
        print(f"[emotiv] LSL outlet '{stream_name}' is live "
              f"({len(indices)} channels)", flush=True)
    if not bundles:
        raise CortexError("Cortex subscribed to no requested streams")
    return bundles


async def run(args: argparse.Namespace) -> int:
    from pylsl import local_clock

    client_id, client_secret = read_credentials(args.credentials_file)
    if not client_id or not client_secret:
        raise CortexError(
            "Missing EMOTIV credentials; select credentials.txt or set "
            "EMOTIV_CLIENT_ID and EMOTIV_CLIENT_SECRET"
        )
    requested = [s.strip() for s in args.streams.split(",") if s.strip()]
    unsupported = set(requested) - {"eeg", "mot"}
    if not requested or unsupported:
        raise CortexError(f"Unsupported EMOTIV streams: {sorted(unsupported)}")

    stop_file = Path(args.stop_file) if args.stop_file else None
    if stop_file:
        stop_file.unlink(missing_ok=True)
    client = CortexClient(client_id, client_secret)
    session_id = None
    token = None
    try:
        print("[emotiv] connecting to EMOTIV Launcher Cortex...", flush=True)
        await client.connect()
        access_deadline = time.monotonic() + 60.0
        while True:
            access = await client.call("requestAccess", {
                "clientId": client_id, "clientSecret": client_secret,
            })
            if access.get("accessGranted"):
                break
            if time.monotonic() >= access_deadline:
                raise CortexError(
                    "Cortex access was not approved in EMOTIV Launcher")
            print("[emotiv] approve the access request in EMOTIV Launcher...",
                  flush=True)
            await asyncio.sleep(2.0)
        headset = await ensure_connected(
            client, await discover_headset(client, args.headset_id))
        print(f"[emotiv] connected headset {headset['id']}", flush=True)
        token = await client.authorize()
        created = await client.call("createSession", {
            "cortexToken": token, "headset": headset["id"], "status": "active",
        })
        session_id = created["id"]
        bundles = await create_subscriptions(
            client, token, session_id, headset, requested,
            {"eeg": args.eeg_stream_name, "mot": args.motion_stream_name},
        )

        started = time.monotonic()
        while True:
            if stop_file and stop_file.exists():
                break
            if args.duration > 0 and time.monotonic() - started >= args.duration:
                break
            try:
                raw = await asyncio.wait_for(client.ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            message = json.loads(raw)
            arrival = local_clock()
            for cortex_name, bundle in bundles.items():
                values = message.get(cortex_name)
                if not isinstance(values, list):
                    continue
                try:
                    sample = [float(values[i]) for i in bundle.indices]
                except (IndexError, TypeError, ValueError):
                    continue
                bundle.outlet.push_sample(sample, timestamp=arrival)
        return 0
    finally:
        if client.ws is not None and token and session_id:
            try:
                await client.call("updateSession", {
                    "cortexToken": token, "session": session_id, "status": "close",
                })
            except Exception:
                pass
        await client.close()
        if stop_file:
            stop_file.unlink(missing_ok=True)
        print("[emotiv] stopped", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge EMOTIV Cortex data to LSL")
    parser.add_argument("--credentials-file")
    parser.add_argument("--headset-id")
    parser.add_argument("--streams", default="eeg,mot")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--stop-file")
    parser.add_argument("--eeg-stream-name", default="EmotivEEG")
    parser.add_argument("--motion-stream-name", default="EmotivMotion")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run(parse_args(argv)))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[emotiv] ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

