"""Compatibility model inference contracts, without Apple Silicon or weights."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import model_catalog as catalog

HAS_ENGINE = all(importlib.util.find_spec(name) for name in ('torch', 'bs_roformer', 'numpy'))


class CompatibilityCatalogTests(unittest.TestCase):
    def test_requested_models_are_available_without_an_upstream_registry(self):
        models = catalog.merge_catalog({}, {})['MDXC']
        by_file = {model['filename']: model for model in models.values()}
        for filename in catalog.COMPAT_MODELS:
            model = by_file[filename]
            self.assertEqual(model['backend'], 'PyTorch CPU')
            self.assertEqual(len(model['urls']), 2)
            self.assertTrue(all(catalog.safe_url(url) for url in model['urls']))
        self.assertIn('becruily_deux.ckpt', by_file)
        self.assertEqual(by_file[catalog.SW_FOUR_STEM]['stems'], ['bass', 'drums', 'other', 'vocals'])
        self.assertEqual(by_file[catalog.SW_FOUR_STEM]['download_files'],
                         by_file['BS-Roformer-SW.ckpt']['download_files'])
        # A restored stale cache must not remove the compatibility backend label.
        self.assertEqual(by_file['model_scnet_ep_36_sdr_10.0891.ckpt']['benchmark'], 'MUSDB test')

    def test_cli_routes_compatibility_models_without_loading_the_mlx_network(self):
        import runner
        from engine_adapters import torch_models
        calls = []
        class Separator:
            def list_supported_model_files(self):
                return {'MDXC': {}}
            def download_model_files(self, filename):
                raise AssertionError('Should be downloaded by compatibility adapter')
            def load_model(self, filename):
                calls.append(filename)
        with patch.dict('sys.modules', {'mlx_audio_separator.core': SimpleNamespace(Separator=Separator)}), \
             patch.object(catalog, 'load_state', return_value={}), \
             patch.object(runner.sys, 'argv', ['runner.py']), \
             patch.object(torch_models, 'load_into') as load:
            runner._install_extra_models_shim()
            separator = Separator()
            for filename, architecture in catalog.COMPAT_MODELS.items():
                separator.load_model(filename)
                load.assert_called_with(separator, filename, architecture)
            self.assertFalse(calls)
            separator.load_model('becruily_deux.ckpt')
            self.assertEqual(calls, ['becruily_deux.ckpt'])


@unittest.skipUnless(HAS_ENGINE, 'Install the pinned compatibility engine to run CPU inference tests')
class TorchModelTests(unittest.TestCase):
    def setUp(self):
        import torch
        torch.set_num_threads(2)

    def config(self, target=None):
        from ml_collections import ConfigDict
        return ConfigDict(dict(
            audio=dict(chunk_size=257, sample_rate=44100),
            model=dict(hop_size=16),
            inference=dict(num_overlap=2),
            training=dict(instruments=['vocals', 'instrument'] if target else
                          ['drums', 'bass', 'other', 'vocals'], target_instrument=target)))

    def test_overlap_add_and_complement_preserve_tail_and_stereo(self):
        import numpy as np
        import torch
        from engine_adapters.torch_models import separate_sources

        class Target(torch.nn.Module):
            def forward(self, audio):
                return audio * 0.25

        rng = np.random.default_rng(1)
        for length in (1, 113, 513, 1501):
            mix = rng.standard_normal((2, length)).astype(np.float32)
            mix[:, -1] = [0.9, -0.3]
            for target in ('vocals', 'instrument'):
                sources = separate_sources(self.config(target), Target(), mix)
                self.assertEqual(set(sources), {'vocals', 'instrumental'})
                np.testing.assert_allclose(sources['vocals'] + sources['instrumental'], mix, atol=1e-6)
                key = 'vocals' if target == 'vocals' else 'instrumental'
                np.testing.assert_allclose(sources[key], mix * 0.25, atol=1e-6)

    def test_four_stems_keep_each_source_and_input_length(self):
        import numpy as np
        import torch
        from engine_adapters.torch_models import separate_sources

        class Four(torch.nn.Module):
            def forward(self, audio):
                return torch.stack([audio * scale for scale in (0.1, 0.2, 0.3, 0.4)], dim=1)

        mix = np.random.default_rng(2).standard_normal((2, 513)).astype(np.float32)
        sources = separate_sources(self.config(), Four(), mix)
        self.assertEqual(set(sources), {'drums', 'bass', 'other', 'vocals'})
        np.testing.assert_allclose(sum(sources.values()), mix, atol=1e-6)

    def test_scnet_network_loads_strictly_and_runs_real_forward(self):
        import numpy as np
        import torch
        from ml_collections import ConfigDict
        from engine_adapters.torch_models import build_network, load_network, separate_sources

        config = self.config()
        config.model = ConfigDict(dict(sources=['drums', 'bass', 'other', 'vocals'],
            audio_channels=2, dims=[4, 8, 16], nfft=128, hop_size=16, win_size=128,
            band_SR=[0.23, 0.37, 0.4], band_stride=[1, 2, 2], band_kernel=[3, 2, 2],
            conv_depths=[1, 1, 1], num_dplayer=2))
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / 'test.ckpt'
            network = build_network('scnet', config)
            torch.save(network.state_dict(), checkpoint)
            loaded = load_network('scnet', config, checkpoint)
            mix = np.random.default_rng(3).standard_normal((2, 513)).astype(np.float32)
            sources = separate_sources(config, loaded, mix)
            self.assertEqual(set(sources), set(config.training.instruments))
            for source in sources.values():
                self.assertEqual(source.shape, mix.shape)
                self.assertTrue(np.isfinite(source).all())
            state = network.state_dict()
            state.pop(next(iter(state)))
            torch.save(state, checkpoint)
            with self.assertRaises(RuntimeError):
                load_network('scnet', config, checkpoint)

    def test_config_loader_rejects_python_objects(self):
        from engine_adapters.torch_models import read_config
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.yaml'
            config.write_text('!!python/object/apply:builtins.print [unsafe]')
            with self.assertRaises(Exception):
                read_config(config)


if __name__ == '__main__':
    unittest.main()
