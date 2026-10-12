"""盯住「朗读不会把采集线程卡死」。

## 这一条判据为什么值得单独存在

Android 的 `Pipeline.capture` 是「一条采集线程里先 `read` 再处理」，
所以它在循环里调 `AudioRecord.stop()` 天然安全。iOS 的对应物是
`AVAudioEngine.installTap` 的回调 —— 但那个回调**不是一条可以自己停自己的线程**：

    removeTap(onBus:) / engine.stop() 会同步等待 tap 回调返回

而 `Pipeline` 的「朗读暂停」分支第一件事就是 `audio.stop()`。
如果 `onFrame` 跑在 tap 回调栈里，那就是**在 tap 回调里等 tap 回调返回**：

    编译 ✅  链接 ✅  安装 ✅  启动 ✅
    只有：朗读不出声（`.record` 会话没让出来）
          + 录音再也不恢复（采集队列卡死）

用户报的症状正是「卡在朗读了，即不出声也不往下」。
这类问题不可能被「能不能编译 / 能不能启动」抓住，必须单独立判据。

## 反向验证（项目约定：没见它红过的判据不算判据）

`CheckerRejectsTheOldCallback` 把**修复前那段真实代码**喂给同一个断言函数，
必须判红；`CheckerRejectsTheOldSessionCode` 同理盯 `SpeechPlayer` 的 `try?`。
**这两条如果在某天变成「都通过」，说明判据退化了。**
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
IOS_CORE = ROOT / "ios" / "VoiceTranslator" / "Core"

AUDIO_CAPTURE = IOS_CORE / "AudioCapture.swift"
SPEECH_PLAYER = IOS_CORE / "SpeechPlayer.swift"
PIPELINE = IOS_CORE / "Pipeline.swift"
APP_MODEL = ROOT / "ios" / "VoiceTranslator" / "App" / "AppModel.swift"
SPEAKER_JAVA = (ROOT / "app" / "src" / "main" / "java" / "com" / "vt"
                / "voicetranslator" / "Speaker.java")

# 修复前的真实代码（v0.2.10 装机版）。留着当负向验证的输入，别删。
OLD_TAP_CALLBACK = """
        input.installTap(onBus: 0, bufferSize: tapBuffer, format: inputFormat) { [weak self] buffer, _ in
            guard let self, self.running else { return }
            self.process(buffer)
        }
"""

OLD_SESSION_CODE = """
    private func activatePlaybackSession() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        try? session.setActive(true, options: [])
    }
"""

# 修复前的真实代码（v0.2.10 装机版）。留着当负向验证的输入，别删。
OLD_ANDROID_PENDING = """
        if (!ready) {
            if (initializing) {
                // 引擎还在起，先存着。handleInit 会补发。
                pendingText = trimmed;
                pendingLanguage = languageCode;
                pendingLabel = languageLabel;
                return;
            }
"""

# --------------------------------------------------------------------------- #
# v0.2.15 修的两个 bug 的「修复前代码」，全部当负向验证的输入，别删。
# --------------------------------------------------------------------------- #

#: 修复前：置了 `captureDone` 却**没有**冲刷分句器 —— 最后半句会被丢掉。
OLD_MARKED_DONE_WITHOUT_FLUSH = """
    private func captureEnded() {
        guard stateLock.withLock({ stopRequested || cancelled || !recording }) else { return }
        segmenter = nil
        stateLock.withLock { captureDone = true }
    }
"""

#: 修复前：冲刷与置位**顺序反了**（先置 `captureDone`，再冲刷）。
#: 主循环会在「ASR 队列已空」上抢先退出，刚冲刷出来的那半句永远不会被转写。
OLD_MARKED_DONE_BEFORE_FLUSH = """
    private func captureEnded() {
        guard stateLock.withLock({ stopRequested || cancelled || !recording }) else { return }
        let leftover = segmenter
        segmenter = nil
        stateLock.withLock { captureDone = true }
        leftover?.flush()
    }
"""

#: 修复前：`captureEnded` 没有守卫 —— 朗读暂停 / 积压暂停那种**临时**停采集
#: 也会被当成会话结束。
OLD_CAPTURE_ENDED_WITHOUT_GUARD = """
    private func captureEnded() {
        let leftover = segmenter
        segmenter = nil
        leftover?.flush()
        stateLock.withLock { captureDone = true }
    }
"""

#: 修复前：`AudioCapture` 根本没有 `onStopped` —— 采集停了没人知道。
OLD_AUDIO_WITHOUT_ON_STOPPED = """
final class AudioCapture {
    var onFrame: ((ContiguousArray<Float>) -> Void)?
    var onError: ((Error) -> Void)?

    private func stopOnQueue() {
        guard running else { return }
        running = false
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        VTLog.event("capture", "采集已停止")
    }
}
"""

#: 修复前：`startCapture` 没有把 `audio.onStopped` 接起来。
OLD_START_CAPTURE_WITHOUT_NOTIFICATION = """
    private func startCapture(vad vadEngine: SherpaVadEngine) {
        let segmenter = Segmenter(pauseMs: pauseMs) { [weak self] pcm in
            self?.onSentenceDetected(pcm)
        }
        audio.onFrame = { [weak self] frame in
            self?.handle(frame)
        }
        audio.onError = { [weak self] error in
            self?.listener?.pipelineState("录音错误：" + error.localizedDescription)
        }
        try? audio.start()
    }
"""

#: 修复前（v0.2.14 装机版）：采集线程退出朗读暂停时把 `playbackHold` 和
#: `playbackHeld` **一起**清掉。调用方很可能刚在这之前为下一条朗读置上
#: `playbackHold` —— 那一置被这里抹掉。
OLD_HOLD_BRANCH_CLEARS_THE_REQUEST = """
        audio.onFrame = { [weak self] frame in
            if self.stateLock.withLock({ self.playbackHold }) {
                self.audio.stop()
                self.stateLock.withLock { self.playbackHeld = true }
                let deadline = Date().addingTimeInterval(Constants.playbackHoldTimeout)
                while self.stateLock.withLock({ self.recording && !self.stopRequested && self.playbackHold }) && Date() < deadline {
                    Thread.sleep(forTimeInterval: Constants.playbackHoldPollInterval)
                }
                let timedOut = Date() >= deadline && self.stateLock.withLock({ self.playbackHold })
                self.stateLock.withLock { self.playbackHold = false; self.playbackHeld = false }
                guard self.stateLock.withLock({ self.recording && !self.stopRequested }) else { return }
                segmenter.reset()
                self.resumeCapture("朗读结束，录音已继续")
                return
            }
        }
"""

#: 修复前（v0.2.14 装机版）：`speechHoldActive` 是在**等待回调里**才置的。
#: 等待期间它是 false、`speech.isSpeaking` 也是 false —— 这个窗口里到达的
#: 第二条译文会再走一遍 `startSpeech`，两条朗读同时在飞，后一条当场打断前一条。
OLD_ASYNC_HOLD_FLAG = """
    private func startSpeech(_ request: SpeechRequest) {
        pendingSpeech = nil
        if !speechHoldActive {
            let needWait = pipeline?.holdForPlayback() ?? false
            waitForCaptureHeld(attempts: needWait ? 25 : 0, waited: needWait) { [weak self] in
                guard let self else { return }
                self.speechHoldActive = needWait
                self.beginSpeaking(request)
            }
            return
        }
        beginSpeaking(request)
    }
"""

#: 修复前：`requestSpeech` 只把「正在读」当成忙，没把「正在等采集让出会话」算进去。
OLD_REQUEST_SPEECH_BLIND = """
    private func requestSpeech(key: String, text: String, language: String, interrupt: Bool) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !Self.isPlaceholder(trimmed) else { return }
        let request = SpeechRequest(key: key, text: trimmed, language: language)

        if speech.isSpeaking {
            pendingSpeech = request
            if interrupt {
                speech.stop()
            }
            return
        }
        startSpeech(request)
    }
"""


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def brace_body(text: str, anchor: str) -> str | None:
    """取出 `anchor` 之后第一个配平的 `{…}` 块（含花括号）。

    回调体里本来就有嵌套闭包（`queue.async { … }`），所以不能只找第一个 `}`。
    """
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


def code_lines(text: str) -> str:
    """去掉整行注释 —— 判据**不许被注释满足**（本仓库踩过这个坑）。

    例：把 `self.playbackHold = false; self.playbackHeld = false` 那一句
    注释掉之后，裸的子串判断照样成立；而代码注释里正好抄了这句「化石形态」，
    不剥注释的话判据会永远绿。
    """
    kept = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        kept.append(line)
    return "\n".join(kept)


def assert_tap_is_passive(body: str) -> None:
    """tap 回调只许「拷贝 + 派发」，不许自己处理帧。"""
    async_at = body.find("queue.async")
    if async_at < 0:
        raise AssertionError(
            "tap 回调里没有 queue.async —— 帧没有派发到采集队列，处理会跑在 tap 回调栈里。"
            "那样 Pipeline 的朗读暂停分支（audio.stop()）就成了「在 tap 回调里等 tap 回调返回」："
            "朗读不出声，录音也再也不恢复。")
    process_at = body.find("process(")
    if process_at >= 0 and process_at < async_at:
        raise AssertionError(
            "process( 出现在 queue.async 之前 —— 处理又回到了 tap 回调栈里。")


def assert_no_swallowed_session_errors(text: str) -> None:
    """音频会话的切换错误不许 `try?` 掉。"""
    for pattern in ("try? session.setCategory", "try? session.setActive(true"):
        if pattern in text:
            raise AssertionError(
                f"音频会话切换又用 `{pattern}` 把错误吞了 —— "
                "这条路径失败的唯一症状就是「朗读没声音」，吞了就再也查不出来。"
                "应该把错误交给 speak() 的返回值，让界面能说出原因。")


def assert_android_pending_speak_has_watchdog(text: str) -> None:
    """Android「引擎未就绪先挂起」那条分支必须挂看门狗。"""
    anchor = text.find("pendingText = trimmed;")
    if anchor < 0:
        raise AssertionError(
            "找不到 Android 的「引擎未就绪先挂起」分支 —— 判据的前提没了，先修判据")
    window = text[anchor:anchor + 400]
    if "postDelayed(initWatchdog" not in window:
        raise AssertionError(
            "挂起那条朗读时没有 `main.postDelayed(initWatchdog, …)` —— "
            "引擎的 init 回调个别实现永远不来，"
            "那样这条朗读既不读、也不回 onFinished，调用方只能等 Pipeline 的 60 秒超时"
            "（录音被挂着，用户看到的就是「卡在朗读」）。")


class CaptureKeepsTheTapCallbackPassive(unittest.TestCase):
    """`AudioCapture` 的 tap 回调只做「深拷贝 + 派发」。"""

    def test_current_capture_is_passive(self):
        body = brace_body(read(AUDIO_CAPTURE), ".installTap(onBus")
        self.assertIsNotNone(body, "找不到 installTap 的回调闭包 —— 判据的前提没了，先修判据")
        assert_tap_is_passive(body)

    def test_the_capture_queue_is_actually_used(self):
        """`queue` 声明了就必须真的被用上。

        这个 bug 的化石形态正是「队列声明在，连接它的那一行漏了」——
        全文件只出现一次声明、再无使用。
        """
        text = read(AUDIO_CAPTURE)
        self.assertGreater(
            text.count("queue"), 1,
            "采集队列声明了却没有使用 —— 帧又跑回 tap 回调栈里了")

    def test_hold_branch_still_lives_in_on_frame(self):
        """这套判据的前提：朗读暂停分支在 `audio.onFrame` 里**先停采集再置位**。

        注意不能只写 `assertIn("self.audio.stop()", body)` —— `onFrame` 里
        「积压暂停」那条分支也有一句 `audio.stop()`，只数出现次数会被它蒙混过关。
        真正要锁的是**语义**：从「看到朗读暂停请求」到「宣布采集已让出」之间，
        必须真的停过采集。否则 `playbackHeld = true` 就是个谎，
        `AppModel` 会据此以为会话可以让出来了。

        哪天这段时序搬走了，上面的推理要跟着重写，不能留着骗人。
        """
        body = brace_body(read(PIPELINE), ".onFrame =")
        self.assertIsNotNone(body, "Pipeline 里找不到 audio.onFrame 的闭包")
        asked = body.find("self.playbackHold")
        held = body.find("playbackHeld = true")
        self.assertGreater(asked, -1, "onFrame 里没有朗读暂停判断，本判据的前提失效")
        self.assertGreater(held, asked,
                           "onFrame 里找不到「宣布采集已让出」这一句，本判据的前提失效")
        self.assertIn("self.audio.stop()", body[asked:held],
                      "朗读暂停分支里没先停采集就置位 playbackHeld —— 时序变了，"
                      "本判据的前提失效")

    def test_checker_rejects_the_old_callback(self):
        """负向自证：修复前那段真实代码必须被判红。"""
        body = brace_body(OLD_TAP_CALLBACK, ".installTap(onBus")
        self.assertIsNotNone(body, "连旧代码都取不到闭包体，说明 brace_body 坏了")
        with self.assertRaises(AssertionError) as caught:
            assert_tap_is_passive(body)
        self.assertIn("queue.async", str(caught.exception))


class CaptureStopDoesNotBlockTheCaller(unittest.TestCase):
    """`stop()` 必须在采集队列上**异步**执行。"""

    def test_stop_is_dispatched_not_synced(self):
        """`Pipeline.cancel()` 在主线程调 `audio.stop()`，而采集队列此刻可能
        正阻塞在朗读等待里。`stop()` 若用 `queue.sync`，主线程会跟着冻住 ——
        冻的是界面，比原来的问题更糟。"""
        body = brace_body(read(AUDIO_CAPTURE), "func stop()")
        self.assertIsNotNone(body, "找不到 AudioCapture.stop()")
        self.assertIn("onQueueAsync", body,
                      "stop() 没有走「不阻塞调用者」的派发路径")
        self.assertNotIn("queue.sync", body,
                         "stop() 里出现 queue.sync —— 会冻住主线程")


class SpeechFailuresAreNotSwallowed(unittest.TestCase):
    """「朗读没声音」必须能被查出来。"""

    def test_current_player_reports_session_errors(self):
        assert_no_swallowed_session_errors(read(SPEECH_PLAYER))

    def test_player_can_say_why_it_did_not_start(self):
        text = read(SPEECH_PLAYER)
        self.assertIn("enum Failure", text, "speak() 没有能区分失败原因的返回值")
        self.assertIn("case session(", text,
                      "「音频会话被占用」和「本机没有语音数据」必须能分开 —— "
                      "两者对用户的处置完全不同")
        self.assertIn("case neverStarted", text,
                      "「派下去了但一直没出声」必须能单独认出来")

    def test_checker_rejects_the_old_session_code(self):
        """负向自证：修复前的 `try?` 版本必须被判红。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "SpeechPlayer.swift"
            fake.write_text(OLD_SESSION_CODE, encoding="utf-8")
            with self.assertRaises(AssertionError) as caught:
                assert_no_swallowed_session_errors(read(fake))
            self.assertIn("try? session", str(caught.exception))


class AndroidEngineInitHasAWatchdog(unittest.TestCase):
    """`TextToSpeech` 的 init 回调个别 OEM 实现**永远不来**。

    没有看门狗时，被挂起的那条朗读既不读、也不回 `onFinished` ——
    调用方只能等 `Pipeline` 的 60 秒超时（录音被挂着），
    用户看到的就是「卡在朗读了，即不出声也不往下」。
    和 `upperBoundMs`（读完的回调也会丢）是同一类保险：
    **收口回调要假设「永远不来」**。
    """

    def test_current_speaker_arms_the_watchdog(self):
        assert_android_pending_speak_has_watchdog(read(SPEAKER_JAVA))

    def test_checker_rejects_the_version_without_it(self):
        """负向自证：修复前那段（只挂起、不看门狗）必须被判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_android_pending_speak_has_watchdog(OLD_ANDROID_PENDING)
        self.assertIn("initWatchdog", str(caught.exception))


# --------------------------------------------------------------------------- #
# v0.2.15：按停止之后，会话必须**真的收尾**
# --------------------------------------------------------------------------- #

def assert_capture_announces_stop(text: str) -> None:
    """采集真的停下来时必须有通知。

    iOS 没有「采集线程的 `finally`」这个时刻可以挂 —— 少了这条回调，
    `Pipeline` 主循环（`while !captureDone || !asrJobs.isEmpty`）等的那一位
    永远没人置。
    """
    if "var onStopped" not in text:
        raise AssertionError(
            "AudioCapture 没有 `onStopped` —— 采集停了这件事没人能知道，"
            "`Pipeline` 的主循环就永远退不出去（对应 Android 采集线程的 `finally`）。")
    body = brace_body(text, "private func stopOnQueue()")
    if body is None:
        raise AssertionError("找不到 AudioCapture.stopOnQueue —— 判据的前提没了，先修判据")
    code = code_lines(body)
    if "onStopped?()" not in code:
        raise AssertionError(
            "`stopOnQueue()` 没有回调 `onStopped` —— 采集停了却没人收尾。"
            "症状：按停止没反应，一直停在「录音已停止，正在处理剩余句子」，"
            "而且之后再也开不了新会话。")
    if code.count("onStopped?()") < 2:
        raise AssertionError(
            "`onStopped` 只在「本来就在跑」那条路回调。`engine.start()` 失败时 "
            "`running` 一直是 false，会话同样需要收尾 —— 早退那条路也必须通知一次。")


def assert_pipeline_arms_the_stop_notification(text: str) -> None:
    body = brace_body(text, "private func startCapture")
    if body is None:
        raise AssertionError("找不到 Pipeline.startCapture —— 判据的前提没了，先修判据")
    if "audio.onStopped" not in code_lines(body):
        raise AssertionError(
            "`startCapture` 没有接 `audio.onStopped` —— 采集结束的通知到了也没人处理，"
            "`captureDone` 永远不会被置真。")


def assert_capture_end_flushes_before_marking_done(text: str) -> None:
    body = brace_body(text, "private func captureEnded")
    if body is None:
        raise AssertionError("找不到 Pipeline.captureEnded —— 判据的前提没了，先修判据")
    code = code_lines(body)
    done = code.find("captureDone = true")
    if done < 0:
        raise AssertionError("`captureEnded` 没有置 `captureDone` —— 主循环还是退不出去")
    flush = code.find(".flush()")
    if flush < 0:
        raise AssertionError(
            "`captureEnded` 没有冲刷分句器 —— 用户说完话紧接着按停止时，最后那半句会被丢掉。"
            "Android 采集线程的 `finally` 里就是 `segmenter.flush()`。")
    if flush > done:
        raise AssertionError(
            "`flush()` 排在 `captureDone = true` **之后** —— 主循环会在「ASR 队列已空」上"
            "抢先退出，刚冲刷出来的那半句永远不会被转写。顺序必须是「先冲刷、再置位」。")


def assert_only_real_ends_mark_capture_done(text: str) -> None:
    """朗读暂停 / 积压暂停也会 `audio.stop()`，那是**临时停**，会话并没有结束。"""
    body = brace_body(text, "private func captureEnded")
    if body is None:
        raise AssertionError("找不到 Pipeline.captureEnded —— 判据的前提没了，先修判据")
    code = code_lines(body)
    anchor = code.find("guard stateLock.withLock({")
    if anchor < 0:
        raise AssertionError(
            "`captureEnded` 没有守卫 —— 采集每一次停都会走到这里，其中「朗读暂停」与"
            "「积压暂停」只是**临时停**（麦克风马上会被 `resumeCapture()` 拉起来），"
            "会话并没有结束。少了守卫，朗读一发生就会被当成「会话结束」。")
    else_at = code.find("else", anchor)
    guard = code[anchor:else_at if else_at > anchor else len(code)]
    missing = [token for token in ("stopRequested", "cancelled", "recording") if token not in guard]
    if missing:
        raise AssertionError(
            f"`captureEnded` 的守卫里少了 {missing} —— 判据是「有没有人表达过结束的意图」："
            "`stopRequested`（按了停止）、`cancelled`（取消）、或者采集自己认输"
            "（`recording` 已被置假，例如转写队列满）。")


# --------------------------------------------------------------------------- #
# v0.2.15：朗读暂停的「等待窗口」不许开出第二条朗读
# --------------------------------------------------------------------------- #

def assert_start_speech_marks_before_waiting(text: str) -> None:
    body = brace_body(text, "private func startSpeech(")
    if body is None:
        raise AssertionError("找不到 AppModel.startSpeech —— 判据的前提没了，先修判据")
    code = code_lines(body)
    mark = code.find("awaitingCaptureHold = true")
    wait = code.find("waitForCaptureHeld(")
    if mark < 0:
        raise AssertionError(
            "`startSpeech` 没有**同步**置 `awaitingCaptureHold`。等待窗口（最长 0.5 s）里"
            "`speech.isSpeaking` 与 `speechHoldActive` 都还是 false，落在窗口里的第二条译文"
            "会再走一遍 `startSpeech`：两条朗读同时在飞，后一条的 `speak()` 把前一条"
            "当场打断（听感：某一句话没被读出来）。")
    if wait < 0:
        raise AssertionError("找不到 `waitForCaptureHeld(` 调用 —— 判据的前提变了，先修判据")
    if mark > wait:
        raise AssertionError(
            "`awaitingCaptureHold` 是在**等待之后**才置的 —— 窗口又开了。"
            "必须先同步置位、再去等。")


def assert_request_speech_treats_waiting_as_busy(text: str) -> None:
    body = brace_body(text, "private func requestSpeech(")
    if body is None:
        raise AssertionError("找不到 AppModel.requestSpeech —— 判据的前提没了，先修判据")
    if "awaitingCaptureHold" not in code_lines(body):
        raise AssertionError(
            "`requestSpeech` 没把「正在等采集让出会话」当成忙 —— 那个窗口里它会直接"
            "`startSpeech(request)`，开出第二条朗读，把前一条当场打断。")


def assert_capture_does_not_revoke_the_callers_request(text: str) -> None:
    """采集线程退出朗读暂停时，不许动调用方的 `playbackHold`。"""
    body = brace_body(text, ".onFrame =")
    if body is None:
        raise AssertionError("找不到 Pipeline 的 audio.onFrame 闭包 —— 判据的前提没了，先修判据")
    code = code_lines(body)
    flat = "".join(code.split())
    if "playbackHold=false;self.playbackHeld=false" in flat:
        raise AssertionError(
            "采集线程把 `playbackHold` 和 `playbackHeld` **一起**清掉了。调用方很可能刚在"
            "这之前为下一条朗读置上了 `playbackHold` —— 那一置会被这里抹掉：调用方永远"
            "等不到 `isPlaybackHeld`，于是超时之后在**没有让出音频会话**的情况下出声"
            "（听感：这一句没声音）。这里只该清 `playbackHeld`；撤销 `playbackHold` 是"
            "调用方的事，只有超时兜底例外。")
    revocations = code.count("self.playbackHold = false")
    if revocations == 0:
        raise AssertionError(
            "采集线程从不撤销 `playbackHold` —— 朗读的收口回调万一丢了（TTS 引擎异常 / "
            "进程被打断），麦克风会一直停到 60 秒兜底。")
    if revocations > 1:
        raise AssertionError(
            f"采集线程有 {revocations} 处撤销 `playbackHold` —— 正常路径那一处会把调用方"
            "刚置上的下一条请求一起抹掉，只留超时兜底那一处。")
    timed = code.find("if timedOut {")
    revoke = code.find("self.playbackHold = false")
    if timed < 0 or revoke < timed:
        raise AssertionError(
            "撤销 `playbackHold` 不在超时分支里 —— 那正常结束也会抹掉调用方的新请求。")
    if "self.playbackHeld = false" not in code:
        raise AssertionError(
            "没有清 `playbackHeld` —— 调用方会一直以为采集还停着，下一次朗读的等待会白等满 0.5 s。")


class TheSessionActuallyEnds(unittest.TestCase):
    """按停止之后，会话必须真的收尾（否则之后再也开不了新会话）。"""

    def test_capture_announces_stop(self):
        assert_capture_announces_stop(read(AUDIO_CAPTURE))

    def test_pipeline_arms_the_notification(self):
        assert_pipeline_arms_the_stop_notification(read(PIPELINE))

    def test_flush_happens_before_capture_done(self):
        assert_capture_end_flushes_before_marking_done(read(PIPELINE))

    def test_only_real_ends_mark_capture_done(self):
        assert_only_real_ends_mark_capture_done(read(PIPELINE))

    def test_checker_rejects_capture_without_on_stopped(self):
        """负向自证：没有 `onStopped` 的采集必须判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_capture_announces_stop(OLD_AUDIO_WITHOUT_ON_STOPPED)
        self.assertIn("onStopped", str(caught.exception))

    def test_checker_rejects_start_capture_without_notification(self):
        """负向自证：`startCapture` 没接通知必须判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_pipeline_arms_the_stop_notification(OLD_START_CAPTURE_WITHOUT_NOTIFICATION)
        self.assertIn("captureDone", str(caught.exception))

    def test_checker_rejects_marking_done_without_flush(self):
        """负向自证：不冲刷就置位必须判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_capture_end_flushes_before_marking_done(OLD_MARKED_DONE_WITHOUT_FLUSH)
        self.assertIn("冲刷", str(caught.exception))

    def test_checker_rejects_marking_done_before_flush(self):
        """负向自证：顺序反了（先置位、后冲刷）也必须判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_capture_end_flushes_before_marking_done(OLD_MARKED_DONE_BEFORE_FLUSH)
        self.assertIn("抢先退出", str(caught.exception))

    def test_checker_rejects_ending_on_any_stop(self):
        """负向自证：朗读暂停那种**临时停**也被当成会话结束 —— 必须判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_only_real_ends_mark_capture_done(OLD_CAPTURE_ENDED_WITHOUT_GUARD)
        self.assertIn("临时停", str(caught.exception))


class TheSpeechHoldWindowIsClosed(unittest.TestCase):
    """「第二句没朗读」：等待采集让出会话的空档里不许开出第二条朗读。"""

    def test_start_speech_marks_before_waiting(self):
        assert_start_speech_marks_before_waiting(read(APP_MODEL))

    def test_request_speech_treats_waiting_as_busy(self):
        assert_request_speech_treats_waiting_as_busy(read(APP_MODEL))

    def test_capture_does_not_revoke_the_callers_request(self):
        assert_capture_does_not_revoke_the_callers_request(read(PIPELINE))

    def test_checker_rejects_the_async_hold_flag(self):
        """负向自证：v0.2.14 那段（标志在等待回调里才置）必须判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_start_speech_marks_before_waiting(OLD_ASYNC_HOLD_FLAG)
        self.assertIn("窗口", str(caught.exception))

    def test_checker_rejects_the_blind_request_speech(self):
        """负向自证：只把「正在读」当忙的 `requestSpeech` 必须判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_request_speech_treats_waiting_as_busy(OLD_REQUEST_SPEECH_BLIND)
        self.assertIn("打断", str(caught.exception))

    def test_checker_rejects_clearing_the_hold_request(self):
        """负向自证：v0.2.14 那句「两个标志一起清」必须判红。"""
        with self.assertRaises(AssertionError) as caught:
            assert_capture_does_not_revoke_the_callers_request(OLD_HOLD_BRANCH_CLEARS_THE_REQUEST)
        self.assertIn("抹掉", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
