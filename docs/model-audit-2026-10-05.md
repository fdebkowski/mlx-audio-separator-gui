# Model audit — 5 October 2026

Compared with the installed 1.3.1 catalog (178 entries). Dates below describe
Hugging Face repository uploads/updates, not necessarily training dates.
The app now checks community metadata and published score registries daily.
Weights download only when a model is selected for separation.

## Four-stem choices

Use **4 stems** above the model list to show vocals/drums/bass/other splitters.
The added SW four-stem mode runs the six-stem checkpoint and adds guitar and
piano to other **before output encoding**. It also works with Other only.
It shares one download with the existing six-stem SW entry.

| Choice | Published evidence | Integration |
| --- | --- | --- |
| BS Roformer SW | Multisong: bass 14.6235, drums 14.1129, vocals 11.3019, other 8.7169 dB. [Source](https://mvsep.com/quality_checker/entry/8374) | Recommended local option; new four-stem fold-down. Published numbers describe the source six-stem evaluation, not a new benchmark of this mode. |
| HTDemucs fine-tuned | Existing official four-stem bag; its four specialist networks produce one stem each. | Already included. |
| ZFTurbo BS Roformer MUSDB18HQ | MUSDB **test** mean 9.65 dB; Multisong mean 9.38 dB. [Author's registry](https://github.com/ZFTurbo/Music-Source-Separation-Training/blob/main/docs/pretrained_models.md) | Added four-stem checkpoint. |
| SYH BS Roformer four-stem FT | [Author's weights and config](https://huggingface.co/SYH99999/bs_roformer_4stems_ft) | Added; no verified score assigned from the filename. |
| Aname Mel-Band Roformer Large / XL | Existing MVSEP per-stem measurements. | Already included. |
| Aname Huge-SCNet v1.2 | Multisong: bass 12.0639, drums 11.7422, vocals 9.6073, other 6.6485 dB. [Source](https://mvsep.com/quality_checker/entry/9648) | Requires an SCNet backend; excluded from runnable discovery. |
| SCNet XL IHF | MUSDB **test** mean 10.08 dB; Multisong mean 9.92 dB. [Author's registry](https://github.com/ZFTurbo/Music-Source-Separation-Training/blob/main/docs/pretrained_models.md) | Requires an SCNet backend. |
| HiDolen Mini-BS-RoFormer V2 46.8M | Author reports 10.03 dB on MUSDB18HQ **validation**, which is not comparable to the test scores above. [Source](https://huggingface.co/HiDolen/Mini-BS-RoFormer-V2-46.8M) | Different architecture/weight layout; requires a port. July 2026 mirrors are not new training releases. |

SW's published results exceed Huge-SCNet v1.2 on each of those four Multisong
stems. This is a comparison of these published runs, not a claim that any model
wins for every song. No full-dataset quality benchmark was run for this change.

## Other omissions identified

| Model | Source date | Result |
| --- | --- | --- |
| [becruily Invert Clean](https://huggingface.co/becruily/mel-band-roformer-invert-clean) | 26 August 2026 | Discovered automatically. Intended to clean acapella inverts, clicks and instrumental residue. |
| [anvuew BS RoFormer / FT1](https://huggingface.co/anvuew/BS-RoFormer) | Updated 17 April 2026 | Discovered with explicitly paired shared config; published Multisong scores added. |
| [Gabox Small Karaoke](https://huggingface.co/GaboxR67/MelBandRoformers/tree/main/melbandroformers/karaoke) | Weight uploaded 28 February 2026 | Discovered using the matching small-model config. |
| [Gabox BS Karaoke IS](https://huggingface.co/GaboxR67/MelBandRoformers/tree/main/bsroformers) | Weight uploaded 22 October 2025 | Discovered with its own BS config. |
| [Aname Full Scratch Vocals](https://huggingface.co/Aname-Tommy/Mel_Band_Roformer_Full_Scratch) | Updated 8 October 2025 | Discovered automatically. |
| [Aname Duality](https://huggingface.co/Aname-Tommy/Mel-Band-Roformer_Duality) | 21 September 2025 | Discovered automatically. |
| [Gabox Flowers V10](https://huggingface.co/GaboxR67/MelBandRoformers/tree/main/melbandroformers/instrumental) | Weight uploaded 4 January 2026 | Different from existing experimental INSTV10. Its normalized STFT is not forwarded by the pinned engine loader; held for engine support. |
| [unwa Large Inst](https://huggingface.co/pcunwa/BS-Roformer-Large-Inst) | 25 January 2026 | Modified Transformer mask estimator; requires an MLX architecture extension. |
| [unwa HyperACE v2 vocals / instrumental](https://huggingface.co/pcunwa/BS-Roformer-HyperACE) | Updated 20 December 2025 | Modified hypergraph architecture; requires an MLX extension. |
| [unwa SiameseRoformer](https://huggingface.co/pcunwa/BS-EXP-SiameseRoformer) | 11 March 2026 | Experimental normalization architecture; held for review. |

Aname's Lazy Bat BS models currently return HTTP 401 for the config download.
They are not presented as usable additions. Archived, experimental and ambiguous
checkpoint/config pairs are also kept out of automatic additions.

## Scores and discovery

`score_overrides.json` records exact model-to-MVSEP submission matches for missing
scores, including Leap instrumental variants, SW, Gabox vocal variants and FV9,
becruily Guitar and Karaoke. Guitar and Lead/Back use separate benchmark labels.
The app does not copy scores out of checkpoint filenames or replace existing
MUSDB scores with unrelated Multisong results.

The online catalog combines the maintained audio-separator/UVR registries,
this app's community manifest, the upstream SDR registry and Hugging Face
metadata from seven known model authors. New Hugging Face entries need an
unambiguous weight/config pair and a configuration the installed engine supports.
No Python code from model repositories is loaded. Checkpoint and config URLs
are pinned to the same Hugging Face revision; downloads use separate namespaces.
Architecture changes still require a software update.

Sources are checked in a background engine process, with a daily interval and a
one-hour retry after partial failures. Cached models remain usable offline.
**Refresh list** forces a check. Right-click **Quality and model details** shows
precise per-stem numbers, benchmark and source. Models without verified scores
keep a blank quality cell. Multi-stem scores show the mean of reported stems.
