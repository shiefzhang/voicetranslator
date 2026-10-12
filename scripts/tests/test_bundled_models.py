"""守卫：**内置集合恰好是 `{asr, opus}`**（v0.2.10 起）。

需求原话：「OPUS-MT 的翻译模型需要内置，转写模型也要内置，只有翻译模型需要自行导入」。
于是「谁内置」不是「取体积最小的那些」，而是一条**写死的规则**：

| kind | 内置？ | 依据 |
|---|---|---|
| `asr` 转写 | ✅ 229 MiB | 没有它整个 App 都跑不起来，必须开箱可用 |
| `opus` 专用翻译 | ✅ 548 MiB | 体积可控，且它决定「智能选择」档能不能用上 |
| `translation` 通用翻译 | ❌ 1.04 GiB | 三份里最大，内置会让两端安装包翻倍 → 用户导入 |

**为什么需要这个文件**：内置跨四层 —— 脚本表 / 两端源码 / 两端产物 / 工程文件 ——
而这几层里**只有工程文件那一层会编译失败**。最阴的坏法是「多内置了一份」：
一切正常工作，只是安装包凭空大 1 GiB，没有任何报错。所以每条判据都对着一个具体坏法写。

判据一览：

| # | 判据 | 不满足的后果 |
|---|---|---|
| 1 | `model-tiers.py` 的 `BUNDLED` ⊆ `RECOMMENDED`，与 `NEVER_BUNDLED` 不相交 | 「内置哪一档」两处各说一套 |
| 2 | 两端产物的 kind 集合**恰好**等于 `BUNDLED` | 客户端多装 / 少装一份 |
| 3 | `translation` 的包**不许**出现在 `ios/BundledModels/` 或 assets 里 | 安装包凭空大 1 GiB |
| 4 | 两端的 `hasBundled` / `isBundled` 与 `BUNDLED` 一致 | 代码说内置了、产物里却没有（或反之）→ 点了没反应 |
| 5 | `selected = installed ?? bundled` 是唯一入口 | 内置那份永远用不到（专用引擎静默失效） |
| 6 | 工程里恰好 1 条 `BundledModels`，且是 `folder` 而非 group | group 会把目录压平 → `manifest.json` 撞名、静默覆盖 |
| 7 | `index.json` 恰好列出 `BUNDLED` | App 读到空索引 → 三行全「未就绪」 |

⚠️ **产物类判据在这些目录不存在时 skip 而不是 fail**：`ios/BundledModels/` 与
`app/src/main/assets/bundled-models/` 都在 `.gitignore` 里（生成物，合计约 1.6 GB），
干净克隆上本来就没有。它们在真机/构建机器上才存在 —— 那台机器上这几条必须真跑。

反向验证：把任一条判据写坏，`python3 -m unittest scripts.tests.test_bundled_models`
必须变红。**没见它红过的判据不算判据。**
"""

import importlib.util
import json
import plistlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# `model-tiers.py` 里 `from model_pack import zip64_info` —— 加载它之前要让
# `scripts/` 可导入（本文件在 scripts/tests/ 下，不会自动带上）。
sys.path.insert(0, str(ROOT / "scripts"))

# 「必须内置」与「刻意永不内置」——把需求写进断言，而不是只写在注释里。
REQUIRED_BUNDLED = {"asr", "opus"}
NEVER_BUNDLED = {"translation"}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_tiers():
    """加载 `scripts/model-tiers.py`（文件名带连字符，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(
        "vt_model_tiers", ROOT / "scripts" / "model-tiers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def app_sources(*suffixes: str):
    for base in (ROOT / "app" / "src", ROOT / "ios" / "VoiceTranslator"):
        for path in base.rglob("*"):
            if path.suffix in suffixes and path.is_file():
                yield path


IOS_BUNDLED = ROOT / "ios" / "BundledModels"
ANDROID_BUNDLED = ROOT / "app" / "src" / "main" / "assets" / "bundled-models"


class BundledSetIsPinned(unittest.TestCase):
    """第 1 条：内置集合是规则，不是「恰好配了这些」。"""

    def setUp(self):
        self.tiers = load_tiers()

    def test_bundled_is_subset_of_recommended(self):
        unknown = set(self.tiers.BUNDLED) - set(self.tiers.RECOMMENDED)
        self.assertEqual(
            unknown, set(),
            f"BUNDLED 里的 {sorted(unknown)} 不在 RECOMMENDED 里 —— "
            "内置的必须也是推荐档位，否则「内置哪一档」会在两处各说一套")

    def test_bundled_and_never_bundled_do_not_overlap(self):
        clash = set(self.tiers.BUNDLED) & set(self.tiers.NEVER_BUNDLED)
        self.assertEqual(clash, set(), f"{sorted(clash)} 同时出现在两张表里")

    def test_bundled_set_is_exactly_asr_and_opus(self):
        self.assertEqual(set(self.tiers.BUNDLED), REQUIRED_BUNDLED,
                         "内置集合变了。改内置集合请同时改本文件的 REQUIRED_BUNDLED，"
                         "并在 README 里写清体积代价")
        self.assertEqual(set(self.tiers.NEVER_BUNDLED), NEVER_BUNDLED,
                         "刻意不内置的集合变了 —— translation 不内置是本轮需求的核心")

    def test_translation_is_never_bundled(self):
        self.assertIn("translation", self.tiers.NEVER_BUNDLED)
        self.assertNotIn("translation", self.tiers.BUNDLED)
        assets = self.tiers.ANDROID_BUNDLED_DIR / "translation.vtmodel"
        self.assertFalse(assets.is_file(),
                         f"{assets.relative_to(ROOT)} 存在 —— 通用翻译不许内置（约 1 GiB）")


class EmittedArtifactsMatchTheTable(unittest.TestCase):
    """第 2/3/7 条：真正写出去的产物必须与表一致。"""

    def setUp(self):
        self.tiers = load_tiers()
        self.expected = set(self.tiers.BUNDLED)

    def test_ios_kind_directories_exactly_match(self):
        if not IOS_BUNDLED.is_dir():
            self.skipTest("ios/BundledModels/ 不存在（干净克隆上正常，本机跑 --emit 后就有）")
        actual = {p.name for p in IOS_BUNDLED.iterdir() if p.is_dir()}
        self.assertEqual(actual, self.expected,
                         "ios/BundledModels/ 下的 kind 目录与 BUNDLED 不一致")

    def test_ios_index_lists_exactly_the_bundled_set(self):
        index = IOS_BUNDLED / "index.json"
        if not index.is_file():
            self.skipTest("ios/BundledModels/index.json 不存在（干净克隆上正常）")
        entries = json.loads(index.read_text(encoding="utf-8"))
        self.assertIsInstance(entries, list,
                              "index.json 顶层必须是数组 —— iOS 的 bundledIndex 就是这么解它的；"
                              "改成对象会让 App 静默读到空索引、以为「没有内置包」")
        self.assertEqual({e["kind"] for e in entries}, self.expected)
        for entry in entries:
            self.assertTrue(entry.get("id") and entry.get("name"),
                            f"index.json 的条目缺 id/name：{entry}")

    def test_android_assets_exactly_match(self):
        if not ANDROID_BUNDLED.is_dir():
            self.skipTest("assets/bundled-models/ 不存在（干净克隆上正常）")
        actual = {p.stem for p in ANDROID_BUNDLED.glob("*.vtmodel")}
        self.assertEqual(actual, self.expected,
                         "assets/bundled-models/ 下的 .vtmodel 与 BUNDLED 不一致")

    def test_never_bundled_kinds_absent_from_both_ends(self):
        """第 3 条单独再盯一遍 —— 这是本轮需求的核心，值得两条独立判据。"""
        for kind in self.tiers.NEVER_BUNDLED:
            self.assertFalse(
                (IOS_BUNDLED / kind).is_dir(),
                f"ios/BundledModels/{kind}/ 存在，但 {kind} 在 NEVER_BUNDLED 里")
            self.assertFalse(
                (ANDROID_BUNDLED / f"{kind}.vtmodel").is_file(),
                f"assets/bundled-models/{kind}.vtmodel 存在，但 {kind} 在 NEVER_BUNDLED 里")

    def test_verify_mode_agrees(self):
        """`--verify` 是 build-ios.sh 用的那道闸，它必须与这里同结论。"""
        if not IOS_BUNDLED.is_dir() or not ANDROID_BUNDLED.is_dir():
            self.skipTest("内置产物不完整，--verify 无从谈起（干净克隆上正常）")
        self.assertEqual(self.tiers.verify_emitted(), 0)


class AppCodeKnowsTheSameSet(unittest.TestCase):
    """第 4/5 条：两端的代码必须按同一张表回落内置。"""

    def test_android_has_bundled_table_matches(self):
        java = read(ROOT / "app" / "src" / "main" / "java" / "com" / "vt"
                    / "voicetranslator" / "ModelPackages.java")
        start = java.find("static boolean hasBundled(String kind)")
        self.assertGreater(start, 0, "Android 侧没有 hasBundled —— 内置回落不见了")
        body = java[start:java.find("}", start)]
        self.assertIn('"asr"', body)
        self.assertIn('"opus"', body)
        self.assertNotIn('"translation"', body,
                         "hasBundled 里出现了 translation —— 通用翻译不许回落内置")

    def test_active_package_is_installed_then_bundled(self):
        """第 5 条：`selected = installed ?? bundled` 必须是两端唯一的取包入口。

        这一条特别容易「半对」：`selected` 写对了，但某个调用点直接调 `installed`，
        于是内置那份在**那个调用点**永远看不到 —— 专用引擎静默失效就是这么来的。
        """
        swift = read(ROOT / "ios" / "VoiceTranslator" / "Models" / "ModelPackages.swift")
        self.assertIn("static func installed(", swift)
        self.assertIn("static func bundled(", swift)
        self.assertIn("static func selected(", swift)
        self.assertIn("installed(kind) ?? bundled(kind)", swift)
        self.assertIn("enum Source", swift)
        self.assertIn("case bundled", swift)

        java = read(ROOT / "app" / "src" / "main" / "java" / "com" / "vt"
                    / "voicetranslator" / "ModelPackages.java")
        self.assertIn("static File installed(Context c,String kind)", java)
        self.assertIn("static File bundled(Context c,String kind)", java)
        self.assertIn("static File selected(Context c,String kind)", java)

    def test_pipeline_asks_for_the_active_package_not_the_imported_one(self):
        """流水线里取专用包必须走 `selected`，不能走 `installed`。

        踩过同类坑：改成 `installed(.opus)` 之后内置的专用包永远取不到，
        症状是「设置成智能选择但一直在用通用模型」—— 不报错，只是变慢。
        """
        swift = read(ROOT / "ios" / "VoiceTranslator" / "Core" / "Pipeline.swift")
        self.assertIn("ModelPackages.selected(.opus)", swift)
        self.assertNotIn("ModelPackages.installed(.opus)", swift)

    def test_ios_reads_the_index_instead_of_hardcoding(self):
        swift = read(ROOT / "ios" / "VoiceTranslator" / "Models" / "ModelPackages.swift")
        self.assertIn('"index.json"', swift,
                      "iOS 应该从 index.json 动态得知内置了哪几类，不要写死在代码里")

    def test_first_launch_reports_missing_packages_by_name(self):
        """通用翻译没有内置可回落，首启必须**说清缺的是它**。"""
        swift = read(ROOT / "ios" / "VoiceTranslator" / "App" / "AppModel.swift")
        self.assertIn("还缺\\(missing)模型包", swift)

        java = read(ROOT / "app" / "src" / "main" / "java" / "com" / "vt"
                    / "voicetranslator" / "MainActivity.java")
        self.assertIn('"还缺"+missing+"模型包', java)


class XcodeProjectShipsBundledModels(unittest.TestCase):
    """第 6 条：工程文件那一层 —— 唯一会编译失败的一层，但也最容易「静默不打包」。"""

    def setUp(self):
        self.pbxproj = ROOT / "ios" / "VoiceTranslator.xcodeproj" / "project.pbxproj"
        if not self.pbxproj.is_file():
            self.skipTest("没有生成 Xcode 工程（干净克隆上正常）")
        self.text = read(self.pbxproj)

    def test_bundled_is_in_the_resources_phase(self):
        """只有列进 Resources 阶段的 `files = (…)` 才会被真正拷进 `.app`。

        ⚠️ 判据不能写成 `assertIn("BundledModels", text)` —— 工程里 `BundledModels`
        这个字面量出现在四处（文件引用对象、构建文件对象、分组 children、Resources 阶段），
        把**真正决定打包的那一条**删掉，按字面量的判据照样通过，而产物里一个文件都没有。
        所以只能对着 Resources 阶段那一段里数。
        """
        if not IOS_BUNDLED.is_dir():
            self.skipTest("ios/BundledModels/ 不存在（干净克隆上正常）")
        phase = self.text.split("/* Begin PBXResourcesBuildPhase section */")[1]
        phase = phase.split("/* End PBXResourcesBuildPhase section */")[0]
        self.assertIn("BundledModels in Resources", phase,
                      "Resources 阶段里没有 BundledModels —— xcodebuild 照编，"
                      "只是**静默不打包**：.app 里不会出现 BundledModels/，"
                      "App 启动后设置页三行全「未就绪」。"
                      "修法：python3 scripts/gen-ios-project.py")

    def test_it_is_a_folder_reference_not_a_group(self):
        """必须是 folder reference。

        group 会把目录**压平**成 bundle 根，于是 asr/manifest.json 与
        opus/manifest.json 撞名，后一个静默覆盖前一个 —— 构建期完全看不出来。
        """
        if not IOS_BUNDLED.is_dir():
            self.skipTest("ios/BundledModels/ 不存在（干净克隆上正常）")
        marker = "/* BundledModels */ = {"
        start = self.text.find(marker)
        self.assertGreater(start, 0, "找不到 BundledModels 的文件引用对象")
        block = self.text[start:self.text.find("};", start)]
        self.assertIn("lastKnownFileType = folder;", block,
                      "BundledModels 不是 folder reference —— 参见本测试的 docstring")
        self.assertNotIn("folder.assetcatalog", block,
                         "BundledModels 被当成 Asset Catalog 了")

    def test_resources_phase_entry_count(self):
        """Resources 阶段的条目数必须是「可预期的那个数」。

        不用字面量判据（注释里也会提到 BundledModels），而是数**实际条目**：
        期望 = Assets.xcassets（若存在）+ BundledModels（若存在/应存在）
             + PrivacyInfo.xcprivacy（若存在）。

        ⚠️ 期望值是**跟着被测文件一起算**的，所以隐私清单被删时这条不会红 ——
        「它必须存在、且必须真的进了 bundle」由下面的
        `PrivacyManifestShipsInTheApp` 独立兜底。判据不能跟着被测对象漂。
        """
        phase = self.text.split("/* Begin PBXResourcesBuildPhase section */")[1]
        phase = phase.split("/* End PBXResourcesBuildPhase section */")[0]
        entries = [line.strip() for line in phase.splitlines()
                   if line.strip()[:1] in "0123456789ABCDEF" and line.strip().endswith(",")]
        expected = 0
        if (ROOT / "ios" / "Assets.xcassets").is_dir():
            expected += 1
        if IOS_BUNDLED.is_dir():
            expected += 1
        if (ROOT / "ios" / "PrivacyInfo.xcprivacy").is_file():
            expected += 1
        self.assertEqual(
            len(entries), expected,
            f"Resources 阶段有 {len(entries)} 个条目，期望 {expected}：{entries}")


class BundlingPipelineTargetsAsrAndOpusOnly(unittest.TestCase):
    """脚本那一层：谁负责写内置、谁负责拦。"""

    def test_build_ios_runs_the_verify_gate(self):
        """那道闸必须真的**被调用**，而不是只在注释里被提到。

        ⚠️ 判据不能写成对全文 `assertIn("--verify", text)`：注释里也有 `--verify`
        三个字（"这里不重复那张表 —— 调 `--verify` 让它自己比对"），
        于是把真正的调用点删掉，按全文的判据照样通过 —— 闸静默失效。
        """
        text = read(ROOT / "scripts" / "build-ios.sh")
        self.assertNotIn("bundle-models.py", text,
                         "build-ios.sh 还在调已删除的 bundle-models.py")
        calls = [line for line in text.splitlines()
                 if "model-tiers.py" in line and not line.lstrip().startswith("#")]
        self.assertTrue(calls, "build-ios.sh 里没有任何一行真的调用 model-tiers.py")
        self.assertTrue(
            any("--verify" in line for line in calls),
            "build-ios.sh 调了 model-tiers.py 却没带 --verify —— 那道闸等于没有。"
            "内置集合漂掉时**不会编译失败**，只能靠它：\n" + "\n".join(calls))

    def test_old_bundler_is_gone(self):
        self.assertFalse((ROOT / "scripts" / "bundle-models.py").exists(),
                         "scripts/bundle-models.py 又出现了 —— 现在只有 model-tiers.py 负责写内置")

    def test_gen_ios_project_wires_the_folder_reference(self):
        text = read(ROOT / "scripts" / "gen-ios-project.py")
        for marker in ("has_bundled", "bundled_uuid", "bundled_build_uuid"):
            self.assertIn(marker, text,
                          f"gen-ios-project.py 里没有 {marker}：内置包没被织进工程")
        self.assertIn("lastKnownFileType = folder;", text,
                      "生成器不会产出 folder reference —— 会在 bundle 里被压平")

    def test_emitter_is_the_only_writer_and_is_gated(self):
        """`--emit` 是唯一往 App 目录写东西的地方，且必须被两张表的冲突检查挡住。"""
        text = read(ROOT / "scripts" / "model-tiers.py")
        self.assertIn("def emit(", text)
        self.assertIn("NEVER_BUNDLED", text)
        # emit 必须真的建两端的目录
        self.assertIn("IOS_BUNDLED_DIR", text)
        self.assertIn("ANDROID_BUNDLED_DIR", text)

    def test_app_sources_do_not_write_bundle_paths(self):
        """目录名是跨四层的字符串契约，四处必须字面一致。

        脚本建哪个目录、工程引用哪个、App 读哪个 —— 任一处拼错都**不会编译失败**，
        只会让 `bundled()` 返回 nil，表现为设置页显示「未就绪」而产物明明在。
        """
        tiers = load_tiers()
        ios_name = tiers.IOS_BUNDLED_DIR.name          # BundledModels
        android_name = tiers.ANDROID_BUNDLED_DIR.name  # bundled-models

        java = read(ROOT / "app" / "src" / "main" / "java" / "com" / "vt"
                    / "voicetranslator" / "ModelPackages.java")
        self.assertIn(f'BUNDLED_DIR="{android_name}"', java,
                      f"Android 的 BUNDLED_DIR 与脚本建目录名 {android_name!r} 不一致")

        swift = read(ROOT / "ios" / "VoiceTranslator" / "Models" / "ModelPackages.swift")
        self.assertIn(f'"{ios_name}"', swift,
                      f"iOS 的 bundledRoot 与脚本建目录名 {ios_name!r} 不一致")

        pbxproj = ROOT / "ios" / "VoiceTranslator.xcodeproj" / "project.pbxproj"
        if pbxproj.is_file():
            self.assertIn(f"path = {ios_name};", read(pbxproj),
                          f"工程里引用的目录名不是 {ios_name!r}")


class PrivacyManifestShipsInTheApp(unittest.TestCase):
    """守卫：隐私清单必须在、必须真的进 bundle（App Store 上架的硬门槛）。

    背景：自 2024-05-01 起，用到 required reason API 却不在 `PrivacyInfo.xcprivacy`
    里声明理由的构建，App Store Connect **直接拒收** —— 不是警告，是上传失败。
    本 App 命中的只有 `UserDefaults`（`ModelPackages.swift` 用它记引擎档位、
    朗读模式、导入的包名），对应理由 `CA92.1`。

    为什么必须单独立一条：`test_resources_phase_entry_count` 的期望值是**跟着
    「文件在不在」算**的 —— 文件被删，期望值同时少 1，两边一起漂，那条判据照样绿。
    这正是「判据跟着被测对象漂」的形态，所以要在这里独立钉死。

    | # | 判据 | 不满足的后果 |
    |---|---|---|
    | 1 | 文件存在，是合法 plist，且四项声明齐全 | 上传被拒收 / 隐私标签对不上 |
    | 2 | 声明了 UserDefaults + CA92.1 | 声明与实际用法不符 |
    | 3 | 生成的工程里，Resources 阶段引用了它 | 文件在、但不会进 bundle |
    | 4 | **生成器里也有引用**（按非注释行判） | 重跑 gen-ios-project.py 后静默丢失 |
    """

    MANIFEST = ROOT / "ios" / "PrivacyInfo.xcprivacy"
    GENERATOR = ROOT / "scripts" / "gen-ios-project.py"

    def _manifest(self) -> dict:
        self.assertTrue(self.MANIFEST.is_file(),
                        "缺 ios/PrivacyInfo.xcprivacy —— App Store Connect 会拒收上传")
        with self.MANIFEST.open("rb") as handle:
            return plistlib.load(handle)

    def test_exists_with_the_four_declarations(self):
        data = self._manifest()
        self.assertIs(data.get("NSPrivacyTracking"), False,
                      "本 App 零网络、零分析 SDK，NSPrivacyTracking 必须是 false")
        self.assertEqual(data.get("NSPrivacyCollectedDataTypes"), [],
                         "本 App 不收集任何数据，数组必须为空")
        self.assertEqual(data.get("NSPrivacyTrackingDomains"), [])
        self.assertTrue(data.get("NSPrivacyAccessedAPITypes"),
                        "NSPrivacyAccessedAPITypes 为空 —— 但代码里用了 UserDefaults")

    def test_it_declares_the_userdefaults_reason(self):
        data = self._manifest()
        reasons = {entry.get("NSPrivacyAccessedAPIType"): entry.get("NSPrivacyAccessedAPITypeReasons")
                   for entry in data.get("NSPrivacyAccessedAPITypes", [])}
        self.assertIn("NSPrivacyAccessedAPICategoryUserDefaults", reasons,
                      "没声明 UserDefaults —— ios/VoiceTranslator/Models/ModelPackages.swift 用了它")
        self.assertIn("CA92.1", reasons["NSPrivacyAccessedAPICategoryUserDefaults"],
                      "UserDefaults 的理由必须是 CA92.1（只访问本 App 自己的默认值）")

    def test_the_generated_project_lists_it_in_resources(self):
        pbxproj = ROOT / "ios" / "VoiceTranslator.xcodeproj" / "project.pbxproj"
        if not pbxproj.is_file():
            self.skipTest("没有生成 Xcode 工程（干净克隆上正常）")
        text = read(pbxproj)
        phase = text.split("/* Begin PBXResourcesBuildPhase section */")[1]
        phase = phase.split("/* End PBXResourcesBuildPhase section */")[0]
        # 对着 Resources 阶段那一段里判，而不是对全文判 —— 文件引用对象、构建文件
        # 对象里都有这个名字，只有这一条决定它会不会被拷进 .app。
        entries = [line for line in phase.splitlines() if line.strip().endswith(",")]
        self.assertTrue(any("PrivacyInfo.xcprivacy" in line for line in entries),
                        "Resources 阶段里没有 PrivacyInfo.xcprivacy —— 文件在、"
                        "但不会进 bundle，上传照样被拒收。修法：python3 scripts/gen-ios-project.py")

    def test_the_generator_itself_references_it(self):
        """⚠️ 这一条是整组里最要紧的。

        工程是 `gen-ios-project.py` **整体重写**的。只在 Xcode 里加（或手改
        pbxproj），下次重跑生成脚本就静默消失 —— 而构建照常成功，症状是
        「本地打包一切正常、上传报缺隐私清单」，极难定位。

        按**非注释行**判：本生成器的注释里也写着 `PrivacyInfo.xcprivacy` 这几个字
        （就在解释为什么要改它），对全文 `assertIn` 等于让注释替代码过审。
        """
        text = read(self.GENERATOR)
        code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        self.assertTrue(
            any("buildfile" in line and "PrivacyInfo.xcprivacy" in line for line in code),
            "gen-ios-project.py 没有为隐私清单建 PBXBuildFile（只在注释里提到不算）")
        self.assertTrue(
            any("resource_entries.append" in line and "privacy" in line for line in code),
            "gen-ios-project.py 建了引用，但没接进 Resources 阶段")


if __name__ == "__main__":
    unittest.main()
