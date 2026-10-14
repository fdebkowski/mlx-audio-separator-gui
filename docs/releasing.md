# Releasing the macOS arm64 application

A release needs a native Apple Silicon build host running macOS 15 or later,
Python 3.13.7 with Tcl/Tk, Xcode Command Line Tools, and a working Metal device.
Linux and Intel/Rosetta builds are unsupported. The GitHub `macos-15` label is
arm64; `scripts/verify_bundle.py` fails if that runner cannot execute MLX on
Metal. If this happens, run the same commands on a physical Apple Silicon Mac
or change the build job to a trusted self-hosted macOS ARM64 runner with Metal.
Do not turn failed inference into a skipped release check.

## GitHub Actions

`.github/workflows/release.yml` builds, tests and validates the ZIP. A push to
`codex/release-*` builds only. A push to `release/v<APP_VERSION>` or a matching
`v<APP_VERSION>` tag also publishes, after validation. For ordinary manual use,
merge the workflow to main and run:

```bash
gh workflow run release.yml --repo fdebkowski/mlx-audio-separator-gui --ref main -f publish=false
gh run list --repo fdebkowski/mlx-audio-separator-gui --workflow release.yml --limit 5
gh run watch <RUN_ID> --repo fdebkowski/mlx-audio-separator-gui --exit-status
gh run download <RUN_ID> --repo fdebkowski/mlx-audio-separator-gui --dir release-artifacts
```

To build and publish v1.4.0 from main (whose `APP_VERSION` must be `1.4.0`):

```bash
gh workflow run release.yml --repo fdebkowski/mlx-audio-separator-gui --ref main -f publish=true
```

Watch the resulting run as above. Publishing requires the repository to allow
the job's `GITHUB_TOKEN` **contents: write** permission. Installing or changing
the workflow requires a GitHub token/app with workflow-write access. The
workflow first creates a draft, uploads the ZIP and evidence files, downloads
the uploaded ZIP to check its checksum, then marks the release public/latest.
An existing release stops publication; it is never silently replaced.

## Local build and full validation

Use a clean checkout of the intended release commit. Python 3.13.7's
python.org installer supplies Tcl/Tk. These commands do not install the
development app into `/Applications` or touch existing user model caches:

```bash
git clone https://github.com/fdebkowski/mlx-audio-separator-gui.git
cd mlx-audio-separator-gui
git checkout <RELEASE_COMMIT_SHA>
xcode-select --install  # once, if Command Line Tools are absent
PYTHON_BIN=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
"$PYTHON_BIN" -c 'import platform, tkinter; assert platform.machine() == "arm64"; assert platform.python_version() == "3.13.7"'
"$PYTHON_BIN" -m venv .release-venv
export MLX_SEPARATOR_VENV="$PWD/.release-venv"
"$MLX_SEPARATOR_VENV/bin/python" -m pip install -r requirements-build.txt
"$MLX_SEPARATOR_VENV/bin/python" -m pip check
"$MLX_SEPARATOR_VENV/bin/python" -m compileall -q app.py runner.py model_catalog.py main_bundle.py bundle_check.py bundle_fix_record.py scripts tests
bash -n build.sh setup.sh build_bundle.sh
"$MLX_SEPARATOR_VENV/bin/python" -m unittest discover -s tests -v
./build_bundle.sh
"$MLX_SEPARATOR_VENV/bin/python" scripts/verify_bundle.py \
  dist/MLX-Audio-Separator-1.4.0-macos-arm64.zip \
  --version 1.4.0 --report dist/validation.json
"$MLX_SEPARATOR_VENV/bin/python" -m pip freeze --all > dist/build-requirements.txt
git rev-parse HEAD > dist/source-commit.txt
sw_vers > dist/build-platform.txt
(cd dist && shasum -a 256 MLX-Audio-Separator-1.4.0-macos-arm64.zip > SHA256SUMS)
```

The native/core and packaging versions are pinned in
`constraints-release.txt`; the actual complete dependency resolution is
recorded in `build-requirements.txt`. For a rebuild, install that recorded file
into a new Python 3.13.7 environment first. Pip will preserve the constrained
native versions when the packaging script installs the engine. Third-party
package indexes, online catalogs and discovery can still change; this is a
repeatable checked process, not a claim of byte-identical ZIPs. The validation
report records the model download hashes and catalog state used in that run.

Validation checks the **extracted ZIP**, its two version fields and frozen
`APP_VERSION`, macOS deployment targets, arm64 binary slices, external dylib
references, strict deep ad-hoc signature, Tcl/Tk/tkdnd, dependency imports and
metadata, MLX GPU arithmetic, and bundled FFmpeg/FFprobe. It uses an isolated
HOME and system-only PATH, downloads models into an empty temporary folder,
and runs five separations: ZFTurbo Vocals v1, the added ZFTurbo and SYH
four-stem choices, SW four-stem, and SW Other-only. Outputs must contain the
expected non-silent finite stereo 44.1 kHz FLAC stems, exactly eight seconds;
SW's two Other outputs must decode identically. This tests functionality with
a synthetic 24-bit fixture, not listening quality.

## Publish a locally verified ZIP

Run only after the entire validation command succeeds. Authenticate `gh` with
a GitHub account/token allowed to create releases and tags in this repository.

```bash
gh auth login
gh release create v1.4.0 --repo fdebkowski/mlx-audio-separator-gui \
  --target "$(cat dist/source-commit.txt)" --draft \
  --title 'MLX Audio Separator v1.4.0' --notes-file docs/releases/v1.4.0.md \
  dist/MLX-Audio-Separator-1.4.0-macos-arm64.zip dist/SHA256SUMS \
  dist/validation.json dist/build-requirements.txt dist/source-commit.txt dist/build-platform.txt
mkdir -p dist/upload-check
gh release download v1.4.0 --repo fdebkowski/mlx-audio-separator-gui \
  --pattern '*.zip' --dir dist/upload-check
cp dist/SHA256SUMS dist/upload-check/
(cd dist/upload-check && shasum -a 256 -c SHA256SUMS)
gh release edit v1.4.0 --repo fdebkowski/mlx-audio-separator-gui --draft=false --latest
gh release view v1.4.0 --repo fdebkowski/mlx-audio-separator-gui --json url,isDraft,assets
gh api repos/fdebkowski/mlx-audio-separator-gui/releases/latest --jq '{tag_name, html_url, assets: [.assets[].name]}'
```

If uploading or checksum verification fails, leave the release as a draft,
inspect it, repair only the draft's assets with `gh release upload`, re-download
and recheck them before publishing. Never publish an empty release just to
update the latest tag. Ad-hoc signing is not Developer ID signing/notarization;
downloaded applications can still require clearing macOS quarantine.
