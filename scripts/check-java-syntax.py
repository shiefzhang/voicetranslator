#!/usr/bin/env python3
"""Java 语法检查 —— 在没有 Android SDK / JDK 的机器上也能拦住低级错误。

## 为什么需要它

Android 侧的改动**没法在本机编译验证**：编译 APK 要 Android SDK + NDK + JDK，
而开发机上三者都没有。于是 `.java` 里一个漏掉的大括号、一个拼错的字面量，
只有在真正接上设备时才会炸 —— 反馈周期太长。

tree-sitter 的 Java 语法能把这件事提前：它对源文件做**真正的解析**，产出
带位置的 ERROR / MISSING 节点。注意它的能力和边界：

- ✅ 能查：括号不配、语句不完整、关键字拼错、`switch` 缺冒号、注解写坏、
  字符串未闭合…… 也就是**一切语法层面**的问题。
- ❌ 不能查：类型对不对、方法存不存在、参数个数对不对 —— 那是语义，需要符号表。
  所以这个脚本是「廉价前置网」，不是编译器的替代品。

## 用法

    python3 scripts/check-java-syntax.py            # 查 app/src/main/java 下全部 .java
    python3 scripts/check-java-syntax.py 文件 [文件…]  # 只查指定文件

退出码非 0 表示有语法错误，方便挂进 CI 或 pre-commit。
"""

import sys
from pathlib import Path

try:
    import tree_sitter_java
    from tree_sitter import Language, Parser
except ImportError:
    sys.exit(
        "缺少依赖：pip install tree_sitter tree_sitter_java\n"
        "（tree_sitter_java 提供预编译语法，不需要本地 C 编译器）"
    )

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO / "app" / "src" / "main" / "java"


def is_bad(node) -> bool:
    """这个节点是不是解析器在报错。

    ⚠️ 判据必须是 `is_error` / `is_missing` 两个**谓词**，不能拿 `node.type` 去比
    字面量 `"ERROR"`/`"MISSING"` —— tree-sitter 的 MISSING 节点 `type` 是**缺的那个
    token 本身**（少一个分号时 `type == ";"`）。写成字符串比较会一条都报不出来，
    而「全部通过」的输出看上去完全正常，是个安静的假阴性（踩过一次）。
    """
    return node.is_error or node.is_missing


def problems(path: Path) -> list[str]:
    """返回该文件的语法问题（人类可读，带 file:line:col）。"""
    source = path.read_bytes()
    parser = Parser(Language(tree_sitter_java.language()))
    tree = parser.parse(source)
    root = tree.root_node
    if not root.has_error:
        return []

    out: list[str] = []

    def walk(node) -> None:
        # 只在**没有出错子节点**的那一层报 —— 外层的 ERROR 会把同一个错误
        # 再包一层，直接递归会把一条错报成七八条，反而看不出重点。
        if is_bad(node) and not any(is_bad(c) for c in node.children):
            row, col = node.start_point
            if node.is_missing:
                # MISSING 节点的字节范围是空的，报 token 名才有意义。
                out.append(f"{path}:{row + 1}:{col + 1}: 缺少 {node.type or 'token'}")
            else:
                snippet = source[node.start_byte:node.end_byte].decode("utf-8", "replace")
                snippet = " ".join(snippet.split())[:60]
                out.append(f"{path}:{row + 1}:{col + 1}: 无法解析"
                           + (f" → {snippet!r}" if snippet else ""))
        for child in node.children:
            walk(child)

    walk(root)
    return out


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]]
    if not targets:
        targets = sorted(DEFAULT_ROOT.rglob("*.java"))
    if not targets:
        print(f"没有找到 .java 文件（默认目录 {DEFAULT_ROOT}）")
        return 1

    total = 0
    for path in targets:
        if not path.is_file():
            print(f"{path}: 不是文件", file=sys.stderr)
            total += 1
            continue
        for line in problems(path):
            print(line)
            total += 1

    if total:
        print(f"\n✗ {total} 处语法问题，检查 {len(targets)} 个文件")
        return 1
    print(f"✓ {len(targets)} 个 Java 文件的语法都通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
