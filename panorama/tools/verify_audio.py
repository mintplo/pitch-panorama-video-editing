"""Compare decoded audio at matching source and output times."""

import argparse
import json
import math
from pathlib import Path
import subprocess

import numpy as np
from scipy import signal


SAMPLE_RATE = 12000
SEARCH_SECONDS = 0.25


def read_audio(path, start, seconds):
    raw = subprocess.check_output([
        "ffmpeg", "-nostdin", "-v", "error", "-ss", str(start), "-i", str(path),
        "-t", str(seconds), "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "f32le", "pipe:1",
    ])
    data = np.frombuffer(raw, dtype="<f4").astype(np.float64)
    if data.size < round(seconds * SAMPLE_RATE) - 2:
        raise ValueError("Audio ends within the selected sample")
    data -= data.mean()
    if not np.isfinite(data).all() or np.sqrt(np.mean(data ** 2)) < 1e-5:
        raise ValueError("Silent sample; choose another interval with --at")
    return data


def compare(source, output):
    """Positive lag means the matching sound occurs later in the source sample."""
    count = min(len(source), len(output))
    radius = round(SEARCH_SECONDS * SAMPLE_RATE)
    # The central output excerpt has equal support at every tested source lag.
    excerpt = output[radius:count - radius]
    reference = source[:count]
    dots = signal.correlate(reference, excerpt, mode="valid", method="fft")
    energy = np.concatenate(([0.0], np.cumsum(reference ** 2)))
    norms = np.linalg.norm(excerpt) * np.sqrt(np.maximum(0, energy[len(excerpt):] - energy[:-len(excerpt)]))
    if not (norms > 0).any():
        raise ValueError("Silent comparison interval; choose another interval with --at")
    scores = np.divide(dots, norms, where=norms > 0, out=np.full_like(dots, -np.inf))
    best = int(np.argmax(scores))
    alternatives = np.abs(np.arange(len(scores)) - best) > round(0.01 * SAMPLE_RATE)
    if scores[best] > 0.9 and np.max(scores[alternatives]) >= scores[best] - 0.005:
        raise ValueError("Ambiguous repeating sound; choose a distinct interval with --at")
    return (best - radius) / SAMPLE_RATE, float(scores[best])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-start", required=True, type=float, help="Source seconds corresponding to output time zero")
    parser.add_argument("--at", type=float, nargs="+", help="Sample starts in output seconds")
    parser.add_argument("--window", type=float, default=10, help="Length of each sample in seconds")
    args = parser.parse_args()
    try:
        if not math.isfinite(args.source_start) or args.source_start < 0:
            raise ValueError("--source-start must be finite and nonnegative")
        if not math.isfinite(args.window) or args.window < 1:
            raise ValueError("--window must be at least one second")
        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(args.output)
        ])
        duration = float(json.loads(probe)["format"]["duration"])
        if not math.isfinite(duration) or duration < 1:
            raise ValueError("At least one second of audio is required")
        window = min(duration, args.window)
        latest = duration - window
        starts = args.at if args.at is not None else sorted({0, latest / 2, latest})
        passed = True
        for start in starts:
            if not math.isfinite(start) or not 0 <= start <= latest:
                raise ValueError("Sample must fit within the output duration")
            source = read_audio(args.source, args.source_start + start, window)
            output = read_audio(args.output, start, window)
            lag, correlation = compare(source, output)
            ok = abs(lag) <= 0.01 and correlation > 0.97
            print(json.dumps(dict(time_seconds=start, lag_seconds=lag, correlation=correlation, passed=ok)))
            passed = passed and ok
        return 0 if passed else 1
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
