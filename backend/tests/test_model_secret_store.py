"""Encrypted model credentials stay private, durable, and database-scoped."""
from __future__ import annotations

import importlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import dotenv
import pytest


@pytest.fixture()
def secret_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # This fixture never loads a real .env or touches the configured user store.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    from app import config
    from app.model_lab import secret_store as store

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "isolated.db"))
    monkeypatch.setattr(config, "LLM_API_KEY", "synthetic-platform-store-test-token")
    return store


def _encrypted_file(store, reference: str) -> Path:
    return store.storage_directory() / (reference.split(":", 1)[1] + ".enc")


def test_encrypts_without_plaintext_and_enforces_owner_permissions(secret_store):
    store = secret_store
    secret = "synthetic-token-that-must-only-appear-in-memory"
    reference = store.store_secret(secret)
    assert reference.startswith("local-model:")
    assert len(reference.removeprefix("local-model:")) == 32
    assert store.resolve_secret_reference(reference) == secret
    assert store.secret_reference_configured(reference) is True

    root = store.storage_directory()
    assert {path.name for path in root.iterdir()} == {
        ".key", ".lock", _encrypted_file(store, reference).name,
    }
    for path in root.iterdir():
        assert secret.encode() not in path.read_bytes()
        assert not path.name.startswith(".pending-")
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in root.iterdir())


def test_existing_store_permissions_are_tightened(secret_store):
    if os.name != "posix":
        pytest.skip("POSIX permission bits are required")
    store = secret_store
    reference = store.store_secret("synthetic-permission-token")
    root = store.storage_directory()
    root.chmod(0o755)
    for path in root.iterdir():
        path.chmod(0o644)

    assert store.resolve_secret_reference(reference) == "synthetic-permission-token"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in root.iterdir())


def test_reload_reads_same_key_and_ciphertext(secret_store):
    store = secret_store
    reference = store.store_secret("synthetic-persistent-token")
    root = store.storage_directory()
    original_key = (root / ".key").read_bytes()
    original_ciphertext = _encrypted_file(store, reference).read_bytes()

    reloaded = importlib.reload(store)
    assert reloaded.resolve_secret_reference(reference) == "synthetic-persistent-token"
    assert (root / ".key").read_bytes() == original_key
    assert _encrypted_file(reloaded, reference).read_bytes() == original_ciphertext


def test_database_paths_have_separate_stores(secret_store, tmp_path, monkeypatch):
    from app import config

    store = secret_store
    first_database = config.DB_PATH
    first_reference = store.store_secret("synthetic-first-database-token")
    first_root = store.storage_directory()

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "second.db"))
    second_root = store.storage_directory()
    assert first_root != second_root
    assert store.secret_reference_configured(first_reference) is False
    assert not second_root.exists()
    second_reference = store.store_secret("synthetic-second-database-token")
    assert store.secret_reference_configured(first_reference) is False
    assert store.resolve_secret_reference(second_reference) == "synthetic-second-database-token"

    monkeypatch.setattr(config, "DB_PATH", first_database)
    assert store.resolve_secret_reference(first_reference) == "synthetic-first-database-token"
    assert store.secret_reference_configured(second_reference) is False


def test_concurrent_creations_remain_independently_readable(secret_store):
    store = secret_store
    values = [f"synthetic-concurrent-token-{index:03d}" for index in range(32)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        references = list(pool.map(store.store_secret, values))
        restored = list(pool.map(store.resolve_secret_reference, references))
    assert len(set(references)) == len(values)
    assert restored == values
    assert len(list(store.storage_directory().glob("*.enc"))) == len(values)
    assert not list(store.storage_directory().glob(".pending-*"))


def test_ciphertext_tampering_is_rejected_without_replacement(secret_store):
    store = secret_store
    reference = store.store_secret("synthetic-tamper-test-token")
    encrypted = _encrypted_file(store, reference)
    raw = bytearray(encrypted.read_bytes())
    raw[len(raw) // 2] = ord("A") if raw[len(raw) // 2] != ord("A") else ord("B")
    encrypted.write_bytes(raw)
    original_key = (store.storage_directory() / ".key").read_bytes()

    with pytest.raises(store.ModelSecretError, match="无法解密"):
        store.resolve_secret_reference(reference)
    assert store.secret_reference_configured(reference) is False
    assert encrypted.read_bytes() == bytes(raw)
    assert (store.storage_directory() / ".key").read_bytes() == original_key


def test_missing_key_never_generates_replacement_for_existing_ciphertexts(secret_store):
    store = secret_store
    reference = store.store_secret("synthetic-lost-key-token")
    encrypted = _encrypted_file(store, reference)
    original_ciphertext = encrypted.read_bytes()
    key_path = store.storage_directory() / ".key"
    key_path.unlink()

    with pytest.raises(store.ModelSecretError, match="缺少解密文件"):
        store.resolve_secret_reference(reference)
    with pytest.raises(store.ModelSecretError, match="缺少解密文件"):
        store.store_secret("synthetic-new-token-must-not-reset-store")
    assert store.secret_reference_configured(reference) is False
    assert not key_path.exists()
    assert encrypted.read_bytes() == original_ciphertext
    assert list(store.storage_directory().glob("*.enc")) == [encrypted]


def test_malformed_key_is_not_silently_replaced(secret_store):
    store = secret_store
    reference = store.store_secret("synthetic-malformed-key-token")
    key_path = store.storage_directory() / ".key"
    key_path.write_bytes(b"invalid-key-file")

    with pytest.raises(store.ModelSecretError, match="解密文件无效"):
        store.resolve_secret_reference(reference)
    with pytest.raises(store.ModelSecretError, match="解密文件无效"):
        store.store_secret("synthetic-new-key-token")
    assert key_path.read_bytes() == b"invalid-key-file"
    assert len(list(store.storage_directory().glob("*.enc"))) == 1


def test_existing_environment_references_still_work_without_creating_store(secret_store, monkeypatch):
    store = secret_store
    reference = "PRONOIA_MODEL_SECRET_STORE_ENV_TEST"
    monkeypatch.setenv(reference, "synthetic-legacy-env-token")
    assert store.resolve_secret_reference(reference) == "synthetic-legacy-env-token"
    assert store.secret_reference_configured(reference) is True
    assert not store.storage_directory().exists()
    monkeypatch.delenv(reference)
    with pytest.raises(store.ModelSecretError, match="尚未配置"):
        store.resolve_secret_reference(reference)
    assert store.secret_reference_configured(reference) is False


@pytest.mark.parametrize("reference", [
    "", "ARK_API_KEY", "PATH", "PRONOIA_MODEL_SECRET_", "PRONOIA_MODEL_SECRET_lowercase",
    "PRONOIA_STRATEGY_SECRET_VENDOR", "env:PRONOIA_MODEL_SECRET_VENDOR",
    "local-model:../.key", "local-model:" + "a" * 31,
    "local-model:" + "A" * 32, "local-model:" + "a" * 32 + "/../.key",
    "local-model:" + "a" * 32 + "\n", "/tmp/private-key", "file:///tmp/private-key",
])
def test_invalid_reference_paths_and_names_are_rejected(secret_store, reference):
    store = secret_store
    with pytest.raises(store.ModelSecretError, match="引用无效"):
        store.resolve_secret_reference(reference)
    assert store.secret_reference_configured(reference) is False
    assert not store.storage_directory().exists()


@pytest.mark.parametrize("secret", [
    "", "   ", "x" * 16_385, "abc\x00def", "abc\ndef", "abc\x7fdef",
    "\U0001f511" * 4_097, "invalid-surrogate-\ud800",
])
def test_invalid_secret_values_do_not_create_store(secret_store, secret):
    store = secret_store
    with pytest.raises(store.ModelSecretError, match="有效的 API Key"):
        store.store_secret(secret)
    assert not store.storage_directory().exists()


def test_maximum_utf8_byte_length_remains_readable(secret_store):
    store = secret_store
    secret = "\U0001f511" * 4_096
    reference = store.store_secret(secret)
    assert store.resolve_secret_reference(reference) == secret
    assert _encrypted_file(store, reference).stat().st_size < 65_536


def test_discard_removes_only_supplied_new_reference(secret_store, monkeypatch):
    store = secret_store
    previous = store.store_secret("synthetic-committed-token")
    new = store.store_secret("synthetic-uncommitted-token")
    root = store.storage_directory()
    original_key = (root / ".key").read_bytes()
    env_ref = "PRONOIA_MODEL_SECRET_DISCARD_ENV_TEST"
    monkeypatch.setenv(env_ref, "synthetic-retained-env-token")

    store.discard_secret(new)
    store.discard_secret(new)  # Cleanup can safely repeat after rollback.
    store.discard_secret(env_ref)
    store.discard_secret("local-model:../.key")
    assert not _encrypted_file(store, new).exists()
    assert store.resolve_secret_reference(previous) == "synthetic-committed-token"
    assert store.resolve_secret_reference(env_ref) == "synthetic-retained-env-token"
    assert (root / ".key").read_bytes() == original_key
    assert list(root.glob("*.enc")) == [_encrypted_file(store, previous)]


@pytest.mark.parametrize("target_file", ["key", "ciphertext"])
def test_symlinked_private_files_are_rejected(secret_store, tmp_path, target_file):
    if os.name != "posix":
        pytest.skip("POSIX symlink protection is required")
    store = secret_store
    reference = store.store_secret("synthetic-symlink-token")
    target = store.storage_directory() / ".key" if target_file == "key" else _encrypted_file(store, reference)
    outside = tmp_path / "outside-file"
    outside.write_bytes(target.read_bytes())
    outside.chmod(0o644)
    original_content = outside.read_bytes()
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(store.ModelSecretError):
        store.resolve_secret_reference(reference)
    assert outside.read_bytes() == original_content
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_service_redacts_reflected_local_credentials_and_internal_references(secret_store):
    from app.model_lab import service

    store = secret_store
    secret = "synthetic-reflected-store-test-token"
    reference = store.store_secret(secret)
    snapshot = {"id": "candidate-test-profile", "secret_env_ref": reference}
    diagnostic = (
        f"provider failure: {secret}; credential_ref={reference}; "
        "platform=synthetic-platform-store-test-token"
    )
    sanitized = service._sanitize_text(diagnostic, snapshot)
    assert "provider failure" in sanitized
    assert secret not in sanitized
    assert reference not in sanitized
    assert "synthetic-platform-store-test-token" not in sanitized
    assert "[redacted]" in sanitized
    assert "[secret reference hidden]" in sanitized

    scrubbed = service._scrub_payload({
        "answer": diagnostic,
        "trace": [{"api_key": secret, "secret_env_ref": reference, "message": diagnostic}],
    }, snapshot)
    assert scrubbed["answer"] == sanitized
    assert scrubbed["trace"] == [{"message": sanitized}]


@pytest.mark.parametrize("broken_store", ["missing-reference", "missing-key", "tampered-ciphertext"])
def test_service_sanitizer_preserves_original_error_when_secret_unavailable(secret_store, broken_store):
    from app.model_lab import service

    store = secret_store
    if broken_store == "missing-reference":
        reference = "local-model:" + "0" * 32
    else:
        reference = store.store_secret("synthetic-unavailable-store-token")
        if broken_store == "missing-key":
            (store.storage_directory() / ".key").unlink()
        else:
            _encrypted_file(store, reference).write_bytes(b"corrupted-ciphertext")
    snapshot = {"id": "missing-credential-profile", "secret_env_ref": reference}
    sanitized = service._safe_error(RuntimeError(f"original-provider-failure ({reference})"), snapshot, None)
    assert sanitized.startswith("RuntimeError: original-provider-failure")
    assert reference not in sanitized
    assert "[secret reference hidden]" in sanitized
