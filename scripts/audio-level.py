#!/usr/bin/env python3
"""Live level meter for what viewers hear: samples zoom_out.monitor (the
same PulseAudio/PipeWire source stream.sh's ffmpeg captures) with parec
and prints one JSON line per ~200ms: {"peak_db": .., "peak_l_db": ..,
"peak_r_db": .., "rms_db": .., "rms_l_db": .., "rms_r_db": .., "t": ..}.
Runs for --seconds then exits; the dashboard keeps one
of these alive only while a panel is open (app/audio_level.py), the
same on-demand pattern as the preview thumbnail. Reads audio only -
never writes, never touches ffmpeg or the sink itself.

Cost: parec + a little arithmetic on 16-bit samples - negligible next
to the encoder, and nice'd anyway."""
import argparse
import json
import math
import os
import struct
import subprocess
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("--seconds", type=int, default=20)
p.add_argument("--source", default="zoom_out.monitor")
args = p.parse_args()

RATE, CH, WINDOW_S = 48000, 2, 0.2
FRAME = 2 * CH                       # s16le * channels
CHUNK = int(RATE * WINDOW_S) * FRAME

os.nice(10)
proc = subprocess.Popen(
    ["parec", f"--device={args.source}", "--format=s16le", f"--channels={CH}", f"--rate={RATE}", "--raw", "--latency-msec=50"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
)
deadline = time.time() + args.seconds
try:
    while time.time() < deadline:
        buf = proc.stdout.read(CHUNK)
        if not buf or len(buf) < FRAME:
            break
        n = len(buf) // 2
        samples = struct.unpack(f"<{n}h", buf[: n * 2])
        left_samples = samples[0::2]
        right_samples = samples[1::2] if CH >= 2 else left_samples
        peak_l = max((abs(s) for s in left_samples), default=0) / 32768.0
        peak_r = max((abs(s) for s in right_samples), default=0) / 32768.0
        peak = max(peak_l, peak_r)
        rms = math.sqrt(sum(s * s for s in samples) / n) / 32768.0
        rms_l = math.sqrt(sum(s * s for s in left_samples) / len(left_samples)) / 32768.0 if left_samples else 0.0
        rms_r = math.sqrt(sum(s * s for s in right_samples) / len(right_samples)) / 32768.0 if right_samples else 0.0
        to_db = lambda x: round(20 * math.log10(x), 1) if x > 1e-6 else -120.0
        try:
            print(json.dumps({
                "peak_db": to_db(peak),
                "peak_l_db": to_db(peak_l),
                "peak_r_db": to_db(peak_r),
                "rms_db": to_db(rms),
                "rms_l_db": to_db(rms_l),
                "rms_r_db": to_db(rms_r),
                "t": round(time.time(), 2)
            }), flush=True)
        except BrokenPipeError:
            # The dashboard stopped listening (panel closed) - that IS our
            # stop signal, since it can't send us one (see app/audio_level.py).
            break
finally:
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    # Avoid Python's "Exception ignored ... BrokenPipeError" noise at exit
    # when the reader has gone away.
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
