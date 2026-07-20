#!/usr/bin/env python3
"""Run the mlx-audio-separator CLI with a fix for 24-bit / 32-bit sources.

The library derives the output encoding from the input file's PCM subtype and
passes 'pcm24'/'pcm32' to mlx_audio_io.save, which only accepts
'auto'/'float32'/'pcm16' and raises "Unsupported encoding …". Coerce any
unsupported value to 'auto' so the writer picks a valid subtype per container.
This patches the single choke point (mlx_audio_io.save) so it covers the sync
and threaded AsyncStemWriter paths without editing the installed package.

Importable as `main()` so the frozen bundle can reuse it (see main_bundle.py);
still runnable as a script for the run-from-source path (app.py shells out to
`python runner.py …` when not frozen).
"""
import os
import sys

_SUPPORTED = {"auto", "float32", "pcm16"}


def _fix_path():
    # When the app is launched from Finder/Dock it inherits launchd's minimal
    # PATH (/usr/bin:/bin:/usr/sbin:/sbin), which omits Homebrew. The library
    # shells out to ffmpeg by bare name, so prepend the common brew/user bin
    # dirs to make ffmpeg discoverable regardless of how we're launched.
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for p in ("/usr/local/bin", "/opt/homebrew/bin"):
        if p not in parts:
            parts.insert(0, p)
    os.environ["PATH"] = os.pathsep.join(parts)


def _install_save_shim():
    import mlx_audio_io as _mac

    orig_save = _mac.save

    def _save(*args, **kwargs):
        enc = kwargs.get("encoding")
        if enc is not None and enc not in _SUPPORTED:
            kwargs["encoding"] = "auto"
        return orig_save(*args, **kwargs)

    _mac.save = _save


def main():
    _fix_path()
    _install_save_shim()
    from mlx_audio_separator.utils.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
