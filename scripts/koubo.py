"""本地口播粗剪。可直接运行，也可把视频拖到「剪口播.cmd」。"""

import argparse
import hashlib
import json
import math
import os
import subprocess
import tempfile
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from koubo_plan import (align_reviewed_words, caption_events, make_plan, map_kept_words,
                        quiet_ranges, read_words, review_groups)
from koubo_proofread import MODEL, project_reference, read_reference, review_captions, transcribe_deepseek
from transcribe import transcribe as whisper_transcribe
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


def render(source, work, video_index, fps, keep, output_frames):
    select = "+".join(f"between(n,{a},{b-1})" for a, b in keep)
    # Select CFR video frames once. Rebase their PTS before rendering subtitles.
    style = "FontName=Microsoft YaHei,Bold=1,FontSize=22,Outline=1.2,Shadow=0,MarginV=30"
    video = (f"[0:{video_index}]fps={fps}:start_time=0,select='{select}',setpts=N,"
             f"settb=AVTB,setpts=PTS*{SPEED.denominator}/{SPEED.numerator},fps={fps},"
             f"tpad=stop=2:stop_mode=clone,trim=end_frame={output_frames},"
             f"subtitles=captions.srt:force_style='{style}'[v]")
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


def export(source, root, min_pause, local_subtitles=False, asr_reference=None, deepseek_audio=False):
    if not math.isfinite(min_pause) or min_pause <= 0:
        raise ValueError("停顿阈值必须大于零。")
    idx, duration, rate, columns = probe(source, with_columns=True)
    receipt = read_reference(asr_reference, source)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, prefix=".koubo-") as temp:
        work = Path(temp)
        wav = work / "audio.wav"
        print("正在本地转写口播…", flush=True)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
                        "-map", "0:a:0", "-af", "aresample=16000:async=1:first_pts=0",
                        "-t", str(duration), "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True)
        words = read_words(transcribe(wav, offline=True))
        quiet = quiet_ranges(wav)
        keep, dropped, edits = make_plan(words, quiet, duration, rate, min_pause)
        retained = map_kept_words(words, dropped, keep, float(rate))
        kept_frames = sum(b - a for a, b in keep)
        output_frames = max(1, round(kept_frames / SPEED))
        out_seconds = float(output_frames / rate)
        timed = [replace(w, start=min(w.start / float(SPEED), out_seconds),
                         end=min(w.end / float(SPEED), out_seconds)) for w in retained]
        timed = [w for w in timed if w.start < w.end]
        proof = {"model": None}
        if not local_subtitles:
            events = review_groups(retained, limit=min(14, columns))
            for event in events:
                event["start"] = min(event["start"] / float(SPEED), out_seconds)
                event["end"] = min(event["end"] / float(SPEED), out_seconds)
            events = [e for e in events if round(e["end"]*1000) > round(e["start"]*1000)]
            seconds = [(float(a/rate), float(b/rate)) for a, b in keep]
            def on_output(segments):
                result = project_reference(segments, seconds)
                for row in result:
                    row["start"] = min(row["start"] / float(SPEED), out_seconds)
                    row["end"] = min(row["end"] / float(SPEED), out_seconds)
                return [s for s in result if round(s["end"]*1000) > round(s["start"]*1000)]
            reference = []
            cache = Path(__file__).resolve().parents[1] / "koubo" / "models"
            snapshot = next(cache.glob("models--Systran--faster-whisper-small/snapshots/*/model.bin"), None)
            if snapshot:
                print("正在用本机 Whisper 复听原声…", flush=True)
                try:
                    reference = on_output(whisper_transcribe(wav, cache, str(snapshot.parent), offline=True)["segments"])
                except (ImportError, OSError, RuntimeError) as exc:
                    print(f"本地复听未完成（{type(exc).__name__}），继续校对现有转写。", flush=True)
            if deepseek_audio:
                print("正在使用 DeepSeek 识别本次指定的原片音轨…", flush=True)
                receipt = transcribe_deepseek(wav, quiet)
            print("正在使用 DeepSeek 逐条核对成片字幕…", flush=True)
            events, proof = review_captions(events, reference, receipt,
                                            on_output(receipt.get("local_spot_checks", [])),
                                            on_output(receipt.get("asr_spot_checks", [])),
                                            model=MODEL if deepseek_audio else None)
            if deepseek_audio:
                proof["audio_asr"] = receipt["chunks"]
            proof["reference_asr"] = "faster-whisper-small" if reference else None
            print(f"字幕校对：输入 {proof['prompt_tokens']}、输出 {proof['completion_tokens']} tokens。", flush=True)
            timed = align_reviewed_words(timed, events)
        events = caption_events(timed, limit=columns, wrap=True)
        write_srt(work / "captions.srt", events)
        proof["input_mode"] = "explicit_reference" if asr_reference else "source_video_only"
        proof["srt_sha256"] = hashlib.sha256((work / "captions.srt").read_bytes()).hexdigest()
        print(f"原片 {duration:.2f} 秒，{float(SPEED)} 倍速成片 {out_seconds:.2f} 秒。正在导出…", flush=True)
        render(source, work, idx, rate, keep, output_frames)

        name = source.stem + "_cut"
        package = work / "result"
        package.mkdir()
        (work / "render.mp4").rename(package / (name + ".mp4"))
        (work / "captions.srt").rename(package / (name + ".srt"))
        report = {"source": source.name, "duration": duration, "output_duration": out_seconds,
                  "fps": str(rate), "keep": [[float(a/rate), float(b/rate)] for a, b in keep],
                  "word_edits": edits, "speed": float(SPEED),
                  "cut_duration": float(kept_frames / rate), "proofreading": proof, "display_cues": events}
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
    parser.add_argument("video", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "output")
    parser.add_argument("--min-pause", type=float, default=0.35, help="开始检查停顿的秒数（默认 0.35）")
    parser.add_argument("--open-folder", action="store_true", help="完成后在资源管理器中打开成片目录")
    parser.add_argument("--publish-path-file", type=Path, help="为发布入口准备视频，并写回本次成片的绝对路径")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--local-subtitles", action="store_true", help="离线导出，使用本地转写字幕")
    inputs.add_argument("--asr-reference", type=Path, help="读取与本片绑定的本地原声听写记录")
    inputs.add_argument("--deepseek-audio", action="store_true", help="仅为本次原片发送音轨给 DeepSeek 识别")
    args = parser.parse_args()
    try:
        source = args.video.absolute()
        if args.publish_path_file and is_exported_video(source):
            video = source
            destination = video.parent
            print("已核对成片和同名字幕，直接使用所选版本。")
        else:
            destination = export(source, args.output_dir.absolute(), args.min_pause,
                                 args.local_subtitles, args.asr_reference, args.deepseek_audio)
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
