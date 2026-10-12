"""守卫：模型包导入显示进度条（v0.2.12）。

需求原话：「导入模型的时候显示进度条」。

## 它防的是什么

导入一份 1 GiB 的包要几十秒到几分钟。只有一行「正在校验 model.gguf…」的话，
「在慢慢走」和「卡死了」在界面上**完全一样** —— 用户唯一能做的就是强杀。

## 判据一览

| # | 判据 | 不满足的后果 |
|---|---|---|
| 1 | 进度**单调不倒退**、最后**恰好到 100%**、且节流到 ≤101 次 | 进度条回跳 / 永远停在 99% / 每次字节都刷 UI 把界面卡住 |
| 2 | 总量未知时走**不确定态**而不是假装 0% | 看起来就是卡住 |
| 3 | 进度必须**穿透到每个条目内部**（不是只在文件之间跳） | 1 GiB 的 `model.gguf` 独占一整段，仍然像卡住 |
| 4 | 导出端必须真把回调接上（两处导入路径、两处解包路径都不能漏） | 某条路静默没有进度 |
| 5 | 失败路径不许谎报 100% | 报「导入成功 100%」然后弹「导入失败」 |

判据 1/2 是**真的把源码跑起来**验的：`ios/VoiceTranslator/Models/ImportProgress.swift`
刻意不依赖任何 App 内部类型（只用标准库），所以这里把它和一段探针拼在一起，
用 `xcrun swift` 直接跑**真实的那份源码**。
⚠️ 这条只在有 Xcode 的机器上跑；没有 `xcrun` 时 **skip**，不假装通过。

判据 3/4/5 是源码级判据。它们一律先切出**函数体**再判 —— 对整篇 `assertIn`
会被注释满足（这个仓库的注释里到处在讲进度条）。

反向验证：把任一条判据对应的实现改坏，
`python3 -m unittest test_import_progress` 必须变红。**没见它红过的判据不算判据。**
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REPORTER = ROOT / "ios" / "VoiceTranslator" / "Models" / "ImportProgress.swift"
MODEL_PACKAGES_SWIFT = ROOT / "ios" / "VoiceTranslator" / "Models" / "ModelPackages.swift"
APP_MODEL = ROOT / "ios" / "VoiceTranslator" / "App" / "AppModel.swift"
SETTINGS = ROOT / "ios" / "VoiceTranslator" / "Views" / "SettingsView.swift"
VTZIP_H = ROOT / "ios" / "VoiceTranslator" / "Bridge" / "VTZip.h"
VTZIP_MM = ROOT / "ios" / "VoiceTranslator" / "Bridge" / "VTZip.mm"
MODEL_PACKAGES_JAVA = ROOT / "app" / "src" / "main" / "java" / "com" / "vt" / "voicetranslator" / "ModelPackages.java"
VTSTOREDZIP_JAVA = ROOT / "app" / "src" / "main" / "java" / "com" / "vt" / "voicetranslator" / "VtStoredZip.java"
ACTIVITY = ROOT / "app" / "src" / "main" / "java" / "com" / "vt" / "voicetranslator" / "MainActivity.java"

# 探针：只喂进度、只看输出，不碰磁盘也不碰 UI。
# ① 两个阶段刻意都不整除（1000 / 900），任何一步的取整错了都会露出来；
# ② 刻意**逐字节**喂（1000 + 900 次），所以「节流」那条判据是真的在验节流，
#    而不是在验「探针自己只喂了几次」。开了节流共 103 次回调，关掉就是 1903 次。
PROBE = r'''
var log: [ImportProgress] = []
var reporter = ImportReporter { log.append($0) }

reporter.beginRead(total: 1000)
reporter.note("正在读取模型包…")
for step in 1...1000 { reporter.read(Int64(step)) }
let readPhaseEnd = log.count

reporter.beginUnpack(total: 900)
reporter.note("正在校验 model.gguf…")
for step in 1...900 { reporter.unpack(Int64(step)) }
reporter.note("模型包已导入")

var failures: [String] = []
let values = log.compactMap { $0.fraction }
if values.count != log.count { failures.append("出现了不确定态：\(log.filter { $0.fraction == nil })") }
if values.count > 1 {
    for index in 1..<values.count where values[index] < values[index - 1] {
        failures.append("进度倒退：\(values[index - 1]) → \(values[index])")
    }
}
if values.prefix(readPhaseEnd).contains(1.0) { failures.append("解包还没开始就报了 100%") }
if values.last != 1.0 { failures.append("最后一条不是 100%：\(String(describing: values.last))") }
// 上界 = 101 个整数档 + 3 次 note 的强制推送。
if log.count > 104 { failures.append("回调 \(log.count) 次，节流没生效") }
if readPhaseEnd > 0, values.count >= readPhaseEnd, values[readPhaseEnd - 1] <= 0.05 {
    failures.append("整包都读完了进度还停在 \(values[readPhaseEnd - 1])")
}

// 总量未知时必须走不确定态，而不是假装 0%；一旦知道了解包总量要立刻恢复确定态。
var blind: [ImportProgress] = []
var blindReporter = ImportReporter { blind.append($0) }
blindReporter.beginRead(total: nil)
blindReporter.note("正在读取模型包…")
blindReporter.read(50)
if blind.first?.fraction != nil { failures.append("读取总量未知时给了假的确定比例") }
blindReporter.beginUnpack(total: 100)
blindReporter.unpack(50)
if blind.last?.fraction == nil { failures.append("知道总量之后仍不给确定比例") }

print(failures.isEmpty ? "PASS" : "FAIL " + failures.joined(separator: " ; "))
'''


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def body_of(text: str, start_marker: str) -> str:
    start = text.find(start_marker)
    if start < 0:
        raise AssertionError(f"找不到 {start_marker!r}")
    end = text.find("\n    }", start)
    return text[start:] if end < 0 else text[start:end]


class ReporterBehavesWhenRun(unittest.TestCase):
    """判据 1/2：跑真实的 ImportReporter 源码。"""

    def test_reporter_is_monotone_throttled_and_completes(self):
        if shutil.which("xcrun") is None:
            self.skipTest("本机没有 Xcode 工具链，跑不了 Swift 探针")
        source = read(REPORTER)
        with tempfile.TemporaryDirectory(prefix="vt-progress-") as tmp:
            probe = Path(tmp) / "probe.swift"
            probe.write_text(source + "\n\n" + PROBE, encoding="utf-8")
            result = subprocess.run(["xcrun", "swift", str(probe)],
                                    capture_output=True, text=True, timeout=600)
        output = (result.stdout or "").strip()
        self.assertEqual(result.returncode, 0,
                         f"探针没跑起来：\n{result.stdout}\n{result.stderr}")
        self.assertTrue(output.startswith("PASS"),
                        f"进度收敛器不满足判据：{output}\n{result.stderr}")

    def test_reporter_stays_dependency_free(self):
        """探针能拼起来的前提是它不依赖 App 内部类型。加了依赖这条会红。"""
        source = read(REPORTER)
        for line in source.splitlines():
            if line.startswith("import "):
                self.assertEqual(line.strip(), "import Foundation",
                                 f"ImportProgress.swift 引入了额外依赖：{line!r} —— "
                                 "它必须能被单独拿去跑，否则本文件那条负向判据就废了")


class ProgressReachesInsideEachEntry(unittest.TestCase):
    """判据 3：粒度必须细到单个条目的内部。"""

    def test_ios_zip_archive_reports_bytes(self):
        header = read(VTZIP_H)
        self.assertIn("progress:(nullable void (^)", header,
                      "VTZipArchive 没有带进度回调的解包方法")
        mm = read(VTZIP_MM)
        self.assertIn("if (progress != nil) progress(written, size);", mm,
                      "进度回调声明了却没在搬运循环里调用 —— 形同虚设")
        self.assertIn("progress:nil", mm,
                      "旧的 4 参数版本没有转发到新实现（两条路会分叉）")

    def test_ios_install_subscribes_to_it(self):
        swift = read(MODEL_PACKAGES_SWIFT)
        body = body_of(swift, "static func install(from source: URL,")
        self.assertIn("progress: { written, _ in", body,
                      "install 没有订阅逐字节进度 —— 1 GiB 的 model.gguf 会独占一整段")
        self.assertIn("reporter.unpack(baseline + Int64(written))", body)

    def test_android_stored_zip_reports_bytes(self):
        java = read(VTSTOREDZIP_JAVA)
        self.assertIn("interface ByteSink", java)
        self.assertIn("ByteSink sink)", java,
                      "copyTo 没有带回调的重载")
        body = java[java.find("long copyTo(OutputStream out, byte[] buffer, long size, MessageDigest hash, ByteSink sink)"):]
        self.assertIn("sink.written(", body[:body.find("return size;")],
                      "回调参数收了却没调用")

    def test_android_model_packages_subscribes_to_it(self):
        java = read(MODEL_PACKAGES_JAVA)
        self.assertIn("written->meter.unpack(baseline+written)", java,
                      "VtStoredZip 那条路没有订阅逐字节进度")
        self.assertIn("meter.unpack(baseline+written);}}", java,
                      "ZipFile 那条路没有订阅逐字节进度")


class EveryPathReportsProgress(unittest.TestCase):
    """判据 4：四条路径（2 种导入 × 2 种解包实现）一条都不能漏。"""

    def test_android_both_unpack_paths_declare_the_total(self):
        java = read(MODEL_PACKAGES_JAVA)
        self.assertEqual(java.count("meter.beginUnpack("), 2,
                         "解包总字节只在一条路上报了 —— 另一条路的进度会一直是 0%")

    def test_android_interface_carries_the_fraction(self):
        java = read(MODEL_PACKAGES_JAVA)
        self.assertIn("interface Progress { void update(String message, double fraction); }", java,
                      "Progress 还是只有 message —— 比例传不出来")
        self.assertIn("meter.finish()", java, "成功路径没有收口，进度可能停在 99%")

    def test_ios_both_phases_are_announced(self):
        swift = read(MODEL_PACKAGES_SWIFT)
        body = body_of(swift, "static func install(from source: URL,")
        self.assertIn("reporter.beginRead(total:", body)
        self.assertIn("reporter.beginUnpack(total:", body)
        self.assertIn("reporter.note(", body)

    def test_android_ui_actually_renders_and_updates_the_bar(self):
        java = read(ACTIVITY)
        self.assertIn("progressBarStyleHorizontal", java, "设置页没有进度条")
        self.assertIn("bar.setProgress(prepPercent())", java, "进度条建出来没设初值")
        self.assertIn("prepBar.setProgress(prepPercent())", java,
                      "showPrep 没有更新进度条 —— 只有文字会动")
        self.assertEqual(java.count("showPrep(m,f)"), 2,
                         "两处进度回调（用户导入 / 首启解内置包）不都转发比例")

    def test_ios_ui_actually_renders_the_bar(self):
        self.assertIn("@Published var importFraction: Double?", read(APP_MODEL))
        self.assertIn("model.importFraction = update.fraction", read(APP_MODEL))
        settings = read(SETTINGS)
        self.assertIn("ProgressView(value:", settings, "设置页没有进度条")
        self.assertIn("model.importFraction", settings)


class FailureDoesNotClaimSuccess(unittest.TestCase):
    """判据 5：失败路径不许谎报 100%。"""

    def test_ios_failure_path_clears_the_fraction(self):
        """⚠️ 判据必须**只圈住 catch 那一段**：成功路径里也有一模一样的
        `model.importFraction = nil`，用「向前取 400 字符」这种窗口会把它算进来，
        于是把 catch 里那一行删掉也照样通过（实测踩过，这就是假绿灯）。"""
        swift = read(APP_MODEL)
        catch = swift.find('model.alert = PendingAlert(title: "导入失败"')
        self.assertGreater(catch, 0, "找不到 iOS 导入失败那条路")
        start = swift.rfind("await MainActor.run {", 0, catch)
        self.assertGreater(start, 0)
        window = swift[start:catch]
        self.assertIn("model.importFraction = nil", window,
                      "失败路径没有把进度清掉 —— 进度条会停在某个百分比再弹「导入失败」")

    def test_android_failure_path_clears_the_fraction(self):
        """同理：窗口只圈 catch 里那个 `ui(()->{ … })`。"""
        java = read(ACTIVITY)
        catch = java.find('message("导入失败："+e.getMessage())')
        self.assertGreater(catch, 0, "找不到 Android 导入失败那条路")
        start = java.rfind("ui(()->{", 0, catch)
        self.assertGreater(start, 0)
        window = java[start:catch]
        self.assertIn("prepFraction=-1", window,
                      "失败路径没有把进度清掉（理由同 iOS 那条）")

    def test_android_meter_only_finishes_on_the_success_path(self):
        java = read(MODEL_PACKAGES_JAVA)
        # ⚠️ 这里必须点名 7 参数的那个重载：6 参数那个只是转发壳，
        # 拿它来判会得到「成功路径没有收口」的假红（也就会漏掉真正的收口缺失）。
        for marker in ("static File unpack(Context c,InputStream raw,",
                       "private static File unpackWithZipFile(Context c,File zip,String kind,"
                       "Meter meter,File base,File staging,boolean fromBundled)"):
            body = body_of(java, marker)
            finish = body.find("meter.finish()")
            self.assertGreater(finish, 0, f"{marker} 的成功路径没有收口")
            activated = body.find("renameTo(activated)")
            self.assertGreater(activated, -1, f"{marker} 里找不到激活那一步")
            self.assertLess(activated, finish,
                            "meter.finish() 跑在激活之前 —— 激活失败会被报成 100%")


if __name__ == "__main__":
    unittest.main()
