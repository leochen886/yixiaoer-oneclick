from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from retro_analyze import (
    analyze_and_write,
    analyze_with_ai,
    load_playbook_prompt,
    load_published_runs,
    parse_metric_number,
    save_playbook,
    summarize_runs,
)


def make_run(
    root: Path,
    name: str,
    title: str,
    *,
    captured_at: str,
    plays: str | None = None,
    likes: str = "10",
    comments: str = "2",
    shares: str = "1",
    saves: str = "3",
    hook_title: str | None = None,
    comment_list: list | None = None,
    use_live_only: bool = False,
):
    run_dir = root / name
    run_dir.mkdir(parents=True, exist_ok=True)
    retro = {
        "capturedAt": captured_at,
        "publishTitle": title,
        "publishDesc": title + " 描述",
        "publishTags": ["人工智能"],
        "postId": None,
        "publishDate": "2026年11月20日 10:00",
        "metricsAtPublish": [{"num": "0"} for _ in range(5)],
    }
    (run_dir / "retro.json").write_text(json.dumps(retro, ensure_ascii=False), encoding="utf-8")
    (run_dir / "transcript.txt").write_text(title + " 口播正文内容。", encoding="utf-8")

    if hook_title:
        cut_dir = root.parent / ("out_" + name)
        cut_dir.mkdir(parents=True, exist_ok=True)
        video_path = cut_dir / (name + "_cut.mp4")
        video_path.write_bytes(b"mp4")
        (run_dir / "prepared-video.txt").write_text(str(video_path), encoding="utf-8")
        (cut_dir / "剪辑记录.json").write_text(
            json.dumps({"hook_title": hook_title, "duration": 60.0, "output_duration": 42.5}, ensure_ascii=False),
            encoding="utf-8",
        )

    if plays is not None:
        raw_metrics = [{"num": v} for v in (plays, likes, comments, shares, saves)]
        if use_live_only:
            (run_dir / "live-metrics.json").write_text(
                json.dumps({"capturedAt": captured_at, "rawMetrics": raw_metrics}, ensure_ascii=False),
                encoding="utf-8",
            )
        else:
            snap = {
                "collectedAt": captured_at,
                "stage": "3天",
                "ageHours": 75.0,
                "rawMetrics": raw_metrics,
                "comments": comment_list if comment_list is not None else [],
                "extraMetrics": {"completionRate": "35.2%"},
            }
            (run_dir / "retro-snapshots.json").write_text(
                json.dumps({"version": 1, "latest": snap, "snapshots": [snap]}, ensure_ascii=False),
                encoding="utf-8",
            )
    return run_dir


class RetroAnalyzeTests(unittest.TestCase):
    def test_parse_metric_number_supports_units_and_missing(self):
        self.assertEqual(parse_metric_number("1.2万"), 12000)
        self.assertEqual(parse_metric_number("1亿"), 100000000)
        self.assertEqual(parse_metric_number("1,234"), 1234)
        self.assertEqual(parse_metric_number("0"), 0)
        self.assertIsNone(parse_metric_number(""))
        self.assertIsNone(parse_metric_number("—"))
        self.assertIsNone(parse_metric_number(None))

    def test_load_summarize_winners_losers_dedup_and_xss_escape(self):
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "publish-runs"
            root.mkdir()
            for idx, plays in enumerate(["1.5万", "8000", "3200", "2500", "1800", "900", "120"]):
                captured = (now - timedelta(hours=80 - idx * 5)).isoformat()
                make_run(
                    root,
                    f"20261120-10000{idx}_v{idx}",
                    f"视频标题{idx}<script>alert(1)</script>",
                    captured_at=captured,
                    plays=plays,
                    hook_title=f"黄字{idx}",
                    comment_list=[{"author": "粉丝A", "time": "今天", "text": f"评论{idx}<b>hi</b>"}],
                )
            make_run(
                root,
                "20261123-090000_pending",
                "还没抓数据的新视频",
                captured_at=(now - timedelta(hours=3)).isoformat(),
                plays=None,
            )

            runs = load_published_runs(root, now=now)
            self.assertEqual(len(runs), 8)
            self.assertEqual(runs[-1]["stage"], "3天")
            self.assertEqual(runs[0]["stage"], "<24h")
            self.assertFalse(runs[0]["collected"])

            summary = summarize_runs(runs)
            self.assertEqual(summary["total_published"], 8)
            self.assertEqual(summary["collected_count"], 7)
            self.assertEqual(summary["max_plays"], 15000)
            self.assertEqual(len(summary["winners"]), 5)
            self.assertEqual(len(summary["losers"]), 2)
            winner_names = {r["run_name"] for r in summary["winners"]}
            loser_names = {r["run_name"] for r in summary["losers"]}
            self.assertTrue(winner_names.isdisjoint(loser_names))
            self.assertEqual(summary["losers"][0]["metrics"]["plays"], 120)

            res = analyze_and_write(root, use_ai=False, open_html=False)
            html_text = res["dashboard_html_path"].read_text(encoding="utf-8")
            md_text = res["overview_md_path"].read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", html_text)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_text)
            self.assertIn("35.2%", html_text)
            self.assertIn("视频标题0", md_text)
            self.assertTrue(res["playbook_path"].is_file())

    def test_ai_analysis_claude_then_deepseek_fallback_and_playbook_cap(self):
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "publish-runs"
            make_run(root, "20261120-100000_a", "标题A", captured_at=now.isoformat(), plays="5000", hook_title="黄字A")
            runs = load_published_runs(root, now=now)
            summary = summarize_runs(runs)

            payload = {
                "executive_summary": "大白话总结：选题聚焦很有效。",
                "why_winners_won": ["开头有反差"],
                "why_losers_lost": ["样本少"],
                "next_topics": [{"title": "下期聊聊实用案例", "hook_title": "到底怎么用？", "angle": "直接上演示"}],
                "winning_patterns": ["用具体数字开头", "结尾留争议问题"],
                "avoid_patterns": ["避免自嗨" * 60],
                "hook_title_tips": ["不超过12字" * 40],
                "copywriting_tips": ["短标题与黄字呼应" * 40],
                "glossary_suggestions": [{"wrong": "杰米奶", "right": "Gemini"}],
            }

            with patch("retro_analyze.subprocess.run") as run_cli:
                run_cli.return_value = Mock(returncode=0, stdout=json.dumps({"result": json.dumps(payload)}), stderr="")
                ai = analyze_with_ai(summary, runs, use_ai=True)
                self.assertEqual(ai["source"], "claude")
                self.assertEqual(ai["glossary_suggestions"], [{"wrong": "杰米奶", "right": "Gemini"}])

            with patch("retro_analyze.subprocess.run", side_effect=OSError("no claude")), patch(
                "retro_analyze.requests.post"
            ) as post, patch("koubo_proofread.api_key", return_value="test"):
                post.return_value.status_code = 200
                post.return_value.raise_for_status = Mock()
                post.return_value.json.return_value = {
                    "choices": [{"message": {"content": json.dumps(payload)}}]
                }
                ai2 = analyze_with_ai(summary, runs, use_ai=True)
                self.assertEqual(ai2["source"], "deepseek")

            save_playbook(root, summary, ai2)
            hook_prompt = load_playbook_prompt(root, mode="hook")
            copy_prompt = load_playbook_prompt(root, mode="copy")
            self.assertTrue(hook_prompt)
            self.assertTrue(copy_prompt)
            self.assertLessEqual(len(hook_prompt), 400)
            self.assertLessEqual(len(copy_prompt), 400)

    def test_compatible_with_workspace_publish_runs(self):
        real_runs = Path(__file__).resolve().parents[1] / "publish-runs"
        if real_runs.is_dir():
            runs = load_published_runs(real_runs)
            self.assertGreaterEqual(len(runs), 18)


if __name__ == "__main__":
    unittest.main()
