// vt_opus.h —— 端侧 OPUS-MT(Marian) 翻译引擎的 C 接口。
//
// 一份实现、两端共用：Android 走 JNI 薄封装，iOS 走 Objective-C++ 薄封装，
// 推理与分词逻辑只有这一份，避免两端译文漂移。
//
// 数据来源是 `scripts/export-opus-package.py` 产出的目录：
//     routes.txt                 语向 -> 单包列表（两个元素 = pivot 两跳）
//     norm/nfkc.bin              NMT-NFKC 逐码点差分表
//     routes/<单包>/              encoder/decoder ONNX + 词表 + 两张 id 映射表 + route.json
//
// 为什么不用 ONNX Runtime 的 C++ API：C++ 包装抛异常，而 Android 侧是 dlopen
// 动态解析（见 vt_opus.cc 的 VT_ORT_DYNAMIC），异常跨 JNI 边界不安全。
//
// 加载是**两段式**的：
//   • `vt_opus_load` 只读接线（routes.txt）、NFKC 表、建 ORT 环境 —— 很快，
//     不碰任何 ONNX 权重；
//   • 单包的 ONNX 会话推迟到 `vt_opus_translate` 里按需建，一次只会建当前语向
//     真正用到的那一两个包（zh→en 只碰 147.6 MiB，不是整包的 548 MiB）。
//   原因是 ORT 的 CreateSession 会把权重读进内存（不像 llama.cpp 用 mmap），
//   装载期一次读完在 A8 / 2 GB 的机器上既慢又占。所以对外表现是
//   「第一句明显慢，之后很快」—— 与通用模型路径的 prefill 形态一致。
//
// 生命周期：vt_opus_load 成功后所有调用都不可重入 —— 调用方（Pipeline）本来就
// 是单翻译线程串行调用，内部只加了一把粗锁兜底（按需装载也在这把锁里）。

#ifndef VT_NMT_VT_OPUS_H_
#define VT_NMT_VT_OPUS_H_

#ifdef __cplusplus
extern "C" {
#endif

typedef struct VtOpus VtOpus;

// 加载包目录。threads <= 0 时用默认值。失败返回 NULL 并写入 error。
// 部分单包缺失不算失败：受影响的语向会走 vt_opus_supports = 0，由调用方回落。
VtOpus *vt_opus_load(const char *directory, int threads, char *error, int error_capacity);

// 该语向是否可用（src/tgt 是 "zh"/"ja"/"ko"/"en"/"yue"）。
int vt_opus_supports(const VtOpus *opus, const char *source, const char *target);

// 翻译。成功返回 malloc 的 UTF-8 串（用 vt_opus_free_string 释放），
// 失败返回 NULL 并写入 error。
char *vt_opus_translate(VtOpus *opus, const char *text, const char *source,
                        const char *target, char *error, int error_capacity);

// 已装载单包与缺失项的摘要（状态展示用），malloc 串。
char *vt_opus_describe(const VtOpus *opus);

void vt_opus_free_string(char *text);

void vt_opus_close(VtOpus *opus);

#ifdef __cplusplus
}
#endif

#endif  // VT_NMT_VT_OPUS_H_
