# Compatibility engine adapters

The GUI remains a standard-library CLI driver. This package extends the engine
process for two architectures unsupported by `mlx-audio-separator==0.1.20`.
Existing models continue to use their existing MLX backend.

HyperACE v2 construction and both models' overlap-add inference are delegated to
[OpenMIRLab bs-roformer-infer](https://github.com/openmirlab/bs-roformer-infer),
pinned to commit `95c8de7b0b091d00f064d694bf916a21866c32f5` in the engine requirements.
PyTorch CPU execution is intentional; no MPS or MLX performance claim is made.
The engine's existing decoder, writer, model cache, cancellation process and
single-stem output selection are reused. Model code is never loaded from a
downloaded Hugging Face repository.

`scnet/scnet.py` and `scnet/separation.py` are unmodified files from
[ZFTurbo/MSST](https://github.com/ZFTurbo/Music-Source-Separation-Training),
commit `6fdad13686bb7e84d029ec66f9dbe76e65c93ebf`, paths
`models/scnet/{scnet,separation}.py`. The upstream MIT license is retained in
`scnet/LICENSE`. This is the official SCNet architecture used by ZFTurbo's
published XL IHF checkpoint, not the separately implemented `scnet_unofficial`.

These licenses cover code only. Model weights retain their authors' terms.
