"""Create or repair the private runtime.  Standard library only (runs before the venv exists).

Steps (each is skipped when already done):
  1. uv            - use the one on PATH, else download the pinned official build (sha256-checked)
  2. venv          - Python 3.12 (uv fetches it when the system has no suitable one)
  3. packages      - requirements.txt
  4. BabelDOC      - layout model + fonts (~300 MB, from Hugging Face / ModelScope)
  5. fonts         - math fonts for formula-preserving rewrites (optional: failure only warns)
  6. OCR models    - RapidOCR models for the common languages (optional: retried on first use)
uv's cache and any Python it downloads live inside the runtime folder, so deleting that folder
uninstalls everything.  Total disk use is about 1.5 GB.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

import lt_paths as P

PY_VERSION = "3.12"
SETUP_VERSION = 3  # bump when requirements / assets change: older runtimes are set up again
UV_VERSION = "0.12.3"
UV_URL = "https://github.com/astral-sh/uv/releases/download/{v}/{asset}"
UV_SHA256 = {
    "uv-x86_64-pc-windows-msvc.zip": "b23350c79e8ad0192b8124af13a0f17e8d4e4549524785e1aef389ae5a06990e",
    "uv-aarch64-pc-windows-msvc.zip": "4343217d668727b8a8eb5cad92389a1d2eeead93c89940d1b955ba1bb15462eb",
    "uv-x86_64-unknown-linux-gnu.tar.gz": "600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101",
    "uv-aarch64-unknown-linux-gnu.tar.gz": "bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2",
    "uv-x86_64-unknown-linux-musl.tar.gz": "0643b9fb8c9fb27458e709ce6ff939695013c41975ff7b02d3f3b138d8d4bdb3",
    "uv-aarch64-unknown-linux-musl.tar.gz": "fa513fca1eb2913334c944fe9adbdd410274a1cbe8dd05d03699a9eb85311d4e",
    "uv-aarch64-apple-darwin.tar.gz": "546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843",
    "uv-x86_64-apple-darwin.tar.gz": "4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b",
}
LOCK = P.HOME / "setup.lock"


def log(msg: str):
    print(f"[setup] {msg}", flush=True)


def run(cmd: list, env: dict | None = None, **kw):
    log("$ " + " ".join(str(c) for c in cmd))
    kw.setdefault("cwd", str(P.HOME))  # never pick up the project folder's uv.toml / pyproject
    subprocess.run([str(c) for c in cmd], check=True, env=env, **kw)


def is_ready() -> bool:
    try:
        info = json.loads(P.READY.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return int(info.get("version", 0)) >= SETUP_VERSION and P.VENV_PY.exists()


# --------------------------------------------------------------------------- lock
class SetupLock:
    """One setup at a time per runtime folder (an agent may retry while the first one still runs)."""

    def __enter__(self):
        P.HOME.mkdir(parents=True, exist_ok=True)
        waited = 0
        while True:
            try:
                fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - LOCK.stat().st_mtime
                except OSError:
                    continue
                if age > 3 * 3600 or (age > 2 and P.lock_owner_gone(LOCK)):  # left by a killed run
                    LOCK.unlink(missing_ok=True)
                    continue
                if waited == 0:
                    log(f"another setup is running ({LOCK}); waiting for it ...")
                time.sleep(5)
                waited += 5
                if waited > 3600:
                    raise SystemExit(f"setup lock still held after 1 h: {LOCK} (delete it if no setup is running)")

    def __exit__(self, *exc):
        LOCK.unlink(missing_ok=True)


# --------------------------------------------------------------------------- uv
def _uv_asset() -> str:
    m = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(m)
    if arch is None:
        raise RuntimeError(f"no prebuilt uv for CPU '{m}'")
    if P.IS_WIN:
        return f"uv-{arch}-pc-windows-msvc.zip"
    if sys.platform == "darwin":
        return f"uv-{arch}-apple-darwin.tar.gz"
    libc = platform.libc_ver()[0]
    return f"uv-{arch}-unknown-linux-{'gnu' if libc == 'glibc' else 'musl'}.tar.gz"


def find_uv(download: bool = True) -> str | None:
    exe = "uv.exe" if P.IS_WIN else "uv"
    local = P.TOOLS / exe
    if local.exists():
        return str(local)
    found = shutil.which("uv")
    if found:
        return found
    if not download:
        return None
    asset = _uv_asset()
    url = UV_URL.format(v=UV_VERSION, asset=asset)
    log(f"uv not found - downloading uv {UV_VERSION}: {url}")
    data = urllib.request.urlopen(url, timeout=300).read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != UV_SHA256[asset]:
        raise RuntimeError(f"uv download checksum mismatch ({digest})")
    P.TOOLS.mkdir(parents=True, exist_ok=True)
    tmp = local.with_suffix(".part")
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next(n for n in z.namelist() if n.split("/")[-1] == "uv.exe")
            tmp.write_bytes(z.read(name))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = next(m for m in t.getmembers() if m.name.split("/")[-1] == "uv")
            tmp.write_bytes(t.extractfile(member).read())
        tmp.chmod(0o755)
    os.replace(tmp, local)
    return str(local)


# --------------------------------------------------------------------------- steps
def ensure_venv(uv: str | None, force: bool):
    if force and P.VENV.exists():
        log(f"--force: removing {P.VENV}")
        shutil.rmtree(P.VENV)
    if P.VENV_PY.exists():
        return
    py = PY_VERSION
    if P.IS_WIN and platform.machine().lower() in ("arm64", "aarch64"):
        py = f"cpython-{PY_VERSION}-windows-x86_64-none"  # BabelDOC has no win-arm64 wheel: x64 emulation
    if uv:
        try:  # a uv-managed CPython kept inside the runtime: same interpreter on every machine
            run([uv, "venv", "--python", py, "--python-preference", "only-managed", P.VENV], env=P.uv_env())
            return
        except subprocess.CalledProcessError:
            log("could not get a managed Python; trying any Python 3.12 on this system")
            shutil.rmtree(P.VENV, ignore_errors=True)
            run([uv, "venv", "--python", py, P.VENV], env=P.uv_env())
            return
    if (3, 10) <= sys.version_info[:2] <= (3, 13):
        run([sys.executable, "-m", "venv", P.VENV])
    else:
        raise SystemExit("need uv or Python 3.10-3.13 to build the runtime; install uv: https://docs.astral.sh/uv/")


def install_packages(uv: str | None):
    req = P.SCRIPTS / "requirements.txt"
    if uv:
        run([uv, "pip", "install", "--python", P.VENV_PY, "-r", req], env=P.uv_env())
    else:
        run(*P.runtime_command(["-m", "pip", "install", "--upgrade", "pip"]))
        run(*P.runtime_command(["-m", "pip", "install", "-r", req]))


def in_runtime(script: str, *args, required: bool = True) -> bool:
    try:
        run(*P.runtime_command(["-s", "-X", "utf8", P.SCRIPTS / script, *args]))
        return True
    except subprocess.CalledProcessError:
        if required:
            raise
        return False


def setup(force: bool = False):
    if is_ready() and not force:
        log(f"runtime already set up: {P.HOME}")
        return
    with SetupLock():
        if is_ready() and not force:  # finished by the run we waited for
            log(f"runtime already set up: {P.HOME}")
            return
        log(f"runtime folder: {P.HOME}  (about 1.5 GB; change it with LT_HOME)")
        P.PROFILE.mkdir(parents=True, exist_ok=True)
        try:
            uv = find_uv()
        except Exception as e:  # noqa: BLE001 - offline / unsupported CPU: fall back to venv + pip
            log(f"could not get uv ({e}); trying the built-in venv module")
            uv = None
        ensure_venv(uv, force)
        install_packages(uv)
        log("BabelDOC layout model and fonts (first time ~300 MB)")
        run(*P.runtime_command(["-s", "-X", "utf8", "-c", "from babeldoc.assets.assets import warmup; warmup()"]))
        log("fonts")
        if not in_runtime("fonts.py", "build", required=False):
            log("WARNING: math fonts not built (only affects inline formulas of Keynote PDFs); "
                "`lt.py setup --force` retries")
        log("OCR models")
        if not in_runtime("image_translate.py", "warmup", "--lang", "en,ko", required=False):
            log("WARNING: OCR models not downloaded yet; they are fetched again on first use")
        P.READY.write_text(json.dumps({"version": SETUP_VERSION, "python": PY_VERSION, "platform": sys.platform},
                                      indent=1), "utf-8")
        log("done")


if __name__ == "__main__":
    setup(force="--force" in sys.argv)
