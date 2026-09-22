import Foundation

/// 翻译模型引擎类型。rawValue 与模型包 manifest 里的 `engine` 字段一致
/// （`ModelPackages.java` 只接受这两个值）。
enum TranslationEngineKind: String {
    case qwen = "llama-qwen2"
    case gemma = "llama-gemma3"

    /// Qwen 走英文语种名，Gemma 走语言代码 —— 与 `Pipeline.translate` 一致。
    func promptSourceCode(_ code: String) -> String {
        self == .gemma ? code : Language.engineName(code)
    }

    func promptTargetCode(_ code: String) -> String {
        self == .gemma ? code : Language.engineName(code)
    }
}

enum VTEngineError: LocalizedError {
    case nativeUnavailable(String)
    case modelNotLoaded
    case cancelled
    case promptCacheFailed
    case decodeFailed
    case sentenceTooLong
    case outputLimitReached
    case emptyOutput
    case wrongScript(String)
    case wrongScriptTwice

    var errorDescription: String? {
        switch self {
        case .nativeUnavailable(let what):
            return "\(what) 原生库未集成，请先运行 scripts/setup-ios-deps.sh"
        case .modelNotLoaded: return "翻译模型未加载"
        case .cancelled: return "翻译已取消"
        case .promptCacheFailed: return "无法复用提示词缓存"
        case .decodeFailed: return "翻译推理中断或失败"
        case .sentenceTooLong: return "句子太长，请分句后重试"
        case .outputLimitReached: return "译文超过输出上限，请缩短句子后重试"
        case .emptyOutput: return "模型没有返回译文"
        case .wrongScript(let engine):
            return engine == "gemma"
                ? "TranslateGemma 返回的文字不符合目标语种，请缩短句子后重试"
                : "模型未返回目标语种，请重试或更换模型"
        case .wrongScriptTwice: return "模型两次返回错误语种，请缩短句子或更换翻译模型包"
        }
    }
}

/// 语音活动检测。
///
/// Android 用 `vad.compute(frame) >= 0.5f`；iOS 的 sherpa C API 只暴露
/// Detected（内部同为 0.5 阈值），语义等价，详见 `VTSherpa.h`。
protocol VadEngine: AnyObject {
    func isSpeech(_ frame: ContiguousArray<Float>) -> Bool
    func release()
}

/// 离线整句识别。
protocol AsrEngine: AnyObject {
    func recognize(_ pcm: ContiguousArray<Float>) -> String
    func release()
}

/// 翻译引擎。Swift 侧负责 prompt 构造、脚本校验与重试策略，
/// 原生侧只负责 tokenize / decode / 采样。
protocol TranslationEngine: AnyObject {
    func translate(sourceText: String,
                   sourceCode: String,
                   targetCode: String,
                   onPartial: ((String) -> Void)?) throws -> String
    func cancel()
    func close()
}
