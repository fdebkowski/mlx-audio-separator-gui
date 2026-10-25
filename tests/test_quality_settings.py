"""Quality controls must reach real inference parameters, including MLX units."""
import argparse
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import quality_settings as quality


class QualityTests(unittest.TestCase):
    def test_presets_and_custom_settings_survive_cli_round_trip(self):
        parser = argparse.ArgumentParser()
        quality.add_arguments(parser)
        for name, value in quality.PRESETS.items():
            resolved = quality.from_arguments(parser.parse_args(['--quality', name.lower()]))
            self.assertEqual(resolved, value)
            self.assertEqual(quality.from_arguments(parser.parse_args(quality.cli_args(value))), value)
        custom = quality.from_arguments(parser.parse_args(['--quality', 'high', '--segment-size', '512', '--overlap', '12']))
        self.assertEqual(custom['segment_size'], 512)
        self.assertEqual(custom['overlap'], 12)
        self.assertTrue(custom['vr_tta'])

    def test_real_architecture_parameters_use_the_correct_units(self):
        params = quality.engine_params(quality.PRESETS['High'])
        self.assertEqual(params['mdxc_params']['overlap'], 8)
        self.assertFalse(params['mdxc_params']['override_model_segment_size'])
        self.assertEqual(params['mdx_params']['overlap'], .75)
        self.assertEqual(params['demucs_params']['overlap'], .75)
        self.assertEqual(params['demucs_params']['shifts'], 4)
        self.assertTrue(params['vr_params']['enable_tta'])

    def test_mlx_count_is_translated_to_stride_seconds(self):
        data = {'model': {'stft_hop_length': 512}, 'audio': {'sample_rate': 44100},
                'inference': {'dim_t': 1024}}
        instance = SimpleNamespace(model_data=deepcopy(data), sample_rate=44100)
        value = dict(quality.PRESETS['High'])
        quality.apply_mlx_mdxc(instance, value)
        chunk = 512 * (1024 - 1)
        self.assertEqual(instance.segment_size, 1024)
        self.assertAlmostEqual(instance.overlap * 44100, chunk / 8)
        previous = instance.overlap
        value['overlap'] = 16
        quality.apply_mlx_mdxc(instance, value)
        self.assertAlmostEqual(instance.overlap, previous / 2)
        self.assertTrue(instance.override_model_segment_size)

    def test_mlx_huggingface_model_preserves_native_sample_context(self):
        instance = SimpleNamespace(sample_rate=44100, model_data={
            'backend': 'huggingface_mel_roformer', 'inference': {'chunk_size': 485100},
            'model': {'stft_hop_length': 512}})
        quality.apply_mlx_mdxc(instance, quality.PRESETS['High'])
        self.assertEqual(instance.model_data['inference'], {'chunk_size': 485100, 'num_overlap': 8})
        quality.apply_mlx_mdxc(instance, quality.PRESETS['Fast'])
        self.assertEqual(instance.model_data['inference']['chunk_size'], 512 * 127)
        self.assertEqual(instance.model_data['inference']['num_overlap'], 2)

    def test_compatibility_models_use_selected_context_and_overlap(self):
        from ml_collections import ConfigDict
        config = ConfigDict(dict(audio=dict(chunk_size=485100), model=dict(stft_hop_length=512), inference={}))
        quality.apply_compat_config(config, quality.PRESETS['High'])
        self.assertEqual(config.audio.chunk_size, 485100)
        self.assertEqual(config.inference.num_overlap, 8)
        quality.apply_compat_config(config, quality.PRESETS['Fast'])
        self.assertEqual(config.audio.chunk_size, 512 * 127)
        self.assertEqual(config.inference.num_overlap, 2)

    def test_invalid_settings_cannot_reach_the_engine(self):
        for key, value in (('overlap', 0), ('overlap', 51), ('segment_size', True), ('segment_size', 1),
                           ('overlap_ratio', float('nan')), ('overlap_ratio', 1), ('demucs_shifts', -1)):
            with self.assertRaises(ValueError):
                quality.validate({**quality.PRESETS['Balanced'], key: value})

    def test_gui_preset_selection_is_persisted_and_changes_engine_arguments(self):
        control = quality.QualityControls.__new__(quality.QualityControls)
        control.preset = SimpleNamespace(get=lambda: 'High')
        saved = []
        control.on_save = saved.append
        with patch.object(control, '_summarize'):
            control._preset_changed()
        self.assertEqual(saved[0], {'preset': 'High', 'values': quality.PRESETS['High']})
        self.assertIn('model', control.engine_args())
        self.assertIn('8', control.engine_args())


if __name__ == '__main__':
    unittest.main()
