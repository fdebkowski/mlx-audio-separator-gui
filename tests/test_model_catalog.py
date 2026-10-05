"""Catalog migration and downloaded-model handling without a Tk window."""
import json
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app


MODEL_ID = "mel-roformer-zfturbo-vocals-v1-mlx"
MODEL_NAME = "ZFTurbo Mel-Band-RoFormer Vocals v1"
CATALOG = {"MDXC": {MODEL_NAME: {
    "filename": MODEL_ID,
    "stems": ["Vocals", "Instrumental"],
    "scores": {},
    "download_files": [f"{MODEL_ID}/model.safetensors", f"{MODEL_ID}/config.json"],
}}}


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("MODEL_DIR", self.root),
                            ("INDEX_CACHE", self.root / "index.json"),
                            ("FROZEN", True)):
            mock = patch.object(app, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.gui = app.SeparatorApp.__new__(app.SeparatorApp)
        self.gui.q = queue.Queue()
        mock = patch.object(app.model_catalog, 'load_state', return_value={})
        mock.start()
        self.addCleanup(mock.stop)

    def load_catalog(self):
        result = SimpleNamespace(stdout=json.dumps(CATALOG))
        with patch.object(app.subprocess, "run", return_value=result) as run:
            self.gui._load_models_worker()
        kind, models = self.gui.q.get_nowait()
        self.assertEqual(kind, "models_loaded")
        self.gui.models = models
        self.gui.model_by_file = {m["filename"]: m for m in models}
        return run

    def test_upgrade_refreshes_stale_catalog_and_retains_nested_files(self):
        app.INDEX_CACHE.write_text(json.dumps({"rev": 4, "index": {}}))
        run = self.load_catalog()
        run.assert_called_once()
        model = self.gui.model_by_file[MODEL_ID]
        self.assertEqual(model["friendly"], MODEL_NAME)
        self.assertIsNone(model["sdr"])
        self.assertEqual(model["files"], CATALOG["MDXC"][MODEL_NAME]["download_files"])
        self.assertEqual(app.stem_options(model["stems"]), {
            "All stems": None, "Vocals only": "vocals", "Instrumental only": "instrumental",
        })
        self.assertEqual(json.loads(app.INDEX_CACHE.read_text())["rev"], app.MODELS_REV)
        self.load_catalog().assert_not_called()

    def test_scan_reveal_and_delete_use_the_model_subfolder(self):
        self.load_catalog()
        # Same-named files in the root belong to other models.
        (self.root / "model.safetensors").write_bytes(b"unrelated")
        self.assertNotIn(MODEL_ID, self.gui._scan_downloaded())
        folder = self.root / MODEL_ID
        folder.mkdir()
        weights = folder / "model.safetensors"
        weights.write_bytes(b"weights")
        config = folder / "config.json"
        config.write_text("{}")
        self.assertIn(MODEL_ID, self.gui._scan_downloaded())
        self.assertEqual(set(self.gui._model_files_on_disk(MODEL_ID)), {weights, config})
        with patch.object(app.subprocess, "run") as reveal:
            self.gui._reveal_model(MODEL_ID)
        reveal.assert_called_once_with(["open", "-R", str(folder)])
        with patch.object(app.messagebox, "askyesno", return_value=True), \
             patch.object(self.gui, "_apply_filter"):
            self.gui._delete_model(MODEL_ID)
        self.assertFalse(weights.exists())
        self.assertFalse(config.exists())
        self.assertEqual((self.root / "model.safetensors").read_bytes(), b"unrelated")
        self.assertNotIn(MODEL_ID, self.gui.downloaded)

    def test_flat_and_demucs_weight_detection_still_requires_all_weights(self):
        self.gui.models = [
            {"filename": "flat.ckpt", "files": ["flat.ckpt", "config.yaml"]},
            {"filename": "bag.yaml", "files": ["bag.yaml", "a.th", "b.th"]},
        ]
        for name in ("flat.ckpt", "a.th"):
            (self.root / name).write_bytes(b"weights")
        self.assertEqual(self.gui._scan_downloaded(), {"flat.ckpt"})
        (self.root / "b.th").write_bytes(b"weights")
        self.assertEqual(self.gui._scan_downloaded(), {"flat.ckpt", "bag.yaml"})

    def test_url_downloads_keep_their_basename(self):
        catalog = {"MDXC": {"Extra": {
            "filename": "extra.ckpt",
            "download_files": ["https://example.com/models/extra.ckpt", "shared.yaml"],
        }}}
        with patch.object(app.subprocess, "run", return_value=SimpleNamespace(
                stdout=json.dumps(catalog))):
            self.gui._load_models_worker()
        kind, models = self.gui.q.get_nowait()
        self.assertEqual(kind, "models_loaded")
        self.assertEqual(models[0]["files"], ["extra.ckpt", "shared.yaml"])


if __name__ == "__main__":
    unittest.main()
