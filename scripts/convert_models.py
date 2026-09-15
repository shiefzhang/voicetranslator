"""Optional conversion helpers.
Qwen: HF directory -> F16 GGUF -> Q4_K_M. TranslateGemma: official HF text
weights -> text-only F16 GGUF -> Q3_K_M and Q4_K_M (no mmproj). Both require
llama.cpp Python requirements and a host-built llama-quantize executable. SenseVoice: quantizes compatible
sherpa-exported FP32 ONNX only, never arbitrary ONNX graphs.
"""
import argparse,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='kind',required=True)
    q=s.add_parser('qwen');q.add_argument('--hf-dir',type=Path,required=True);q.add_argument('--quantize-bin',type=Path,required=True);q.add_argument('--output-dir',type=Path,required=True)
    g=s.add_parser('translategemma')
    g.add_argument('--hf-dir',type=Path,required=True)
    g.add_argument('--quantize-bin',type=Path,required=True)
    g.add_argument('--output-root',type=Path,required=True)
    a=s.add_parser('sensevoice-int8');a.add_argument('--fp32',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.kind=='qwen':
        if not args.quantize_bin.is_file():raise FileNotFoundError('Build llama-quantize for this host first')
        config=json.loads((args.hf_dir/'config.json').read_text())
        if config.get('model_type')!='qwen2':raise ValueError('This profile supports Qwen2/Qwen2.5 only')
        args.output_dir.mkdir(parents=True,exist_ok=True)
        fp16=args.output_dir/'model-f16.gguf';quant=args.output_dir/'model.gguf'
        if fp16.exists() or quant.exists():raise FileExistsError('Choose an empty output directory')
        subprocess.run([sys.executable,str(ROOT/'third_party/llama.cpp/convert_hf_to_gguf.py'),str(args.hf_dir),'--outfile',str(fp16),'--outtype','f16'],check=True)
        subprocess.run([str(args.quantize_bin.resolve()),str(fp16),str(quant),'Q4_K_M'],check=True)
    elif args.kind=='translategemma':
        if not args.quantize_bin.is_file():raise FileNotFoundError('Build llama-quantize for this host first')
        config=json.loads((args.hf_dir/'config.json').read_text(encoding='utf-8'))
        architectures=config.get('architectures',[])
        if config.get('model_type')!='gemma3' and not any('Gemma3' in x for x in architectures):
            raise ValueError('Expected an official TranslateGemma/Gemma 3 checkpoint')
        if 'translategemma' not in str(args.hf_dir).lower():
            raise ValueError('HF directory name must identify TranslateGemma')
        args.output_root.mkdir(parents=True,exist_ok=True)
        fp16=args.output_root/'translategemma-4b-text-f16.gguf'
        if fp16.exists():raise FileExistsError(fp16)
        # Omitting --mmproj is intentional: convert_hf_to_gguf defaults to the
        # language model only and does not emit the SigLIP vision projector.
        subprocess.run([sys.executable,str(ROOT/'third_party/llama.cpp/convert_hf_to_gguf.py'),str(args.hf_dir),'--outfile',str(fp16),'--outtype','f16'],check=True)
        for quant_name,folder in [('Q3_K_M','q3_k_m'),('Q4_K_M','q4_k_m')]:
            dest=args.output_root/folder
            dest.mkdir(exist_ok=False)
            subprocess.run([str(args.quantize_bin.resolve()),str(fp16),str(dest/'model.gguf'),quant_name],check=True)
        if any(args.output_root.glob('mmproj-*.gguf')):
            raise RuntimeError('Unexpected multimodal projector output')
    else:
        import onnx
        from onnxruntime.quantization import quantize_dynamic,QuantType
        if args.output.exists():raise FileExistsError(args.output)
        model=onnx.load(str(args.fp32));meta={x.key:x.value for x in model.metadata_props}
        if meta.get('model_type') not in ('sense_voice','sense_voice_ctc') or not {'neg_mean','inv_stddev'}.issubset(meta):
            raise ValueError('Use a sherpa SenseVoice export with frontend metadata first')
        onnx.checker.check_model(model)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        quantize_dynamic(str(args.fp32),str(args.output),op_types_to_quantize=['MatMul'],weight_type=QuantType.QInt8)
        onnx.checker.check_model(onnx.load(str(args.output)))
    print('Converted. Run inference validation before model_pack.py build.')
if __name__=='__main__':main()
