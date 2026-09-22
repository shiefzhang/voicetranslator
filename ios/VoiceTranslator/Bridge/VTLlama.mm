#import "VTLlama.h"

// 只有在工程里真正接入了 llama.cpp（头文件可见）时才编译原生实现。
// 未集成时退化为"不可用"，保证整个 App 仍能编译运行（UI 会给出明确提示）。
#if __has_include(<llama.h>)
#define VT_HAVE_LLAMA 1
#endif

#ifdef VT_HAVE_LLAMA

#include <llama.h>

#include <atomic>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

std::mutex g_gate;
std::atomic<bool> g_cancelled{false};
llama_model *g_model = nullptr;
llama_context *g_ctx = nullptr;
std::string g_modelPath, g_modelEngine, g_cacheKey;
llama_pos g_cachePos = 0;

constexpr int kBatch = 512;
constexpr int kNCtx = 2048;
constexpr int kThreads = 2;

void freeModel() {
    if (g_ctx) llama_free(g_ctx);
    if (g_model) llama_model_free(g_model);
    g_ctx = nullptr;
    g_model = nullptr;
    g_modelPath.clear();
    g_modelEngine.clear();
    g_cacheKey.clear();
    g_cachePos = 0;
}

std::string toStd(NSString *s) {
    if (s == nil) return {};
    const char *utf8 = s.UTF8String;
    return utf8 ? std::string(utf8) : std::string();
}

NSError *vtError(NSString *message) {
    return [NSError errorWithDomain:@"VoiceTranslator.Llama"
                               code:1
                           userInfo:@{NSLocalizedDescriptionKey: message}];
}

/// translator.cpp `tokenize`
std::vector<llama_token> tokenize(const llama_vocab *v, const std::string &s, bool special) {
    int n = llama_tokenize(v, s.data(), (int)s.size(), nullptr, 0, false, special);
    if (n >= 0) return {};
    std::vector<llama_token> out(-n);
    n = llama_tokenize(v, s.data(), (int)s.size(), out.data(), (int)out.size(), false, special);
    if (n < 0) throw std::runtime_error("Tokenization failed");
    out.resize(n);
    return out;
}

}  // namespace

@implementation VTLlama

+ (BOOL)isAvailable { return YES; }

+ (BOOL)loadModelAtPath:(NSString *)path
                 engine:(NSString *)engine
                  error:(NSError **)error {
    std::lock_guard<std::mutex> lock(g_gate);
    std::string requested = toStd(path);
    std::string requestedEngine = toStd(engine);
    g_cancelled = false;

    if (requestedEngine != "llama-qwen2" && requestedEngine != "llama-gemma3") {
        if (error) *error = vtError(@"不支持的翻译模型引擎");
        return NO;
    }
    // 已加载同一模型时直接复用（对应 Android 的 early-return）。
    if (g_ctx && requested == g_modelPath && requestedEngine == g_modelEngine) return YES;

    freeModel();
    llama_backend_init();
    auto params = llama_model_default_params();
    params.n_gpu_layers = 0;
    params.use_mmap = true;
    g_model = llama_model_load_from_file(requested.c_str(), params);
    if (!g_model) {
        if (error) *error = vtError(@"翻译模型加载失败");
        return NO;
    }
    // 把算力留给录音与 ASR：线程数与 Android 保持一致。
    auto ctxParams = llama_context_default_params();
    ctxParams.n_ctx = kNCtx;
    ctxParams.n_batch = kBatch;
    ctxParams.n_ubatch = 256;
    ctxParams.n_threads = kThreads;
    ctxParams.n_threads_batch = kThreads;
    g_ctx = llama_init_from_model(g_model, ctxParams);
    if (!g_ctx) {
        freeModel();
        if (error) *error = vtError(@"无法创建翻译上下文");
        return NO;
    }
    g_modelPath = requested;
    g_modelEngine = requestedEngine;
    llama_set_abort_callback(g_ctx, [](void *) { return g_cancelled.load(); }, nullptr);
    return YES;
}

+ (nullable NSString *)generateWithCacheKey:(NSString *)cacheKey
                                     prefix:(NSString *)prefix
                                     suffix:(NSString *)suffix
                                      input:(NSString *)input
                                  maxTokens:(int)maxTokens
                                    partial:(void (^)(NSString *))partial
                                      error:(NSError **)error {
    std::lock_guard<std::mutex> lock(g_gate);
    try {
        if (!g_ctx) throw std::runtime_error("翻译模型未加载");
        if (g_cancelled) throw std::runtime_error("翻译已取消");

        const llama_vocab *vocab = llama_model_get_vocab(g_model);
        const std::string key = toStd(cacheKey);
        const std::string sourceText = toStd(input);

        auto prefixTokens = tokenize(vocab, toStd(prefix), true);
        auto suffixTokens = tokenize(vocab, toStd(suffix), true);

        // 语言对变化时才重新计算并缓存系统提示的 KV。
        if (g_cacheKey != key) {
            llama_memory_clear(llama_get_memory(g_ctx), true);
            for (size_t pos = 0; pos < prefixTokens.size(); pos += kBatch) {
                int n = (int)std::min<size_t>(kBatch, prefixTokens.size() - pos);
                auto batch = llama_batch_get_one(prefixTokens.data() + pos, n);
                if (llama_decode(g_ctx, batch) != 0) throw std::runtime_error("提示词缓存失败");
            }
            g_cacheKey = key;
            g_cachePos = (llama_pos)prefixTokens.size();
        }

        auto generate = [&](const std::string &body) -> std::string {
            // 丢弃上一句在 KV 里留下的尾部，只保留缓存的系统提示。
            if (!llama_memory_seq_rm(llama_get_memory(g_ctx), 0, g_cachePos, -1)) {
                g_cacheKey.clear();
                g_cachePos = 0;
                throw std::runtime_error("无法复用提示词缓存");
            }
            auto bodyTokens = tokenize(vocab, body, false);
            std::vector<llama_token> tokens;
            tokens.insert(tokens.end(), bodyTokens.begin(), bodyTokens.end());
            tokens.insert(tokens.end(), suffixTokens.begin(), suffixTokens.end());

            if (g_cachePos + (llama_pos)tokens.size() + maxTokens > llama_n_ctx(g_ctx)) {
                throw std::runtime_error("句子太长，请分句后重试");
            }
            for (size_t pos = 0; pos < tokens.size(); pos += kBatch) {
                int n = (int)std::min<size_t>(kBatch, tokens.size() - pos);
                auto batch = llama_batch_get_one(tokens.data() + pos, n);
                if (llama_decode(g_ctx, batch) != 0) throw std::runtime_error("翻译推理中断或失败");
            }

            auto sampler = llama_sampler_init_greedy();
            std::string generated;
            bool ended = false;
            for (int i = 0; i < maxTokens; i++) {
                if (g_cancelled) {
                    llama_sampler_free(sampler);
                    throw std::runtime_error("翻译已取消");
                }
                llama_token token = llama_sampler_sample(sampler, g_ctx, -1);
                if (llama_vocab_is_eog(vocab, token)) {
                    ended = true;
                    break;
                }
                char small[256];
                int n = llama_token_to_piece(vocab, token, small, sizeof(small), 0, false);
                if (n >= 0) {
                    generated.append(small, n);
                } else {
                    std::vector<char> buf(-n);
                    n = llama_token_to_piece(vocab, token, buf.data(), (int)buf.size(), 0, false);
                    if (n > 0) generated.append(buf.data(), n);
                }
                if (partial && (i % 4 == 3)) {
                    @autoreleasepool {
                        NSString *text = [[NSString alloc] initWithBytes:generated.data()
                                                                  length:generated.size()
                                                                encoding:NSUTF8StringEncoding];
                        if (text) partial(text);
                    }
                }
                auto batch = llama_batch_get_one(&token, 1);
                if (llama_decode(g_ctx, batch) != 0) {
                    llama_sampler_free(sampler);
                    throw std::runtime_error("翻译推理失败");
                }
            }
            llama_sampler_free(sampler);
            if (!ended) throw std::runtime_error("译文超过输出上限，请缩短句子后重试");
            if (generated.empty()) throw std::runtime_error("模型没有返回译文");
            return generated;
        };

        std::string out = generate(sourceText);
        NSString *result = [[NSString alloc] initWithBytes:out.data()
                                                    length:out.size()
                                                  encoding:NSUTF8StringEncoding];
        if (result == nil) throw std::runtime_error("模型返回了非法编码");
        return result;
    } catch (const std::exception &e) {
        if (error) *error = vtError([NSString stringWithUTF8String:e.what()]);
        return nil;
    }
}

+ (void)cancel { g_cancelled = true; }

+ (void)close {
    std::lock_guard<std::mutex> lock(g_gate);
    freeModel();
}

@end

#else  // !VT_HAVE_LLAMA —— 未集成 llama.cpp 时的降级实现

@implementation VTLlama

+ (BOOL)isAvailable { return NO; }

+ (BOOL)loadModelAtPath:(NSString *)path
                 engine:(NSString *)engine
                  error:(NSError **)error {
    if (error) {
        *error = [NSError errorWithDomain:@"VoiceTranslator.Llama"
                                     code:2
                                 userInfo:@{
                                     NSLocalizedDescriptionKey:
                                         @"llama.cpp 未集成，请先运行 scripts/setup-ios-deps.sh"
                                 }];
    }
    return NO;
}

+ (nullable NSString *)generateWithCacheKey:(NSString *)cacheKey
                                     prefix:(NSString *)prefix
                                     suffix:(NSString *)suffix
                                      input:(NSString *)input
                                  maxTokens:(int)maxTokens
                                    partial:(void (^)(NSString *))partial
                                      error:(NSError **)error {
    if (error) {
        *error = [NSError errorWithDomain:@"VoiceTranslator.Llama"
                                     code:2
                                 userInfo:@{
                                     NSLocalizedDescriptionKey:
                                         @"llama.cpp 未集成，请先运行 scripts/setup-ios-deps.sh"
                                 }];
    }
    return nil;
}

+ (void)cancel {}
+ (void)close {}

@end

#endif
