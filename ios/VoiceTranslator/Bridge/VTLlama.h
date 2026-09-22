#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// llama.cpp 推理桥（Objective-C++）。
///
/// 由 Android 的 `app/src/main/cpp/translator.cpp` 改写而来：**只删掉 JNI 壳与
/// `__android_log_print`，采样循环、prompt 缓存、KV 清理与停止条件一行未改**。
/// 移植设计书 §3.6.2 要求这些语义必须保持，否则跨平台译文无法逐字复现。
///
/// 约束（与 Android 相同）：
/// - 单 model / 单 context，全部入口由 C++ 侧 `std::mutex` 串行化；
/// - `use_mmap = true`、`n_gpu_layers = 0`（纯 CPU）；
/// - `n_ctx = 2048`、`n_batch = n_ubatch = 512`、`n_threads = 2`；
/// - 贪心采样（对应 Android 的确定性输出要求）。
@interface VTLlama : NSObject

/// 原生库是否已集成（`llama.h` 可被包含）。
+ (BOOL)isAvailable;

/// 加载 GGUF。`engine` 取 `llama-qwen2` 或 `llama-gemma3`。
/// 同一路径 + 同一引擎重复调用时直接复用（对应 Android 的 early-return）。
+ (BOOL)loadModelAtPath:(NSString *)path
                 engine:(NSString *)engine
                  error:(NSError **)error;

/// 单轮补全。
///
/// - `prefix`：系统提示 + few-shot（Qwen）或 Gemma role prompt，按**特殊 token** 解析，
///   命中缓存时只在切换语言对后重新 decode 一次；
/// - `suffix`：`<|im_end|>\n<|im_start|>assistant\n` 之类，同样按特殊 token 解析；
/// - `input`：本句原文，**不**按特殊 token 解析（对应 `parse_special=false`）；
/// - `partial`：每生成 4 个 token 回调一次当前累计译文。
+ (nullable NSString *)generateWithCacheKey:(NSString *)cacheKey
                                     prefix:(NSString *)prefix
                                     suffix:(NSString *)suffix
                                      input:(NSString *)input
                                  maxTokens:(int)maxTokens
                                    partial:(void (^_Nullable)(NSString *partial))partial
                                      error:(NSError **)error;

/// 请求取消当前推理。不会释放模型。
+ (void)cancel;

/// 释放 model + context。
+ (void)close;

@end

NS_ASSUME_NONNULL_END
