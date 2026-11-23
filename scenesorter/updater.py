"""Self-update from GitHub Releases.

The release workflow attaches ``SceneSorter.exe`` to each tagged release.
On start (and from Help ▸ Check for updates) the app asks the GitHub API for
the latest release; if it's newer, it downloads the exe next to the running
one and swaps it in with a tiny helper script after the app exits.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from . import __version__

ASSET_NAME = "SceneSorter.exe"


@dataclass
class UpdateInfo:
    version: str
    notes: str
    asset_url: str
    page_url: str


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v.split("+")[0].split("-")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def check_for_update(repo: str, current: str = __version__, timeout: float = 10) -> UpdateInfo | None:
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"SceneSorter/{current}"},
        timeout=timeout,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    tag = data.get("tag_name", "")
    if not is_newer(tag, current):
        return None
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    if not asset:
        return None
    return UpdateInfo(tag.lstrip("v"), data.get("body") or "", asset["browser_download_url"], data.get("html_url", ""))


def download_update(info: UpdateInfo, progress: Callable[[int, int], None] | None = None) -> Path:
    target_dir = Path(sys.executable).parent if is_frozen() else Path(tempfile.gettempdir())
    target = target_dir / f"SceneSorter-{info.version}.exe.new"
    with requests.get(info.asset_url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        done = 0
        with open(target, "wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    return target


def apply_update_and_restart(new_exe: Path) -> None:
    """Replace the running exe after we exit, then relaunch. Windows only."""
    if not (is_frozen() and sys.platform.startswith("win")):
        raise RuntimeError("Self-update is only supported for the packaged Windows build.")
    current = Path(sys.executable)
    script = Path(tempfile.gettempdir()) / "scenesorter_update.cmd"
    script.write_text(
        "@echo off\r\n"
        f"set PID={os.getpid()}\r\n"
        ":wait\r\n"
        'tasklist /FI "PID eq %PID%" 2>NUL | find "%PID%" >NUL && (timeout /t 1 /nobreak >NUL & goto wait)\r\n'
        f'move /Y "{new_exe}" "{current}" >NUL\r\n'
        f'start "" "{current}"\r\n'
        'del "%~f0"\r\n',
        encoding="utf-8",
    )
    # A PyInstaller one-file exe passes its private env vars (_MEIPASS2, _PYI_*) to child
    # processes; the relaunched exe would then look for its DLLs in *our* temp folder, which
    # is deleted when we exit ("Failed to load Python DLL"). Start it with a clean slate.
    env = {k: v for k, v in os.environ.items() if not (k.startswith("_PYI") or k.startswith("_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(["cmd", "/c", str(script)], env=env, creationflags=0x08000000)  # CREATE_NO_WINDOW
