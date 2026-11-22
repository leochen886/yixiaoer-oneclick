"""DeepSeek 听写原声音轨并分析全文，在本地剪辑、烧录字幕和导出。"""

import argparse
import hashlib
import json
import math
import os
import subprocess
import tempfile
from dataclasses import asdict, replace
from fractions import Fraction
from pathlib import Path

from koubo_deepseek import build_cut_plan, compact_silences
from koubo_plan import (Word, align_reviewed_words, caption_events, kept_ranges, make_plan, map_kept_words,
                        quiet_ranges, read_words, review_groups)
from koubo_proofread import read_reference, review_captions
from transcribe import open_recognizer
from transcribe_funasr import transcribe


SPEED = Fraction(13, 10)


def probe(source, *, with_columns=False):
    info = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)
    ]))
    video = next((s for s in info["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if not video or not any(s["codec_type"] == "audio" for s in info["streams"]):
        raise ValueError("素材需要同时包含画面和音轨。")
    if video.get("duration") not in (None, "N/A"):
        origin = float(info["format"].get("start_time", 0))
        duration = float(video["duration"]) + float(video.get("start_time", origin)) - origin
    else:
        duration = float(info["format"]["duration"])
    for key in ("r_frame_rate", "avg_frame_rate"):
        rate = video.get(key, "0/0")
        if rate != "0/0" and 0 < Fraction(rate) <= 120:
            result = video["index"], duration, Fraction(rate)
            if with_columns:
                aspect = video["width"] / video["height"]
                sar = video.get("sample_aspect_ratio", "1:1")
                if sar not in ("N/A", "0:1"):
                    aspect *= float(Fraction(sar.replace(":", "/")))
                rotation = next((s["rotation"] for s in video.get("side_data_list", []) if "rotation" in s),
                                video.get("tags", {}).get("rotate", 0))
                if int(rotation) % 180 == 90:
                    aspect = 1 / aspect
                # The SRT renderer uses a 288-unit play height and a 22-unit font.
                # Leave space for the side margins and glyph outlines.
                return (*result, max(1, min(16, math.floor(aspect * 11))))
            return result
    raise ValueError("无法读取可用的视频帧率。")


def write_srt(path, events):
    def stamp(t):
        seconds, ms = divmod(round(t * 1000), 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"

    path.write_text("".join(f"{i}\n{stamp(e['start'])} --> {stamp(e['end'])}\n{e['text']}\n\n"
                            for i, e in enumerate(events, 1)), encoding="utf-8")


def write_burn_captions(path, events, columns):
    # Match the previous SRT renderer's coordinate system, font and margins.
    # ASS WrapStyle 2 disables automatic wrapping inside the encoded video.
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 384
PlayResY: 288
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,22,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1.2,0,2,10,10,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def stamp(seconds):
        seconds, cs = divmod(round(seconds * 100), 100)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours}:{minutes:02}:{seconds:02}.{cs:02}"

    with path.open("w", encoding="utf-8") as out:
        out.write(header)
        for cue in events:
            text = cue["text"]
            # Allow one em per glyph, including wide Latin letters. An
            # indivisible oversized term stays intact and scales for this cue.
            size = 22 * columns / max(len(text), columns)
            style = rf"{{\fs{size:.3f}}}" if size < 22 else ""
            # A literal backslash must never introduce an ASS newline or tag.
            literal = text.replace("\\", "\\\u2060").replace("{", r"\{").replace("}", r"\}")
            out.write(f"Dialogue: 0,{stamp(cue['start'])},{stamp(cue['end'])},Default,,0,0,0,,{style}{literal}\n")


def render(source, work, video_index, fps, keep, output_frames):
    select = "+".join(f"between(n,{a},{b-1})" for a, b in keep)
    # Select CFR video frames once. Rebase their PTS before rendering subtitles.
    video = (f"[0:{video_index}]fps={fps}:start_time=0,select='{select}',setpts=N,"
             f"settb=AVTB,setpts=PTS*{SPEED.denominator}/{SPEED.numerator},fps={fps},"
             f"tpad=stop=2:stop_mode=clone,trim=end_frame={output_frames},"
             f"ass=captions.ass[v]")
    # Audio is trimmed to the same frame boundaries, at sample precision.
    labels = "".join(f"[src{i}]" for i in range(len(keep)))
    def samples(frame):
        return round(frame * 48000 / fps)
    parts = [video, f"[0:a:0]aresample=48000:async=1:first_pts=0,"
             f"apad=whole_len={samples(keep[-1][1])},asplit={len(keep)}{labels}"]
    for i, (a, b) in enumerate(keep):
        parts.append(f"[src{i}]atrim=start_sample={samples(a)}:end_sample={samples(b)},"
                     f"asetpts=PTS-STARTPTS[a{i}]")
    parts.append("".join(f"[a{i}]" for i in range(len(keep))) +
                 f"concat=n={len(keep)}:v=0:a=1,atempo={float(SPEED)},"
                 f"apad=whole_len={samples(output_frames)},atrim=end_sample={samples(output_frames)}[a]")
    (work / "filters.txt").write_text(";\n".join(parts), encoding="ascii")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
                    "-filter_complex_script", "filters.txt", "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "render.mp4"],
                   cwd=work, check=True)


def prepare_source_edits(source, work, duration, rate, min_pause, local_subtitles, asr_reference):
    receipt = read_reference(asr_reference, source)
    wav = work / "audio.wav"
    print("正在本地转写口播…", flush=True)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
                    "-map", "0:a:0", "-af", "aresample=16000:async=1:first_pts=0",
                    "-t", str(duration), "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True)
    words = read_words(transcribe(wav, offline=True))
    quiet = quiet_ranges(wav)
    proof = {"model": None}
    if not local_subtitles:
        events = review_groups(words, limit=14)
        reference = []
        listen_again = None
        cache = Path(__file__).resolve().parents[1] / "koubo" / "models"
        snapshot = next(cache.glob("models--Systran--faster-whisper-small/snapshots/*/model.bin"), None)
        if snapshot:
            print("正在用本机 Whisper 复听原声…", flush=True)
            try:
                recognize = open_recognizer(wav, cache, str(snapshot.parent), offline=True)
                reference = recognize()["segments"]

                def listen_again(start, end):
                    window = recognize(max(0, start - .25), min(duration, end + .25))
                    reading = "".join(s["text"] for s in window["segments"]).strip()
                    return [reading] if reading else []
            except (ImportError, OSError, RuntimeError) as exc:
                print(f"本地复听未完成（{type(exc).__name__}），继续校对现有转写。", flush=True)
        print("正在使用 DeepSeek 校对原速逐字稿，疑处在本机按原声复听…", flush=True)
        events, proof = review_captions(events, reference, receipt,
                                        receipt.get("local_spot_checks", []),
                                        receipt.get("asr_spot_checks", []),
                                        listen_again=listen_again)
        proof["reference_asr"] = "faster-whisper-small" if reference else None
        print(f"字幕校对：输入 {proof['prompt_tokens']}、输出 {proof['completion_tokens']} tokens。", flush=True)
        words = align_reviewed_words(words, events)
        if "".join("".join(w.text.split()) for w in words) != "".join(
                "".join(e["text"].split()) for e in events):
            raise ValueError("原速校对稿与时间锚点不一致，已停止导出。")
    decisions = []
    print("正在按原速逐字稿标记口误和长停顿…", flush=True)
    keep, dropped, edits = make_plan(words, quiet, duration, rate, min_pause, decisions=decisions)
    return words, proof, decisions, keep, dropped, edits


def restore_source_edits(source, duration, rate, report_path, restore_ids):
    saved = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if (saved.get("source") != source.name or saved.get("fps") != str(rate)
            or abs(saved.get("duration", 0) - duration) > 0.001
            or saved.get("source_size", source.stat().st_size) != source.stat().st_size
            or "source_words" not in saved or "cut_decisions" not in saved):
        raise ValueError("剪辑记录与原片不符，或没有保存可恢复的原片剪点。")
    words = [Word(**item) for item in saved["source_words"]]
    active = [d for d in saved["cut_decisions"] if not d.get("restored")]
    unknown = set(restore_ids) - {d["id"] for d in active}
    if unknown:
        raise ValueError("剪辑记录中找不到可恢复的剪点：" + "、".join(sorted(unknown)))
    for d in active:
        if d["id"] in restore_ids:
            d["restored"] = True
    remaining = [d for d in active if not d.get("restored")]
    keep = kept_ranges(round(duration * float(rate)), [d["frames"] for d in remaining])
    dropped, edits = set(), []
    for d in remaining:
        if d["first_word"] is not None:
            first, last = d["first_word"], d["last_word"]
            dropped.update(range(first, last+1))
            edits.append({"reason": d["reason"], "text": "".join(w.text for w in words[first:last+1]),
                          "start": d["source_start"], "end": d["source_end"]})
    return words, saved["proofreading"], saved["cut_decisions"], keep, dropped, edits


def compact_word_clock(words, cuts, base_keep, fps):
    """Project both ends through verified silence. Earlier speech cuts were checked separately."""
    def base_time(frame):
        return sum(max(0, min(frame, b)-a) for a,b in base_keep) / fps

    intervals = [(base_time(a),base_time(b)) for a,b in cuts]
    def project(t):
        return t - sum(max(0., min(t,b)-a) for a,b in intervals)

    mapped = [replace(w, start=project(w.start), end=project(w.end)) for w in words]
    if any(w.end <= w.start + 1e-8 for w in mapped):
        raise ValueError("静音剪点吞掉了完整的字幕词，已停止导出。")
    return mapped


def export(source, root, min_pause, local_subtitles=False, asr_reference=None, deepseek_audio=None,
           cut_report=None, restore_ids=(), compact_pauses=None):
    if not math.isfinite(min_pause) or min_pause <= 0:
        raise ValueError("停顿阈值必须大于零。")
    if compact_pauses is None:
        # New recordings use the compact rhythm; a replay follows its saved cuts.
        compact_pauses = cut_report is None
    idx, duration, rate, columns = probe(source, with_columns=True)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, prefix=".koubo-") as temp:
        work = Path(temp)
        if cut_report:
            words, proof, decisions, keep, dropped, edits = restore_source_edits(
                source, duration, rate, cut_report, restore_ids)
        elif local_subtitles or asr_reference or deepseek_audio is False:
            words, proof, decisions, keep, dropped, edits = prepare_source_edits(
                source, work, duration, rate, min_pause, local_subtitles, asr_reference)
        else:
            plan = build_cut_plan(source, work / "原声记录", duration, rate,
                                  send_audio=True, min_pause=min_pause)
            words, proof, decisions, keep, dropped, edits = restore_source_edits(
                source, duration, rate, plan, ())
        frames = round(duration * float(rate))
        if compact_pauses and not any(d["reason"] == "复核静音" for d in decisions):
            print("正在按紧凑节奏复核原声静音…", flush=True)
            # Restored intervals are reserved too: a new pass must not undo an explicit restore.
            eligible = kept_ranges(frames, [d["frames"] for d in decisions])
            spoken = [w for i,w in enumerate(words) if i not in dropped]
            for a,b in compact_silences(source, duration, float(rate), spoken, eligible):
                decisions.append({"id":f"{a}-{b}", "frames":[a,b], "source_start":float(a/rate),
                                  "source_end":float(b/rate), "reason":"复核静音",
                                  "first_word":None, "last_word":None})
            decisions.sort(key=lambda d: d["frames"])
        active = [d for d in decisions if not d.get("restored")]
        tight = [d["frames"] for d in active if d["reason"] == "复核静音"]
        if tight:
            base_keep = kept_ranges(frames, [d["frames"] for d in active if d["reason"] != "复核静音"])
            retained = map_kept_words(words, dropped, base_keep, float(rate))
            retained = compact_word_clock(retained, tight, base_keep, float(rate))
            keep = kept_ranges(frames, [d["frames"] for d in active])
        else:
            retained = map_kept_words(words, dropped, keep, float(rate))
        kept_frames = sum(b - a for a, b in keep)
        output_frames = max(1, round(kept_frames / SPEED))
        out_seconds = float(output_frames / rate)
        timed = [replace(w, start=min(w.start / float(SPEED), out_seconds),
                         end=min(w.end / float(SPEED), out_seconds)) for w in retained]
        timed = [w for w in timed if w.start < w.end]
        def output_time(t):
            frames = sum(max(0, min(t * float(rate), b) - a) for a, b in keep)
            return round(min(out_seconds, frames / float(rate) / float(SPEED)), 3)
        for group in ("rows", "edits", "unconfirmed", "unmatched"):
            for entry in proof.get(group, []):
                entry.setdefault("source_time", entry["output_time"])
                entry["output_time"] = output_time(entry["source_time"])
        for decision in decisions:
            decision["output_time"] = output_time(decision["source_start"])
        events = caption_events(timed, limit=columns, end_time=out_seconds)
        write_srt(work / "captions.srt", events)
        write_burn_captions(work / "captions.ass", events, columns)
        proof.setdefault("input_mode", "explicit_reference" if asr_reference else "source_video_only")
        proof["srt_sha256"] = hashlib.sha256((work / "captions.srt").read_bytes()).hexdigest()
        print(f"原片 {duration:.2f} 秒，{float(SPEED)} 倍速成片 {out_seconds:.2f} 秒。正在导出…", flush=True)
        render(source, work, idx, rate, keep, output_frames)

        name = source.stem + "_cut"
        package = work / "result"
        package.mkdir()
        (work / "render.mp4").rename(package / (name + ".mp4"))
        (work / "captions.srt").rename(package / (name + ".srt"))
        report = {"source": source.name, "source_size": source.stat().st_size,
                  "duration": duration, "output_duration": out_seconds,
                  "fps": str(rate), "keep": [[float(a/rate), float(b/rate)] for a, b in keep],
                  "word_edits": edits, "speed": float(SPEED), "cut_decisions": decisions,
                  "source_words": [asdict(w) for w in words],
                  "cut_duration": float(kept_frames / rate), "proofreading": proof, "display_cues": events}
        if cut_report:
            report["replay"] = {"cut_report_sha256": hashlib.sha256(cut_report.read_bytes()).hexdigest(),
                                "restored_cut_ids": list(restore_ids)}
        (package / "剪辑记录.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
        # Publish the completed set together. A repeat run receives its own folder.
        version = 1
        while True:
            destination = root / (name if version == 1 else f"{name}_{version}")
            if destination.exists():
                version += 1
                continue
            try:
                package.rename(destination)
                return destination
            except OSError:
                # Windows can report access denied when another export already
                # owns this directory, including while Explorer is viewing it.
                if not destination.exists():
                    raise
                version += 1


def is_exported_video(video):
    report_path = video.with_name("剪辑记录.json")
    if video.suffix.lower() != ".mp4" or not report_path.is_file():
        return False
    info = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if not isinstance(info, dict) or not isinstance(info.get("source"), str) or "keep" not in info:
        return False
    original = Path(info["source"])
    if video.name == original.name:
        return False
    srt = video.with_suffix(".srt")
    expected_hash = info.get("proofreading", {}).get("srt_sha256")
    canonical_name = video.stem.casefold() == (original.stem + "_cut").casefold()
    actual_hash = hashlib.sha256(srt.read_bytes()).hexdigest() if expected_hash and srt.is_file() else None
    if not canonical_name and not (expected_hash and actual_hash == expected_hash):
        return False
    if not srt.is_file() or (expected_hash and actual_hash != expected_hash):
        raise ValueError("成片的同名 SRT 缺失或与剪辑记录不符，已停止准备发布。")
    _, duration, rate = probe(video)
    if abs(duration - info["output_duration"]) > 1 / float(rate):
        raise ValueError("所选视频时长与剪辑记录不符，已停止准备发布。")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", help="口播原片路径")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "output")
    parser.add_argument("--min-pause", type=float, default=0.35, help="开始检查停顿的秒数（默认 0.35）")
    parser.add_argument("--open-folder", action="store_true", help="完成后在资源管理器中打开成片目录")
    parser.add_argument("--publish-path-file", type=Path, help="为发布入口准备视频，并写回本次成片的绝对路径")
    parser.add_argument("--cut-report", type=Path, help="沿用原片的剪点与自动校对稿，在本机重新导出")
    parser.add_argument("--restore-cut", action="append", default=[], help="恢复指定剪点 ID，可重复使用")
    parser.add_argument("--compact-pauses", action="store_true", default=None,
                        help="复核并收紧静音（新原片默认启用；重导旧剪辑记录时可指定）")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--local-subtitles", action="store_true", help="离线导出，使用本地转写字幕")
    inputs.add_argument("--asr-reference", type=Path, help="读取与本片绑定的本地原声听写记录")
    inputs.add_argument("--deepseek-audio", action="store_true", default=None,
                        help="使用默认的 DeepSeek 原声听写流程")
    args = parser.parse_args()
    if (args.restore_cut and not args.cut_report) or (args.cut_report and
            (args.local_subtitles or args.asr_reference or args.deepseek_audio or args.publish_path_file)):
        parser.error("--restore-cut 需要 --cut-report。使用剪辑记录时只支持本地重导出。")
    try:
        video_path = args.video
        if not video_path:
            if not args.open_folder or args.publish_path_file:
                parser.error("请提供视频路径。")
            try:
                video_path = input("把原视频拖进这个窗口或粘贴文件路径，然后按回车（直接回车取消）：\n").strip()
            except (EOFError, KeyboardInterrupt):
                video_path = ""
            if not video_path:
                print("已取消剪辑。")
                return
            if len(video_path) >= 2 and video_path[0] == video_path[-1] and video_path[0] in "\"'":
                video_path = video_path[1:-1]
        source = Path(video_path).absolute()
        if not source.is_file():
            raise ValueError(f"找不到视频文件：{source}")
        if args.publish_path_file and is_exported_video(source):
            video = source
            destination = video.parent
            print("已核对成片和同名字幕，直接使用所选版本。")
        else:
            destination = export(source, args.output_dir.absolute(), args.min_pause,
                                 args.local_subtitles, args.asr_reference, args.deepseek_audio,
                                 args.cut_report, args.restore_cut, compact_pauses=args.compact_pauses)
            video = destination / (source.stem + "_cut.mp4")
            if args.publish_path_file and not is_exported_video(video):
                raise ValueError("导出的成片未通过核对，已停止准备发布。")
        if args.publish_path_file:
            args.publish_path_file.write_text(str(video), encoding="utf-8")
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"剪口播失败：{exc}\n")
    print(f"成片和字幕已保存到 {destination}")
    if args.open_folder and os.name == "nt":
        os.startfile(destination)
    return destination


if __name__ == "__main__":
    main()
