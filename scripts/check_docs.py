#!/usr/bin/env python3
"""只读一致性校验：契约位置、需求编号与测试映射。不会修改任何文件。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "specs" / "001-github-cache-proxy" / "spec.md"
TESTING = ROOT / "testing.md"
CONTRACT = ROOT / "contracts" / "openapi.yaml"

FR_RE = re.compile(r"FR-\d{3}")
TEST_REF_RE = re.compile(r"`(tests/[\w/]+\.py)::(\w+)`")


def fail(message: str) -> bool:
    print(f"FAIL {message}")
    return False


def main() -> int:
    ok = True
    ok &= CONTRACT.is_file() or fail(f"契约缺失: {CONTRACT}")
    if CONTRACT.is_file():
        ok &= "openapi:" in CONTRACT.read_text(encoding="utf-8") or fail("契约不是 OpenAPI 文档")

    duplicates = [p for p in (ROOT / "specs").rglob("contracts") if p.is_dir()]
    ok &= not duplicates or fail(f"feature 目录下出现契约副本: {duplicates}")

    if not SPEC.is_file() or not TESTING.is_file():
        print("FAIL spec.md 或 testing.md 缺失")
        return 1

    spec_frs = set(FR_RE.findall(SPEC.read_text(encoding="utf-8")))
    testing_text = TESTING.read_text(encoding="utf-8")
    testing_frs = set(FR_RE.findall(testing_text))

    missing = sorted(spec_frs - testing_frs)
    extra = sorted(testing_frs - spec_frs)
    ok &= not missing or fail(f"testing.md 缺少需求的测试映射: {', '.join(missing)}")
    ok &= not extra or fail(f"testing.md 引用了 spec 中不存在的编号: {', '.join(extra)}")

    refs = TEST_REF_RE.findall(testing_text)
    ok &= bool(refs) or fail("testing.md 没有引用任何测试")
    for rel_path, func in refs:
        path = ROOT / rel_path
        if not path.is_file():
            ok = fail(f"测试文件不存在: {rel_path}")
            continue
        if f"def {func}(" not in path.read_text(encoding="utf-8"):
            ok = fail(f"{rel_path} 中找不到 {func}")

    if ok:
        print(
            f"OK 契约 {CONTRACT.relative_to(ROOT)}；"
            f"{len(spec_frs)} 条需求全部有映射；{len(refs)} 个测试引用有效"
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
