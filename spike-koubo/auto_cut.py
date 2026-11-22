"""Spike: 自动剪口播核心 —— 删口水词 + 剪停顿，纯本地、不用大模型。

输入 FunASR 转写（text + 字级 timestamp），输出：
  keep.json   要保留的片段（原视频时间轴）
  events.json 字幕事件（已映射到剪辑后的时间轴）
"""
import json
import sys

PUNCT = set("，。！？、；：,.!?;")

# 口水词表（可配置）。单字词几乎必是语气词；多字词在口播里多为口头禅。
FILLERS = [
    "就是说", "然后呢", "然后", "就是", "这个", "那个", "其实",
    "嗯", "啊", "呃", "哦", "哎",
]


def remap(t, keep):
    """原始时间 -> 剪辑后时间。"""
    out = 0.0
    for s, e in keep:
        if t <= s:
            return out
        if t < e:
            return out + (t - s)
        out += e - s
    return out


def main():
    transcript_path, keep_out, events_out = sys.argv[1], sys.argv[2], sys.argv[3]
    merge_gap = float(sys.argv[4]) if len(sys.argv) > 4 else 0.35
    pad = float(sys.argv[5]) if len(sys.argv) > 5 else 0.12

    item = json.load(open(transcript_path, encoding="utf-8"))[0]
    text = item["text"]
    ts = item["timestamp"]  # 每个非标点字一个 [start_ms, end_ms]

    # 字 -> 时间戳索引（标点无时间戳）
    char2ts = []
    j = 0
    for c in text:
        if c in PUNCT:
            char2ts.append(None)
        else:
            char2ts.append(j)
            j += 1
    assert j == len(ts), f"对齐失败 {j} != {len(ts)}"

    # 1. 找口水词（按起始位置、长度降序去重：重叠时保留最长）
    matches = []
    for f in FILLERS:
        i = 0
        while True:
            i = text.find(f, i)
            if i < 0:
                break
            matches.append((i, i + len(f), f))
            i += 1
    matches.sort(key=lambda m: (m[0], -(m[1] - m[0])))
    accepted, last_end = [], -1
    for s, e, w in matches:
        if s >= last_end:
            accepted.append((s, e, w))
            last_end = e
    filler_chars = set()
    for s, e, w in accepted:
        for k in range(s, e):
            filler_chars.add(k)

    # 2. 保留区：非标点、非口水词的字区间，间隔 > merge_gap 即切开（停顿）
    intervals = []
    for k, c in enumerate(text):
        if c in PUNCT or k in filler_chars:
            continue
        ti = char2ts[k]
        intervals.append([ts[ti][0] / 1000.0, ts[ti][1] / 1000.0])

    merged = []
    for s, e in intervals:
        if merged and s - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    # 3. 边缘 pad，再合并
    keep_raw = [[max(0.0, s - pad), e + pad] for s, e in merged]
    keep = []
    for s, e in keep_raw:
        if keep and s <= keep[-1][1] + 0.01:
            keep[-1][1] = max(keep[-1][1], e)
        else:
            keep.append([s, e])
    # Padding and merging must not put a removed filler back into the audio.
    excluded = [(ts[char2ts[a]][0] / 1000, ts[char2ts[b-1]][1] / 1000) for a, b, _ in accepted]
    trimmed = []
    for start, end in keep:
        end = min(end, ts[-1][1] / 1000)
        for left, right in excluded:
            if left >= end:
                break
            if right <= start:
                continue
            if start < left:
                trimmed.append([start, left])
            start = max(start, right)
        if start < end:
            trimmed.append([start, end])
    keep = [[round(s, 3), round(e, 3)] for s, e in trimmed]

    # 4. 字幕：跳过口水词，按标点断句，每行最长 14 字，时间映射到剪辑后
    events = []
    buf = []

    def flush():
        if not buf:
            return
        events.append({
            "text": "".join(c for c, _, _ in buf),
            "start": round(remap(buf[0][1], keep), 2),
            "end": round(remap(buf[-1][2], keep), 2),
        })
        buf.clear()

    for k, c in enumerate(text):
        if k in filler_chars:
            continue
        if c in PUNCT:
            flush()
            continue
        ti = char2ts[k]
        buf.append((c, ts[ti][0] / 1000.0, ts[ti][1] / 1000.0))
        if len(buf) >= 14:
            flush()
    flush()

    total = ts[-1][1] / 1000.0
    kept = sum(e - s for s, e in keep)
    json.dump({"keep": keep, "duration": round(total, 3), "cut_saved_s": round(total - kept, 3)},
              open(keep_out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(events, open(events_out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 摘要
    cleaned = "".join(c for k, c in enumerate(text) if k not in filler_chars)
    print("=" * 60)
    print(f"识别口水词 {len(accepted)} 处，保留片段 {len(keep)} 段")
    print(f"原长 {total:.1f}s -> 剪辑后 {kept:.1f}s（剪掉 {total - kept:.1f}s）")
    print("-" * 60)
    for s, e, w in accepted:
        print(f"  删 {s:6.2f}-{e:6.2f}  {w}")
    print("-" * 60)
    print("去口水词后的文本：")
    print("  " + cleaned)
    print("-" * 60)
    print(f"字幕 {len(events)} 条：")
    for ev in events:
        print(f"  [{ev['start']:5.1f}-{ev['end']:5.1f}] {ev['text']}")


if __name__ == "__main__":
    main()
