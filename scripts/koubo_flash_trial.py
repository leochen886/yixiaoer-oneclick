"""使用独立记录目录进行可重放的 DeepSeek 口播盲测。"""

import argparse
from pathlib import Path

from koubo import export, probe
from koubo_deepseek import build_cut_plan


def run(source, root, send_audio):
    _, duration, rate = probe(source)
    plan = build_cut_plan(source, root / "原声记录", duration, rate, send_audio=send_audio)
    return export(source, root, .35, cut_report=plan)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--send-audio", action="store_true", help="向 DeepSeek 发送本次音轨")
    args = parser.parse_args()
    print(run(args.video.resolve(), args.output_dir.resolve(), args.send_audio))
