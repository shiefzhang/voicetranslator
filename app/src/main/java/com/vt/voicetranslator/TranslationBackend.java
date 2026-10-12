package com.vt.voicetranslator;

/**
 * 翻译后端策略 —— 设置页里那个「翻译引擎」选项。对应 iOS 的 {@code TranslationBackend.swift}。
 *
 * 两个取值：
 * <ul>
 *   <li>{@link #HYBRID}（默认）：<b>中英互译与中日互译</b>走 OPUS-MT，其余语向走通用模型。</li>
 *   <li>{@link #UNIFIED}：全部走通用模型（Qwen / Gemma），风格统一但慢。</li>
 * </ul>
 *
 * <h3>四个语向都交给 OPUS，但 ja→zh 是「用速度换质量」</h3>
 * 依据是 2026-09-28 的 20 条 / 4 语向门禁（见 {@code NMT换引擎门禁报告.html}）：
 * <ul>
 *   <li>{@code zh→ja} OPUS <b>赢</b>（关键错误率 33% vs Qwen 50%）；</li>
 *   <li>{@code zh↔en} 与 Qwen 打平（zh-en chrF 0.7577 vs 0.7498，略胜）；</li>
 *   <li>{@code ja→zh} 是四个语向里<b>最弱</b>的一向（75% vs 50%）—— 因为官方根本没有
 *       {@code ja→zh} 模型，只能英文中转两跳（ja→en→en-zh），误差会叠加。</li>
 * </ul>
 * 2026-10-09 起把 {@code ja→zh} 也交给 OPUS：质量那条差距是真的，但它 P50 延迟
 * 69 ms vs Qwen 1405 ms，<b>快约 20×</b> —— 连续翻译场景里这个差距比几个百分点的
 * 关键错误率更要命。要退回旧行为，只需删掉下面 {@link #prefersOpus} 里的
 * {@code ja→zh} 分支：包内 {@code routes.txt} 就算有 {@code ja-zh} 也不会被用，
 * 第 ① 层判定就拦住了。
 *
 * <h3>这里只表达「意图」</h3>
 * 真正决定用哪个引擎的是 {@code Pipeline.loadTranslationModel()}：语向没有对应的
 * OPUS 接线、或者包根本没导入时，会<b>静默回落到通用模型</b>。
 * 所以这个选项永远不会把 App 变砖 —— 「智能选择」在没有专用包的构建上等价于「统一模型」。
 */
enum TranslationBackend {

    HYBRID("hybrid", "智能选择"),
    UNIFIED("unified", "统一模型");

    /** SharedPreferences 的键名。 */
    static final String KEY = "backend";

    /** 认不出已存的值时用哪一档（与 iOS 的 {@code .fallback} 对齐）。 */
    static final TranslationBackend FALLBACK = HYBRID;

    final String value;
    final String label;

    TranslationBackend(String value, String label) {
        this.value = value;
        this.label = label;
    }

    /**
     * 从偏好里读出来。取值可能来自旧版本或被人手动改过，认不出就回落默认档 ——
     * 大不了多花点时间，但绝不能因为一个偏好值就让「开始翻译」直接抛错。
     */
    static TranslationBackend of(String raw) {
        for (TranslationBackend backend : values()) {
            if (backend.value.equals(raw)) return backend;
        }
        return FALLBACK;
    }

    /** 设置页选择器的说明文案。 */
    String hint() {
        switch (this) {
            case HYBRID:
                return "中英互译与中日互译使用更快的轻量专用引擎，其余语向使用通用模型。"
                        + "没有专用引擎可用时一律自动回落通用模型。改动在下次开始翻译时生效";
            default:
                return "所有语向都使用同一个通用模型，译文风格一致，但每句等待更久";
        }
    }

    /** 该语向是否交给 OPUS 引擎。 */
    boolean prefersOpus(String source, String target) {
        if (this != HYBRID) return false;
        if (source.equals("zh") && target.equals("en")) return true;
        if (source.equals("en") && target.equals("zh")) return true;
        if (source.equals("zh") && target.equals("ja")) return true;
        return source.equals("ja") && target.equals("zh");
    }
}
