"""Data-source configuration, global discovery and offline refresh contracts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import model_catalog as catalog

CONFIG = '''model: {dim: 32, depth: 2, num_stems: 1, num_bands: 60}
training: {instruments: [vocals, instrumental], target_instrument: vocals}
'''


class ModelSourcesTests(unittest.TestCase):
    def test_windows_drive_paths_are_rejected(self):
        self.assertFalse(catalog.safe_path('C:/escape.ckpt'))
        self.assertFalse(catalog.safe_path('weights.ckpt:stream'))
        self.assertFalse(catalog.safe_path('bad\x00.ckpt'))

    def test_user_configuration_survives_and_invalid_repositories_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            defaults = catalog.source_settings(tmp)
            self.assertIn('roformer', defaults['huggingface_searches'])
            path = Path(tmp) / 'model_sources.json'
            catalog.atomic_json(path, {'huggingface_repositories': ['new-author/new-model'], 'search_limit': 12})
            self.assertEqual(catalog.source_settings(tmp)['search_limit'], 12)
            self.assertEqual(catalog.read_json(path)['huggingface_repositories'], ['new-author/new-model'])
            catalog.atomic_json(path, {'huggingface_repositories': ['../escape']})
            with self.assertRaises(ValueError):
                catalog.source_settings(tmp)
            catalog.atomic_json(path, {'search_limit': True})
            with self.assertRaises(ValueError):
                catalog.source_settings(tmp)

    def test_global_search_discovers_unknown_authors_and_pins_revision(self):
        repo = {'id': 'new-author/new-roformer', 'sha': 'a' * 40, 'lastModified': '2026-10-25T12:00:00Z',
                'siblings': [{'rfilename': 'weights.ckpt'}, {'rfilename': 'config.yaml'}]}
        settings = {'huggingface_authors': [], 'huggingface_searches': ['roformer'],
                    'huggingface_repositories': [repo['id']], 'search_limit': 10}
        requests = []
        def fetch(url):
            requests.append(url)
            if '/api/models?' in url:
                return json.dumps([repo])
            if url.endswith('/api/models/' + repo['id']):
                return json.dumps(repo)
            if url.endswith('/config.yaml'):
                return CONFIG
            raise OSError('offline feed')
        with tempfile.TemporaryDirectory() as tmp, patch.object(catalog, 'fetch', side_effect=fetch), \
                patch.object(catalog, 'source_settings', return_value=settings):
            state = catalog.refresh_sources({}, tmp)
            self.assertEqual(len(state['discovered']), 1)
            entry = state['discovered'][0]
            self.assertIn('/resolve/' + repo['sha'] + '/', entry['urls'][0])
            self.assertEqual(entry['updated'], repo['lastModified'])
            self.assertTrue(any('search=roformer' in url for url in requests))
            self.assertTrue(any('limit=10' in url for url in requests))
            self.assertEqual(catalog.catalog_status(state)['discovered_models'], 1)
            with patch.object(catalog, 'fetch', side_effect=OSError('offline')):
                offline = catalog.refresh_sources({}, tmp)
            self.assertEqual(offline['discovered'], state['discovered'])
            self.assertTrue(offline['warnings'])

    def test_changed_incompatible_configuration_evicts_prior_revision(self):
        repo_id = 'new-author/new-roformer'
        old = {'source_key': repo_id + '/weights.ckpt', 'revision': 'a' * 40,
               'source_url': 'https://huggingface.co/' + repo_id, 'source_filename': 'weights.ckpt',
               'filename': 'hf/old/weights.ckpt', 'friendly': 'Old'}
        repo = {'id': repo_id, 'sha': 'b' * 40,
                'siblings': [{'rfilename': 'weights.ckpt'}, {'rfilename': 'config.yaml'}]}
        def fetch(url):
            if '/api/models?' in url:
                return json.dumps([repo])
            if url.endswith('/config.yaml'):
                return CONFIG.replace('dim: 32', 'dim: 32, custom_layer: true')
            raise OSError('offline')
        settings = {'huggingface_authors': [], 'huggingface_searches': ['roformer'],
                    'huggingface_repositories': [], 'search_limit': 10}
        with tempfile.TemporaryDirectory() as tmp:
            catalog.atomic_json(Path(tmp) / 'catalog_sources.json', {'schema': catalog.SCHEMA, 'discovered': [old]})
            with patch.object(catalog, 'fetch', side_effect=fetch), patch.object(catalog, 'source_settings', return_value=settings):
                state = catalog.refresh_sources({}, tmp)
            self.assertFalse(state['discovered'])
            self.assertTrue(state['skipped'])


if __name__ == '__main__':
    unittest.main()
