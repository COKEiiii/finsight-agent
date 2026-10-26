"""An opt-in macOS collector job. It never runs a model or submits trades."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.finsight.daily"


def job_definition(data_dir: Path, python: str | None = None) -> dict:
    package_source = Path(__file__).resolve().parents[1]
    root = Path.cwd().resolve()
    return {
        "Label": LABEL,
        "ProgramArguments": [python or sys.executable, "-m", "finsight", "--data-dir", str(data_dir.resolve()), "watch", "--once"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PYTHONPATH": str(package_source)},
        "RunAtLoad": True,
        "StartCalendarInterval": {"Hour": 18, "Minute": 5},
        "StandardOutPath": str(data_dir.resolve() / "scheduler.log"),
        "StandardErrorPath": str(data_dir.resolve() / "scheduler.log"),
    }


def install(data_dir: Path) -> dict:
    if sys.platform != "darwin":
        raise ValueError("automatic schedule installation supports macOS; use watch --once with your system scheduler")
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    folder = Path.home() / "Library/LaunchAgents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{LABEL}.plist"
    old = path.read_bytes() if path.exists() else None
    definition = job_definition(data_dir)
    if old and plistlib.loads(old).get("WorkingDirectory") != definition["WorkingDirectory"]:
        raise ValueError("a FinSight schedule for a different checkout already exists; inspect it before replacing it")
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    loaded = subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
    if loaded:
        subprocess.run(["launchctl", "bootout", target], check=True, capture_output=True)
    path.write_bytes(plistlib.dumps(definition))
    path.chmod(0o600)
    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        if old:
            path.write_bytes(old)
            if loaded:
                subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True)
        else:
            path.unlink(missing_ok=True)
        raise ValueError("launchd could not start the collector; previous schedule was restored") from exc
    return {"status": "installed", "label": LABEL, "plist": str(path), "time": "18:05 system local time",
            "log": definition["StandardOutPath"], "model_calls": False,
            "note": "The collector also checks at login; missed sessions retain their data availability gaps."}


def status() -> dict:
    if sys.platform != "darwin":
        return {"status": "unsupported_platform"}
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    return {"status": "loaded" if result.returncode == 0 else "not_loaded", "label": LABEL}
