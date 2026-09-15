package com.vt.voicetranslator;
import java.util.Arrays;

/** Sample-count based endpoint controller. Thread-confined to the capture worker. */
final class Segmenter {
    interface Sink { void sentence(float[] audio); }
    private final Sink sink;
    private final int pauseSamples;
    private final float[] current=new float[16000*15+512];
    private final float[] pre=new float[4800];
    private int count,preCount,silence,voiced;
    Segmenter(int pauseMs,Sink sink){this.pauseSamples=pauseMs*16;this.sink=sink;}
    void push(float[] frame,boolean speech){
        if(count==0){
            if(!speech){int keep=Math.min(preCount,pre.length-frame.length);System.arraycopy(pre,preCount-keep,pre,0,keep);System.arraycopy(frame,0,pre,keep,frame.length);preCount=keep+frame.length;return;}
            System.arraycopy(pre,0,current,0,preCount);count=preCount;preCount=0;
        }
        System.arraycopy(frame,0,current,count,frame.length);count+=frame.length;
        if(speech){silence=0;voiced+=frame.length;}else silence+=frame.length;
        if(silence>=pauseSamples||count>=16000*15)flush();
    }
    void flush(){if(count>0&&voiced>=2400)sink.sentence(Arrays.copyOf(current,count));count=0;silence=0;voiced=0;preCount=0;}
    float[] snapshot(){return count>16000&&voiced>=2400?Arrays.copyOf(current,count):null;}
}
