"""Read the actual encoded subtype, not the worker's requested encoding."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runner

try:
    import mlx_audio_io as audio_io
    import numpy as np
    import soundfile as sf
except ImportError:
    audio_io = None


@unittest.skipIf(audio_io is None, 'native audio writer not installed')
class ExportDepthTests(unittest.TestCase):
    def test_explicit_depth_wins_in_sync_and_threaded_stem_writers(self):
        from mlx_audio_separator.utils.performance import AsyncStemWriter, SaveTask, save_stem
        from mlx import core as mx

        samples = np.stack((np.linspace(.1, .2, 256), np.linspace(-.2, -.1, 256))).astype('float32')
        source = mx.array(samples)
        mx.eval(source)
        with tempfile.TemporaryDirectory() as tmp:
            for ext, depth, subtype in (('wav', '16', 'PCM_16'), ('wav', '24', 'PCM_24'),
                                        ('wav', '32-float', 'FLOAT'), ('flac', '16', 'PCM_16'),
                                        ('flac', '24', 'PCM_24')):
                with self.subTest(ext=ext, depth=depth), patch.object(audio_io, 'save', audio_io.save):
                    runner._install_save_shim(depth)
                    for workers in (1, 2):
                        path = str(Path(tmp) / f'{depth}-{workers}.{ext}')
                        task = dict(stem_path=path, stem_source=source, sample_rate=44100,
                                    layout='channels_first', encoding='pcm16', bitrate='auto')
                        if workers == 1:
                            save_stem(SaveTask(**task))
                        else:
                            writer = AsyncStemWriter(workers=workers)
                            try:
                                writer.submit(**task)
                            finally:
                                writer.close()
                        self.assertEqual(sf.info(path).subtype, subtype)
                        decoded, rate = sf.read(path, dtype='float32')
                        self.assertEqual(rate, 44100)
                        np.testing.assert_allclose(decoded, samples.T, atol=1 / 32768, rtol=0)

    def test_float_flac_is_rejected_without_writing_a_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(audio_io, 'save', audio_io.save):
            runner._install_save_shim('32-float')
            path = Path(tmp) / 'output.flac'
            with self.assertRaisesRegex(ValueError, 'WAV'):
                audio_io.save(path, np.zeros((32, 2), dtype='float32'), 44100)
            self.assertFalse(path.exists())
