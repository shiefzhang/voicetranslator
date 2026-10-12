package com.vt.voicetranslator;

import android.media.*;
import android.os.Process;
import android.util.Log;
import com.k2fsa.sherpa.onnx.*;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

final class Pipeline {
    private static final String TAG="VT";
    private static final int PAUSE_BACKLOG=4,RESUME_BACKLOG=2;
    /** 与通用模型一致的线程数（translator.cpp 里也是 2）—— 把核心留给采集与识别。 */
    private static final int TRANSLATION_THREADS=2;
    /**
     * 朗读暂停的兜底超时。朗读的回调万一丢了（TTS 引擎异常、进程被打断），
     * 录音不能就此永久停摆 —— 到点自动恢复并说明原因。
     */
    private static final long PLAYBACK_HOLD_TIMEOUT_MS=60_000,PLAYBACK_HOLD_POLL_MS=50;
    interface Listener {
        void state(String status); void draft(String text);
        void focus(long id);
        void sentence(long id,String source,String translation);
        void done();
    }
    private static final class Job {final long id;float[] pcm;String text;Job(long id,float[] pcm){this.id=id;this.pcm=pcm;}}
    private static final class Draft {final long revision;final float[] pcm;Draft(long r,float[] p){revision=r;pcm=p;}}
    private final Listener listener;
    /** 转写模型包目录。**必需**：没有它整个 App 都跑不起来。 */
    private final File asrDir;
    /**
     * 通用翻译模型包目录。**可以为 null** —— 设置成「智能选择」且这个语向在专用包的
     * 接线里时，专用引擎一个人就能跑完，用户不必先导入那 1 GiB 的通用包（v0.2.12）。
     * 走到真正装载那一步若发现专用引擎用不上，会抛一条明确的错误，而不是静默变砖。
     */
    private final File mtDir;
    /** 专用翻译引擎的包目录；这个安装包没带、或用户把它删了就是 null（回落通用模型）。 */
    private final File opusDir;
    /** 设置页选定的翻译后端。决定这次会话走不走专用引擎 —— 见 loadTranslationModel()。 */
    private final TranslationBackend backend;
    /** 通用模型的引擎名；没有通用包时为 null（此时只能靠专用引擎）。 */
    private final String mtEngine;
    private final String source,target;
    private final int pauseMs;
    /** 本次会话最终用上了专用引擎没有。只在翻译线程里读写，不必 volatile。 */
    private boolean opusReady;
    private final BlockingQueue<Job> asrJobs=new ArrayBlockingQueue<>(8);
    private final BlockingQueue<Job> mtJobs=new LinkedBlockingQueue<>();
    private final AtomicReference<Draft> latest=new AtomicReference<>();
    private final AtomicInteger pendingAsr=new AtomicInteger();
    private final AtomicLong generation=new AtomicLong();
    private volatile boolean recording,backlogPaused,stopRequested,captureDone,asrDone,cancelled,recognizing;
    /** 朗读请求暂停采集（调用方设）。采集线程看到它就真的把音频通路让出去。 */
    private volatile boolean playbackHold;
    /** 采集**确实已经停下**了（采集线程设）。调用方等这一位再开始出声。 */
    private volatile boolean playbackHeld;
    private volatile AudioRecord recorder;
    private long nextId;
    /**
     * @param mt 通用翻译模型包；**允许为 null**（该语向由专用引擎覆盖、用户还没导入通用包）
     */
    Pipeline(File asr,File mt,File opus,TranslationBackend backend,String source,String target,int pauseMs,Listener l){
        asrDir=asr;mtDir=mt;opusDir=opus;this.backend=backend;this.source=source;this.target=target;this.pauseMs=pauseMs;listener=l;
        // 构造期只校验**通用模型**的清单 —— 专用引擎是可选加速，它的可用性在
        // loadTranslationModel() 里判定并允许失败回落，不能拦在这里。
        // 没有通用包时（专用引擎覆盖本语向）这一项就是 null，不校验也不用校验。
        try{mtEngine=mt==null?null:ModelPackages.engine(mt);}catch(Exception e){throw new IllegalArgumentException("翻译模型清单无效",e);}
    }
    void start(){new Thread(this::run,"vt-session").start();}
    boolean isRecording(){return recording;}
    void stop(){stopRequested=true;recording=false;AudioRecord r=recorder;if(r!=null){try{r.stop();}catch(Exception ignored){}}}
    /**
     * 请求暂停采集，把音频通路让给朗读。
     *
     * @return {@code true} 表示<b>需要等</b>（正在采集，得等 {@link #isPlaybackHeld()} 变 true 再出声）；
     *         {@code false} 表示本来就没在采集，直接朗读即可，不必等。
     *
     * <p>为什么要等：{@code AudioRecord.stop()} 是异步生效的。不等就出声，
     * 外放会被还没停下的麦克风采进去。
     */
    boolean holdForPlayback(){
        if(!recording||stopRequested)return false;
        playbackHold=true;
        return true;
    }
    /** 朗读结束，放开采集。采集线程会自己把 {@code AudioRecord} 重新拉起来。幂等。 */
    void releasePlaybackHold(){playbackHold=false;}
    /** 采集是否**确实已经停下**（音频通路已让出）。 */
    boolean isPlaybackHeld(){return playbackHeld;}
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
    /**
     * 装载翻译引擎 —— 设置页那个「翻译引擎」选项真正生效的地方。
     *
     * 两条路径：
     *   • 「智能选择」且该语向在专用包接线里 → OpusNmt（OPUS-MT）
     *   • 其余一律 → LocalLlm（通用模型，任何构建下都可用）
     *
     * <b>专用引擎失败时一律静默回落通用模型</b>，只写日志。这是刻意的：这个选项不该让
     * App 变砖 ——「智能选择」在没有专用翻译包时（用户没导入、或自己移除了）必须等价于
     * 「统一模型」。判定分三层，每一层都能独立地让这次会话回到通用模型：
     * <ol>
     *   <li>设置档位与语向（纯策略，不碰磁盘）；</li>
     *   <li>有没有可用的专用模型包（用户没导入、构建时 `--omit opus` 都会命中）；</li>
     *   <li>native 库/ORT 可不可用、包能不能装载、这个语向在不在 routes.txt 里。</li>
     * </ol>
     *
     * <p>⚠️ 三层<b>全都</b>落到「回落通用模型」时，通用模型必须真的在。v0.2.12 允许
     * 通用包缺席（靠专用引擎开跑），所以这里要显式区分两种情况：用户有通用包 → 照旧回落；
     * 用户没有 → <b>报错并说清该做什么</b>。没有这一层，缺包会被 LocalLlm 报成
     * 「加载模型失败」，用户根本不知道要去导入。
     */
    private void loadTranslationModel(){
        if(!loadOpusEngineIfPreferred()){
            if(mtDir==null||mtEngine==null){
                throw new IllegalStateException(source+"→"+target
                        +" 这次没能用上专用引擎，通用翻译模型包也还没导入。"
                        +"请在设置里导入翻译模型包");
            }
            LocalLlm.load(new File(mtDir,"model.gguf").getAbsolutePath().getBytes(StandardCharsets.UTF_8),mtEngine.getBytes(StandardCharsets.UTF_8));
        }
    }

    /** 试着装专用引擎。返回 false 表示「这次不该用、或用不了」，调用方回落通用模型。 */
    private boolean loadOpusEngineIfPreferred(){
        if(!backend.prefersOpus(source,target))return false;
        if(opusDir==null){
            Log.i(TAG,"翻译引擎："+source+"→"+target+" 按设置为专用引擎，但没有可用的专用模型包，回落通用模型");
            return false;
        }
        if(!OpusNmt.isAvailable()){
            Log.i(TAG,"翻译引擎：ONNX Runtime 或 native 库不可用，回落通用模型");
            return false;
        }
        try{
            OpusNmt.load(opusDir.getAbsolutePath().getBytes(StandardCharsets.UTF_8),TRANSLATION_THREADS);
            byte[] from=source.getBytes(StandardCharsets.UTF_8),to=target.getBytes(StandardCharsets.UTF_8);
            if(!OpusNmt.supports(from,to)){
                // 包装起来了但接线里没有这个语向 —— 不 close，句柄留着给别的语向用。
                Log.i(TAG,"翻译引擎：专用包不覆盖 "+source+"→"+target+"，回落通用模型");
                return false;
            }
            opusReady=true;
            Log.i(TAG,"翻译引擎："+source+"→"+target+" 使用专用引擎（"
                    +new String(OpusNmt.describe(),StandardCharsets.UTF_8)+"）");
            return true;
        }catch(Throwable t){
            opusReady=false;
            Log.w(TAG,"翻译引擎：专用引擎装载失败，回落通用模型："+t.getMessage());
            return false;
        }
    }

    private String translate(Job j){
        if(opusReady){
            // 专用引擎没有流式 partial：一次贪心解码几十毫秒到一两秒，分片回调只是抖动。
            return new String(OpusNmt.translate(j.text.getBytes(StandardCharsets.UTF_8),
                    source.getBytes(StandardCharsets.UTF_8),target.getBytes(StandardCharsets.UTF_8)),
                    StandardCharsets.UTF_8).trim();
        }
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
                if(playbackHold){
                    // 朗读暂停：优先级高于积压暂停 —— 它是用户直接触发的，也占用音频通路。
                    // 流程与积压暂停一致：真的停掉采集 → 阻塞等（这一次等的是朗读结束）
                    // → 重新 start。停/等/启全在这条采集线程上，避免与调用方竞态。
                    try{r.stop();}catch(Exception ignored){}have=0;latest.set(null);listener.draft("");
                    segmenter.reset();   // 丢掉半句：朗读前后的语音不该拼成一句
                    playbackHeld=true;listener.state("正在朗读译文，录音已暂停");
                    long deadline=System.currentTimeMillis()+PLAYBACK_HOLD_TIMEOUT_MS;
                    while(recording&&!stopRequested&&playbackHold&&System.currentTimeMillis()<deadline)Thread.sleep(PLAYBACK_HOLD_POLL_MS);
                    // 到点还没放开 = 朗读的回调丢了，走超时兜底，别把录音永久挂住。
                    boolean timedOut=playbackHold&&System.currentTimeMillis()>=deadline;
                    playbackHold=false;playbackHeld=false;
                    if(!recording||stopRequested)break;
                    segmenter.reset();r.startRecording();lastDraft=System.nanoTime();
                    listener.state(timedOut?"朗读超时，录音已自动继续 · 停顿 "+(pauseMs/1000f)+" 秒自动翻译":"朗读结束，录音已继续 · 停顿 "+(pauseMs/1000f)+" 秒自动翻译");
                    continue;
                }
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
    static String languageName(String s){switch(s){case "zh":return "Chinese";case "ja":return "Japanese";case "ko":return "Korean";case "yue":return "Cantonese";case "fr":return "French";case "de":return "German";case "ru":return "Russian";default:return "English";}}
}
