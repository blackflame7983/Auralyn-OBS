"""制限した子プロセスでもWindowsの標準フォルダーを解決できることを確認する。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from native_support import runtime_env


@unittest.skipUnless(os.name=='nt','Windows shell APIの回帰試験')
class RuntimeEnvironmentTests(unittest.TestCase):
    def test_program_data_known_folder_resolves_in_restricted_child(self):
        # 環境変数の名前をなぞるだけでなく、OBSの探索と同じShell APIを実行する。
        code=('import ctypes as c,json; b=c.create_unicode_buffer(260);'
              'hr=c.WinDLL("shell32").SHGetFolderPathW(None,35,None,0,b);'
              'print(json.dumps({"hresult":hr,"path":b.value}))')
        result=subprocess.run([sys.executable,'-c',code],env=runtime_env(),capture_output=True,timeout=15,check=True)
        value=json.loads(result.stdout)
        self.assertEqual(value['hresult'],0)
        self.assertEqual(Path(value['path']).resolve(),Path(os.environ['PROGRAMDATA']).resolve())


if __name__=='__main__':
    unittest.main()
