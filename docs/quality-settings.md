# Separation quality controls

Choose **Fast**, **Balanced**, or **High** under **Choose what you get**. **Advanced…** exposes controls for the selected model family. Applying manual settings selects **Custom**. Settings are saved between launches and printed in Show Details.

| Control | Fast | Balanced (default) | High |
| --- | --- | --- | --- |
| Roformer / MDXC segment | 128 frames | 256 frames | Model's configured segment |
| Roformer / MDXC overlap count | 2 | 4 | 8 |
| MDX segment | 128 frames | 256 frames | 256 frames |
| MDX / Demucs overlap fraction | 0.25 | 0.50 | 0.75 |
| Demucs segment | 5 seconds | Model default | Model default |
| Demucs shift averaging | 1 pass | 2 passes | 4 passes |
| VR test-time augmentation | Off | Off | On |

**Overlap** combines neighboring predictions and can reduce boundary artifacts at the cost of time. MDXC/Roformer use a count (`step = chunk_samples / overlap_count`); MDX/Demucs use a fraction (`step = chunk_samples × (1 - overlap_fraction)`). The advanced dialog labels the units separately.

**Segment size** controls the audio predicted in one pass. Larger segments use more memory and may improve context, but bigger does not guarantee a better result. Model retains the checkpoint's configured context for Roformer/MDXC; MDX uses its standard 256 frames. Frame context is approximately `STFT hop × (frames - 1) / sample rate` seconds. Demucs uses seconds directly, capped at 7 in Advanced to accommodate Hybrid Transformer models.

Start with Balanced on a 6 GB NVIDIA card. Compare High on a representative excerpt: listen for remaining instruments, lost syllables, reverb and boundary artifacts. Model choice remains important. High describes computation, not a measured SDR improvement. WAV/FLAC preserve predictions without lossy compression; MP3 bitrate does not improve separation.

## Backend handling

- CUDA passes settings to native audio-separator architecture parameters. Native Roformer context is also retained on its short-clip path.
- MLX 0.1.20 interprets ordinary Roformer overlap as stride seconds. The app translates the count to this stride; blindly passing 8 could reduce overlap. The Hugging Face MLX path uses count/sample values directly.
- HyperACE/SCNet adapters convert frames to sample chunks and apply the overlap count to their overlap-add inference.
- Windows memory recovery reduces the segment to 128 frames (Demucs to 3 seconds), retaining overlap. The effective settings are logged; this recovery tradeoff can change the sound.

## Sources

Checked against [UVR5 revision a5f8845](https://github.com/Anjok07/ultimatevocalremovergui/tree/a5f88453bfb2b38b05a965bcf67727243e0cbf19): `gui_data/constants.py` defines controls/help and `separate.py` implements prediction schedules. This app provides presets and translates units for each backend; it does not copy UVR's implementation or claim benchmark superiority.

Engines: [python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator), [mlx-audio-separator](https://github.com/ssmall256/mlx-audio-separator).
