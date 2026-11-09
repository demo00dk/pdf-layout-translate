"""Where pdf-layout-translate keeps its runtime.  Standard library only: this module is imported
before the runtime (venv, models, fonts) exists.

Runtime folder (venv + BabelDOC model/fonts + OCR models + generated fonts + uv cache):
    $LT_HOME  >  "home" in <skill>/config.local.json  >  per-user data folder
        Windows  %LOCALAPPDATA%\\pdf-layout-translate
        macOS    ~/Library/Application Support/pdf-layout-translate
        Linux    $XDG_DATA_HOME/pdf-layout-translate  (~/.local/share/...)
Jobs folder: $LT_JOBS  >  "jobs" in config.local.json  >  <runtime>/jobs
Relative paths in config.local.json are relative to the skill folder.

The outermost process resolves everything once; runtime processes run with the user profile
pointed at <runtime>/profile (where BabelDOC caches its assets), so they must not recompute
paths from the home folder: runtime_env() passes the resolved values down.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP = "pdf-layout-translate"
SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
IS_WIN = sys.platform == "win32"
REAL_HOME = Path(os.environ.get("LT_REAL_HOME") or Path.home())  # the user's home, also in runtime processes


def _local_config() -> dict:
    p = SKILL / "config.local.json"
    try:
        return json.loads(p.read_text("utf-8-sig")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def _expand(value: str, base: Path) -> Path:
    s = str(value)
    if s == "~" or s.startswith(("~/", "~\\")):
        s = str(REAL_HOME) + s[1:]
    p = Path(os.path.expandvars(s))
    return (p if p.is_absolute() else base / p).resolve()


def _default_home() -> Path:
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or str(REAL_HOME / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(REAL_HOME / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(REAL_HOME / ".local" / "share")
    return Path(base) / APP


CONFIG = _local_config()
if os.environ.get("LT_HOME"):
    HOME = _expand(os.environ["LT_HOME"], Path.cwd())
elif CONFIG.get("home"):
    HOME = _expand(CONFIG["home"], SKILL)
else:
    HOME = _default_home().resolve()
VENV = HOME / "venv"
VENV_PY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
PROFILE = HOME / "profile"       # used as the user profile dir: BabelDOC caches under ~/.cache/babeldoc
BABELDOC_FONTS = PROFILE / ".cache" / "babeldoc" / "fonts"
FONTS = HOME / "fonts"           # math fonts built locally (never redistributed)
MODELS = HOME / "models"         # RapidOCR models
TOOLS = HOME / "tools"           # uv, downloaded when not installed
UV_CACHE = HOME / "uv-cache"
UV_PYTHONS = HOME / "uv-python"
READY = HOME / "ready.json"
if os.environ.get("LT_JOBS"):
    JOBS = _expand(os.environ["LT_JOBS"], Path.cwd())
elif CONFIG.get("jobs"):
    JOBS = _expand(CONFIG["jobs"], SKILL)
else:
    JOBS = HOME / "jobs"

# variables of the user's own Python setup that must not leak into the runtime's interpreter
_PY_LEAKS = ("PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "PYTHONSTARTUP", "PYTHONUSERBASE",
             "PYTHONNOUSERSITE", "VIRTUAL_ENV", "CONDA_PREFIX", "__PYVENV_LAUNCHER__")


def runtime_env(base: dict | None = None) -> dict:
    """Environment for runtime processes: resolved locations passed down explicitly, the user
    profile pointed at <runtime>/profile (BabelDOC's asset cache), foreign Python settings removed."""
    env = dict(os.environ if base is None else base)
    for k in _PY_LEAKS:
        env.pop(k, None)
    env["LT_REAL_HOME"] = str(REAL_HOME)
    env["LT_HOME"] = str(HOME)
    env["LT_JOBS"] = str(JOBS)
    env["USERPROFILE" if IS_WIN else "HOME"] = str(PROFILE)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _venv_base_python() -> Path | None:
    """Windows: the interpreter behind the venv's Scripts\\python.exe redirector (pyvenv.cfg "home")."""
    try:
        lines = (VENV / "pyvenv.cfg").read_text("utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip().lower() == "home":
            exe = Path(value.strip()) / "python.exe"
            # Store Python cannot be started from its WindowsApps folder
            return exe if exe.is_file() and "WindowsApps" not in exe.parts else None
    return None


def runtime_command(args: list, base: dict | None = None) -> tuple[list[str], dict]:
    """(command, environment) that runs the runtime's interpreter with `args`.

    On Windows a venv's python.exe is a small unsigned redirector that starts the real interpreter.
    Security suites (360, Tencent PC Manager, ...) kill such unknown redirectors together with their
    child - silently, exit code 0 - when the child rewrites PDF files (a ransomware heuristic).  So the
    real interpreter is started directly and pointed at the venv, which is what multiprocessing
    itself does in venvs (__PYVENV_LAUNCHER__, bpo-35797; Python removes the variable at startup)."""
    env = runtime_env(base)
    exe = str(VENV_PY)
    if IS_WIN and os.environ.get("LT_USE_VENV_LAUNCHER") != "1":
        real = _venv_base_python()
        if real:
            exe = str(real)
            env["__PYVENV_LAUNCHER__"] = str(VENV_PY)
    return [exe, *[str(a) for a in args]], env


def pid_alive(pid: int) -> bool:
    """Is process `pid` still running?  (lock files of killed runs are taken over at once)"""
    if pid <= 0:
        return False
    if IS_WIN:  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: exists, owned by someone else
        try:
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle.argtypes = (ctypes.c_void_p,)
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_owner_gone(lock: Path) -> bool:
    """The process that wrote `lock` (its pid is the file content) no longer runs."""
    try:
        text = lock.read_text("ascii").strip()
        return bool(text) and not pid_alive(int(text))  # empty: being written right now
    except (OSError, ValueError):
        return False


def uv_env(base: dict | None = None) -> dict:
    """Environment for uv: cache and managed Pythons inside the runtime folder (self-contained,
    removable), and no uv.toml / pyproject settings from whatever folder the agent runs in."""
    env = dict(os.environ if base is None else base)
    for k in _PY_LEAKS:
        env.pop(k, None)
    env["UV_CACHE_DIR"] = str(UV_CACHE)
    env["UV_PYTHON_INSTALL_DIR"] = str(UV_PYTHONS)
    env["UV_NO_CONFIG"] = "1"
    return env
