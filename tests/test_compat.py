import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PING = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}) + "\n"


def run(script, env):
    r = subprocess.run([sys.executable, str(ROOT / script)], input=PING, capture_output=True, text=True,
                       env={**os.environ, **env}, timeout=20)
    return json.loads(r.stdout.splitlines()[0])["result"]


class CompatTest(unittest.TestCase):
    def test_new_and_old_entrypoints(self):
        # новый путь и старый (olx_mcp/server.py из прежних настроек Claude) — один и тот же сервер
        for script in ("uzparser/server.py", "olx_mcp/server.py"):
            self.assertEqual(run(script, {})["serverInfo"]["name"], "uzparser")

    def test_old_env_names_still_work(self):
        from uzparser import server as S
        sent = []
        for env in ({"OLX_MCP_TOKEN": "t-old", "OLX_MCP_URL": "https://old.test"},
                    {"UZPARSER_TOKEN": "t-new", "UZPARSER_URL": "https://new.test", "OLX_MCP_TOKEN": "t-old"}):
            saved = {k: os.environ.pop(k, None) for k in ("UZPARSER_TOKEN", "UZPARSER_URL", "OLX_MCP_TOKEN", "OLX_MCP_URL")}
            os.environ.update(env)
            try:
                captured = {}
                orig = S.Server.__init__

                def spy(self, api, send, sleep=None):
                    captured["api"] = api
                    orig(self, api, send)
                S.Server.__init__ = spy
                old_stdin, sys.stdin = sys.stdin, open(os.devnull)
                try:
                    S.main()
                finally:
                    sys.stdin, S.Server.__init__ = old_stdin, orig
                api = captured["api"]
                expect = ("t-new", "https://new.test") if "UZPARSER_TOKEN" in env else ("t-old", "https://old.test")
                self.assertEqual((api.token, api.base), expect)
            finally:
                for k in ("UZPARSER_TOKEN", "UZPARSER_URL", "OLX_MCP_TOKEN", "OLX_MCP_URL"):
                    os.environ.pop(k, None)
                os.environ.update({k: v for k, v in saved.items() if v is not None})

    def test_data_dir_migrates_from_old_name(self):
        from uzparser import avtoelon as A
        with tempfile.TemporaryDirectory() as home:
            old = Path(home) / ".olx-mcp" / "avtoelon"
            old.mkdir(parents=True)
            (old / "avtoelon_x-20261005-000000.json").write_text("{}")
            env = {k: os.environ.pop(k, None) for k in ("UZPARSER_DATA", "OLX_MCP_DATA", "HOME")}
            os.environ["HOME"] = home
            try:
                d = A.data_dir()
            finally:
                os.environ.pop("HOME")
                os.environ.update({k: v for k, v in env.items() if v is not None})
            self.assertEqual(d, Path(home) / ".uzparser" / "avtoelon")
            self.assertTrue((d / "avtoelon_x-20261005-000000.json").exists())
            self.assertFalse((Path(home) / ".olx-mcp").exists())


if __name__ == "__main__":
    unittest.main()
