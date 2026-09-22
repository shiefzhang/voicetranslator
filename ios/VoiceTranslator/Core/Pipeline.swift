import Foundation

/// 线程安全阻塞队列。
///
/// 直接对应 Java 侧的 `ArrayBlockingQueue`（有界，用于 ASR）与
/// `LinkedBlockingQueue`（无界，用于翻译）。用 `NSCondition` 实现阻塞语义，
/// 而不是 Swift Concurrency —— 因为原设计的正确性依赖于"可被阻塞的工作线程"
/// 和"后台优先级"，这些用 Task/actor 表达会改变时序。
final class BlockingQueue<Element> {
    private var items: [Element] = []
    private let condition = NSCondition()
    private let capacity: Int?

    /// `capacity == nil` 表示无界队列。
    init(capacity: Int? = nil) { self.capacity = capacity }

    /// 入队。有界且已满时返回 false（对应 `offer()` 失败）。
    @discardableResult
    func offer(_ element: Element) -> Bool {
        condition.lock()
        defer { condition.unlock() }
        if let capacity, items.count >= capacity { return false }
        items.append(element)
        condition.signal()
        return true
    }

    /// 出队。`timeout == nil` 表示不等待，立即返回。
    func poll(timeout: TimeInterval? = nil) -> Element? {
        condition.lock()
        defer { condition.unlock() }
        if items.isEmpty, let timeout {
            condition.wait(until: Date().addingTimeInterval(timeout))
        }
        return items.isEmpty ? nil : items.removeFirst()
    }

    var isEmpty: Bool {
        condition.lock()
        defer { condition.unlock() }
        return items.isEmpty
    }

    func clear() {
        condition.lock()
        items.removeAll()
        condition.unlock()
    }
}

/// 一次录音会话的流水线。逐条对齐 Android `Pipeline.java`（v0.2.2）。
///
/// 三条线程：
/// - `vt-session`：主循环，串行消费 ASR 队列，顺带刷新"实时草稿"
/// - `vt-audio`：采集 + VAD + 分句（最高优先级）
/// - `vt-translation`：后台优先级，串行访问唯一的 llama context
///
/// 关键设计（必须保留）：
/// - 识别优先占用延迟预算，翻译线程刻意用后台优先级，避免 llama.cpp 饿死 ASR；
/// - ASR 积压 ≥ 4 句自动暂停录音、回落到 ≤ 2 句自动继续；
/// - 翻译开始前先等 ASR 队列排空；
/// - 草稿刷新节流 1.5 s，且要求 ASR 空闲。
final class Pipeline {

    // MARK: - 对外事件

    protocol Listener: AnyObject {
        /// 状态文本（对应 Android 的 `status`）。
        func pipelineState(_ text: String)
        /// 实时草稿（未定稿的转写）。
        func pipelineDraft(_ text: String)
        /// 开始翻译某句时把该句置为焦点行。
        func pipelineFocus(_ id: Int64)
        /// 一句话的转写 / 译文更新。
        func pipelineSentence(id: Int64, source: String, translation: String)
        /// 会话结束。
        func pipelineDone()
    }

    private struct Job {
        let id: Int64
        var pcm: ContiguousArray<Float>?
        var text: String = ""
    }

    private struct Draft {
        let revision: Int64
        let pcm: ContiguousArray<Float>
    }

    private enum Constants {
        static let pauseBacklog = 4
        static let resumeBacklog = 2
        static let sentenceQueueCapacity = 8
        static let draftInterval: TimeInterval = 1.5
        static let idleSleep: TimeInterval = 0.03
        static let translateIdleSleep: TimeInterval = 0.025
        static let maxHistoryPerPage = 200
    }

    // MARK: - 不可变配置

    private let asrDirectory: URL
    private let translationDirectory: URL
    private let translationEngine: TranslationEngineKind
    private let source: String
    private let target: String
    private let pauseMs: Int
    private weak var listener: Listener?

    // MARK: - 共享状态

    private let asrJobs = BlockingQueue<Job>(capacity: Constants.sentenceQueueCapacity)
    private let translationJobs = BlockingQueue<Job>()
    private let stateLock = NSLock()
    private var latestDraft: Draft?
    private var pendingAsr = 0
    private var generation: Int64 = 0

    private var recording = false
    private var backlogPaused = false
    private var stopRequested = false
    private var captureDone = false
    private var asrDone = false
    private var cancelled = false
    private var recognizing = false

    private let audio = AudioCapture()
    private var asr: SherpaAsrEngine?
    private var vad: SherpaVadEngine?
    private var translator: TranslationEngine?
    private var nextId: Int64 = 0

    init(asrDirectory: URL,
         translationDirectory: URL,
         source: String,
         target: String,
         pauseMs: Int,
         listener: Listener) throws {
        self.asrDirectory = asrDirectory
        self.translationDirectory = translationDirectory
        self.source = source
        self.target = target
        self.pauseMs = pauseMs
        self.listener = listener
        // 构造期就校验引擎（对应 Android 构造里读 manifest 的 engine）。
        self.translationEngine = try ModelPackages.engine(of: translationDirectory)
    }

    // MARK: - 控制

    func start() {
        let thread = Thread { [weak self] in self?.run() }
        thread.name = "vt-session"
        thread.start()
    }

    var isRecording: Bool { stateLock.withLock { recording } }

    func stop() {
        stateLock.withLock { stopRequested = true; recording = false }
        audio.stop()
    }

    func cancel() {
        stateLock.withLock { cancelled = true; stopRequested = true; recording = false }
        audio.stop()
        translator?.cancel()
        VTLlama.cancel()
    }

    // MARK: - 主循环（对应 Pipeline.run）

    private func run() {
        // 识别优先占用延迟预算：与 Android 的 THREAD_PRIORITY_URGENT_DISPLAY 对应。
        setCurrentThreadPriority(0.7)
        var asrEngine: SherpaAsrEngine?
        var vadEngine: SherpaVadEngine?
        var translationThread: Thread?

        do {
            listener?.pipelineState("正在加载模型…")

            asrEngine = try SherpaAsrEngine(modelDirectory: asrDirectory.path,
                                            language: source,
                                            threads: 2)
            asr = asrEngine

            vadEngine = try SherpaVadEngine(modelPath: asrDirectory
                                                .appendingPathComponent("silero_vad.onnx").path,
                                            sampleRate: 16_000,
                                            threads: 1)
            vad = vadEngine

            try loadTranslationModel()
            if stateLock.withLock({ stopRequested }) { return }

            stateLock.withLock { recording = true }
            listener?.pipelineState("正在录音 · 停顿 \(Float(pauseMs) / 1000) 秒自动翻译")

            let mt = Thread { [weak self] in self?.translateLoop() }
            mt.name = "vt-translation"
            translationThread = mt
            mt.start()

            guard let vadEngine else { return }
            startCapture(vad: vadEngine)

            // 主循环：优先消费 ASR 队列，空闲时刷新草稿。
            while !(stateLock.withLock { captureDone }) || !asrJobs.isEmpty {
                if stateLock.withLock({ cancelled }) {
                    asrJobs.clear()
                    stateLock.withLock { latestDraft = nil }
                    break
                }
                if let job = asrJobs.poll() {
                    stateLock.withLock { latestDraft = nil }
                    stateLock.withLock { recognizing = true }
                    var job = job
                    if let pcm = job.pcm {
                        job.text = asrEngine?.recognize(pcm) ?? ""
                        job.pcm = nil
                    }
                    stateLock.withLock { recognizing = false }
                    finishAsr(job: job)
                } else if let draft = takeDraft() {
                    if !stateLock.withLock({ stopRequested }) {
                        let text = asrEngine?.recognize(draft.pcm) ?? ""
                        let current = stateLock.withLock { (generation, recording) }
                        if draft.revision == current.0 && current.1 {
                            listener?.pipelineDraft(text)
                        }
                    }
                } else {
                    Thread.sleep(forTimeInterval: Constants.idleSleep)
                }
            }
        } catch {
            listener?.pipelineState("错误：" + error.localizedDescription)
        }

        // —— finally ——
        stop()
        audio.stop()
        stateLock.withLock { captureDone = true; asrDone = true }
        translationThread?.cancel()
        waitForTranslationDrain(translationThread)

        vadEngine?.release()
        asrEngine?.release()
        vad = nil
        asr = nil
        listener?.pipelineDraft("")
        listener?.pipelineDone()
    }

    private func finishAsr(job: Job) {
        if job.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            listener?.pipelineSentence(id: job.id, source: "未识别到有效语音", translation: "")
        } else if job.text.unicodeScalars.filter({ CharacterSet.alphanumerics.contains($0) }).prefix(2).count < 2 {
            // 对应 Android：过滤掉不足两个字母/数字的碎片。
            listener?.pipelineSentence(id: job.id, source: job.text, translation: "（不足两个字，已跳过）")
        } else {
            listener?.pipelineSentence(id: job.id, source: job.text, translation: "翻译中…")
            translationJobs.offer(job)
        }
        stateLock.withLock { pendingAsr -= 1 }
    }

    private func waitForTranslationDrain(_ thread: Thread?) {
        let deadline = Date().addingTimeInterval(120)
        while Date() < deadline {
            let done = stateLock.withLock { asrDone } && translationJobs.isEmpty
            if done { break }
            Thread.sleep(forTimeInterval: 0.05)
        }
    }

    private func takeDraft() -> Draft? {
        stateLock.withLock {
            let draft = latestDraft
            latestDraft = nil
            return draft
        }
    }

    // MARK: - 翻译线程（对应 Pipeline.translateLoop）

    private func translateLoop() {
        setCurrentThreadPriority(0.1)  // 后台优先级

        while true {
            let asrFinished = stateLock.withLock { asrDone }
            if asrFinished && translationJobs.isEmpty { break }

            guard let job = translationJobs.poll(timeout: 0.1) else { continue }

            if stateLock.withLock({ cancelled }) {
                listener?.pipelineSentence(id: job.id, source: job.text, translation: "已取消")
                continue
            }

            // 已提交的 ASR 工作还没排空时，不要启动新的 LLM 推理。
            while !stateLock.withLock({ cancelled || (!recognizing && asrJobs.isEmpty) }) {
                Thread.sleep(forTimeInterval: Constants.translateIdleSleep)
            }
            if stateLock.withLock({ cancelled }) {
                listener?.pipelineSentence(id: job.id, source: job.text, translation: "已取消")
                continue
            }

            listener?.pipelineFocus(job.id)
            do {
                let result = try translate(job)
                listener?.pipelineSentence(id: job.id, source: job.text, translation: result)
            } catch {
                listener?.pipelineSentence(id: job.id, source: job.text,
                                           translation: "翻译失败：" + error.localizedDescription)
            }
        }
    }

    private func loadTranslationModel() throws {
        let modelPath = translationDirectory.appendingPathComponent("model.gguf").path
        translator = try LlamaTranslationEngine(engine: translationEngine, modelPath: modelPath)
    }

    private func translate(_ job: Job) throws -> String {
        guard let translator else { throw VTEngineError.modelNotLoaded }
        let result = try translator.translate(sourceText: job.text,
                                             sourceCode: source,
                                             targetCode: target,
                                             onPartial: { [weak self] partial in
            self?.listener?.pipelineSentence(id: job.id,
                                             source: job.text,
                                             translation: partial.trimmingCharacters(in: .whitespacesAndNewlines) + "▌")
        })
        return result.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - 采集线程（对应 Pipeline.capture）

    private func startCapture(vad vadEngine: SherpaVadEngine) {
        let segmenter = Segmenter(pauseMs: pauseMs) { [weak self] pcm in
            guard let self else { return }
            self.onSentenceDetected(pcm)
        }

        audio.onFrame = { [weak self] frame in
            guard let self else { return }

            if self.stateLock.withLock({ self.backlogPaused }) {
                // 积压自动暂停：等 ASR 追上再继续。
                self.audio.stop()
                self.stateLock.withLock { self.latestDraft = nil }
                self.listener?.pipelineDraft("")
                while self.stateLock.withLock({ self.recording && !self.stopRequested && self.pendingAsr > Constants.resumeBacklog }) {
                    Thread.sleep(forTimeInterval: 0.05)
                }
                guard self.stateLock.withLock({ self.recording && !self.stopRequested }) else { return }
                self.stateLock.withLock { self.backlogPaused = false }
                try? self.audio.start()
                self.lastDraftAt = Date()
                self.listener?.pipelineState("处理速度已恢复，录音已自动继续 · 停顿 \(Float(self.pauseMs) / 1000) 秒自动翻译")
                return
            }

            let speech = vadEngine.isSpeech(frame)
            segmenter.push(frame, speech: speech)

            if Date().timeIntervalSince(self.lastDraftAt) > Constants.draftInterval,
               self.stateLock.withLock({ self.pendingAsr }) == 0 {
                let snapshot = segmenter.snapshot()
                self.stateLock.withLock {
                    let revision = self.generation
                    self.latestDraft = snapshot.map { Draft(revision: revision, pcm: $0) }
                }
                self.lastDraftAt = Date()
            }
        }

        audio.onError = { [weak self] error in
            self?.listener?.pipelineState("录音错误：" + error.localizedDescription)
        }

        do {
            try audio.start()
        } catch let error as NSError where error.domain == NSOSStatusErrorDomain {
            listener?.pipelineState("录音错误：麦克风权限已撤销，请重新授权")
        } catch {
            listener?.pipelineState("录音错误：" + error.localizedDescription)
            stateLock.withLock { recording = false }
        }
    }

    private var lastDraftAt = Date()

    private func onSentenceDetected(_ pcm: ContiguousArray<Float>) {
        stateLock.withLock {
            generation += 1
            latestDraft = nil
        }
        listener?.pipelineDraft("")

        nextId += 1
        let id = nextId
        var job = Job(id: id, pcm: pcm)
        job.text = ""
        stateLock.withLock { pendingAsr += 1 }

        if !asrJobs.offer(job) {
            stateLock.withLock { pendingAsr -= 1 }
            listener?.pipelineState("转写队列已满，请停止后重试")
            stateLock.withLock { recording = false }
            audio.stop()
            return
        }
        listener?.pipelineSentence(id: id, source: "正在转写…", translation: "等待转写…")

        let backlog = stateLock.withLock { pendingAsr }
        if backlog >= Constants.pauseBacklog && !stateLock.withLock({ stopRequested }) {
            stateLock.withLock { backlogPaused = true }
            listener?.pipelineState("转写暂时跟不上，录音已自动暂停 · 待转写 \(backlog) 句")
        }
    }

    // MARK: - 工具

    /// Foundation 没有公开的 `pthread_setschedparam` 封装，用 `Thread` 的
    /// qualityOfService 表达优先级差别（与 Android 的 Process.setThreadPriority 对应）。
    private func setCurrentThreadPriority(_ qos: Double) {
        if qos >= 0.5 {
            Thread.current.qualityOfService = .userInteractive
        } else {
            Thread.current.qualityOfService = .background
        }
    }
}

extension NSLock {
    @discardableResult
    func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}
