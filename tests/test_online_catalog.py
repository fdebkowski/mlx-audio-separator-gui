"""Failure/compatibility boundaries of online discovery and real download routes."""
import copy
import json
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import model_catalog as catalog
import runner

CONFIG = '''audio: {sample_rate: 44100}
model: {dim: 256, depth: 12, num_stems: 4, freqs_per_bands: [2, 3]}
training: {instruments: [bass, drums, other, vocals], target_instrument: null}
'''
REVISION = 'a' * 40


class DiscoveryTests(unittest.TestCase):
    def test_only_unambiguous_supported_configs_become_runnable(self):
        repo = {'id': 'becruily/new-four-stem', 'sha': REVISION,
                'siblings': [{'rfilename': p} for p in ('model.ckpt', 'config.yaml')]}
        with patch.object(catalog, 'fetch', return_value=CONFIG):
            entries, skipped = catalog.discover_repo(repo, set(), {}, {})
        self.assertFalse(skipped)
        entry = entries[0]
        self.assertEqual(entry['stems'], ['bass', 'drums', 'other', 'vocals'])
        self.assertEqual(entry['target_stem'], None)
        self.assertIn(REVISION, entry['urls'][0])
        self.assertTrue(entry['filename'].startswith('hf/becruily/new-four-stem/'))
        # Different author/config.yaml cannot collide with this config.
        second = dict(repo, id='pcunwa/new-four-stem')
        with patch.object(catalog, 'fetch', return_value=CONFIG):
            other, _ = catalog.discover_repo(second, set(), {}, {})
        self.assertNotEqual(entry['config'], other[0]['config'])
        repo['siblings'].append({'rfilename': 'custom_model.py'})
        with patch.object(catalog, 'fetch') as fetch:
            entries, skipped = catalog.discover_repo(repo, set(), {}, {})
        self.assertFalse(entries)
        self.assertTrue(skipped)
        fetch.assert_not_called()

    def test_aliases_and_archived_models_do_not_create_duplicate_rows(self):
        repo = {'id': 'pcunwa/new-four-stem', 'sha': REVISION,
                'siblings': [{'rfilename': p} for p in ('weights.ckpt', 'config.yaml')]}
        settings = {'aliases': {repo['id'] + '/weights.ckpt': 'renamed.ckpt'}}
        with patch.object(catalog, 'fetch') as fetch:
            self.assertEqual(catalog.discover_repo(repo, {'renamed.ckpt'}, settings, {}), ([], []))
        fetch.assert_not_called()
        self.assertFalse(catalog._pairs(repo['id'], ['archive/model.ckpt', 'archive/model.yaml'], {}))
        self.assertFalse(catalog.repo_allowed('anvuew/BS_RoFormer_mag', {}))

    def test_normalization_custom_architectures_and_unsafe_yaml_are_rejected(self):
        for suffix in ('stft_normalized: true', 'skip_connection: true', 'custom_hypergraph: true'):
            config = CONFIG.replace('num_stems: 4', 'num_stems: 4, ' + suffix)
            with self.assertRaises(ValueError):
                catalog.config_metadata(config)
        with self.assertRaises(Exception):
            catalog.config_metadata('!!python/object/apply:os.system [touch /tmp/should-not-exist]')
        compatible = CONFIG + 'loss_windows: !!python/tuple [4096, 2048]\n'
        self.assertEqual(catalog.config_metadata(compatible)['family'], 'BS Roformer')

    def test_online_failure_preserves_previous_catalog_and_score_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = {'schema': catalog.SCHEMA, 'scores': {'old.ckpt': {'median_scores': {'vocals': {'SDR': 10}}}},
                       'extras': {}, 'discovered': []}
            catalog.atomic_json(Path(tmp) / 'catalog_sources.json', initial)
            with patch.object(catalog, 'fetch', side_effect=OSError('offline')), \
                 patch.object(catalog, 'AUTHORS', ('becruily',)):
                state = catalog.refresh_sources({}, support=tmp)
            self.assertEqual(state['scores'], initial['scores'])
            self.assertTrue(state['warnings'])
            self.assertFalse(catalog.due(state, now=state['checked_at'] + 10))
            self.assertTrue(catalog.due(state, now=state['checked_at'] + 3601))

    def test_invalid_remote_catalog_cannot_override_working_data(self):
        for filename in ('../weights.ckpt', '/tmp/weights.ckpt', 'foo/../../weights.ckpt'):
            with self.assertRaises(ValueError):
                catalog.validate_extras({'bad': {'filename': filename, 'config': 'config.yaml',
                                                'urls': ['https://huggingface.co/a/b/resolve/main/model.ckpt'] * 2,
                                                'stems': ['vocals', 'other']}})
        for value in (float('nan'), float('inf'), True, '11.1'):
            self.assertFalse(catalog.valid_scores({'vocals': {'SDR': value}}))

    def test_missing_scores_are_filled_without_relabelling_existing_benchmarks(self):
        baseline = {'MDXC': {'known': {'filename': 'known.ckpt', 'scores': {}, 'stems': ['vocals', 'other'], 'target_stem': 'vocals', 'download_files': ['known.ckpt']},
                             'measured': {'filename': 'measured.ckpt', 'scores': {'vocals': {'SDR': 12}}, 'benchmark': 'MUSDB', 'download_files': ['measured.ckpt']}}}
        override = {'scores': {'vocals': {'SDR': 10.5}, 'instrumental': {'SDR': 17}}, 'target_stem': 'vocals', 'benchmark': 'MVSEP', 'source_url': 'https://mvsep.com/quality_checker/entry/1'}
        with patch.object(catalog, 'bundled_json', return_value={}):
            merged = catalog.merge_catalog(baseline, {'overrides': {'known.ckpt': override, 'measured.ckpt': override}})
        self.assertEqual(merged['MDXC']['known']['score_source'], override['source_url'])
        self.assertEqual(merged['MDXC']['known']['benchmark'], 'MVSEP')
        self.assertEqual(app.best_sdr(merged['MDXC']['known']), 10.5)
        self.assertEqual(merged['MDXC']['measured']['scores']['vocals']['SDR'], 12)
        self.assertEqual(merged['MDXC']['measured']['benchmark'], 'MUSDB')
        self.assertEqual(baseline['MDXC']['known']['scores'], {})
        self.assertEqual(app.best_sdr({'scores': {'bass': {'SDR': 14}, 'other': {'SDR': 6}}, 'target_stem': None}), 10)


class OfflineGuiTests(unittest.TestCase):
    def test_stale_index_refresh_failure_keeps_working_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / 'index.json'
            old = {'MDXC': {'old': {'filename': 'old.ckpt', 'download_files': ['old.ckpt']}}}
            catalog.atomic_json(index, {'rev': app.MODELS_REV, 'checked_at': 1, 'index': old})
            gui = app.SeparatorApp.__new__(app.SeparatorApp)
            gui.q = queue.Queue()
            with patch.object(app, 'INDEX_CACHE', index), patch.object(app, 'FROZEN', True), \
                 patch.object(app.subprocess, 'run', side_effect=TimeoutError('offline')):
                gui._load_models_worker(force=True)
            event, models = gui.q.get_nowait()
            self.assertEqual(event, 'models_loaded')
            self.assertEqual(models[0]['filename'], 'old.ckpt')
            self.assertEqual(gui.q.get_nowait()[0], 'catalog_warning')
            self.assertEqual(json.loads(index.read_text())['index'], old)


class DownloadAndFoldTests(unittest.TestCase):
    def test_partial_download_is_not_accepted_as_complete(self):
        class Separator:
            def list_supported_model_files(self):
                return {'MDXC': {}}
            def download_model_files(self, name):
                raise ValueError(name)
            def load_model(self, name):
                pass
            def download_file_if_not_exists(self, url, path):
                Path(path).write_bytes(b'partial')
                raise OSError('interrupted')
        with tempfile.TemporaryDirectory() as tmp:
            entry = {'filename': 'hf/test/model.ckpt', 'config': 'hf/test/config.yaml',
                     'urls': ['https://huggingface.co/a/b/resolve/main/model.ckpt', 'https://huggingface.co/a/b/resolve/main/config.yaml'],
                     'stems': ['vocals', 'other']}
            with patch.dict('sys.modules', {'mlx_audio_separator.core': SimpleNamespace(Separator=Separator)}), \
                 patch.object(catalog, 'load_state', return_value={}), \
                 patch.object(catalog, 'bundled_json', side_effect=lambda name: {'new': entry} if name == 'extra_models.json' else {}), \
                 patch.object(runner.sys, 'argv', ['runner.py']):
                runner._install_extra_models_shim()
                separator = Separator()
                separator.model_file_dir = tmp
                with self.assertRaises(OSError):
                    separator.download_model_files(entry['filename'])
                self.assertFalse((Path(tmp) / entry['filename']).exists())
                self.assertFalse(list(Path(tmp).rglob('*.partial')))

    def test_fold_preserves_all_six_sources_and_requires_all_inputs(self):
        # Scalars are sufficient to verify the linear fold without MLX/numpy.
        source = {'bass': 1, 'drums': 2, 'vocals': 3, 'other': 4, 'guitar': 5, 'piano': 6}
        four = runner.fold_four_stems(source)
        self.assertEqual(four, {'bass': 1, 'drums': 2, 'vocals': 3, 'other': 15})
        self.assertEqual(sum(source.values()), sum(four.values()))
        with self.assertRaises(ValueError):
            runner.fold_four_stems({'bass': 1})

    def test_other_only_fold_runs_all_six_masks_and_preserves_selection(self):
        observed = []
        class Separator:
            def list_supported_model_files(self):
                return {'MDXC': {'SW': {'filename': 'BS-Roformer-SW.ckpt', 'download_files': ['BS-Roformer-SW.ckpt']}}}
            def download_model_files(self, model_filename):
                return model_filename
            def load_model(self, model_filename):
                self.model_instance = SimpleNamespace(output_single_stem='other')
                def demix(*args, **kwargs):
                    observed.append(self.model_instance.output_single_stem)
                    return {'bass': 1, 'drums': 2, 'vocals': 3, 'other': 4, 'guitar': 5, 'piano': 6}
                self.model_instance._demix_mlx = demix
        with patch.dict('sys.modules', {'mlx_audio_separator.core': SimpleNamespace(Separator=Separator)}), \
             patch.object(catalog, 'load_state', return_value={}), \
             patch.object(runner.sys, 'argv', ['runner.py']):
            runner._install_extra_models_shim()
            separator = Separator()
            separator.load_model(model_filename=catalog.SW_FOUR_STEM)
            sources = separator.model_instance._demix_mlx(None)
            self.assertEqual(observed, [None])
            self.assertEqual(sources['other'], 15)
            self.assertEqual(separator.model_instance.output_single_stem, 'other')


if __name__ == '__main__':
    unittest.main()
