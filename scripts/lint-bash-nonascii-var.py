#!/usr/bin/env python3
"""检查 bash 脚本里「变量后紧跟非 ASCII 字符」的写法 —— 这是个会反复咬人的坑。

## 问题

bash 的变量名只认 [A-Za-z0-9_]。但在 UTF-8 环境下，写成

    log "源 $attempt：${url}"

本意是「变量 $attempt 后面跟一个全角冒号」，bash 却会把后面的**多字节首字节**
当成变量名的一部分，于是去找一个名叫 `attempt\\xef` 的变量。配合 `set -u`
直接致命：

    scripts/setup-ios-deps.sh: line 275: attempt: unbound variable

## 为什么容易漏

`bash -n`（纯语法检查）**查不出**这个问题 —— 语法是合法的，要到运行时展开
变量才炸。所以中文注释/文案密集的脚本必须单独跑这个检查。

在本项目里它已经发生过两次（一次在既有代码 9 处，一次在新写的下载循环里 2 处）。

## 修法

用花括号界定变量名：

    log "源 ${attempt}：${url}"

## 用法

    python3 scripts/lint-bash-nonascii-var.py scripts/*.sh

退出码 1 表示发现问题（可直接用于 CI）。不带参数时默认检查 scripts/ 下的 .sh。
"""

from __future__ import annotations

import pathlib
import re
import sys

# $VAR 后面紧跟非 ASCII（且未被 ${} 包裹、也不是 $$ / $'...' 这类形式）
PATTERN = re.compile(r"(?<![\{\$])\$([A-Za-z_][A-Za-z0-9_]*)([^\x00-\x7f])")


def check(path: pathlib.Path) -> list[tuple[int, str, str, str]]:
    """返回 [(行号, 变量名, 紧跟的字符, 该行内容), ...]。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"  [跳过] {path}: {exc}", file=sys.stderr)
        return []

    found = []
    in_heredoc: str | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        # 粗略跳过 heredoc 正文：里面的内容不参与变量展开（<<'EOF' 那种带引号的形式）
        stripped = line.strip()
        if in_heredoc is not None:
            if stripped == in_heredoc:
                in_heredoc = None
            continue
        m = re.search(r"<<-?\s*'([A-Za-z_][A-Za-z0-9_]*)'", line)
        if m:
            in_heredoc = m.group(1)
            continue

        for hit in PATTERN.finditer(line):
            # 行内注释里提到这种写法时说别改（本文件自己的文档就是例子）
            found.append((lineno, hit.group(1), hit.group(2), line.strip()))
    return found


def main(argv: list[str]) -> int:
    if argv[1:]:
        targets = [pathlib.Path(a) for a in argv[1:]]
    else:
        here = pathlib.Path(__file__).resolve().parent
        targets = sorted(here.glob("*.sh"))

    total = 0
    for path in targets:
        if not path.exists():
            print(f"  [跳过] {path}: 不存在", file=sys.stderr)
            continue
        hits = check(path)
        if not hits:
            print(f"  ✅ {path}")
            continue
        print(f"  ❌ {path} —— {len(hits)} 处：")
        for lineno, var, ch, line in hits:
            print(f"       {lineno}: ${var} 后面紧跟 {ch!r}  → 建议改成 ${{{var}}}")
            print(f"           {line[:100]}")
        total += len(hits)

    if total:
        print()
        print(f"共 {total} 处。修法：把 $VAR 写成 ${{VAR}}。")
        print("注意：bash -n 查不出这类问题，它只在运行时展开变量时才炸。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
