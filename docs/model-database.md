# Automatic model discovery

The model database is assembled from the maintained UVR model registry, audio-separator's model registry and score database, the original app's curated model manifest, configured Hugging Face authors, and global Hugging Face searches sorted by last modification time. Searching globally discovers new authors as well as new models from known publishers. No central database service or account is required.

While the application is running, the GUI checks whether a refresh is due hourly and refreshes daily. A failed network source retains its last good data and retries after an hour. **Refresh list** forces an immediate check. Model weights are downloaded only when selected for processing. The application does not run a background service when closed.

## Configure sources

The bundled `model_sources.json` supplies defaults. On first refresh it is copied to the app's support directory; edit that user copy to override defaults without modifying the application. Help → Model sources and refresh status displays the exact path.

- macOS: `~/Library/Application Support/MLX Audio Separator/model_sources.json`
- Windows CUDA: `%LOCALAPPDATA%\CUDA Audio Separator\model_sources.json`

Example:

```json
{
  "huggingface_authors": ["becruily", "pcunwa", "GaboxR67"],
  "huggingface_searches": ["roformer", "music-source-separation"],
  "huggingface_repositories": ["becruily/mel-band-roformer"],
  "search_limit": 30
}
```

Arrays replace the corresponding defaults. `search_limit` bounds each author's/search's latest results (1–100). Explicit repositories are checked individually. Checkpoint/config aliases and reviewed pairings remain in `catalog_settings.json`. The original curated GitHub feed remains public so both backends can consume it.

## Compatibility and trust

Automatic discovery accepts supported BS/Mel-Band Roformer YAML configurations with unambiguous checkpoint/config pairings. Both URLs are pinned to the same immutable Hugging Face commit. Authors with multiple checkpoints can require an explicit mapping. The app parses YAML safely and never downloads or executes repository Python. Experimental architectures, incompatible options and ambiguous pairs are recorded in `catalog_sources.json` under `skipped`. A new revision whose config becomes incompatible removes its previous discovery. Network failure preserves compatible cached entries.

Each backend still needs implementation support for new architectures. The Windows app excludes MLX-converted weights. The existing reviewed HyperACE/SCNet integrations retain their platform-specific backend.

## Inspect and refresh

Run using the app's environment:

```text
python catalog_cli.py
python catalog_cli.py --refresh
```

The JSON report includes last-check time, discoveries, feed warnings, models requiring review and the newest 15 discoveries with source links. `--refresh` exits 1 when a feed failed, even if cached data remains usable. The GUI's **Newest first** option orders models with known modification dates first; undated registry models follow. Dates do not imply quality rankings. Model license terms and SDR benchmark sources remain attached to their original authors.
