"""微信视频号复盘数据库引擎（history/）：统一管理历史视频档案、官方 CSV 导入、窗口保护与策略记忆。"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import sys
import tempfile
from typing import Any
import unicodedata


SHANGHAI_TZ = timezone(timedelta(hours=8))
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 2

METRIC_KEYS = (
    "plays",
    "completion_rate",
    "avg_watch_sec",
    "recommends",
    "likes",
    "comments",
    "shares",
    "follows",
    "saves",
    "forward_chat_moments",
    "set_ringtone",
    "set_status",
    "set_moments_cover",
    "wecom_clicks",
    "wecom_click_users",
    "wecom_adds",
    "wecom_add_users",
)

STAGES_ORDER = ("<24h", "1天", "3天", "7天+", "未知")


def default_history_dir(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else DEFAULT_ROOT
    return base / "history"


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


def _read_text_auto(path: Path) -> str:
    raw_bytes = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI_TZ) if value.tzinfo else value.replace(tzinfo=SHANGHAI_TZ)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        return dt.astimezone(SHANGHAI_TZ)
    except ValueError:
        pass
    m_cn = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", text)
    if m_cn:
        y, m, d = int(m_cn.group(1)), int(m_cn.group(2)), int(m_cn.group(3))
        hh = int(m_cn.group(4) or 0)
        mm = int(m_cn.group(5) or 0)
        ss = int(m_cn.group(6) or 0)
        return datetime(y, m, d, hh, mm, ss, tzinfo=SHANGHAI_TZ)
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI_TZ)
        except ValueError:
            continue
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    dt = _parse_datetime(value)
    return dt.date() if dt else None


def _parse_run_dir_time(dir_name: str) -> datetime | None:
    m = re.match(r"^(\d{8})-(\d{6})(?:-\d+)?_", dir_name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI_TZ)
    except ValueError:
        return None


def parse_percent(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return round(v / 100.0 if v > 1.0 else v, 6)
    raw = str(value).strip().replace(",", "")
    if not raw or raw in ("-", "—", "--", "N/A"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%?", raw)
    if not m:
        return None
    return round(float(m.group(1)) / 100.0, 6)


def parse_duration_sec(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    raw = str(value).strip()
    if not raw or raw in ("-", "—", "--", "N/A"):
        return None
    m_min_sec = re.fullmatch(r"(\d+)\s*分\s*(\d+(?:\.\d+)?)\s*秒?", raw)
    if m_min_sec:
        return round(int(m_min_sec.group(1)) * 60 + float(m_min_sec.group(2)), 2)
    m_colon = re.fullmatch(r"(\d+):(\d{1,2}(?:\.\d+)?)", raw)
    if m_colon:
        return round(int(m_colon.group(1)) * 60 + float(m_colon.group(2)), 2)
    m_sec = re.fullmatch(r"(\d+(?:\.\d+)?)\s*秒?", raw)
    if m_sec:
        return round(float(m_sec.group(1)), 2)
    return None


def parse_int_metric(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    raw = re.sub(r"[,，\s]", "", str(value))
    if not raw or raw in ("-", "—", "--", "N/A"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(万|亿)?", raw)
    if not m:
        return None
    mult = {"万": 10_000, "亿": 100_000_000}.get(m.group(2), 1)
    return int(round(float(m.group(1)) * mult))


def split_desc_and_tags(raw_desc: str) -> tuple[str, list[str]]:
    text = re.sub(r"<[^>]+>", " ", str(raw_desc or ""))
    tags: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"#([^\s#]+)", text):
        tag = m.group(1).strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    body = re.sub(r"#[^\s#]+", " ", text)
    body = re.sub(r"[ \t\r\n]+", " ", body).strip()
    return body, tags


def normalize_match_text(text: str) -> str:
    body, _ = split_desc_and_tags(text)
    norm = unicodedata.normalize("NFKC", body).casefold()
    return "".join(ch for ch in norm if ch.isalnum())


def extract_opening_3s(transcript: str, max_chars: int = 50) -> str:
    cleaned = re.sub(r"\s+", " ", str(transcript or "")).strip().lstrip("，。！？；、,.!?;\n\t").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "…"


def stage_from_hours(age_hours: float | None) -> str:
    if age_hours is None:
        return "未知"
    if age_hours >= 168:
        return "7天+"
    if age_hours >= 72:
        return "3天"
    if age_hours >= 24:
        return "1天"
    return "<24h"


def _compute_age_hours(published_at: str | None, publish_date: str | None, at_time: datetime) -> float | None:
    dt = _parse_datetime(published_at)
    if dt is None and publish_date:
        d = _parse_date(publish_date)
        if d:
            dt = datetime(d.year, d.month, d.day, 12, 0, tzinfo=SHANGHAI_TZ)
    if dt is None:
        return None
    return round(max(0.0, (at_time.astimezone(SHANGHAI_TZ) - dt).total_seconds() / 3600.0), 2)


def _empty_metrics() -> dict[str, Any]:
    return {k: None for k in METRIC_KEYS}


def compute_engagement(metrics: dict[str, Any], output_duration_sec: float | None = None) -> dict[str, Any]:
    parts = [metrics[k] for k in ("likes", "comments", "shares", "saves") if metrics.get(k) is not None]
    interactions = sum(parts) if parts else None
    plays = metrics.get("plays")

    def rate(val: int | float | None) -> float | None:
        if plays is None or plays <= 0 or val is None:
            return None
        return round(float(val) / float(plays), 4)

    avg_watch = metrics.get("avg_watch_sec")
    watch_ratio = None
    if avg_watch is not None and output_duration_sec and output_duration_sec > 0:
        watch_ratio = round(float(avg_watch) / float(output_duration_sec), 4)

    return {
        "interactions": interactions,
        "interaction_rate": rate(interactions),
        "like_rate": rate(metrics.get("likes")),
        "recommend_rate": rate(metrics.get("recommends")),
        "comment_rate": rate(metrics.get("comments")),
        "share_rate": rate(metrics.get("shares")),
        "save_rate": rate(metrics.get("saves")),
        "follow_rate": rate(metrics.get("follows")),
        "watch_ratio": watch_ratio,
    }


def _short_hash(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]


def _make_video_id(publish_date: str | None, seed: str) -> str:
    date_part = (publish_date or "00000000").replace("-", "")
    if len(date_part) != 8 or not date_part.isdigit():
        date_part = "00000000"
    return f"v_{date_part}_{_short_hash(seed)}"


def _ensure_history_layout(history_dir: Path) -> None:
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / "videos").mkdir(parents=True, exist_ok=True)
    (history_dir / "reviews").mkdir(parents=True, exist_ok=True)
    (history_dir / "raw-csv").mkdir(parents=True, exist_ok=True)


def _new_video_record(
    *,
    video_id: str,
    platform_id: str | None = None,
    published_at: str | None = None,
    publish_date: str | None = None,
    publish_time_exact: bool = False,
) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "id": video_id,
        "platform_id": platform_id or None,
        "run_names": [],
        "published_at": published_at,
        "publish_date": publish_date,
        "publish_time_exact": publish_time_exact,
        "creative": {
            "publish_title": "",
            "hook_context": "",
            "hook_title": "",
            "hook_summary": "",
            "publish_desc": "",
            "publish_tags": [],
            "transcript": "",
            "opening_3s": "",
            "source_duration_sec": None,
            "output_duration_sec": None,
        },
        "latest_metrics": _empty_metrics(),
        "engagement": compute_engagement(_empty_metrics()),
        "metrics_scope": "pending",
        "metrics_source": "retro",
        "metrics_updated_at": None,
        "window_metrics": {"7d": None, "30d": None, "custom": None},
        "snapshots": [],
        "comments": [],
        "review": {
            "status": "active",
            "user_mark": "",
            "topic_tags": [],
            "style_tags": [],
            "user_notes": "",
            "ai_diagnosis": "",
        },
    }


def _video_path(history_dir: Path, video_id: str) -> Path:
    return history_dir / "videos" / f"{video_id}.json"


def load_all_videos(history_dir: Path) -> list[dict[str, Any]]:
    videos_dir = Path(history_dir) / "videos"
    if not videos_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(videos_dir.glob("*.json")):
        try:
            data = _read_json(p)
            if isinstance(data, dict) and data.get("id"):
                out.append(data)
        except (OSError, ValueError):
            continue
    return out


def load_video(history_dir: Path, video_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", video_id or ""):
        return None
    p = _video_path(Path(history_dir), video_id)
    if not p.is_file():
        return None
    try:
        data = _read_json(p)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_video(history_dir: Path, video: dict[str, Any]) -> Path:
    _ensure_history_layout(Path(history_dir))
    path = _video_path(Path(history_dir), video["id"])
    _atomic_write_json(path, video)
    return path


def _snap_sort_key(snap: dict[str, Any]) -> tuple[str, int]:
    ts = str(snap.get("collected_at") or "")
    plays = snap.get("metrics", {}).get("plays")
    return (ts, int(plays) if isinstance(plays, (int, float)) else -1)


def _snapshot_signature(snap: dict[str, Any]) -> tuple[Any, ...]:
    m = snap.get("metrics") or {}
    return (
        snap.get("source"),
        snap.get("window_type"),
        snap.get("window_start"),
        snap.get("window_end"),
        m.get("plays"),
        m.get("completion_rate"),
        m.get("avg_watch_sec"),
        m.get("recommends"),
        m.get("likes"),
        m.get("comments"),
        m.get("shares"),
        m.get("follows"),
        m.get("saves"),
    )


def _append_snapshot(video: dict[str, Any], snap: dict[str, Any]) -> bool:
    snaps = video.setdefault("snapshots", [])
    sig = _snapshot_signature(snap)
    snap_dt = _parse_datetime(snap.get("collected_at"))
    for existing in snaps:
        if _snapshot_signature(existing) != sig:
            continue
        if snap.get("import_id") and existing.get("import_id") == snap.get("import_id"):
            return False
        ex_dt = _parse_datetime(existing.get("collected_at"))
        if snap_dt and ex_dt and abs((snap_dt - ex_dt).total_seconds()) <= 120:
            return False
    snaps.append(snap)
    snaps.sort(key=lambda s: str(s.get("collected_at") or ""))
    return True


def recompute_latest_metrics(video: dict[str, Any]) -> None:
    snaps = [s for s in (video.get("snapshots") or []) if isinstance(s, dict)]
    csv_lifetime = [s for s in snaps if s.get("source") == "csv" and s.get("covers_full_lifetime")]
    ego_snaps = [s for s in snaps if s.get("source") == "ego"]
    partial_csvs = [s for s in snaps if s.get("source") == "csv" and not s.get("covers_full_lifetime")]

    latest_saves = None
    latest_ego_with_saves = sorted(
        [s for s in ego_snaps if (s.get("metrics") or {}).get("saves") is not None],
        key=_snap_sort_key,
    )
    if latest_ego_with_saves:
        latest_saves = latest_ego_with_saves[-1]["metrics"]["saves"]

    merged = _empty_metrics()
    scope = "pending"
    source = "retro"
    updated_at = None

    if csv_lifetime:
        best_csv = sorted(csv_lifetime, key=_snap_sort_key)[-1]
        merged.update(best_csv.get("metrics") or {})
        if latest_saves is not None:
            merged["saves"] = latest_saves
        updated_at = best_csv.get("collected_at")
        source = "csv+ego" if ego_snaps else "csv"
        scope = "lifetime"

        if ego_snaps:
            best_ego = sorted(ego_snaps, key=_snap_sort_key)[-1]
            ego_dt = _parse_datetime(best_ego.get("collected_at"))
            csv_dt = _parse_datetime(best_csv.get("collected_at"))
            ego_plays = (best_ego.get("metrics") or {}).get("plays")
            csv_plays = merged.get("plays")
            if ego_dt and csv_dt and ego_dt > csv_dt and ego_plays is not None and (csv_plays is None or ego_plays > csv_plays):
                for key in ("plays", "likes", "comments", "shares", "saves"):
                    val = (best_ego.get("metrics") or {}).get(key)
                    if val is not None:
                        merged[key] = val
                updated_at = best_ego.get("collected_at")
                source = "ego+csv"
    elif ego_snaps:
        best_ego = sorted(ego_snaps, key=_snap_sort_key)[-1]
        for k in METRIC_KEYS:
            val = (best_ego.get("metrics") or {}).get(k)
            if val is not None:
                merged[k] = val
        scope = "lifetime"
        source = "ego"
        updated_at = best_ego.get("collected_at")
    elif partial_csvs:
        best_partial = max(
            partial_csvs,
            key=lambda s: (
                (s.get("metrics") or {}).get("plays") if (s.get("metrics") or {}).get("plays") is not None else -1,
                str(s.get("collected_at") or ""),
            ),
        )
        merged.update(best_partial.get("metrics") or {})
        scope = f"window:{best_partial.get('window_type') or 'custom'}"
        source = "csv"
        updated_at = best_partial.get("collected_at")

    video["latest_metrics"] = merged
    out_dur = (video.get("creative") or {}).get("output_duration_sec")
    video["engagement"] = compute_engagement(merged, output_duration_sec=out_dur)
    video["metrics_scope"] = scope
    video["metrics_source"] = source
    video["metrics_updated_at"] = updated_at


def _find_matching_video(
    videos: list[dict[str, Any]],
    *,
    platform_id: str | None = None,
    run_name: str | None = None,
    desc_text: str | None = None,
    title_text: str | None = None,
    pub_date_str: str | None = None,
) -> dict[str, Any] | None:
    if platform_id:
        for v in videos:
            if v.get("platform_id") == platform_id:
                return v
    if run_name:
        for v in videos:
            if run_name in (v.get("run_names") or []):
                return v

    target_date = _parse_date(pub_date_str)

    def _date_close(v: dict[str, Any], max_days: int = 1) -> bool:
        if not target_date:
            return True
        vd = _parse_date(v.get("publish_date") or v.get("published_at"))
        if not vd:
            return True
        return abs((target_date - vd).days) <= max_days

    norm_desc = normalize_match_text(desc_text or "")
    if norm_desc:
        exact_hits = [
            v for v in videos
            if _date_close(v, 1) and normalize_match_text((v.get("creative") or {}).get("publish_desc") or "") == norm_desc
        ]
        if len(exact_hits) == 1:
            return exact_hits[0]
        if len(norm_desc) >= 24:
            prefix_hits = []
            for v in videos:
                if not _date_close(v, 1):
                    continue
                vd = normalize_match_text((v.get("creative") or {}).get("publish_desc") or "")
                if vd and len(vd) >= 24 and (vd.startswith(norm_desc[:24]) or norm_desc.startswith(vd[:24])):
                    prefix_hits.append(v)
            if len(prefix_hits) == 1:
                return prefix_hits[0]

    norm_title = normalize_match_text(title_text or "")
    if norm_title:
        title_hits = [
            v for v in videos
            if _date_close(v, 1) and normalize_match_text((v.get("creative") or {}).get("publish_title") or "") == norm_title
        ]
        if len(title_hits) == 1:
            return title_hits[0]
    return None


def _date_bounds_for_window(
    window_type: str,
    window_start: str | None,
    window_end: str | None,
    pub_dates: list[date],
    collected_dt: datetime,
) -> tuple[str, date, date]:
    end_d = _parse_date(window_end) or (max(pub_dates) if pub_dates else collected_dt.date())
    start_d = _parse_date(window_start)
    wtype = (window_type or "auto").strip().lower()
    if wtype not in ("auto", "7d", "30d", "custom"):
        raise ValueError(f"不支持的统计窗口：{window_type}")

    if wtype == "auto":
        min_d = min(pub_dates) if pub_dates else end_d
        span = (end_d - min_d).days
        if span <= 7:
            wtype = "7d"
        elif span <= 31:
            wtype = "30d"
        else:
            wtype = "custom"

    if start_d is None:
        if wtype == "7d":
            start_d = end_d - timedelta(days=6)
        elif wtype == "30d":
            start_d = end_d - timedelta(days=29)
        else:
            start_d = min(pub_dates) if pub_dates else end_d

    return wtype, start_d, end_d


def _read_csv_rows(csv_text: str) -> list[dict[str, str]]:
    text = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "视频ID" not in reader.fieldnames or "播放量" not in reader.fieldnames:
        raise ValueError("CSV 缺少必要列（需包含 视频描述、视频ID、发布时间、播放量 等官方导出字段）")
    return [dict(row) for row in reader]


def import_csv(
    csv_source: Path | str,
    *,
    history_dir: Path | None = None,
    window_type: str = "auto",
    window_start: str | None = None,
    window_end: str | None = None,
    collected_at: str | None = None,
    csv_text: str | None = None,
    filename: str | None = None,
    save_raw: bool = True,
) -> dict[str, Any]:
    hdir = Path(history_dir) if history_dir is not None else default_history_dir()
    _ensure_history_layout(hdir)

    source_label = filename or (str(csv_source) if csv_source else "upload.csv")
    if csv_text is None:
        src_path = Path(csv_source)
        if not src_path.is_file():
            raise FileNotFoundError(f"找不到 CSV 文件：{src_path}")
        raw_text = _read_text_auto(src_path)
        source_label = src_path.name
        mtime_dt = datetime.fromtimestamp(src_path.stat().st_mtime, tz=SHANGHAI_TZ)
    else:
        raw_text = csv_text
        mtime_dt = _now_shanghai()

    collected_dt = _parse_datetime(collected_at) or mtime_dt
    collected_iso = collected_dt.isoformat()
    rows = _read_csv_rows(raw_text)

    pub_dates: list[date] = []
    for r in rows:
        d = _parse_date(r.get("发布时间"))
        if d:
            pub_dates.append(d)

    resolved_window, start_d, end_d = _date_bounds_for_window(
        window_type, window_start, window_end, pub_dates, collected_dt
    )
    start_iso = start_d.isoformat()
    end_iso = end_d.isoformat()

    import_id = f"imp_{collected_dt.strftime('%Y%m%d_%H%M%S')}_{_short_hash(source_label + collected_iso)}"
    archived_rel = None
    if save_raw:
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", Path(source_label).name or "export.csv")
        archived_path = hdir / "raw-csv" / f"{collected_dt.strftime('%Y%m%d-%H%M%S')}_{resolved_window}_{safe_name}"
        _atomic_write_text(archived_path, raw_text)
        archived_rel = archived_path.relative_to(hdir).as_posix()

    videos = load_all_videos(hdir)
    matched_count = 0
    new_count = 0
    updated_lifetime_count = 0
    window_only_count = 0
    changed_videos: list[dict[str, Any]] = []

    for row in rows:
        pid = str(row.get("视频ID") or "").strip()
        raw_desc = str(row.get("视频描述") or "")
        desc_body, tags = split_desc_and_tags(raw_desc)
        pub_d = _parse_date(row.get("发布时间"))
        pub_d_str = pub_d.isoformat() if pub_d else None
        if not pid and not desc_body:
            continue

        video = _find_matching_video(
            videos,
            platform_id=pid or None,
            desc_text=desc_body,
            pub_date_str=pub_d_str,
        )
        if video is None:
            seed = pid or f"{pub_d_str}:{normalize_match_text(desc_body)}"
            vid = _make_video_id(pub_d_str, seed)
            pub_iso = f"{pub_d_str}T00:00:00+08:00" if pub_d_str else None
            video = _new_video_record(
                video_id=vid,
                platform_id=pid or None,
                published_at=pub_iso,
                publish_date=pub_d_str,
                publish_time_exact=False,
            )
            videos.append(video)
            new_count += 1
        else:
            matched_count += 1
            if pid and not video.get("platform_id"):
                video["platform_id"] = pid
            if pub_d_str and not video.get("publish_date"):
                video["publish_date"] = pub_d_str
            if pub_d_str and not video.get("published_at"):
                video["published_at"] = f"{pub_d_str}T00:00:00+08:00"

        creative = video["creative"]
        if desc_body and not creative.get("publish_desc"):
            creative["publish_desc"] = desc_body
        if tags and not creative.get("publish_tags"):
            creative["publish_tags"] = tags

        row_metrics = {
            "plays": parse_int_metric(row.get("播放量")),
            "completion_rate": parse_percent(row.get("完播率")),
            "avg_watch_sec": parse_duration_sec(row.get("平均播放时长")),
            "recommends": parse_int_metric(row.get("推荐")),
            "likes": parse_int_metric(row.get("喜欢")),
            "comments": parse_int_metric(row.get("评论量")),
            "shares": parse_int_metric(row.get("分享量")),
            "follows": parse_int_metric(row.get("关注量")),
            "saves": None,
            "forward_chat_moments": parse_int_metric(row.get("转发聊天和朋友圈")),
            "set_ringtone": parse_int_metric(row.get("设为铃声")),
            "set_status": parse_int_metric(row.get("设为状态")),
            "set_moments_cover": parse_int_metric(row.get("设为朋友圈封面")),
            "wecom_clicks": parse_int_metric(row.get("企微链接点击次数")),
            "wecom_click_users": parse_int_metric(row.get("企微链接点击人数")),
            "wecom_adds": parse_int_metric(row.get("添加到通讯录次数")),
            "wecom_add_users": parse_int_metric(row.get("添加到通讯录人数")),
        }

        covers_full = bool(pub_d is not None and pub_d >= start_d)
        if covers_full:
            updated_lifetime_count += 1
        else:
            window_only_count += 1

        age_hours = _compute_age_hours(video.get("published_at"), video.get("publish_date"), collected_dt)
        snap = {
            "snapshot_id": f"{import_id}_{_short_hash(video['id'])}",
            "source": "csv",
            "collected_at": collected_iso,
            "window_type": resolved_window,
            "window_start": start_iso,
            "window_end": end_iso,
            "covers_full_lifetime": covers_full,
            "import_id": import_id,
            "age_hours": age_hours,
            "stage": stage_from_hours(age_hours),
            "metrics": row_metrics,
        }
        _append_snapshot(video, snap)

        video.setdefault("window_metrics", {"7d": None, "30d": None, "custom": None})
        video["window_metrics"][resolved_window] = {
            "collected_at": collected_iso,
            "window_start": start_iso,
            "window_end": end_iso,
            "covers_full_lifetime": covers_full,
            "metrics": row_metrics,
        }

        recompute_latest_metrics(video)
        changed_videos.append(video)

    for v in changed_videos:
        save_video(hdir, v)

    imports_path = hdir / "imports.json"
    imports_list: list[dict[str, Any]] = []
    if imports_path.is_file():
        try:
            loaded = _read_json(imports_path)
            if isinstance(loaded, list):
                imports_list = loaded
        except (OSError, ValueError):
            imports_list = []

    batch = {
        "import_id": import_id,
        "imported_at": _now_shanghai().isoformat(),
        "collected_at": collected_iso,
        "source_file": source_label,
        "archived_csv": archived_rel,
        "window_type": resolved_window,
        "window_start": start_iso,
        "window_end": end_iso,
        "row_count": len(rows),
        "matched_count": matched_count,
        "new_count": new_count,
        "updated_lifetime_count": updated_lifetime_count,
        "window_only_count": window_only_count,
    }
    imports_list.insert(0, batch)
    _atomic_write_json(imports_path, imports_list[:200])

    index = rebuild_index(hdir)
    return {**batch, "total_videos": index["total_videos"]}


def _find_cut_record_for_run(run_dir: Path) -> dict[str, Any]:
    candidates: list[Path] = []
    prep = run_dir / "prepared-video.txt"
    if prep.is_file():
        try:
            vp = Path(prep.read_text(encoding="utf-8-sig").strip())
            candidates.append(vp.with_name("剪辑记录.json"))
        except OSError:
            pass
    ego_in = run_dir / "ego-input.json"
    if ego_in.is_file():
        try:
            inp = _read_json(ego_in)
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


def _extract_ego_metrics(raw_metrics: Any, parsed: Any, extra: Any) -> dict[str, Any]:
    out = _empty_metrics()
    if isinstance(parsed, dict) and any(k in parsed for k in ("plays", "likes", "comments", "shares", "saves")):
        out["plays"] = parse_int_metric(parsed.get("plays"))
        out["likes"] = parse_int_metric(parsed.get("likes"))
        out["comments"] = parse_int_metric(parsed.get("comments"))
        out["shares"] = parse_int_metric(parsed.get("shares"))
        out["saves"] = parse_int_metric(parsed.get("saves"))
    else:
        items = raw_metrics if isinstance(raw_metrics, list) else []
        keys = ("plays", "likes", "comments", "shares", "saves")
        for idx, k in enumerate(keys):
            if idx < len(items):
                cell = items[idx]
                raw = cell.get("num") if isinstance(cell, dict) else cell
                out[k] = parse_int_metric(raw)
    if isinstance(extra, dict):
        if extra.get("completionRate") is not None:
            out["completion_rate"] = parse_percent(extra.get("completionRate"))
        if extra.get("avgWatchDuration") is not None:
            out["avg_watch_sec"] = parse_duration_sec(extra.get("avgWatchDuration"))
    return out


def _sync_run_into_list(run_dir: Path, videos: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    retro_path = run_dir / "retro.json"
    if not retro_path.is_file():
        return None, False
    try:
        retro = _read_json(retro_path)
        if not isinstance(retro, dict):
            return None, False
    except (OSError, ValueError):
        return None, False

    pub_dt = _parse_datetime(retro.get("capturedAt")) or _parse_run_dir_time(run_dir.name)
    pub_iso = pub_dt.isoformat() if pub_dt else None
    pub_date = pub_dt.date().isoformat() if pub_dt else None

    post_id = str(retro.get("postId") or "").strip() or None
    raw_desc = str(retro.get("publishDesc") or "")
    desc_body, desc_tags = split_desc_and_tags(raw_desc)
    pub_title = str(retro.get("publishTitle") or "").strip()

    video = _find_matching_video(
        videos,
        platform_id=post_id,
        run_name=run_dir.name,
        desc_text=desc_body,
        title_text=pub_title,
        pub_date_str=pub_date,
    )
    created = False
    if video is None:
        seed = post_id or run_dir.name
        vid = _make_video_id(pub_date, seed)
        video = _new_video_record(
            video_id=vid,
            platform_id=post_id,
            published_at=pub_iso,
            publish_date=pub_date,
            publish_time_exact=pub_dt is not None,
        )
        videos.append(video)
        created = True
    else:
        if post_id and not video.get("platform_id"):
            video["platform_id"] = post_id
        if pub_iso and not video.get("publish_time_exact"):
            video["published_at"] = pub_iso
            video["publish_date"] = pub_date
            video["publish_time_exact"] = True

    if run_dir.name not in video["run_names"]:
        video["run_names"].append(run_dir.name)

    transcript = ""
    t_path = run_dir / "transcript.txt"
    if t_path.is_file():
        try:
            transcript = t_path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            transcript = ""

    cut_rec = _find_cut_record_for_run(run_dir)
    creative = video["creative"]
    if pub_title and not creative.get("publish_title"):
        creative["publish_title"] = pub_title
    if desc_body and not creative.get("publish_desc"):
        creative["publish_desc"] = desc_body
    tags = [str(t).strip().lstrip("#") for t in (retro.get("publishTags") or desc_tags) if str(t).strip()]
    if tags and not creative.get("publish_tags"):
        creative["publish_tags"] = tags
    if transcript and not creative.get("transcript"):
        creative["transcript"] = transcript
    if creative.get("transcript") and not creative.get("opening_3s"):
        creative["opening_3s"] = extract_opening_3s(creative["transcript"])

    for src_k, dst_k in (
        ("hook_context", "hook_context"),
        ("hook_title", "hook_title"),
        ("hook_summary", "hook_summary"),
    ):
        val = str(cut_rec.get(src_k) or "").strip()
        if val and not creative.get(dst_k):
            creative[dst_k] = val
    if isinstance(cut_rec.get("duration"), (int, float)) and creative.get("source_duration_sec") is None:
        creative["source_duration_sec"] = round(float(cut_rec["duration"]), 2)
    if isinstance(cut_rec.get("output_duration"), (int, float)) and creative.get("output_duration_sec") is None:
        creative["output_duration_sec"] = round(float(cut_rec["output_duration"]), 2)

    # Read BOTH retro-snapshots.json and live-metrics.json so fresher live data is never dropped.
    snap_path = run_dir / "retro-snapshots.json"
    if snap_path.is_file():
        try:
            snap_store = _read_json(snap_path)
            snaps = snap_store.get("snapshots") if isinstance(snap_store, dict) else []
            for item in snaps or []:
                if not isinstance(item, dict):
                    continue
                cdt = _parse_datetime(item.get("collectedAt"))
                if cdt is None:
                    continue
                m = _extract_ego_metrics(item.get("rawMetrics"), item.get("parsedMetrics"), item.get("extraMetrics"))
                age_h = _compute_age_hours(video.get("published_at"), video.get("publish_date"), cdt)
                _append_snapshot(
                    video,
                    {
                        "snapshot_id": f"ego_{cdt.strftime('%Y%m%d_%H%M%S')}_{_short_hash(run_dir.name)}",
                        "source": "ego",
                        "collected_at": cdt.isoformat(),
                        "window_type": "lifetime",
                        "window_start": video.get("publish_date"),
                        "window_end": cdt.date().isoformat(),
                        "covers_full_lifetime": True,
                        "import_id": run_dir.name,
                        "age_hours": age_h,
                        "stage": stage_from_hours(age_h),
                        "metrics": m,
                    },
                )
                if isinstance(item.get("comments"), list) and item["comments"]:
                    video["comments"] = item["comments"]
        except (OSError, ValueError):
            pass

    live_path = run_dir / "live-metrics.json"
    if live_path.is_file():
        try:
            live = _read_json(live_path)
            if isinstance(live, dict):
                cdt = _parse_datetime(live.get("capturedAt") or live.get("collectedAt"))
                if cdt is not None:
                    m = _extract_ego_metrics(live.get("rawMetrics"), live.get("parsedMetrics"), live.get("extraMetrics"))
                    age_h = _compute_age_hours(video.get("published_at"), video.get("publish_date"), cdt)
                    _append_snapshot(
                        video,
                        {
                            "snapshot_id": f"live_{cdt.strftime('%Y%m%d_%H%M%S')}_{_short_hash(run_dir.name)}",
                            "source": "ego",
                            "collected_at": cdt.isoformat(),
                            "window_type": "lifetime",
                            "window_start": video.get("publish_date"),
                            "window_end": cdt.date().isoformat(),
                            "covers_full_lifetime": True,
                            "import_id": run_dir.name,
                            "age_hours": age_h,
                            "stage": stage_from_hours(age_h),
                            "metrics": m,
                        },
                    )
        except (OSError, ValueError):
            pass

    recompute_latest_metrics(video)
    return video, created


def _enrich_legacy_payloads(root: Path, videos: list[dict[str, Any]]) -> int:
    """Enrich existing CSV-imported legacy Oct 27-28 videos with titles/transcripts from workspace payloads."""
    enriched = 0
    specs: list[tuple[Path, Path | None, Path | None, str]] = [
        (root / "sph-1028" / "payload-sph-10月28日.json", root / "sph-1028" / "details-final.json", None, "2026-10-28"),
        (
            root / "sph-1028" / "payload-sph-10月28日-2.json",
            root / "sph-1028" / "details-2-final.json",
            root / "sph-1028" / "transcript-1028-2.txt",
            "2026-10-28",
        ),
        (
            root / "sph-1028" / "payload-sph-10月28日-3.json",
            root / "sph-1028" / "details-3-final.json",
            root / "sph-1028" / "transcript-1028-3.txt",
            "2026-10-28",
        ),
        (root / "payload-sph-10月27日.json", None, None, "2026-10-27"),
    ]
    for payload_path, details_path, transcript_path, pub_date in specs:
        if not payload_path.is_file():
            continue
        try:
            payload = _read_json(payload_path)
            form = payload["publishArgs"]["accountForms"][0]["contentPublishForm"]
            video_meta = payload["publishArgs"]["accountForms"][0].get("video") or {}
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue

        doc_id = None
        if details_path and details_path.is_file():
            try:
                det = _read_json(details_path)
                doc_id = det["data"]["tasks"][0].get("documentId") or None
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                doc_id = None

        desc_body, tags = split_desc_and_tags(form.get("description") or "")
        title = str(form.get("title") or "").strip()
        video = _find_matching_video(
            videos,
            platform_id=doc_id,
            desc_text=desc_body,
            title_text=title,
            pub_date_str=pub_date,
        )
        if video is None:
            continue

        changed = False
        creative = video["creative"]
        if title and not creative.get("publish_title"):
            creative["publish_title"] = title
            changed = True
        if desc_body and not creative.get("publish_desc"):
            creative["publish_desc"] = desc_body
            changed = True
        if tags and not creative.get("publish_tags"):
            creative["publish_tags"] = tags
            changed = True
        if transcript_path and transcript_path.is_file() and not creative.get("transcript"):
            t_txt = _read_text_auto(transcript_path).strip()
            if t_txt:
                creative["transcript"] = t_txt
                creative["opening_3s"] = extract_opening_3s(t_txt)
                changed = True
        dur = video_meta.get("duration")
        if isinstance(dur, (int, float)) and creative.get("output_duration_sec") is None:
            creative["output_duration_sec"] = round(float(dur), 2)
            recompute_latest_metrics(video)
            changed = True
        if changed:
            enriched += 1
    return enriched


def sync_single_run(
    run_dir: Path,
    *,
    history_dir: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    hdir = Path(history_dir) if history_dir is not None else default_history_dir(root)
    _ensure_history_layout(hdir)
    videos = load_all_videos(hdir)
    video, created = _sync_run_into_list(run_dir, videos)
    if video is None:
        return {"synced": False, "reason": "no_retro", "run_dir": str(run_dir)}
    save_video(hdir, video)
    index = rebuild_index(hdir)
    return {
        "synced": True,
        "created": created,
        "video_id": video["id"],
        "total_videos": index["total_videos"],
    }


def sync_publish_runs(
    runs_dir: Path | None = None,
    *,
    history_dir: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    base_root = Path(root) if root is not None else DEFAULT_ROOT
    rdir = Path(runs_dir) if runs_dir is not None else (base_root / "publish-runs")
    hdir = Path(history_dir) if history_dir is not None else default_history_dir(base_root)
    _ensure_history_layout(hdir)

    videos = load_all_videos(hdir)
    created_count = 0
    updated_count = 0
    touched: dict[str, dict[str, Any]] = {}

    if rdir.is_dir():
        for sub in sorted(rdir.iterdir(), key=lambda p: p.name):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            video, created = _sync_run_into_list(sub, videos)
            if video is None:
                continue
            touched[video["id"]] = video
            if created:
                created_count += 1
            else:
                updated_count += 1

    legacy_enriched = _enrich_legacy_payloads(base_root, videos)
    for v in videos:
        if v["id"] in touched or legacy_enriched:
            save_video(hdir, v)

    index = rebuild_index(hdir)
    return {
        "runs_processed": created_count + updated_count,
        "created": created_count,
        "updated": updated_count,
        "legacy_enriched": legacy_enriched,
        "total_videos": index["total_videos"],
    }


def update_video_review(
    history_dir: Path,
    video_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    video = load_video(history_dir, video_id)
    if video is None:
        raise FileNotFoundError(f"找不到视频记录：{video_id}")

    review = video.setdefault("review", {})
    if "status" in patch and patch["status"] in ("active", "excluded"):
        review["status"] = patch["status"]
    if "user_mark" in patch and patch["user_mark"] in ("", "标杆", "想二创", "踩坑"):
        review["user_mark"] = patch["user_mark"]
    if "user_notes" in patch:
        review["user_notes"] = str(patch["user_notes"] or "").strip()
    if "ai_diagnosis" in patch:
        review["ai_diagnosis"] = str(patch["ai_diagnosis"] or "").strip()
    for list_key in ("topic_tags", "style_tags"):
        if list_key in patch:
            raw = patch[list_key]
            if isinstance(raw, str):
                items = [x.strip() for x in re.split(r"[,，、\s]+", raw) if x.strip()]
            elif isinstance(raw, list):
                items = [str(x).strip() for x in raw if str(x).strip()]
            else:
                items = []
            review[list_key] = list(dict.fromkeys(items))

    creative = video.setdefault("creative", {})
    for field in ("transcript", "publish_title", "hook_context", "hook_title", "publish_desc"):
        if field in patch and patch[field] is not None:
            creative[field] = str(patch[field]).strip()
    if "transcript" in patch:
        creative["opening_3s"] = extract_opening_3s(creative.get("transcript") or "")

    save_video(history_dir, video)
    rebuild_index(history_dir)
    return video


def _make_index_item(video: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    c = video.get("creative") or {}
    m = video.get("latest_metrics") or {}
    e = video.get("engagement") or {}
    rev = video.get("review") or {}
    age_h = _compute_age_hours(video.get("published_at"), video.get("publish_date"), now_dt)
    stage = stage_from_hours(age_h)
    plays = m.get("plays")
    title = c.get("publish_title") or (c.get("publish_desc") or "")[:24] or video["id"]
    return {
        "id": video["id"],
        "platform_id": video.get("platform_id"),
        "publish_date": video.get("publish_date"),
        "published_at": video.get("published_at"),
        "age_hours": age_h,
        "stage": stage,
        "publish_title": title,
        "hook_context": c.get("hook_context") or "",
        "hook_title": c.get("hook_title") or "",
        "hook_summary": c.get("hook_summary") or "",
        "publish_desc_excerpt": (c.get("publish_desc") or "")[:90],
        "publish_tags": c.get("publish_tags") or [],
        "opening_3s": c.get("opening_3s") or "",
        "has_transcript": bool((c.get("transcript") or "").strip()),
        "transcript_len": len((c.get("transcript") or "").strip()),
        "output_duration_sec": c.get("output_duration_sec"),
        "plays": plays,
        "completion_rate": m.get("completion_rate"),
        "avg_watch_sec": m.get("avg_watch_sec"),
        "recommends": m.get("recommends"),
        "likes": m.get("likes"),
        "comments": m.get("comments"),
        "shares": m.get("shares"),
        "follows": m.get("follows"),
        "saves": m.get("saves"),
        "interactions": e.get("interactions"),
        "interaction_rate": e.get("interaction_rate"),
        "follow_rate": e.get("follow_rate"),
        "watch_ratio": e.get("watch_ratio"),
        "metrics_scope": video.get("metrics_scope") or "pending",
        "metrics_source": video.get("metrics_source") or "retro",
        "metrics_updated_at": video.get("metrics_updated_at"),
        "comments_count": len(video.get("comments") or []),
        "snapshots_count": len(video.get("snapshots") or []),
        "has_7d_window": (video.get("window_metrics") or {}).get("7d") is not None,
        "has_30d_window": (video.get("window_metrics") or {}).get("30d") is not None,
        "status": rev.get("status") or "active",
        "user_mark": rev.get("user_mark") or "",
        "topic_tags": rev.get("topic_tags") or [],
        "style_tags": rev.get("style_tags") or [],
        "user_notes": rev.get("user_notes") or "",
    }


def _compute_bucket_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    plays = [x["plays"] for x in items if isinstance(x.get("plays"), (int, float))]
    comps = [x["completion_rate"] for x in items if isinstance(x.get("completion_rate"), (int, float))]
    watches = [x["avg_watch_sec"] for x in items if isinstance(x.get("avg_watch_sec"), (int, float))]
    rates = [x["interaction_rate"] for x in items if isinstance(x.get("interaction_rate"), (int, float))]
    follows = [x["follows"] for x in items if isinstance(x.get("follows"), (int, float))]
    return {
        "count": len(items),
        "total_plays": int(sum(plays)) if plays else 0,
        "avg_plays": round(sum(plays) / len(plays), 1) if plays else 0.0,
        "median_plays": round(statistics.median(plays), 1) if plays else 0.0,
        "max_plays": int(max(plays)) if plays else 0,
        "avg_completion_rate": round(sum(comps) / len(comps), 4) if comps else None,
        "avg_watch_sec": round(sum(watches) / len(watches), 2) if watches else None,
        "avg_interaction_rate": round(sum(rates) / len(rates), 4) if rates else None,
        "total_follows": int(sum(follows)) if follows else 0,
    }


def rebuild_index(history_dir: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    hdir = Path(history_dir) if history_dir is not None else default_history_dir()
    _ensure_history_layout(hdir)
    now_dt = (now or _now_shanghai()).astimezone(SHANGHAI_TZ)
    videos = load_all_videos(hdir)

    items = [_make_index_item(v, now_dt) for v in videos]
    items.sort(key=lambda x: (str(x.get("published_at") or x.get("publish_date") or ""), x["id"]), reverse=True)

    active = [x for x in items if x["status"] != "excluded"]
    stages: dict[str, Any] = {}
    for st in STAGES_ORDER:
        bucket = [x for x in active if x["stage"] == st]
        if bucket:
            stages[st] = _compute_bucket_stats(bucket)

    mature = [x for x in active if x["stage"] in ("1天", "3天", "7天+")]
    summary = {
        **_compute_bucket_stats(active),
        "excluded_count": len(items) - len(active),
        "with_transcript_count": sum(1 for x in active if x["has_transcript"]),
        "mature_count": len(mature),
        "mature_median_plays": round(statistics.median([x["plays"] for x in mature if x.get("plays") is not None]), 1)
        if any(x.get("plays") is not None for x in mature)
        else 0.0,
        "by_stage": stages,
    }

    index_data = {
        "version": SCHEMA_VERSION,
        "updated_at": now_dt.isoformat(),
        "total_videos": len(items),
        "active_videos": len(active),
        "stats_summary": summary,
        "videos": items,
    }
    _atomic_write_json(hdir / "index.json", index_data)
    return index_data


def _rule_id(category: str, text: str) -> str:
    return f"r_{category[:3]}_{_short_hash(category + ':' + text)}"


def _topic_id(title: str) -> str:
    return f"t_{_short_hash(title)}"


def empty_playbook() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "updated_at": None,
        "source_videos_count": 0,
        "collected_count": 0,
        "analysis_source": "local",
        "executive_summary": "",
        "rules": [],
        "next_topics": [],
        "glossary_suggestions": [],
        "top_examples": [],
        "why_winners_won": [],
        "why_losers_lost": [],
    }


def load_playbook(history_dir: Path | None = None) -> dict[str, Any]:
    hdir = Path(history_dir) if history_dir is not None else default_history_dir()
    pb_path = hdir / "playbook.json"
    if not pb_path.is_file():
        return empty_playbook()
    try:
        data = _read_json(pb_path)
        if isinstance(data, dict):
            base = empty_playbook()
            base.update(data)
            return base
    except (OSError, ValueError):
        pass
    return empty_playbook()


def save_playbook(history_dir: Path | None, playbook: dict[str, Any]) -> Path:
    hdir = Path(history_dir) if history_dir is not None else default_history_dir()
    _ensure_history_layout(hdir)
    playbook["version"] = SCHEMA_VERSION
    playbook["updated_at"] = _now_shanghai().isoformat()
    path = hdir / "playbook.json"
    _atomic_write_json(path, playbook)
    return path


def bootstrap_playbook_from_legacy(history_dir: Path, legacy_path: Path) -> dict[str, Any] | None:
    pb_path = Path(history_dir) / "playbook.json"
    if pb_path.is_file() or not Path(legacy_path).is_file():
        return None
    try:
        old = _read_json(Path(legacy_path))
        if not isinstance(old, dict):
            return None
    except (OSError, ValueError):
        return None

    now_iso = _now_shanghai().isoformat()
    rules: list[dict[str, Any]] = []
    for cat, old_key in (
        ("winning", "winning_patterns"),
        ("avoid", "avoid_patterns"),
        ("hook", "hook_title_tips"),
        ("copywriting", "copywriting_tips"),
    ):
        for txt in old.get(old_key) or []:
            t = str(txt).strip()
            if t:
                rules.append({
                    "id": _rule_id(cat, t),
                    "category": cat,
                    "text": t,
                    "evidence_ids": [],
                    "status": "proposed",
                    "origin": "legacy",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                })

    topics: list[dict[str, Any]] = []
    for item in old.get("next_topics") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        topics.append({
            "id": _topic_id(title),
            "title": title,
            "hook_title": str(item.get("hook_title") or "").strip(),
            "hook_context": str(item.get("hook_context") or "").strip(),
            "angle": str(item.get("angle") or "").strip(),
            "status": "todo",
            "created_at": now_iso,
        })

    glossary: list[dict[str, Any]] = []
    for g in old.get("glossary_suggestions") or []:
        if isinstance(g, dict) and str(g.get("right") or "").strip():
            glossary.append({
                "wrong": str(g.get("wrong") or "").strip(),
                "right": str(g.get("right") or "").strip(),
                "status": "proposed",
            })

    pb = empty_playbook()
    pb.update({
        "source_videos_count": int(old.get("source_runs_count") or 0),
        "collected_count": int(old.get("collected_count") or 0),
        "analysis_source": str(old.get("analysis_source") or "legacy"),
        "executive_summary": str(old.get("executive_summary") or ""),
        "rules": rules,
        "next_topics": topics,
        "glossary_suggestions": glossary,
        "top_examples": list(old.get("top_examples") or []),
    })
    save_playbook(history_dir, pb)
    return pb


def merge_ai_into_playbook(
    history_dir: Path,
    analysis: dict[str, Any],
    *,
    source_videos_count: int,
    collected_count: int,
    top_examples: list[dict[str, Any]],
) -> dict[str, Any]:
    existing = load_playbook(history_dir)
    now_iso = _now_shanghai().isoformat()

    by_id: dict[str, dict[str, Any]] = {}
    by_norm_text: dict[tuple[str, str], dict[str, Any]] = {}
    for r in existing.get("rules") or []:
        if isinstance(r, dict) and r.get("id") and r.get("text"):
            by_id[r["id"]] = r
            by_norm_text[(r.get("category") or "", normalize_match_text(r["text"]))] = r

    new_rules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Keep all user-authored, approved, or rejected rules so user choices never vanish.
    # If this run is only a local offline fallback and richer AI/legacy proposed rules already exist, keep them too.
    preserve_existing_proposed = (
        analysis.get("source") == "local"
        and existing.get("analysis_source") in ("claude", "deepseek", "legacy", "ai")
        and bool(existing.get("rules"))
    )
    for r in existing.get("rules") or []:
        if not isinstance(r, dict):
            continue
        if r.get("status") in ("approved", "rejected") or r.get("origin") == "user" or (
            preserve_existing_proposed and r.get("status") == "proposed"
        ):
            new_rules.append(r)
            seen_ids.add(r["id"])

    for cat, key in (
        ("winning", "winning_patterns"),
        ("avoid", "avoid_patterns"),
        ("hook", "hook_title_tips"),
        ("copywriting", "copywriting_tips"),
    ):
        for raw_item in analysis.get(key) or []:
            if isinstance(raw_item, dict):
                txt = str(raw_item.get("text") or "").strip()
                ev_ids = [str(x) for x in (raw_item.get("evidence_ids") or []) if str(x).strip()]
            else:
                txt = str(raw_item).strip()
                ev_ids = []
            if not txt:
                continue
            norm_k = (cat, normalize_match_text(txt))
            if norm_k in by_norm_text:
                prev = by_norm_text[norm_k]
                if prev["id"] in seen_ids:
                    if ev_ids:
                        prev["evidence_ids"] = list(dict.fromkeys(list(prev.get("evidence_ids") or []) + ev_ids))
                    continue
                prev["evidence_ids"] = ev_ids or prev.get("evidence_ids") or []
                prev["updated_at"] = now_iso
                new_rules.append(prev)
                seen_ids.add(prev["id"])
                continue
            rid = _rule_id(cat, txt)
            if rid in seen_ids:
                continue
            new_rules.append({
                "id": rid,
                "category": cat,
                "text": txt,
                "evidence_ids": ev_ids,
                "status": "proposed",
                "origin": analysis.get("source") or "ai",
                "created_at": now_iso,
                "updated_at": now_iso,
            })
            seen_ids.add(rid)

    existing_topics = {
        normalize_match_text(t.get("title") or ""): t
        for t in (existing.get("next_topics") or [])
        if isinstance(t, dict) and t.get("title")
    }
    merged_topics: list[dict[str, Any]] = []
    seen_topic_keys: set[str] = set()
    for t in existing.get("next_topics") or []:
        if isinstance(t, dict) and t.get("status") in ("filmed", "dismissed"):
            k = normalize_match_text(t.get("title") or "")
            if k and k not in seen_topic_keys:
                merged_topics.append(t)
                seen_topic_keys.add(k)
    for item in analysis.get("next_topics") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        k = normalize_match_text(title)
        if k in seen_topic_keys:
            continue
        prev_t = existing_topics.get(k)
        merged_topics.append({
            "id": prev_t["id"] if prev_t else _topic_id(title),
            "title": title,
            "hook_title": str(item.get("hook_title") or "").strip(),
            "hook_context": str(item.get("hook_context") or "").strip(),
            "angle": str(item.get("angle") or "").strip(),
            "status": prev_t.get("status", "todo") if prev_t else "todo",
            "created_at": prev_t.get("created_at", now_iso) if prev_t else now_iso,
        })
        seen_topic_keys.add(k)

    # Merge glossary suggestions, preserving rejected status.
    old_glossary = {
        str(g.get("right") or "").casefold(): g
        for g in (existing.get("glossary_suggestions") or [])
        if isinstance(g, dict) and g.get("right")
    }
    for g in analysis.get("glossary_suggestions") or []:
        if not isinstance(g, dict):
            continue
        right = str(g.get("right") or "").strip()
        if not right:
            continue
        key = right.casefold()
        if key not in old_glossary:
            old_glossary[key] = {
                "wrong": str(g.get("wrong") or "").strip(),
                "right": right,
                "status": "proposed",
            }

    pb = empty_playbook()
    pb.update({
        "source_videos_count": source_videos_count,
        "collected_count": collected_count,
        "analysis_source": existing["analysis_source"] if preserve_existing_proposed else str(analysis.get("source") or "local"),
        "executive_summary": existing["executive_summary"] if (preserve_existing_proposed and existing.get("executive_summary")) else str(analysis.get("executive_summary") or ""),
        "rules": new_rules,
        "next_topics": existing["next_topics"] if (preserve_existing_proposed and existing.get("next_topics")) else merged_topics,
        "glossary_suggestions": list(old_glossary.values()),
        "top_examples": top_examples,
        "why_winners_won": list(analysis.get("why_winners_won") or []),
        "why_losers_lost": list(analysis.get("why_losers_lost") or []),
    })
    save_playbook(history_dir, pb)
    return pb


def update_playbook_rule(
    history_dir: Path,
    rule_id: str,
    *,
    status: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    pb = load_playbook(history_dir)
    target = None
    for r in pb.get("rules") or []:
        if isinstance(r, dict) and r.get("id") == rule_id:
            target = r
            break
    if target is None:
        raise FileNotFoundError(f"找不到规则：{rule_id}")
    if status is not None:
        if status not in ("proposed", "approved", "rejected"):
            raise ValueError(f"无效规则状态：{status}")
        target["status"] = status
    if text is not None and str(text).strip():
        target["text"] = str(text).strip()
    target["updated_at"] = _now_shanghai().isoformat()
    save_playbook(history_dir, pb)
    return pb


def add_playbook_rule(
    history_dir: Path,
    *,
    category: str,
    text: str,
    status: str = "approved",
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    if category not in ("winning", "avoid", "hook", "copywriting"):
        raise ValueError(f"无效规则分类：{category}")
    if status not in ("proposed", "approved", "rejected"):
        raise ValueError(f"无效规则状态：{status}")
    clean_text = str(text or "").strip()
    if not clean_text:
        raise ValueError("规则内容不能为空")

    pb = load_playbook(history_dir)
    now_iso = _now_shanghai().isoformat()
    rid = _rule_id(category, clean_text)
    for r in pb["rules"]:
        if r.get("id") == rid:
            r["status"] = status
            r["origin"] = "user"
            r["updated_at"] = now_iso
            save_playbook(history_dir, pb)
            return pb

    pb["rules"].insert(0, {
        "id": rid,
        "category": category,
        "text": clean_text,
        "evidence_ids": list(evidence_ids or []),
        "status": status,
        "origin": "user",
        "created_at": now_iso,
        "updated_at": now_iso,
    })
    save_playbook(history_dir, pb)
    return pb


def update_topic_status(history_dir: Path, topic_id: str, status: str) -> dict[str, Any]:
    if status not in ("todo", "filmed", "dismissed"):
        raise ValueError(f"无效选题状态：{status}")
    pb = load_playbook(history_dir)
    found = False
    for t in pb.get("next_topics") or []:
        if isinstance(t, dict) and t.get("id") == topic_id:
            t["status"] = status
            found = True
            break
    if not found:
        raise FileNotFoundError(f"找不到选题：{topic_id}")
    save_playbook(history_dir, pb)
    return pb


def format_playbook_prompt(history_dir: Path, mode: str = "copy") -> str:
    pb = load_playbook(history_dir)
    rules = [r for r in (pb.get("rules") or []) if isinstance(r, dict) and r.get("status") != "rejected"]
    # Prioritise user-approved rules over AI-proposed ones.
    rules.sort(key=lambda r: (0 if r.get("status") == "approved" else 1))

    def by_cat(cat: str, limit: int = 2) -> list[str]:
        return [str(r["text"]).strip() for r in rules if r.get("category") == cat and r.get("text")][:limit]

    parts: list[str] = []
    if mode == "hook":
        tips = by_cat("hook", 2)
        if tips:
            parts.append("黄字经验：" + "；".join(tips))
        ex = [str(e.get("hook_title")).strip() for e in (pb.get("top_examples") or []) if isinstance(e, dict) and e.get("hook_title")]
        if ex:
            parts.append("高播放黄字参考：" + " / ".join(ex[:2]))
    else:
        tips = by_cat("copywriting", 2)
        if tips:
            parts.append("文案经验：" + "；".join(tips))
        ex = [str(e.get("title")).strip() for e in (pb.get("top_examples") or []) if isinstance(e, dict) and e.get("title")]
        if ex:
            parts.append("高播放标题参考：" + " / ".join(ex[:2]))

    wins = by_cat("winning", 2)
    if wins:
        parts.append("有效套路：" + "；".join(wins))
    avoids = by_cat("avoid", 2)
    if avoids:
        parts.append("注意避坑：" + "；".join(avoids))

    text = "\n".join(p for p in parts if p.strip()).strip()
    if len(text) > 400:
        text = text[:397].rstrip("；;、，, \n") + "…"
    return text


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-dir", type=Path, default=None, help="history 数据库目录路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync_runs = sub.add_parser("sync-runs", help="全量同步 publish-runs 与历史留档到 history/")
    p_sync_runs.add_argument("--runs-dir", type=Path, default=None)

    p_sync_run = sub.add_parser("sync-run", help="同步单条 publish-runs 目录到 history/")
    p_sync_run.add_argument("--run-dir", type=Path, required=True)

    p_import = sub.add_parser("import-csv", help="导入视频号后台下载的动态数据明细 CSV")
    p_import.add_argument("csv_path", type=Path)
    p_import.add_argument("--window", choices=("auto", "7d", "30d", "custom"), default="auto")
    p_import.add_argument("--start", default=None, help="窗口起日 YYYY-MM-DD")
    p_import.add_argument("--end", default=None, help="窗口止日 YYYY-MM-DD")
    p_import.add_argument("--no-save-raw", action="store_true")

    sub.add_parser("rebuild-index", help="重建 history/index.json")

    p_prompt = sub.add_parser("print-prompt", help="打印精简的策略记忆提示词")
    p_prompt.add_argument("--mode", choices=("copy", "hook"), default="copy")

    args = parser.parse_args(argv)
    hdir = args.history_dir or default_history_dir()

    if args.cmd == "sync-runs":
        res = sync_publish_runs(args.runs_dir, history_dir=hdir)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if args.cmd == "sync-run":
        res = sync_single_run(args.run_dir, history_dir=hdir)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if args.cmd == "import-csv":
        res = import_csv(
            args.csv_path,
            history_dir=hdir,
            window_type=args.window,
            window_start=args.start,
            window_end=args.end,
            save_raw=not args.no_save_raw,
        )
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if args.cmd == "rebuild-index":
        idx = rebuild_index(hdir)
        print(json.dumps({"total_videos": idx["total_videos"], "active_videos": idx["active_videos"]}, ensure_ascii=False))
        return 0
    if args.cmd == "print-prompt":
        txt = format_playbook_prompt(hdir, mode=args.mode)
        if txt:
            print(txt)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
