// vt_ort.h —— 统一两端的 ONNX Runtime 引入方式。
//
// iOS：`libonnxruntime.a` 静态库，直接链接，`OrtGetApiBase` 是普通符号。
//      ⚠️ 但这条路径**只在第三方依赖构建过之后**才成立（`scripts/setup-ios-deps.sh`
//      会生成 `ios/Config/Deps.xcconfig`，其中带 `VT_DEPS_DEFINES = VT_ORT_STATIC`）。
//      依赖没构建时宏不存在，这里就退化成「拿不到 ORT」——
//      工程照样能编译链接启动，只是设置页里的 OPUS 选项会回落到通用模型。
//      这条约定与 VTLlama / VTSherpa 的 `isAvailable` 是同一个思路。
//
// Android：ORT 由 sherpa-onnx 的 AAR 提供（`lib/arm64-v8a/libonnxruntime.so`），
//          已经随 APK 分发。这里走 `dlopen` 而不是链接期 `-lonnxruntime` ——
//          省掉「从 AAR 里解出 .so 再喂给 CMake、同时不能重复打包」那一整套 Gradle 编排，
//          而且运行时必然能加载（库就在同一个 APK 里）。所以 Android 总是定义
//          `VT_ORT_DYNAMIC`。
//
// ⚠️ 两端的 ORT 都是 **1.27.1**（Android AAR 与 ios/third_party/sherpa-onnx/
//    onnxruntime.xcframework 同版本），所以 native/nmt/third_party/onnxruntime/
//    里那一份 header 两边通用，不需要按平台挑版本。
//    （本机 macOS 上做验收时用的是 pip 的 1.30.0，向前兼容，`GetApi(27)` 照样有效。）

#ifndef VT_NMT_VT_ORT_H_
#define VT_NMT_VT_ORT_H_

#include "onnxruntime_c_api.h"

#if defined(VT_ORT_DYNAMIC)
#include <dlfcn.h>
#endif

namespace vt {

/// 取 OrtApiBase。返回 nullptr 表示这个平台上拿不到 ORT。
inline const OrtApiBase *OrtApiBaseHandle() {
#if defined(VT_ORT_DYNAMIC)
  static const OrtApiBase *cached = [] {
    void *handle = dlopen("libonnxruntime.so", RTLD_NOW | RTLD_GLOBAL);
    if (!handle) return static_cast<const OrtApiBase *>(nullptr);
    auto entry = reinterpret_cast<const OrtApiBase *(*)()>(dlsym(handle, "OrtGetApiBase"));
    return entry ? entry() : nullptr;
  }();
  return cached;
#elif defined(VT_ORT_STATIC)
  return OrtGetApiBase();
#else
  return nullptr;  // iOS 未构建第三方依赖：不链接 ORT，引擎不可用但工程可编译
#endif
}

/// 取 OrtApi。版本按 header 里编译期常量请求 —— 与两端的库版本一致。
inline const OrtApi *Api() {
  const OrtApiBase *base = OrtApiBaseHandle();
  return base ? base->GetApi(ORT_API_VERSION) : nullptr;
}

}  // namespace vt

#endif  // VT_NMT_VT_ORT_H_

