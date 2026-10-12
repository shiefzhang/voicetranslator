r"""盯住「闪退之后还查得下去」。

## 为什么这组判据值得单独存在

在它出现之前，这个 App **没有任何会落盘的日志**。真机上「朗读完第一句就闪退」
这种问题，现场只剩一句「它没了」：

    编译 ✅  链接 ✅  安装 ✅  启动 ✅  跑一会儿 💥
    没有日志、没有异常名、没有调用栈 —— 只能靠猜

于是这里验证的不是「功能对不对」，而是**排障能力本身还在不在**：

  1. 崩溃真的被接住了吗（ObjC 异常 + 六种信号）；
  2. 崩溃路径本身安全吗（信号处理器里**不许分配内存**）；
  3. **真机崩溃报告指到的那个点真的被堵上了吗**（`process` 的切片循环里
     「回调跨过 `pending` 清空」→ `removeFirst` 越界断言）；
  4. 另一条同形路径堵上了吗（`installTap` 的显式 `format:`）；
  5. 「静默不恢复」还会不会发生（`try? audio.start()`）；
  6. 日志有没有上限、用户拿不拿得出来。

## 反向验证（项目约定：没见它红过的判据不算判据）

`CheckerRejects*` 这几条把**修复前那段真实代码**喂给同一个断言函数，
必须判红。**它们哪天变成「都通过」，说明判据退化了。**
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
IOS = ROOT / "ios" / "VoiceTranslator"

VT_LOG = IOS / "Core" / "VTLog.swift"
AUDIO_CAPTURE = IOS / "Core" / "AudioCapture.swift"
SPEECH_PLAYER = IOS / "Core" / "SpeechPlayer.swift"
PIPELINE = IOS / "Core" / "Pipeline.swift"
APP_MODEL = IOS / "App" / "AppModel.swift"
APP = IOS / "App" / "VoiceTranslatorApp.swift"
SETTINGS = IOS / "Views" / "SettingsView.swift"

#: 修复前的真实代码（v0.2.12 装机版）。留着当负向验证的输入，别删。
OLD_TAP_FORMAT = """
        input.installTap(onBus: 0, bufferSize: tapBuffer, format: inputFormat) { [weak self] buffer, _ in
            guard let self, let copied = Self.copy(buffer) else { return }
            self.queue.async { [weak self] in
                guard let self, self.running else { return }
                self.process(copied)
            }
        }
"""

#: 修复前的真实代码：恢复采集失败被静默吞掉。
OLD_RESUME_SWALLOWED = """
                segmenter.reset()
                try? self.audio.start()
                self.lastDraftAt = Date()
"""

#: 修复前的真实代码（v0.2.13 装机版），**也就是真机崩溃报告指到的那个位置**。
#:
#: 症状：`process` 在切片循环里逐帧回调 `onFrame`，而回调方（`Pipeline` 的朗读暂停
#: 分支）会**同步** `audio.stop()` / `audio.start()`，两者都 `pending.removeAll()`。
#: 回调回来时 `offset` 已指向被清空的缓冲，循环退出后的 `removeFirst(offset)` 越界
#: → `EXC_BREAKPOINT` / SIGTRAP。**朗读完第一句时必现。**
OLD_FRAME_CALLBACK_INSIDE_LOOP = """
    private func process(_ buffer: AVAudioPCMBuffer) {
        var offset = 0
        while pending.count - offset >= Self.frameLength {
            let frame = ContiguousArray(pending[offset..<(offset + Self.frameLength)])
            offset += Self.frameLength
            onFrame?(frame)
        }
        if offset > 0 { pending.removeFirst(offset) }
    }
"""

#: 反面样例：在信号处理器里做字符串插值（会分配内存）。
BAD_SIGNAL_HANDLER = """
private func vtCrashSignalHandler(_ sig: Int32) {
    let header = "崩溃：收到信号 \\(sig)\\n"
    NSLog("%@", header)
    signal(sig, SIG_DFL)
    _ = raise(sig)
}
"""

#: 反面样例：**轮转只删备份、不搬当前日志**。
#:
#: 这一条专治子串污染：`removeItem` 从第 2 个字符起就是 `moveItem`，
#: 判据若裸写 `"moveItem"`，这个坏版本会**被判绿**。
ROTATION_WITHOUT_MOVE = """
private let maxBytes = 1024
private func writeRaw(_ line: String) {
    if written > Self.maxBytes { rotateLocked() }
}
private func rotateLocked() {
    guard let url = fileURL, let backup = backupURL else { return }
    try? FileManager.default.removeItem(at: backup)
}
"""

#: 反面样例：入口那行被**注释掉** —— 判据若只看「子串在不在」，会放过它。
COMMENTED_OUT_INSTALL = """
struct VoiceTranslatorApp: App {
    init() {
        // VTLog.install()
    }
}
"""

SIGNAL_NAMES = ["SIGABRT", "SIGSEGV", "SIGBUS", "SIGILL", "SIGFPE", "SIGTRAP"]

#: 信号处理器里**不许**出现的记号：它们都可能分配内存或加锁。
#: `malloc` 中途崩溃时再调 `malloc` 是死锁 —— 日志写不出来，系统报告也生成不了。
FORBIDDEN_IN_SIGNAL_HANDLER = [
    "NSLog(", "String(", "print(", "FileHandle", "DateFormatter",
    "VTLog.shared", "withCString", "append(",
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def brace_body(text: str, anchor: str) -> str | None:
    """取出 `anchor` 之后第一个配平的 `{…}` 块（含花括号）。"""
    start = text.find(anchor)
    if start < 0:
        return None
    opening = text.find("{", start)
    if opening < 0:
        return None
    depth = 0
    for index in range(opening, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[opening:index + 1]
    return None


def bracket_body(text: str, anchor: str) -> str | None:
    """取出 `anchor` 之后第一个配平的 `[…]` 块（含方括号）。"""
    start = text.find(anchor)
    if start < 0:
        return None
    opening = text.find("[", start)
    if opening < 0:
        return None
    depth = 0
    for index in range(opening, len(text)):
        char = text[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[opening:index + 1]
    return None


def code_lines(text: str) -> str:
    """去掉整行注释，避免**判据被注释满足**（本仓库踩过这个坑）。"""
    kept = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        kept.append(line)
    return "\n".join(kept)


def install_tap_format_argument(text: str) -> str:
    """`installTap(...)` 里 `format:` 后面那个实参。"""
    anchor = text.find(".installTap(onBus")
    if anchor < 0:
        raise AssertionError("找不到 `.installTap(onBus` 调用 —— 判据的前提没了，先修判据")
    closing = text.find(")", anchor)
    fmt = text.find("format:", anchor)
    if fmt < 0 or (closing >= 0 and fmt > closing):
        raise AssertionError("installTap 没有显式写 `format:` —— 判据的前提变了，先修判据")
    return text[fmt + len("format:"):closing].strip()


def signal_list_body(text: str) -> str:
    """`crashSignals` 的**数组字面量**。

    ⚠️ 锚点必须是**声明本身**，不能是 `crashSignals` 这个名字的第一次出现。
    实现里先用过一次（`installCrashCapture` 里 `for sig in Self.crashSignals`），
    从那里往后再找 `=` 会撞进 ObjC 异常文案里的 `=====` 分隔线 ——
    取出来的就成了一串等号，六种信号全被报成「缺失」。
    """
    anchor = text.find("static let crashSignals")
    if anchor < 0:
        raise AssertionError("找不到 `crashSignals` 的声明 —— 判据的前提没了，先修判据")
    equals = text.find("=", anchor)
    if equals < 0:
        raise AssertionError("`crashSignals` 没有赋值 —— 判据的前提变了，先修判据")
    body = bracket_body(text[equals:], "=")
    if body is None:
        raise AssertionError("`crashSignals` 的值不是数组字面量 —— 判据的前提变了，先修判据")
    return body


# --------------------------------------------------------------------------- #
# 1. 崩溃真的被接住了吗
# --------------------------------------------------------------------------- #

def assert_all_signals_are_armed(text: str) -> None:
    body = signal_list_body(text)
    missing = [name for name in SIGNAL_NAMES if name not in body]
    if missing:
        raise AssertionError(
            f"`crashSignals` 里少了 {missing} —— 少一种信号就少一类现场。"
            "SIGABRT 尤其不能少：未捕获的 ObjC 异常（例如音频引擎那种）走的正是它。")


def assert_exception_handler_is_installed(text: str) -> None:
    # 锚点必须带上那个 `{`：类文档里也提到了这个 API 名字，
    # 只找名字的话会命中注释、取到**整个类**的函数体，判据就成了假绿。
    anchor = "NSSetUncaughtExceptionHandler {"
    if anchor not in text:
        raise AssertionError(
            "没有装 `NSSetUncaughtExceptionHandler` —— ObjC 异常这条路没人接，"
            "而 `AVAudioEngine.installTap` 抛的正是 ObjC 异常（Swift 的 try/catch 拦不住）。")
    handler = brace_body(text, anchor)
    if handler is None:
        raise AssertionError("找到了 NSSetUncaughtExceptionHandler 但取不到闭包体，判据坏了")
    for token in ("reason", "callStackSymbols"):
        if token not in handler:
            raise AssertionError(f"异常处理器里没记 `{token}` —— 少了它就只剩一个异常名，等于没记")


def assert_install_reaches_the_capture(text: str) -> None:
    start = brace_body(text, "private func start()")
    if start is None:
        raise AssertionError("找不到 VTLog.start() —— 判据的前提没了")
    # ⚠️ 必须剥注释再找：把这一行**注释掉**（`// installCrashCapture()`）时，
    #    裸的 `in start` 照样成立 —— 那正是「判据被注释满足」这一类假绿灯。
    if "installCrashCapture()" not in code_lines(start):
        raise AssertionError("`start()` 里没有调 `installCrashCapture()` —— 钩子装不上")


def assert_signal_handler_is_allocation_free(text: str) -> None:
    body = brace_body(text, "private func vtCrashSignalHandler")
    if body is None:
        raise AssertionError("找不到 vtCrashSignalHandler —— 判据的前提没了")
    bad = [token for token in FORBIDDEN_IN_SIGNAL_HANDLER if token in body]
    if bad:
        raise AssertionError(
            f"信号处理器里出现了 {bad} —— 它们都可能分配内存或加锁。"
            "崩在 malloc 里时再调 malloc 就是死锁：日志写不出来、系统崩溃报告也生成不了。"
            "文案要走 `StaticString`（静态内存，零分配）。")
    for token in ("SIG_DFL", "raise("):
        if token not in body:
            raise AssertionError(
                f"信号处理器里没有 `{token}` —— 恢复默认并重新触发这一步不能省，"
                "否则系统自己的崩溃报告会被我们吞掉。")


def assert_crash_writer_is_static(text: str) -> None:
    anchor = text.find("private func vtCrashWrite")
    if anchor < 0:
        raise AssertionError("找不到 vtCrashWrite —— 判据的前提没了")
    # `StaticString` 写在**签名**里，不在函数体里 —— 只看函数体会假红。
    signature = text[anchor:text.find("{", anchor)]
    if "StaticString" not in signature:
        raise AssertionError(
            "vtCrashWrite 的入参不是 `StaticString` —— 收 `String` 就意味着调用点会做"
            "字符串插值，插值要分配内存。")
    body = brace_body(text, "private func vtCrashWrite")
    if body is None:
        raise AssertionError("取不到 vtCrashWrite 的函数体，判据坏了")
    if "utf8Start" not in body or "utf8CodeUnitCount" not in body:
        raise AssertionError("vtCrashWrite 没走 `utf8Start` / `utf8CodeUnitCount`（零分配的写法）")


# --------------------------------------------------------------------------- #
# 2. 最可疑的崩溃点堵上了吗
# --------------------------------------------------------------------------- #

def assert_tap_format_is_nil(text: str) -> None:
    value = install_tap_format_argument(text)
    if value != "nil":
        raise AssertionError(
            f"`installTap` 的 `format:` 传的是 `{value}`，不是 `nil`。\n"
            "朗读会把音频会话从 .record 抢到 .playback、读完再抢回来；这一来一回之后，"
            "启动前取到的格式可能与节点真实的硬件格式对不上，而 installTap 在两者不一致时抛的是\n"
            "    com.apple.coreaudio.avfaudio\n"
            "    required condition is false: format.sampleRate == hwFormat.sampleRate\n"
            "**ObjC 异常，Swift 拦不住，进程直接 SIGABRT** —— 症状就是"
            "「第一句话朗读完、录音刚要恢复的那一刻闪退」。\n"
            "传 nil 让引擎自己用总线当前的格式；源格式从 tap 交来的 buffer 上取。")


def assert_converter_follows_the_delivered_format(text: str) -> None:
    process = brace_body(text, "private func process(_ buffer")
    if process is None:
        raise AssertionError("找不到 AudioCapture.process —— 判据的前提没了")
    if "converter(for: buffer.format)" not in process:
        raise AssertionError(
            "`process` 没有按 tap **实际交付的**格式取转换器。"
            "缓存着启动时那个格式时不会报错，只会安静地按错的采样率算比例"
            "（变调 / VAD 全判成静音），是最难查的一类。")

    maker = brace_body(text, "private func converter(for format")
    if maker is None:
        raise AssertionError("找不到 `converter(for:)` —— 判据的前提没了")
    for token in ("sampleRate", "channelCount", "AVAudioConverter(from:"):
        if token not in maker:
            raise AssertionError(
                f"`converter(for:)` 里没有 `{token}` —— 格式比对不完整，"
                "路由变化（插拔耳机）时会继续用旧的转换器。")


def assert_frame_callback_runs_after_pending_is_trimmed(text: str) -> None:
    """`process` 必须**先收缩 `pending`**，再回调 `onFrame`。

    真机崩溃报告（`VoiceTranslator-2026-10-08-110512.ips`）的触发线程就是这里：

        RangeReplaceableCollection.removeFirst(_:)      ← libswiftCore 断言
        AudioCapture.process(_:)
        closure #1 in closure #1 in AudioCapture.startOnQueue()

    崩的是「回调跨过了一次 `pending` 清空」。所以判据盯的是**顺序**：
    `onFrame?(` 必须出现在 `pending.removeFirst(` 之后。
    """
    process = brace_body(text, "private func process(_ buffer")
    if process is None:
        raise AssertionError("找不到 AudioCapture.process —— 判据的前提没了")
    code = code_lines(process)
    callback = code.find("onFrame?(")
    if callback < 0:
        raise AssertionError("`process` 里没有回调 `onFrame?` —— 判据的前提没了")
    trim = code.find("pending.removeFirst(")
    if trim < 0:
        raise AssertionError("`process` 里没有 `pending.removeFirst(` —— 判据的前提没了")
    if callback < trim:
        raise AssertionError(
            "`process` 在 `pending.removeFirst(...)` **之前**就回调了 `onFrame` —— "
            "回调方（Pipeline 的朗读暂停分支）会同步 `audio.stop()` / `audio.start()`，"
            "而它们在采集队列上**立即执行**，各自都会 `pending.removeAll()`。"
            "回来时 `offset` 已指向空缓冲，`removeFirst(offset)` 越界断言（SIGTRAP）。"
            "顺序必须是「先切完、先收缩 pending，再逐帧回调」。")
    collect = code.find("frames.append(")
    if collect < 0 or collect > trim:
        raise AssertionError(
            "`process` 没有在收缩前把帧收集起来 —— 「先切完再回调」这层保护没了："
            "回调里一旦清空 `pending`，手里的 `offset` 就是脏的。")


def assert_capture_restart_failure_is_reported(text: str) -> None:
    if "try? self.audio.start()" in code_lines(text):
        raise AssertionError(
            "Pipeline 里还有 `try? self.audio.start()` —— 恢复采集失败会被静默吞掉。"
            "界面上只表现为「录音再也没动静」，和「卡死」在观感上完全一样，"
            "而且不留任何痕迹，事后只能靠猜。")
    body = brace_body(text, "private func resumeCapture")
    if body is None:
        raise AssertionError("找不到 `resumeCapture` —— 判据的前提没了，先修判据")
    for token in ("catch", "pipelineState", "VTLog.event"):
        if token not in body:
            raise AssertionError(
                f"`resumeCapture` 里没有 `{token}` —— 恢复失败必须同时落日志与写状态栏，"
                "少一样就还是「静默不恢复」。")


# --------------------------------------------------------------------------- #
# 3. 日志本身：有上限、拿得出来、打点够用
# --------------------------------------------------------------------------- #

def assert_log_is_bounded(text: str) -> None:
    if "maxBytes" not in text:
        raise AssertionError("VTLog 没有体积上限 —— 诊断日志不该成为吃掉磁盘的东西")
    body = brace_body(text, "private func writeRaw(")
    if body is None:
        raise AssertionError("找不到 writeRaw —— 判据的前提没了")
    if "rotateLocked()" not in code_lines(body):
        raise AssertionError("写入路径没有触发轮转 —— 上限形同虚设")
    rotate = brace_body(text, "private func rotateLocked()")
    if rotate is None:
        raise AssertionError("找不到 rotateLocked —— 判据的前提没了")
    # 轮转的语义是「丢掉旧的备份，把当前的搬成备份」——两句都要在。
    # ⚠️ 记号必须带 `default.` 前缀：`removeItem` 从第 2 个字符起就是 `moveItem`，
    #    裸写 `"moveItem"` 会被 `removeItem` 满足 —— 那一版判据把 moveItem 那行
    #    整个删掉都照样通过（负向验证 #11 红着脸抓到的）。
    code = code_lines(rotate)
    for token in ("backupURL", "default.removeItem(at:", "default.moveItem(at:"):
        if token not in code:
            raise AssertionError(f"轮转里没有 `{token}` —— 旧内容没有真的被搬走")


def assert_log_never_breaks_the_app(text: str) -> None:
    body = brace_body(text, "private func append(_ tag")
    if body is None:
        raise AssertionError("找不到 append —— 判据的前提没了")
    if "guard installed" not in body:
        raise AssertionError(
            "`append` 没有 `guard installed` —— 安装之前 / 打开文件失败之后的日志"
            "会走一条半初始化的路径。日志系统自身绝不该把 App 弄崩。")


def assert_export_entry_exists() -> None:
    # ⚠️ 一律剥掉注释：文案在注释里也有一份（`/// 导出诊断日志。`），
    #    只看 `in settings` 的话，把入口整行删掉都还是绿的。
    settings_code = code_lines(read(SETTINGS))
    model = read(APP_MODEL)
    log = read(VT_LOG)
    if 'Text("导出诊断日志")' not in settings_code:
        raise AssertionError(
            "设置页没有「导出诊断日志」这一行 —— 日志落盘了却拿不出来，等于没记")
    if "exportDiagnostics()" not in settings_code:
        raise AssertionError("设置页那一行没有调 `exportDiagnostics()`")
    if "func exportDiagnostics()" not in model:
        raise AssertionError("AppModel 里没有 `exportDiagnostics()`")
    if "static func exportURL()" not in log:
        raise AssertionError("VTLog 没有 `exportURL()` —— 导出路径断了")


def assert_install_runs_first(text: str | None = None) -> None:
    source = read(APP) if text is None else text
    body = brace_body(source, "init()")
    if body is None:
        raise AssertionError("找不到 VoiceTranslatorApp.init() —— 判据的前提没了")
    # ⚠️ 同 `assert_install_reaches_the_capture`：注释掉那行不能被当成装上了。
    if "VTLog.install()" not in code_lines(body):
        raise AssertionError(
            "App 入口没有调 `VTLog.install()` —— 钩子与文件都要在最早的时候备好，"
            "否则启动期的崩溃照样抓不到。")


def event_tags(text: str) -> set[str]:
    """真实的 `VTLog.event("tag", …)` 调用标签（先去掉整行注释）。"""
    return set(re.findall(r'VTLog\.event\("([a-z]+)"', code_lines(text)))


def assert_timeline_covers_the_suspect_path() -> None:
    """崩溃最可能发生在「朗读结束 → 采集重启」这条路上，这条路上的每一段都要有记录。"""
    required = {
        SPEECH_PLAYER: ["speech"],
        AUDIO_CAPTURE: ["capture"],
        PIPELINE: ["session", "capture", "engine", "tl"],
    }
    for path, tags in required.items():
        found = event_tags(read(path))
        missing = [tag for tag in tags if tag not in found]
        if missing:
            raise AssertionError(
                f"{path.name} 里缺少时序标签 {missing} —— "
                "崩溃后读日志时正是靠这几个标签还原「死在哪一步」，少一个就断链。")


# --------------------------------------------------------------------------- #
# 判据本身
# --------------------------------------------------------------------------- #

class CrashCaptureIsReal(unittest.TestCase):
    """崩溃必须被接住，而且接住的那段代码本身不能是新的崩溃源。"""

    def test_all_signals_armed(self):
        assert_all_signals_are_armed(read(VT_LOG))

    def test_exception_handler_installed(self):
        assert_exception_handler_is_installed(read(VT_LOG))

    def test_install_hook_reaches_capture(self):
        assert_install_reaches_the_capture(read(VT_LOG))

    def test_signal_handler_is_allocation_free(self):
        assert_signal_handler_is_allocation_free(read(VT_LOG))

    def test_crash_writer_uses_static_string(self):
        assert_crash_writer_is_static(read(VT_LOG))

    def test_checker_rejects_interpolating_handler(self):
        """负向自证：带插值 + NSLog 的处理器必须被判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_signal_handler_is_allocation_free(BAD_SIGNAL_HANDLER)
        message = str(caught.exception)
        self.assertIn("分配内存", message)


class TheSuspectCrashSiteIsClosed(unittest.TestCase):
    """真机崩溃报告指到的那个点，以及两条同形路径，都必须堵上。"""

    def test_frame_callback_after_pending_is_trimmed(self):
        assert_frame_callback_runs_after_pending_is_trimmed(read(AUDIO_CAPTURE))

    def test_checker_rejects_callback_inside_the_loop(self):
        """负向自证：**真机崩溃现场那段代码**必须被判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_frame_callback_runs_after_pending_is_trimmed(OLD_FRAME_CALLBACK_INSIDE_LOOP)
        self.assertIn("空缓冲", str(caught.exception))

    def test_tap_passes_nil_format(self):
        assert_tap_format_is_nil(read(AUDIO_CAPTURE))

    def test_converter_follows_delivered_format(self):
        assert_converter_follows_the_delivered_format(read(AUDIO_CAPTURE))

    def test_restart_failure_is_reported(self):
        assert_capture_restart_failure_is_reported(read(PIPELINE))

    def test_checker_rejects_the_old_explicit_format(self):
        """负向自证：修复前那行 `format: inputFormat` 必须被判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_tap_format_is_nil(OLD_TAP_FORMAT)
        message = str(caught.exception)
        self.assertIn("inputFormat", message)
        self.assertIn("SIGABRT", message)

    def test_checker_rejects_the_swallowed_restart(self):
        """负向自证：`try? self.audio.start()` 必须被判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_capture_restart_failure_is_reported(OLD_RESUME_SWALLOWED)
        self.assertIn("静默", str(caught.exception))


class TheLogIsUsableAndBounded(unittest.TestCase):
    """日志要有上限、打点够用、用户拿得出来。"""

    def test_log_is_bounded(self):
        assert_log_is_bounded(read(VT_LOG))

    def test_log_never_breaks_the_app(self):
        assert_log_never_breaks_the_app(read(VT_LOG))

    def test_export_entry_exists(self):
        assert_export_entry_exists()

    def test_install_runs_first(self):
        assert_install_runs_first()

    def test_checker_rejects_commented_out_hook(self):
        """负向自证：把 `VTLog.install()` 注释掉必须被判红（判据不许被注释满足）。"""
        with self.assertRaises(AssertionError) as caught:
            assert_install_runs_first(COMMENTED_OUT_INSTALL)
        self.assertIn("最早", str(caught.exception))

    def test_timeline_covers_the_suspect_path(self):
        assert_timeline_covers_the_suspect_path()

    def test_tag_scanner_ignores_comments(self):
        """自证：把标签**只写在注释里**，扫描器不应该上当。"""
        fake = '''
        // VTLog.event("speech", "骗人的")
        /// VTLog.event("capture", "也是骗人的")
        '''
        self.assertEqual(event_tags(fake), set())

    def test_checker_rejects_rotation_without_move(self):
        """负向自证：轮转不搬走当前日志必须判红 —— 专治 `removeItem` ⊃ `moveItem`。"""
        with self.assertRaises(AssertionError) as caught:
            assert_log_is_bounded(ROTATION_WITHOUT_MOVE)
        self.assertIn("default.moveItem(at:", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
