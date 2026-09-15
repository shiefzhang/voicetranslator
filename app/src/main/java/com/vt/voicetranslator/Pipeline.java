package com.vt.voicetranslator;

import android.media.*;
import android.os.Process;
import com.k2fsa.sherpa.onnx.*;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

final class Pipeline {
    private static final int PAUSE_BACKLOG=4,RESUME_BACKLOG=2;
    interface Listener {
        void state(String status); void draft(String text);
        void focus(long id);
        void sentence(long id,String source,String translation);
        void done();
    }
    private static final class Job {final long id;float[] pcm;String text;Job(long id,float[] pcm){this.id=id;this.pcm=pcm;}}
    private static final class Draft {final long revision;final float[] pcm;Draft(long r,float[] p){revision=r;pcm=p;}}
    private final Listener listener;
    private final File asrDir,mtDir;
    private final String mtEngine;
    private final String source,target;
    private final int pauseMs;
    private final BlockingQueue<Job> asrJobs=new ArrayBlockingQueue<>(8);
    private final BlockingQueue<Job> mtJobs=new LinkedBlockingQueue<>();
    private final AtomicReference<Draft> latest=new AtomicReference<>();
    private final AtomicInteger pendingAsr=new AtomicInteger();
    private final AtomicLong generation=new AtomicLong();
    private volatile boolean recording,backlogPaused,stopRequested,captureDone,asrDone,cancelled,recognizing;
    private volatile AudioRecord recorder;
    private long nextId;
    Pipeline(File asr,File mt,String source,String target,int pauseMs,Listener l){
        asrDir=asr;mtDir=mt;this.source=source;this.target=target;this.pauseMs=pauseMs;listener=l;
        try{mtEngine=ModelPackages.engine(mt);}catch(Exception e){throw new IllegalArgumentException("翻译模型清单无效",e);}
    }
    void start(){new Thread(this::run,"vt-session").start();}
    boolean isRecording(){return recording;}
    void stop(){stopRequested=true;recording=false;AudioRecord r=recorder;if(r!=null){try{r.stop();}catch(Exception ignored){}}}
    void cancel(){cancelled=true;stop();LocalLlm.cancel();}
    private String recognize(OfflineRecognizer asr,float[] pcm){
        OfflineStream stream=asr.createStream();
        try{stream.acceptWaveform(pcm,16000);asr.decode(stream);return asr.getResult(stream).getText().trim();}finally{stream.release();}
    }
    private void run(){
        // Recognition owns the latency budget.  The translation worker is
        // deliberately background-priority so llama.cpp cannot starve ASR.
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_DISPLAY);
        OfflineRecognizer asr=null;Vad vad=null;Thread audio=null,translation=null;
        try{
            listener.state("正在加载模型…");
            OfflineSenseVoiceModelConfig sense=new OfflineSenseVoiceModelConfig();sense.setModel(new File(asrDir,"model.int8.onnx").getPath());sense.setLanguage(source);sense.setUseInverseTextNormalization(true);
            OfflineModelConfig model=new OfflineModelConfig();model.setSenseVoice(sense);model.setTokens(new File(asrDir,"tokens.txt").getPath());model.setNumThreads(2);model.setProvider("cpu");
            OfflineRecognizerConfig config=new OfflineRecognizerConfig();config.setModelConfig(model);asr=new OfflineRecognizer(null,config);
            SileroVadModelConfig silero=new SileroVadModelConfig();silero.setModel(new File(asrDir,"silero_vad.onnx").getPath());silero.setWindowSize(512);
            VadModelConfig vc=new VadModelConfig();vc.setSileroVadModelConfig(silero);vc.setSampleRate(16000);vc.setNumThreads(1);vad=new Vad(null,vc);
            loadTranslationModel();
            if(stopRequested)return;
            recording=true;listener.state("正在录音 · 停顿 "+(pauseMs/1000f)+" 秒自动翻译");
            translation=new Thread(this::translateLoop,"vt-translation");translation.start();
            Vad finalVad=vad;audio=new Thread(()->capture(finalVad),"vt-audio");audio.start();
            while(!captureDone||!asrJobs.isEmpty()){
                if(cancelled){asrJobs.clear();latest.set(null);break;}
                Job j=asrJobs.poll();
                if(j!=null){
                    latest.set(null);
                    recognizing=true;
                    try{j.text=recognize(asr,j.pcm);j.pcm=null;
                        if(j.text.isBlank()){listener.sentence(j.id,"未识别到有效语音","");continue;}
                        if(j.text.codePoints().filter(Character::isLetterOrDigit).limit(2).count()<2){listener.sentence(j.id,j.text,"（不足两个字，已跳过）");continue;}
                        listener.sentence(j.id,j.text,"翻译中…");mtJobs.put(j);
                    }catch(Exception e){listener.sentence(j.id,"转写失败",e.getMessage());}
                    finally{recognizing=false;pendingAsr.decrementAndGet();}
                }else{
                    Draft draft=latest.getAndSet(null);
                    if(draft!=null&&!stopRequested){String text=recognize(asr,draft.pcm);if(draft.revision==generation.get()&&recording)listener.draft(text);}
                    else Thread.sleep(30);
                }
            }
        }catch(Throwable e){listener.state("错误："+(e.getMessage()==null?e.getClass().getSimpleName():e.getMessage()));}
        finally{
            stop();if(audio!=null){try{audio.join();}catch(InterruptedException e){Thread.currentThread().interrupt();}}
            captureDone=true;asrDone=true;
            if(translation!=null){try{translation.join();}catch(InterruptedException e){Thread.currentThread().interrupt();}}
            if(vad!=null)vad.release();if(asr!=null)asr.release();listener.draft("");listener.done();
        }
    }
    private void translateLoop(){
        Process.setThreadPriority(Process.THREAD_PRIORITY_BACKGROUND);
        while(!asrDone||!mtJobs.isEmpty()){
            try{
                Job j=mtJobs.poll(100,TimeUnit.MILLISECONDS);if(j==null)continue;
                if(cancelled){listener.sentence(j.id,j.text,"已取消");continue;}
                // Do not begin another expensive LLM pass while committed ASR
                // work is waiting. Once inference starts, Linux nice priority
                // keeps its native llama.cpp workers behind capture and ASR.
                while(!cancelled&&(recognizing||!asrJobs.isEmpty()))Thread.sleep(25);
                listener.focus(j.id);
                try{
                    String result=translate(j);
                    listener.sentence(j.id,j.text,result.trim());
                }
                catch(Exception e){listener.sentence(j.id,j.text,"翻译失败："+e.getMessage());}
            }catch(InterruptedException e){Thread.currentThread().interrupt();return;}
        }
    }
    private void loadTranslationModel(){LocalLlm.load(new File(mtDir,"model.gguf").getAbsolutePath().getBytes(StandardCharsets.UTF_8),mtEngine.getBytes(StandardCharsets.UTF_8));}
    private String translate(Job j){
        boolean gemma=mtEngine.equals("llama-gemma3");
        String from=gemma?source:languageName(source),to=gemma?target:languageName(target);
        byte[] result=LocalLlm.translate(j.text.getBytes(StandardCharsets.UTF_8),from.getBytes(StandardCharsets.UTF_8),to.getBytes(StandardCharsets.UTF_8),partial->listener.sentence(j.id,j.text,new String(partial,StandardCharsets.UTF_8).trim()+"▌"));
        return new String(result,StandardCharsets.UTF_8);
    }
    private void capture(Vad vad){
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO);
        Segmenter segmenter=new Segmenter(pauseMs,pcm->{
            generation.incrementAndGet();latest.set(null);listener.draft("");
            long id=++nextId;Job job=new Job(id,pcm);pendingAsr.incrementAndGet();
            if(!asrJobs.offer(job)){pendingAsr.decrementAndGet();listener.state("转写队列已满，请停止后重试");recording=false;return;}
            listener.sentence(id,"正在转写…","等待转写…");
            if(pendingAsr.get()>=PAUSE_BACKLOG&&!stopRequested){backlogPaused=true;listener.state("转写暂时跟不上，录音已自动暂停 · 待转写 "+pendingAsr.get()+" 句");}
        });
        try{
            int min=AudioRecord.getMinBufferSize(16000,AudioFormat.CHANNEL_IN_MONO,AudioFormat.ENCODING_PCM_16BIT);
            if(min<=0)throw new IllegalStateException("设备不支持 16kHz 录音");
            AudioRecord r=new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION,16000,AudioFormat.CHANNEL_IN_MONO,AudioFormat.ENCODING_PCM_16BIT,Math.max(min*4,8192));recorder=r;
            if(r.getState()!=AudioRecord.STATE_INITIALIZED)throw new IllegalStateException("麦克风初始化失败");
            if(stopRequested)return;r.startRecording();short[] buffer=new short[512];int have=0;long lastDraft=System.nanoTime();
            while(recording&&!stopRequested){
                if(backlogPaused){
                    try{r.stop();}catch(Exception ignored){}have=0;latest.set(null);listener.draft("");
                    while(recording&&!stopRequested&&pendingAsr.get()>RESUME_BACKLOG)Thread.sleep(50);
                    if(!recording||stopRequested)break;
                    backlogPaused=false;r.startRecording();lastDraft=System.nanoTime();
                    listener.state("处理速度已恢复，录音已自动继续 · 停顿 "+(pauseMs/1000f)+" 秒自动翻译");
                    continue;
                }
                int n=r.read(buffer,have,512-have);if(n<0){if(!stopRequested)throw new IllegalStateException("麦克风读取失败："+n);break;}if(n==0)continue;
                have+=n;if(have<512)continue;float[] pcm=new float[512];for(int i=0;i<512;i++)pcm[i]=buffer[i]/32768f;have=0;
                boolean speech=vad.compute(pcm)>=0.5f;segmenter.push(pcm,speech);
                if(System.nanoTime()-lastDraft>1_500_000_000L&&pendingAsr.get()==0){float[] snapshot=segmenter.snapshot();latest.set(snapshot==null?null:new Draft(generation.get(),snapshot));lastDraft=System.nanoTime();}
            }
        }catch(SecurityException e){listener.state("录音错误：麦克风权限已撤销，请重新授权");}
        catch(Exception e){listener.state("录音错误："+e.getMessage());}
        finally{recording=false;segmenter.flush();latest.set(null);captureDone=true;AudioRecord r=recorder;recorder=null;if(r!=null){try{r.stop();}catch(Exception ignored){}r.release();}}
    }
    static String languageName(String s){switch(s){case "zh":return "Chinese";case "ja":return "Japanese";case "ko":return "Korean";default:return "English";}}
}
