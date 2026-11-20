"""Build one frame-aligned edit list for both the recording and its subtitles."""

import math
import re
import unicodedata
import wave
from bisect import bisect_left
from dataclasses import dataclass, replace

import numpy as np


@dataclass
class Word:
    text: str
    start: float
    end: float
    new_clause: bool
    space: bool


def read_words(items):
    words = []
    for item in items:
        text = item["text"]
        times = item.get("timestamp", [])
        # FunASR usually gives a Chinese character or an English word one stamp.
        for pattern in (r"[A-Za-z0-9]+|[^\s]", r"[^\s]"):
            tokens = [m for m in re.finditer(pattern, text)
                      if unicodedata.category(m[0][0])[0] != "P"]
            if len(tokens) == len(times):
                break
        else:
            raise ValueError("转写文字与时间戳数量不符，无法可靠定位剪点。")
        previous = 0
        for token, (start, end) in zip(tokens, times):
            gap = text[previous:token.start()]
            start, end = start / 1000, end / 1000
            if not (0 <= start < end and math.isfinite(end)) or (words and start < words[-1].end):
                raise ValueError("转写时间戳重叠或无效，无法可靠定位剪点。")
            words.append(Word(token[0], start, end, previous == 0 or any(
                unicodedata.category(c).startswith("P") for c in gap), bool(gap and gap[-1].isspace())))
            previous = token.end()
    if not words:
        raise ValueError("没有识别到带时间戳的人声。")
    return words


def quiet_ranges(wav_path):
    """Return continuous quiet runs. Every 10 ms block must pass the RMS test."""
    with wave.open(str(wav_path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError("剪点分析需要 16 kHz、16 位单声道 WAV。")
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    blocks = samples[:len(samples) // 160 * 160].reshape(-1, 160)
    power = np.einsum("ij,ij->i", blocks, blocks, dtype=np.float64) / 160
    quiet = power <= (32768 * 10 ** (-40 / 20)) ** 2
    edges = np.flatnonzero(np.diff(quiet, prepend=False, append=False))
    return [(a / 100, b / 100) for a, b in zip(edges[::2], edges[1::2])]


def make_plan(words, quiet, duration, fps, min_pause=0.35, pad=0.12):
    if not (math.isfinite(min_pause) and min_pause > 0 and math.isfinite(pad) and pad >= 0):
        raise ValueError("停顿阈值必须大于零，留白必须大于等于零。")
    fps = float(fps)
    frames = round(duration * fps)
    if frames < 1 or words[-1].end > duration + 1 / fps:
        raise ValueError("转写时间超出视频范围，请核对素材。")
    quiet_ends = [b for _, b in quiet]
    cuts, edits, dropped = [], [], set()

    def quiet_inside(lo, hi):
        for i in range(bisect_left(quiet_ends, lo), len(quiet)):
            a, b = quiet[i]
            if a > hi:
                break
            # Keep at least 40 ms of quiet on both sides of a splice.
            yield max(lo, a + 0.045), min(hi, b - 0.045)

    def nearest(i, before):
        w = words[i]
        if before and i == 0 and w.start <= 0.2:
            return 0
        if not before and i == len(words) - 1 and duration - w.end <= 0.2:
            return frames
        if before:
            lo = max(words[i - 1].end if i else 0, w.start - 0.2)
            hi = w.start
        else:
            lo = w.end
            hi = min(words[i + 1].start if i + 1 < len(words) else duration, w.end + 0.2)
        candidates = []
        for a, b in quiet_inside(lo, hi):
            left, right = math.ceil(a * fps), math.floor(b * fps)
            if left <= right:
                candidates.append(right if before else left)
        if candidates:
            return max(candidates) if before else min(candidates)
        return None

    def remove_words(first, last, reason):
        left = nearest(first, True)
        right = nearest(last, False)
        if left is None or right is None or left >= right:
            return False
        cuts.append((left, right))
        dropped.update(range(first, last + 1))
        edits.append({"reason": reason, "text": "".join(w.text for w in words[first:last+1]),
                      "start": left / fps, "end": right / fps})
        return True

    # A short abandoned opening, followed by the same words and a continuation.
    # Repeated complete sentences and different versions stay in the recording.
    clauses = [i for i, w in enumerate(words) if w.new_clause] + [len(words)]
    for first, second, end in zip(clauses, clauses[1:], clauses[2:]):
        prefix = [w.text for w in words[first:second]]
        if prefix[-1] in ("啊", "嗯", "呃"):
            prefix.pop()
        if (3 <= len(prefix) <= 12 and end - second > len(prefix)
                and 0.2 <= words[second].start - words[second-1].end <= 1.2
                and prefix == [w.text for w in words[second:second + len(prefix)]]):
            remove_words(first, second - 1, "重复起句")

    i = 0
    while i < len(words):
        w = words[i]
        after_clause = i + 1 == len(words) or words[i + 1].new_clause
        if i in dropped:
            i += 1
            continue
        if w.text in ("嗯", "呃") or (w.text in ("啊", "哦", "哎") and w.new_clause and after_clause):
            remove_words(i, i, "独立语气词")
        elif w.text in ("我", "你", "他", "她"):
            j = i + 1
            while (j < len(words) and words[j].text == w.text and not words[j].new_clause
                   and words[j].start - words[j - 1].end <= 0.6):
                j += 1
            if j - i >= 3:
                # Preserve the last pronoun with its sentence. Find safe seams
                # within the run when the first repetition is joined to speech.
                left = next((k for k in range(i, j-1) if nearest(k, True) is not None), None)
                if left is not None:
                    for k in range(j-2, left-1, -1):
                        if remove_words(left, k, "卡顿重复"):
                            break
            i = j - 1
        i += 1

    gaps = zip([0.0] + [w.end for w in words], [w.start for w in words] + [duration])
    for lo, hi in gaps:
        if hi - lo < min_pause:
            continue
        for a, b in quiet_inside(lo, hi):
            left = math.ceil((a + pad) * fps)
            right = math.floor((b - pad) * fps)
            if lo == 0 and a <= 0.045:
                left = 0
            if hi == duration and b >= duration - 0.056:
                right = frames
            if right - left >= max(1, math.ceil(0.08 * fps)):
                cuts.append((left, right))

    keep, cursor = [], 0
    for left, right in sorted(cuts):
        if left > cursor:
            keep.append([cursor, left])
        cursor = max(cursor, right)
    if cursor < frames:
        keep.append([cursor, frames])
    if not keep or len(dropped) == len(words):
        raise ValueError("素材中没有可保留的口播。")
    return keep, dropped, edits


def lexical_units(words):
    """Find word boundaries in the whole transcript before considering pauses."""
    import jieba

    pieces = [(" " if w.space else "") + w.text for w in words]
    boundaries = {end for _, _, end in jieba.tokenize("".join(pieces))}
    start = offset = 0
    for i, piece in enumerate(pieces):
        offset += len(piece)
        if offset in boundaries:
            yield replace(words[start], text="".join(pieces[start:i+1]).lstrip(), end=words[i].end)
            start = i + 1


def caption_events(words, limit=14):
    units, lines, size = [], [], 0

    def flush():
        remaining = sum(len(w.text) + int(w.space) for w in units)
        while units:
            target = math.ceil(remaining / math.ceil(remaining / limit))
            # Choose the closest boundary that fits. A single long name stays whole.
            length, choices = 0, []
            for i, w in enumerate(units):
                length += len(w.text) + int(w.space)
                if i and length > limit:
                    break
                choices.append((abs(length - target), i + 1, length))
            _, end, length = min(choices)
            line = units[:end]
            lines.append({"text": "".join((" " if w.space else "") + w.text for w in line).strip(),
                          "start": line[0].start, "end": line[-1].end})
            del units[:end]
            remaining -= length

    for w in lexical_units(words):
        if units:
            gap = w.start - units[-1].end
            if gap >= 1.2 or (size >= 4 and (w.new_clause or gap >= 0.35)):
                flush()
                size = 0
        units.append(w)
        size += len(w.text)
    flush()
    return lines


def map_kept_words(words, dropped, keep, fps):
    """Map the retained words to the exact frame ranges used by the encoder."""
    mapped = []
    segment = elapsed = 0

    for i, w in enumerate(words):
        if i in dropped:
            continue
        while segment + 1 < len(keep) and w.start >= keep[segment][1] / fps:
            elapsed += keep[segment][1] - keep[segment][0]
            segment += 1
        a, b = keep[segment]
        if w.start < a / fps - 1e-6 or (w.end > b / fps + 1e-6 and segment + 1 < len(keep)):
            raise ValueError("剪点落在保留词内，已停止导出。")
        start = (elapsed - a) / fps + w.start
        end = (elapsed - a) / fps + min(w.end, b / fps)
        mapped.append(replace(w, start=start, end=end))
    return mapped


def make_events(words, dropped, keep, fps, limit=14):
    return caption_events(map_kept_words(words, dropped, keep, fps), limit)
