#!/usr/bin/env python3
"""bump.py —— Pronoia 单点版本号管理。

按 SemVer 规范：
  patch: 3.0.0 -> 3.0.1  (bug 修复 / 文案 / 重构)
  minor: 3.0.0 -> 3.1.0  (向后兼容的新功能)
  major: 3.0.0 -> 4.0.0  (破坏性变更，需显式指定)

自动同步到：
  - VERSION.py         (单一来源)
  - frontend/src/version.ts
  - frontend/package.json
  - backend/app/main.py (FastAPI app.version)
  - frontend/src/components/Sidebar.tsx  (Pronoia logo 旁角标)

附加：
  - --changelog "..."  在 CHANGELOG.md 追加一节（最新在前）
  - --commit           bump 后自动 git add + commit
  - --push             bump + commit 后自动 git push
  - --tag              bump + commit 后自动打 tag (vX.Y.Z)

示例：
  python scripts/bump.py patch -m "修复右栏默认折叠" --changelog "右栏默认折叠 + 状态持久化"
  python scripts/bump.py minor -m "新增事件预测员" --commit --push --tag
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent.parent
VERSION_PY = ROOT / "VERSION.py"
FRONTEND_VERSION_TS = ROOT / "frontend/src/version.ts"
FRONTEND_PACKAGE_JSON = ROOT / "frontend/package.json"
BACKEND_MAIN_PY = ROOT / "backend/app/main.py"
SIDEBAR_TSX = ROOT / "frontend/src/components/Sidebar.tsx"
CHANGELOG = ROOT / "CHANGELOG.md"


def read_version() -> tuple[int, int, int]:
    """从 VERSION.py 解析当前版本号（X.Y.Z 形式）。"""
    src = VERSION_PY.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*["\'](\d+)\.(\d+)\.(\d+)["\']', src)
    if not m:
        sys.exit(f"[bump] 解析 VERSION.py 失败：未找到 __version__")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump(kind: str, current: tuple[int, int, int]) -> tuple[int, int, int]:
    major, minor, patch = current
    if kind == "patch":
        return major, minor, patch + 1
    if kind == "minor":
        return major, minor + 1, 0
    if kind == "major":
        return major + 1, 0, 0
    sys.exit(f"[bump] 未知 bump kind: {kind}")


def write_all(new_ver: str) -> None:
    """把新版本号同步到 5 个文件。"""
    # 1) VERSION.py
    src = VERSION_PY.read_text(encoding="utf-8")
    src = re.sub(
        r'__version__\s*=\s*["\'].*?["\']',
        f'__version__ = "{new_ver}"',
        src,
        count=1,
    )
    VERSION_PY.write_text(src, encoding="utf-8")

    # 2) frontend/src/version.ts
    FRONTEND_VERSION_TS.write_text(
        f"// Pronoia 前端版本号（脚本自动维护：scripts/bump.py）\n"
        f"// 单一来源：VERSION.py；前端只读这里。\nexport const VERSION = \"{new_ver}\";\n",
        encoding="utf-8",
    )

    # 3) frontend/package.json（保留其它字段）
    pkg = json.loads(FRONTEND_PACKAGE_JSON.read_text(encoding="utf-8"))
    pkg["version"] = new_ver
    FRONTEND_PACKAGE_JSON.write_text(
        json.dumps(pkg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 4) backend/app/main.py
    main_src = BACKEND_MAIN_PY.read_text(encoding="utf-8")
    main_src = re.sub(
        r'(FastAPI\(\s*title="Pronoia",\s*version=)"[^"]*"',
        rf'\1"{new_ver}"',
        main_src,
        count=1,
    )
    BACKEND_MAIN_PY.write_text(main_src, encoding="utf-8")

    # 5) Sidebar.tsx 角标
    sb_src = SIDEBAR_TSX.read_text(encoding="utf-8")
    sb_src = re.sub(
        r'(rounded bg-jade-soft px-1\.5 py-px text-\[10px\] font-semibold text-jade">)\d+\.\d+\.\d+',
        rf'\g<1>{new_ver}',
        sb_src,
        count=1,
    )
    SIDEBAR_TSX.write_text(sb_src, encoding="utf-8")


def update_changelog(ver: str, kind: str, msg: str) -> None:
    """在 CHANGELOG.md 顶部标题后插入新一节（最新在前）。"""
    today = date.today().isoformat()
    label = {"patch": "修补", "minor": "功能", "major": "重大"}[kind]
    entry = f"## {ver} · {today}\n\n- {label}：{msg}\n"

    if CHANGELOG.exists():
        src = CHANGELOG.read_text(encoding="utf-8")
    else:
        src = "# 更新日志\n"
    lines = src.splitlines(keepends=True)
    # 跳过顶部的一级标题与空行，在其后插入新条目
    i = 0
    while i < len(lines) and (lines[i].startswith("# ") or lines[i].strip() == ""):
        i += 1
    out = lines[:i] + [entry + "\n"] + lines[i:]
    CHANGELOG.write_text("".join(out), encoding="utf-8")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in repo root; return CompletedProcess."""
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Pronoia 单点版本号管理（SemVer patch/minor/major）")
    ap.add_argument("kind", choices=["patch", "minor", "major"], help="升级类型")
    ap.add_argument("-m", "--message", required=True, help="本次更新说明（用于 changelog 与 commit）")
    ap.add_argument("--changelog", help="额外写入 README 更新日志的简述（默认 = -m）")
    ap.add_argument("--commit", action="store_true", help="bump 后自动 git add + commit")
    ap.add_argument("--push", action="store_true", help="bump + commit 后自动 git push")
    ap.add_argument("--tag", action="store_true", help="bump + commit 后自动 git tag vX.Y.Z")
    args = ap.parse_args()

    cur = read_version()
    new = bump(args.kind, cur)
    new_ver = f"{new[0]}.{new[1]}.{new[2]}"
    old_ver = f"{cur[0]}.{cur[1]}.{cur[2]}"
    print(f"[bump] {old_ver} -> {new_ver}  ({args.kind})")

    write_all(new_ver)
    update_changelog(new_ver, args.kind, args.changelog or args.message)
    print(f"[bump] 已同步 VERSION.py / version.ts / package.json / main.py / Sidebar.tsx / README.md")

    if args.commit or args.push or args.tag:
        git("add",
            "VERSION.py",
            "frontend/src/version.ts",
            "frontend/package.json",
            "backend/app/main.py",
            "frontend/src/components/Sidebar.tsx",
            "README.md")
        subject = f"bump: {new_ver} · {args.kind}"
        git("commit", "-m", f"{subject}\n\n{args.message}")
        print(f"[bump] 已 commit: {subject}")
    if args.tag:
        git("tag", f"v{new_ver}")
        print(f"[bump] 已 tag: v{new_ver}")
    if args.push:
        git("push", "origin", "main")
        if args.tag:
            git("push", "origin", f"v{new_ver}")
        print(f"[bump] 已 push")

    return 0


if __name__ == "__main__":
    sys.exit(main())
