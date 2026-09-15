package com.vt.voicetranslator;
import java.util.*;
public class SegmenterTest {
    static void check(boolean value,String msg){if(!value)throw new AssertionError(msg);}
    public static void main(String[] args){
        List<float[]> out=new ArrayList<>();Segmenter s=new Segmenter(800,out::add);float[] block=new float[512];Arrays.fill(block,0.2f);
        for(int i=0;i<50;i++)s.push(block,false);check(out.isEmpty(),"Silence created sentence");
        for(int i=0;i<32;i++)s.push(block,true);
        for(int i=0;i<12;i++)s.push(block,false);check(out.isEmpty(),"Short pause split sentence");
        for(int i=0;i<16;i++)s.push(block,true);
        for(int i=0;i<25;i++)s.push(block,false);check(out.size()==1,"Endpoint missing");
        s.flush();check(out.size()==1,"Duplicate final flush");
        for(int i=0;i<20;i++)s.push(block,true);s.flush();check(out.size()==2,"Stop lost tail");
        check(out.get(1).length==20*512,"Previous audio leaked");
        for(int i=0;i<500;i++)s.push(block,true);check(out.size()>=3,"Long sentence not bounded");
        for(float[] sentence:out)check(sentence.length<=240512,"Buffer exceeded bound");
        System.out.println("Segmenter: silence / pause / resume / stop / isolation / long audio passed");
    }
}
