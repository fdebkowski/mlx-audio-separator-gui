#!/usr/bin/env python3
"""Run the mlx-audio-separator CLI with a fix for 24-bit / 32-bit sources.

The library derives the output encoding from the input file's PCM subtype and
passes 'pcm24'/'pcm32' to mlx_audio_io.save, which only accepts
'auto'/'float32'/'pcm16' and raises "Unsupported encoding …". Coerce any
unsupported value to 'auto' so the writer picks a valid subtype per container.
This patches the single choke point (mlx_audio_io.save) so it covers the sync
and threaded AsyncStemWriter paths without editing the installed package.

Also merges extra_models.json (curated community models newer than the
engine's bundled registry) into the engine's model list; see
_install_extra_models_shim.

Importable as `main()` so the frozen bundle can reuse it (see main_bundle.py);
still runnable as a script for the run-from-source path (app.py shells out to
`python runner.py …` when not frozen).
"""
import json
import os
import sys
from pathlib import Path

_SUPPORTED = {"auto", "float32", "pcm16"}


def _bundled_ffmpeg_dir():
    """Directory of the ffmpeg/ffprobe the .app ships, or None when unbundled.

    build_bundle.sh embeds static arm64 ffmpeg/ffprobe in
    Contents/Resources/ffmpeg. In the frozen app sys.executable is
    Contents/MacOS/<exe>, so the pair sits one level up under Resources.
    """
    if not getattr(sys, "frozen", False):
        return None
    d = Path(sys.executable).resolve().parent.parent / "Resources" / "ffmpeg"
    return d if (d / "ffmpeg").exists() else None


def _fix_path():
    # When the app is launched from Finder/Dock it inherits launchd's minimal
    # PATH (/usr/bin:/bin:/usr/sbin:/sbin), which omits Homebrew. The library
    # shells out to ffmpeg by bare name, so prepend the common brew/user bin
    # dirs to make ffmpeg discoverable regardless of how we're launched.
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for p in ("/usr/local/bin", "/opt/homebrew/bin"):
        if p not in parts:
            parts.insert(0, p)
    # The self-contained bundle carries its own ffmpeg; prefer it over any
    # system copy so the app works with no Homebrew install at all.
    bundled = _bundled_ffmpeg_dir()
    if bundled is not None:
        parts.insert(0, str(bundled))
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


def _load_extra_models():
    """Entries from extra_models.json, or {} when the file is absent/broken.

    From source it sits next to this file; in the frozen .app PyInstaller
    unpacks --add-data files into sys._MEIPASS.
    """
    for base in (Path(__file__).resolve().parent,
                 Path(getattr(sys, "_MEIPASS", "."))):
        path = base / "extra_models.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                return {}
    return {}


def _install_extra_models_shim():
    """Merge curated extra models into the engine's registry.

    Separator.list_supported_model_files is the single choke point: it feeds
    both --list_models and download_model_files, so patching it makes the
    extras listable and downloadable without editing the installed package.
    """
    extras = _load_extra_models()
    if not extras:
        return

    from mlx_audio_separator.core import Separator

    orig_list = Separator.list_supported_model_files

    def _list(self):
        grouped = orig_list(self)
        mdxc = grouped.setdefault("MDXC", {})
        known = {info.get("filename") for info in mdxc.values()}
        for name, extra in extras.items():
            if name in mdxc or extra["filename"] in known:
                continue  # the engine's registry caught up; prefer its entry
            mdxc[name] = {
                "filename": extra["filename"],
                "scores": extra.get("scores") or {},
                "stems": extra.get("stems") or [],
                "target_stem": extra.get("target_stem"),
                # URLs first so both files land on disk under their basenames;
                # the bare yaml name after them then resolves as the model's
                # config (already present, so no repo-prefix download attempt).
                "download_files": list(extra["urls"]) + [extra["config"]],
            }
        return grouped

    Separator.list_supported_model_files = _list


def main():
    _fix_path()
    _install_save_shim()
    _install_extra_models_shim()
    from mlx_audio_separator.utils.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
