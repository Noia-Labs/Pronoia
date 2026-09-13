"""Local, encrypted credentials for user-managed model connections.

Profiles and immutable run snapshots contain only an opaque reference. Each
replacement creates a new reference so an existing run keeps its credential.
The key and ciphertext live beside the database in an owner-only directory;
they are never written to .env or served by the frontend.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .. import config
from ..model_endpoint_security import validate_secret_env_name

try:
    import fcntl
except ImportError:  # pragma: no cover - local Windows installations
    fcntl = None

_LOCAL_REF = re.compile(r"local-model:([0-9a-f]{32})\Z")
_LOCK = threading.RLock()


class ModelSecretError(RuntimeError):
    """A credential error safe to show without revealing values or paths."""


def storage_directory() -> Path:
    database = Path(config.DB_PATH).expanduser().resolve()
    return database.parent / f".{database.name}.model-secrets"


def _private_file(path: Path, flags: int) -> int:
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ModelSecretError("本机模型密钥存储不可用")
    os.fchmod(fd, 0o600)
    return fd


@contextmanager
def _storage_lock(*, create: bool):
    with _LOCK:
        root = storage_directory()
        try:
            if create:
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if root.is_symlink() or not root.is_dir():
                raise ModelSecretError("本机尚未保存此模型的 API Key")
            root.chmod(0o700)
            fd = _private_file(root / ".lock", os.O_RDWR | os.O_CREAT)
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield root
            finally:
                os.close(fd)
        except OSError as exc:
            raise ModelSecretError("无法访问本机模型密钥存储，请检查文件权限") from exc


def _read_private(path: Path) -> bytes:
    with os.fdopen(_private_file(path, os.O_RDONLY), "rb") as stream:
        value = stream.read(65_537)
    if len(value) > 65_536:
        raise ModelSecretError("本机模型密钥文件无效，请重新填写 API Key")
    return value


def _atomic_write(path: Path, value: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _cipher(root: Path, *, create: bool = False) -> Fernet:
    key_path = root / ".key"
    if not key_path.exists():
        # A lost encryption key must not silently invalidate existing secrets.
        if not create or any(root.glob("*.enc")):
            raise ModelSecretError("本机模型密钥存储缺少解密文件，请恢复原文件")
        _atomic_write(key_path, Fernet.generate_key())
    try:
        return Fernet(_read_private(key_path))
    except (ValueError, TypeError) as exc:
        raise ModelSecretError("本机模型密钥存储的解密文件无效") from exc


def store_secret(secret: str) -> str:
    cleaned = str(secret or "").strip()
    try:
        plaintext = cleaned.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ModelSecretError("请输入有效的 API Key") from exc
    if not plaintext or len(plaintext) > 16_384 or any(ord(c) < 32 or ord(c) == 127 for c in cleaned):
        raise ModelSecretError("请输入有效的 API Key")
    with _storage_lock(create=True) as root:
        identity = uuid.uuid4().hex
        encrypted = _cipher(root, create=True).encrypt(plaintext)
        _atomic_write(root / f"{identity}.enc", encrypted)
    return f"local-model:{identity}"


def resolve_secret_reference(reference: str) -> str:
    ref = str(reference or "")
    local = _LOCAL_REF.fullmatch(ref)
    if local is None:
        try:
            name = validate_secret_env_name(ref, namespace="model", label="模型 API 密钥引用")
        except ValueError as exc:
            raise ModelSecretError("模型 API 密钥引用无效") from exc
        secret = os.getenv(name, "")
        if not secret:
            raise ModelSecretError("模型 API Key 尚未配置")
        return secret
    with _storage_lock(create=False) as root:
        try:
            encrypted = _read_private(root / f"{local.group(1)}.enc")
            secret = _cipher(root).decrypt(encrypted).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ModelSecretError("本机模型密钥无法解密，请重新填写 API Key") from exc
    if not secret:
        raise ModelSecretError("模型 API Key 尚未配置")
    return secret


def secret_reference_configured(reference: str) -> bool:
    try:
        return bool(resolve_secret_reference(reference))
    except ModelSecretError:
        return False


def discard_secret(reference: str) -> None:
    """Remove an uncommitted credential after profile creation/update failed."""
    local = _LOCAL_REF.fullmatch(str(reference or ""))
    if local is None:
        return
    with _storage_lock(create=False) as root:
        (root / f"{local.group(1)}.enc").unlink(missing_ok=True)
