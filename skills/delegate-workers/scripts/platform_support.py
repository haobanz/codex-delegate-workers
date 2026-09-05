"""Operating-system details for the local installer and command launchers."""

import contextlib
import ntpath
import os
import sys
from pathlib import Path

if os.name == "nt":
    import msvcrt
    import winreg
else:
    import fcntl


WINDOWS = os.name == "nt"


def configure_console():
    if WINDOWS:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


@contextlib.contextmanager
def file_lock(path):
    with path.open("a+b") as stream:
        if WINDOWS:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError("Another installer is running") from exc
        else:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if WINDOWS:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def default_bin():
    if WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return local / "Programs/DelegateWorkers/bin"
    return Path.home() / ".local/bin"


def windows_batch_launcher(*, legacy=False):
    if not legacy:
        # End batch processing before dispatch, as used by npm/cmd-shim:
        # https://github.com/npm/cmd-shim/blob/main/lib/index.js
        return (
            "@echo off\r\n"
            "rem Managed by Delegate Workers\r\n"
            "setlocal DisableDelayedExpansion\r\n"
            'set "delegatePython=python"\r\n'
            'set "delegatePrefix="\r\n'
            'py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1\r\n'
            "if errorlevel 1 goto dispatch\r\n"
            'set "delegatePython=py"\r\n'
            'set "delegatePrefix=-3"\r\n'
            ":dispatch\r\n"
            "endlocal & goto _delegate_workers_undefined_ 2>nul || title %COMSPEC% & "
            '"%delegatePython%" %delegatePrefix% -X utf8 "%~dp0delegate-workers-entry.py" %*\r\n'
        ).encode("ascii")
    ending = "\r\nexit /b %errorlevel%\r\n"
    return (
        "@echo off\r\n"
        "rem Managed by Delegate Workers\r\n"
        "setlocal DisableDelayedExpansion\r\n"
        'py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1\r\n'
        "if errorlevel 1 goto python\r\n"
        'py -3 -X utf8 "%~dp0delegate-workers-entry.py" %*' + ending +
        ":python\r\n"
        'python -X utf8 "%~dp0delegate-workers-entry.py" %*' + ending
    ).encode("ascii")


def windows_python_launcher(script, codex_home):
    return (
        "# Managed by Delegate Workers\n"
        "import runpy\nimport sys\nfrom pathlib import Path\n"
        'if sys.version_info < (3, 10):\n    raise SystemExit("Python 3.10+ is required")\n'
        f"script = {ascii(str(script))}\n"
        "sys.path.insert(0, str(Path(script).parent))\n"
        f"sys.argv = [script, '--codex-home', {ascii(str(codex_home))}, *sys.argv[1:]]\n"
        "runpy.run_path(script, run_name='__main__')\n"
    ).encode("ascii")


def normalized_path(value):
    return ntpath.normcase(ntpath.normpath(ntpath.expandvars(value.strip().strip('"'))))


def update_path(value, directory, *, remove=False):
    parts = value.split(";") if value else []
    wanted = normalized_path(str(directory))
    matches = [normalized_path(part) == wanted for part in parts]
    if remove:
        return ";".join(part for part, match in zip(parts, matches) if not match)
    return value if any(matches) else ";".join([*parts, str(directory)])


def read_user_path():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        return None


def write_user_path(state):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        if state is None:
            try:
                winreg.DeleteValue(key, "Path")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, "Path", 0, state[1], state[0])
    # Let Explorer refresh the environment inherited by new terminal windows.
    try:
        import ctypes
        from ctypes import wintypes
        broadcast = ctypes.windll.user32.SendMessageTimeoutW
        broadcast.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                              wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        environment = ctypes.create_unicode_buffer("Environment")
        broadcast(0xFFFF, 0x001A, 0, ctypes.cast(environment, ctypes.c_void_p).value, 2, 1000, None)
    except Exception:
        pass
