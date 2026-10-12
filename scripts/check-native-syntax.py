#!/usr/bin/env python3
"""native 侧 C++ 源码的语法检查 —— 没有 Android NDK 也能跑。

## 为什么需要它

`app/src/main/cpp/vt_opus_jni.cpp` 在开发机上**从来没有被编译过**：编 APK 要
Android SDK + NDK + JDK，本机一个都没有。而它是 Java 与推理核心之间唯一的接口层，
写错了要到真机才炸。`native/nmt/vt_opus.cc` 虽然被 `verify-opus-package.py` 编过，
但那是「先编了再跑」，出错也没有干净的报错出口。

这个脚本只做 `-fsyntax-only`，用假 `jni.h` 补齐 JNI 那一层，把
「语法 / 拼写 / 类型不匹配」这类错误提前暴露。

## 能力边界（别高估它）

- ✅ 语法错误、拼错的名字、参数个数/类型不对、`const` 用错、缺 include。
- ❌ 符号的**链接**是否存在（`-fsyntax-only` 不链接）；ABI、ARM 指令集、
  NDK 版本差异一概查不到。
- ⚠️ `jni.h` 是**替身**，只有本文件用到的那些成员。它按需生成到临时目录，
  **不入库** —— 免得哪天被误加进 include 路径，把真的 NDK 头文件遮蔽掉。

所以它是「廉价前置网」，真正的验收仍然是 `scripts/build-ios.sh`（iOS 全量编）
与设备上的实跑。

## 用法

    python3 scripts/check-native-syntax.py

退出码非 0 表示有语法错误。
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NMT = REPO / "native" / "nmt"
ORT = NMT / "third_party" / "onnxruntime"
SSP = NMT / "third_party" / "simple-sentencepiece"
APP_CPP = REPO / "app" / "src" / "main" / "cpp"

# ---------------------------------------------------------------------------
# 最小 jni.h 替身。只声明 vt_opus_jni.cpp 真正用到的成员；多写一个字都会让人
# 误以为它是个完整的头文件。放临时目录、用完即删。
# ---------------------------------------------------------------------------
JNI_STUB = r"""
// 语法检查用的最小 jni.h 替身 —— 不是真正的 JNI 头文件，不要在任何构建里引用。
#ifndef VT_CHECK_JNI_STUB_H_
#define VT_CHECK_JNI_STUB_H_

#include <cstdint>

typedef int8_t  jbyte;
typedef int32_t jint;
typedef uint8_t jboolean;
typedef int32_t jsize;
typedef void   *jobject;
typedef jobject jclass;
typedef jobject jarray;
typedef jarray  jbyteArray;

#ifndef JNI_TRUE
#define JNI_TRUE 1
#endif
#ifndef JNI_FALSE
#define JNI_FALSE 0
#endif
#define JNIEXPORT __attribute__((visibility("default")))
#define JNICALL

struct JNIEnv {
  jclass FindClass(const char *name);
  jint ThrowNew(jclass clazz, const char *message);
  jsize GetArrayLength(jarray array);
  void GetByteArrayRegion(jbyteArray array, jsize start, jsize len, jbyte *buf);
  jbyteArray NewByteArray(jsize length);
  void SetByteArrayRegion(jbyteArray array, jsize start, jsize len,
                          const jbyte *buf);
};

#endif  // VT_CHECK_JNI_STUB_H_
"""

# 每个目标：(相对路径, 额外的 include 目录, 额外的编译参数, 是否抑制警告)
#
# 标准统一用 gnu++17：Android 侧的 CMakeLists.txt 全局设了 CMAKE_CXX_STANDARD 17，
# iOS 侧 gen-ios-project.py 给这同一批文件打的是 -std=gnu++17。少了这一条，
# vendored 的 threadpool.h 会在 C++20 下因为 std::result_of 被移除而炸 —— 踩过一次。
TARGETS = [
    (
        APP_CPP / "vt_opus_jni.cpp",
        [NMT, ORT],
        ["-DVT_ORT_DYNAMIC"],
        False,
    ),
    (
        NMT / "vt_opus.cc",
        [NMT, ORT, SSP],
        ["-DVT_ORT_DYNAMIC"],
        False,
    ),
    (
        SSP / "ssentencepiece" / "csrc" / "ssentencepiece.cc",
        # 它自己引的是 `ssentencepiece/csrc/ssentencepiece.h`（自 SSP 根起算），
        # 内部的 darts.h / threadpool.h 又是同目录裸名引用，所以两个都要加。
        [SSP / "ssentencepiece" / "csrc", SSP],
        [],
        True,  # 第三方源码，警告与我们无关，刷屏会盖住真问题
    ),
]


def main() -> int:
    clang = shutil.which("clang++")
    if clang is None:
        print("找不到 clang++，无法做 native 语法检查", file=sys.stderr)
        return 1

    stub_dir = Path(tempfile.mkdtemp(prefix="vt-jni-stub-"))
    failures = 0
    try:
        (stub_dir / "jni.h").write_text(JNI_STUB, encoding="utf-8")

        for path, includes, defines, quiet in TARGETS:
            if not path.is_file():
                print(f"{path}: 文件不存在", file=sys.stderr)
                failures += 1
                continue
            command = [
                clang, "-std=gnu++17", "-fsyntax-only",
                "-Wno-deprecated-declarations",
                "-w" if quiet else "-Wall",
                *defines,
                f"-I{stub_dir}",
                *[f"-I{d}" for d in includes],
                str(path),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            label = path.relative_to(REPO)
            if result.returncode == 0:
                print(f"✓ {label}")
                continue
            failures += 1
            print(f"✗ {label}")
            # 只印编译器自己的诊断。命令行原样打出来便于手工复跑 —— 报错里
            # 的 `jni.h` 路径是临时目录，直接复制命令重跑会失效，所以标出来。
            for line in result.stderr.rstrip().splitlines():
                print(f"    {line}")
            print(f"    （复跑：上面这条命令；jni.h 替身在 {stub_dir}）")
    finally:
        shutil.rmtree(stub_dir, ignore_errors=True)

    if failures:
        print(f"\n✗ {failures} 个文件语法检查失败")
        return 1
    print(f"\n✓ {len(TARGETS)} 个 native 源文件的语法都通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
