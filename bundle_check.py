"""Release diagnostics executed by the frozen binary, without external Python."""
import importlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path


def main():
    import app
    import model_catalog
    import runner

    if not getattr(sys, 'frozen', False):
        raise RuntimeError('Bundle diagnostics must run inside the frozen application')
    contents = Path(sys.executable).resolve().parent.parent
    result = {'version': app.APP_VERSION, 'python': platform.python_version(),
              'architecture': platform.machine(), 'dependencies': {}}
    if result['architecture'] != 'arm64':
        raise RuntimeError('Expected native arm64 execution')
    for module, distribution in (
            ('mlx.core', 'mlx'), ('mlx_audio_separator', 'mlx-audio-separator'),
            ('mlx_audio_io', 'mlx-audio-io'), ('mlx_spectro', 'mlx-spectro'),
            ('numpy', 'numpy'), ('yaml', 'PyYAML'), ('certifi', 'certifi'),
            ('torch', 'torch'), ('onnx', 'onnx'), ('demucs', 'demucs'),
            ('bs_roformer', 'bs-roformer-infer'), ('ml_collections', 'ml-collections'),
            ('lameenc', 'lameenc')):
        loaded = importlib.import_module(module)
        location = Path(loaded.__file__).resolve()
        if not location.is_relative_to(contents):
            raise RuntimeError(f'External dependency: {module}: {location}')
        result['dependencies'][distribution] = {
            'version': importlib.metadata.version(distribution), 'path': str(location)}
    if result['dependencies']['mlx-audio-separator']['version'] != '0.1.20':
        raise RuntimeError('Unexpected separation engine version')
    if result['dependencies']['bs-roformer-infer']['version'] != '0.1.6':
        raise RuntimeError('Unexpected compatibility engine version')

    # Importing mlx_audio_io also exercises its native ABI/RECORD verification.
    import mlx.core as mx
    result['metal_available'] = mx.metal.is_available()
    if not result['metal_available']:
        raise RuntimeError('Metal unavailable: use a physical Apple Silicon release runner')
    with mx.stream(mx.gpu):
        value = mx.sum(mx.array([1.0, 2.0, 3.0]))
        mx.eval(value)
        if value.item() != 6.0:
            raise RuntimeError('MLX GPU arithmetic failed')

    runner._fix_path()
    result['ffmpeg_directory'] = str(runner._bundled_ffmpeg_dir())
    if runner._bundled_ffmpeg_dir() is None:
        raise RuntimeError('Bundled FFmpeg missing')
    for name in ('extra_models.json', 'compat_models.json', 'score_overrides.json', 'catalog_settings.json'):
        data = model_catalog.bundled_json(name)
        if not data:
            raise RuntimeError(f'Missing bundled catalog data: {name}')
    # Exercise the same Tcl/Tk and drag-and-drop package used by the GUI.
    root = app.tk.Tk()
    try:
        root.withdraw()
        root.tk.call('lappend', 'auto_path', str(app.tkdnd_dir()))
        result['tkdnd'] = root.tk.call('package', 'require', 'tkdnd')
        result['tk'] = root.tk.call('info', 'patchlevel')
        root.update()
    finally:
        root.destroy()
    print(json.dumps(result, indent=2))
    return 0
