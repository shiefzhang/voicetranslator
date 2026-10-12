r"""守卫：「专用引擎能翻的语向，不必先导入通用翻译模型包」（v0.2.12）。

需求原话：「直接能用 opus 模型的时候不必先导入翻译模型」。

## 它防的是什么

内置集合是 `{asr, opus}`，通用翻译（`translation`）要用户导入 1 GiB。可在
「智能选择」档下，中译英 / 英译中 / 中译日本来就由专用引擎跑完 —— 这时还拦着
用户去导入通用包，等于白等一次大文件传输。

## 判据一览

| # | 判据 | 不满足的后果 |
|---|---|---|
| 1 | 「覆盖」判定必须读**包内 `routes.txt`** | 拿设置页那句策略当能力 → 自制小包放行后装载时才炸 |
| 2 | 判定必须比对**具体语向键**（`contains("zh-ja")`） | 只看「有没有专用包」→ 没覆盖的语向被放行 |
| 3 | 两端门禁都必须由该判定推出（`!translationRequired \|\| translation != nil`） | 放宽没生效（照旧要导入）或恒放行（没包也敢跑） |
| 4 | `start()` 与设置页提示必须调**同一个**判定 | 设置页说能用、点开始报缺包 |
| 5 | `Pipeline` 允许通用包缺席，但缺席时**必须报错说清** | 静默变砖：用户看到「加载模型失败」，不知道该去导入 |

⚠️ 判据 1/2 特别容易写成假绿灯：`assertIn("routes.txt", text)` 会被**注释**满足
（本仓库的注释里到处在讲 routes.txt）。所以下面一律**先切出函数体再判**，
第 3/4 条则用「调用点计数」而不是「出现过」。

反向验证：把任一条判据对应的实现改坏，
`python3 -m unittest test_translation_shortcut` 必须变红。**没见它红过的判据不算判据。**
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

APP_MODEL = ROOT / "ios" / "VoiceTranslator" / "App" / "AppModel.swift"
SETTINGS = ROOT / "ios" / "VoiceTranslator" / "Views" / "SettingsView.swift"
PIPELINE_SWIFT = ROOT / "ios" / "VoiceTranslator" / "Core" / "Pipeline.swift"
PIPELINE_JAVA = ROOT / "app" / "src" / "main" / "java" / "com" / "vt" / "voicetranslator" / "Pipeline.java"
MODEL_PACKAGES_SWIFT = ROOT / "ios" / "VoiceTranslator" / "Models" / "ModelPackages.swift"
MODEL_PACKAGES_JAVA = ROOT / "app" / "src" / "main" / "java" / "com" / "vt" / "voicetranslator" / "ModelPackages.java"
ENGINE_TYPES = ROOT / "ios" / "VoiceTranslator" / "Engines" / "EngineTypes.swift"
ACTIVITY = ROOT / "app" / "src" / "main" / "java" / "com" / "vt" / "voicetranslator" / "MainActivity.java"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def body_of(text: str, start_marker: str) -> str:
    """截出 `start_marker` 那一段（到下一个 4 空格缩进的 `}` 为止）。

    全程用这个而不是对整篇 `assertIn` —— 注释里会把同样的词写一遍，
    对整篇判等于让注释替代码过审。
    """
    start = text.find(start_marker)
    if start < 0:
        raise AssertionError(f"找不到 {start_marker!r}")
    end = text.find("\n    }", start)
    return text[start:] if end < 0 else text[start:end]


class OpusCoverageIsReadFromThePackage(unittest.TestCase):
    """判据 1/2：覆盖范围只能来自包内 routes.txt，且必须比对具体语向键。"""

    def test_swift_reads_routes_txt_inside_the_package(self):
        swift = read(MODEL_PACKAGES_SWIFT)
        self.assertIn('appendingPathComponent("routes.txt")', swift,
                      "iOS 的覆盖判定没有读包内 routes.txt —— 那是端侧 C++ 真正据以"
                      "装载单包的表，别的来源都会与它漂移")

    def test_swift_compares_the_actual_direction_key(self):
        swift = read(MODEL_PACKAGES_SWIFT)
        body = body_of(swift, "static func opusCovers(")
        self.assertIn("prefersOpus", body, "第三层判定没先问设置档位与语向")
        self.assertIn("routes.contains(", body,
                      "opusCovers 没有按语向键比对 —— 只判「有没有专用包」的话，"
                      "一个只带 zh-en 的包会把中译日也放行")
        self.assertIn("-\\(target)", body)

    def test_java_reads_routes_txt_inside_the_package(self):
        java = read(MODEL_PACKAGES_JAVA)
        self.assertIn('new File(directory,"routes.txt")', java,
                      "Android 的覆盖判定没有读包内 routes.txt")

    def test_java_compares_the_actual_direction_key(self):
        java = read(MODEL_PACKAGES_JAVA)
        body = body_of(java, "static boolean opusCovers(")
        self.assertIn("prefersOpus", body)
        self.assertIn("routes.contains(", body, "同 test_swift_compares_the_actual_direction_key")
        self.assertIn('source+"-"+target', body)

    def test_manifest_routes_field_is_not_used_as_coverage(self):
        """manifest 的 `routes` 是**跳数**（pack-opus-models.py 写 declared_hops），
        两跳语向在里面根本不出现 —— 拿它当覆盖范围会漏掉整条 pivot。

        判据：覆盖判定那一段里不许出现 manifest / readManifest。
        """
        for path, marker in ((MODEL_PACKAGES_SWIFT, "static func opusRoutes("),
                             (MODEL_PACKAGES_JAVA, "static Set<String> opusRoutes(")):
            body = body_of(read(path), marker)
            for forbidden in ("manifest", "Manifest", "readManifest"):
                self.assertNotIn(forbidden, body,
                                 f"{path.name} 的 routes 解析用到了 {forbidden}："
                                 "manifest 的 routes 是跳数，不是语向键")


class TheGateActuallyUsesTheVerdict(unittest.TestCase):
    """判据 3：算出判定就得用，且不能反过来变成「永不要求通用包」。"""

    def test_ios_gate_is_derived_from_the_verdict(self):
        """iOS 的 `translationRequired` 必须**一路追到** `opusCovers`。

        v1.0.0 起 `start()` 不再内联写 `!ModelPackages.opusCovers(...)`，而是调
        `translationPackageRequired(on:)` —— 因为界面那条「缺通用模型包」提示条
        要用**同一个**判定，免得出现「提示条说不用导入、点开始却被拦」。

        于是判据分两跳，缺一不可：

          ① `let translationRequired` 由那个入口赋值（不是 `= false` 这种恒放行）；
          ② 那个入口的**函数体**里真的出现 `!ModelPackages.opusCovers(`。

        只判 ① → 入口返回 false 也能过；只判 ② → `start()` 根本没用它也能过。
        两跳都判才与改动前等价（改动前判的就是内联的那一句）。
        """
        swift = read(APP_MODEL)
        start = swift.find("let translationRequired")
        self.assertGreater(start, 0, "AppModel 里没有 translationRequired —— 门禁没放宽")
        self.assertIn("translationPackageRequired(on: page)", swift[start:start + 200],
                      "translationRequired 不再由 translationPackageRequired 推出")
        verdict = body_of(swift, "func translationPackageRequired(")
        self.assertIn("!ModelPackages.opusCovers(", verdict,
                      "translationPackageRequired 不是由 opusCovers 推出来的")
        self.assertIn("guard let asr, !translationRequired || translation != nil else", swift,
                      "门禁没带上 translationRequired —— 要么放宽没生效，"
                      "要么恒放行（没包也敢跑）")

    def test_ios_gate_is_not_reverted_to_always_require(self):
        swift = read(APP_MODEL)
        self.assertNotIn("guard let asr, let translation else", swift,
                         "门禁退回「永远要求通用翻译模型包」了 —— 这就是本轮的退化点")

    def test_android_gate_is_derived_from_the_verdict(self):
        java = read(ACTIVITY)
        self.assertIn("final boolean translationRequired=!ModelPackages.opusCovers(", java,
                      "Android 的 translationRequired 不是由 opusCovers 推出来的")
        self.assertIn("if(asr==null||(translationRequired&&mt==null))", java,
                      "Android 门禁没带上 translationRequired")

    def test_android_gate_is_not_reverted_to_always_require(self):
        java = read(ACTIVITY)
        self.assertNotIn("if(asr==null||mt==null)", java,
                         "门禁退回「永远要求通用翻译模型包」了")


class SettingsAndGateShareOneVerdict(unittest.TestCase):
    """判据 4：两处各写一套必然各说一套。"""

    def test_ios_settings_reuses_the_verdict(self):
        settings = read(SETTINGS)
        body = body_of(settings, "private var opusCoversCurrentDirection")
        self.assertIn("ModelPackages.opusCovers(", body,
                      "设置页的提示文案没有走 opusCovers（自己另写了一套语向表）—— "
                      "会出现「设置页说能用、点开始却报缺包」")
        self.assertIn("opusCoversCurrentDirection", settings.replace(body, ""))

    def test_android_calls_the_helper_exactly_twice(self):
        java = read(ACTIVITY)
        self.assertEqual(java.count("ModelPackages.opusCovers("), 2,
                         "Android 侧 opusCovers 的调用点数不是 2（start 的门禁 + 设置页的提示）——"
                         "少一处就会各说一套，多一处说明有第三份判定")


class PipelineFailsLoudlyWithoutAGenericModel(unittest.TestCase):
    """判据 5：允许缺席，但缺席时必须说清缺什么。"""

    def test_swift_throws_a_specific_error(self):
        body = body_of(read(PIPELINE_SWIFT), "private func loadTranslationModel()")
        self.assertIn("translationDirectory", body)
        self.assertIn("translationPackageMissing", body,
                      "专用引擎用不上又没有通用包时没有专门的错误 —— "
                      "会被报成「找不到 model.gguf」，用户不知道该去导入")

    def test_swift_error_message_names_the_missing_piece(self):
        engine = read(ENGINE_TYPES)
        self.assertIn("case translationPackageMissing(String)", engine)
        self.assertIn("case .translationPackageMissing(let route):", engine,
                      "新增的 error case 没有对应的文案 —— 走到那条路的用户只会看到空白原因")

    def test_java_throws_a_specific_error(self):
        body = body_of(read(PIPELINE_JAVA), "private void loadTranslationModel(){")
        self.assertIn("mtDir==null||mtEngine==null", body)
        self.assertIn("IllegalStateException", body,
                      "Android 侧没有专门的错误 —— 会被 LocalLlm 报成「加载模型失败」")

    def test_java_pipeline_accepts_a_null_generic_package(self):
        java = read(PIPELINE_JAVA)
        self.assertIn("mtEngine=mt==null?null:ModelPackages.engine(mt)", java,
                      "构造期把通用包当必需项了 —— 没有它 Pipeline 根本建不起来，"
                      "放宽门禁等于白改")


if __name__ == "__main__":
    unittest.main()
