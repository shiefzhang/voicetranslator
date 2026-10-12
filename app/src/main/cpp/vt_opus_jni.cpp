// vt_opus_jni.cpp —— Android 侧的 JNI 薄封装（对应 iOS 的 VTOpusNmt.mm）。
//
// 这一层只做「转发 + 错误转换」，推理全在 native/nmt/vt_opus.cc 里 ——
// 一份实现两端共用，两端译文才不会漂移。改推理请改那边，别在这加逻辑。
//
// 与 translator.cpp「一次一个模型」不同，OPUS 是**多语向**的：
// 一个句柄里装着 routes.txt 描述的多条接线，单包的 ONNX 会话按需建
// （见 vt_opus.cc 的 EnsureHop）。所以这里只有 load / translate / close，
// 没有 per-direction 的状态。
//
// ONNX Runtime 不在这里链接：它在 sherpa-onnx 的 AAR 里，由 vt_ort.h 走
// dlopen("libonnxruntime.so") 拿 OrtGetApiBase（VT_ORT_DYNAMIC）。

#include <jni.h>

#include <mutex>
#include <string>

#include "vt_opus.h"
#include "vt_ort.h"

namespace {

// 与 translator.cpp 同一把锁的语义：Java 侧只有一条翻译线程串行调用，
// 这把锁是兜底（vt_opus.cc 内部还有一把更细的）。
std::mutex gate;
VtOpus *handle = nullptr;
std::string loaded_path;

// 与 vt_opus.h 约定一致：512 字节的错误缓冲。
constexpr int kErrorCapacity = 512;

void Fail(JNIEnv *env, const char *message) {
  env->ThrowNew(env->FindClass("java/lang/IllegalStateException"), message);
}

std::string Bytes(JNIEnv *env, jbyteArray array) {
  if (array == nullptr) return std::string();
  std::string value(static_cast<size_t>(env->GetArrayLength(array)), '\0');
  env->GetByteArrayRegion(array, 0, static_cast<jsize>(value.size()),
                          reinterpret_cast<jbyte *>(value.data()));
  return value;
}

jbyteArray ToBytes(JNIEnv *env, const std::string &text) {
  jbyteArray array = env->NewByteArray(static_cast<jsize>(text.size()));
  if (array != nullptr && !text.empty()) {
    env->SetByteArrayRegion(array, 0, static_cast<jsize>(text.size()),
                            reinterpret_cast<const jbyte *>(text.data()));
  }
  return array;
}

void Unload() {
  if (handle != nullptr) {
    vt_opus_close(handle);
    handle = nullptr;
    loaded_path.clear();
  }
}

}  // namespace

extern "C" {

// ONNX Runtime 是否可用。对应 iOS VTOpusNmt.isAvailable —— 第三方依赖没就位时为 false，
// Java 侧据此直接回落通用模型，不抛异常。
JNIEXPORT jboolean JNICALL
Java_com_vt_voicetranslator_OpusNmt_available(JNIEnv *, jclass) {
  return vt::Api() != nullptr ? JNI_TRUE : JNI_FALSE;
}

// 装载包目录。与 LocalLlm.load 一样做**跨会话复用**：路径没变就直接返回，
// 不再重读 NFKC 表与接线（建 ONNX 会话本来就推迟到第一次翻译）。
JNIEXPORT void JNICALL
Java_com_vt_voicetranslator_OpusNmt_load(JNIEnv *env, jclass, jbyteArray directory,
                                         jint threads) {
  std::lock_guard<std::mutex> lock(gate);
  const std::string requested = Bytes(env, directory);
  if (requested.empty()) {
    Fail(env, "专用翻译模型包目录为空");
    return;
  }
  if (handle != nullptr && requested == loaded_path) return;
  Unload();

  char error[kErrorCapacity] = {0};
  handle = vt_opus_load(requested.c_str(), static_cast<int>(threads), error,
                        kErrorCapacity);
  if (handle == nullptr) {
    Fail(env, error[0] ? error : "专用翻译模型包加载失败");
    return;
  }
  loaded_path = requested;
}

// 该语向在这份接线里能不能翻。**只看接线与包文件是否齐**，不看会话建没建 ——
// 会话是按需建的，所以这里返回 true 之后第一次 translate 才会付出建会话的代价。
JNIEXPORT jboolean JNICALL
Java_com_vt_voicetranslator_OpusNmt_supports(JNIEnv *env, jclass, jbyteArray source,
                                             jbyteArray target) {
  std::lock_guard<std::mutex> lock(gate);
  if (handle == nullptr) return JNI_FALSE;
  const std::string from = Bytes(env, source);
  const std::string to = Bytes(env, target);
  return vt_opus_supports(handle, from.c_str(), to.c_str()) ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jbyteArray JNICALL
Java_com_vt_voicetranslator_OpusNmt_translate(JNIEnv *env, jclass, jbyteArray text,
                                              jbyteArray source, jbyteArray target) {
  std::lock_guard<std::mutex> lock(gate);
  if (handle == nullptr) {
    Fail(env, "专用翻译引擎未加载");
    return nullptr;
  }
  const std::string input = Bytes(env, text);
  const std::string from = Bytes(env, source);
  const std::string to = Bytes(env, target);

  char error[kErrorCapacity] = {0};
  char *result = vt_opus_translate(handle, input.c_str(), from.c_str(), to.c_str(),
                                   error, kErrorCapacity);
  if (result == nullptr) {
    Fail(env, error[0] ? error : "专用翻译失败");
    return nullptr;
  }
  const std::string value(result);
  vt_opus_free_string(result);
  return ToBytes(env, value);
}

JNIEXPORT jbyteArray JNICALL
Java_com_vt_voicetranslator_OpusNmt_describe(JNIEnv *env, jclass) {
  std::lock_guard<std::mutex> lock(gate);
  if (handle == nullptr) return ToBytes(env, "未加载");
  char *text = vt_opus_describe(handle);
  if (text == nullptr) return ToBytes(env, "未加载");
  const std::string value(text);
  vt_opus_free_string(text);
  return ToBytes(env, value);
}

JNIEXPORT void JNICALL
Java_com_vt_voicetranslator_OpusNmt_close(JNIEnv *, jclass) {
  std::lock_guard<std::mutex> lock(gate);
  Unload();
}

}  // extern "C"
