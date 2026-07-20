#!/usr/bin/env python3
"""Run the mlx-audio-separator CLI with a fix for 24-bit / 32-bit sources.

The library derives the output encoding from the input file's PCM subtype and
passes 'pcm24'/'pcm32' to mlx_audio_io.save, which only accepts
'auto'/'float32'/'pcm16' and raises "Unsupported encoding …". Coerce any
unsupported value to 'auto' so the writer picks a valid subtype per container.
This patches the single choke point (mlx_audio_io.save) so it covers the sync
and threaded AsyncStemWriter paths without editing the installed package.
"""
import os
import sys

# When the app is launched from Finder/Dock it inherits launchd's minimal PATH
# (/usr/bin:/bin:/usr/sbin:/sbin), which omits Homebrew. The library shells out
# to ffmpeg by bare name, so prepend the common brew/user bin dirs here to make
# ffmpeg discoverable regardless of how we're launched.
_path_parts = os.environ.get("PATH", "").split(os.pathsep)
for _p in ("/usr/local/bin", "/opt/homebrew/bin"):
    if _p not in _path_parts:
        _path_parts.insert(0, _p)
os.environ["PATH"] = os.pathsep.join(_path_parts)

import mlx_audio_io as _mac

_SUPPORTED = {"auto", "float32", "pcm16"}
_orig_save = _mac.save


def _save(*args, **kwargs):
    enc = kwargs.get("encoding")
    if enc is not None and enc not in _SUPPORTED:
        kwargs["encoding"] = "auto"
    return _orig_save(*args, **kwargs)


_mac.save = _save

from mlx_audio_separator.utils.cli import main

sys.exit(main())
