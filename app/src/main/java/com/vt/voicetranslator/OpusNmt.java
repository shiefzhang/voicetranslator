package com.vt.voicetranslator;

/**
 * OPUS-MT（Marian）专用翻译引擎，Java 侧薄封装。
 *
 * 对应 iOS 的 {@code OpusTranslationEngine} + {@code VTOpusNmt} 两层，
 * 这里合成一个类 —— Java 没有「桥接层」这个额外概念，多一层只是噪音。
 *
 * 与 {@link LocalLlm}（通用模型）的几个关键区别：
 *
 * <ul>
 *   <li><b>没有 prompt</b>。Marian 是单语向模型，方向由 {@code decoder_start_token_id}
 *       决定，既不需要也不能套 few-shot 模板 —— 所以 PromptBuilder 那条链路完全不参与。</li>
 *   <li><b>没有「错语种重试一次」</b>。输出语种由模型本身保证，不存在跑偏的可能；
 *       退一步说，重试也只是同样的贪心结果。</li>
 *   <li><b>没有流式 partial</b>。一次贪心解码几十毫秒到一两秒，分片回调只会带来抖动。</li>
 * </ul>
 *
 * 装载是两段式的（见 native/nmt/vt_opus.h）：{@link #load} 只读接线与 NFKC 表（毫秒级），
 * 单包的 ONNX 会话推迟到第一次 {@link #translate} 才建 —— 一次只会建当前语向
 * 真正用到的那一两个包。所以对外表现是「第一句明显慢，之后很快」。
 */
final class OpusNmt {

    /**
     * native 库是否装载成功。
     *
     * 刻意不用 {@code static { System.loadLibrary(...) }} 直接抛 —— 那会在**类被第一次
     * 引用时**就 UnsatisfiedLinkError，把整条翻译流水线带崩。而按需降级是可接受的：
     * 专用引擎是可选加速，没有它就该走通用模型。
     */
    private static final boolean linked;

    static {
        boolean ok = true;
        try {
            System.loadLibrary("vt-opus");
        } catch (Throwable ignored) {
            ok = false;
        }
        linked = ok;
    }

    private OpusNmt() {}

    /** ONNX Runtime 与 native 库是否都可用。调用方据此决定要不要走专用引擎。 */
    static boolean isAvailable() {
        return linked && available();
    }

    /**
     * 装载包目录。路径不变时直接复用已装载的句柄（跨会话复用，与 {@link LocalLlm#load} 一致）。
     *
     * @param directory 端侧包目录（含 routes.txt / norm/nfkc.bin / routes/）的绝对路径，UTF-8 字节
     * @param threads   ORT 的 intra-op 线程数；与通用模型一样用 2，把核心留给采集与识别
     * @throws IllegalStateException 包缺文件、格式不对，或这个平台上拿不到 ONNX Runtime
     */
    static native void load(byte[] directory, int threads);

    /** 该语向能不能翻（只看接线与包文件是否齐；会话是按需建的）。 */
    static native boolean supports(byte[] source, byte[] target);

    /**
     * 翻译。不再支持的自定义语向、包缺失、推理失败都抛 {@link IllegalStateException}。
     */
    static native byte[] translate(byte[] text, byte[] source, byte[] target);

    /** 已装载单包与缺失项的摘要（排查用）。 */
    static native byte[] describe();

    /** 释放会话与环境。与 LocalLlm 一样：**不**在每次会话结束时调用会更快，但内存会常驻。 */
    static native void close();

    private static native boolean available();
}
