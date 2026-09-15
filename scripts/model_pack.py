"""VoiceTranslator .vtmodel packages (ZIP_STORED + SHA256 manifest).
Usage: python scripts/model_pack.py build MODEL_DIR MANIFEST_JSON OUTPUT.vtmodel
       python scripts/model_pack.py verify OUTPUT.vtmodel
"""
import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from fetch import sha256

LANGUAGES = ['zh', 'ja', 'ko', 'en']
REQUIRED = {'asr': ['model.int8.onnx', 'tokens.txt', 'silero_vad.onnx'],
            'translation': ['model.gguf']}
MAX_BYTES = 3 * 1024**3

def safe_name(name):
    return bool(name) and '\\' not in name and ':' not in name and not name.startswith('/') and all(x not in ('', '.', '..') for x in name.split('/'))

def validate_manifest(m):
    if m.get('schemaVersion') != 1 or m.get('kind') not in REQUIRED:
        raise ValueError('Unsupported schema/kind')
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', m.get('id', '')):
        raise ValueError('Invalid package id')
    if sorted(m.get('languages', [])) != sorted(LANGUAGES):
        raise ValueError('Package must support exactly zh, ja, ko, en')
    engines = {'asr': {'sherpa-sensevoice'},
               'translation': {'llama-qwen2', 'llama-gemma3'}}
    if m.get('engine') not in engines[m['kind']]: raise ValueError('Unsupported engine')
    files = m.get('files', [])
    names = [x['path'] for x in files]
    if len(names)!=len(set(names)) or not all(safe_name(n) for n in names):
        raise ValueError('Unsafe/duplicate file path')
    if not set(REQUIRED[m['kind']]).issubset(names): raise ValueError('Missing model files')
    if not any(n.startswith('licenses/') for n in names): raise ValueError('License files required')
    if sum(x['size'] for x in files)>MAX_BYTES: raise ValueError('Package too large')
    for x in files:
        if x['size']<0 or not re.fullmatch('[a-f0-9]{64}',x['sha256']): raise ValueError('Invalid integrity record')

def build(root, metadata, target):
    root, target = Path(root).resolve(), Path(target)
    m=json.loads(Path(metadata).read_text(encoding='utf-8'))
    m['files']=[]
    for p in sorted(root.rglob('*')):
        if p.is_symlink(): raise ValueError('Symlinks prohibited')
        if p.is_file():
            name=p.relative_to(root).as_posix()
            if name=='manifest.json': raise ValueError('Reserved filename')
            m['files'].append({'path':name,'size':p.stat().st_size,'sha256':sha256(p)})
    validate_manifest(m)
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix('.partial')
    with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
        z.writestr('manifest.json',json.dumps(m,ensure_ascii=False,indent=2).encode())
        for f in m['files']: z.write(root/f['path'],f['path'])
    verify(temp)
    temp.replace(target)
    target.with_suffix(target.suffix+'.sha256').write_text(sha256(target)+'  '+target.name+'\n')
    print(f'Created {target}: {target.stat().st_size:,} bytes')

def verify(path):
    with zipfile.ZipFile(path) as z:
        entries=z.infolist()
        if len(entries)>128 or len({x.filename for x in entries})!=len(entries): raise ValueError('Duplicate/too many entries')
        if any(not safe_name(x.filename) for x in entries): raise ValueError('Unsafe path')
        if z.getinfo('manifest.json').file_size>131072: raise ValueError('Manifest too large')
        m=json.loads(z.read('manifest.json'));validate_manifest(m)
        if set(z.namelist())!={'manifest.json',*(f['path'] for f in m['files'])}: raise ValueError('Undeclared file')
        for f in m['files']:
            info=z.getinfo(f['path'])
            if info.file_size!=f['size']:raise ValueError('Size mismatch')
            h=hashlib.sha256()
            with z.open(info) as src:
                for b in iter(lambda:src.read(4*1024*1024),b''): h.update(b)
            if h.hexdigest()!=f['sha256']:raise ValueError('Checksum mismatch: '+f['path'])
    print(f'Verified {path}: {m["kind"]}, {m["languages"]}')
    return m

if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('build');b.add_argument('root');b.add_argument('metadata');b.add_argument('output')
    v=sub.add_parser('verify');v.add_argument('package')
    a=p.parse_args()
    if a.command=='build':build(a.root,a.metadata,a.output)
    else:verify(a.package)
