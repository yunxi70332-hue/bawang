from __future__ import annotations

import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "tools" / "blutter" / "external"
BIN = ROOT / "tools" / "blutter" / "bin"
EXT.mkdir(parents=True, exist_ok=True)
BIN.mkdir(parents=True, exist_ok=True)


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def prepare_icu() -> None:
    archive = EXT / "icu4c.zip"
    if not archive.is_file():
        raise FileNotFoundError(archive)
    outer = EXT / "_icu_outer"
    reset_dir(outer)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(outer)
    inner = next(outer.rglob("icu-windows.zip"), None)
    if inner is None:
        raise RuntimeError("icu-windows.zip not found inside downloaded archive")
    out = EXT / "icu-windows"
    reset_dir(out)
    with zipfile.ZipFile(inner) as zf:
        zf.extractall(out)
    shutil.rmtree(outer, ignore_errors=True)


def prepare_capstone() -> None:
    archive = EXT / "capstone.zip"
    if not archive.is_file():
        raise FileNotFoundError(archive)
    outer = EXT / "_cap_outer"
    reset_dir(outer)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(outer)
    roots = [p for p in outer.iterdir() if p.is_dir()]
    if not roots:
        raise RuntimeError("capstone archive has no root directory")
    out = EXT / "capstone"
    if out.exists():
        shutil.rmtree(out)
    shutil.move(str(roots[0]), out)
    shutil.rmtree(outer, ignore_errors=True)


def copy_runtime_dlls() -> None:
    cap_dll = EXT / "capstone" / "capstone.dll"
    icu_root = EXT / "icu-windows"
    candidates = list(icu_root.rglob("icudt73.dll")) + list(icu_root.rglob("icuuc73.dll"))
    if not cap_dll.is_file() or len(candidates) < 2:
        raise RuntimeError("runtime DLLs not found after extraction")
    shutil.copy2(cap_dll, BIN / "capstone.dll")
    for name in ("icudt73.dll", "icuuc73.dll"):
        src = next(icu_root.rglob(name))
        shutil.copy2(src, BIN / name)


if __name__ == "__main__":
    prepare_icu()
    prepare_capstone()
    copy_runtime_dlls()
    print(f"prepared {EXT}")
