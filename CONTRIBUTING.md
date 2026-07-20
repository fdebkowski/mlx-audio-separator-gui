# Contributing

Thanks for helping make MLX Audio Separator better!

## Dev setup

```bash
git clone https://github.com/fdebkowski/mlx-audio-separator-gui.git
cd mlx-audio-separator-gui
./setup.sh                                        # venv + engine + app build
~/.venvs/mlx-audio-separator/bin/python app.py    # run the GUI from source
```

No editable install, no build step — edit `app.py` and rerun. Rebuild the
`.app` bundle with `./build.sh` only when you need to test Finder/Dock launch
behavior (that's where `runner.py`'s PATH fix matters).

## Ground rules

- **Keep the GUI stdlib-only.** The app deliberately has no runtime
  dependencies beyond Tkinter and the engine CLI. Open an issue first if you
  think a dependency is worth it.
- **The GUI stays a thin driver.** Separation logic belongs in the
  [engine](https://github.com/ssmall256/mlx-audio-separator), not here. This
  repo owns UX, process management, and macOS integration.
- **Match the existing style** — small methods on `SeparatorApp`, queue-based
  UI updates from worker threads, comments only where the *why* isn't obvious.
- **Design for casual users.** Plain language in labels, safe defaults,
  technical detail behind "Show Details".

## Checks

There's no test suite (it's a Tkinter app); CI just compiles the sources.
Before opening a PR:

```bash
~/.venvs/mlx-audio-separator/bin/python -m py_compile app.py runner.py
bash -n build.sh setup.sh
```

…and click through a real separation run.

## Commits & PRs

- Conventional commit style: `feat:`, `fix:`, `refactor:`, `docs:`, …
- One focused change per PR, with a screenshot for anything visual.

## Reporting issues

Please include your macOS version, Mac model, and the contents of the
**Show Details** panel from the failed run.
