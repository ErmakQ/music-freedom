"""Build a distributable Windows EXE package without a user-facing batch launcher."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
RELEASE = ROOT / "release"
APP_NAME = "MusicShelf"


def run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd or ROOT, check=True)


def find_inno_compiler() -> str | None:
    candidates = [
        shutil.which("iscc"),
        str(Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"),
        r"C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe",
        r"C:\\Program Files\\Inno Setup 6\\ISCC.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def main() -> None:
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("Node.js/npm is required to build frontend assets.")

    run(npm, "ci", cwd=FRONTEND)
    run(npm, "run", "build", cwd=FRONTEND)
    run(sys.executable, "-m", "pip", "install", "-r", "requirements-build.txt")

    shutil.rmtree(BUILD, ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)
    RELEASE.mkdir(exist_ok=True)

    data_separator = ";" if sys.platform == "win32" else ":"
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--icon",
        "assets/music-shelf.ico",
        "--add-data",
        f"frontend/dist{data_separator}frontend/dist",
        "--add-data",
        f"assets{data_separator}assets",
        "--collect-all",
        "pystray",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "desktop.py",
    )
    shutil.copy2(ROOT / "LICENSE", DIST / APP_NAME / "LICENSE")

    archive_base = RELEASE / "music-shelf-windows"
    zip_path = archive_base.with_suffix(".zip")
    zip_path.unlink(missing_ok=True)
    shutil.make_archive(str(archive_base), "zip", DIST, APP_NAME)
    print(f"Portable EXE package: {zip_path}")
    print(f"Executable: {DIST / APP_NAME / (APP_NAME + '.exe')}")

    compiler = find_inno_compiler()
    if compiler:
        run(compiler, "installer.iss")
        print(f"Installer: {RELEASE / 'music-shelf-setup.exe'}")
    else:
        print("Inno Setup was not found; portable EXE package was created without an installer.")


if __name__ == "__main__":
    main()
