"""Online catalog data, with a last-known-good cache and conservative discovery.

Imported by the GUI without MLX/PyYAML; discovery runs in its engine subprocess.
Only metadata/configuration is fetched during refresh. Weights stay on demand.
"""
import concurrent.futures
import copy
import hashlib
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

SCHEMA = 1
REFRESH_INTERVAL = 24 * 3600
RETRY_INTERVAL = 3600
SUPPORT = Path.home() / 'Library' / 'Application Support' / 'MLX Audio Separator'
REMOTE_BASE = 'https://raw.githubusercontent.com/fdebkowski/mlx-audio-separator-gui/main/'
SCORES_URL = 'https://raw.githubusercontent.com/nomadkaraoke/python-audio-separator/main/audio_separator/models-scores.json'
MODELS_URL = 'https://raw.githubusercontent.com/nomadkaraoke/python-audio-separator/main/audio_separator/models.json'
UVR_URL = 'https://raw.githubusercontent.com/TRvlvr/application_data/main/filelists/download_checks.json'
AUTHORS = ('becruily', 'pcunwa', 'GaboxR67', 'Aname-Tommy', 'anvuew', 'SYH99999', 'Sucial')
SW_FOUR_STEM = 'BS-Roformer-SW-4stem'
MAX_BYTES = 4 * 1024 * 1024


def ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'MLX-Audio-Separator/catalog', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=12, context=ssl_context()) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Catalog response exceeds size limit')
    return data.decode('utf-8')


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def bundled_json(name):
    for base in (Path(__file__).resolve().parent, Path(getattr(sys, '_MEIPASS', '.'))):
        path = base / name
        if path.is_file():
            return read_json(path)
    return {}


def load_state(support=SUPPORT):
    state = read_json(Path(support) / 'catalog_sources.json')
    return state if isinstance(state, dict) and state.get('schema') == SCHEMA else {}


def due(state, now=None):
    now = time.time() if now is None else now
    interval = RETRY_INTERVAL if state.get('warnings') else REFRESH_INTERVAL
    checked = state.get('checked_at', 0)
    return not isinstance(checked, (int, float)) or now - checked >= interval


def safe_path(path):
    return (isinstance(path, str) and bool(path) and '\\' not in path
            and not PurePosixPath(path).is_absolute()
            and all(p not in ('', '.', '..') for p in path.split('/')))


def safe_url(url):
    if not isinstance(url, str):
        return False
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.hostname in ('huggingface.co', 'raw.githubusercontent.com', 'github.com')
            and not parsed.query and not parsed.fragment
            and '..' not in urllib.parse.unquote(parsed.path).split('/'))


def valid_scores(scores):
    if not isinstance(scores, dict):
        return {}
    return {stem: {'SDR': metrics['SDR']} for stem, metrics in scores.items()
            if isinstance(stem, str) and isinstance(metrics, dict)
            and isinstance(metrics.get('SDR'), (int, float)) and not isinstance(metrics['SDR'], bool)
            and math.isfinite(metrics['SDR']) and -100 < metrics['SDR'] < 100}


def validate_extras(entries):
    if not isinstance(entries, dict) or len(entries) > 1000:
        raise ValueError('Invalid community catalog')
    for name, entry in entries.items():
        if (not isinstance(name, str) or not isinstance(entry, dict)
                or not safe_path(entry.get('filename')) or not safe_path(entry.get('config'))
                or not isinstance(entry.get('urls'), list) or len(entry['urls']) != 2
                or not all(safe_url(url) for url in entry['urls'])
                or not isinstance(entry.get('stems'), list)
                or not all(isinstance(s, str) for s in entry['stems'])):
            raise ValueError('Invalid model entry: ' + str(name))
    return entries


def validate_overrides(entries):
    if not isinstance(entries, dict):
        raise ValueError('Invalid score overrides')
    for filename, entry in entries.items():
        if (not safe_path(filename) or not isinstance(entry, dict)
                or not valid_scores(entry.get('scores')) or not entry.get('benchmark')
                or not isinstance(entry.get('source_url'), str)
                or not entry['source_url'].startswith('https://mvsep.com/quality_checker/entry/')):
            raise ValueError('Invalid score source: ' + str(filename))
    return entries


# Inference options consumed by the pinned MLX Roformer loader; the rest below
# affect training or acceleration only. Unknown architecture options need review.
MODEL_KEYS = set(('dim depth stereo num_stems time_transformer_depth freq_transformer_depth '
                 'linear_transformer_depth num_bands freqs_per_bands dim_head heads '
                 'attn_dropout ff_dropout mlp_expansion_factor mask_estimator_depth '
                 'sample_rate stft_n_fft stft_hop_length stft_win_length').split())
IGNORED_KEYS = set(('flash_attn dim_freqs_in use_torch_checkpoint '
                   'multi_stft_resolution_loss_weight multi_stft_resolutions_window_sizes '
                   'multi_stft_hop_size multi_stft_normalized').split())


def config_metadata(text):
    import yaml
    class ConfigLoader(yaml.SafeLoader):
        pass
    # Community configs use !!python/tuple for training loss windows. Accept
    # that sequence only, without allowing Python object construction.
    ConfigLoader.add_constructor('tag:yaml.org,2002:python/tuple', lambda loader, node: loader.construct_sequence(node))
    config = yaml.load(text, Loader=ConfigLoader)
    if not isinstance(config, dict):
        raise ValueError('Not a model configuration')
    model = config.get('model') or {}
    training = config.get('training') or {}
    if not isinstance(model, dict) or not isinstance(training, dict):
        raise ValueError('Invalid model/training sections')
    if not {'dim', 'depth'}.issubset(model) or not ({'num_bands', 'freqs_per_bands'} & model.keys()):
        raise ValueError('Architecture is not a supported BS/Mel-Band Roformer')
    unknown = set(model) - MODEL_KEYS - IGNORED_KEYS - {'stft_normalized', 'skip_connection'}
    if unknown or model.get('stft_normalized') or model.get('skip_connection'):
        raise ValueError('Needs engine support: ' + ', '.join(sorted(unknown or {'stft_normalized/skip_connection'})))
    stems = training.get('instruments')
    target = training.get('target_instrument')
    if not isinstance(stems, list) or not 2 <= len(stems) <= 8 or not all(isinstance(s, str) for s in stems):
        raise ValueError('Missing output stem metadata')
    count = model.get('num_stems', 1)
    if count == 1 and target not in stems:
        raise ValueError('Single-stem model has no valid target instrument')
    if count != 1 and count != len(stems):
        raise ValueError('Configuration and output stem count disagree')
    return {'stems': stems, 'target_stem': target, 'family': 'MelBand Roformer' if 'num_bands' in model else 'BS Roformer'}


def _pairs(repo, paths, mappings):
    weights = [p for p in paths if p.endswith(('.ckpt', '.pth'))]
    configs = [p for p in paths if p.endswith(('.yaml', '.yml'))]
    explicit = mappings.get(repo, {})
    pairs = []
    for weight in weights:
        if re.search(r'(?i)(?:^|/)(archive[^/]*|experimental)(?:/|$)', weight):
            continue
        if weight in explicit:
            config = explicit[weight]
        else:
            candidates = [p for p in configs if PurePosixPath(p).with_suffix('') == PurePosixPath(weight).with_suffix('')]
            if len(weights) == 1 and len(configs) == 1:
                candidates = configs
            config = candidates[0] if len(candidates) == 1 else None
        if config and config in paths and safe_path(weight) and safe_path(config):
            pairs.append((weight, config))
    return pairs


def repo_allowed(repo_id, settings):
    return (repo_id not in settings.get('excluded_repos', [])
            and not re.search(r'(?i)(?:^|[-_/])(test|exp|scnet|fno|hyperace|siamese|mag)(?:$|[-_/])', repo_id))


def discover_repo(repo, baseline, settings, previous):
    repo_id = repo['id']
    revision = repo.get('sha')
    if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{40}', revision):
        return [], []
    paths = [s['rfilename'] for s in repo.get('siblings', []) if isinstance(s.get('rfilename'), str)]
    if not any(p.endswith(('.ckpt', '.pth')) for p in paths):
        return [], []
    if not repo_allowed(repo_id, settings) or any(p.endswith('.py') for p in paths):
        return [], [{'repo': repo_id, 'updated': repo.get('lastModified'), 'reason': 'Custom/experimental architecture requires review'}]
    pairs = _pairs(repo_id, paths, settings.get('config_pairs', {}))
    if not pairs:
        return [], [{'repo': repo_id, 'updated': repo.get('lastModified'), 'reason': 'Checkpoint/config pairing requires review'}]
    entries, skipped = [], []
    for weight, config in pairs:
        source_key = repo_id + '/' + weight
        alias = settings.get('aliases', {}).get(source_key, PurePosixPath(weight).name)
        if alias in baseline:
            continue
        # Pin both files to the same repo revision. Namespace configs and generic
        # model.ckpt names, so an author cannot overwrite another model's files.
        identity = hashlib.sha256(source_key.encode()).hexdigest()[:12]
        folder = f'hf/{repo_id}/{identity}-{revision[:12]}'
        filename = folder + '/' + PurePosixPath(weight).name
        old = previous.get(source_key)
        if old and old.get('revision') == revision:
            entries.append(old)
            continue
        try:
            base = f'https://huggingface.co/{repo_id}/resolve/{revision}/'
            config_url = base + urllib.parse.quote(config)
            metadata = config_metadata(fetch(config_url))
            entry = dict(metadata, filename=filename, config=folder + '/config.yaml',
                         urls=[base + urllib.parse.quote(weight), config_url],
                         source_key=source_key, source_filename=PurePosixPath(weight).name,
                         source_url=f'https://huggingface.co/{repo_id}', revision=revision,
                         updated=repo.get('lastModified'), published=repo.get('createdAt'),
                         friendly=f"{metadata['family']} | {PurePosixPath(weight).stem} by {repo_id.split('/')[0]}")
            entries.append(entry)
        except Exception as exc:
            skipped.append({'repo': repo_id, 'checkpoint': weight, 'reason': str(exc), 'updated': repo.get('lastModified')})
    return entries, skipped


def discover_upstream(models, baseline):
    """Accept new registry Roformers after inspecting their published config."""
    known = {i['filename'] for group in baseline.values() for i in group.values()}
    entries, skipped = {}, []
    for name, files in models.get('roformer_download_list', {}).items():
        if not isinstance(files, dict) or len(files) != 1:
            continue
        filename, config = next(iter(files.items()))
        if filename in known or not safe_path(filename) or not safe_path(config):
            continue
        if re.search('(?i)hyperace|siamese|fno|scnet|polarformer|conformer', name):
            skipped.append({'repo': 'audio-separator', 'checkpoint': filename, 'reason': 'Custom architecture requires review'})
            continue
        try:
            config_url = 'https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/' + urllib.parse.quote(config)
            try:
                metadata = config_metadata(fetch(config_url))
            except urllib.error.HTTPError:
                config_url = 'https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/mdx_model_data/mdx_c_configs/' + urllib.parse.quote(config)
                metadata = config_metadata(fetch(config_url))
            entries[name] = dict(metadata, filename=filename, scores={}, download_files=[filename, config], source_url=MODELS_URL)
        except Exception as exc:
            skipped.append({'repo': 'audio-separator', 'checkpoint': filename, 'reason': str(exc)})
    return entries, skipped


def refresh_sources(baseline, support=SUPPORT):
    """Refresh independent data feeds; failed feeds retain their previous value."""
    state = load_state(support)
    warnings = []
    settings = bundled_json('catalog_settings.json')
    feeds = {
        'extras': (REMOTE_BASE + 'extra_models.json', validate_extras),
        'overrides': (REMOTE_BASE + 'score_overrides.json', validate_overrides),
        'scores': (SCORES_URL, lambda v: v if isinstance(v, dict) and any(valid_scores(i.get('median_scores')) for i in v.values() if isinstance(i, dict)) else None),
        'models': (MODELS_URL, lambda v: v if isinstance(v, dict) and isinstance(v.get('roformer_download_list'), dict) else None),
        'uvr': (UVR_URL, lambda v: v if isinstance(v, dict) and all(isinstance(v.get(k), dict) for k in ('demucs_download_list', 'vr_download_list', 'mdx_download_list', 'mdx23c_download_list')) else None),
    }
    def get_feed(item):
        key, (url, validate) = item
        value = validate(json.loads(fetch(url)))
        if value is None:
            raise ValueError('Invalid ' + key + ' feed')
        return key, value
    repos = []
    def get_author(author):
        query = urllib.parse.urlencode({'author': author, 'sort': 'lastModified', 'direction': -1, 'limit': 100, 'full': 'true'})
        value = json.loads(fetch('https://huggingface.co/api/models?' + query))
        if not isinstance(value, list):
            raise ValueError('Invalid Hugging Face author listing')
        return [r for r in value if isinstance(r, dict) and str(r.get('id', '')).startswith(author + '/')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(get_feed, item): item[0] for item in feeds.items()}
        jobs.update({pool.submit(get_author, author): 'hf:' + author for author in AUTHORS})
        for future in concurrent.futures.as_completed(jobs):
            key = jobs[future]
            try:
                result = future.result()
                if key.startswith('hf:'):
                    repos.extend(result)
                else:
                    name, value = result
                    state[name] = value
            except Exception as exc:
                warnings.append(f'{key}: {exc}')
    # Keep the engine's UVR download list current, which it otherwise caches
    # indefinitely. The full validated list remains available when offline.
    if state.get('uvr'):
        atomic_json(Path(support) / 'models' / 'download_checks.json', state['uvr'])
    existing = {info['filename'] for group in baseline.values() for info in group.values()}
    existing.update(e['filename'] for e in bundled_json('extra_models.json').values())
    existing.update(e['filename'] for e in state.get('extras', {}).values())
    upstream, blocked = discover_upstream(state.get('models', {}), baseline)
    state['upstream_entries'] = {**state.get('upstream_entries', {}), **upstream}
    existing.update(e['filename'] for e in state['upstream_entries'].values())
    previous = {e['source_key']: e for e in state.get('discovered', [])}
    # Retain discovered entries across temporary host failures/deleted repos.
    discovered = {key: entry for key, entry in previous.items()
                  if repo_allowed(entry['source_url'].split('huggingface.co/')[-1], settings)
                  and settings.get('aliases', {}).get(key, entry['source_filename']) not in existing
                  and not re.search(r'(?i)(?:^|/)(archive[^/]*|experimental)(?:/|$)', key)}
    skipped = list(blocked)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(discover_repo, r, existing, settings, previous): r['id'] for r in repos}
        for future in concurrent.futures.as_completed(jobs):
            try:
                entries, blocked = future.result()
                for entry in entries:
                    discovered[entry['source_key']] = entry
                skipped.extend(blocked)
            except Exception as exc:
                warnings.append(f'{jobs[future]}: {exc}')
    state.update(schema=SCHEMA, checked_at=time.time(), warnings=warnings,
                 discovered=sorted(discovered.values(), key=lambda e: e['source_key']),
                 skipped=sorted(skipped, key=lambda e: (e['repo'], e.get('checkpoint', ''))))
    atomic_json(Path(support) / 'catalog_sources.json', state)
    return state


def merged_extras(state):
    return {**bundled_json('extra_models.json'), **state.get('extras', {})}


def merge_catalog(grouped, state):
    """One catalog for GUI listing and actual engine downloads; no GUI-only rows."""
    grouped = copy.deepcopy(grouped)
    mdxc = grouped.setdefault('MDXC', {})
    known = {info['filename'] for group in grouped.values() for info in group.values()}
    for name, extra in merged_extras(state).items():
        if extra['filename'] not in known:
            mdxc[name] = {**extra, 'download_files': [extra['filename'], extra['config']], 'source_url': extra.get('source_url') or extra['urls'][0].split('/resolve/')[0]}
            known.add(extra['filename'])
    for extra in state.get('discovered', []):
        if extra['source_filename'] in known or extra['filename'] in known:
            continue
        mdxc[extra['friendly']] = {**extra, 'scores': {}, 'download_files': [extra['filename'], extra['config']], 'catalog_download': True}
        known.add(extra['filename'])
    for name, info in state.get('upstream_entries', {}).items():
        if info['filename'] not in known:
            mdxc[name] = info
            known.add(info['filename'])
    overrides = {**bundled_json('score_overrides.json'), **state.get('overrides', {})}
    upstream = state.get('scores', {})
    for group in grouped.values():
        for info in group.values():
            fn = info.get('source_filename', info['filename'])
            latest = upstream.get(fn, {})
            scores = valid_scores(latest.get('median_scores'))
            if scores and not info.get('benchmark'):
                info['scores'] = {**info.get('scores', {}), **scores}
                info['benchmark'] = 'MUSDB'
                info['score_source'] = SCORES_URL
            override = overrides.get(fn)
            if override and not valid_scores(info.get('scores')):
                info['scores'] = valid_scores(override['scores'])
                info['benchmark'] = override['benchmark']
                info['score_source'] = override['source_url']
                info['score_notes'] = override.get('notes', '')
                if override.get('target_stem'):
                    info['target_stem'] = override['target_stem']
                if not info.get('stems') and override.get('stems'):
                    info['stems'] = override['stems']
    sw = next((i for i in mdxc.values() if i['filename'] == 'BS-Roformer-SW.ckpt'), None)
    if sw:
        sw['stems'] = ['bass', 'drums', 'other', 'vocals', 'guitar', 'piano']
        sw['config'] = 'BS-Roformer-SW.yaml'
        release = 'https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/'
        sw['urls'] = [release + 'BS-Roformer-SW.ckpt', release + sw['config']]
        sw['source_url'] = 'https://huggingface.co/enerjazzer/BS-ROFO-SW-Fixed'
        mdxc['BS Roformer SW | 4-Stem (guitar + piano folded into other)'] = {
            **sw, 'filename': SW_FOUR_STEM, 'stems': ['bass', 'drums', 'other', 'vocals'],
            'scores': {}, 'benchmark': None, 'score_notes': 'Uses the SW six-stem checkpoint. Guitar, piano and other are added before encoding. The four-stem mode has not been independently benchmarked.',
            'source_url': 'https://mvsep.com/quality_checker/entry/8374', 'score_source': None,
        }
    return grouped
