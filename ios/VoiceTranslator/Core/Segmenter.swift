import Foundation

/// 停顿端点控制器。逐行对齐 Android `Segmenter.java`。
///
/// - `pauseSamples = pauseMs * 16`（16 kHz 下 1 ms = 16 个样本）
/// - 句前保留最多 4800 个样本（0.3 s）的预卷音频，避免吃掉首字
/// - 单句硬上限 15 s（`16000 * 15`），到顶强制切句
/// - 只有有效语音 ≥ 2400 个样本（0.15 s）的句子才会被提交，滤掉咳嗽/噪声
///
/// 线程约束：与 Android 一样，本类型**只允许在采集线程上访问**，内部不做加锁。
final class Segmenter {
    typealias Sink = (ContiguousArray<Float>) -> Void

    private let sink: Sink
    private let pauseSamples: Int

    private var current: ContiguousArray<Float>
    private var pre: ContiguousArray<Float>
    private var count = 0
    private var preCount = 0
    private var silence = 0
    private var voiced = 0

    private static let maxSentenceSamples = 16000 * 15
    private static let minVoicedSamples = 2400

    init(pauseMs: Int, sink: @escaping Sink) {
        self.pauseSamples = pauseMs * 16
        self.sink = sink
        self.current = ContiguousArray(repeating: 0, count: Self.maxSentenceSamples + 512)
        self.pre = ContiguousArray(repeating: 0, count: 4800)
    }

    /// 注入一帧音频。`speech` 由 VAD 给出（Android 侧阈值 `prob >= 0.5`）。
    func push(_ frame: ContiguousArray<Float>, speech: Bool) {
        if count == 0 {
            if !speech {
                // 尚未起句：只维护预卷环形窗口。
                let keep = min(preCount, pre.count - frame.count)
                if keep > 0 {
                    pre.replaceSubrange(0..<keep, with: pre[(preCount - keep)..<preCount])
                }
                pre.replaceSubrange(keep..<(keep + frame.count), with: frame)
                preCount = keep + frame.count
                return
            }
            // 起句：把预卷样本接到句首。
            if preCount > 0 {
                current.replaceSubrange(0..<preCount, with: pre[0..<preCount])
            }
            count = preCount
            preCount = 0
        }

        current.replaceSubrange(count..<(count + frame.count), with: frame)
        count += frame.count

        if speech {
            silence = 0
            voiced += frame.count
        } else {
            silence += frame.count
        }

        if silence >= pauseSamples || count >= Self.maxSentenceSamples {
            flush()
        }
    }

    /// 结束当前句（停顿达标或超长时调用）。不足最小时长的整段丢弃。
    func flush() {
        if count > 0 && voiced >= Self.minVoicedSamples {
            sink(ContiguousArray(current[0..<count]))
        }
        count = 0
        silence = 0
        voiced = 0
        preCount = 0
    }

    /// 供"实时草稿"使用的当前句快照；未成句时返回 nil。
    func snapshot() -> ContiguousArray<Float>? {
        guard count > 16000, voiced >= Self.minVoicedSamples else { return nil }
        return ContiguousArray(current[0..<count])
    }

    /// 采集结束时残留的数据要显式冲刷（Android 在 `finally` 里调用）。
    func reset() {
        count = 0
        silence = 0
        voiced = 0
        preCount = 0
    }
}
