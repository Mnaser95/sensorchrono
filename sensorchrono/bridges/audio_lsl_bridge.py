"""Audio capture LSL bridge.

Captures PCM audio from ANY input device the operator binds (by name substring
or index via --device); with no --device it falls back to the system default
input. Publishes to LSL as an `Audio` stream at the configured sample rate. Not
tied to any particular microphone brand.

Timestamping: each block of N samples gets a single LSL timestamp set to
the LSL clock immediately after the sounddevice callback fired. Per-sample
times are reconstructed at sample_rate during post-processing.

Also writes a WAV file alongside the LSL stream so analysis tools that
prefer file-based audio can use it directly.
"""
import argparse
import queue
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pylsl
import sounddevice as sd


def find_input_device(name_hint=None):
    devs = sd.query_devices()
    if name_hint:
        for i, d in enumerate(devs):
            if name_hint.lower() in d["name"].lower() and d["max_input_channels"] > 0:
                return i, d
    # default
    default_input = sd.default.device[0]
    return default_input, devs[default_input]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default=None,
                        help="Input device name substring or index; omit for the system default input")
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--block-size", type=int, default=480,
                        help="Frames per block (480 @ 48kHz = 10 ms)")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--tag", default="exp03")
    parser.add_argument("--out-dir", default=r"C:\Users\ngoldbla\Desktop\LSL_data")
    parser.add_argument("--stream-name", default="Audio",
                        help="LSL stream name (default Audio; set to Audio_1 etc. "
                             "for the second microphone in a multi-mic rig).")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"{args.tag}_audio.wav"

    dev_idx, dev_info = find_input_device(args.device)
    print(f"[audio] using device #{dev_idx}: {dev_info['name']}")
    print(f"[audio] {args.sample_rate} Hz, {args.channels} ch, block={args.block_size} frames "
          f"({1000*args.block_size/args.sample_rate:.1f} ms)")

    info = pylsl.StreamInfo(
        name=args.stream_name,
        type="Audio",
        channel_count=args.channels,
        nominal_srate=float(args.sample_rate),
        channel_format=pylsl.cf_float32,
        source_id=f"sensorchrono_mic_{args.stream_name}",
    )
    chns = info.desc().append_child("channels")
    for c in range(args.channels):
        ch = chns.append_child("channel")
        ch.append_child_value("label", f"audio_ch{c}")
        ch.append_child_value("unit", "normalized")
        ch.append_child_value("type", "Audio")
    info.desc().append_child_value("manufacturer", str(dev_info["name"]))
    outlet = pylsl.StreamOutlet(info, chunk_size=args.block_size, max_buffered=600)
    print(f"[audio] LSL outlet '{args.stream_name}' is live.")

    # Open WAV writer
    wav = wave.open(str(wav_path), "wb")
    wav.setnchannels(args.channels)
    wav.setsampwidth(2)  # int16
    wav.setframerate(args.sample_rate)
    print(f"[audio] writing WAV to {wav_path}")

    q = queue.Queue()
    stop = threading.Event()

    def callback(indata, frames, t, status):
        # Take LSL timestamp at callback entry (not playback time)
        lsl_t = pylsl.local_clock()
        if status:
            sys.stderr.write(f"[audio] sd status: {status}\n")
        q.put((indata.copy(), lsl_t))

    stream = sd.InputStream(
        device=dev_idx,
        samplerate=args.sample_rate,
        channels=args.channels,
        blocksize=args.block_size,
        dtype="float32",
        callback=callback,
    )

    t_start = pylsl.local_clock()
    t_end = t_start + args.duration if args.duration > 0 else None
    n_blocks = 0
    total_samples = 0
    last_print = t_start

    try:
        with stream:
            print("[audio] capturing...")
            while True:
                try:
                    block, lsl_t = q.get(timeout=0.5)
                except queue.Empty:
                    if t_end is not None and pylsl.local_clock() >= t_end:
                        break
                    continue
                # Convert to int16 for WAV
                int16 = (np.clip(block, -1.0, 1.0) * 32767).astype(np.int16)
                wav.writeframes(int16.tobytes())
                # Push to LSL as a chunk; LSL handles per-sample timing
                # We pass timestamp of the LAST sample in the block; LSL fills earlier
                # samples by subtracting 1/rate per index.
                last_sample_t = lsl_t  # callback returned for end of block
                outlet.push_chunk(block.tolist(), timestamp=last_sample_t)
                n_blocks += 1
                total_samples += len(block)
                now = pylsl.local_clock()
                if now - last_print >= 5.0:
                    elapsed = now - t_start
                    eff = total_samples / elapsed if elapsed > 0 else 0
                    print(f"[audio] t={elapsed:6.1f}s  blocks={n_blocks:6d}  "
                          f"samples={total_samples:9d}  eff_rate={eff:.1f} Hz")
                    last_print = now
                if t_end is not None and now >= t_end:
                    break
    except KeyboardInterrupt:
        print("\n[audio] Ctrl+C received.")
    finally:
        wav.close()
        elapsed = pylsl.local_clock() - t_start
        eff = total_samples / elapsed if elapsed > 0 else 0
        print(f"[audio] stopped. {total_samples} samples in {elapsed:.1f}s = {eff:.1f} Hz.")


if __name__ == "__main__":
    main()
