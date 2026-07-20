#!/usr/bin/env python3
"""PyInstaller entry point for the self-contained .app.

The bundle has no external Python, so the GUI shells out to *itself* to run the
separation engine. One frozen binary therefore serves two roles:

  * default                     → launch the Tkinter GUI (app.main)
  * --separator-runner <args…>  → run the engine (runner.main), used by the GUI
                                  as a subprocess for both separation runs and
                                  the model-list fetch.
"""
import multiprocessing
import sys


def _main():
    if len(sys.argv) > 1 and sys.argv[1] == "--separator-runner":
        # Present the remaining args as if runner.py was invoked directly.
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        import runner
        sys.exit(runner.main())
    import app
    app.main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _main()
