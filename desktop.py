"""Windows EXE entry point for Музыкальная полка."""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


APP_SLUG = "MusicShelf"
APP_TITLE = "Музыкальная полка"


def _resource_path(*parts: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root.joinpath(*parts)


def _data_root() -> Path:
    configured = os.getenv("MUSIC_SHELF_DATA_DIR")
    if configured:
        root = Path(configured).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        return root
    local_app_data = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    root = local_app_data / APP_SLUG
    root.mkdir(parents=True, exist_ok=True)
    return root


def _configure_runtime() -> Path:
    data_root = _data_root()
    (data_root / "logs").mkdir(exist_ok=True)
    os.environ.setdefault("MUSIC_SHELF_DATA_DIR", str(data_root))
    # Relative cache paths used by search providers stay outside the installation folder.
    os.chdir(data_root)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(data_root / "logs" / "music-shelf.log", encoding="utf-8")],
    )
    return data_root


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _open_when_ready(server, url: str) -> None:
    for _ in range(150):
        if server.started:
            webbrowser.open(url)
            return
        if server.should_exit:
            return
        time.sleep(0.1)


def _run_tray(server, url: str) -> object | None:
    try:
        from PIL import Image
        import pystray

        image = Image.open(_resource_path("assets", "music-shelf-avatar.png"))

        def open_app(_icon, _item) -> None:
            webbrowser.open(url)

        def exit_app(icon, _item) -> None:
            server.should_exit = True
            icon.stop()

        icon = pystray.Icon(
            APP_SLUG,
            image,
            APP_TITLE,
            menu=pystray.Menu(
                pystray.MenuItem("Открыть Музыкальную полку", open_app),
                pystray.MenuItem("Выйти", exit_app),
            ),
        )
        icon.run_detached()
        return icon
    except Exception:
        logging.exception("Unable to start system tray icon")
        return None


def main() -> None:
    _configure_runtime()

    import uvicorn
    from web.app import app

    port = _free_local_port()
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=_open_when_ready, args=(server, url), daemon=True).start()
    tray = _run_tray(server, url)
    try:
        server.run()
    finally:
        if tray is not None:
            tray.stop()


if __name__ == "__main__":
    main()
