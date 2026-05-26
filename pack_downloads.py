from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path


def resolve_source_dir(source_dir: Path) -> Path | None:
    if not source_dir.is_dir():
        return None

    accepted = source_dir / "accepted"
    if accepted.is_dir():
        return accepted

    files = [p for p in source_dir.iterdir() if p.is_file()]
    if files:
        return source_dir

    run_dirs = [p for p in source_dir.iterdir() if p.is_dir()]
    if not run_dirs:
        return source_dir

    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for run_dir in run_dirs:
        accepted = run_dir / "accepted"
        if accepted.is_dir() and any(p.is_file() for p in accepted.iterdir()):
            return accepted
        run_files = [p for p in run_dir.iterdir() if p.is_file() and p.suffix.lower() != ".log"]
        if run_files:
            return run_dir
    return run_dirs[0]


def build_zip(source_dir: Path, output_zip: Path) -> int:
    resolved_dir = resolve_source_dir(source_dir)
    if resolved_dir is None:
        print(f"Папка не найдена: {source_dir}", file=sys.stderr)
        return 1

    files = sorted(
        p
        for p in resolved_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() not in {".log", ".json", ".csv", ".txt"}
    )
    if not files:
        print(f"В папке нет файлов для архивации: {resolved_dir}", file=sys.stderr)
        return 2

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, arcname=path.name)

    print(f"ZIP создан: {output_zip}")
    print(f"Источник: {resolved_dir}")
    print(f"Файлов в архиве: {len(files)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Упаковать уже скачанные треки в ZIP."
    )
    parser.add_argument(
        "--source",
        default="downloaded_music",
        help="Папка с уже скачанными файлами",
    )
    parser.add_argument(
        "--output",
        default="playlist_partial.zip",
        help="Имя итогового zip-файла",
    )
    args = parser.parse_args()

    return build_zip(Path(args.source), Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
