"""Encrypt secrets (cloud API keys, GitHub token, LAN token) at rest.

On Windows the secrets are sealed with DPAPI: only the same Windows user on the same PC can open them, so a copy of
settings.json that leaks (OneDrive, a backup, a screenshot) is useless without the machine. Sealed values look like
"dpapi:<base64>". On other systems there is no OS keystore to lean on, so values are stored as they are.
"""
from __future__ import annotations

import base64
import copy
import ctypes
import sys
from ctypes import wintypes
from typing import Any, Callable

PREFIX = "dpapi:"
_ENTROPY = b"cerberus-settings-v1"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def available() -> bool:
    return sys.platform == "win32"


def _blob(data: bytes) -> tuple[_Blob, Any]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _crypt(data: bytes, encrypt: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    src, keep1 = _blob(data)
    ent, keep2 = _blob(_ENTROPY)
    out = _Blob()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    args = (ctypes.byref(src), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out))
    if not fn(*args):
        raise OSError("DPAPI refused the data (different Windows user or PC?)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)
        del keep1, keep2


def protect(text: str) -> str:
    if not text or not available() or text.startswith(PREFIX):
        return text
    return PREFIX + base64.b64encode(_crypt(text.encode("utf-8"), True)).decode("ascii")


def unprotect(text: str) -> str:
    """Plain values pass through. A sealed value that cannot be opened becomes '' (the secret must be re-entered)."""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return text
    try:
        return _crypt(base64.b64decode(text[len(PREFIX):]), False).decode("utf-8")
    except (OSError, ValueError):
        return ""


def _each_secret(settings: dict, fn: Callable[[dict, str], None]) -> None:
    """Call fn(container, key) for every secret field: api_token, github.token, cloud.providers.*.api_key."""
    fn(settings, "api_token")
    if isinstance(settings.get("github"), dict):
        fn(settings["github"], "token")
    providers = (settings.get("cloud") or {}).get("providers")
    for spec in (providers or {}).values():
        if isinstance(spec, dict):
            fn(spec, "api_key")


def _apply(settings: dict, op: Callable[[str], str]) -> dict:
    out = copy.deepcopy(settings)

    def one(box: dict, key: str) -> None:
        if isinstance(box.get(key), str):
            box[key] = op(box[key])
    _each_secret(out, one)
    return out


def seal(settings: dict) -> dict:
    """Copy of the settings with every secret encrypted, ready to be written to disk."""
    return _apply(settings, protect)


def unseal(settings: dict) -> dict:
    return _apply(settings, unprotect)


def strip(settings: dict) -> dict:
    """Copy with every secret blanked (for exports that leave the machine)."""
    return _apply(settings, lambda _v: "")


def secret_values(settings: dict) -> list[str]:
    """Every secret currently set, in the clear (for scrubbing them out of logs; never store or send this list)."""
    found: list[str] = []

    def one(box: dict, key: str) -> None:
        v = box.get(key)
        if isinstance(v, str) and len(v) >= 8 and not v.startswith(PREFIX):
            found.append(v)
    _each_secret(settings, one)
    return found


def has_plaintext(settings: dict) -> bool:
    found: list[bool] = []

    def one(box: dict, key: str) -> None:
        v = box.get(key)
        if isinstance(v, str) and v and not v.startswith(PREFIX):
            found.append(True)
    _each_secret(settings, one)
    return bool(found) and available()
