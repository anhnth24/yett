"""Chọn secret backend theo config (spec P0-P1 §3.2)."""

from __future__ import annotations


def build_secret_store(backend: str):
    if backend == "env":
        from yett.secrets.backends import EnvSecretStore

        return EnvSecretStore()
    if backend == "file":
        from yett.secrets.file_store import FileSecretStore

        return FileSecretStore()
    # keyring backend: adapter native để sau — hiện dùng file store (an toàn, đơn giản).
    from yett.secrets.file_store import FileSecretStore

    return FileSecretStore()
