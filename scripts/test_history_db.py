from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import history_db
import retro_analyze


CSV_HEADER = (
    '"视频描述","视频ID","发布时间","完播率","平均播放时长","播放量","推荐","喜欢","评论量","分享量","关注量",'
    '"转发聊天和朋友圈","设为铃声","设为状态","设为朋友圈封面","企微链接点击次数","企微链接点击人数","添加到通讯录次数","添加到通讯录人数"\n'
)


def make_run_fixture(
    root: Path,
    run_name: str,
    *,
    captured_at: str,
    title: str,
    desc: str,
    transcript: str,
    hook_context: str = "白字背景",
    hook_title: str = "黄字疑问吗",
    snap_plays: str | None = None,
    snap_at: str | None = None,
    live_plays: str | None = None,
    live_at: str | None = None,
    post_id: str | None = None,
    comments: list | None = None,
) -> Path:
    runs_dir = root / "publish-runs"
    run_dir = runs_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    retro = {
        "capturedAt": captured_at,
        "publishTitle": title,
        "publishDesc": desc,
        "publishTags": ["人工智能", "大模型"],
        "postId": post_id,
        "publishDate": "2026年11月20日 10:00",
        "metricsAtPublish": [{"num": "0"} for _ in range(5)],
    }
    (run_dir / "retro.json").write_text(json.dumps(retro, ensure_ascii=False), encoding="utf-8")
    (run_dir / "transcript.txt").write_text(transcript, encoding="utf-8")

    cut_dir = root / "output" / (run_name + "_cut")
    cut_dir.mkdir(parents=True, exist_ok=True)
    vp = cut_dir / "final.mp4"
    vp.write_bytes(b"mp4")
    (run_dir / "prepared-video.txt").write_text(str(vp), encoding="utf-8")
    (cut_dir / "剪辑记录.json").write_text(
        json.dumps(
            {
                "hook_context": hook_context,
                "hook_title": hook_title,
                "hook_summary": "主旨概括",
                "duration": 45.0,
                "output_duration": 30.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    if snap_plays is not None:
        snap = {
            "collectedAt": snap_at or captured_at,
            "stage": "1天",
            "ageHours": 25.0,
            "rawMetrics": [{"num": snap_plays}, {"num": "2"}, {"num": "1"}, {"num": "1"}, {"num": "4"}],
            "comments": comments if comments is not None else [{"author": "粉丝", "time": "今天", "text": "说得好"}],
        }
        (run_dir / "retro-snapshots.json").write_text(
            json.dumps({"version": 1, "latest": snap, "snapshots": [snap]}, ensure_ascii=False),
            encoding="utf-8",
        )

    if live_plays is not None:
        live = {
            "capturedAt": live_at or captured_at,
            "rawMetrics": [{"num": live_plays}, {"num": "5"}, {"num": "2"}, {"num": "3"}, {"num": "6"}],
        }
        (run_dir / "live-metrics.json").write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")

    return run_dir


class HistoryDbTests(unittest.TestCase):
    def test_parsers_handle_multiline_units_and_missing(self):
        self.assertAlmostEqual(history_db.parse_percent("38.04%"), 0.3804)
        self.assertIsNone(history_db.parse_percent("-"))
        self.assertEqual(history_db.parse_duration_sec("13.92秒"), 13.92)
        self.assertEqual(history_db.parse_duration_sec("1分12.5秒"), 72.5)
        self.assertEqual(history_db.parse_duration_sec("01:12"), 72.0)
        self.assertIsNone(history_db.parse_duration_sec("-"))
        self.assertEqual(history_db.parse_int_metric("1,234"), 1234)
        self.assertEqual(history_db.parse_int_metric("1.2万"), 12000)
        self.assertIsNone(history_db.parse_int_metric(""))

        body, tags = history_db.split_desc_and_tags("第一行观点。\n第二行总结。\n\n#克劳德 #AI ")
        self.assertEqual(body, "第一行观点。 第二行总结。")
        self.assertEqual(tags, ["克劳德", "AI"])

    def test_7d_csv_does_not_overwrite_30d_lifetime_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            hdir = Path(tmp) / "history"
            row_30d = (
                CSV_HEADER
                + '"老视频讲透一件事 #认知","export/OLD1","2026/11/01","32.50%","18.20秒",5000,3,20,4,6,12,6,0,0,0,,,,\n'
                + '"新视频刚发两天 #热点","export/NEW1","2026/11/23","40.00%","12.00秒",800,1,4,1,2,3,2,0,0,0,,,,\n'
            )
            res30 = history_db.import_csv(
                "30d.csv",
                history_dir=hdir,
                csv_text=row_30d,
                window_type="30d",
                window_end="2026-11-24",
                collected_at="2026-11-24T10:00:00+08:00",
            )
            self.assertEqual(res30["updated_lifetime_count"], 2)

            # Now import a 7d CSV where the old video only received 120 plays in the last 7 days.
            row_7d = (
                CSV_HEADER
                + '"老视频讲透一件事 #认知","export/OLD1","2026/11/01","15.00%","8.00秒",120,0,1,0,0,1,0,0,0,0,,,,\n'
                + '"新视频刚发两天 #热点","export/NEW1","2026/11/23","42.00%","13.00秒",1500,2,8,2,3,7,3,0,0,0,,,,\n'
            )
            res7 = history_db.import_csv(
                "7d.csv",
                history_dir=hdir,
                csv_text=row_7d,
                window_type="7d",
                window_end="2026-11-24",
                collected_at="2026-11-24T16:00:00+08:00",
            )
            self.assertEqual(res7["window_only_count"], 1)
            self.assertEqual(res7["updated_lifetime_count"], 1)

            videos = {v["platform_id"]: v for v in history_db.load_all_videos(hdir)}
            old_v = videos["export/OLD1"]
            new_v = videos["export/NEW1"]
            # Old video retains 5000 lifetime plays, while 7d window stores 120!
            self.assertEqual(old_v["latest_metrics"]["plays"], 5000)
            self.assertEqual(old_v["metrics_scope"], "lifetime")
            self.assertEqual(old_v["window_metrics"]["7d"]["covers_full_lifetime"], False)
            self.assertEqual(old_v["window_metrics"]["7d"]["metrics"]["plays"], 120)
            self.assertEqual(old_v["window_metrics"]["30d"]["metrics"]["plays"], 5000)

            # New video (published 2026-11-23 >= 2026-11-18) IS covered by 7d, so updates to 1500!
            self.assertEqual(new_v["latest_metrics"]["plays"], 1500)
            self.assertEqual(new_v["window_metrics"]["7d"]["covers_full_lifetime"], True)

    def test_partial_window_only_marks_scope_and_merges_both_orders(self):
        for order in ("runs_first", "csv_first"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                hdir = root / "history"
                make_run_fixture(
                    root,
                    "20261120-120000-100_demo",
                    captured_at="2026-11-20T12:00:00+08:00",
                    title="测试短标题",
                    desc="这是一段带换行和标点的正文。\n第二句看这里。",
                    transcript="大家好这是完整口播文案第一句很关键。",
                    hook_context="顶部白字铺垫",
                    hook_title="顶部黄字问题",
                    snap_plays="200",
                    snap_at="2026-11-21T12:00:00+08:00",
                    live_plays="350",
                    live_at="2026-11-22T12:00:00+08:00",
                )
                csv_multiline = (
                    CSV_HEADER
                    + '"这是一段带换行和标点的正文。\n第二句看这里。\n\n#人工智能 #大模型","export/ID123","2026/11/20","35.50%","14.50秒",1200,2,10,3,4,8,4,0,0,0,,,,\n'
                )
                if order == "runs_first":
                    history_db.sync_publish_runs(root / "publish-runs", history_dir=hdir, root=root)
                    history_db.import_csv(
                        "m.csv",
                        history_dir=hdir,
                        csv_text=csv_multiline,
                        window_type="30d",
                        window_end="2026-11-24",
                        collected_at="2026-11-24T15:00:00+08:00",
                    )
                else:
                    history_db.import_csv(
                        "m.csv",
                        history_dir=hdir,
                        csv_text=csv_multiline,
                        window_type="30d",
                        window_end="2026-11-24",
                        collected_at="2026-11-24T15:00:00+08:00",
                    )
                    history_db.sync_publish_runs(root / "publish-runs", history_dir=hdir, root=root)

                vids = history_db.load_all_videos(hdir)
                self.assertEqual(len(vids), 1)
                v = vids[0]
                self.assertEqual(v["platform_id"], "export/ID123")
                self.assertEqual(v["creative"]["hook_context"], "顶部白字铺垫")
                self.assertEqual(v["creative"]["hook_title"], "顶部黄字问题")
                self.assertIn("完整口播文案", v["creative"]["transcript"])
                self.assertEqual(v["latest_metrics"]["plays"], 1200)
                self.assertAlmostEqual(v["latest_metrics"]["completion_rate"], 0.355)
                self.assertEqual(v["latest_metrics"]["follows"], 8)
                # Saves & comments preserved from ego snapshot!
                self.assertEqual(v["latest_metrics"]["saves"], 6)
                self.assertEqual(len(v["comments"]), 1)

    def test_retro_analyze_live_metrics_bug_fix_and_stage_separation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hdir = root / "history"
            runs_dir = root / "publish-runs"
            make_run_fixture(
                root,
                "20261121-100000-1_mature",
                captured_at=(datetime.now(timezone.utc) - timedelta(hours=75)).isoformat(),
                title="成熟老视频",
                desc="成熟视频描述",
                transcript="成熟视频口播",
                snap_plays="100",
                snap_at=(datetime.now(timezone.utc) - timedelta(hours=50)).isoformat(),
                live_plays="980",
                live_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            )
            make_run_fixture(
                root,
                "20261124-140000-2_new",
                captured_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                title="刚发两小时新视频",
                desc="新视频描述",
                transcript="新视频口播",
                live_plays="12",
                live_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            )

            # Verify legacy load_published_runs picks fresher live-metrics (980 > 100)!
            loaded = retro_analyze.load_published_runs(runs_dir)
            by_title = {r["publish_title"]: r for r in loaded}
            self.assertEqual(by_title["成熟老视频"]["metrics"]["plays"], 980)
            self.assertEqual(by_title["成熟老视频"]["hook_context"], "白字背景")

            # Verify analyze_history keeps <24h observing video out of mature losers!
            history_db.sync_publish_runs(runs_dir, history_dir=hdir, root=root)
            res = retro_analyze.analyze_history(hdir, use_ai=False)
            self.assertEqual(res["summary"]["mature_count"], 1)
            self.assertEqual(res["summary"]["observing_count"], 1)
            review_doc = json.loads(res["review_path"].read_text(encoding="utf-8"))
            self.assertEqual(len(review_doc["losers_ids"]), 0)
            self.assertEqual(len(review_doc["observing_ids"]), 1)

    def test_playbook_rejected_rules_excluded_and_survive_reanalysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hdir = root / "history"
            runs_dir = root / "publish-runs"
            runs_dir.mkdir(parents=True)
            history_db.add_playbook_rule(hdir, category="copywriting", text="保留规则：第一句给数字", status="approved")
            history_db.add_playbook_rule(hdir, category="copywriting", text="废弃规则：不要再提这条", status="rejected")

            prompt = retro_analyze.load_playbook_prompt(runs_dir, mode="copy")
            self.assertIn("保留规则：第一句给数字", prompt)
            self.assertNotIn("废弃规则", prompt)

            # Running analyze_history must preserve approved & rejected choices.
            make_run_fixture(
                root,
                "20261121-100000-1_mature",
                captured_at="2026-11-21T10:00:00+08:00",
                title="测试标题",
                desc="测试描述",
                transcript="测试口播",
                live_plays="500",
            )
            history_db.sync_publish_runs(runs_dir, history_dir=hdir, root=root)
            retro_analyze.analyze_history(hdir, use_ai=False)

            pb = history_db.load_playbook(hdir)
            statuses = {r["text"]: r["status"] for r in pb["rules"]}
            self.assertEqual(statuses["保留规则：第一句给数字"], "approved")
            self.assertEqual(statuses["废弃规则：不要再提这条"], "rejected")
            prompt_after = retro_analyze.load_playbook_prompt(runs_dir, mode="copy")
            self.assertNotIn("废弃规则", prompt_after)


if __name__ == "__main__":
    unittest.main()
