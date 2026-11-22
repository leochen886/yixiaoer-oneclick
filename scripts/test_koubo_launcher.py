import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
class PreviewLauncherTests(unittest.TestCase):
    def launch(self, arguments, stdin):
        launcher = Path(__file__).resolve().parents[1] / "剪口播.cmd"
        cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        command = f'"{cmd}" /d /s /c ""{launcher}"{arguments}"'
        env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
        return subprocess.run(command, input=stdin, capture_output=True, encoding="utf-8",
                              env=env, timeout=20)

    def test_drop_and_paste_preserve_the_entire_video_path(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "原片 & (带空格) !.mp4"
            for arguments, stdin in [(f' "{missing}"', "\n"), ("", f'"{missing}"\n\n')]:
                with self.subTest(arguments=arguments):
                    result = self.launch(arguments, stdin)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(f"找不到视频文件：{missing}", result.stderr)
                    self.assertEqual("把原视频拖进这个窗口" in result.stdout, not arguments)

    def test_double_click_waits_for_input_and_blank_line_cancels(self):
        result = self.launch("", "\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("已取消剪辑", result.stdout)
