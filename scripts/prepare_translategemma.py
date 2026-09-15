"""Prepare official TranslateGemma 4B text-only Q3_K_M/Q4_K_M packages.

The Google repository is gated. Accept the Gemma terms on Hugging Face first,
then authenticate with `hf auth login` or set HF_TOKEN. The vision projector is
never converted or packaged.
"""
import argparse,json,shutil,subprocess,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
REPO='google/translategemma-4b-it'
REVISION='10042cb0e6e7fdce748996a71dc3dc432a4e0c89'

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--hf-dir',type=Path,help='Existing official HF snapshot; skips download')
    p.add_argument('--quantize-bin',type=Path,default=ROOT/'.tools/llama-win/llama-quantize.exe')
    p.add_argument('--keep-f16',action='store_true')
    args=p.parse_args()
    hf_dir=args.hf_dir
    if hf_dir is None:
        try:from huggingface_hub import snapshot_download
        except ImportError as e:raise SystemExit('Install huggingface_hub first: python -m pip install huggingface_hub') from e
        hf_dir=Path(snapshot_download(
            REPO,revision=REVISION,local_dir=ROOT/'downloads/translategemma-4b-it',
            allow_patterns=['*.json','*.jinja','*.model','*.safetensors','README.md']))
    config=json.loads((hf_dir/'config.json').read_text(encoding='utf-8'))
    if not any('Gemma3' in x for x in config.get('architectures',[])):
        raise SystemExit('The supplied directory is not a Gemma 3 checkpoint')
    work=ROOT/'model-work/translategemma-4b'
    if work.exists():raise SystemExit(f'Refusing to overwrite existing work directory: {work}')
    subprocess.run([sys.executable,str(ROOT/'scripts/convert_models.py'),'translategemma',
                    '--hf-dir',str(hf_dir),'--quantize-bin',str(args.quantize_bin),
                    '--output-root',str(work)],check=True)
    source=(f'TranslateGemma 4B IT, official Google checkpoint, text-only GGUF.\n'
            f'Source: https://huggingface.co/{REPO}\nRevision: {REVISION}\n'
            'The multimodal projector is intentionally excluded.\n'
            'Terms: https://ai.google.dev/gemma/terms\n')
    for quant,folder in [('Q3_K_M','q3_k_m'),('Q4_K_M','q4_k_m')]:
        dest=work/folder
        licenses=dest/'licenses';licenses.mkdir()
        (dest/'SOURCE.txt').write_text(source+f'Quantization: {quant}\n',encoding='utf-8')
        readme=hf_dir/'README.md'
        if readme.is_file():shutil.copy2(readme,licenses/'TranslateGemma-model-card.md')
        (licenses/'Gemma-Terms.txt').write_text(
            'This model is governed by the Gemma Terms of Use. Review the current terms at:\n'
            'https://ai.google.dev/gemma/terms\n',encoding='utf-8')
        manifest=ROOT/'model-manifests'/f'translategemma-4b-{folder}.json'
        output=ROOT/'dist/models'/f'translategemma-4b-text-{folder}.vtmodel'
        subprocess.run([sys.executable,str(ROOT/'scripts/model_pack.py'),'build',str(dest),str(manifest),str(output)],check=True)
    if not args.keep_f16:(work/'translategemma-4b-text-f16.gguf').unlink()

if __name__=='__main__':main()
