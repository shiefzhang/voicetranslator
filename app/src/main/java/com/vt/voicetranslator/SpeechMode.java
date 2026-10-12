package com.vt.voicetranslator;

/**
 * 译文朗读策略 —— 设置页里那个「翻译朗读」选项。对应 iOS 的 {@code SpeechMode.swift}。
 *
 * <ul>
 *   <li>{@link #OFF}：完全不朗读，译文行尾也不显示喇叭。</li>
 *   <li>{@link #TAP}：译文行尾显示喇叭，<b>点哪句读哪句</b>，再点一次停止。</li>
 *   <li>{@link #AUTO}（默认）：新译文一到达就自动朗读；<b>同时保留</b>行尾喇叭，
 *       所以可以随时停止或回听任意一句。</li>
 * </ul>
 *
 * <p>{@code AUTO} 是 {@code TAP} 的超集 —— 这样「切档位」只增不减能力：
 * 从自动切到点按不会突然失去「回听某一句」的手段。
 *
 * <h3>朗读为什么要暂停录音</h3>
 * 外放出来的译文会被麦克风重新采进去（自激），Android 上还会和
 * {@code AudioRecord} 抢音频通路。所以每次朗读都会调
 * {@code Pipeline.holdForPlayback()} 暂停采集，读完自动恢复 —— 时序由
 * {@code MainActivity} 统一调度，不要绕过它。
 *
 * <p>注意 {@code AUTO} 的代价：朗读那几秒采集是停的，说话不会被录到。
 * 这不是 bug，是「播放时自动暂停录音」的直接后果，设置页说明里写明了。
 */
enum SpeechMode {

    OFF("off", "关闭"),
    TAP("tap", "点按朗读"),
    AUTO("auto", "自动朗读");

    /** SharedPreferences 的键名。 */
    static final String KEY = "speech";

    /** 认不出已存的值时用哪一档（与 iOS 的 {@code .fallback} 对齐）。 */
    static final SpeechMode FALLBACK = AUTO;

    final String value;
    final String label;

    SpeechMode(String value, String label) {
        this.value = value;
        this.label = label;
    }

    /**
     * 从偏好里读出来。取值可能来自旧版本或被人手动改过，认不出就回落默认档 ——
     * 绝不能因为一个偏好值就让译文页直接崩掉。
     */
    static SpeechMode of(String raw) {
        for (SpeechMode mode : values()) {
            if (mode.value.equals(raw)) return mode;
        }
        return FALLBACK;
    }

    /** 设置页选择器的说明文案。 */
    String hint() {
        switch (this) {
            case OFF:
                return "不朗读译文";
            case TAP:
                return "译文每行末尾显示喇叭，点哪句读哪句，再点一次停止";
            default:
                return "新译文一到达就自动朗读，译文行末尾的喇叭仍可随时停止或回听。"
                        + "朗读期间会自动暂停录音、读完自动恢复，因此朗读那几秒说的话不会被录到";
        }
    }

    /** 译文行尾是否显示喇叭。 */
    boolean showsSpeaker() { return this != OFF; }

    /** 新译文到达时是否自动朗读。 */
    boolean speaksAutomatically() { return this == AUTO; }
}
