# Modern separation model integration — 22 October 2026

## Outputs

| Model | Outputs | Execution |
| --- | --- | --- |
| HyperACE v2 Vocals by unwa | Vocals + instrumental, with instrumental derived from mixture minus vocals | PyTorch CPU compatibility adapter |
| HyperACE v2 Instrumental by unwa | Instrumental + vocals, with vocals derived from mixture minus instrumental | PyTorch CPU compatibility adapter |
| SCNet XL IHF by ZFTurbo | Drums, bass, other, vocals | PyTorch CPU compatibility adapter |
| MelBand Roformer Deux by becruily | Vocals + instrumental | Existing MLX path |
| BS Roformer SW four-stem fold | Drums, bass, vocals, other + guitar + piano | Existing MLX path |

Deux and the SW fold were already integrated. This change adds the previously
unsupported HyperACE and SCNet models, repairs/pins the SW download source and
ensures SW is present even when the engine registry is empty or stale.
The historical [5 October audit](model-audit-2026-10-05.md) described HyperACE and
SCNet as unsupported; the new CPU adapter supersedes that limitation, but does
not provide native MLX implementations.

## Integration boundaries

The app still drives one engine subprocess per separation job. The adapter
reuses the engine's downloads, audio decoder, output encoder, batch processing
and single-stem selection. Cancellation terminates the same engine process.
Compatibility models are labelled CPU (slower) in the model list and details.
They do not change existing models' execution backend.

The compatibility manifest is bundled separately from the remotely refreshed
extras manifest, so old app versions do not discover models requiring code
they do not have. The catalog revision is bumped to refresh installed indexes.

SCNet's upstream source and MIT notice are retained. HyperACE construction and
overlap-add inference use an immutable bs-roformer-infer commit. Restricted
checkpoint loading and strict tensor matching are used for both architectures.
No Python code is downloaded or executed from model repositories.

## Validation

- 25 headless tests pass, including CPU inference through a real small SCNet,
  four-source overlap-add, complementary two-stem reconstruction, stereo/tail
  preservation, incomplete checkpoints and CLI model routing.
- Python compilation and shell syntax checks pass.
- All three actual published checkpoints downloaded and loaded strictly using
  PyTorch 2.8.0 on Linux CPU: each HyperACE network has 72,042,108 parameters;
  SCNet XL IHF has 53,466,800 parameters.
- Short inference with those real weights returned the expected nonzero,
  finite stereo stems, exactly 4,097 samples long. HyperACE outputs summed
  back to the input within 1e-6. The smoke test used shortened inference chunks
  (32,768 samples for HyperACE; 16,384 for SCNet) to bound runtime. Runtime
  defaults continue to use each published config's chunk size, rounded up to
  an STFT hop boundary to preserve the final samples.
- No full-dataset SDR benchmark or listening comparison was run.
- The frozen macOS app and its native I/O pipeline were not run on this Linux
  host. The mandatory extracted-bundle release gate now covers both HyperACE
  models, SCNet XL IHF, Deux and the existing SW fold before publication.

The new model weights retain their authors' terms independently of the app's
MIT license. CPU support is not a claim of MLX or MPS acceleration.
