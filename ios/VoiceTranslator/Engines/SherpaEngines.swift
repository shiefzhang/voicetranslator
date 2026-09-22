import Foundation

/// `VTVad` 的 Swift 封装。
final class SherpaVadEngine: VadEngine {
    private let vad: VTVad

    static var isAvailable: Bool { VTVad.isAvailable() }

    init(modelPath: String, sampleRate: Int = 16_000, threads: Int = 1) throws {
        guard VTVad.isAvailable() else {
            throw VTEngineError.nativeUnavailable("sherpa-onnx")
        }
        self.vad = try VTVad(modelPath: modelPath, sampleRate: Int32(sampleRate), threads: Int32(threads))
    }

    func isSpeech(_ frame: ContiguousArray<Float>) -> Bool {
        frame.withUnsafeBufferPointer { buffer in
            guard let base = buffer.baseAddress else { return false }
            return vad.isSpeech(base, count: Int32(buffer.count))
        }
    }

    func release() { vad.releaseResources() }
}

/// `VTAsr` 的 Swift 封装。对应 `Pipeline.recognize`。
final class SherpaAsrEngine: AsrEngine {
    private let asr: VTAsr

    static var isAvailable: Bool { VTAsr.isAvailable() }

    init(modelDirectory: String, language: String, threads: Int = 2) throws {
        guard VTAsr.isAvailable() else {
            throw VTEngineError.nativeUnavailable("sherpa-onnx")
        }
        self.asr = try VTAsr(modelDirectory: modelDirectory,
                             language: language,
                             threads: Int32(threads))
    }

    func recognize(_ pcm: ContiguousArray<Float>) -> String {
        pcm.withUnsafeBufferPointer { buffer in
            guard let base = buffer.baseAddress, buffer.count > 0 else { return "" }
            return asr.recognize(base, count: Int32(buffer.count)) ?? ""
        }
    }

    func release() { asr.releaseResources() }
}
