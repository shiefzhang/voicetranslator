import AVFoundation
import Foundation

/// 麦克风采集。把输入统一成 Android 侧的全链路口径：
/// **16 kHz / 单声道 / Float32 / [-1,1] / 每帧 512 个样本**。
///
/// 对齐 `Pipeline.capture`：
/// - Android `AudioRecord.read` 每次要 512 个 short（32 ms），此处同样按 512 切帧；
/// - 归一化除数必须是 **32768**（Android 用 `short / 32768f`）。
///   AVFoundation 直接给 Float32，等价于已经除过 32768，因此不再二次缩放；
/// - 采满一帧才算一帧，不足的留在缓冲区等下一次 tap。
final class AudioCapture {

    static let sampleRate: Double = 16_000
    /// Android 侧 `new short[512]` —— VAD 的 window_size 也是 512。
    static let frameLength = 512

    /// 每产出一帧回调一次（在采集队列上，禁止阻塞）。
    var onFrame: ((ContiguousArray<Float>) -> Void)?
    /// 采集过程中出现错误时回调。
    var onError: ((Error) -> Void)?

    private let engine = AVAudioEngine()
    private let queue = DispatchQueue(label: "vt-audio-capture")
    private var converter: AVAudioConverter?
    private var pending: [Float] = []
    private var running = false

    private lazy var targetFormat: AVAudioFormat = {
        AVAudioFormat(commonFormat: .pcmFormatFloat32,
                      sampleRate: Self.sampleRate,
                      channels: 1,
                      interleaved: false)!
    }()

    // MARK: - 权限

    /// 当前是否已获得麦克风权限。
    static var hasPermission: Bool {
        if #available(iOS 17.0, *) {
            return AVAudioApplication.shared.recordPermission == .granted
        } else {
            return AVAudioSession.sharedInstance().recordPermission == .granted
        }
    }

    /// 请求麦克风权限（对齐 Android 的 `RECORD_AUDIO` 授权流程）。
    static func requestPermission(_ completion: @escaping (Bool) -> Void) {
        if #available(iOS 17.0, *) {
            AVAudioApplication.requestRecordPermission(completionHandler: completion)
        } else {
            AVAudioSession.sharedInstance().requestRecordPermission(completion)
        }
    }

    // MARK: - 生命周期

    func start() throws {
        guard !running else { return }

        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .measurement, options: [.duckOthers])
        try session.setPreferredSampleRate(Self.sampleRate)
        // 与 Android 的 ioBufferSize = max(minBufferSize*4, 8192) 对应的缓冲余量。
        try session.setPreferredIOBufferDuration(0.02)
        try session.setActive(true, options: [])

        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            throw NSError(domain: "VoiceTranslator.Audio", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "麦克风初始化失败"
            ])
        }

        guard let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
            throw NSError(domain: "VoiceTranslator.Audio", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "无法创建 16 kHz 转换器"
            ])
        }
        converter.sampleRateConverterQuality = AVAudioQuality.high.rawValue
        self.converter = converter
        pending.removeAll(keepingCapacity: true)

        let tapBuffer = AVAudioFrameCount(inputFormat.sampleRate * 0.1)  // 100 ms
        input.installTap(onBus: 0, bufferSize: tapBuffer, format: inputFormat) { [weak self] buffer, _ in
            guard let self, self.running else { return }
            self.process(buffer)
        }

        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw error
        }
        running = true
    }

    func stop() {
        guard running else { return }
        running = false
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        converter = nil
        pending.removeAll(keepingCapacity: true)
        try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
    }

    // MARK: - 转换与切帧

    private func process(_ buffer: AVAudioPCMBuffer) {
        guard let converter else { return }
        let ratio = targetFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else { return }

        var consumed = false
        var conversionError: NSError?
        converter.convert(to: out, error: &conversionError) { _, status in
            if consumed {
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        if let conversionError {
            onError?(conversionError)
            return
        }
        guard out.frameLength > 0, let channel = out.floatChannelData else { return }

        let samples = UnsafeBufferPointer(start: channel[0], count: Int(out.frameLength))
        pending.append(contentsOf: samples)

        // 切片成 512 样本一帧（与 Android `new short[512]` 对齐）。
        var offset = 0
        while pending.count - offset >= Self.frameLength {
            let frame = ContiguousArray(pending[offset..<(offset + Self.frameLength)])
            offset += Self.frameLength
            onFrame?(frame)
        }
        if offset > 0 { pending.removeFirst(offset) }
    }
}
