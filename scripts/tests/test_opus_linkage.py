"""盯住 iOS 上「OPUS-MT 专用引擎是活的」。

## 这一条判据为什么值得单独存在

`native/nmt/vt_ort.h` 的 `OrtApiBaseHandle()` 只在定义了 `VT_ORT_STATIC` 时才调
`OrtGetApiBase()`，否则 `return nullptr`。宏由 `ios/Config/Deps.xcconfig`
（`scripts/setup-ios-deps.sh` 生成）提供。

**宏掉了的失败形态是「慢」，不是「报错」**：

    编译 ✅  链接 ✅  安装 ✅  启动 ✅
    只有：vt::Api() == nullptr → VTOpusNmt.isAvailable() == NO
          → OpusTranslationEngine.init 抛 nativeUnavailable
          → Pipeline 静默回落通用模型 → 用户看到「每句都慢」

所以它不可能被任何「能不能编译/能不能启动」的检查抓住，必须单独立判据。

## 反向验证（项目约定：没见它红过的判据不算判据）

`CheckerRejectsBrokenObject` 用**真的编译出来的**两个目标文件对撞：
一个只引用普通符号（对应宏没生效的 `VTOpusNmt.o`），一个引用 `OrtGetApiBase`
（对应宏生效的）。前者必须 exit 1，后者必须 exit 0。
**这条测试如果在某天变成「两边都通过」，说明判据退化了。**
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "scripts"

SETUP_DEPS = SCRIPTS / "setup-ios-deps.sh"
BUILD_IOS = SCRIPTS / "build-ios.sh"
CHECKER = SCRIPTS / "verify-opus-linkage.py"
DEPS_XCCONFIG = ROOT / "ios" / "Config" / "Deps.xcconfig"

EXPECTED_DEFINE = "VT_DEPS_DEFINES = VT_ORT_STATIC"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def effective_lines(text: str) -> list[str]:
    """去掉注释行与空行 —— 只留 xcconfig 里真正生效的赋值。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("//") or s.startswith("#"):
            continue
        out.append(s)
    return out


class GeneratorEmitsTheDefine(unittest.TestCase):
    """`setup-ios-deps.sh --config-only` 必须写出 VT_DEPS_DEFINES。"""

    def test_script_writes_the_define(self):
        text = read(SETUP_DEPS)
        self.assertIn(
            'echo "VT_DEPS_DEFINES = VT_ORT_STATIC"', text,
            "setup-ios-deps.sh 不再写 VT_DEPS_DEFINES —— 重跑依赖后 Deps.xcconfig 会"
            "丢掉这个宏，OPUS 引擎静默死掉（只表现为「每句都慢」）")

    def test_define_is_conditional_on_the_archive(self):
        """宏必须写在「**每个平台**都真的有 libonnxruntime.a」那个分支里。

        否则另一个平台会变成「宏定义了但库不在」→ 链接期
        `Undefined symbols: _OrtGetApiBase` —— 把静默故障换成编译失败，也算退步。

        ⚠️ 判据是**位置关系**，不是「文件里出现过某个词」：
        只 `assertIn("ort_everywhere", text)` 的话，把它改名成 `ort_everywhere_x`
        照样通过（本轮负向验证实测踩过）。
        """
        text = read(SETUP_DEPS)
        probe = '"$VENDOR/sherpa-onnx/lib/$platform/libonnxruntime.a"'
        guard = 'if [ "$ort_everywhere" = "1" ]; then'
        emit = 'echo "VT_DEPS_DEFINES = VT_ORT_STATIC"'

        self.assertIn(probe, text, "前置判断没有去查各平台的 libonnxruntime.a")
        self.assertIn(guard, text, "缺少「各平台都有 ORT 静态库」这个前置判断")

        i_guard = text.index(guard)
        i_emit = text.index(emit)
        self.assertGreater(i_emit, i_guard,
                           "宏不是在「各平台都有 ORT」的分支里写的 —— 会无条件写出去")

        i_else = text.find("未在各平台都找到", i_guard)
        self.assertGreater(i_else, i_emit,
                           "缺少 else 分支：只在部分平台有 ORT 时，"
                           "另一个平台会带着宏去链一个不存在的库")

    def test_config_only_flag_exists(self):
        """`--config-only` 让「改了写配置的逻辑」不必重跑 20 分钟交叉编译。

        没有它，下次有人补这个宏时会选择「手工改 Deps.xcconfig」——
        而那个文件顶上写着「请勿手工维护」，下一次 setup 就冲掉了。

        ⚠️ 只看**非注释行**：`--config-only` 在文件顶部用法注释里也出现，
        按字面量查全文件会被注释骗过（本项目已经栽过一次）。
        """
        arms = [ln.strip() for ln in effective_lines(read(SETUP_DEPS))
                if ln.strip().startswith("--config-only)")]
        self.assertEqual(len(arms), 1,
                         f"case 分支里没有生效的 --config-only（找到 {arms}）")
        self.assertIn("BUILD_SHERPA=0", arms[0],
                      "--config-only 没有关掉 sherpa 构建")
        self.assertIn("BUILD_LLAMA=0", arms[0],
                      "--config-only 没有关掉 llama 构建")


class BuildRunsTheGate(unittest.TestCase):
    """构建脚本必须两道都跑：配置一道 + 产物一道。"""

    def test_build_ios_checks_config(self):
        line = [ln for ln in effective_lines(read(BUILD_IOS))
                if "verify-opus-linkage.py" in ln and "--check-config" in ln]
        self.assertTrue(line,
                        "build-ios.sh 没跑 `verify-opus-linkage.py --check-config`")

    def test_build_ios_checks_products(self):
        line = [ln for ln in effective_lines(read(BUILD_IOS))
                if "verify-opus-linkage.py" in ln and "--build-dir" in ln]
        self.assertTrue(line,
                        "build-ios.sh 没跑 `verify-opus-linkage.py --build-dir` —— "
                        "只验配置证明不了目标文件里真的生效")

    def test_gate_is_top_level_not_nested(self):
        """产物判据不能被别的开关顺手关掉。

        多变体判据整块套在 `if [ "$GATE_VARIANTS" = "1" ]` 里，
        `--no-variant-gate` 会连它一起跳过。专用引擎这件事与指令集派发无关，
        所以它的调用必须是**顶格**（无缩进）的 —— 缩进就说明被某个分支包住了。
        """
        hits = [ln for ln in read(BUILD_IOS).splitlines()
                if "verify-opus-linkage.py" in ln and "--build-dir" in ln]
        self.assertEqual(len(hits), 1, f"期望恰好一处调用，实际 {len(hits)} 处")
        self.assertFalse(hits[0].startswith((" ", "\t")),
                         f"调用被缩进了，说明它嵌在某个条件分支里：{hits[0]!r}")


class ShippedConfigCarriesTheDefine(unittest.TestCase):
    """仓库里这份生成结果也要有（否则干净增量构建拿不到宏）。"""

    def test_deps_xcconfig_has_effective_define(self):
        if not DEPS_XCCONFIG.is_file():
            self.skipTest("ios/Config/Deps.xcconfig 不存在（无原生依赖的克隆上正常）")
        lines = effective_lines(read(DEPS_XCCONFIG))
        if not any("libonnxruntime.a" in ln for ln in lines):
            self.skipTest("这份 Deps.xcconfig 没有链 ORT —— 不该定义该宏")
        self.assertIn(EXPECTED_DEFINE, lines,
                      "Deps.xcconfig 链了 libonnxruntime.a 却没有定义 VT_ORT_STATIC —— "
                      "OPUS 引擎在 iOS 上永远是死的")


@unittest.skipUnless(shutil.which("cc") or shutil.which("clang"),
                     "需要 cc/clang 才能造出用于反向验证的目标文件")
class CheckerRejectsBrokenObject(unittest.TestCase):
    """**反向验证**：判据必须真的会红。

    用真编译出来的两个 .o 对撞。这两个文件刻意做得和 `VTOpusNmt.o` 的两种状态
    一模一样：一个有 `U _OrtGetApiBase`，一个没有。
    """

    @classmethod
    def setUpClass(cls):
        cls.cc = shutil.which("cc") or shutil.which("clang")
        cls.tmp = Path(tempfile.mkdtemp(prefix="vt-opus-linkage-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _compile(self, name: str, body: str) -> Path:
        src = self.tmp / f"{name}.c"
        obj = self.tmp / f"{name}.o"
        src.write_text(body, encoding="utf-8")
        subprocess.run([str(self.cc), "-c", str(src), "-o", str(obj)],
                       check=True, capture_output=True)
        return obj

    def _run_checker(self, *objects: Path) -> subprocess.CompletedProcess:
        args = [sys.executable, str(CHECKER)]
        for obj in objects:
            args += ["--object", str(obj)]
        return subprocess.run(args, capture_output=True, text=True, check=False)

    def test_no_reference_is_red(self):
        """宏没生效的样子：编译单元里没有 OrtGetApiBase。"""
        obj = self._compile("noort", "int vt_dummy(void) { return 0; }\n")
        out = self._run_checker(obj)
        self.assertEqual(
            out.returncode, 1,
            "判据没有对「没有引用 OrtGetApiBase 的目标文件」变红 —— "
            "这就是宏掉了的那个状态，它本该被拦住。\n" + out.stdout + out.stderr)

    def test_reference_is_green(self):
        """宏生效的样子：编译单元里有 `U _OrtGetApiBase`。"""
        obj = self._compile(
            "withort",
            "void OrtGetApiBase(void);\nvoid vt_dummy(void) { OrtGetApiBase(); }\n")
        out = self._run_checker(obj)
        self.assertEqual(
            out.returncode, 0,
            "判据把正常的目标文件判红了 —— 假红同样会让这道闸被绕过。\n"
            + out.stdout + out.stderr)

    def test_mixed_batch_is_red(self):
        """一批里只要有一个坏的就得整体红（多切片构建会同时产出真机/模拟器两份）。"""
        good = self._compile(
            "mixgood",
            "void OrtGetApiBase(void);\nvoid vt_a(void) { OrtGetApiBase(); }\n")
        bad = self._compile("mixbad", "int vt_b(void) { return 1; }\n")
        out = self._run_checker(good, bad)
        self.assertEqual(out.returncode, 1,
                         "混入一个坏目标文件时没有变红\n" + out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
