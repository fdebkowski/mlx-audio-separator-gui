"""Exercise MLX CLI translation and the post-load quality adapter."""
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import runner
import quality_settings as quality
import model_catalog as catalog


class RunnerQualityTests(unittest.TestCase):
    def test_cli_translates_shared_controls_and_retains_input_arguments(self):
        cli = SimpleNamespace(main=lambda: list(sys.argv))
        args = ['runner.py', 'song.wav', '--quality', 'high', '--segment-size', '512', '--overlap', '12']
        with patch.object(sys, 'argv', args), patch.object(runner, '_fix_path'), \
                patch.object(runner, '_install_save_shim'), patch.object(runner, '_install_extra_models_shim') as shim, \
                patch.dict(sys.modules, {'mlx_audio_separator.utils.cli': cli}):
            translated = runner.main()
        options = shim.call_args.args[0]
        self.assertEqual(options['segment_size'], 512)
        self.assertEqual(options['overlap'], 12)
        self.assertIn('song.wav', translated)
        self.assertNotIn('--quality', translated)
        self.assertIn('--vr_enable_tta', translated)
        self.assertEqual(translated[translated.index('--demucs_shifts') + 1], '4')

    def test_native_model_loading_applies_mlx_overlap_conversion(self):
        class Separator:
            def list_supported_model_files(self):
                return {}
            def download_model_files(self, name):
                return name
            def load_model(self, name):
                self.model_type = 'MDXC'
                self.model_instance = SimpleNamespace(sample_rate=44100, model_data={
                    'inference': {'dim_t': 1024}, 'model': {'stft_hop_length': 512}})
        with patch.dict(sys.modules, {'mlx_audio_separator.core': SimpleNamespace(Separator=Separator)}), \
                patch.object(catalog, 'load_state', return_value={}), patch.object(sys, 'argv', ['runner.py']):
            runner._install_extra_models_shim(quality.PRESETS['High'])
            separator = Separator()
            separator.load_model('normal-roformer.ckpt')
        self.assertEqual(separator.model_instance.segment_size, 1024)
        self.assertAlmostEqual(separator.model_instance.overlap * 44100, 512 * 1023 / 8)


if __name__ == '__main__':
    unittest.main()
