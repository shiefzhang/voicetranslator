package com.vt.voicetranslator;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;

import java.util.HashMap;
import java.util.Locale;

/**
 * 译文朗读 —— 系统 TTS（{@link TextToSpeech}）。对应 iOS 的 {@code SpeechPlayer.swift}。
 *
 * <p><b>离线</b>：用的是设备自带的语音引擎，不走网络、不落盘，与本项目
 * 「音频与对话文本只留在内存」的定位一致。代价是**语音数据得设备上有** ——
 * 不少机器只预装了中英，日/韩/粤要用户自己去「文字转语音」里下载，
 * 所以 {@link #onUnavailable} 是必须处理的正常分支，不是异常。
 *
 * <h3>与录音的关系</h3>
 * 外放出来的译文会被麦克风重新采进去（自激），Android 上还要和 {@code AudioRecord}
 * 抢音频通路。所以调用方必须<b>先</b> {@code Pipeline.holdForPlayback()} 暂停采集、
 * <b>再</b>调 {@link #speak}，收到 {@link Listener#onFinished} 后恢复 —— 见 {@code MainActivity}。
 *
 * <h3>回调契约（重要）</h3>
 * 每次 {@link #speak} 都<b>恰好</b>收到一次 {@link Listener#onFinished} <i>或</i>
 * {@link Listener#onUnavailable}，二选一，不会都来、也不会都不来：
 * <ul>
 *   <li>读完 / 被 {@link #stop()} 打断 / 引擎报错 → {@code onFinished}；</li>
 *   <li>引擎装不上、语种没有语音数据 → {@code onUnavailable}。</li>
 * </ul>
 * 调用方据此释放「暂停录音」，所以这条契约是录音能否恢复的唯一保证。
 * 即便如此，{@code Pipeline} 的播放暂停**另有一层超时兜底**，回调万一丢了也不会永久停摆。
 *
 * <p>所有回调都投递到主线程，调用方不必自己切线程。
 */
final class Speaker {

    interface Listener {
        /** 这一条结束了（读完 / 被停 / 出错）。**恰好一次**。 */
        void onFinished();

        /** 这一条压根没能开始，附带一句可以直接显示的说明。**恰好一次**。 */
        void onUnavailable(String message);
    }

    private static final String TAG_PREFIX = "vt-";

    /**
     * 引擎初始化看门狗：{@code TextToSpeech} 的 init 回调个别 OEM 实现会**永远不来**。
     *
     * <p>没有它，被挂起的那条朗读既不读、也不回 {@code onFinished} —— 调用方只能等
     * {@code Pipeline} 那个 60 秒超时，而这段时间录音是被挂着的。
     * 与 {@link #upperBoundMs} 是同一类保险：<b>收口回调要假设「永远不来」</b>。
     */
    private static final long INIT_WATCHDOG_MS = 3_000L;

    private final Listener listener;
    private final Handler main = new Handler(Looper.getMainLooper());

    private TextToSpeech tts;
    private boolean ready;
    private boolean initializing = true;
    private boolean released;
    private boolean speaking;
    /** 每次 speak 自增，用来认出「哪一条」—— 过期条目的回调直接丢掉。 */
    private int generation;
    private int activeGeneration;

    /** 引擎还没初始化好就被要求朗读时，先记下来，init 完再补上。 */
    private String pendingText;
    private String pendingLanguage;
    private String pendingLabel;
    /** {@link #INIT_WATCHDOG_MS} 到点时执行的动作。 */
    private final Runnable initWatchdog = this::failPendingInit;

    Speaker(Context context, Listener listener) {
        this.listener = listener;
        // onInit 用 post 兜一层：个别实现会在构造器返回**之前**就回调，
        // 那时 tts 字段还没赋值，直接在里面用它就是 NPE。
        tts = new TextToSpeech(context.getApplicationContext(), status -> main.post(() -> handleInit(status)));
    }

    // MARK: - 生命周期

    private void handleInit(int status) {
        initializing = false;
        if (released) return;
        if (status != TextToSpeech.SUCCESS) {
            notifyUnavailable("本机没有可用的语音引擎，请在系统「文字转语音」设置里安装或启用一个");
            return;
        }
        ready = true;
        tts.setOnUtteranceProgressListener(progress);
        if (pendingText != null) {
            // 起来了，看门狗撤掉 —— 免得它把马上要读的这一条误判成「启动超时」。
            main.removeCallbacks(initWatchdog);
            String text = pendingText, language = pendingLanguage, label = pendingLabel;
            pendingText = null;
            pendingLanguage = null;
            pendingLabel = null;
            speak(text, language, label);
        }
    }

    /**
     * init 迟迟不来：把挂起的那条当场作废，给调用方一个交代。
     *
     * <p>宁可说「引擎启动超时」，也不能让这条朗读既不读、也不回 ——
     * 那样录音会被 {@code Pipeline} 挂满 60 秒，用户看到的就是「卡在朗读」。
     */
    private void failPendingInit() {
        if (pendingText == null) return;   // handleInit 已经接手了
        pendingText = null;
        pendingLanguage = null;
        pendingLabel = null;
        notifyUnavailable("语音引擎启动超时，请稍后重试");
    }

    /** 释放引擎。Activity 销毁时调用；之后再用这个实例的 speak 会被忽略。 */
    void release() {
        released = true;
        speaking = false;
        activeGeneration = 0;
        pendingText = null;
        main.removeCallbacks(initWatchdog);
        if (tts != null) {
            // stop 会让在飞的回调变成过期条目，自动被丢弃 —— 不必手动补 onFinished。
            try { tts.stop(); } catch (Exception ignored) {}
            tts.shutdown();
            tts = null;
        }
    }

    // MARK: - 播放

    boolean isSpeaking() { return speaking; }

    /**
     * 朗读一段文本。结果通过 {@link Listener} 回来，**不在返回值里**。
     *
     * @param languageCode  语种码（zh / ja / ko / en / yue / fr / de / ru）
     * @param languageLabel 该语种的显示名，只用来拼「本机没有××的语音数据」这句提示
     */
    void speak(String text, String languageCode, String languageLabel) {
        String trimmed = text == null ? "" : text.trim();
        if (trimmed.isEmpty() || released) return;

        if (!ready) {
            if (initializing) {
                // 引擎还在起，先存着。handleInit 会补发。
                pendingText = trimmed;
                pendingLanguage = languageCode;
                pendingLabel = languageLabel;
                // 但 init 回调个别实现永远不来 —— 到点就当场认输，别把这条挂死。
                main.removeCallbacks(initWatchdog);
                main.postDelayed(initWatchdog, INIT_WATCHDOG_MS);
                return;
            }
            notifyUnavailable("本机没有可用的语音引擎，请在系统「文字转语音」设置里安装或启用一个");
            return;
        }

        int support = tts.setLanguage(locale(languageCode));
        if (support == TextToSpeech.LANG_MISSING_DATA || support == TextToSpeech.LANG_NOT_SUPPORTED) {
            notifyUnavailable("本机没有" + languageLabel + "的语音数据，请在系统「文字转语音」设置里下载后再试");
            return;
        }

        // 打断上一条：先把当前条目作废，它迟到的 onStop 就认不出自己了，
        // 不会误触发 onFinished（否则会把恢复录音提前一次）。
        if (speaking) {
            speaking = false;
            activeGeneration = 0;
            try { tts.stop(); } catch (Exception ignored) {}
        }

        int id = ++generation;
        activeGeneration = id;
        speaking = true;
        String utteranceId = TAG_PREFIX + id;
        HashMap<String, String> params = new HashMap<>();
        // 不传 utteranceId 的话 onDone / onError 根本不会回调（官方文档明确说了）。
        params.put(TextToSpeech.Engine.KEY_PARAM_UTTERANCE_ID, utteranceId);
        int code = tts.speak(trimmed, TextToSpeech.QUEUE_FLUSH, params, utteranceId);
        if (code == TextToSpeech.ERROR) {
            speaking = false;
            activeGeneration = 0;
            notifyUnavailable("语音引擎调用失败，请稍后重试");
            return;
        }
        // 上界看门狗：onDone 万一被引擎吞掉（部分 OEM 引擎会），按字数估一个**宽松**上界
        // 强制收口。宁可多等，也不能把录音挂到 Pipeline 那个 60 秒超时。
        final int watchdogId = id;
        main.postDelayed(() -> {
            if (speaking && activeGeneration == watchdogId) finishIfCurrent(TAG_PREFIX + watchdogId);
        }, upperBoundMs(trimmed));
    }

    /**
     * 一条朗读的时长上界。中文 TTS 默认语速约 4~6 字/秒，这里按 0.7 秒/字算，
     * 相当于给了 3~4 倍余量 —— 正常朗读绝不会被它打断，只有丢失回调才会命中。
     */
    private static long upperBoundMs(String text) {
        return Math.max(15_000L, text.length() * 700L);
    }

    /** 停止朗读；会走一次 {@link Listener#onFinished}，调用方据此恢复录音。 */
    void stop() {
        if (!speaking) return;
        speaking = false;
        activeGeneration = 0;
        if (tts != null) {
            try { tts.stop(); } catch (Exception ignored) {}
        }
        // 主动停也走同一个出口 —— 对调用方来说「停」和「读完」是同一种收敛。
        listener.onFinished();
    }

    // MARK: - 回调

    private final UtteranceProgressListener progress = new UtteranceProgressListener() {
        @Override public void onStart(String utteranceId) {}

        @Override public void onDone(String utteranceId) {
            main.post(() -> finishIfCurrent(utteranceId));
        }

        @Override public void onError(String utteranceId) {
            main.post(() -> finishIfCurrent(utteranceId));
        }

        @Override public void onError(String utteranceId, int errorCode) {
            main.post(() -> finishIfCurrent(utteranceId));
        }

        @Override public void onStop(String utteranceId, boolean interrupted) {
            main.post(() -> finishIfCurrent(utteranceId));
        }
    };

    /** 只有「当前这条」的回调才算数；被打断的旧条目的迟到回调在这里被丢掉。 */
    private void finishIfCurrent(String utteranceId) {
        if (!speaking || !(TAG_PREFIX + activeGeneration).equals(utteranceId)) return;
        speaking = false;
        activeGeneration = 0;
        listener.onFinished();
    }

    private void notifyUnavailable(String message) {
        speaking = false;
        activeGeneration = 0;
        listener.onUnavailable(message);
    }

    // MARK: - 语种映射

    /**
     * 语种码 → {@link Locale}。
     *
     * <p>{@code yue}（粤语）在系统里是「中文（香港）」。fr / de / ru 只能作翻译目标语言，
     * 但也可能成为朗读对象，所以一并映射。
     */
    private static Locale locale(String code) {
        switch (code) {
            case "zh": return Locale.SIMPLIFIED_CHINESE;
            case "yue": return new Locale("zh", "HK");
            case "ja": return Locale.JAPANESE;
            case "ko": return Locale.KOREAN;
            case "fr": return Locale.FRENCH;
            case "de": return Locale.GERMAN;
            case "ru": return new Locale("ru", "RU");
            default: return Locale.US;
        }
    }
}
