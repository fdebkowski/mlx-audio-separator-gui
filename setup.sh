#!/bin/bash
# One-time setup: create the Python venv, install the separation engine,
# and build "MLX Audio Separator.app" into /Applications.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HOME/.venvs/mlx-audio-separator"

# Apple Silicon check (Rosetta terminals report x86_64, so ask sysctl).
if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]]; then
  echo "ERROR: This app needs an Apple Silicon Mac (M1 or newer) — MLX does not run on Intel." >&2
  exit 1
fi

# Pick a Python that ships Tcl/Tk; the GUI runs on the venv's Python.
# The python.org installer bundles its own Tk and is the most reliable choice.
PY=""
for cand in \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
  "$(command -v python3 || true)"; do
  [[ -n "$cand" && -x "$cand" ]] || continue
  if "$cand" -c 'import tkinter' >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done
if [[ -z "$PY" ]]; then
  echo "ERROR: No Python with Tkinter found." >&2
  echo "Install Python 3.13 from https://www.python.org/downloads/ (it bundles Tcl/Tk)," >&2
  echo "then re-run this script." >&2
  exit 1
fi
echo "==> Using $PY"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "==> Creating venv at $VENV"
  "$PY" -m venv "$VENV"
fi

echo "==> Installing the separation engine (mlx-audio-separator)"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install --upgrade 'mlx-audio-separator[convert]'

if ! command -v ffmpeg >/dev/null 2>&1 \
   && [[ ! -x /opt/homebrew/bin/ffmpeg && ! -x /usr/local/bin/ffmpeg ]]; then
  echo
  echo "NOTE: ffmpeg was not found. MP3/M4A input and MP3 output need it:"
  echo "        brew install ffmpeg"
  echo
fi

echo "==> Building the app"
"$HERE/build.sh"

echo
echo "All set — launch 'MLX Audio Separator' from /Applications."
