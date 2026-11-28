import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

import history_db
import review_workbench


CSV_FIXTURE = (
    '"视频描述","视频ID","发布时间","完播率","平均播放时长","播放量","推荐","喜欢","评论量","分享量","关注量",'
    '"转发聊天和朋友圈","设为铃声","设为状态","设为朋友圈封面","企微链接点击次数","企微链接点击人数","添加到通讯录次数","添加到通讯录人数"\n'
    '"测试<script>alert(1)</script>描述 #科技","export/WB1","2026/11/22","31.20%","15.40秒",2100,2,11,3,4,9,4,0,0,0,,,,\n'
)


def _http_json(url: str, *, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class ReviewWorkbenchTests(unittest.TestCase):
    def test_workbench_end_to_end_api_and_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hdir = root / "history"
            rdir = root / "publish-runs"
            ddir = root / "Downloads"
            rdir.mkdir()
            ddir.mkdir()
            (ddir / "视频号动态数据明细.csv").write_text(CSV_FIXTURE, encoding="utf-8")

            server = review_workbench.create_server(
                history_dir=hdir,
                runs_dir=rdir,
                downloads_dir=ddir,
                root_dir=root,
                port=0,
            )
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{port}"
            try:
                # 1. HTML UI page serves cleanly
                with urllib.request.urlopen(base + "/", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    html = resp.read().decode("utf-8")
                    self.assertIn("视频号复盘工作台", html)

                # 2. Overview detects Downloads CSV
                code, ov = _http_json(base + "/api/overview")
                self.assertEqual(code, 200)
                self.assertIsNotNone(ov["detected_downloads_csv"])
                self.assertEqual(ov["detected_downloads_csv"]["filename"], "视频号动态数据明细.csv")

                # 3. Import CSV from Downloads (30d)
                code, imp_res = _http_json(
                    base + "/api/import-csv",
                    method="POST",
                    body={"mode": "downloads", "window_type": "30d"},
                )
                self.assertEqual(code, 200)
                self.assertEqual(imp_res["result"]["new_count"], 1)
                vid = imp_res["overview"]["index"]["videos"][0]["id"]

                # 4. Fetch & patch video (add transcript, mark, notes)
                code, video = _http_json(base + f"/api/video/{vid}")
                self.assertEqual(code, 200)
                self.assertEqual(video["latest_metrics"]["plays"], 2100)

                code, patched = _http_json(
                    base + f"/api/video/{vid}",
                    method="PATCH",
                    body={
                        "user_mark": "标杆",
                        "user_notes": "这条完播不错",
                        "topic_tags": "AI, 商业",
                        "transcript": "这是补录的完整口播文案。",
                    },
                )
                self.assertEqual(code, 200)
                self.assertEqual(patched["video"]["review"]["user_mark"], "标杆")
                self.assertEqual(patched["video"]["creative"]["opening_3s"], "这是补录的完整口播文案。")

                # 5. Playbook add & approve/reject rule
                code, pb_res = _http_json(
                    base + "/api/playbook/rule",
                    method="POST",
                    body={"category": "copywriting", "text": "开头直接给具体数字", "status": "approved"},
                )
                self.assertEqual(code, 200)
                rid = pb_res["playbook"]["rules"][0]["id"]
                code, pb_res2 = _http_json(
                    base + f"/api/playbook/rule/{rid}",
                    method="PATCH",
                    body={"status": "rejected"},
                )
                self.assertEqual(code, 200)
                self.assertEqual(pb_res2["playbook"]["rules"][0]["status"], "rejected")

                # 6. Export context excludes rejected rule and includes transcript
                code, ctx = _http_json(
                    base + "/api/export-context",
                    method="POST",
                    body={"video_ids": [vid]},
                )
                self.assertEqual(code, 200)
                self.assertEqual(ctx["count"], 1)
                self.assertIn("这是补录的完整口播文案", ctx["markdown"])
                self.assertNotIn("开头直接给具体数字", ctx["markdown"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


@unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
class CmdLauncherTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def _run_cmd(self, launcher_name: str, arguments: str, stdin: str = "\n\n") -> subprocess.CompletedProcess:
        launcher = self.ROOT / launcher_name
        cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        command = f'"{cmd}" /d /s /c "chcp 936 >nul & "{launcher}"{arguments}"'
        env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
        return subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=25,
        )

    def test_launchers_are_ascii_crlf(self):
        for name in ("视频号复盘工作台.cmd", "视频号一键发布.cmd", "视频号一键复盘.cmd", "剪口播.cmd"):
            raw = (self.ROOT / name).read_bytes()
            self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"), f"{name} 必须全部使用 CRLF 换行")
            self.assertTrue(all(b <= 127 for b in raw), f"{name} 不应包含非 ASCII 字符以免 CP936 乱码")

    def test_workbench_cmd_runs_cleanly_under_cp936(self):
        res = self._run_cmd("视频号复盘工作台.cmd", " --help", stdin="")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("--open", res.stdout)
        self.assertNotIn("不是内部或外部命令", res.stderr + res.stdout)

    def test_oneclick_publish_cmd_routes_double_click_special_paths_and_flags(self):
        # 1) Double-click (no arguments, blank line)
        res_empty = self._run_cmd("视频号一键发布.cmd", "", stdin="\n\n")
        self.assertEqual(res_empty.returncode, 1)
        self.assertIn("没有提供视频路径", res_empty.stdout)

        # 2) Dragged path containing &, (), !, spaces, and Chinese
        with tempfile.TemporaryDirectory() as tmp:
            special = Path(tmp) / "原片 & (1) !.mp4"
            res_drag = self._run_cmd("视频号一键发布.cmd", f' "{special}"', stdin="\n")
            self.assertEqual(res_drag.returncode, 1)
            self.assertIn(f"找不到文件：{special}", res_drag.stdout)

            # 3) CLI flag routing (-PrepareOnly)
            missing = Path(tmp) / "missing_flag.mp4"
            res_flag = self._run_cmd("视频号一键发布.cmd", f' -PrepareOnly "{missing}"', stdin="\n")
            self.assertEqual(res_flag.returncode, 1)
            self.assertIn(f"找不到文件：{missing}", res_flag.stdout)


if __name__ == "__main__":
    unittest.main()
