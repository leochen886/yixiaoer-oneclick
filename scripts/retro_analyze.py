"""微信视频号复盘分析引擎：汇总数据、生成大白话诊断、维护账号经验小本本与可视化看板。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
from typing import Any

import requests


def parse_metric_number(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    raw = re.sub(r"[,，\s]", "", str(value))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(万|亿)?", raw)
    if not m:
        return None
    mult = {"万": 10_000, "亿": 100_000_000}.get(m.group(2), 1)
    return int(round(float(m.group(1)) * mult))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, data: Any) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _parse_published_at(retro: dict[str, Any], dir_name: str) -> datetime | None:
    captured = retro.get("capturedAt")
    if isinstance(captured, str) and captured.strip():
        iso = captured.strip()
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    m = re.match(r"^(\d{8})-(\d{6})(?:-\d+)?_", dir_name)
    if m:
        try:
            local_tz = datetime.now().astimezone().tzinfo
            dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=local_tz)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def _stage_of_hours(age_hours: float | None) -> str:
    if age_hours is None:
        return "未知"
    if age_hours >= 168:
        return "7天+"
    if age_hours >= 72:
        return "3天"
    if age_hours >= 24:
        return "1天"
    return "<24h"


def _extract_metrics_tuple(raw_metrics: Any, parsed: dict[str, Any] | None = None) -> dict[str, int | None]:
    if isinstance(parsed, dict) and any(k in parsed for k in ("plays", "likes", "comments", "shares", "saves")):
        return {
            "plays": parse_metric_number(parsed.get("plays")),
            "likes": parse_metric_number(parsed.get("likes")),
            "comments": parse_metric_number(parsed.get("comments")),
            "shares": parse_metric_number(parsed.get("shares")),
            "saves": parse_metric_number(parsed.get("saves")),
        }
    items = raw_metrics if isinstance(raw_metrics, list) else []
    keys = ("plays", "likes", "comments", "shares", "saves")
    out: dict[str, int | None] = {}
    for idx, key in enumerate(keys):
        if idx < len(items):
            cell = items[idx]
            raw = cell.get("num") if isinstance(cell, dict) else cell
            out[key] = parse_metric_number(raw)
        else:
            out[key] = None
    return out


def _compute_engagement(metrics: dict[str, int | None]) -> dict[str, Any]:
    parts = [metrics[k] for k in ("likes", "comments", "shares", "saves") if metrics.get(k) is not None]
    interactions = sum(parts) if parts else None
    plays = metrics.get("plays")

    def rate(val: int | None) -> float | None:
        if plays is None or plays <= 0 or val is None:
            return None
        return round(val / plays, 4)

    return {
        "interactions": interactions,
        "interaction_rate": rate(interactions),
        "like_rate": rate(metrics.get("likes")),
        "comment_rate": rate(metrics.get("comments")),
        "share_rate": rate(metrics.get("shares")),
        "save_rate": rate(metrics.get("saves")),
    }


def _find_cut_record(run_dir: Path) -> dict[str, Any]:
    candidates: list[Path] = []
    prep = run_dir / "prepared-video.txt"
    if prep.is_file():
        try:
            video_path = Path(prep.read_text(encoding="utf-8-sig").strip())
            candidates.append(video_path.with_name("剪辑记录.json"))
        except OSError:
            pass
    ego_input = run_dir / "ego-input.json"
    if ego_input.is_file():
        try:
            inp = _read_json(ego_input)
            vp = inp.get("videoPath")
            if isinstance(vp, str) and vp.strip():
                candidates.append(Path(vp).with_name("剪辑记录.json"))
        except (OSError, ValueError):
            pass
    for path in candidates:
        try:
            if path.is_file():
                data = _read_json(path)
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            continue
    return {}


def load_published_runs(runs_dir: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    records: list[dict[str, Any]] = []

    for sub in sorted(runs_dir.iterdir(), key=lambda p: p.name):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        retro_path = sub / "retro.json"
        if not retro_path.is_file():
            continue
        try:
            retro = _read_json(retro_path)
            if not isinstance(retro, dict):
                continue
        except (OSError, ValueError):
            continue

        published_at = _parse_published_at(retro, sub.name)
        age_hours = (
            round(max(0.0, (now_utc - published_at).total_seconds() / 3600.0), 2)
            if published_at is not None
            else None
        )
        stage = _stage_of_hours(age_hours)

        snap_store = {}
        snap_path = sub / "retro-snapshots.json"
        if snap_path.is_file():
            try:
                snap_store = _read_json(snap_path)
            except (OSError, ValueError):
                snap_store = {}
        snapshots = snap_store.get("snapshots") if isinstance(snap_store, dict) else None
        if not isinstance(snapshots, list):
            snapshots = []
        latest_snap = snap_store.get("latest") if isinstance(snap_store, dict) else None
        if not isinstance(latest_snap, dict) and snapshots:
            latest_snap = snapshots[-1] if isinstance(snapshots[-1], dict) else None

        live_data = None
        live_path = sub / "live-metrics.json"
        if live_path.is_file():
            try:
                loaded_live = _read_json(live_path)
                if isinstance(loaded_live, dict):
                    live_data = loaded_live
            except (OSError, ValueError):
                live_data = None

        source_data: dict[str, Any] = {}
        if latest_snap and live_data:
            snap_ts = str(latest_snap.get("collectedAt") or "")
            live_ts = str(live_data.get("capturedAt") or live_data.get("collectedAt") or "")
            if live_ts > snap_ts:
                source_data = {
                    **live_data,
                    "collectedAt": live_ts,
                    "extraMetrics": live_data.get("extraMetrics") or latest_snap.get("extraMetrics"),
                }
            else:
                source_data = latest_snap
        else:
            source_data = latest_snap or live_data or {}

        collected = bool(latest_snap is not None or live_data is not None)
        metrics = _extract_metrics_tuple(
            source_data.get("rawMetrics") if collected else retro.get("metricsAtPublish"),
            source_data.get("parsedMetrics") if collected else None,
        )
        engagement = _compute_engagement(metrics)

        extra_metrics = source_data.get("extraMetrics") if isinstance(source_data.get("extraMetrics"), dict) else None
        if not extra_metrics and isinstance(latest_snap, dict) and isinstance(latest_snap.get("extraMetrics"), dict):
            extra_metrics = latest_snap["extraMetrics"]
        comments: list[dict[str, Any]] | None = None
        if isinstance(source_data.get("comments"), list):
            comments = source_data["comments"]
        else:
            for item in reversed(snapshots):
                if isinstance(item, dict) and isinstance(item.get("comments"), list):
                    comments = item["comments"]
                    break

        transcript = ""
        transcript_path = sub / "transcript.txt"
        if transcript_path.is_file():
            try:
                transcript = transcript_path.read_text(encoding="utf-8-sig").strip()
            except OSError:
                transcript = ""

        cut_record = _find_cut_record(sub)
        hook_title = cut_record.get("hook_title") if isinstance(cut_record.get("hook_title"), str) else ""
        hook_context = cut_record.get("hook_context") if isinstance(cut_record.get("hook_context"), str) else ""
        hook_summary = cut_record.get("hook_summary") if isinstance(cut_record.get("hook_summary"), str) else ""
        source_duration = cut_record.get("duration")
        output_duration = cut_record.get("output_duration")

        records.append({
            "run_name": sub.name,
            "run_dir": str(sub),
            "post_id": retro.get("postId") or None,
            "publish_title": str(retro.get("publishTitle") or ""),
            "publish_desc": str(retro.get("publishDesc") or ""),
            "publish_tags": list(retro.get("publishTags") or []),
            "publish_date": str(retro.get("publishDate") or ""),
            "published_at": published_at.isoformat() if published_at else None,
            "age_hours": age_hours,
            "stage": stage,
            "collected": collected,
            "collected_at": source_data.get("collectedAt") or source_data.get("capturedAt"),
            "has_report": (sub / "report.md").is_file(),
            "snapshots_count": len(snapshots),
            "hook_context": hook_context,
            "hook_title": hook_title,
            "hook_summary": hook_summary,
            "source_duration": source_duration,
            "output_duration": output_duration,
            "transcript": transcript,
            "metrics": metrics,
            "engagement": engagement,
            "extra_metrics": extra_metrics,
            "comments": comments,
        })

    records.sort(key=lambda r: r["published_at"] or r["run_name"], reverse=True)
    return records


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    collected_runs = [r for r in runs if r.get("collected")]
    with_plays = [r for r in collected_runs if r["metrics"].get("plays") is not None]
    plays_values = [r["metrics"]["plays"] for r in with_plays]
    rate_values = [
        r["engagement"]["interaction_rate"]
        for r in collected_runs
        if r["engagement"].get("interaction_rate") is not None
    ]

    winners = sorted(
        with_plays,
        key=lambda r: (
            -(r["metrics"]["plays"] or 0),
            -(r["engagement"].get("interaction_rate") or -1.0),
        ),
    )[:5]
    winner_keys = {r["run_name"] for r in winners}

    losers = sorted(
        [r for r in with_plays if r["run_name"] not in winner_keys],
        key=lambda r: (
            r["metrics"]["plays"] or 0,
            r["engagement"].get("interaction_rate") if r["engagement"].get("interaction_rate") is not None else 999.0,
        ),
    )[:5]

    hot_comments: list[dict[str, Any]] = []
    for run in sorted(collected_runs, key=lambda r: -(r["metrics"].get("plays") or 0)):
        for c in (run.get("comments") or [])[:5]:
            if isinstance(c, dict) and c.get("text"):
                hot_comments.append({
                    "video_title": run["publish_title"] or run["run_name"],
                    "author": str(c.get("author") or "匿名"),
                    "time": str(c.get("time") or ""),
                    "text": str(c.get("text") or ""),
                })
        if len(hot_comments) >= 30:
            break

    return {
        "total_published": len(runs),
        "collected_count": len(collected_runs),
        "pending_count": len(runs) - len(collected_runs),
        "total_plays": sum(plays_values) if plays_values else 0,
        "avg_plays": round(sum(plays_values) / len(plays_values), 1) if plays_values else 0.0,
        "median_plays": round(statistics.median(plays_values), 1) if plays_values else 0.0,
        "max_plays": max(plays_values) if plays_values else 0,
        "avg_interaction_rate": round(sum(rate_values) / len(rate_values), 4) if rate_values else None,
        "winners": winners,
        "losers": losers,
        "hot_comments": hot_comments[:30],
    }


def _compact_run_for_ai(run: dict[str, Any]) -> dict[str, Any]:
    m = run["metrics"]
    e = run["engagement"]
    return {
        "短标题": run["publish_title"],
        "顶部黄字": run["hook_title"],
        "成片秒数": round(run["output_duration"], 1) if isinstance(run["output_duration"], (int, float)) else None,
        "发布阶段": run["stage"],
        "播放": m.get("plays"),
        "点赞": m.get("likes"),
        "评论数": m.get("comments"),
        "转发": m.get("shares"),
        "收藏": m.get("saves"),
        "总互动率": f"{e['interaction_rate'] * 100:.2f}%" if e.get("interaction_rate") is not None else None,
        "深度数据": run.get("extra_metrics"),
        "标签": run.get("publish_tags"),
        "口播全文节选": (run.get("transcript") or "")[:360],
        "热门评论": [
            f"{c.get('author', '观众')}: {c.get('text', '')}"
            for c in (run.get("comments") or [])[:5]
            if isinstance(c, dict) and c.get("text")
        ],
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI 返回里找不到 JSON 对象")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI 返回结果不是 JSON 对象")
    return parsed


def _normalize_ai_payload(raw: dict[str, Any], summary: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    def str_list(val: Any) -> list[str]:
        if isinstance(val, str):
            return [val.strip()] if val.strip() else []
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        return []

    topics: list[dict[str, str]] = []
    for item in raw.get("next_topics") or []:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("topic") or "").strip()
            hook = str(item.get("hook_title") or "").strip()
            angle = str(item.get("angle") or "").strip()
            if title or hook or angle:
                topics.append({"title": title, "hook_title": hook, "angle": angle})
        elif isinstance(item, str) and item.strip():
            topics.append({"title": item.strip(), "hook_title": "", "angle": ""})

    glossary: list[dict[str, str]] = []
    for item in raw.get("glossary_suggestions") or []:
        if isinstance(item, dict):
            wrong = str(item.get("wrong") or "").strip()
            right = str(item.get("right") or "").strip()
            if right:
                glossary.append({"wrong": wrong, "right": right})
        elif isinstance(item, str) and item.strip():
            glossary.append({"wrong": "", "right": item.strip()})

    fallback = _local_fallback_analysis(summary, runs)
    return {
        "source": raw.get("source") or "ai",
        "executive_summary": str(raw.get("executive_summary") or fallback["executive_summary"]).strip(),
        "why_winners_won": str_list(raw.get("why_winners_won")) or fallback["why_winners_won"],
        "why_losers_lost": str_list(raw.get("why_losers_lost")) or fallback["why_losers_lost"],
        "next_topics": topics or fallback["next_topics"],
        "winning_patterns": str_list(raw.get("winning_patterns")) or fallback["winning_patterns"],
        "avoid_patterns": str_list(raw.get("avoid_patterns")) or fallback["avoid_patterns"],
        "hook_title_tips": str_list(raw.get("hook_title_tips")) or fallback["hook_title_tips"],
        "copywriting_tips": str_list(raw.get("copywriting_tips")) or fallback["copywriting_tips"],
        "glossary_suggestions": glossary,
    }


def _local_fallback_analysis(summary: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    total = summary["total_published"]
    collected = summary["collected_count"]
    if collected == 0:
        return {
            "source": "local",
            "executive_summary": f"目前一共发了 {total} 条视频，还没有抓回后台播放和评论数据。先跑一次「一键复盘」抓回数据，这里就会给出红黑榜对比和下一期选题建议。",
            "why_winners_won": ["抓回后台数据后，会自动拆解跑得最好的视频为什么能吸睛。"],
            "why_losers_lost": ["抓回后台数据后，会自动对比播放偏低视频的问题出在标题、开头还是选题。"],
            "next_topics": [
                {"title": "等数据抓回后自动推荐选题", "hook_title": "下期拍啥更吸睛？", "angle": "跑完一次一键复盘，系统会结合评论区提问给出 3~5 个选题。"}
            ],
            "winning_patterns": ["保持一句能看懂的顶部黄字 + 明确观点开头"],
            "avoid_patterns": ["避免标题和画面黄字说两件事"],
            "hook_title_tips": ["先读完整片。优先用白字写具体处境，让黄字接一句观众真想问、口播真能答的问题。白字重复时可省略"],
            "copywriting_tips": ["短标题直接点出痛点或反差，简介结尾留一个能引发评论的问题"],
            "glossary_suggestions": [],
        }

    top = summary["winners"][0] if summary["winners"] else None
    bottom = summary["losers"][0] if summary["losers"] else None
    rate_text = (
        f"平均每 100 次播放带来 {summary['avg_interaction_rate'] * 100:.2f} 次互动。"
        if summary.get("avg_interaction_rate") is not None
        else ""
    )
    top_desc = (
        f"最好的一条是《{top['publish_title']}》，播放 {top['metrics']['plays']}。"
        if top
        else ""
    )
    return {
        "source": "local",
        "executive_summary": (
            f"一共发了 {total} 条视频，已抓回 {collected} 条的数据：累计播放 {summary['total_plays']}，"
            f"平均每条 {summary['avg_plays']} 播放。{rate_text}{top_desc}"
        ).strip(),
        "why_winners_won": [
            f"《{r['publish_title']}》拿到 {r['metrics']['plays']} 播放，主题明确、观点直接，互动表现相对稳。"
            for r in summary["winners"][:3]
        ],
        "why_losers_lost": [
            f"《{r['publish_title']}》目前只有 {r['metrics']['plays']} 播放，可检查开头黄字和短标题是否够抓人。"
            for r in summary["losers"][:3]
        ] or ["暂无明显落后样本，等更多视频累积 3 天数据后再对比。"],
        "next_topics": [
            {
                "title": f"顺着《{top['publish_title']}》的观众反馈继续深挖一个具体问题" if top else "挑一条最近争议最大的话题讲透",
                "hook_title": "这事到底怎么选？",
                "angle": "直接回应评论区或上一条最受关注的点，开头 3 秒先亮结论。",
            }
        ],
        "winning_patterns": ["开头直接抛反差或具体数字，观众更容易停下来看"],
        "avoid_patterns": ["少用空泛大词，顶部黄字和短标题不要各讲各的"],
        "hook_title_tips": ["白字优先摆出人、事或代价，黄字把答案留给口播。白字重复或揭底时可省略。两行合计最多 24 个全角字，都不加标点"],
        "copywriting_tips": ["短标题 16 字内说清冲突或好处，简介结尾用开放式提问拉评论"],
        "glossary_suggestions": [],
    }


def _call_claude_cli(prompt: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--tools", "", "--safe-mode", "--disable-slash-commands"],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude 退出码 {proc.returncode}: {proc.stderr.strip()}")
    envelope = None
    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        for line in reversed(proc.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    cand = json.loads(line)
                    if isinstance(cand, dict) and "result" in cand:
                        envelope = cand
                        break
                except ValueError:
                    continue
    if not isinstance(envelope, dict) or not envelope.get("result"):
        raise ValueError("无法解析 claude 输出")
    parsed = _extract_json_object(str(envelope["result"]))
    parsed["source"] = "claude"
    return parsed


def _call_deepseek_flash(prompt: str) -> dict[str, Any]:
    try:
        from koubo_proofread import ENDPOINT, MODEL, api_key
    except ImportError:
        from scripts.koubo_proofread import ENDPOINT, MODEL, api_key

    payload = {
        "model": MODEL,
        "thinking": {"type": "disabled"},
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "你是只说大白话的短视频运营教练，只输出 JSON 对象。"},
            {"role": "user", "content": prompt},
        ],
    }
    resp = requests.post(
        ENDPOINT,
        json=payload,
        headers={"Authorization": "Bearer " + api_key()},
        timeout=(15, 120),
        allow_redirects=False,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    parsed = _extract_json_object(content)
    parsed["source"] = "deepseek"
    return parsed


def analyze_with_ai(summary: dict[str, Any], runs: list[dict[str, Any]], *, use_ai: bool = True) -> dict[str, Any]:
    if not use_ai or summary["collected_count"] == 0:
        return _local_fallback_analysis(summary, runs)

    payload_data = {
        "概览": {
            "总发布数": summary["total_published"],
            "已抓回数据数": summary["collected_count"],
            "累计播放": summary["total_plays"],
            "平均播放": summary["avg_plays"],
            "中位播放": summary["median_plays"],
            "最高播放": summary["max_plays"],
            "平均互动率": f"{summary['avg_interaction_rate'] * 100:.2f}%" if summary.get("avg_interaction_rate") is not None else None,
        },
        "红榜Top5": [_compact_run_for_ai(r) for r in summary["winners"]],
        "黑榜Bottom5": [_compact_run_for_ai(r) for r in summary["losers"]],
        "其余样本": [
            _compact_run_for_ai(r)
            for r in runs
            if r.get("collected")
            and r["run_name"] not in {x["run_name"] for x in summary["winners"]}
            and r["run_name"] not in {x["run_name"] for x in summary["losers"]}
        ][:10],
        "观众热门评论": summary["hot_comments"][:25],
    }

    prompt = (
        "你是一位帮老板看视频号数据的运营搭档。老板不懂任何计算机黑话，只关心：哪些视频跑得好、为什么、哪些没跑起来、接下来拍什么能更火。\n"
        "请严格基于下面给出的真实数据、顶部黄字、短标题、口播节选和评论，用**最接地气的大白话**做复盘，绝对不要编造数据里没有的数字。\n"
        "只输出一个 JSON 对象，不要任何解释或代码块标记，必须包含以下字段：\n"
        "{\n"
        '  "executive_summary": "2到4句大白话总评，说清当前整体表现和最大发现",\n'
        '  "why_winners_won": ["跑得好的第1个原因（结合具体视频名或黄字）", "..."],\n'
        '  "why_losers_lost": ["没跑起来的第1个原因（结合具体视频名）", "..."],\n'
        '  "next_topics": [{"title": "下期选题方向", "hook_title": "建议一行无标点黄字 提出后文能回应的具体疑问", "angle": "怎么开讲最抓人"}],\n'
        '  "winning_patterns": ["总结出的有效套路1", "..."],\n'
        '  "avoid_patterns": ["要避开的坑1", "..."],\n'
        '  "hook_title_tips": ["顶部黄字写法建议1", "..."],\n'
        '  "copywriting_tips": ["短标题与简介标签写法建议1", "..."],\n'
        '  "glossary_suggestions": [{"wrong": "常见听错写法如杰米奶", "right": "正确专名如Gemini"}]\n'
        "}\n"
        "next_topics 请给出 3 到 5 个具体能直接开拍的选题。\n\n"
        "真实数据如下：\n"
        + json.dumps(payload_data, ensure_ascii=False, indent=2)
    )

    for caller in (_call_claude_cli, _call_deepseek_flash):
        try:
            raw = caller(prompt)
            return _normalize_ai_payload(raw, summary, runs)
        except Exception:
            continue
    return _local_fallback_analysis(summary, runs)


def save_playbook(runs_dir: Path, summary: dict[str, Any], analysis: dict[str, Any]) -> Path:
    top_examples = [
        {
            "title": r["publish_title"],
            "hook_title": r["hook_title"],
            "plays": r["metrics"].get("plays"),
            "interaction_rate": r["engagement"].get("interaction_rate"),
        }
        for r in summary["winners"][:3]
    ]
    playbook = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source_runs_count": summary["total_published"],
        "collected_count": summary["collected_count"],
        "analysis_source": analysis.get("source", "local"),
        "executive_summary": analysis["executive_summary"],
        "winning_patterns": analysis["winning_patterns"],
        "avoid_patterns": analysis["avoid_patterns"],
        "hook_title_tips": analysis["hook_title_tips"],
        "copywriting_tips": analysis["copywriting_tips"],
        "next_topics": analysis["next_topics"],
        "glossary_suggestions": analysis["glossary_suggestions"],
        "top_examples": top_examples,
    }
    path = Path(runs_dir) / "account-playbook.json"
    _atomic_write_json(path, playbook)
    return path


def _import_history_db():
    try:
        import history_db
        return history_db
    except ImportError:
        from scripts import history_db
        return history_db


def load_playbook_prompt(runs_dir: Path, mode: str = "copy") -> str:
    history_dir = Path(runs_dir).parent / "history"
    history_pb = history_dir / "playbook.json"
    if history_pb.is_file():
        try:
            hdb = _import_history_db()
            text = hdb.format_playbook_prompt(history_dir, mode=mode)
            if text:
                return text
        except Exception:
            pass

    path = Path(runs_dir) / "account-playbook.json"
    if not path.is_file():
        return ""
    try:
        pb = _read_json(path)
        if not isinstance(pb, dict):
            return ""
    except (OSError, ValueError):
        return ""

    parts: list[str] = []
    if mode == "hook":
        tips = pb.get("hook_title_tips") or []
        if tips:
            parts.append("黄字经验：" + "；".join(str(x) for x in tips[:2]))
        examples = [e.get("hook_title") for e in (pb.get("top_examples") or []) if isinstance(e, dict) and e.get("hook_title")]
        if examples:
            parts.append("高播放黄字参考：" + " / ".join(str(x) for x in examples[:2]))
    else:
        tips = pb.get("copywriting_tips") or []
        if tips:
            parts.append("文案经验：" + "；".join(str(x) for x in tips[:2]))
        examples = [e.get("title") for e in (pb.get("top_examples") or []) if isinstance(e, dict) and e.get("title")]
        if examples:
            parts.append("高播放标题参考：" + " / ".join(str(x) for x in examples[:2]))

    wins = pb.get("winning_patterns") or []
    if wins:
        parts.append("有效套路：" + "；".join(str(x) for x in wins[:2]))
    avoids = pb.get("avoid_patterns") or []
    if avoids:
        parts.append("注意避坑：" + "；".join(str(x) for x in avoids[:2]))

    text = "\n".join(p for p in parts if p.strip()).strip()
    if len(text) > 400:
        text = text[:397].rstrip("；;、，, \n") + "…"
    return text


def _fmt_num(val: int | float | None) -> str:
    if val is None:
        return "—"
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _fmt_rate(val: float | None) -> str:
    if val is None:
        return "—"
    return f"{val * 100:.2f}%"


def render_overview_markdown(summary: dict[str, Any], runs: list[dict[str, Any]], analysis: dict[str, Any]) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 微信视频号复盘总览",
        "",
        f"- 更新时间：{now_str}",
        f"- 累计发布：{summary['total_published']} 条（已抓回数据 {summary['collected_count']} 条）",
        f"- 累计播放：{_fmt_num(summary['total_plays'])} ｜ 平均每条播放：{_fmt_num(summary['avg_plays'])} ｜ 最高播放：{_fmt_num(summary['max_plays'])} ｜ 平均互动率：{_fmt_rate(summary.get('avg_interaction_rate'))}",
        "",
        "## 一句话总结",
        "",
        analysis["executive_summary"],
        "",
        "## 为什么这几条跑得好？",
        "",
    ]
    for item in analysis["why_winners_won"]:
        lines.append(f"- {item}")
    lines += ["", "## 为什么那几条没跑起来？", ""]
    for item in analysis["why_losers_lost"]:
        lines.append(f"- {item}")
    lines += ["", "## 下一期可以直接开拍的选题", ""]
    for idx, t in enumerate(analysis["next_topics"], 1):
        hook = f"（建议顶部黄字：{t['hook_title']}）" if t.get("hook_title") else ""
        angle = f" — {t['angle']}" if t.get("angle") else ""
        lines.append(f"{idx}. **{t.get('title', '')}**{hook}{angle}")

    if summary["winners"]:
        lines += [
            "",
            "## 红榜（表现最好的前几条）",
            "",
            "| 短标题 | 顶部黄字 | 阶段 | 播放 | 点赞 | 评论 | 转发 | 收藏 | 互动率 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in summary["winners"]:
            m, e = r["metrics"], r["engagement"]
            lines.append(
                f"| {r['publish_title']} | {r['hook_title'] or '—'} | {r['stage']} | "
                f"{_fmt_num(m['plays'])} | {_fmt_num(m['likes'])} | {_fmt_num(m['comments'])} | "
                f"{_fmt_num(m['shares'])} | {_fmt_num(m['saves'])} | {_fmt_rate(e['interaction_rate'])} |"
            )

    if summary["losers"]:
        lines += [
            "",
            "## 黑榜（播放暂时垫底的几条）",
            "",
            "| 短标题 | 顶部黄字 | 阶段 | 播放 | 点赞 | 评论 | 转发 | 收藏 | 互动率 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in summary["losers"]:
            m, e = r["metrics"], r["engagement"]
            lines.append(
                f"| {r['publish_title']} | {r['hook_title'] or '—'} | {r['stage']} | "
                f"{_fmt_num(m['plays'])} | {_fmt_num(m['likes'])} | {_fmt_num(m['comments'])} | "
                f"{_fmt_num(m['shares'])} | {_fmt_num(m['saves'])} | {_fmt_rate(e['interaction_rate'])} |"
            )

    if summary["hot_comments"]:
        lines += ["", "## 观众评论原声摘录", ""]
        for c in summary["hot_comments"][:15]:
            lines.append(f"- 《{c['video_title']}》 **{c['author']}**：{c['text']}")

    lines += [
        "",
        "---",
        "",
        "> 账号经验小本本 `account-playbook.json` 已更新，下次拖视频盲发时会自动参考以上经验。",
        "",
    ]
    return "\n".join(lines)


def render_dashboard_html(summary: dict[str, Any], runs: list[dict[str, Any]], analysis: dict[str, Any]) -> str:
    esc = html.escape
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    def render_list(items: list[str]) -> str:
        if not items:
            return "<li>暂无</li>"
        return "".join(f"<li>{esc(str(x))}</li>" for x in items)

    topic_cards = "".join(
        f"""<div class="topic-card">
  <div class="topic-num">选题 {idx}</div>
  <h3>{esc(t.get("title") or "")}</h3>
  {f'<div class="hook-badge">顶部黄字：{esc(t["hook_title"])}</div>' if t.get("hook_title") else ""}
  <p>{esc(t.get("angle") or "")}</p>
</div>"""
        for idx, t in enumerate(analysis["next_topics"], 1)
    )

    def rank_rows(items: list[dict[str, Any]]) -> str:
        if not items:
            return '<tr><td colspan="6" class="empty">暂无足够数据</td></tr>'
        rows_html = []
        for r in items:
            m, e = r["metrics"], r["engagement"]
            rows_html.append(
                f"<tr>"
                f"<td><strong>{esc(r['publish_title'] or r['run_name'])}</strong>"
                f"<div class='sub'>{esc(r['hook_title'] or '无顶部黄字')}</div></td>"
                f"<td>{esc(r['stage'])}</td>"
                f"<td class='num'>{esc(_fmt_num(m['plays']))}</td>"
                f"<td class='num'>{esc(_fmt_num(m['likes']))} / {esc(_fmt_num(m['comments']))}</td>"
                f"<td class='num'>{esc(_fmt_num(m['shares']))} / {esc(_fmt_num(m['saves']))}</td>"
                f"<td class='num'>{esc(_fmt_rate(e['interaction_rate']))}</td>"
                f"</tr>"
            )
        return "".join(rows_html)

    all_rows = []
    for r in runs:
        m, e = r["metrics"], r["engagement"]
        dur = f"{round(r['output_duration'])}秒" if isinstance(r.get("output_duration"), (int, float)) else "—"
        extra = r.get("extra_metrics") or {}
        comp = extra.get("completionRate") or extra.get("avgWatchDuration") or "—"
        status_tag = "<span class='tag ok'>已抓回</span>" if r["collected"] else "<span class='tag wait'>待抓数据</span>"
        plays_sort = m["plays"] if (r["collected"] and m["plays"] is not None) else -1
        time_sort = r["published_at"] or r["run_name"]
        disp = (lambda v: _fmt_num(v) if r["collected"] else "—")
        all_rows.append(
            f"<tr data-plays='{plays_sort}' data-time='{esc(time_sort)}' data-search='{esc((r['publish_title'] + ' ' + r['hook_title']).lower())}'>"
            f"<td>{status_tag}</td>"
            f"<td><strong>{esc(r['publish_title'] or r['run_name'])}</strong>"
            f"<div class='sub'>{esc(r['publish_date'] or r['run_name'])}</div></td>"
            f"<td>{esc(r['hook_title'] or '—')}</td>"
            f"<td>{esc(dur)}</td>"
            f"<td>{esc(r['stage'])}</td>"
            f"<td class='num'>{esc(disp(m['plays']))}</td>"
            f"<td class='num'>{esc(disp(m['likes']))}</td>"
            f"<td class='num'>{esc(disp(m['comments']))}</td>"
            f"<td class='num'>{esc(disp(m['shares']))}</td>"
            f"<td class='num'>{esc(disp(m['saves']))}</td>"
            f"<td class='num'>{esc(_fmt_rate(e['interaction_rate']) if r['collected'] else '—')}</td>"
            f"<td class='num'>{esc(str(comp))}</td>"
            f"</tr>"
        )

    comments_html = "".join(
        f"<div class='comment-item'><div class='comment-meta'>《{esc(c['video_title'])}》 · {esc(c['author'])} · {esc(c['time'])}</div>"
        f"<div class='comment-text'>{esc(c['text'])}</div></div>"
        for c in summary["hot_comments"][:20]
    ) or "<p class='empty'>暂无抓回来的观众评论。</p>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视频号复盘看板</title>
<style>
  :root {{
    --bg: #f6f7fb;
    --card: #ffffff;
    --text: #1b2230;
    --muted: #647084;
    --line: #e5e8f0;
    --accent: #07c160;
    --accent-soft: #eaf9f0;
    --warn: #d97706;
    --warn-soft: #fff7ed;
    --bad: #dc2626;
    --bad-soft: #fef2f2;
    --gold: #ca8a04;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: "Microsoft YaHei", "PingFang SC", system-ui, -apple-system, sans-serif;
    line-height: 1.6;
  }}
  .wrap {{ max-width: 1200px; margin: 0 auto; padding: 28px 20px 56px; }}
  .hero {{
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-end; gap: 12px;
    margin-bottom: 20px;
  }}
  .hero h1 {{ margin: 0; font-size: 28px; }}
  .hero .meta {{ color: var(--muted); font-size: 14px; }}
  .notice {{
    background: var(--accent-soft); border: 1px solid #b7ebc9; color: #0f5132;
    padding: 12px 16px; border-radius: 12px; margin-bottom: 20px; font-size: 14px;
  }}
  .kpis {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 14px; margin-bottom: 22px;
  }}
  .kpi {{
    background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px;
    box-shadow: 0 2px 6px rgba(15,23,42,0.03);
  }}
  .kpi .label {{ font-size: 13px; color: var(--muted); }}
  .kpi .val {{ font-size: 26px; font-weight: 700; margin-top: 4px; }}
  .card {{
    background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 20px 22px;
    margin-bottom: 20px; box-shadow: 0 2px 8px rgba(15,23,42,0.03);
  }}
  .card h2 {{ margin: 0 0 12px; font-size: 19px; }}
  .summary-lead {{ font-size: 16px; margin: 0; }}
  .grid-2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; margin-bottom: 20px; }}
  ul {{ margin: 0; padding-left: 20px; }}
  li {{ margin-bottom: 6px; }}
  .topics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }}
  .topic-card {{
    border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; background: #fbfcff;
  }}
  .topic-num {{ font-size: 12px; color: var(--accent); font-weight: 700; }}
  .topic-card h3 {{ margin: 4px 0 8px; font-size: 16px; }}
  .hook-badge {{
    display: inline-block; background: #fef9c3; color: #854d0e; font-size: 12px;
    padding: 2px 8px; border-radius: 999px; margin-bottom: 8px; font-weight: 600;
  }}
  .topic-card p {{ margin: 0; font-size: 13px; color: var(--muted); }}
  .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; align-items: center; }}
  .toolbar input {{
    padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; min-width: 240px; font-size: 14px;
  }}
  .toolbar button {{
    padding: 8px 12px; border: 1px solid var(--line); background: #fff; border-radius: 8px; cursor: pointer; font-size: 13px;
  }}
  .toolbar button.active {{ background: var(--text); color: #fff; border-color: var(--text); }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 10px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
  th {{ color: var(--muted); font-weight: 600; white-space: nowrap; }}
  td.num, th.num {{ text-align: right; white-space: nowrap; }}
  .sub {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; white-space: nowrap; }}
  .tag.ok {{ background: var(--accent-soft); color: #0f5132; }}
  .tag.wait {{ background: var(--warn-soft); color: #9a3412; }}
  .empty {{ color: var(--muted); text-align: center; padding: 16px; }}
  .comment-item {{ padding: 10px 0; border-bottom: 1px solid var(--line); }}
  .comment-item:last-child {{ border-bottom: none; }}
  .comment-meta {{ font-size: 12px; color: var(--muted); margin-bottom: 3px; }}
  .comment-text {{ font-size: 14px; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div>
      <h1>微信视频号复盘看板</h1>
      <div class="meta">更新时间：{esc(now_str)} ｜ 互动率 =（点赞 + 评论 + 转发 + 收藏）÷ 播放量</div>
    </div>
  </div>

  <div class="notice">
    ✅ <strong>经验已自动接回发布流程</strong>：这份复盘总结出的套路、避坑点和常用词写法，已经写进账号经验小本本，下次你拖视频盲发时 AI 会自动参考。
  </div>

  <div class="kpis">
    <div class="kpi"><div class="label">已发视频</div><div class="val">{summary["total_published"]}</div></div>
    <div class="kpi"><div class="label">已抓回数据</div><div class="val">{summary["collected_count"]}</div></div>
    <div class="kpi"><div class="label">累计播放</div><div class="val">{esc(_fmt_num(summary["total_plays"]))}</div></div>
    <div class="kpi"><div class="label">平均每条播放</div><div class="val">{esc(_fmt_num(summary["avg_plays"]))}</div></div>
    <div class="kpi"><div class="label">最高一条播放</div><div class="val">{esc(_fmt_num(summary["max_plays"]))}</div></div>
    <div class="kpi"><div class="label">平均互动率</div><div class="val">{esc(_fmt_rate(summary.get("avg_interaction_rate")))}</div></div>
  </div>

  <div class="card">
    <h2>一句话总结</h2>
    <p class="summary-lead">{esc(analysis["executive_summary"])}</p>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>👍 跑得好的原因</h2>
      <ul>{render_list(analysis["why_winners_won"])}</ul>
    </div>
    <div class="card">
      <h2>⚠️ 没跑起来的原因</h2>
      <ul>{render_list(analysis["why_losers_lost"])}</ul>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>✅ 下次继续用的套路</h2>
      <ul>{render_list(analysis["winning_patterns"] + analysis["hook_title_tips"][:2])}</ul>
    </div>
    <div class="card">
      <h2>🚫 下次要避开的坑</h2>
      <ul>{render_list(analysis["avoid_patterns"] + analysis["copywriting_tips"][:2])}</ul>
    </div>
  </div>

  <div class="card">
    <h2>🎬 下期可以直接开拍的选题</h2>
    <div class="topics">{topic_cards}</div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>🏆 红榜（播放最高的前几条）</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>短标题 / 顶部黄字</th><th>阶段</th><th class="num">播放</th><th class="num">赞/评</th><th class="num">转/藏</th><th class="num">互动率</th></tr></thead>
          <tbody>{rank_rows(summary["winners"])}</tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h2>📉 黑榜（播放垫底的几条）</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>短标题 / 顶部黄字</th><th>阶段</th><th class="num">播放</th><th class="num">赞/评</th><th class="num">转/藏</th><th class="num">互动率</th></tr></thead>
          <tbody>{rank_rows(summary["losers"])}</tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>📋 全部已发视频明细</h2>
    <div class="toolbar">
      <input id="searchBox" type="search" placeholder="输入标题或黄字关键词筛选…">
      <button id="sortTime" class="active" onclick="sortTable('time')">按发布时间</button>
      <button id="sortPlays" onclick="sortTable('plays')">按播放量从高到低</button>
    </div>
    <div class="table-wrap">
      <table id="allTable">
        <thead>
          <tr>
            <th>状态</th><th>短标题 / 发布时间</th><th>顶部黄字</th><th>时长</th><th>阶段</th>
            <th class="num">播放</th><th class="num">点赞</th><th class="num">评论</th>
            <th class="num">转发</th><th class="num">收藏</th><th class="num">互动率</th><th class="num">深度指标</th>
          </tr>
        </thead>
        <tbody>{''.join(all_rows) or '<tr><td colspan="12" class="empty">暂无记录</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2>💬 观众热门评论摘录</h2>
    {comments_html}
  </div>
</div>
<script>
const searchBox = document.getElementById("searchBox");
const tbody = document.querySelector("#allTable tbody");
searchBox.addEventListener("input", () => {{
  const q = searchBox.value.trim().toLowerCase();
  for (const tr of tbody.querySelectorAll("tr[data-search]")) {{
    tr.style.display = !q || tr.dataset.search.includes(q) ? "" : "none";
  }}
}});
function sortTable(mode) {{
  document.getElementById("sortTime").classList.toggle("active", mode === "time");
  document.getElementById("sortPlays").classList.toggle("active", mode === "plays");
  const rows = Array.from(tbody.querySelectorAll("tr[data-plays]"));
  rows.sort((a, b) => {{
    if (mode === "plays") return Number(b.dataset.plays) - Number(a.dataset.plays);
    return b.dataset.time.localeCompare(a.dataset.time);
  }});
  for (const r of rows) tbody.appendChild(r);
}}
</script>
</body>
</html>
"""


def _normalize_rule_entries(val: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(val, str):
        if val.strip():
            items.append({"text": val.strip(), "evidence_ids": []})
        return items
    if isinstance(val, list):
        for x in val:
            if isinstance(x, dict):
                txt = str(x.get("text") or "").strip()
                ev = [str(eid).strip() for eid in (x.get("evidence_ids") or []) if str(eid).strip()]
                if txt:
                    items.append({"text": txt, "evidence_ids": ev})
            elif str(x).strip():
                items.append({"text": str(x).strip(), "evidence_ids": []})
    return items


def _compact_history_video_for_ai(item: dict[str, Any], full_video: dict[str, Any] | None = None) -> dict[str, Any]:
    c = (full_video or {}).get("creative") or {}
    m = (full_video or {}).get("latest_metrics") or {}
    e = (full_video or {}).get("engagement") or {}
    rev = (full_video or {}).get("review") or {}
    transcript = str(c.get("transcript") or "")
    comments = (full_video or {}).get("comments") or []
    comp = item.get("completion_rate") if item.get("completion_rate") is not None else m.get("completion_rate")
    ir = item.get("interaction_rate") if item.get("interaction_rate") is not None else e.get("interaction_rate")
    fr = item.get("follow_rate") if item.get("follow_rate") is not None else e.get("follow_rate")
    win = (full_video or {}).get("window_metrics") or {}
    win_brief = {}
    for wk in ("7d", "30d", "custom"):
        if isinstance(win.get(wk), dict):
            wm = win[wk].get("metrics") or {}
            win_brief[wk] = {
                "完整周期": bool(win[wk].get("covers_full_lifetime")),
                "播放": wm.get("plays"),
                "完播率": f"{wm['completion_rate'] * 100:.1f}%" if wm.get("completion_rate") is not None else None,
            }
    return {
        "video_id": item["id"],
        "发布日期": item.get("publish_date"),
        "阶段": item.get("stage"),
        "短标题": item.get("publish_title"),
        "顶部白字": item.get("hook_context") or c.get("hook_context") or "",
        "顶部黄字": item.get("hook_title") or c.get("hook_title") or "",
        "成片秒数": item.get("output_duration_sec"),
        "播放量": item.get("plays"),
        "完播率": f"{comp * 100:.2f}%" if isinstance(comp, (int, float)) else None,
        "平均播放时长秒": item.get("avg_watch_sec"),
        "涨粉数": item.get("follows"),
        "涨粉率": f"{fr * 100:.2f}%" if isinstance(fr, (int, float)) else None,
        "推荐": item.get("recommends"),
        "喜欢": item.get("likes"),
        "评论": item.get("comments"),
        "分享": item.get("shares"),
        "收藏": item.get("saves"),
        "总互动率": f"{ir * 100:.2f}%" if isinstance(ir, (int, float)) else None,
        "数据口径": item.get("metrics_scope"),
        "用户标注": rev.get("user_mark") or item.get("user_mark") or "",
        "用户备注": rev.get("user_notes") or item.get("user_notes") or "",
        "开头前3秒": item.get("opening_3s") or c.get("opening_3s") or "",
        "口播节选": transcript[:450],
        "热门评论": [
            f"{cm.get('author', '观众')}: {cm.get('text', '')}"
            for cm in comments[:5]
            if isinstance(cm, dict) and cm.get("text")
        ],
        "窗口对照": win_brief or None,
    }


def _render_history_overview_markdown(
    summary: dict[str, Any],
    winners: list[dict[str, Any]],
    losers: list[dict[str, Any]],
    observing: list[dict[str, Any]],
    analysis: dict[str, Any],
) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    comp_str = _fmt_rate(summary.get("avg_completion_rate"))
    watch_str = f"{summary['avg_watch_sec']:.1f}秒" if summary.get("avg_watch_sec") is not None else "—"
    lines = [
        "# 微信视频号复盘总览（history 数据库版）",
        "",
        f"- 更新时间：{now_str}",
        f"- 有效作品：{summary['active_count']} 条（已排除测试/无效 {summary['excluded_count']} 条；成熟≥24h {summary['mature_count']} 条，观察<24h {summary['observing_count']} 条）",
        f"- 累计播放：{_fmt_num(summary['total_plays'])} ｜ 中位播放：{_fmt_num(summary['median_plays'])} ｜ 最高播放：{_fmt_num(summary['max_plays'])} ｜ 平均完播率：{comp_str} ｜ 均播时长：{watch_str} ｜ 累计涨粉：{_fmt_num(summary['total_follows'])}",
        "",
        "## 一句话总结",
        "",
        analysis["executive_summary"],
        "",
        "## 这段时间做得好的原因",
        "",
    ]
    for item in analysis["why_winners_won"]:
        lines.append(f"- {item}")
    lines += ["", "## 没跑起来 / 需要注意的问题", ""]
    for item in analysis["why_losers_lost"]:
        lines.append(f"- {item}")
    lines += ["", "## 写文案 & 起标题提醒", ""]
    for item in (analysis.get("copywriting_tips") or []) + (analysis.get("hook_title_tips") or []):
        txt = item["text"] if isinstance(item, dict) else str(item)
        lines.append(f"- {txt}")
    lines += ["", "## 下期可以直接开拍的选题", ""]
    for idx, t in enumerate(analysis["next_topics"], 1):
        hook = f"（建议黄字：{t['hook_title']}）" if t.get("hook_title") else ""
        angle = f" — {t['angle']}" if t.get("angle") else ""
        lines.append(f"{idx}. **{t.get('title', '')}**{hook}{angle}")

    def _add_table(heading: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        lines.extend([
            "",
            f"## {heading}",
            "",
            "| 日期 | 短标题 | 顶部白字/黄字 | 阶段 | 播放 | 完播率 | 均播 | 涨粉 | 互动率 |",
            "|---|---|---|---|---|---|---|---|---|",
        ])
        for r in rows:
            hy = " / ".join(x for x in (r.get("hook_context"), r.get("hook_title")) if x) or "—"
            watch = f"{r['avg_watch_sec']:.1f}s" if r.get("avg_watch_sec") is not None else "—"
            lines.append(
                f"| {r.get('publish_date') or '—'} | {r.get('publish_title') or r['id']} | {hy} | {r.get('stage')} | "
                f"{_fmt_num(r.get('plays'))} | {_fmt_rate(r.get('completion_rate'))} | {watch} | "
                f"{_fmt_num(r.get('follows'))} | {_fmt_rate(r.get('interaction_rate'))} |"
            )

    _add_table("红榜（成熟作品表现 Top）", winners)
    _add_table("黑榜（成熟作品偏低，不含刚发<24h）", losers)
    _add_table("观察期（<24h 新发视频，暂不判死刑）", observing[:8])
    lines += ["", "---", "", "> 策略记忆已合并写入 `history/playbook.json`，已手动划掉的规则不会被恢复。", ""]
    return "\n".join(lines)


def _local_history_fallback_analysis(
    summary: dict[str, Any],
    winners: list[dict[str, Any]],
    losers: list[dict[str, Any]],
) -> dict[str, Any]:
    top = winners[0] if winners else None
    comp_txt = _fmt_rate(summary.get("avg_completion_rate"))
    top_txt = f"最高一条《{top['publish_title']}》播放 {_fmt_num(top['plays'])}、完播率 {_fmt_rate(top.get('completion_rate'))}、涨粉 {_fmt_num(top.get('follows'))}。" if top else ""
    return {
        "source": "local",
        "executive_summary": (
            f"当前有效作品 {summary['active_count']} 条（成熟≥24h {summary['mature_count']} 条，观察<24h {summary['observing_count']} 条），"
            f"累计播放 {_fmt_num(summary['total_plays'])}，中位播放 {_fmt_num(summary['median_plays'])}，平均完播率 {comp_txt}，累计涨粉 {_fmt_num(summary['total_follows'])}。{top_txt}"
        ),
        "why_winners_won": [
            f"《{r['publish_title']}》播放 {_fmt_num(r.get('plays'))}（完播率 {_fmt_rate(r.get('completion_rate'))}，涨粉 {_fmt_num(r.get('follows'))}），可对照其开头前3秒与白黄字结构复用。"
            for r in winners[:3]
        ] or ["待积累更多成熟样本后对比总结。"],
        "why_losers_lost": [
            f"《{r['publish_title']}》成熟期播放 {_fmt_num(r.get('plays'))}（完播率 {_fmt_rate(r.get('completion_rate'))}），可重点复查开头前3秒是否慢热或黄字是否缺少具体疑问。"
            for r in losers[:3]
        ] or ["暂无成熟期落后样本。"],
        "next_topics": [
            {
                "title": f"围绕《{top['publish_title']}》同类角度继续深挖一个具体问题" if top else "挑一条最近高关注话题做拆解",
                "hook_title": "这波到底该怎么看",
                "angle": "前3秒直接亮冲突或算账结果，白字交代具体数字或处境。",
            }
        ],
        "winning_patterns": [{"text": "前3秒直接亮观点/算账，用白字交代处境、黄字抛出具体疑问", "evidence_ids": [r["id"] for r in winners[:2]]}],
        "avoid_patterns": [{"text": "不要把刚发不到24小时的视频过早定性；避免开头铺垫过长拉低完播率", "evidence_ids": [r["id"] for r in losers[:2]]}],
        "hook_title_tips": [{"text": "白字摆出人/钱/动作，黄字问代价或反差，两行无标点且不剧透答案", "evidence_ids": []}],
        "copywriting_tips": [{"text": "短标题16字内给锋利观点，口播前50字先抛出冲突或反常识结论", "evidence_ids": []}],
        "glossary_suggestions": [],
    }


def analyze_history(
    history_dir: Path,
    *,
    use_ai: bool = True,
    min_mature_hours: float = 24.0,
) -> dict[str, Any]:
    hdb = _import_history_db()
    hdir = Path(history_dir)
    index = hdb.rebuild_index(hdir)
    all_videos = {v["id"]: v for v in hdb.load_all_videos(hdir)}

    active_items = [x for x in (index.get("videos") or []) if x.get("status") != "excluded"]
    collected_items = [x for x in active_items if x.get("plays") is not None]
    mature_items = [
        x for x in collected_items
        if x.get("age_hours") is None or x.get("age_hours") >= min_mature_hours
    ]
    observing_items = [
        x for x in collected_items
        if x.get("age_hours") is not None and x.get("age_hours") < min_mature_hours
    ]

    pool = mature_items if mature_items else collected_items
    winners = sorted(
        pool,
        key=lambda r: (
            -(r.get("plays") or 0),
            -(r.get("completion_rate") if r.get("completion_rate") is not None else -1.0),
            -(r.get("interaction_rate") if r.get("interaction_rate") is not None else -1.0),
        ),
    )[:5]
    winner_ids = {r["id"] for r in winners}
    losers = sorted(
        [r for r in pool if r["id"] not in winner_ids],
        key=lambda r: (
            r.get("plays") or 0,
            r.get("completion_rate") if r.get("completion_rate") is not None else 999.0,
            r.get("interaction_rate") if r.get("interaction_rate") is not None else 999.0,
        ),
    )[:5]

    stats = index.get("stats_summary") or {}
    summary = {
        "total_videos": index.get("total_videos", 0),
        "active_count": len(active_items),
        "excluded_count": stats.get("excluded_count", 0),
        "mature_count": len(mature_items),
        "observing_count": len(observing_items),
        "collected_count": len(collected_items),
        "total_plays": stats.get("total_plays", 0),
        "avg_plays": stats.get("avg_plays", 0.0),
        "median_plays": stats.get("median_plays", 0.0),
        "max_plays": stats.get("max_plays", 0),
        "avg_completion_rate": stats.get("avg_completion_rate"),
        "avg_watch_sec": stats.get("avg_watch_sec"),
        "avg_interaction_rate": stats.get("avg_interaction_rate"),
        "total_follows": stats.get("total_follows", 0),
    }

    analysis_raw: dict[str, Any] | None = None
    if use_ai and collected_items:
        used_ids = {r["id"] for r in winners} | {r["id"] for r in losers}
        standouts = sorted(
            [r for r in pool if r["id"] not in used_ids and (r.get("follows") or 0) > 0],
            key=lambda r: (-(r.get("follows") or 0), -(r.get("completion_rate") or 0.0)),
        )[:5]
        used_ids |= {r["id"] for r in standouts}
        others = [r for r in active_items if r["id"] not in used_ids][:10]

        payload_data = {
            "总盘概览": {
                "有效作品数": summary["active_count"],
                "已排除测试数": summary["excluded_count"],
                "成熟作品数(≥24h)": summary["mature_count"],
                "观察期作品数(<24h)": summary["observing_count"],
                "累计播放": summary["total_plays"],
                "中位播放": summary["median_plays"],
                "最高播放": summary["max_plays"],
                "平均完播率": _fmt_rate(summary.get("avg_completion_rate")),
                "平均播放秒数": summary.get("avg_watch_sec"),
                "累计涨粉": summary["total_follows"],
            },
            "红榜(成熟Top5)": [_compact_history_video_for_ai(r, all_videos.get(r["id"])) for r in winners],
            "黑榜(成熟偏低Bottom5_不含刚发<24h)": [_compact_history_video_for_ai(r, all_videos.get(r["id"])) for r in losers],
            "高涨粉/高完播样本": [_compact_history_video_for_ai(r, all_videos.get(r["id"])) for r in standouts],
            "观察期新发(<24h_勿与成熟榜混比)": [_compact_history_video_for_ai(r, all_videos.get(r["id"])) for r in observing_items[:6]],
            "其余近期样本": [_compact_history_video_for_ai(r, all_videos.get(r["id"])) for r in others],
        }
        prompt = (
            "你是一位帮视频号创作者做深度数据复盘与文案把关的教练。不要讲空泛套话，只基于下面的真实数据（含完播率、均播时长、涨粉、播放、白字/黄字、开头前3秒、完整口播节选、评论和用户备注）做诊断。\n"
            "特别纪律：观察期(<24h)的新发视频还在跑流量，绝对不要拿来当作黑榜失败案例；分析红黑榜时必须对比【成熟作品】，同时把「完播率」「涨粉」「开头前3秒」「白黄字配合」纳入归因。\n"
            "只输出一个 JSON 对象，不要任何解释或代码块标记，字段如下：\n"
            "{\n"
            '  "executive_summary": "3到4句大白话总评：这段时间总体盘面、最大发现、最关键的涨粉/完播结论",\n'
            '  "why_winners_won": ["结合具体短标题与真实指标（播放/完播率/涨粉）说明为什么跑得好"],\n'
            '  "why_losers_lost": ["结合具体成熟期作品和真实指标说明问题出在选题、白黄字、开头还是口播节奏"],\n'
            '  "next_topics": [{"title": "下期可直接开拍的选题", "hook_title": "一行无标点黄字疑问", "hook_context": "可选上方白字铺垫", "angle": "开头3秒怎么讲、情绪和落脚点"}],\n'
            '  "winning_patterns": [{"text": "可复用的有效套路", "evidence_ids": ["对应video_id"]}],\n'
            '  "avoid_patterns": [{"text": "明确的踩坑点/红线", "evidence_ids": ["对应video_id"]}],\n'
            '  "hook_title_tips": [{"text": "白字与黄字写法建议", "evidence_ids": []}],\n'
            '  "copywriting_tips": [{"text": "写口播文案、开头前3秒与短标题的注意事项", "evidence_ids": []}],\n'
            '  "glossary_suggestions": [{"wrong": "常见误识词", "right": "正确专名"}]\n'
            "}\n"
            "next_topics 给出 3 到 5 个选题。\n\n"
            "真实数据库如下：\n"
            + json.dumps(payload_data, ensure_ascii=False, indent=2)
        )
        for caller in (_call_claude_cli, _call_deepseek_flash):
            try:
                analysis_raw = caller(prompt)
                break
            except Exception:
                continue

    fallback = _local_history_fallback_analysis(summary, winners, losers)
    if analysis_raw:
        def _str_list(v: Any) -> list[str]:
            if isinstance(v, str):
                return [v.strip()] if v.strip() else []
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return []

        topics = []
        for item in analysis_raw.get("next_topics") or []:
            if isinstance(item, dict):
                t = str(item.get("title") or item.get("topic") or "").strip()
                if t:
                    topics.append({
                        "title": t,
                        "hook_title": str(item.get("hook_title") or "").strip(),
                        "hook_context": str(item.get("hook_context") or "").strip(),
                        "angle": str(item.get("angle") or "").strip(),
                    })
        glossary = []
        for item in analysis_raw.get("glossary_suggestions") or []:
            if isinstance(item, dict) and str(item.get("right") or "").strip():
                glossary.append({"wrong": str(item.get("wrong") or "").strip(), "right": str(item.get("right") or "").strip()})

        analysis = {
            "source": str(analysis_raw.get("source") or "ai"),
            "executive_summary": str(analysis_raw.get("executive_summary") or fallback["executive_summary"]).strip(),
            "why_winners_won": _str_list(analysis_raw.get("why_winners_won")) or fallback["why_winners_won"],
            "why_losers_lost": _str_list(analysis_raw.get("why_losers_lost")) or fallback["why_losers_lost"],
            "next_topics": topics or fallback["next_topics"],
            "winning_patterns": _normalize_rule_entries(analysis_raw.get("winning_patterns")) or fallback["winning_patterns"],
            "avoid_patterns": _normalize_rule_entries(analysis_raw.get("avoid_patterns")) or fallback["avoid_patterns"],
            "hook_title_tips": _normalize_rule_entries(analysis_raw.get("hook_title_tips")) or fallback["hook_title_tips"],
            "copywriting_tips": _normalize_rule_entries(analysis_raw.get("copywriting_tips")) or fallback["copywriting_tips"],
            "glossary_suggestions": glossary,
        }
    else:
        analysis = fallback

    top_examples = [
        {
            "video_id": r["id"],
            "title": r.get("publish_title") or "",
            "hook_title": r.get("hook_title") or "",
            "plays": r.get("plays"),
            "completion_rate": r.get("completion_rate"),
            "follows": r.get("follows"),
            "interaction_rate": r.get("interaction_rate"),
        }
        for r in winners[:3]
    ]

    playbook = hdb.merge_ai_into_playbook(
        hdir,
        analysis,
        source_videos_count=summary["active_count"],
        collected_count=summary["collected_count"],
        top_examples=top_examples,
    )

    today_str = datetime.now(hdb.SHANGHAI_TZ).strftime("%Y-%m-%d")
    review_doc = {
        "date": today_str,
        "generated_at": datetime.now(hdb.SHANGHAI_TZ).isoformat(),
        "summary": summary,
        "winners_ids": [r["id"] for r in winners],
        "losers_ids": [r["id"] for r in losers],
        "observing_ids": [r["id"] for r in observing_items],
        "analysis": analysis,
    }
    review_path = hdir / "reviews" / f"{today_str}.json"
    _atomic_write_json(review_path, review_doc)
    overview_md_path = hdir / "复盘总览.md"
    _atomic_write_text(
        overview_md_path,
        _render_history_overview_markdown(summary, winners, losers, observing_items, analysis),
    )

    return {
        "summary": summary,
        "analysis": analysis,
        "playbook": playbook,
        "playbook_path": hdir / "playbook.json",
        "review_path": review_path,
        "overview_md_path": overview_md_path,
    }


def analyze_and_write(runs_dir: Path, *, use_ai: bool = True, open_html: bool = False) -> dict[str, Any]:
    runs_dir = Path(runs_dir)
    runs = load_published_runs(runs_dir)
    summary = summarize_runs(runs)
    analysis = analyze_with_ai(summary, runs, use_ai=use_ai)

    playbook_path = save_playbook(runs_dir, summary, analysis)
    overview_md_path = runs_dir / "复盘总览.md"
    dashboard_html_path = runs_dir / "复盘看板.html"
    _atomic_write_text(overview_md_path, render_overview_markdown(summary, runs, analysis))
    _atomic_write_text(dashboard_html_path, render_dashboard_html(summary, runs, analysis))

    if open_html and os.name == "nt":
        os.startfile(str(dashboard_html_path))

    return {
        "runs": runs,
        "summary": summary,
        "analysis": analysis,
        "playbook_path": playbook_path,
        "overview_md_path": overview_md_path,
        "dashboard_html_path": dashboard_html_path,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    default_runs = Path(__file__).resolve().parents[1] / "publish-runs"
    parser.add_argument("--runs-dir", type=Path, default=default_runs, help="publish-runs 目录路径")
    parser.add_argument("--history-dir", type=Path, default=None, help="history 数据库目录路径（传入时对 history/ 运行复盘）")
    parser.add_argument("--no-ai", action="store_true", help="离线汇总，不调用 AI")
    parser.add_argument("--open-html", action="store_true", help="完成后打开复盘看板 HTML")
    parser.add_argument("--print-prompt", choices=("hook", "copy"), help="仅输出账号经验小本本提示段")
    args = parser.parse_args(argv)

    if args.print_prompt:
        if args.history_dir is not None:
            text = _import_history_db().format_playbook_prompt(args.history_dir, mode=args.print_prompt)
        else:
            text = load_playbook_prompt(args.runs_dir, mode=args.print_prompt)
        if text:
            print(text)
        return 0

    if args.history_dir is not None:
        hres = analyze_history(args.history_dir, use_ai=not args.no_ai)
        s = hres["summary"]
        print(
            f"history 复盘完成：有效作品 {s['active_count']} 条（成熟 {s['mature_count']} 条），已抓回数据 {s['collected_count']} 条。"
        )
        print(f"复盘报告：{hres['review_path']}")
        print(f"复盘总览：{hres['overview_md_path']}")
        print(f"策略记忆：{hres['playbook_path']}")
        return 0

    result = analyze_and_write(args.runs_dir, use_ai=not args.no_ai, open_html=args.open_html)
    summary = result["summary"]
    print(
        f"复盘分析完成：共 {summary['total_published']} 条已发布，已抓回数据 {summary['collected_count']} 条。"
    )
    print(f"复盘看板：{result['dashboard_html_path']}")
    print(f"复盘总览：{result['overview_md_path']}")
    print(f"账号经验：{result['playbook_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
