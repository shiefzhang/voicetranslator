"""Run all four actual sample WAVs against the packaged ASR and test VAD.
Requires sherpa-onnx, numpy, onnx. Outputs machine-readable measured results.
"""
import json,time,wave
from pathlib import Path
import numpy as np
import onnx
import sherpa_onnx
ROOT=Path(__file__).resolve().parents[1]

def main():
    root=ROOT/'model-work/asr'
    model=onnx.load(str(root/'model.int8.onnx'));onnx.checker.check_model(model)
    meta={x.key:x.value for x in model.metadata_props}
    print('Metadata:',{k:v for k,v in meta.items() if k not in ('neg_mean','inv_stddev')})
    results=[]
    for lang in ['zh','ja','ko','en']:
        r=sherpa_onnx.OfflineRecognizer.from_sense_voice(model=str(root/'model.int8.onnx'),tokens=str(root/'tokens.txt'),language=lang,use_itn=True,num_threads=2)
        wav=ROOT/'model-work/upstream/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/test_wavs'/f'{lang}.wav'
        with wave.open(str(wav)) as w:
            rate=w.getframerate();pcm=np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16).astype(np.float32)/32768
        start=time.perf_counter();s=r.create_stream();s.accept_waveform(rate,pcm);r.decode_stream(s);elapsed=time.perf_counter()-start
        if not s.result.text.strip():raise RuntimeError('Empty ASR output: '+lang)
        results.append({'language':lang,'text':s.result.text,'seconds':elapsed,'audioSeconds':len(pcm)/rate})
        print(lang,s.result.text,elapsed,flush=True)
    config=sherpa_onnx.VadModelConfig();config.silero_vad.model=str(root/'silero_vad.onnx');config.sample_rate=16000
    vad=sherpa_onnx.VoiceActivityDetector(config,buffer_size_in_seconds=60)
    for i in range(0,len(pcm)-512,512):vad.accept_waveform(pcm[i:i+512])
    vad.flush()
    if vad.empty():raise RuntimeError('VAD failed to detect actual speech')
    out=ROOT/'dist/verification/asr-smoke.json';out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps({'runtime':sherpa_onnx.__version__,'platform':'Windows CPU, not Android performance','results':results,'vad':'passed'},ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
