#!/usr/bin/env python3
"""Repair mlx_audio_io's RECORD hash inside a built .app bundle.

mlx_audio_io ships a runtime self-check (`_native_loader.verify_record_hash`)
that hashes its own `_core.*.so` and compares it to the sha256 recorded in the
wheel's dist-info/RECORD, to catch a `.venv` copied across incompatible
machines. PyInstaller legitimately rewrites that binary (thinning to a single
arch, re-signing), so its hash no longer matches RECORD and the guard refuses
to load. We ship a coherent set (the check's real concern — ABI/arch/mlx
version — is still enforced by verify_compatibility), so recompute the RECORD
entry to match the binary as bundled.

Run AFTER the binary's final (ad-hoc) signature is applied, then re-seal the
app bundle without `--deep` so the .so bytes stay put. See build_bundle.sh.
"""
import base64
import hashlib
import sys
from pathlib import Path


def record_hash(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def real_match(app: Path, pattern: str) -> Path | None:
    for p in app.rglob(pattern):
        if not p.is_symlink() and p.is_file():
            return p
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: bundle_fix_record.py <path-to.app>", file=sys.stderr)
        return 2
    app = Path(sys.argv[1])

    core = real_match(app, "mlx_audio_io/_core*.so")
    record = real_match(app, "mlx_audio_io-*.dist-info/RECORD")
    if core is None or record is None:
        print(f"ERROR: could not find _core.so ({core}) or RECORD ({record}) in {app}",
              file=sys.stderr)
        return 1

    rel = f"mlx_audio_io/{core.name}"
    new_hash = record_hash(core)
    size = core.stat().st_size

    lines = record.read_text().splitlines()
    patched = False
    for i, line in enumerate(lines):
        if line.split(",", 1)[0] == rel:
            lines[i] = f"{rel},{new_hash},{size}"
            patched = True
            break
    if not patched:
        print(f"ERROR: {rel} not present in RECORD; nothing patched", file=sys.stderr)
        return 1

    record.write_text("\n".join(lines) + "\n")
    print(f"Patched RECORD for {rel}: {new_hash} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
