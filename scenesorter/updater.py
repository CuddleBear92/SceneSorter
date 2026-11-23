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


def build_update_script(pid: int, new_exe: Path, current: Path, log: Path) -> str:
    """Windows batch script that swaps in the new exe once we've exited.

    Notes on the gotchas this handles:
    * A PyInstaller one-file exe runs as two processes (bootloader + app). We
      only know the app's PID, and the bootloader keeps SceneSorter.exe locked
      for a moment after the app exits while it cleans up — so the move is
      retried for up to a minute instead of attempted once.
    * ``timeout`` fails instantly without a console (we run hidden), so sleeps
      use ``ping -n 2 127.0.0.1`` (~1 s).
    * Every step is logged to data/update.log.
    """
    exe_name = current.name
    lines = [
        "@echo off",
        "chcp 65001 >NUL",
        "setlocal",
        f'set "PID={pid}"',
        f'set "NEW={new_exe}"',
        f'set "CUR={current}"',
        f'set "LOG={log}"',
        "set WAITS=0",
        "set TRIES=0",
        'echo [%date% %time%] update: waiting for PID %PID% to exit>>"%LOG%"',
        ":wait",
        f'tasklist /FI "PID eq %PID%" /NH 2>NUL | find /I "{exe_name}" >NUL',
        "if errorlevel 1 goto move",
        "set /a WAITS+=1",
        "if %WAITS% GEQ 120 goto move",
        "ping -n 2 127.0.0.1 >NUL",
        "goto wait",
        ":move",
        'move /Y "%NEW%" "%CUR%" >NUL 2>&1',
        "if not errorlevel 1 goto moved",
        "set /a TRIES+=1",
        "if %TRIES% GEQ 60 goto failed",
        "ping -n 2 127.0.0.1 >NUL",
        "goto move",
        ":moved",
        'echo [%date% %time%] update: replaced exe (retries: %TRIES%)>>"%LOG%"',
        'start "" "%CUR%"',
        "goto end",
        ":failed",
        'echo [%date% %time%] update: FAILED to replace exe, still locked after 60 tries; starting old exe>>"%LOG%"',
        'start "" "%CUR%"',
        ":end",
        'del "%~f0"',
    ]
    return "\r\n".join(lines) + "\r\n"


def apply_update_and_restart(new_exe: Path) -> None:
    """Replace the running exe after we exit, then relaunch. Windows only."""
    if not (is_frozen() and sys.platform.startswith("win")):
        raise RuntimeError("Self-update is only supported for the packaged Windows build.")
    from .config import DATA_DIR

    current = Path(sys.executable)
    script = Path(tempfile.gettempdir()) / f"scenesorter_update_{os.getpid()}.cmd"
    script.write_text(build_update_script(os.getpid(), new_exe, current, DATA_DIR / "update.log"),
                      encoding="utf-8")
    # A PyInstaller one-file exe passes its private env vars (_MEIPASS2, _PYI_*) to child
    # processes; the relaunched exe would then look for its DLLs in *our* temp folder, which
    # is deleted when we exit ("Failed to load Python DLL"). Start it with a clean slate.
    env = {k: v for k, v in os.environ.items() if not (k.startswith("_PYI") or k.startswith("_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(["cmd", "/c", str(script)], env=env, creationflags=0x08000000)  # CREATE_NO_WINDOW


def leftover_update() -> tuple[Path, str] | None:
    """A downloaded update that never got installed (e.g. the swap failed).

    Returns (file, version) if it's newer than us; stale/older leftovers are deleted.
    """
    if not is_frozen():
        return None
    best: tuple[Path, str] | None = None
    for f in Path(sys.executable).parent.glob("SceneSorter-*.exe.new"):
        ver = f.name[len("SceneSorter-"):-len(".exe.new")]
        if is_newer(ver) and (best is None or is_newer(ver, best[1])):
            best = (f, ver)
        else:
            try:
                f.unlink()
            except OSError:
                pass
    return best
