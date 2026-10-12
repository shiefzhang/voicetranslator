#!/usr/bin/env python3
"""给 vendored llama.cpp 打「静态多变体 CPU 后端」补丁（幂等）。

改三个文件，全部用精确锚点 + 幂等标记，重复执行是空操作：

  ggml/CMakeLists.txt
      新增三个缓存变量：GGML_CPU_VARIANTS_STATIC / VT_CPU_NS_DIR / VT_CPU_DISPATCH_SRC

  ggml/src/CMakeLists.txt
      * 把 ggml 的 CPU 变体分支从「只有一个未命名变体」改成「按列表建 N 个带名变体」
      * 给 ggml 目标塞进 vt_cpu_dispatch.c 与生成头目录

  ggml/src/ggml-cpu/CMakeLists.txt
      * 让 GGML_CPU_ALL_VARIANTS 的那套 -march= 推导逻辑同时对静态多变体生效
      * 静态多变体下不建 <name>-feats 打分对象（那是 DL 路线的东西），
        改为给该变体 -include vt_ns_<tag>.h 注入符号前缀

为什么不直接用上游的 GGML_CPU_ALL_VARIANTS
------------------------------------------
它 message(FATAL_ERROR) 要求 GGML_BACKEND_DL（ggml/src/CMakeLists.txt:372），
而 GGML_BACKEND_DL 又 FATAL_ERROR 要求 BUILD_SHARED_LIBS（同文件:188-189）。
那条路会把 libllama/libggml/libggml-base 全变成 dylib，iOS 侧要新增嵌入 + 重签名 +
rpath 一整套改动，而 iPad mini 4 必须继续跑 —— 风险不成比例。
静态多变体只动 ggml-cpu 的编译方式，其余链接形态一字不改。

⚠️ 下面的 CMake 片段一律用普通字符串，标记写成 @@NAME@@ 之后再做文本替换。
绝不能用 f-string：这些片段里全是 CMake 的 ${...}，f-string 会把它们当替换字段，
抛出来的是 NameError，看着跟 CMake 毫无关系（本项目已经踩过一次）。

用法
----
    python3 scripts/patch-llama-cpu-variants.py --check
    python3 scripts/patch-llama-cpu-variants.py --apply [--llama <src dir>]
    python3 scripts/patch-llama-cpu-variants.py --revert [--llama <src dir>]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_LLAMA = Path("ios/third_party/src/llama.cpp")

M_OPT, M_OPT_END = "# >>> vt: static cpu variants >>>", "# <<< vt: static cpu variants <<<"
M_SRC, M_SRC_END = "# >>> vt: static cpu variants (src) >>>", "# <<< vt: static cpu variants (src) <<<"
M_CPU, M_CPU_END = "# >>> vt: static cpu variants (cpu) >>>", "# <<< vt: static cpu variants (cpu) <<<"

OPTION_ANCHOR = 'set(GGML_CPU_ARM_ARCH        "" CACHE STRING "ggml: CPU architecture for ARM")'

OPTION_BLOCK = """@@MARK@@
# 静态多变体：把同一个 CPU 后端按不同 -march 编 N 份静态库，非选中变体的全局符号
# 统一加 vtv<i>_ 前缀（由 VT_CPU_NS_DIR 下的 vt_ns_<tag>.h 以 -include 注入），
# 运行时由 VT_CPU_DISPATCH_SRC 按 CPU 实际特性挑一份，未选中的变体一行都不会执行。
# GGML_CPU_VARIANTS_STATIC 格式：tag[:FEAT1,FEAT2] 用 | 连接。
# 例：armv8a|dotprod:DOTPROD|i8mm:DOTPROD,MATMUL_INT8
set(GGML_CPU_VARIANTS_STATIC "" CACHE STRING "ggml: N static CPU backend variants (tag[:FEAT1,FEAT2] joined by '|')")
set(VT_CPU_NS_DIR           "" CACHE PATH     "ggml: dir holding generated vt_ns_<tag>.h and vt_cpu_variants.h")
set(VT_CPU_DISPATCH_SRC     "" CACHE FILEPATH "ggml: vt_cpu_dispatch.c, provides the single unprefixed ggml_backend_cpu_reg()")
@@MARK_END@@"""

SRC_VARIANTS_ANCHOR = """elseif (GGML_CPU)
    ggml_add_cpu_backend_variant_impl("")
endif()"""

SRC_VARIANTS_BLOCK = """elseif (GGML_CPU_VARIANTS_STATIC)
@@MARK@@
    # 每个变体产出一份 libggml-cpu-<tag>.a。`-march=` / 特性宏的推导复用
    # ggml-cpu 里 GGML_CPU_ALL_VARIANTS 的那段逻辑（见 ggml-cpu/CMakeLists.txt 的补丁）。
    if (NOT GGML_SYSTEM_ARCH STREQUAL "ARM")
        message(FATAL_ERROR "GGML_CPU_VARIANTS_STATIC 目前只支持 aarch64（GGML_SYSTEM_ARCH=${GGML_SYSTEM_ARCH}）")
    endif()
    if (GGML_CPU_ARM_ARCH)
        message(FATAL_ERROR "GGML_CPU_VARIANTS_STATIC 与 GGML_CPU_ARM_ARCH 互斥")
    endif()
    string(REPLACE "|" ";" VT_CPU_VARIANT_ENTRIES "${GGML_CPU_VARIANTS_STATIC}")
    set(VT_CPU_VARIANT_TAGS "")
    foreach (_vt_entry IN LISTS VT_CPU_VARIANT_ENTRIES)
        if (_vt_entry STREQUAL "")
            continue()
        endif()
        string(REGEX REPLACE "^([^:]*):.*$" "\\\\1" _vt_tag "${_vt_entry}")
        string(REGEX REPLACE "^[^:]*:(.*)$" "\\\\1" _vt_feats "${_vt_entry}")
        if (_vt_tag STREQUAL "")
            message(FATAL_ERROR "变体 tag 不能为空：'${_vt_entry}'")
        endif()
        string(REPLACE "," ";" _vt_feats "${_vt_feats}")
        ggml_add_cpu_backend_variant(${_vt_tag} ${_vt_feats})
        list(APPEND VT_CPU_VARIANT_TAGS "${_vt_tag}")
        message(STATUS "静态 CPU 变体：${_vt_tag} 特性 [${_vt_feats}]")
    endforeach()
    if (NOT VT_CPU_VARIANT_TAGS)
        message(FATAL_ERROR "GGML_CPU_VARIANTS_STATIC 解析后为空")
    endif()
    # 第一遍「符号捐献」允许只建一个变体、且不带前缀头 —— 前缀清单正是从它反推出来的。
    # 但只要变体数 >1 又没有前缀头，变体之间必然符号重名，报错点会出现在链接期
    # （"duplicate symbol"），离这里十万八千里，所以宁可在配置期就拦下。
    list(LENGTH VT_CPU_VARIANT_TAGS _vt_variant_n)
    if (_vt_variant_n GREATER 1 AND NOT VT_CPU_NS_DIR)
        message(FATAL_ERROR "多个变体必须提供 VT_CPU_NS_DIR：先跑一遍单变体构建当符号捐献者，"
                            "再用 scripts/vt-cpu-variants.py gen 生成前缀头（见 scripts/build-ggml-cpu-variants.sh）")
    endif()
    if (VT_CPU_NS_DIR)
        foreach (_vt_tag IN LISTS VT_CPU_VARIANT_TAGS)
            if (NOT EXISTS "${VT_CPU_NS_DIR}/vt_ns_${_vt_tag}.h")
                message(FATAL_ERROR "缺少 ${VT_CPU_NS_DIR}/vt_ns_${_vt_tag}.h（先跑 scripts/build-ggml-cpu-variants.sh）")
            endif()
        endforeach()
    else()
        message(WARNING "GGML_CPU_VARIANTS_STATIC 只有 ${_vt_variant_n} 个变体且未提供 VT_CPU_NS_DIR："
                        "符号前缀不会注入，本遍产物不能用于多变体链接（符号捐献者专用）。")
    endif()
@@MARK_END@@
elseif (GGML_CPU)
    ggml_add_cpu_backend_variant_impl("")
endif()"""

SRC_TARGET_ANCHOR = "add_library(ggml::ggml ALIAS ggml)"

SRC_TARGET_BLOCK = """@@ANCHOR@@
@@MARK@@
if (GGML_CPU_VARIANTS_STATIC AND VT_CPU_DISPATCH_SRC)
    # 多静态变体时，**未改名**形态的 ggml_backend_cpu_reg() 只由这个文件提供。
    # 它必须待在 ggml 目标里（而不是某个变体里），才能与被改名的变体完全无关。
    target_sources(ggml PRIVATE ${VT_CPU_DISPATCH_SRC})
    target_include_directories(ggml PRIVATE ${VT_CPU_NS_DIR})
endif()
@@MARK_END@@"""

CPU_ELSEIF_ANCHOR = """                elseif(GGML_CPU_ALL_VARIANTS)
                    # Begin with the lowest baseline"""

CPU_ELSEIF_NEW = """                elseif(GGML_CPU_ALL_VARIANTS OR GGML_CPU_VARIANTS_STATIC)
                    # Begin with the lowest baseline"""

CPU_FEATS_ANCHOR = """                    list(APPEND ARCH_FLAGS "-march=${ARM_MCPU}${ARCH_TAGS}")
                    ggml_add_cpu_backend_features(${GGML_CPU_NAME} arm ${ARCH_DEFINITIONS})"""

CPU_FEATS_NEW = """@@MARK@@
                    list(APPEND ARCH_FLAGS "-march=${ARM_MCPU}${ARCH_TAGS}")
                    if (GGML_CPU_ALL_VARIANTS)
                        # 上游 DL 路线：额外编一个不带 arch flag 的 <name>-feats 对象，
                        # 由它导出 ggml_backend_score 给 ggml_backend_load_all 打分。
                        ggml_add_cpu_backend_features(${GGML_CPU_NAME} arm ${ARCH_DEFINITIONS})
                    elseif (GGML_CPU_VARIANTS_STATIC)
                        # 静态路线：不建 feats 对象（没有 dlopen，也没有 score 符号；
                        # 特性探测在我们自己的 vt_cpu_dispatch.c 里做）。
                        # 改为把这个变体的「符号前缀定义」用 -include 注入它的每个 TU。
                        if (VT_CPU_NS_DIR)
                            set(_vt_ns_file "${VT_CPU_NS_DIR}/vt_ns_${GGML_CPU_TAG_NAME}.h")
                            if (NOT EXISTS "${_vt_ns_file}")
                                message(FATAL_ERROR "缺少符号前缀头：${_vt_ns_file}")
                            endif()
                            target_compile_options(${GGML_CPU_NAME} PRIVATE -include "${_vt_ns_file}")
                            message(STATUS "  符号前缀：-include ${_vt_ns_file}")
                        endif()
                    endif()
@@MARK_END@@"""


def fill(text: str, **kw: str) -> str:
    for key, val in kw.items():
        text = text.replace(f"@@{key}@@", val)
    if "@@" in text:
        raise AssertionError(f"未替换的占位符: {text[:200]}")
    return text


def jobs(root: Path):
    return [
        (root / "ggml/CMakeLists.txt",
         [(OPTION_ANCHOR, OPTION_ANCHOR + "\n" + fill(OPTION_BLOCK, MARK=M_OPT, MARK_END=M_OPT_END))],
         M_OPT, M_OPT_END),
        (root / "ggml/src/CMakeLists.txt",
         [(SRC_VARIANTS_ANCHOR, fill(SRC_VARIANTS_BLOCK, MARK=M_SRC, MARK_END=M_SRC_END)),
          (SRC_TARGET_ANCHOR + "\n",
           fill(SRC_TARGET_BLOCK, ANCHOR=SRC_TARGET_ANCHOR, MARK=M_SRC, MARK_END=M_SRC_END) + "\n")],
         M_SRC, M_SRC_END),
        (root / "ggml/src/ggml-cpu/CMakeLists.txt",
         [(CPU_ELSEIF_ANCHOR, CPU_ELSEIF_NEW),
          (CPU_FEATS_ANCHOR, fill(CPU_FEATS_NEW, MARK=M_CPU, MARK_END=M_CPU_END))],
         M_CPU, M_CPU_END),
    ]


def label(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="llama.cpp 静态多变体 CPU 后端补丁")
    ap.add_argument("--llama", default=str(DEFAULT_LLAMA), help="llama.cpp 源码目录")
    ap.add_argument("--apply", action="store_true", help="写入补丁")
    ap.add_argument("--check", action="store_true", help="只检查锚点")
    ap.add_argument("--revert", action="store_true", help="按标记整块删掉")
    args = ap.parse_args()

    root = Path(args.llama)
    if not (root / "ggml/CMakeLists.txt").is_file():
        sys.exit(f"✗ 找不到 llama.cpp 源码：{root}")

    if args.revert:
        # 按「整段替换」反向还原：把 replacement 换回它对应的 anchor。
        # ⚠️ 不能用「标记块整块删除」—— cpu 那处的新块**包含**了原始两行，
        #    整块删掉会把 `list(APPEND ARCH_FLAGS ...)` / `ggml_add_cpu_backend_features(...)`
        #    一起带走，文件就回不到原样了（本项目已经踩过一次，只能从
        #    downloads/llama.tar.gz 重新解出来）。
        for path, pairs, _, _ in jobs(root):
            text = path.read_text()
            original = text
            for anchor, replacement in pairs:
                if replacement in text:
                    text = text.replace(replacement, anchor, 1)
            if text != original:
                path.write_text(text)
                print(f"✓ 已还原: {label(root, path)}")
            else:
                print(f"✓ 无变化: {label(root, path)}")
        return

    bad = 0
    for path, pairs, mark, mark_end in jobs(root):
        name = label(root, path)
        text = path.read_text()
        touched = text
        for anchor, replacement in pairs:
            if replacement in touched:
                continue
            n = touched.count(anchor)
            if n == 0:
                print(f"✗ 锚点缺失: {name} :: {anchor.splitlines()[0][:70]!r}")
                bad += 1
                continue
            if n != 1:
                print(f"✗ 锚点不唯一（{n} 处）: {name} :: {anchor.splitlines()[0][:70]!r}")
                bad += 1
                continue
            touched = touched.replace(anchor, replacement, 1)
        if bad:
            continue
        if mark not in touched or mark_end not in touched:
            # 两条 pair 都该带标记；缺了说明上面某条没落上
            print(f"✗ 块标记不完整: {name}")
            bad += 1
            continue
        if args.check:
            print(f"✓ 待应用: {name}")
        elif touched != text:
            path.write_text(touched)
            print(f"✓ 已打补丁: {name}")
        else:
            print(f"✓ 无变化: {name}")

    if bad:
        sys.exit(1)
    if not (args.check or args.apply):
        print("\n提示：加 --apply 才会写入。")


if __name__ == "__main__":
    main()
