//
//  VoiceTranslator-Bridging-Header.h
//
//  Swift ↔ Objective-C++ 的桥接头（对应移植设计书 §6 的 TranslationBridge 契约）。
//
//  只暴露三个纯 ObjC 头文件：VTLlama.h / VTSherpa.h / VTZip.h。
//  这三个头都**不包含任何 C++ 类型**，因此可以被 Swift 安全地文本包含；
//  真正的 C++ 细节（llama.h / sherpa-onnx c-api.h / zlib）全部封在 .mm 内。
//
//  注意：这个文件由 `SWIFT_OBJC_BRIDGING_HEADER` 指定，不要在 Swift 里 import。
//

#import "VTLlama.h"
#import "VTSherpa.h"
#import "VTZip.h"
