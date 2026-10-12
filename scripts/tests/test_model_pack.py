import json,sys,tempfile,unittest,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from model_pack import build,verify,safe_name,repack,zip64_info,OPUS_ROUTE_FILES

class Packages(unittest.TestCase):
    def test_paths(self):
        for p in ['../bad','/bad','x/../bad','C:/bad','x\\bad','a//b','a/./b']:self.assertFalse(safe_name(p))
        self.assertTrue(safe_name('licenses/LICENSE.txt'))
    def test_roundtrip_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'model';root.mkdir();(root/'licenses').mkdir()
            (root/'model.gguf').write_bytes(b'GGUF-test-fixture')
            (root/'licenses/LICENSE').write_text('test license')
            meta=Path(d)/'meta.json';meta.write_text(json.dumps(dict(schemaVersion=1,id='test',name='fixture',kind='translation',engine='llama-qwen2',languages=['zh','ja','ko','en','yue'])))
            target=Path(d)/'ok.vtmodel';build(root,meta,target);verify(target)
            bad=Path(d)/'bad.vtmodel'
            with zipfile.ZipFile(target) as original,zipfile.ZipFile(bad,'w') as z:
                for info in original.infolist():
                    data=original.read(info)
                    if info.filename=='model.gguf':data=b'X'+data[1:]
                    z.writestr(info.filename,data)
            with self.assertRaisesRegex(ValueError,'Checksum'):verify(bad)
    def test_traversal_archive(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad.zip'
            with zipfile.ZipFile(p,'w') as z:z.writestr('../escape','x')
            with self.assertRaisesRegex(ValueError,'Unsafe'):verify(p)
    def test_language_contract_is_exactly_five(self):
        # v0.2.4 起契约是「恰好」zh/ja/ko/en/yue 五语，与 Android ModelPackages.java、
        # iOS ModelPackages.swift 的运行时校验逐条对齐。
        # 少一个、多一个、或回退成旧的四语都必须被拒 —— 踩过的坑是：
        # 模型包多声明了 yue 而两端 App 还停在四语，结果包被整体拒收。
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'model';root.mkdir();(root/'licenses').mkdir()
            (root/'model.gguf').write_bytes(b'GGUF')
            (root/'licenses/LICENSE').write_text('L')
            meta=Path(d)/'m.json'
            def attempt(langs):
                meta.write_text(json.dumps(dict(schemaVersion=1,id='t',name='t',kind='translation',engine='llama-qwen2',languages=langs)))
                return Path(d)/'out.vtmodel'
            for langs in (['zh','ja','ko','en'],['zh','ja','ko','en','yue','fr'],[],['zh','ja','ko','en','yue']):
                target=attempt(langs)
                if langs==['zh','ja','ko','en','yue']:
                    build(root,meta,target);verify(target)
                else:
                    with self.assertRaises(ValueError):build(root,meta,target)
            # 顺序不同但集合相同 → 视为通过（比较用 sorted）。
            target=attempt(['yue','en','ko','ja','zh']);build(root,meta,target);verify(target)


class OpusPackages(unittest.TestCase):
    """`kind=opus` 的包契约。

    opus 包与 asr/translation 有两条**不同的**规则，测试要盯着它们：
      1. languages 允许是五语的**子集** —— OPUS 是「一个语向一个模型」，
         默认包只覆盖 zh/en/ja，不可能凑齐五语；
      2. 清单里必须多一个 `routes` 字段，且每个跳数目录要齐 6 个文件。
    同时五语契约对 opus **也必须**成立（不能冒出 fr/de/ru）。
    """

    @staticmethod
    def _fixture(root,*,routedirs=('zh-en',),declared=('zh-en',),languages=('zh','en'),
                 with_routes_txt=True,omit_file=''):
        """按参数搭一个 opus 组装目录。缺什么由参数控制，便于逐个断言拒绝理由。"""
        root.mkdir(parents=True)
        (root/'licenses').mkdir();(root/'licenses/LICENSE').write_text('L')
        (root/'norm').mkdir();(root/'norm/nfkc.bin').write_bytes(b'VTNF')
        if with_routes_txt:(root/'routes.txt').write_text('\n'.join(f'{h}\t{h}' for h in routedirs)+'\n')
        for hop in routedirs:
            d=root/'routes'/hop;d.mkdir(parents=True)
            for name in OPUS_ROUTE_FILES:
                if name!=omit_file:(d/name).write_bytes(b'x')
        meta=dict(schemaVersion=1,id='opus-test',name='opus fixture',kind='opus',
                  engine='opus-mt-marian',languages=list(languages),routes=list(declared))
        return meta

    def _run(self,d,root,**kw):
        meta=self._fixture(root,**kw)
        meta_path=Path(d)/'meta.json';meta_path.write_text(json.dumps(meta))
        target=Path(d)/'out.vtmodel';build(root,meta_path,target);verify(target)
        return target

    def test_subset_languages_are_accepted(self):
        # 这一条正是 opus 相对 asr/translation 的差异所在：三语子集必须过。
        with tempfile.TemporaryDirectory() as d:
            self._run(d,Path(d)/'m',languages=('zh','ja','en'),routedirs=('zh-en','en-zh','zh-ja'),
                      declared=('zh-en','en-zh','zh-ja'))

    def test_two_hop_pivot(self):
        # ja→zh 官方没有直接模型，是 ja-en → en-zh 两跳；包结构必须容得下两个目录。
        with tempfile.TemporaryDirectory() as d:
            self._run(d,Path(d)/'m',routedirs=('ja-en','en-zh'),declared=('ja-en','en-zh'),
                      languages=('zh','ja','en'))

    def test_language_outside_contract_is_rejected(self):
        # 五语契约对 opus 同样成立 —— 子集允许，越界不允许。
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                self._run(d,Path(d)/'m',languages=('zh','en','fr'))

    def test_empty_languages_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                self._run(d,Path(d)/'m',languages=())

    def test_missing_route_files_rejected(self):
        # 少一个 encode.i32 就整包拒收：端侧加载是「按文件名硬取」，
        # 缺文件不会在打包期报错，只会在真机上变成整句 <unk>。
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                self._run(d,Path(d)/'m',omit_file='encode.i32')

    def test_declared_route_must_have_directory(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                self._run(d,Path(d)/'m',routedirs=('zh-en',),declared=('zh-en','en-zh'))

    def test_empty_routes_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                self._run(d,Path(d)/'m',routedirs=(),declared=(),languages=('zh',))

    def test_missing_routes_txt_rejected(self):
        # routes.txt 是端侧真正的语向数据源（C++ 直接从包根读它），必须声明。
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                self._run(d,Path(d)/'m',with_routes_txt=False)


class ModelTierCatalog(unittest.TestCase):
    """`model-tiers.py` 的推荐档位必须**显式钉住**，不能被「体积更小」顶掉。

    ⚠️ 两端**不再内置任何模型包**，所以这个脚本已经不写盘了，只剩「列档位 + 体检」。
    但「推荐哪一档」这件事仍然必须钉死，因为**别的输出都读它**（导入清单、
    给用户的说明、发布说明）。

    踩过的坑：脚本原本的策略是「每个 kind 取体积最小的包」，而当时钉的 translation
    是 **Q4_K_M**（A8 的 armv8-a 基线口径下速度第一、质量优于 Q4_0）。可 models/ 里
    **Q3 更小**，于是任何一次重跑都会把推荐静默从 Q4_K_M 降到 Q3 ——
    而输出是「成功、体积还变小了」，看日志完全发现不了。

    所以盯着两件事：推荐表不被体积影响；推荐包缺席时必须**明说**，
    绝不能静默顶一个上去。另外体检本身的每条判据也各有一条测试。
    """

    @staticmethod
    def _module():
        """按路径加载带连字符的脚本名（`model-tiers.py` 不是合法模块名）。"""
        import importlib.util
        path = Path(__file__).resolve().parents[1] / 'model-tiers.py'
        spec = importlib.util.spec_from_file_location('model_tiers', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _candidate(module, kind, ident, mebibytes, first_entry=None):
        manifest = {"kind": kind, "id": ident, "name": ident, "engine": "test-engine",
                    "files": [{"path": "model.gguf", "size": mebibytes * 1048576},
                              {"path": "licenses/L.txt", "size": 1}]}
        return module.Candidate(Path(ident + ".vtmodel"), manifest,
                                first_entry if first_entry is not None else module.MANIFEST)

    def _run(self, module, found, argv=(), health=None):
        """跑一次 main()，返回 (exit_code, stdout, stderr)。

        `main()` 判定不通过时走 `die()` → `SystemExit`，这里要接住并取它的 code，
        否则「退出码」这件事就没法断言了。
        """
        import contextlib, io
        from unittest import mock
        out, err = io.StringIO(), io.StringIO()
        patch_health = (mock.patch.object(module, 'health', health) if health
                        else mock.patch.object(module, 'health', lambda c: []))
        with mock.patch.object(module, 'scan', lambda: found), patch_health, \
                mock.patch.object(sys, 'argv', ['model-tiers.py', *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = module.main()
            except SystemExit as exit_signal:
                code = int(exit_signal.code or 0)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def _marked_ids(output):
        """解析出被标成 `→ 推荐` 的那些 id（每个候选打两行，下一行才是 `id=…`）。"""
        lines = output.splitlines()
        return [lines[i + 1].split('id=')[1].split()[0]
                for i, line in enumerate(lines) if '→ 推荐' in line]

    def test_recommended_id_is_pinned_not_smallest(self):
        module = self._module()
        found = {'translation': [
            self._candidate(module, 'translation', 'qwen25-1.5b-q3-zh-ja-ko-en-yue-v1', 882),
            self._candidate(module, 'translation', 'qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1', 1066),
        ]}
        code, output, errors = self._run(module, found)
        self.assertEqual(code, 0)
        self.assertEqual(self._marked_ids(output), ['qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1'],
                         '钉住的 Q4_K_M 必须胜过体积更小的 Q3')
        self.assertNotIn('不在 models/ 下', errors)

    def test_recommended_table_is_a_literal_pin(self):
        """推荐表必须能把 id 原样写出来 —— 不能是算出来的。

        这条是防「有人把它改回按体积排序」的：一旦是算出来的，
        下面这三个字面量就会对不上。
        """
        module = self._module()
        self.assertEqual(module.RECOMMENDED, {
            'asr': 'sensevoice-four-language-int8-v1',
            'translation': 'qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1',
            'opus': 'opus-mt-zh-en-ja-v1',
        })
        self.assertEqual(set(module.RECOMMENDED) - set(module.KINDS), set(),
                         '推荐表里出现了 KINDS 之外的 kind')

    def test_no_implicit_pick_when_nothing_is_recommended(self):
        """推荐表里没有这个 kind 时，**不许**替它挑一个。

        静默挑一个正是「推荐漂档」那个坑的形态：输出照样「成功」。
        """
        module = self._module()
        found = {'translation': [
            self._candidate(module, 'translation', 'whatever-v1', 100)]}
        import contextlib, io
        from unittest import mock
        out = io.StringIO()
        with mock.patch.object(module, 'RECOMMENDED', {}), \
                mock.patch.object(module, 'health', lambda c: []), \
                mock.patch.object(module, 'scan', lambda: found), \
                mock.patch.object(sys, 'argv', ['model-tiers.py']), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(module.main(), 0)
        self.assertEqual(self._marked_ids(out.getvalue()), [],
                         '表里没有就说没有，不能自己挑一个当推荐')

    def test_missing_recommended_is_said_out_loud(self):
        """推荐包不在 models/ 下时必须写进 stderr，且不能假装选中了它。"""
        module = self._module()
        found = {'translation': [
            self._candidate(module, 'translation', 'some-other-q3-v1', 882)]}
        code, output, errors = self._run(module, found)
        self.assertEqual(code, 0)
        self.assertIn('不在 models/ 下', errors)
        self.assertIn('qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1', errors)  # 明说是哪个
        self.assertEqual(self._marked_ids(output), [])

    def test_health_flags_manifest_not_first(self):
        """清单不在第一个 entry —— Android 的顺序读器会报「缺少有效 manifest.json」。"""
        module = self._module()
        bad = self._candidate(module, 'translation', 'x-v1', 10, first_entry='model.gguf')
        with tempfile.NamedTemporaryFile(suffix='.vtmodel') as f:
            bad.path = Path(f.name)
            f.write(b'PK\x06\x07' + b'\0' * 64)
            f.flush()
            problems = module.health(bad)
        self.assertTrue(any('第一个 zip entry' in p for p in problems), problems)

    def test_health_flags_missing_zip64(self):
        """没有 ZIP64 记录要报出来；有则不报。"""
        module = self._module()
        candidate = self._candidate(module, 'translation', 'x-v1', 10)
        with tempfile.TemporaryDirectory() as d:
            plain = Path(d) / 'plain.vtmodel'
            plain.write_bytes(b'PK\x03\x04' + b'\0' * 128)      # 没有 PK\x06\x07
            candidate.path = plain
            self.assertTrue(any('ZIP64' in p for p in module.health(candidate)),
                            module.health(candidate))
            with_locator = Path(d) / 'z64.vtmodel'
            with_locator.write_bytes(b'PK\x06\x07' + b'\0' * 128)
            candidate.path = with_locator
            self.assertEqual([p for p in module.health(candidate) if 'ZIP64' in p], [])

    def test_health_flags_missing_license_file(self):
        module = self._module()
        candidate = self._candidate(module, 'translation', 'x-v1', 10)
        candidate.manifest['files'] = [{"path": "model.gguf", "size": 10}]
        with tempfile.NamedTemporaryFile(suffix='.vtmodel') as f:
            candidate.path = Path(f.name)
            f.write(b'PK\x06\x07' + b'\0' * 64)
            f.flush()
            problems = module.health(candidate)
        self.assertTrue(any('licenses/' in p for p in problems), problems)

    def test_health_never_raises_on_absent_file(self):
        """候选指向的文件不在磁盘上时，体检要给出人话，不能抛 FileNotFoundError。

        回归来源：`zip64_info()` 内部会 `stat`，把「包不在磁盘上」伪装成脚本崩溃。
        这是从上一版 bundle-models.py 学来的 —— 那道闸踩过一次，这里不能再踩。
        """
        module = self._module()
        ghost = self._candidate(module, 'translation', 'ghost-v1', 882)
        problems = module.health(ghost)          # 不抛就算过
        self.assertTrue(any('不在磁盘上' in p for p in problems), problems)

    def test_recommended_package_that_fails_health_exits_1(self):
        module = self._module()
        found = {'translation': [
            self._candidate(module, 'translation', 'qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1', 1066)]}
        code, _output, errors = self._run(
            module, found, health=lambda c: ['伪造的问题'])
        self.assertEqual(code, 1)
        # 断言用**稳定子串**：v0.2.10 起这条消息从「推荐档位…」扩成「推荐/内置档位…」
        # （内置包体检不过同样是硬失败），锁整句会在这种有意扩展时假红。
        self.assertIn('档位体检不通过', errors)
        self.assertIn('qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1', errors,
                      '必须把致错的档位 id 打出来 —— 只说「有一档坏了」等于没说')

    def test_non_recommended_package_only_fails_under_strict(self):
        """非推荐包坏掉只是提示 —— 用户仍可能导入它，但不是发布阻断项。"""
        module = self._module()
        found = {'translation': [
            self._candidate(module, 'translation', 'qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1', 1066),
            self._candidate(module, 'translation', 'weird-v1', 10)]}
        def health(candidate):
            return ['伪造的问题'] if candidate.id == 'weird-v1' else []
        code, _output, errors = self._run(module, found, health=health)
        self.assertEqual(code, 0)
        self.assertIn('非推荐包体检不通过', errors)
        code, _output, errors = self._run(module, found, argv=('--strict',), health=health)
        self.assertEqual(code, 1)
        self.assertIn('--strict', errors)

    def test_json_catalog_marks_required_and_recommended(self):
        module = self._module()
        found = {
            'asr': [self._candidate(module, 'asr', 'sensevoice-four-language-int8-v1', 229)],
            'translation': [
                self._candidate(module, 'translation', 'qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1', 1066),
                self._candidate(module, 'translation', 'qwen25-1.5b-q3-zh-ja-ko-en-yue-v1', 882)],
        }
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'catalog.json'
            code, _output, _errors = self._run(module, found, argv=('--json', str(target)))
            self.assertEqual(code, 0)
            catalog = json.loads(target.read_text('utf-8'))
        packages = catalog['packages']
        self.assertTrue(packages['sensevoice-four-language-int8-v1']['required'])
        self.assertTrue(packages['sensevoice-four-language-int8-v1']['recommended'])
        self.assertTrue(packages['qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1']['recommended'])
        self.assertFalse(packages['qwen25-1.5b-q3-zh-ja-ko-en-yue-v1']['recommended'],
                         'Q3 更小也不是推荐档 —— 推荐是钉的，不是算的')
        self.assertTrue(packages['qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1']['required'])


class SizeOnlyPickIsGone(unittest.TestCase):
    """「取体积最小」这条隐式策略不许以任何形式回来。

    两端不再内置模型包（版本库里有专门的守卫测试盯着源码），所以脚本也不该再
    有任何「选中 / 写盘」的动作 —— 一旦有人把 `--pick`、`--target`、`emit_*`
    加回来，就是把刚拆掉的耦合又装回去。
    """

    def test_no_write_or_pick_switches(self):
        module = ModelTierCatalog._module()
        for banned in ('emit_ios', 'emit_android', 'install_bundled', 'write_bundled'):
            self.assertFalse(hasattr(module, banned),
                             f'model-tiers.py 不该再有 {banned}()：两端不再内置模型包')

    def test_removed_switches_are_rejected(self):
        """`--pick` / `--target` / `--omit` / `--force` 必须已经不存在。

        它们都是「往 App 里塞包」那套流程的开关。保留着就等于把刚拆掉的耦合
        又留了一扇门 —— argparse 对未定义开关的退出码是 2。
        """
        import subprocess
        script = str(Path(__file__).resolve().parents[1] / 'model-tiers.py')
        for flag in ('--pick', '--target', '--omit', '--force'):
            result = subprocess.run([sys.executable, script, flag],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2,
                             f'{flag} 应该已经不存在了，却还能被接受')


class Zip64Emission(unittest.TestCase):
    """`.vtmodel` 必须**无条件**带 ZIP64。

    2 GiB（0x80000000）是中央目录偏移的 32 位有符号悬崖：越过去之后 Android 的
    `java.util.zip.ZipFile` 会把 EOCD 里的 cdOffset 读成负数，抛
    `invalid CEN header (bad signature)` —— 报错点离真正的原因很远。

    实测 TranslateGemma 4B Q3_K_M 的中央目录偏移距 2^31 只剩约 49 MB，
    所以「小包现在没事」不代表没事：真越过去时才发现就已经晚了。
    这一组盯的是「无条件」三个字 —— 它是保证 >2 GiB 不再出问题的唯一着力点。
    """

    @staticmethod
    def _fixture(d):
        root = Path(d) / 'model'
        root.mkdir()
        (root / 'licenses').mkdir()
        (root / 'model.gguf').write_bytes(b'GGUF-test-fixture')
        (root / 'licenses/LICENSE').write_text('test license')
        meta = Path(d) / 'meta.json'
        meta.write_text(json.dumps(dict(schemaVersion=1, id='zip64-fixture', name='fixture',
                                        kind='translation', engine='llama-qwen2',
                                        languages=['zh', 'ja', 'ko', 'en', 'yue'])))
        return root, meta

    @staticmethod
    def _strip_zip64(source, target):
        """用普通 zipfile 重打同样的内容 —— 模拟「无条件写 ZIP64」之前的旧包。"""
        with zipfile.ZipFile(source) as src, \
                zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED) as out:
            for info in src.infolist():
                out.writestr(info.filename, src.read(info))
        return target

    def test_build_always_emits_zip64(self):
        with tempfile.TemporaryDirectory() as d:
            root, meta = self._fixture(d)
            target = Path(d) / 'out.vtmodel'
            build(root, meta, target)
            self.assertGreaterEqual(zip64_info(target)[0], 0,
                                    'build 的产物必须带 ZIP64 定位器（与包大小无关）')
            with zipfile.ZipFile(target) as z:
                for info in z.infolist():
                    self.assertGreaterEqual(info.extract_version, 45,
                                            f'{info.filename} 的 version_needed 应 >= 45（ZIP64）')

    def test_verify_rejects_archive_without_zip64(self):
        with tempfile.TemporaryDirectory() as d:
            root, meta = self._fixture(d)
            good = Path(d) / 'good.vtmodel'
            build(root, meta, good)
            plain = self._strip_zip64(good, Path(d) / 'plain.vtmodel')
            # 夹具本身要真的是非 ZIP64，否则这条测试什么也没验到。
            self.assertEqual(zip64_info(plain)[0], -1, '夹具应当是非 ZIP64 包')
            # 内容完好、只缺 ZIP64 → 必须被拦下，且信息要指向 repack 这个修法。
            with self.assertRaisesRegex(ValueError, 'ZIP64'):
                verify(plain)
            # 而它内容确实没问题 —— 被拒的**只**是容器。
            self.assertIn('repack', str(self._captured(plain)))

    @staticmethod
    def _captured(path):
        try:
            verify(path)
        except ValueError as exc:
            return exc
        raise AssertionError('本该失败')

    def test_repack_emits_zip64_and_keeps_content(self):
        with tempfile.TemporaryDirectory() as d:
            root, meta = self._fixture(d)
            good = Path(d) / 'good.vtmodel'
            build(root, meta, good)
            plain = self._strip_zip64(good, Path(d) / 'plain.vtmodel')
            fixed = Path(d) / 'fixed.vtmodel'
            repack(plain, fixed)
            self.assertGreaterEqual(zip64_info(fixed)[0], 0, 'repack 的产物必须带 ZIP64')
            verify(fixed)
            # 容器变了，内容一个字节都不能变。
            with zipfile.ZipFile(plain) as a, zipfile.ZipFile(fixed) as b:
                self.assertEqual(json.loads(a.read('manifest.json')),
                                 json.loads(b.read('manifest.json')))
                for name in a.namelist():
                    if name == 'manifest.json':
                        continue
                    self.assertEqual(a.read(name), b.read(name), f'{name} 的内容被改动了')

    def test_repack_requires_manifest_override_for_stale_languages(self):
        """旧包的四语声明必须**显式改**，不能被 repack 悄悄放行。

        这正是 2026-09-30 在 TranslateGemma 4B 上实测到的形态：包本身完好，
        但清单是 v0.2.4 时代的 zh/ja/ko/en —— 两端 App 会直接拒收
        （「模型包语言不符合契约」）。repack 只换容器，改不了语义。
        """
        with tempfile.TemporaryDirectory() as d:
            root, meta = self._fixture(d)
            good = Path(d) / 'good.vtmodel'
            build(root, meta, good)
            stale = Path(d) / 'stale.vtmodel'
            with zipfile.ZipFile(good) as src, \
                    zipfile.ZipFile(stale, 'w', compression=zipfile.ZIP_STORED) as out:
                for info in src.infolist():
                    data = src.read(info)
                    if info.filename == 'manifest.json':
                        m = json.loads(data)
                        m['languages'] = ['zh', 'ja', 'ko', 'en']   # ← 掉成四语
                        data = json.dumps(m, ensure_ascii=False).encode()
                    out.writestr(info.filename, data)

            # ① 不带 --manifest：必须在**清单校验**这一步就拦住，而且要说清修法。
            with self.assertRaisesRegex(ValueError, 'languages|exactly') as ctx:
                repack(stale, Path(d) / 'nope.vtmodel')
            self.assertIn('--manifest', str(ctx.exception), '报错里必须给出修法')

            # ② 带当前模板：容器与语义一起修好。
            fixed = Path(d) / 'fixed.vtmodel'
            repack(stale, fixed, manifest_path=meta)
            self.assertGreaterEqual(zip64_info(fixed)[0], 0)
            verify(fixed)
            with zipfile.ZipFile(fixed) as z:
                self.assertEqual(json.loads(z.read('manifest.json'))['languages'],
                                 ['zh', 'ja', 'ko', 'en', 'yue'])


class VerifyLanguagesDefaults(unittest.TestCase):
    """`verify_model_languages.py` 的默认参数必须指向**当前**的包。

    踩过的坑：默认写的是 `dist/models/qwen25-3b-zh-ja-ko-en.vtmodel` ——
    ① `dist/` 下没有 `models/` 这个子目录，默认参数必然 FileNotFoundError；
    ② 名字是 3B 时代的，而全项目早已统一到 1.5B Q4_K_M。
    这类「默认参数过期」平时完全不报错，只在真跑的那一次才炸。
    """

    @staticmethod
    def _module(name, filename):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / filename
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_defaults_point_at_current_packages(self):
        module = self._module('verify_model_languages', 'verify_model_languages.py')
        self.assertTrue(module.DEFAULT_MODELS, '默认参数不能为空')
        models_dir = Path(__file__).resolve().parents[2] / 'models'
        for path in module.DEFAULT_MODELS:
            self.assertEqual(path.parent, models_dir,
                             f'{path}：默认参数必须落在 models/ 下（model-tiers.py 只扫那里）')
            self.assertNotIn('3b', path.name.lower(),
                             f'{path.name}：全项目已统一到 1.5B Q4_K_M，不该再有 3B')

    def test_defaults_cover_every_recommended_pick(self):
        """三个默认包要覆盖 RECOMMENDED 的三个 kind，且 id 一致。

        `models/` 是本地目录（在 .gitignore 里），干净克隆上没有这些包，
        所以只在包存在时做 id 比对 —— 但**一旦存在就必须对得上**。
        """
        verify_langs = self._module('verify_model_languages', 'verify_model_languages.py')
        tiers = ModelTierCatalog._module()
        present = [p for p in verify_langs.DEFAULT_MODELS if p.is_file()]
        if not present:
            self.skipTest('models/ 下没有包（干净克隆），只做路径与命名的静态检查')
        ids = {}
        for path in present:
            with zipfile.ZipFile(path) as z:
                manifest = json.loads(z.read('manifest.json'))
            ids[manifest['kind']] = manifest['id']
        for kind, recommended in tiers.RECOMMENDED.items():
            self.assertIn(kind, ids, f'默认参数里缺 kind={kind} 的包')
            self.assertEqual(ids[kind], recommended,
                             f'{kind} 的默认包 id 是 {ids[kind]}，而 RECOMMENDED 钉的是 {recommended}')


if __name__=='__main__':unittest.main()
