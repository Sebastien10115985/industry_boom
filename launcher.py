"""Windows 可执行程序的启动入口：启动本地 Streamlit 服务并打开浏览器。"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _pick_port() -> int:
    preferred = int(os.environ.get("INDUSTRY_BOOM_PORT", "8501"))
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


BASE_DIR = _base_dir()
APP_PATH = BASE_DIR / "app.py"
PORT = str(_pick_port())
URL = f"http://localhost:{PORT}"


def _open_browser_when_ready() -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{URL}/_stcore/health", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(URL)
                    return
        except Exception:
            pass
        time.sleep(1)


def main() -> None:
    if not APP_PATH.exists():
        raise SystemExit(f"找不到应用入口：{APP_PATH}")
    os.chdir(BASE_DIR)
    print(f"IndustryBoom 启动中：{URL}", flush=True)
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()

    from streamlit.web.cli import main as streamlit_main

    sys.argv = [
        "streamlit",
        "run",
        str(APP_PATH),
        f"--server.port={PORT}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
        "--theme.base=light",
    ]
    streamlit_main()


if __name__ == "__main__":
    main()
