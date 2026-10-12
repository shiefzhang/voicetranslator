#!/usr/bin/env python3
"""静态多变体 CPU 后端 —— 符号命名空间生成与冲突门禁。

背景
----
llama.cpp 上游只有两条路可选 CPU 后端：

  * 单个变体（`GGML_CPU_ARM_ARCH`），编译期钉死一条指令集；
  * 多动态变体（`GGML_CPU_ALL_VARIANTS`），但**强依赖 `GGML_BACKEND_DL`**，
    而后者又强依赖 `BUILD_SHARED_LIBS`（`ggml/src/CMakeLists.txt:372`）。

前者在 iPad mini 4（A8，无 dotprod）与 iPhone 16（A18，dotprod+i8mm）之间只能二选一；
后者会把整个 llama/ggml 变成一堆 dylib，动到 iOS 的打包与签名链路。

本脚本支撑第三条路：**静态多变体**。同一个静态库链路上编 N 份 `libggml-cpu-<tag>.a`，
每份用不同的 `-march=`，再把非选中变体的全局符号统一加 `vtv<N>_` 前缀，于是
「同一份符号表出现 N 次」的链接冲突消失；运行时由 `native/cpu/vt_cpu_dispatch.c`
按 CPU 实际特性挑一份，其余变体的代码永不执行 —— A8 上永远不会碰到 dotprod 指令。

为什么不能只改 C 符号
----------------------
`libggml-cpu.a` 里有约 270 个 C++ 全局符号（repack.cpp 的模板实例占大头）。
mangled 名字改不了，但 **mangled 是从源码标识符推出来的** —— 所以做法是反查：
`nm` 取 mangled → `c++filt` 还原 → 抽出「声明路径」里的标识符 → 以
`-D<ident>=vtv<N>_<ident>` 形态喂给预处理器（本脚本生成 `vt_ns_<tag>.h`，
由 CMake 以 `-include` 注入）。改名在 TU 内一致，故不破坏语义。

门禁
----
改名清单是自动推出来的，所以必须验。`check` 子命令断言两份归档的**已定义全局符号
交集为空**；`scripts/verify-cpu-variants.py` 在此之上再加「A8 基线变体不得含
sdot/smmla」的静态判据。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# nm 的 type 列：大写=全局，U=未定义。`C` 是 common。
NM_TYPE_RE = re.compile(r"^\s*(?:[0-9a-fA-F]+)?\s+([A-Z])\s+(\S.*)$")
# 这些是编译器/运行时内部符号，改名会破坏与 libc++ 的约定
CXX_SPECIAL_PREFIXES = (
    "vtable for ",
    "VTT for ",
    "typeinfo for ",
    "typeinfo name for ",
    "construction vtable for ",
    "guard variable for ",
    "non-virtual thunk to ",
    "virtual thunk to ",
    "covariant return thunk to ",
    "transaction clone for ",
    "reference temporary for ",
    "initializer for ",
    "operator",
)
# C++ 关键字 / 字面量，不能进 -D
NOT_IDENTIFIER = {
    "const", "void", "int", "long", "short", "char", "signed", "unsigned", "float",
    "double", "bool", "wchar_t", "char8_t", "char16_t", "char32_t", "auto", "decltype",
    "struct", "class", "union", "enum", "namespace", "template", "typename", "using",
    "public", "private", "protected", "static", "extern", "inline", "virtual", "explicit",
    "constexpr", "consteval", "noexcept", "throw", "operator", "new", "delete", "sizeof",
    "alignof", "true", "false", "nullptr", "undefined", "restrict", "volatile",
    "std", "abi", "nqe220106", "u", "l", "ul", "ll", "ull", "__int128",
}
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"命令失败: {' '.join(cmd)}\n{p.stderr}")
    return p.stdout


def demacho(sym: str) -> str:
    """去掉 Mach-O **_一个** 前导下划线。

    ⚠️ 千万别写成 `sym.lstrip("_")` —— 那是「去掉所有前导下划线」，会把 C++ 的
    `__ZN4ggml...` 削成 `Z19ggml...`，c++filt 认不出来，于是 mangled 名字被当成
    源码标识符直接写进 -D 清单（改名全废，且看起来「跑通了」）。
    """
    return sym[1:] if sym.startswith("_") else sym


def defined_globals(archive: Path, with_kind: bool = False) -> list[str]:
    """归档里「已定义的全局符号」，去掉了 Mach-O 的前导下划线。

    ⚠️ 必须包含所有大写 type（T/D/B/S/C/A 都算）—— 漏掉 common(`C`) 或
    absolute(`A`) 会让某一类符号在两个变体里重复出现，而门禁只查 T 是查不出来的。
    """
    out = run(["nm", "-g", "--defined-only", str(archive)])
    names: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip() or line.endswith(":"):
            continue
        m = NM_TYPE_RE.match(line)
        if not m:
            continue
        kind, sym = m.group(1), m.group(2)
        if kind == "U":
            continue
        names[demacho(sym)] = kind
    return sorted(f"{n}:{k}" if with_kind else n for n, k in names.items())


def demangle(names: list[str]) -> dict[str, str]:
    """批量反查 mangled → demangled；非 C++ 符号原样返回。"""
    cxx = [n for n in names if n.startswith("_Z")]
    if not cxx:
        return {}
    p = subprocess.run(["c++filt"] + cxx, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"c++filt 失败: {p.stderr}")
    return dict(zip(cxx, p.stdout.splitlines()))


def strip_template_args(text: str) -> str:
    """去掉 `<...>` 里的模板实参 —— 那些是类型/枚举名，绝不能改名。

    例：`ggml::cpu::repack::tensor_traits<block_q4_0, 8u, ggml_type::GGML_TYPE_Q4_0>`
        → `ggml::cpu::repack::tensor_traits`
    """
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def strip_arg_list(text: str) -> str:
    """只保留函数名部分：把第一个顶层 `(` 之后的内容丢掉。"""
    return text.split("(", 1)[0].strip()


def declaration_path(dem: str) -> str:
    """从 c++filt 的输出里取出「声明路径」（`a::b::c`），剥掉返回类型与参数表。

    ⚠️ c++filt **会带上返回类型**，所以 `std::__1::__introsort<...>(...)` 实际输出是
    `void std::__1::__introsort<...>(...)` —— 用 `startswith("std::")` 判会全部漏判，
    于是 `__1`/`__introsort`/`emplace_back`/`vector` 这些 libc++ 名字会被当成源码标识符
    写进改名清单（把 `std::vector::emplace_back` 改名，属于无声的 ODR 破坏）。
    正确做法：去掉模板实参与参数表后，**取最后一个空白分隔的 token**。
    """
    text = dem.strip()
    if not text:
        return ""
    # 剥掉 c++filt 的定语前缀。顺序有讲究：`typeinfo name for` 必须先于 `typeinfo for`。
    for _ in range(4):
        for pre in sorted(CXX_SPECIAL_PREFIXES, key=len, reverse=True):
            if text.startswith(pre):
                text = text[len(pre):].strip()
                break
        else:
            break
    text = strip_arg_list(strip_template_args(text))
    words = text.split()
    return words[-1] if words else ""


def identifiers_from_demangled(dem: str) -> set[str]:
    """从 demangled 名字里抽出「需要改名以消除跨变体冲突」的源码标识符。"""
    path = declaration_path(dem)
    if not path:
        return set()

    # ⚠️ libc++ 的 ABI 标记符号（`[abi:nqe220106]`）**不能改名**：
    #    `std::length_error` 改名成 `std::vtv1_length_error` 就是换了一个异常类型，
    #    变体里抛出去的东西 libc++ 的 catch 子句认不出来 → std::terminate。
    #    好在实测（`ld -r` 合并两个变体对象）它们本来就**不冲突**：ld64 对 ABI 标记符号
    #    做合并 —— 8 个冲突全是 `ggml_*`，`std::` 0 个。所以这里直接跳过。
    if path.startswith("std::") or path.startswith("__"):
        return set()

    # 只看 `::` 分隔的声明路径。首段是 `ggml` 且后面还有 `::` 时它是命名空间，
    # 保留不动（ggml-cpu 会 include ggml-base 的头，改命名空间会把两边类型拆开）。
    parts = [p.strip() for p in path.split("::") if p.strip()]
    if len(parts) > 1 and parts[0] == "ggml":
        parts = parts[1:]

    idents: set[str] = set()
    for part in parts:
        # 去掉前导 `~`（析构）、`operator` 之类
        part = part.lstrip("~")
        if not IDENT_RE.fullmatch(part):
            continue
        if part in NOT_IDENTIFIER:
            continue
        idents.add(part)
    return idents


def rename_identifiers(archive: Path) -> list[str]:
    """算出一份归档需要加前缀的源码标识符清单。"""
    syms = defined_globals(archive)
    dems = demangle(syms)
    idents: set[str] = set()
    for s in syms:
        if s in dems:
            idents |= identifiers_from_demangled(dems[s])
        else:
            # 纯 C 符号：名字就是标识符本身
            if IDENT_RE.fullmatch(s) and s not in NOT_IDENTIFIER:
                idents.add(s)
    # 兜底：mangled 名字**出现在清单里**的 C 符号，以及 C++ 里被 demangle 漏掉的情况。
    # 直接从 mangled 名里也抠一遍标识符，覆盖 `__Z...` 之外的内联汇编标签等。
    for s in syms:
        if s.startswith("_Z"):
            continue
        if IDENT_RE.fullmatch(s):
            idents.add(s)
    # 断言：任何 `_Z` 开头的符号都必须已经被 demangle 处理过。没处理到 = 上面
    # demacho/去重口径出错，此时清单里会混进 mangled 串（改名静默失效）。
    missing = [s for s in syms if s.startswith("_Z") and s not in dems]
    if missing:
        sys.exit(f"✗ 有 {len(missing)} 个 C++ 符号没被反查（例：{missing[:3]}）")
    leaked = [i for i in idents if i.startswith("_Z")]
    if leaked:
        sys.exit(f"✗ 改名清单里混进了 mangled 名字（例：{leaked[:3]}）")
    return sorted(idents)


def cmd_names(args: argparse.Namespace) -> None:
    for i in rename_identifiers(Path(args.archive)):
        print(i)


def is_libcxx_symbol(sym: str) -> bool:
    """判断这是不是 libc++ / 编译器运行时的符号（不是 ggml 自己的）。

    这些符号**跨变体重复但不冲突**：实测 `ld -r -all_load` 把三个变体归档一起合并，
    退出码 0、duplicate symbol 0 条 —— ld64 会把带 ABI 标记（`[abi:nqe220106]`）的
    libc++ 符号合并掉。所以门禁里要把它们从「冲突」里剔除，但**必须显式剔除**：
    不写出来，下一个人会以为门禁在放水；写出来，就知道这是链接器的既定行为。
    """
    if not (sym.startswith("_Z")):
        return False
    dem = demangle([sym]).get(sym)
    if dem is None:
        return False
    path = declaration_path(dem)
    return path.startswith("std::") or path.startswith("__") or "std::__" in path


def cmd_check(args: argparse.Namespace) -> None:
    """断言两份归档的已定义全局符号交集为空。

    `--ignore-libcxx`：把 libc++ 符号排除在判据外（见 is_libcxx_symbol 的理由），
    并把被排除的名单打出来 —— 剔除要有据可查，不能静默。
    """
    a = set(defined_globals(Path(args.a)))
    b = set(defined_globals(Path(args.b)))
    shared = sorted(a & b)

    ignored: list[str] = []
    if args.ignore_libcxx:
        ignored = [s for s in shared if is_libcxx_symbol(s)]
        shared = [s for s in shared if not is_libcxx_symbol(s)]

    if shared:
        print(f"✗ 符号冲突 {len(shared)} 个：", file=sys.stderr)
        for s in shared[:40]:
            print(f"    {s}", file=sys.stderr)
        if len(shared) > 40:
            print(f"    …还有 {len(shared) - 40} 个", file=sys.stderr)
        print(f"  {Path(args.a).name}: {len(a)} 个全局符号", file=sys.stderr)
        print(f"  {Path(args.b).name}: {len(b)} 个全局符号", file=sys.stderr)
        sys.exit(1)

    tail = f"，另有 {len(ignored)} 个 libc++ ABI 符号重叠（ld64 会合并，已按设计剔除）" if ignored else ""
    print(f"✓ 无符号冲突（{Path(args.a).name} {len(a)} 个 / {Path(args.b).name} {len(b)} 个{tail}）")


def cmd_gen(args: argparse.Namespace, ) -> None:
    """生成 vt_ns_<tag>.h（-include 注入）与 vt_cpu_variants.h（调度表）。"""
    base = Path(args.baseline)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    idents = rename_identifiers(base)
    if not idents:
        sys.exit(f"✗ 从 {base} 里没推出任何可改名标识符，基线归档不完整？")

    tags = args.tag or []
    variants = []
    for idx, spec in enumerate(tags):
        tag, _, feats = spec.partition(":")
        feats = [f for f in feats.split(",") if f]
        prefix = f"vtv{idx}_"
        variants.append({"tag": tag, "prefix": prefix, "features": feats,
                         "macros": [{"DOTPROD": "GGML_USE_DOTPROD",
                                     "FP16_VECTOR_ARITHMETIC": "GGML_USE_FP16_VECTOR_ARITHMETIC",
                                     "SVE": "GGML_USE_SVE",
                                     "MATMUL_INT8": "GGML_USE_MATMUL_INT8",
                                     "SVE2": "GGML_USE_SVE2",
                                     "SME": "GGML_USE_SME"}[f] for f in feats]})
        # 该变体自己的头文件
        lines = [
            f"/* 自动生成，勿手改 —— scripts/vt-cpu-variants.py gen */",
            f"/* 变体 {tag}：全局符号统一加 {prefix} 前缀，与其它变体共存于同一个静态链接里。 */",
            "#ifndef VT_CPU_NS_H",
            "#define VT_CPU_NS_H",
        ]
        for ident in idents:
            lines.append(f"#define {ident} {prefix}{ident}")
        lines.append("#endif")
        (out / f"vt_ns_{tag}.h").write_text("\n".join(lines) + "\n")

    # 调度表头：给 vt_cpu_dispatch.c 用。
    # 每个变体要暴露 (reg 符号名, 人类可读名, 需要的特性集合)。
    d_lines = [
        "/* 自动生成，勿手改 —— scripts/vt-cpu-variants.py gen */",
        "#ifndef VT_CPU_VARIANTS_H",
        "#define VT_CPU_VARIANTS_H",
        "",
        "/* 需要 CPU 提供的特性位（与 ggml_backend_cpu_aarch64_score 的口径一致） */",
        "#define VT_FEAT_DOTPROD  (1u << 0)",
        "#define VT_FEAT_I8MM     (1u << 1)",
        "#define VT_FEAT_FP16_VA  (1u << 2)",
        "#define VT_FEAT_SVE      (1u << 3)",
        "#define VT_FEAT_SVE2     (1u << 4)",
        "#define VT_FEAT_SME      (1u << 5)",
        "#define VT_FEAT_NONE     0u",
        "",
        "typedef struct vt_cpu_variant {",
        "    const char * tag;      /* 归档名后缀，如 armv8a */",
        "    unsigned     requires; /* VT_FEAT_* 位；缺任意一位则本变体不可用 */",
        "    int          score;    /* 满足要求时的优先级，大者胜 */",
        "} vt_cpu_variant;",
        "",
        f"#define VT_CPU_VARIANT_COUNT {len(variants)}",
        "",
        "/* vtv<i>_ggml_backend_cpu_reg —— 拼出各变体「改名后」的入口符号 */",
        "#define VT_CPU_REG(i) vtv##i##_ggml_backend_cpu_reg",
        "",
    ]
    # 变体入口函数指针列表：dispatch 靠它拿 reg。
    # 各变体入口的 extern 声明由 vt_cpu_dispatch.c 自己用 VT_CPU_REG(i) 展开
    # （这里不能声明：`void *` 与真实的 `ggml_backend_reg_t` 类型对不上）。
    d_lines.append("/* 变体入口函数指针列表，用于在静止表里按下标取入口 */")
    d_lines.append("#define VT_CPU_VARIANT_REG_LIST \\")
    d_lines.append("    { " + ", ".join(f"VT_CPU_REG({i})" for i in range(len(variants))) + " }")
    d_lines.append("")

    feat_bit = {"DOTPROD": "VT_FEAT_DOTPROD", "MATMUL_INT8": "VT_FEAT_I8MM",
                "FP16_VECTOR_ARITHMETIC": "VT_FEAT_FP16_VA", "SVE": "VT_FEAT_SVE",
                "SVE2": "VT_FEAT_SVE2", "SME": "VT_FEAT_SME"}
    d_lines.append("static const vt_cpu_variant k_vt_cpu_variants[] = {")
    for v in variants:
        req = " | ".join(feat_bit[f] for f in v["features"]) or "VT_FEAT_NONE"
        score = 1 + sum({  # 与 ggml 的 aarch64 score 同权
            "DOTPROD": 1 << 1, "FP16_VECTOR_ARITHMETIC": 1 << 2, "SVE": 1 << 3,
            "MATMUL_INT8": 1 << 4, "SVE2": 1 << 5, "SME": 1 << 6}[f] for f in v["features"])
        d_lines.append(f'    {{ "{v["tag"]}", {req}, {score} }},')
    d_lines += [
        "};",
        "",
        "#endif",
    ]
    (out / "vt_cpu_variants.h").write_text("\n".join(d_lines) + "\n")

    print(f"✓ 改名标识符 {len(idents)} 个 → {out}/vt_ns_<tag>.h（{len(variants)} 份）")
    print(f"✓ 调度表 → {out}/vt_cpu_variants.h")
    for v in variants:
        print(f"    [{v['tag']}] 前缀 {v['prefix']} 特性 {v['features'] or ['(无)']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="静态多变体 CPU 后端符号工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("names", help="打印需要改名的源码标识符")
    p.add_argument("archive", help="基线 libggml-cpu.a")
    p.set_defaults(func=cmd_names)

    p = sub.add_parser("check", help="断言两份归档符号不冲突")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--ignore-libcxx", action="store_true",
                   help="把 libc++ ABI 标记符号排除在判据外（ld64 会合并它们），并打印被排除的名单")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("gen", help="生成命名空间头与调度表头")
    p.add_argument("--baseline", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--tag", action="append", required=True,
                   help="tag[:FEAT1,FEAT2]，可重复；顺序即 vtv0/vtv1/… 的编号")
    p.set_defaults(func=cmd_gen)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
