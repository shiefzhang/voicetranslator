package com.vt.voicetranslator;
import android.app.Instrumentation;
import android.os.Bundle;
import android.content.Context;
import android.net.Uri;
import com.k2fsa.sherpa.onnx.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import org.json.*;

/** Explicitly invoked test APK only. No diagnostic intent exported by production app. */
public class SmokeInstrumentation extends Instrumentation {
    @Override public void onCreate(Bundle args){super.onCreate(args);start();}
    @Override public void onStart(){
        Bundle result=new Bundle();JSONObject report=new JSONObject();
        try{
            Context c=getTargetContext();File input=c.getExternalFilesDir(null);
            File asr=ModelPackages.install(c,Uri.fromFile(new File(input,"sensevoice-zh-ja-ko-en.vtmodel")),"asr",s->{});
            File mt=ModelPackages.install(c,Uri.fromFile(new File(input,"qwen25-3b-zh-ja-ko-en.vtmodel")),"translation",s->{});
            report.put("packageImport","passed");save(report);
            JSONArray rows=new JSONArray();String[] codes={"zh","ja","ko","en"};
            for(String code:codes){
                OfflineSenseVoiceModelConfig sc=new OfflineSenseVoiceModelConfig();sc.setModel(new File(asr,"model.int8.onnx").getPath());sc.setLanguage(code);sc.setUseInverseTextNormalization(true);
                OfflineModelConfig mc=new OfflineModelConfig();mc.setSenseVoice(sc);mc.setTokens(new File(asr,"tokens.txt").getPath());mc.setNumThreads(2);
                OfflineRecognizerConfig rc=new OfflineRecognizerConfig();rc.setModelConfig(mc);OfflineRecognizer r=new OfflineRecognizer(null,rc);
                OfflineStream stream=r.createStream();try{stream.acceptWaveform(wav(new File(input,code+".wav")),16000);r.decode(stream);String text=r.getResult(stream).getText();if(text.trim().isEmpty())throw new AssertionError("Empty ASR "+code);rows.put(new JSONObject().put("language",code).put("text",text));}finally{stream.release();r.release();}
            }
            report.put("asr",rows);save(report);
            LocalLlm.load(new File(mt,"model.gguf").getPath().getBytes(StandardCharsets.UTF_8),ModelPackages.engine(mt).getBytes(StandardCharsets.UTF_8));JSONArray translations=new JSONArray();
            String[] phrases={"最近的地铁站在哪里？","一番近い地下鉄の駅はどこですか？","가장 가까운 지하철역은 어디인가요?","Where is the nearest subway station?"};
            try{for(int i=0;i<4;i++)for(int j=0;j<4;j++)if(i!=j){String text=new String(LocalLlm.translate(phrases[i].getBytes(StandardCharsets.UTF_8),Pipeline.languageName(codes[i]).getBytes(StandardCharsets.UTF_8),Pipeline.languageName(codes[j]).getBytes(StandardCharsets.UTF_8),null),StandardCharsets.UTF_8);if(text.isBlank())throw new AssertionError("Empty translation");translations.put(new JSONObject().put("from",codes[i]).put("to",codes[j]).put("text",text));report.put("translations",translations);save(report);}}finally{LocalLlm.close();}
            report.put("translations",translations);report.put("status","passed");result.putString("result","passed");
        }catch(Throwable e){try{report.put("status","failed");report.put("error",e.toString());}catch(Exception ignored){}result.putString("error",e.toString());}
        try{File out=new File(getTargetContext().getExternalFilesDir(null),"android-smoke.json");try(FileOutputStream f=new FileOutputStream(out)){f.write(report.toString(2).getBytes(StandardCharsets.UTF_8));}}catch(Exception e){result.putString("writeError",e.toString());}
        finish(result.containsKey("error")?1:-1,result);
    }
    private void save(JSONObject report)throws Exception{try(FileOutputStream f=new FileOutputStream(new File(getTargetContext().getExternalFilesDir(null),"android-smoke.json"))){f.write(report.toString(2).getBytes(StandardCharsets.UTF_8));}}
    private static float[] wav(File file)throws Exception {
        try(RandomAccessFile f=new RandomAccessFile(file,"r")){
            f.seek(12);while(f.getFilePointer()+8<f.length()){
                byte[] name=new byte[4];f.readFully(name);int size=Integer.reverseBytes(f.readInt());
                if(new String(name,StandardCharsets.US_ASCII).equals("data")){float[] out=new float[size/2];for(int i=0;i<out.length;i++)out[i]=Short.reverseBytes(f.readShort())/32768f;return out;}
                f.seek(f.getFilePointer()+size+(size%2));
            }throw new IOException("WAV data missing");
        }
    }
}
