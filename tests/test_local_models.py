"""Local checkpoint import works without network catalog or settings edits."""
import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import model_catalog as catalog
import runner

CONFIG = '''
audio: {sample_rate: 44100}
model: {num_bands: 60, dim: 64, depth: 2}
training: {instruments: [vocals, other], target_instrument: other}
inference: {dim_t: 256}
'''


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.models = self.root / 'models'
        self.weight = self.root / 'Flowers V10.ckpt'
        self.weight.write_bytes(b'checkpoint fixture')
        self.config = self.root / 'separately named.yaml'
        self.config.write_text(CONFIG)

    def test_import_publishes_a_private_copy_and_retains_pair_metadata(self):
        filename = catalog.import_local_model(self.models, self.weight, self.config)
        another = catalog.import_local_model(self.models, self.weight, self.config)
        self.weight.unlink()
        self.config.unlink()
        self.assertNotEqual(filename, another)
        entries = catalog.local_models(self.models)
        self.assertEqual(len(entries), 2)
        name, entry = next((name, e) for name, e in entries.items() if e['filename'] == filename)
        self.assertIn('Flowers V10', name)
        self.assertEqual(entry['target_stem'], 'other')
        self.assertEqual(entry['stems'], ['vocals', 'other'])
        self.assertEqual((self.models / filename).read_bytes(), b'checkpoint fixture')
        self.assertEqual(json.loads((self.models / entry['config']).read_text())['inference']['dim_t'], 256)

    def test_invalid_yaml_and_interrupted_copy_leave_no_visible_import(self):
        self.config.write_text('model: [invalid]')
        with self.assertRaises(ValueError):
            catalog.import_local_model(self.models, self.weight, self.config)
        self.config.write_text(CONFIG)
        with patch('shutil.copyfile', side_effect=OSError('disk full')), self.assertRaises(OSError):
            catalog.import_local_model(self.models, self.weight, self.config)
        self.assertEqual(catalog.local_models(self.models), {})
        self.assertEqual(list((self.models / 'imports').iterdir()), [])

    def test_clean_offline_gui_lists_import_and_does_not_save_it_to_online_index(self):
        filename = catalog.import_local_model(self.models, self.weight, self.config)
        gui = app.SeparatorApp.__new__(app.SeparatorApp)
        gui.q = queue.Queue()
        index = self.root / 'index.json'
        with patch.object(app, 'MODEL_DIR', self.models), patch.object(app, 'INDEX_CACHE', index), \
                patch.object(app, 'FROZEN', True), patch.object(app.subprocess, 'run', side_effect=TimeoutError('offline')):
            gui._load_models_worker()
        event, rows = gui.q.get_nowait()
        self.assertEqual(event, 'models_loaded')
        self.assertEqual([row['filename'] for row in rows], [filename])
        self.assertFalse(index.exists())

    def test_worker_resolves_import_before_contacting_network_catalog(self):
        filename = catalog.import_local_model(self.models, self.weight, self.config)
        class Separator:
            model_file_dir = str(self.models)
            def list_supported_model_files(self):
                raise OSError('offline')
            def download_model_files(self, name):
                raise AssertionError('local model must not download')
            def load_model(self, name):
                self.model_instance = SimpleNamespace()
                return self.download_model_files(name)
        with patch.dict(sys.modules, {'mlx_audio_separator.core': SimpleNamespace(Separator=Separator)}), \
                patch.object(sys, 'argv', ['runner.py']):
            runner._install_extra_models_shim()
            separator = Separator()
            loaded = separator.load_model(filename)
            self.assertEqual(loaded[3], str(self.models / filename))
            self.assertEqual(loaded[4], str((self.models / filename).parent / 'roformer.yaml'))
            entries = separator.list_supported_model_files()['MDXC'].values()
            self.assertIn(filename, [row['filename'] for row in entries])

    def test_normalized_mel_transform_and_target_order_are_preserved(self):
        try:
            from mlx_audio_separator.separator.models.roformer.mel_band_roformer import MelBandRoformerMLX
            from mlx_spectro import get_transform_mlx
        except ImportError as error:
            self.skipTest(str(error))
        config = catalog.read_roformer_config(CONFIG.replace('num_bands: 60', 'num_bands: 60, stft_normalized: true'))
        instance = SimpleNamespace(model_data=config, primary_stem_name='vocals', secondary_stem_name='other',
                                   model=MelBandRoformerMLX(dim=64, depth=1))
        runner._configure_imported_roformer(instance)
        self.assertEqual((instance.primary_stem_name, instance.secondary_stem_name), ('other', 'vocals'))
        self.assertTrue(instance.model.stft_normalized)
        model = instance.model
        self.assertIs(model._stft_transform, get_transform_mlx(
            n_fft=model.stft_n_fft, hop_length=model.stft_hop_length, win_length=model.stft_win_length,
            window_fn='hann', window=None, periodic=True, center=True, normalized=True))
