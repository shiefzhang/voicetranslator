"""Download pinned model weights, stage licenses and build both Android packages.
No GPU, PyTorch or conversion required for the supported pre-quantized defaults.
"""
import argparse
import json
import shutil
import tarfile
from pathlib import Path
from fetch import download,sha256
from model_pack import build

ROOT=Path(__file__).resolve().parents[1]
ASR_HASH='7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e'
# Qwen2.5-1.5B-Instruct Q4_K_M。
#
# ⚠️ 这里曾是 3B 的哈希（626b4a66…）。全项目统一到 1.5B Q4_K_M 之后必须同步换掉 ——
# 留着旧哈希的后果不是「报错」，而是 fetch.download 的校验永远失败，看起来像来源不可靠。
# 本值取自实际随包分发的 model.gguf（models/qwen25-1.5b-q4km-zh-ja-ko-en-yue.vtmodel
# 内 manifest.json 的 files[model.gguf].sha256），这是权威值，不是估算。
QWEN_HASH='6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e'
VAD_HASH='9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6'

def prepare(kind):
    cache=ROOT/'downloads';cache.mkdir(exist_ok=True)
    dest=ROOT/'model-work'/kind;dest.mkdir(parents=True,exist_ok=True)
    licenses=dest/'licenses';licenses.mkdir(exist_ok=True)
    if kind=='asr':
        archive=cache/'sensevoice-api.tar.bz2'
        download('https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/assets/288366523',archive,ASR_HASH)
        upstream=ROOT/'model-work/upstream'
        upstream.mkdir(parents=True,exist_ok=True)
        with tarfile.open(archive) as t:t.extractall(upstream,filter='data')
        model=upstream/'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
        for name in ['model.int8.onnx','tokens.txt']:shutil.copy2(model/name,dest/name)
        download('https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/assets/271935959',cache/'silero_vad.onnx',VAD_HASH)
        shutil.copy2(cache/'silero_vad.onnx',dest/'silero_vad.onnx')
        shutil.copy2(model/'LICENSE',licenses/'upstream-reference.txt')
        for name in ['FunASR-MODEL-LICENSE','Silero-LICENSE']:
            p=cache/name
            if not p.is_file():raise FileNotFoundError(f'{p}: run scripts/bootstrap.py first')
            shutil.copy2(p,licenses/(name+'.txt'))
        shutil.copy2(model/'export-onnx.py',ROOT/'scripts/vendor/sensevoice-export-onnx.py')
        (dest/'SOURCE.txt').write_text('SenseVoiceSmall (Alibaba / FunAudioLLM), ONNX export by k2-fsa.\nArchive SHA256: '+ASR_HASH+'\nSilero VAD SHA256: '+VAD_HASH+'\nSupported App languages: Chinese, Japanese, Korean, English.\n',encoding='utf-8')
        metadata='asr';output='sensevoice-zh-ja-ko-en.vtmodel'
    else:
        # 全项目唯一的翻译档位：Qwen2.5-1.5B-Instruct Q4_K_M。
        # 之所以钉死 1.5B Q4_K_M 而不是「取最小/取最大」，判据在
        # model-tiers.py 顶部的 RECOMMENDED 注释和「量化档位与Qwen3.5端侧实测报告.html」：
        # Q4_K_M 在 A8 的 armv8-a 基线口径下速度第一、质量优于 Q4_0，
        # 而 Q4_0 中译粤会稳定复读崩坏。
        repo='https://www.modelscope.cn/models/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/master'
        model=cache/'qwen2.5-1.5b-instruct-q4_k_m.gguf'
        download(repo+'/qwen2.5-1.5b-instruct-q4_k_m.gguf',model,QWEN_HASH)
        download(repo+'/LICENSE',cache/'Qwen-LICENSE','832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e')
        shutil.copy2(model,dest/'model.gguf');shutil.copy2(cache/'Qwen-LICENSE',licenses/'Qwen-Apache-2.0.txt')
        (dest/'SOURCE.txt').write_text('Qwen2.5-1.5B-Instruct Q4_K_M, official Qwen distribution.\nUpstream SHA256: '+QWEN_HASH+'\nSource: '+repo+'/qwen2.5-1.5b-instruct-q4_k_m.gguf\n',encoding='utf-8')
        metadata='qwen25-1.5b-q4km';output='qwen25-1.5b-q4km-zh-ja-ko-en-yue.vtmodel'
    # 产物直接落 `models/` —— `model-tiers.py` 只扫这一处，
    # 写去 dist/models/ 会多出一次「为什么推荐的不是我刚做的那个包」的手工拷贝。
    build(dest,ROOT/'model-manifests'/f'{metadata}.json',ROOT/'models'/output)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--kind',choices=['asr','translation','all'],default='all');a=p.parse_args()
    (ROOT/'scripts/vendor').mkdir(parents=True,exist_ok=True)
    for kind in (['asr','translation'] if a.kind=='all' else [a.kind]):prepare(kind)
