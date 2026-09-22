#import "VTSherpa.h"

// 只有真正接入了 sherpa-onnx（C API 头文件可见）时才编译原生实现。
#if __has_include(<sherpa-onnx/c-api/c-api.h>)
#define VT_HAVE_SHERPA 1
#endif

#ifdef VT_HAVE_SHERPA

#include <sherpa-onnx/c-api/c-api.h>

#include <cstring>
#include <string>

namespace {

NSError *vtError(NSString *message) {
    return [NSError errorWithDomain:@"VoiceTranslator.Sherpa"
                               code:1
                           userInfo:@{NSLocalizedDescriptionKey: message}];
}

NSString *vtString(const char *utf8) {
    if (utf8 == nullptr) return @"";
    NSString *s = [NSString stringWithUTF8String:utf8];
    return s ?: @"";
}

/// 采样率等常量与 Android 保持一致（16 kHz 全链路唯一口径）。
constexpr int kSampleRate = 16000;

}  // namespace

@implementation VTVad {
    const SherpaOnnxVoiceActivityDetector *_vad;
}

+ (BOOL)isAvailable { return YES; }

- (nullable instancetype)initWithModelPath:(NSString *)path
                                sampleRate:(int)sampleRate
                                   threads:(int)threads
                                     error:(NSError **)error {
    self = [super init];
    if (self == nil) return nil;

    SherpaOnnxVadModelConfig config;
    std::memset(&config, 0, sizeof(config));
    config.silero_vad.model = path.UTF8String;
    config.silero_vad.threshold = 0.5f;
    // 与 Android 的 compute()>=0.5 对齐：迟滞窗口压到最小。
    config.silero_vad.min_silence_duration = 0.1f;
    config.silero_vad.min_speech_duration = 0.1f;
    config.silero_vad.max_speech_duration = 20.0f;
    config.silero_vad.window_size = 512;
    config.sample_rate = sampleRate > 0 ? sampleRate : kSampleRate;
    config.num_threads = threads > 0 ? threads : 1;
    config.provider = "cpu";
    config.debug = 0;

    _vad = SherpaOnnxCreateVoiceActivityDetector(&config, 30.0f);
    if (_vad == nullptr) {
        if (error) *error = vtError(@"Silero VAD 模型加载失败");
        return nil;
    }
    return self;
}

- (BOOL)isSpeech:(const float *)samples count:(int)count {
    if (_vad == nullptr || samples == nullptr || count <= 0) return NO;
    SherpaOnnxVoiceActivityDetectorAcceptWaveform(_vad, samples, count);
    return SherpaOnnxVoiceActivityDetectorDetected(_vad) != 0;
}

- (void)releaseResources {
    if (_vad) {
        SherpaOnnxDestroyVoiceActivityDetector(_vad);
        _vad = nullptr;
    }
}

- (void)dealloc { [self releaseResources]; }

@end

@implementation VTAsr {
    const SherpaOnnxOfflineRecognizer *_recognizer;
}

+ (BOOL)isAvailable { return YES; }

- (nullable instancetype)initWithModelDirectory:(NSString *)directory
                                       language:(NSString *)language
                                        threads:(int)threads
                                          error:(NSError **)error {
    self = [super init];
    if (self == nil) return nil;

    NSString *modelPath = [directory stringByAppendingPathComponent:@"model.int8.onnx"];
    NSString *tokensPath = [directory stringByAppendingPathComponent:@"tokens.txt"];

    SherpaOnnxOfflineRecognizerConfig config;
    std::memset(&config, 0, sizeof(config));
    config.feat_config.sample_rate = kSampleRate;
    config.feat_config.feature_dim = 80;
    config.model_config.sense_voice.model = modelPath.UTF8String;
    config.model_config.sense_voice.language = language.UTF8String;
    config.model_config.sense_voice.use_itn = 1;  // 对齐 setUseInverseTextNormalization(true)
    config.model_config.tokens = tokensPath.UTF8String;
    config.model_config.num_threads = threads > 0 ? threads : 2;
    config.model_config.provider = "cpu";
    config.model_config.debug = 0;
    config.decoding_method = "greedy_search";

    _recognizer = SherpaOnnxCreateOfflineRecognizer(&config);
    if (_recognizer == nullptr) {
        if (error) *error = vtError(@"转写模型加载失败");
        return nil;
    }
    return self;
}

- (nullable NSString *)recognize:(const float *)samples count:(int)count {
    if (_recognizer == nullptr || samples == nullptr || count <= 0) return @"";

    // 对应 Pipeline.recognize：建流 → acceptWaveform → decode → getResult
    const SherpaOnnxOfflineStream *stream = SherpaOnnxCreateOfflineStream(_recognizer);
    if (stream == nullptr) return @"";
    SherpaOnnxAcceptWaveformOffline(stream, kSampleRate, samples, count);
    SherpaOnnxDecodeOfflineStream(_recognizer, stream);

    const SherpaOnnxOfflineRecognizerResult *result = SherpaOnnxGetOfflineStreamResult(stream);
    NSString *text = @"";
    if (result != nullptr) {
        text = [vtString(result->text) stringByTrimmingCharactersInSet:
                    [NSCharacterSet whitespaceAndNewlineCharacterSet]];
        SherpaOnnxDestroyOfflineRecognizerResult(result);
    }
    SherpaOnnxDestroyOfflineStream(stream);
    return text;
}

- (void)releaseResources {
    if (_recognizer) {
        SherpaOnnxDestroyOfflineRecognizer(_recognizer);
        _recognizer = nullptr;
    }
}

- (void)dealloc { [self releaseResources]; }

@end

#else  // !VT_HAVE_SHERPA —— 未集成 sherpa-onnx 时的降级实现

static NSError *vtSherpaMissing(void) {
    return [NSError errorWithDomain:@"VoiceTranslator.Sherpa"
                               code:2
                           userInfo:@{
                               NSLocalizedDescriptionKey:
                                   @"sherpa-onnx 未集成，请先运行 scripts/setup-ios-deps.sh"
                           }];
}

@implementation VTVad

+ (BOOL)isAvailable { return NO; }

- (nullable instancetype)initWithModelPath:(NSString *)path
                                sampleRate:(int)sampleRate
                                   threads:(int)threads
                                     error:(NSError **)error {
    if (error) *error = vtSherpaMissing();
    return nil;
}

- (BOOL)isSpeech:(const float *)samples count:(int)count { return NO; }
- (void)releaseResources {}

@end

@implementation VTAsr

+ (BOOL)isAvailable { return NO; }

- (nullable instancetype)initWithModelDirectory:(NSString *)directory
                                       language:(NSString *)language
                                        threads:(int)threads
                                          error:(NSError **)error {
    if (error) *error = vtSherpaMissing();
    return nil;
}

- (nullable NSString *)recognize:(const float *)samples count:(int)count { return nil; }
- (void)releaseResources {}

@end

#endif
