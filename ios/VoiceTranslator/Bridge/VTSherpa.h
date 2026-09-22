#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Silero VAD 活动检测桥。
///
/// Android 侧用的是 Kotlin API 的 `Vad.compute(samples): Float`（逐帧概率），
/// 阈值 `>= 0.5f`。sherpa-onnx 的 **C API 不暴露逐帧概率**，只提供
/// `SherpaOnnxVoiceActivityDetectorDetected`，其内部判定同样是
/// "概率 >= threshold"。因此这里用 `threshold = 0.5` 的 Detected 等价替代，
/// 语义与 Android 的 `compute() >= 0.5f` 一致。
///
/// ⚠️ 已知差异：Detected 受 `min_silence_duration` 迟滞影响，会在语音结束后的
/// 一小段时间内仍报 speech。为避免拉长停顿判定，这里把该值压到 0.1 s。
@interface VTVad : NSObject

+ (BOOL)isAvailable;

- (nullable instancetype)initWithModelPath:(NSString *)path
                                sampleRate:(int)sampleRate
                                   threads:(int)threads
                                     error:(NSError **)error;

/// 单帧是否包含语音（等价于 Android 的 `compute(frame) >= 0.5f`）。
- (BOOL)isSpeech:(const float *)samples count:(int)count;

- (void)releaseResources;

@end

/// SenseVoice 离线整句识别桥（对齐 `Pipeline.recognize`）。
@interface VTAsr : NSObject

+ (BOOL)isAvailable;

/// `language` 直接使用 zh/ja/ko/en 代码（sherpa 的 SenseVoice 接受这些字符串）。
- (nullable instancetype)initWithModelDirectory:(NSString *)directory
                                       language:(NSString *)language
                                        threads:(int)threads
                                          error:(NSError **)error;

/// 16 kHz 单声道 float 样本 → 文本（返回值已 trim）。
- (nullable NSString *)recognize:(const float *)samples count:(int)count;

- (void)releaseResources;

@end

NS_ASSUME_NONNULL_END
