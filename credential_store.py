import ctypes
import os
from ctypes import wintypes
from pathlib import Path


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _require_windows():
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is available only on Windows")


def _blob_from_bytes(data: bytes):
    if not data:
        data = b"\0"
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def protect_bytes(data: bytes) -> bytes:
    _require_windows()
    in_blob, _buffer = _blob_from_bytes(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def unprotect_bytes(data: bytes) -> bytes:
    _require_windows()
    in_blob, _buffer = _blob_from_bytes(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def save_secret(path: Path, secret: str):
    path = Path(path)
    secret = secret or ""
    if not secret:
        delete_secret(path)
        return
    protected = protect_bytes(secret.encode("utf-8"))
    path.write_bytes(protected)


def load_secret(path: Path) -> str:
    path = Path(path)
    if not path.exists():
        return ""
    try:
        protected = path.read_bytes()
        return unprotect_bytes(protected).decode("utf-8")
    except Exception:
        return ""


def delete_secret(path: Path):
    path = Path(path)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def secret_exists(path: Path) -> bool:
    return Path(path).exists()
