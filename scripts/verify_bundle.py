#!/usr/bin/env python3
"""Validate the distributable ZIP on macOS arm64, with no user model cache.

Runs the extracted frozen binary with an isolated HOME and system-only PATH.
All release checks, including real MLX inference, are mandatory. A missing
Metal device fails validation; it never becomes a successful skipped test.
"""
import argparse
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import random
import re
import subprocess
import tempfile
import wave

MODELS = {
    'mel-roformer-zfturbo-vocals-v1-mlx': {'vocals', 'instrumental'},
    'model_bs_roformer_ep_17_sdr_9.6568.ckpt': {'vocals', 'drums', 'bass', 'other'},
    'bs_roformer_4stems_ft.pth': {'vocals', 'drums', 'bass', 'other'},
    'BS-Roformer-SW-4stem': {'vocals', 'drums', 'bass', 'other'},
}
MACHO = {b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
         b'\xfe\xed\xfa\xce', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
         b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}


def run(args, *, env=None, timeout=120):
    result = subprocess.run([str(a) for a in args], env=env, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}\n'
                           f'{result.stdout[-8000:]}\n{result.stderr[-8000:]}')
    return result.stdout


def json_output(output):
    positions = [p for p in (output.find('{'), output.find('[')) if p >= 0]
    if not positions:
        raise ValueError(f'No JSON returned: {output[:500]}')
    return json.loads(output[min(positions):])


def version_tuple(value):
    return tuple(int(part) for part in value.split('.')) + (0,) * (3 - len(value.split('.')))


def sha256(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def inspect_binaries(app):
    paths = []
    for path in app.rglob('*'):
        if path.is_symlink():
            if not path.resolve().is_relative_to(app):
                raise RuntimeError(f'External bundle symlink: {path}')
            continue
        if not path.is_file():
            continue
        with path.open('rb') as handle:
            if handle.read(4) not in MACHO:
                continue
        if 'arm64' not in run(['/usr/bin/lipo', '-archs', path]).split():
            raise RuntimeError(f'Binary lacks arm64: {path}')
        dependencies = run(['/usr/bin/otool', '-L', path])
        for line in dependencies.splitlines():
            dependency = line.strip().split(' (', 1)[0]
            if dependency.startswith('/') and not dependency.endswith(':'):
                if not dependency.startswith(('/System/Library/', '/usr/lib/')):
                    raise RuntimeError(f'External library dependency: {path}: {dependency}')
        commands = run(['/usr/bin/otool', '-l', '-arch', 'arm64', path])
        for block in re.split(r'Load command \d+', commands):
            if 'cmd LC_BUILD_VERSION' in block or 'cmd LC_VERSION_MIN_MACOSX' in block:
                match = re.search(r'(?:minos|\bversion)\s+(\d+(?:\.\d+){1,2})', block)
                if match and version_tuple(match[1]) > (15, 0, 0):
                    raise RuntimeError(f'Binary needs macOS {match[1]} (declared 15.0): {path}')
        paths.append(str(path.relative_to(app)))
    if not paths:
        raise RuntimeError('No native binaries found')
    return paths


def make_fixture(destination):
    """Eight seconds of deterministic harmonics, bass and percussion; no music rights."""
    rng = random.Random(140)
    frames = array('h')
    for index in range(8 * 44100):
        t = index / 44100
        beat = t % 0.5
        bass = 0.2 * math.sin(2 * math.pi * 110 * t)
        chord = sum(0.035 * math.sin(2 * math.pi * f * t) for f in (261.63, 329.63, 392))
        voice = 0.08 * math.sin(2 * math.pi * 220 * t) * (0.5 + 0.5 * math.sin(5 * t))
        drum = 0.18 * math.exp(-beat * 35) * rng.uniform(-1, 1)
        for channel in (1.0, 0.94):
            frames.append(int(32767 * channel * (bass + chord + voice + drum)))
    with wave.open(str(destination), 'wb') as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(frames.tobytes())


def inspect_audio(path, ffmpeg, ffprobe, env):
    data = json_output(run([ffprobe, '-v', 'error', '-show_streams', '-show_format',
                            '-of', 'json', path], env=env))
    stream, = [s for s in data['streams'] if s['codec_type'] == 'audio']
    if stream['codec_name'] != 'flac' or int(stream['sample_rate']) != 44100 or stream['channels'] != 2:
        raise RuntimeError(f'Invalid stem format: {path}: {stream}')
    result = subprocess.run([str(ffmpeg), '-v', 'error', '-i', str(path),
                             '-f', 'f32le', '-acodec', 'pcm_f32le', '-'], env=env,
                            capture_output=True, check=True, timeout=120)
    samples = array('f', result.stdout)
    if len(samples) != 8 * 44100 * 2 or not all(math.isfinite(x) for x in samples):
        raise RuntimeError(f'Invalid length or non-finite audio: {path}')
    if max(abs(x) for x in samples) < 1e-7:
        raise RuntimeError(f'Silent stem: {path}')
    return {'file': path.name, 'frames': len(samples) // 2,
            'decoded_sha256': hashlib.sha256(result.stdout).hexdigest()}


def validate(archive, version):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('Validation requires native Apple Silicon macOS 15+ with Metal')
    report = {'version': version, 'zip_sha256': sha256(archive),
              'platform': platform.platform(), 'separations': {}}
    with tempfile.TemporaryDirectory(prefix='mlx-release-') as temporary:
        work = Path(temporary)
        run(['/usr/bin/ditto', '-x', '-k', archive, work / 'extracted'])
        app = (work / 'extracted' / 'MLX Audio Separator.app').resolve()
        plist = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        for key in ('CFBundleShortVersionString', 'CFBundleVersion'):
            if plist[key] != version:
                raise RuntimeError(f'{key}: {plist[key]} != {version}')
        if plist['LSMinimumSystemVersion'] != '15.0':
            raise RuntimeError('Expected macOS 15.0 minimum')
        executable = app / 'Contents/MacOS' / plist['CFBundleExecutable']
        run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', app])
        signature = subprocess.run(['/usr/bin/codesign', '-d', '--verbose=4', str(app)],
                                   capture_output=True, text=True, check=True).stderr
        if 'Signature=adhoc' not in signature:
            raise RuntimeError('Expected ad-hoc code signature')
        report['signature'] = signature
        report['native_binaries'] = inspect_binaries(app)
        isolated = work / 'user'
        isolated.mkdir()
        env = dict(os.environ, HOME=str(isolated), PATH='/usr/bin:/bin:/usr/sbin:/sbin',
                   PYTHONNOUSERSITE='1')
        for key in ('PYTHONPATH', 'PYTHONHOME', 'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH',
                    'DYLD_INSERT_LIBRARIES'):
            env.pop(key, None)
        diagnostics = json_output(run([executable, '--bundle-check'], env=env))
        if diagnostics['version'] != version:
            raise RuntimeError('Frozen APP_VERSION differs from release version')
        report['bundle'] = diagnostics
        ffdir = app / 'Contents/Resources/ffmpeg'
        ffmpeg, ffprobe = ffdir / 'ffmpeg', ffdir / 'ffprobe'
        for binary in (ffmpeg, ffprobe):
            report[binary.name] = run([binary, '-version'], env=env).splitlines()[0]
        for name in ('LICENSE', 'README.txt'):
            if not (ffdir / name).stat().st_size:
                raise RuntimeError(f'Missing FFmpeg notice: {name}')
        models = work / 'models'
        base = [executable, '--separator-runner']
        catalog = json_output(run(base + ['--gui-refresh-catalog', '--list_models', '--list_format',
                                         'json', '--log_level', 'error', '--model_file_dir', models],
                                  env=env, timeout=240))
        entries = {info['filename']: info for group in catalog.values() for info in group.values()}
        for model, stems in MODELS.items():
            if model not in entries or {s.lower() for s in entries[model]['stems']} != stems:
                raise RuntimeError(f'Missing or incorrect release model: {model}')
        report['catalog_count'] = len(entries)
        support = isolated / 'Library/Application Support/MLX Audio Separator'
        report['catalog_state'] = json.loads((support / 'catalog_sources.json').read_text())
        make_fixture(work / 'fixture.wav')
        # 24-bit input exercises the bundled save shim as well as decode/encode.
        source = work / 'fixture-24bit.wav'
        run([ffmpeg, '-y', '-v', 'error', '-i', work / 'fixture.wav', '-acodec', 'pcm_s24le', source], env=env)
        for model, expected in MODELS.items():
            print(f'Separating with {model}', flush=True)
            output = work / model / 'output'
            arguments = [source, '--model_filename', model, '--model_file_dir', models,
                         '--output_format', 'FLAC', '--output_dir', output, '--log_level', 'info']
            log = run(base + arguments, env=env, timeout=1200)
            stems = {}
            for path in output.glob('*.flac'):
                matches = [stem for stem in expected if re.search(r'\(' + stem + r'\)', path.name, re.I)]
                if len(matches) != 1 or matches[0] in stems:
                    raise RuntimeError(f'Unexpected output stem: {path.name}')
                stems[matches[0]] = inspect_audio(path, ffmpeg, ffprobe, env)
            if stems.keys() != expected:
                raise RuntimeError(f'Incorrect stems for {model}: {stems.keys()} != {expected}')
            report['separations'][model] = {'stems': stems, 'log': log[-4000:]}
            if model == 'BS-Roformer-SW-4stem':
                single = work / model / 'other-only'
                run(base + arguments + ['--single_stem', 'other', '--output_dir', single], env=env, timeout=1200)
                file, = single.glob('*.flac')
                other = inspect_audio(file, ffmpeg, ffprobe, env)
                if other['decoded_sha256'] != stems['other']['decoded_sha256']:
                    raise RuntimeError('SW Other-only differs from the four-stem Other output')
                report['sw_other_only_matches'] = True
        report['model_files'] = {str(p.relative_to(models)): sha256(p)
                                 for p in models.rglob('*') if p.is_file()}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zip', type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.zip.resolve(), args.version)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Validated {args.zip.name}: {report["zip_sha256"]}')


if __name__ == '__main__':
    main()
