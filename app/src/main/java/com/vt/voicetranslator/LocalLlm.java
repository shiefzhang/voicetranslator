package com.vt.voicetranslator;
final class LocalLlm {
    interface Progress { void onPartial(byte[] text); }
    static { System.loadLibrary("vt-llama"); }
    static native void load(byte[] path, byte[] engine);
    static native byte[] translate(byte[] text, byte[] sourceLanguage, byte[] targetLanguage, Progress progress);
    static native void cancel();
    static native void close();
}
