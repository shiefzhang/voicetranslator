"""Prepare pinned native source, Android jars and license files. Python 3.12+."""
import base64,json,tarfile,urllib.request
from pathlib import Path
from fetch import download,sha256
ROOT=Path(__file__).resolve().parents[1]
COMMIT='6e14286edaa60a223292c8a996506905b2f66f66'

def main():
    cache=ROOT/'downloads';cache.mkdir(exist_ok=True)
    libs=ROOT/'app/libs';libs.mkdir(parents=True,exist_ok=True)
    artifacts=[
      ('https://jitpack.io/com/github/k2-fsa/sherpa-onnx/sherpa-onnx/v1.13.6/sherpa-onnx-v1.13.6.aar','sherpa-onnx-v1.13.6.aar','0012d9a28f15bd6fb966b62b70a75da3990512fdccce28b83098248ce4be1698'),
      ('https://repo.maven.apache.org/maven2/org/jetbrains/kotlin/kotlin-stdlib/2.0.21/kotlin-stdlib-2.0.21.jar','kotlin-stdlib-2.0.21.jar','f31cc53f105a7e48c093683bbd5437561d1233920513774b470805641bedbc09'),
      ('https://repo.maven.apache.org/maven2/org/jetbrains/annotations/23.0.0/annotations-23.0.0.jar','annotations-23.0.0.jar','7b0f19724082cbfcbc66e5abea2b9bc92cf08a1ea11e191933ed43801eb3cd05')]
    for url,name,digest in artifacts:download(url,libs/name,digest)
    source=ROOT/'third_party/llama.cpp'
    if not (source/'include/llama.h').is_file():
        archive=download(f'https://codeload.github.com/ggml-org/llama.cpp/tar.gz/{COMMIT}',cache/'llama.tar.gz','2d82acb3734f50bf23f33b1fb431246611eb3b31923569319e2824be8b3842a1')
        source.parent.mkdir(exist_ok=True)
        with tarfile.open(archive) as t:t.extractall(source.parent,filter='data')
        (source.parent/f'llama.cpp-{COMMIT}').rename(source)
    for name,repo,path,digest in [
      ('FunASR-MODEL-LICENSE','modelscope/FunASR','MODEL_LICENSE','7dba975a2069691db4992b0592d70828b330d2f8a30a71450f4e152a554e84f8'),
      ('Silero-LICENSE','snakers4/silero-vad','LICENSE','2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b')]:
        target=cache/name
        if target.exists() and sha256(target)==digest:continue
        data=json.load(urllib.request.urlopen(f'https://api.github.com/repos/{repo}/contents/{path}',timeout=30))
        content=base64.b64decode(data['content'])
        import hashlib
        if hashlib.sha256(content).hexdigest()!=digest:raise ValueError('License changed: review before accepting '+name)
        target.write_bytes(content)
    print('Pinned dependencies ready.')
if __name__=='__main__':main()
