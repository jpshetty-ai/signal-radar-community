"""Run the jobs on this Mac with launchd (ADR-0014). One LaunchAgent per job, in the user's login session,
so the keychain (mail password, Cloudflare token) and the local Ollama are available.

launchd runs a calendar job that was missed while the Mac slept at the next wake, but not one missed while it
was shut down. So jobs also run at login (RunAtLoad) with `--catch-up`, which does nothing unless the last
successful run is older than CATCH_UP_HOURS: "daily" means once a day whenever the Mac is on. Add a job (e.g. the
weekly brief) by adding a line to JOBS.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import DATA_DIR, PROJECT_ROOT

LABEL = "io.signal-radar.{job}"
AGENTS = Path.home() / "Library" / "LaunchAgents"

# job -> (signal-radar arguments, default calendar: launchd StartCalendarInterval keys)
JOBS: dict[str, tuple[list[str], dict[str, int]]] = {
    "daily": (["daily", "--catch-up"], {"Hour": 7, "Minute": 30}),
}
CATCH_UP_HOURS = 20


def _stamp(job: str) -> Path:
    return DATA_DIR / "logs" / f"{job}.last_success"


def mark_success(job: str) -> None:
    from datetime import UTC, datetime

    _stamp(job).parent.mkdir(parents=True, exist_ok=True)
    _stamp(job).write_text(datetime.now(UTC).isoformat(timespec="seconds"))


def hours_since_success(job: str) -> float | None:
    from datetime import UTC, datetime

    try:
        last = datetime.fromisoformat(_stamp(job).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return (datetime.now(UTC) - last).total_seconds() / 3600


def _executable() -> str:
    exe = Path(sys.argv[0]).resolve() if Path(sys.argv[0]).name == "signal-radar" else None
    found = exe if exe and exe.exists() else shutil.which("signal-radar")
    if not found:
        raise RuntimeError("signal-radar is not on PATH; install the package (pip install -e .) first")
    return str(found)


def _path_env() -> str:
    """launchd starts with a bare PATH: add where npx (wrangler) and Homebrew tools live."""
    dirs = [str(Path(p).parent) for p in (shutil.which("npx"), shutil.which("ollama")) if p]
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(dirs))


def plist(job: str, at: dict[str, int] | None = None, exe: str | None = None) -> dict:
    args, default_at = JOBS[job]
    logs = DATA_DIR / "logs"
    return {
        "Label": LABEL.format(job=job),
        "ProgramArguments": [exe or _executable(), *args],
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": {"PATH": _path_env(), "SIGNAL_RADAR_HOME": str(PROJECT_ROOT)},
        "StartCalendarInterval": at or default_at,
        "RunAtLoad": True,   # at login, so a run missed while the Mac was off catches up (--catch-up decides)
        "StandardOutPath": str(logs / f"{job}.log"),
        "StandardErrorPath": str(logs / f"{job}.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(job: str, at: dict[str, int] | None = None) -> dict:
    spec = plist(job, at)
    path = AGENTS / f"{spec['Label']}.plist"
    AGENTS.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{spec['Label']}")  # replace a previous version; fails harmlessly if none
    path.write_bytes(plistlib.dumps(spec))
    r = _launchctl("bootstrap", domain, str(path))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip()}")
    return {"job": job, "plist": str(path), "runs": spec["ProgramArguments"], "at": spec["StartCalendarInterval"],
            "log": spec["StandardOutPath"]}


def uninstall(job: str) -> dict:
    label = LABEL.format(job=job)
    _launchctl("bootout", f"gui/{os.getuid()}/{label}")
    path = AGENTS / f"{label}.plist"
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"job": job, "removed": existed}


def status() -> list[dict]:
    out = []
    for job in JOBS:
        label = LABEL.format(job=job)
        path = AGENTS / f"{label}.plist"
        r = _launchctl("print", f"gui/{os.getuid()}/{label}")
        last = next((ln.split("=", 1)[1].strip() for ln in r.stdout.splitlines() if "last exit code" in ln), None)
        out.append({"job": job, "installed": path.exists(), "loaded": r.returncode == 0, "last_exit": last,
                    "at": plistlib.loads(path.read_bytes())["StartCalendarInterval"] if path.exists() else None})
    return out


def run_now(job: str) -> dict:
    r = _launchctl("kickstart", f"gui/{os.getuid()}/{LABEL.format(job=job)}")
    return {"job": job, "started": r.returncode == 0, "error": r.stderr.strip() or None}
