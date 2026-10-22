"""Bridge reviewed PyTorch models into the MLX engine's existing I/O pipeline.

Network construction and overlap-add inference come from upstream engines.
These models deliberately run on CPU: their complex STFT/FFT paths have not
been validated on MPS. No downloaded model-repository Python is executed.
"""
from pathlib import Path


def read_config(path):
    import yaml
    from ml_collections import ConfigDict

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor('tag:yaml.org,2002:python/tuple',
                           lambda loader, node: loader.construct_sequence(node))
    return ConfigDict(yaml.load(Path(path).read_text(), Loader=Loader))


def build_network(architecture, config):
    if architecture == 'hyperace_v2':
        from bs_roformer.utils import get_model_from_config
        # Preserve the author's unnormalized STFT and original DC behavior.
        config.model.zero_dc = False
        return get_model_from_config('bs_roformer', config, model_variation='hyperace')
    if architecture == 'scnet':
        from .scnet.scnet import SCNet
        return SCNet(**dict(config.model))
    raise ValueError('Unsupported compatibility architecture: ' + architecture)


def load_network(architecture, config, checkpoint):
    import torch
    model = build_network(architecture, config)
    # Restricted loading only; never retry an untrusted checkpoint with pickle.
    state = torch.load(checkpoint, map_location='cpu', weights_only=True)
    if isinstance(state, dict) and 'state_dict' in state:
        state = state['state_dict']
    state = {key: value for key, value in state.items() if key != '_metadata'}
    model.load_state_dict(state, strict=True)
    return model.cpu().eval()


def separate_sources(config, model, mix):
    """Upstream chunked inference, with complementary two-stem reconstruction."""
    import numpy as np
    import torch
    from bs_roformer.utils import demix_track

    original = np.asarray(mix, dtype=np.float32)
    samples = original.shape[-1]
    # BSRoformer's ISTFT without an explicit length rounds down to its hop.
    # Pad the model chunk, not the requested output, to avoid a truncated tail.
    hop = config.model.get('stft_hop_length', config.model.get('hop_size', 1))
    chunk = config.audio.chunk_size
    config.inference.chunk_size = ((chunk + hop - 1) // hop) * hop
    config.training.target_instrument = config.training.get('target_instrument')
    if not samples:
        raise ValueError('Input contains no audio samples')
    sources, _ = demix_track(config, model, torch.from_numpy(original.copy()), 'cpu')
    sources = {name: np.asarray(value, dtype=np.float32)[..., :samples]
               for name, value in sources.items()}
    if any(value.shape != original.shape for value in sources.values()):
        raise ValueError('Compatibility engine returned an unexpected audio length')
    target = config.training.target_instrument
    if target is not None:
        # HyperACE uses "instrument" in its config; expose standard GUI names.
        estimate = sources[target]
        if target.lower() == 'vocals':
            sources = {'vocals': estimate, 'instrumental': original - estimate}
        elif target.lower() in ('instrument', 'instrumental', 'other'):
            sources = {'instrumental': estimate, 'vocals': original - estimate}
        else:
            raise ValueError('Unsupported single-target compatibility model: ' + target)
    return sources


def load_into(separator, filename, architecture):
    """Use the existing engine for downloads, decoding, encoding and batch jobs."""
    from mlx_audio_separator.separator.common_separator import CommonSeparator
    from mlx_audio_separator.separator.architectures.mdxc_separator import MDXCSeparator

    _, _, _, checkpoint, config_name = separator.download_model_files(filename)
    config = read_config(Path(separator.model_file_dir) / config_name)
    network = load_network(architecture, config, checkpoint)

    class CompatibilitySeparator(MDXCSeparator):
        def __init__(self):
            common = {key: getattr(separator, key) for key in (
                'logger', 'log_level', 'output_format', 'output_bitrate', 'output_dir',
                'normalization_threshold', 'amplification_threshold', 'output_single_stem',
                'invert_using_spec', 'performance_params', 'performance_params_explicit_keys')}
            common.update(model_name=Path(filename).stem, model_path=checkpoint,
                          model_data=config.to_dict(), sample_rate=config.audio.sample_rate)
            CommonSeparator.__init__(self, config=common)
            self.model_run = network
            self.override_model_segment_size = False

        def _demix_mlx(self, mix, **kwargs):
            return separate_sources(config, self.model_run, mix)

    separator.model_name = Path(filename).stem
    separator.sample_rate = config.audio.sample_rate
    separator.model_type = 'MDXC'
    separator._skip_auto_tune = True
    separator.model_instance = CompatibilitySeparator()
    separator.logger.info('PyTorch CPU compatibility backend (this model is not MLX accelerated).')
