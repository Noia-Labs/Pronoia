#!/usr/bin/env python3
"""Remove sender-specific model API configuration from a staged share bundle."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ENV_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$"
)
PRIVATE_VALUE_KEY_RE = re.compile(
    r"(?:^|_)(?:API_KEY|KEY|TOKEN|SECRET|PASSWORD)$", re.IGNORECASE
)
ADDRESS_VALUE_KEY_RE = re.compile(
    r"(?:^|_)(?:API_URL|BASE_URL|ENDPOINT_URL|ENDPOINT)$", re.IGNORECASE
)

# These are deliberately semantic rather than a blanket URL matcher. Public
# market-data endpoints, localhost development URLs and package-lock download
# URLs are application dependencies, not the sender's model connection.
MODEL_API_ADDRESS_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9_])"
        r"[A-Z][A-Z0-9_]*(?:API_URL|BASE_URL|ENDPOINT_URL)\s*=\s*"
        r"[\"']?(?P<address>https?://[^\s\"'`<>),]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:baseUrl|apiUrl|modelApiUrl)\s*:\s*[\"']"
        r"(?P<address>https?://[^\"']+)[\"']"
    ),
    re.compile(
        r"os\.getenv\(\s*[\"'][A-Z0-9_]*(?:API_URL|BASE_URL|ENDPOINT_URL)[\"']"
        r"\s*,\s*[\"'](?P<address>https?://[^\"']+)[\"']\s*\)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdefault_(?:api_)?url\s*=\s*[\"']"
        r"(?P<address>https?://[^\"']+)[\"']",
        re.IGNORECASE,
    ),
)

EXAMPLE_PRIVATE_ASSIGNMENT_RE = re.compile(
    r"(?m)^(?P<prefix>\s*(?:export\s+)?[A-Z][A-Z0-9_]*"
    r"(?:API_KEY|TOKEN|SECRET|PASSWORD)\s*=\s*).*$"
)


def _text_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            sample = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"cannot inspect staged file: {path}") from exc
        if b"\0" in sample:
            continue
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            continue
        result.append(path)
    return result


def _unquote_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _source_private_values(env_paths: list[Path]) -> tuple[set[str], set[str]]:
    addresses: set[str] = set()
    private_values: set[str] = set()
    for env_path in env_paths:
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = ENV_LINE_RE.match(line)
            if not match:
                continue
            key = match.group("key")
            value = _unquote_env_value(match.group("value"))
            if len(value) < 8:
                continue
            if PRIVATE_VALUE_KEY_RE.search(key):
                private_values.add(value)
            if ADDRESS_VALUE_KEY_RE.search(key) and value.lower().startswith(("http://", "https://")):
                addresses.add(value.rstrip("/"))
                addresses.add(value)
    return addresses, private_values


def _model_addresses(files: list[Path]) -> set[str]:
    addresses: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in MODEL_API_ADDRESS_PATTERNS:
            addresses.update(match.group("address") for match in pattern.finditer(text))
    return {value for value in addresses if value}


def sanitize(stage_root: Path, source_envs: list[Path]) -> tuple[int, int, int]:
    if not stage_root.is_dir():
        raise ValueError("staging directory does not exist")
    if stage_root.resolve() in {Path("/"), Path.home().resolve()}:
        raise ValueError("refusing to sanitize a broad filesystem root")

    files = _text_files(stage_root)
    source_addresses, source_private_values = _source_private_values(source_envs)
    addresses = _model_addresses(files) | source_addresses
    replacement_values = sorted(addresses | source_private_values, key=len, reverse=True)

    changed_files = 0
    blanked_example_values = 0
    for path in files:
        original = path.read_text(encoding="utf-8")
        updated = original
        for value in replacement_values:
            updated = updated.replace(value, "")
        if path.name in {".env.example", ".env.share.example"}:
            updated, count = EXAMPLE_PRIVATE_ASSIGNMENT_RE.subn(r"\g<prefix>", updated)
            blanked_example_values += count
        relative = path.relative_to(stage_root).as_posix()
        if relative == ".env.example":
            updated = re.sub(
                r"(?m)^# 模型 API.*$",
                "# 模型 API（请填写接收者自己的兼容接口地址）",
                updated,
            )
        elif relative == "frontend/src/modelProviders.ts":
            updated = updated.replace('addressHint: "预填', 'addressHint: "请填写')
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed_files += 1

    remaining_files = _text_files(stage_root)
    for path in remaining_files:
        text = path.read_text(encoding="utf-8")
        if any(value and value in text for value in replacement_values):
            raise RuntimeError("a private configuration value remains in the staged bundle")
        if any(pattern.search(text) for pattern in MODEL_API_ADDRESS_PATTERNS):
            raise RuntimeError("a prefilled model API address remains in the staged bundle")

    marker = stage_root / "SHARE_SANITIZATION.md"
    marker.write_text(
        "# 分享包脱敏说明\n\n"
        "此副本未包含发送者的模型 API 地址、API Key、数据库、行情数据或历史回测结果。\n"
        "模型接口地址已留空；首次启动时请填写接收者自己的服务配置。\n"
        "应用运行所需的本机地址与公共行情数据源不属于模型配置，仍按源码保留。\n",
        encoding="utf-8",
    )
    return len(addresses), len(source_private_values), changed_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage_root", type=Path)
    parser.add_argument("--source-env", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    try:
        address_count, private_count, file_count = sanitize(
            args.stage_root.resolve(), args.source_env
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"分享包脱敏失败：{exc}", file=sys.stderr)
        return 1
    print(
        "分享包脱敏完成："
        f"移除 {address_count} 个模型 API 地址值、"
        f"{private_count} 个本机凭据值，更新 {file_count} 个文件。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
