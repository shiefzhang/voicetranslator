"""VoiceTranslator .vtmodel packages (ZIP_STORED + SHA256 manifest).
Usage: python scripts/model_pack.py build MODEL_DIR MANIFEST_JSON OUTPUT.vtmodel
       python scripts/model_pack.py verify OUTPUT.vtmodel
       python scripts/model_pack.py repack OLD.vtmodel NEW.vtmodel
"""
import argparse
import contextlib
import hashlib
import json
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from fetch import sha256

LANGUAGES = ['zh', 'ja', 'ko', 'en', 'yue']

# 每类包允许的引擎，以及必须声明的文件。
#
# `opus` 是新增的一类：OPUS-MT 是「一个语向一个模型」，所以它**不是单模型包**，
# 而是一个装着多套 encoder/decoder 的**语向矩阵**，由 `routes.txt` 描述接线，
# 官方缺的语向（如 ja→zh）由多跳 pivot 表达。见 scripts/export-opus-package.py。
ENGINES = {'asr': {'sherpa-sensevoice'},
           'translation': {'llama-qwen2', 'llama-gemma3'},
           'opus': {'opus-mt-marian'}}
REQUIRED_FILES = {'asr': ['model.int8.onnx', 'tokens.txt', 'silero_vad.onnx'],
                  'translation': ['model.gguf'],
                  'opus': ['routes.txt', 'norm/nfkc.bin']}
# opus 包每个语向目录里必须齐这几样
OPUS_ROUTE_FILES = ['encoder_model_int8.onnx', 'decoder_model_merged_int8.onnx',
                    'source.vocab.txt', 'decode.pieces.txt', 'encode.i32', 'route.json']

MAX_BYTES = 3 * 1024**3

# ------------------------------------------------------------------ ZIP64
#
# 2 GiB（0x80000000）是**中央目录偏移的 32 位有符号悬崖**。
#
# `.vtmodel` 是 STORED（无压缩）的，包多大模型就多大，所以 TranslateGemma 4B
# 这一档必然越过它。越过去之后 EOCD 里的 `cdOffset` 会被按有符号 int32 读成负数，
# Android 的 `java.util.zip.ZipFile` 就直接报 `invalid CEN header (bad signature)` ——
# 报错点离真正的原因（少了 ZIP64 记录）非常远，所以这里必须**无条件**写 ZIP64，
# 而不是等超过 4 GiB 的常规阈值才写。
#
# 判据是**包尾存在 ZIP64 EOCD 定位器 `PK\\x06\\x07`**，不是文件大小。
# `verify()` 会断言它，`repack` 用来把早于本修复的旧包重打成 ZIP64。
ZIP64_LOCATOR = b'PK\x06\x07'
ZIP64_EOCD = b'PK\x06\x06'
EOCD = b'PK\x05\x06'
EOCD_MIN = 22


@contextlib.contextmanager
def _force_zip64():
    """让 zipfile 对每个条目都写 ZIP64 扩展字段，且 EOCD 走 8 字节偏移。

    `ZIP64_LIMIT` 是模块级全局量，没有按 ZeiFile 传的开关，所以只能改了再还原 ——
    必须放在 try/finally 里，否则一次异常就会把进程里所有后续 zip 写入都带上这个副作用。
    """
    old = zipfile.ZIP64_LIMIT
    zipfile.ZIP64_LIMIT = 0
    try:
        yield
    finally:
        zipfile.ZIP64_LIMIT = old


def zip64_info(path):
    """返回 (locator_offset, eocd_offset)；缺 ZIP64 时 locator 为 -1。

    只读包尾 64 KiB —— 对 2 GB 的包也是常数开销。
    """
    size = Path(path).stat().st_size
    with open(path, 'rb') as f:
        tail_len = min(size, 65536 + EOCD_MIN)
        f.seek(size - tail_len)
        tail = f.read()
    base = size - len(tail)
    eocd = tail.rfind(EOCD)
    locator = tail.rfind(ZIP64_LOCATOR)
    return (base + locator if locator >= 0 else -1,
            base + eocd if eocd >= 0 else -1)


def require_zip64(path):
    """断言包带 ZIP64 记录。缺了就抛 —— 别等 Android 报 invalid CEN header。"""
    locator, eocd = zip64_info(path)
    if locator < 0:
        raise ValueError(
            f'{path}: 缺少 ZIP64 记录（包尾没有 {ZIP64_LOCATOR!r}）。'
            '这个包在中央目录偏移越过 2 GiB 后会被 Android 拒绝；'
            f'用 `python scripts/model_pack.py repack {path} <新路径>` 重打一遍。')
    if eocd < 0:
        raise ValueError(f'{path}: 找不到 EOCD')
    return locator

def safe_name(name):
    return bool(name) and '\\' not in name and ':' not in name and not name.startswith('/') and all(x not in ('', '.', '..') for x in name.split('/'))

def validate_manifest(m):
    if m.get('schemaVersion') != 1 or m.get('kind') not in ENGINES:
        raise ValueError('Unsupported schema/kind')
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', m.get('id', '')):
        raise ValueError('Invalid package id')

    kind = m['kind']
    languages = m.get('languages', [])
    if kind == 'opus':
        # OPUS 包只覆盖它能翻的语向，所以**允许是五语的子集** ——
        # 「必须恰好五语」这条只对 asr / translation 成立。
        if not languages or not set(languages) <= set(LANGUAGES):
            raise ValueError('OPUS package languages must be a non-empty subset of '
                             + ', '.join(LANGUAGES))
    elif sorted(languages) != sorted(LANGUAGES):
        raise ValueError('Package must support exactly ' + ', '.join(LANGUAGES))

    if m.get('engine') not in ENGINES[kind]:
        raise ValueError('Unsupported engine')
    files = m.get('files', [])
    names = [x['path'] for x in files]
    if len(names) != len(set(names)) or not all(safe_name(n) for n in names):
        raise ValueError('Unsafe/duplicate file path')
    if not set(REQUIRED_FILES[kind]).issubset(names):
        raise ValueError('Missing model files')
    if kind == 'opus':
        hops = {n.split('/')[1] for n in names if n.startswith('routes/') and n.count('/') >= 2}
        if not hops:
            raise ValueError('OPUS package declares no route directory')
        declared = m.get('routes')
        if not isinstance(declared, list) or not declared or not set(declared) <= hops:
            raise ValueError('OPUS package routes do not match its directories')
        for hop in hops:
            for name in OPUS_ROUTE_FILES:
                if 'routes/%s/%s' % (hop, name) not in names:
                    raise ValueError('Route %s is missing %s' % (hop, name))
    if not any(n.startswith('licenses/') for n in names):
        raise ValueError('License files required')
    if sum(x['size'] for x in files) > MAX_BYTES:
        raise ValueError('Package too large')
    for x in files:
        if x['size'] < 0 or not re.fullmatch('[a-f0-9]{64}', x['sha256']):
            raise ValueError('Invalid integrity record')

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
    # Android ZipFile has a signed 32-bit central-directory offset bug.
    # Emit ZIP64 metadata even below the 4 GiB threshold so archives whose
    # central directory crosses 2 GiB remain importable on Android.
    # （理由与判据见文件顶部的 ZIP64 说明。)
    with _force_zip64():
        with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
            z.writestr('manifest.json',json.dumps(m,ensure_ascii=False,indent=2).encode())
            for f in m['files']: z.write(root/f['path'],f['path'])
    # 写完立刻断言 —— 这里是「以后永远不会再出 >2 GiB 解压报错」的唯一保证点。
    # 只靠 verify() 查不出来：小包的 ZIP64 缺失在 2 GiB 以下完全无症状。
    require_zip64(temp)
    try:
        verify(temp)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    temp.replace(target)
    print(f'Created {target}: {target.stat().st_size:,} bytes, ZIP64=yes')

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
    # ZIP64 放在**内容校验之后**断言是有意的：坏包应该先报它真正的毛病
    # （篡改 / 越界路径），而不是被一个「缺 ZIP64」盖过去。反过来，
    # 内容完好却缺 ZIP64 的包必须在这里被拦住 —— 否则它会一路走到 Android，
    # 报一个 `invalid CEN header (bad signature)` 那种离原因很远的错。
    require_zip64(path)
    print(f'Verified {path}: {m["kind"]}, {m["languages"]}, ZIP64=yes')
    return m


def repack(source, target, manifest_path=None):
    """把旧包重新打成 ZIP64，**文件内容与 SHA-256 一个字节都不变**。

    动机只有一个：2026-09-30「无条件写 ZIP64」之前产出的包。
    实测 `models/translategemma-4b-text-q3_k_m.vtmodel` 的中央目录偏移是
    2,098,494,372，距 2^31 只剩 49 MB —— 它当下不报错纯属余量没用完。
    重打比重新跑 `prepare_translategemma.py` 便宜得多（不需要 HF 权重、
    不需要 gated 授权，也不动 2 GB 的 gguf）。

    `manifest_path` 用来**重新声明元数据**，内容（files 的 size/sha256）仍按实际
    条目现算。需要它的原因：重打只解决**容器**问题，而旧包往往同时带着**语义**问题 ——
    例如同一个 Gemma 包里的清单是 v0.2.4 时代的四语声明，两端 App 会直接拒收。
    这时 `--manifest model-manifests/<模板>.json` 一次把两件事都办掉。"""
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ValueError('repack 的源和目标不能是同一个文件；请先写到临时路径再替换')
    with zipfile.ZipFile(source) as z:
        names = z.namelist()
        if 'manifest.json' not in names:
            raise ValueError(f'{source}: 缺少 manifest.json')
        src_raw = z.read('manifest.json')
        src_manifest = json.loads(src_raw)
        expected = {f['path']: f for f in src_manifest.get('files', [])}
        with tempfile.TemporaryDirectory(prefix='vt-repack-') as tmp:
            root = Path(tmp) / 'root'
            root.mkdir()
            for name in names:
                if name == 'manifest.json':
                    continue
                spec = expected.get(name)
                if spec is None:
                    raise ValueError(f'{source}: 条目 {name} 未在清单里声明')
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                written = 0
                with z.open(name) as src, open(dest, 'wb') as out:
                    for chunk in iter(lambda: src.read(4 * 1024 * 1024), b''):
                        written += len(chunk)
                        digest.update(chunk)
                        out.write(chunk)
                if written != spec['size'] or digest.hexdigest() != spec['sha256']:
                    raise ValueError(f'{source}: 条目 {name} 与清单不符，拒绝重打')
            meta = Path(tmp) / 'meta.json'
            meta.write_bytes(Path(manifest_path).read_bytes() if manifest_path else src_raw)
            try:
                build(root, meta, target)
            except ValueError as exc:
                raise ValueError(
                    f'{source}: 重打前的清单校验没通过 —— {exc}\n'
                    '  注意这是**语义**问题，重打本身只换 ZIP64 容器、不改内容。\n'
                    '  包里的清单多半是旧契约（例如 v0.2.4 时代的四语声明），'
                    '这种包两端 App 会直接拒收。用当前模板重声明元数据再重打：\n'
                    f'    python scripts/model_pack.py repack {source} {target} '
                    '--manifest model-manifests/<对应模板>.json') from exc
    return target

if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('build');b.add_argument('root');b.add_argument('metadata');b.add_argument('output')
    v=sub.add_parser('verify');v.add_argument('package')
    r=sub.add_parser('repack',help='把旧包重打成 ZIP64（内容与 SHA-256 不变）')
    r.add_argument('source');r.add_argument('target')
    r.add_argument('--manifest',help='用这个模板重新声明元数据（files 仍按实际条目现算）')
    a=p.parse_args()
    if a.command=='build':build(a.root,a.metadata,a.output)
    elif a.command=='repack':repack(a.source,a.target,a.manifest)
    else:verify(a.package)
