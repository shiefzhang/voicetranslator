#!/usr/bin/env python3
"""判据：iOS 构建里 OPUS-MT 专用引擎**真的被编进来了**，不是静默死掉。

## 为什么需要这个脚本

`native/nmt/vt_ort.h` 的 `OrtApiBaseHandle()` 有三个分支：

```c
#if defined(VT_ORT_DYNAMIC)      // Android：dlopen("libonnxruntime.so")
  ...
#elif defined(VT_ORT_STATIC)     // iOS：静态链，直接调 OrtGetApiBase()
  return OrtGetApiBase();
#else
  return nullptr;                // ← 宏都没定义时落这里
#endif
```

宏由 `ios/Config/Deps.xcconfig` 的 `VT_DEPS_DEFINES = VT_ORT_STATIC` 提供，
而那个文件是 `scripts/setup-ios-deps.sh` 生成的。**只要这一环掉了**：

    vt::Api() == nullptr
      → VTOpusNmt.isAvailable() == NO
      → OpusTranslationEngine.init 抛 nativeUnavailable
      → Pipeline.loadOpusEngineIfPreferred() 静默返回 nil
      → 每一句都走通用模型 Qwen

**编译、链接、启动、安装全部正常**，只有「中译日 / 中英互译不再走专用引擎」
这一个后果 —— 用户感知到的就是「**每句都慢**」，界面上没有任何提示。
这正是 `Pipeline.swift` 注释里那句「这个选项不该让 App 变砖」的另一面。

## 为什么不能查最终 binary

`libonnxruntime.a` 是被整体链进 App 的（`VT_DEPS_LDFLAGS` 里有它），
所以即便宏不存在，`nm VoiceTranslator.app/VoiceTranslator | grep Ort` 照样一片符号。
**符号在 ≠ 宏在。** 唯一能区分的是**编译单元**：
宏生效时 `VTOpusNmt.o` 里必然有一个未定义引用 `U _OrtGetApiBase`；
宏不生效时那个引用根本不会产生。

## 用法

    python3 scripts/verify-opus-linkage.py --check-config
    python3 scripts/verify-opus-linkage.py --build-dir ios/build
    python3 scripts/verify-opus-linkage.py --object /path/to/VTOpusNmt.o

`--check-config` 只看 `Deps.xcconfig`（快，构建前跑）；`--build-dir` 扫
`Build/Intermediates.noindex/**/Objects-normal/*/VTOpusNmt.o`（真判据，构建后跑）。

**负向验证**：`/tmp/vt-neg/VTOpusNmt-o-broken-*.o` 是补宏之前这次构建的真实产物，
对它跑本脚本必须 exit 1。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

IOS = ROOT / "ios"
DEPS_XCCONFIG = IOS / "Config" / "Deps.xcconfig"
ORT_ARCHIVE = IOS / "third_party" / "sherpa-onnx" / "lib"
PLATFORMS = ("iphoneos", "iphonesimulator")

# 编译单元里必须出现的未定义引用。
REQUIRED_SYMBOL = "_OrtGetApiBase"
OBJECT_NAME = "VTOpusNmt.o"

# 生成器里那行必须**逐字**长这样（xcconfig 语法，不带 -D）。
EXPECTED_DEFINE = "VT_DEPS_DEFINES = VT_ORT_STATIC"


def die(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    sys.exit(1)


def ort_archives_present() -> list[Path]:
    """各平台都存在的 libonnxruntime.a。只要有一个平台缺，就不该定义宏。"""
    return [ORT_ARCHIVE / p / "libonnxruntime.a" for p in PLATFORMS]


def undefined_symbols(obj: Path) -> set[str]:
    """`nm -u` 的未定义符号集合（去掉前导下划线归一化交给调用方）。"""
    try:
        out = subprocess.run(["nm", "-u", str(obj)],
                             capture_output=True, text=True, check=False)
    except FileNotFoundError:
        die("找不到 nm —— 需要 Xcode Command Line Tools")
    if out.returncode != 0 and not out.stdout.strip():
        die(f"nm 读不了 {obj}：{out.stderr.strip()}")
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def check_config() -> int:
    missing = [str(a.relative_to(ROOT)) for a in ort_archives_present() if not a.is_file()]
    if missing:
        # 没有原生依赖的干净克隆：工程照样可编译，这里不算失败。
        print(f"跳过：未找到 libonnxruntime.a（{', '.join(missing)}）—— 无原生依赖的构建，"
              "OPUS 会走降级分支，这是预期状态。")
        return 0

    if not DEPS_XCCONFIG.is_file():
        die(f"各平台的 libonnxruntime.a 都在，但 {DEPS_XCCONFIG.relative_to(ROOT)} 不存在。\n"
            "      这个文件由 scripts/setup-ios-deps.sh 生成 —— 重跑它，"
            "或手工补一行：\n"
            f"          {EXPECTED_DEFINE}")

    text = DEPS_XCCONFIG.read_text(encoding="utf-8")
    # 只看可执行行：注释里出现同样的字串不算（本项目的判据踩过这个坑）。
    effective = [ln for ln in text.splitlines()
                 if ln.strip() == EXPECTED_DEFINE]
    if not effective:
        die(f"{DEPS_XCCONFIG.relative_to(ROOT)} 里没有生效的 `{EXPECTED_DEFINE}`。\n"
            "      后果：vt::Api() 恒为 nullptr → OPUS 专用引擎在 iOS 上永远装不起来\n"
            "            → 「智能选择」每句都静默回落成通用模型（只表现为「慢」，不报错）。\n"
            "      修法：重跑 scripts/setup-ios-deps.sh（它会写这一行），"
            "或直接补上：\n"
            f"          {EXPECTED_DEFINE}")
    print(f"  ✓ {DEPS_XCCONFIG.relative_to(ROOT)} 含 `{EXPECTED_DEFINE}`")
    return 0


def find_objects(build_dir: Path) -> list[Path]:
    return sorted(build_dir.rglob(f"Objects-normal/*/{OBJECT_NAME}"))


def check_objects(objects: list[Path]) -> int:
    if not objects:
        die(f"没有找到任何 {OBJECT_NAME} —— 构建目录里没有产物？\n"
            "      先编一次（scripts/build-ios.sh），或显式传 --object 指向目标文件。")

    bad: list[Path] = []
    for obj in objects:
        rel = obj.relative_to(ROOT) if obj.is_relative_to(ROOT) else obj
        if REQUIRED_SYMBOL in undefined_symbols(obj):
            print(f"  ✓ {rel} 有未定义引用 {REQUIRED_SYMBOL}（宏 VT_ORT_STATIC 生效）")
        else:
            bad.append(obj)
            print(f"  ✗ {rel} 没有引用 {REQUIRED_SYMBOL} —— 宏没生效，OPUS 引擎是死的")

    if bad:
        die(f"{len(bad)} 个编译单元没编出 ORT 入口。\n"
            "      ⚠️ 注意别去查最终 binary —— libonnxruntime.a 被整体链进去，"
            "那里永远有一堆 Ort* 符号。\n"
            "      判据只能看**编译单元**。修法：确认 ios/Config/Deps.xcconfig 里有\n"
            f"          {EXPECTED_DEFINE}\n"
            "      然后**重新编译**（改 xcconfig 不会自动触发重编，要 clean 或 touch 源文件）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-config", action="store_true",
                    help="只检查 Deps.xcconfig（构建前用，很快）")
    ap.add_argument("--build-dir", type=Path, default=None,
                    help="扫描这个构建目录下的 VTOpusNmt.o（构建后用，真判据）")
    ap.add_argument("--object", type=Path, action="append", default=[],
                    help="直接指定目标文件，可重复")
    args = ap.parse_args()

    if not (args.check_config or args.build_dir or args.object):
        ap.error("至少要给一个：--check-config / --build-dir / --object")

    print("==> 判据：OPUS-MT 专用引擎在 iOS 上是活的")

    if args.check_config:
        rc = check_config()
        if rc:
            return rc

    objects = list(args.object)
    if args.build_dir:
        objects += find_objects(args.build_dir)
    if objects:
        return check_objects(objects)

    # 只跑了 --check-config：配置对但没产物可验，明确说清这一步没验到。
    print("  （未指定产物，只验了配置。真判据要 --build-dir。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
