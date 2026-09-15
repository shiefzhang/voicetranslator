import json,sys,tempfile,unittest,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from model_pack import build,verify,safe_name

class Packages(unittest.TestCase):
    def test_paths(self):
        for p in ['../bad','/bad','x/../bad','C:/bad','x\\bad','a//b','a/./b']:self.assertFalse(safe_name(p))
        self.assertTrue(safe_name('licenses/LICENSE.txt'))
    def test_roundtrip_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'model';root.mkdir();(root/'licenses').mkdir()
            (root/'model.gguf').write_bytes(b'GGUF-test-fixture')
            (root/'licenses/LICENSE').write_text('test license')
            meta=Path(d)/'meta.json';meta.write_text(json.dumps(dict(schemaVersion=1,id='test',name='fixture',kind='translation',engine='llama-qwen2',languages=['zh','ja','ko','en'])))
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
if __name__=='__main__':unittest.main()
