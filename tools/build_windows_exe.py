"""把 IndustryBoom 打包为 Windows 可执行程序（onedir）。

用法（在虚拟环境内）：
    python tools/build_windows_exe.py

产物：dist/IndustryBoom/IndustryBoom.exe
同目录会放入 app.py、各业务模块及 catalog/、data/、output/ 数据目录，
程序运行时写入的数据保存在该目录内，不会因重启丢失。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "IndustryBoom"
DIST_DIR = ROOT / "dist" / APP_NAME


def build_bundle() -> None:
    args = [
        str(ROOT / "launcher.py"),
        f"--name={APP_NAME}",
        "--onedir",
        "--noconfirm",
        "--clean",
        "--collect-all=streamlit",
        "--collect-all=altair",
        "--collect-binaries=pyarrow",
        "--collect-data=pyarrow",
        "--hidden-import=streamlit.web.cli",
        f"--distpath={ROOT / 'dist'}",
        f"--workpath={ROOT / 'build'}",
        f"--specpath={ROOT / 'build'}",
    ]
    PyInstaller.__main__.run(args)


def copy_runtime_files() -> None:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    for source in sorted(ROOT.glob("*.py")):
        if source.name.startswith("test_") or source.name == "launcher.py":
            continue
        shutil.copy2(source, DIST_DIR / source.name)
    for folder in ("catalog", "data", "output"):
        source = ROOT / folder
        if source.is_dir():
            shutil.copytree(source, DIST_DIR / folder, dirs_exist_ok=True)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("此脚本仅用于在 Windows 上生成 exe")
    build_bundle()
    copy_runtime_files()
    exe = DIST_DIR / f"{APP_NAME}.exe"
    print(f"\n打包完成：{exe}")
    print(f"运行时数据目录：{DIST_DIR / 'data'}")
    print("双击 exe 即可启动；关闭控制台窗口停止服务。")


if __name__ == "__main__":
    main()
