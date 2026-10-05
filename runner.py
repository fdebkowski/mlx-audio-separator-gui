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
    import model_catalog as catalog
    from mlx_audio_separator.core import Separator
    orig_list = Separator.list_supported_model_files
    orig_download = Separator.download_model_files
    orig_load = Separator.load_model
    state = catalog.load_state()
    if '--gui-refresh-catalog' in sys.argv:
        sys.argv.remove('--gui-refresh-catalog')
        # The engine's UVR catalog may be unavailable on a clean offline
        # install; discovery still works with the bundled community manifest.
        try:
            baseline = orig_list(Separator(log_level=40))
        except Exception:
            baseline = {}
        state = catalog.refresh_sources(baseline)

    def _list(self):
        grouped = orig_list(self)
        return catalog.merge_catalog(grouped, state)

    def _download(self, model_filename):
        filename = model_filename
        if filename == catalog.SW_FOUR_STEM:
            return _download(self, 'BS-Roformer-SW.ckpt')
        for name, info in _list(self).get('MDXC', {}).items():
            if info['filename'] != filename or not info.get('urls'):
                continue
            for relative, url in zip((info['filename'], info['config']), info['urls']):
                if not catalog.safe_path(relative) or not catalog.safe_url(url):
                    raise ValueError('Invalid model download path or URL')
                destination = Path(self.model_file_dir) / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Do not turn an interrupted download into an "On disk" model.
                if not destination.is_file():
                    temporary = destination.with_suffix(destination.suffix + '.partial')
                    try:
                        temporary.unlink(missing_ok=True)
                        self.download_file_if_not_exists(url, str(temporary))
                        temporary.replace(destination)
                    finally:
                        temporary.unlink(missing_ok=True)
            return filename, 'MDXC', name, str(Path(self.model_file_dir) / filename), info['config']
        return orig_download(self, filename)

    def _load(self, model_filename='model_bs_roformer_ep_317_sdr_12.9755.ckpt'):
        filename = model_filename
        result = orig_load(self, filename)
        if '/' in filename:
            # Engine output filenames include model_name; source namespaces
            # belong in the cache path, not in an output filename.
            import hashlib
            model_name = Path(filename).stem + '_' + hashlib.sha256(filename.encode()).hexdigest()[:8]
            self.model_name = self.model_instance.model_name = model_name
        if filename == catalog.SW_FOUR_STEM:
            instance = self.model_instance
            demix = instance._demix_mlx

            def four_stems(*args, **kwargs):
                # A single "other" request must still run guitar and piano.
                single = instance.output_single_stem
                instance.output_single_stem = None
                try:
                    sources = demix(*args, **kwargs)
                finally:
                    instance.output_single_stem = single
                return fold_four_stems(sources)

            instance._demix_mlx = four_stems
        return result

    Separator.list_supported_model_files = _list
    Separator.download_model_files = _download
    Separator.load_model = _load


def fold_four_stems(sources):
    """Lossless in-memory fold before WAV/FLAC/MP3 output encoding."""
    names = {name.lower(): value for name, value in sources.items()}
    required = {'bass', 'drums', 'other', 'vocals', 'guitar', 'piano'}
    if not required.issubset(names):
        raise ValueError('SW checkpoint did not return all six expected stems')
    return {'bass': names['bass'], 'drums': names['drums'], 'vocals': names['vocals'],
            'other': names['other'] + names['guitar'] + names['piano']}


def main():
    _fix_path()
    _install_save_shim()
    _install_extra_models_shim()
    from mlx_audio_separator.utils.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
