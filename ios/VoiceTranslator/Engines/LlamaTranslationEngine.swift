import Foundation

/// llama.cpp 翻译引擎（v0.2 对齐版本）。
///
/// 职责划分：Swift 负责 prompt 构造、脚本校验与"错语种重试一次"策略，
/// 原生 `VTLlama` 只负责 tokenize / decode / 贪心采样。
/// 这样 §6.4 要求的确定性输出（贪心、无随机种子）与 §6.5 的重试语义
/// 都能被单元测试覆盖。
final class LlamaTranslationEngine: TranslationEngine {

    /// C 侧 `int maxTokens` 对应 Swift `Int32`。
    static let maxTokens: Int32 = 256

    private let engine: TranslationEngineKind
    private let modelPath: String

    init(engine: TranslationEngineKind, modelPath: String) throws {
        guard VTLlama.isAvailable() else {
            throw VTEngineError.nativeUnavailable("llama.cpp")
        }
        self.engine = engine
        self.modelPath = modelPath
        try VTLlama.loadModel(atPath: modelPath, engine: engine.rawValue)
    }

    func translate(sourceText: String,
                   sourceCode: String,
                   targetCode: String,
                   onPartial: ((String) -> Void)? = nil) throws -> String {
        let prompt = PromptBuilder.build(
            engine: engine,
            source: engine.promptSourceCode(sourceCode),
            target: engine.promptTargetCode(targetCode)
        )

        func generate(_ input: String) throws -> String {
            try VTLlama.generate(withCacheKey: prompt.cacheKey,
                                 prefix: prompt.prefix,
                                 suffix: prompt.suffix,
                                 input: input,
                                 maxTokens: Self.maxTokens,
                                 partial: onPartial)
        }

        var out = try generate(sourceText)
        if !PromptBuilder.scriptOK(out, targetName: prompt.targetName) {
            if engine == .gemma {
                throw VTEngineError.wrongScript("gemma")
            }
            // Qwen：加强指令后重试一次（cache 前缀不变，只换 body）。
            out = try generate(PromptBuilder.retryPrompt(targetName: prompt.targetName,
                                                         sourceText: sourceText))
        }
        if !PromptBuilder.scriptOK(out, targetName: prompt.targetName) {
            throw VTEngineError.wrongScriptTwice
        }
        return out
    }

    func cancel() { VTLlama.cancel() }
    func close() { VTLlama.close() }
    deinit { VTLlama.close() }
}
