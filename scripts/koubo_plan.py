"""Build one frame-aligned edit list for both the recording and its subtitles."""

import math
import re
import unicodedata
import wave
from bisect import bisect_left
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from functools import lru_cache

import numpy as np


@dataclass
class Word:
    text: str
    start: float
    end: float
    new_clause: bool
    space: bool
    sentence_start: bool = False


# One-to-one display variants keep the offsets in the original string valid.
_ASR_DISPLAY_FORMS = {
    **dict(zip(range(0xff01, 0xff5f), range(0x21, 0x7f))),
    **str.maketrans("‘’‐‑", "''--"),
}


def timestamp_spans(text, raw_text, count):
    # Paraformer returns its timestamped units space-separated before punctuation.
    # Locate those exact units in the display text so punctuation still marks clauses.
    units = raw_text.split() if isinstance(raw_text, str) else []
    if units:
        if len(units) != count:
            raise ValueError(f"原始转写有 {len(units)} 个词、{count} 个时间戳，无法可靠定位剪点。")
        # Punctuation restoration can change typography or insert spaces within
        # a token. Keep source offsets to recover the actual punctuation between words.
        indices = [i for i, c in enumerate(text) if not c.isspace()]
        display = "".join(text[i] for i in indices).translate(_ASR_DISPLAY_FORMS)
        spans = []
        pos = 0
        for unit in units:
            expected = unit.translate(_ASR_DISPLAY_FORMS).casefold()
            while display[pos:pos + len(unit)].casefold() != expected:
                if pos == len(display) or not unicodedata.category(display[pos]).startswith("P"):
                    raise ValueError(f"转写文字与原始分词不符，第 {len(spans) + 1} 个时间戳应对应 {unit!r}。")
                pos += 1
            spans.append((indices[pos], indices[pos + len(unit) - 1] + 1))
            pos += len(unit)
        if any(not unicodedata.category(c).startswith("P") for c in display[pos:]):
            raise ValueError("转写末尾仍有文字没有对应的时间戳。")
        return spans

    # Older results may contain only the display text and timestamps. Accept a
    # whole-word or character tokenization only when its count matches exactly.
    for pattern in (r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*|[^\s]", r"[A-Za-z0-9]+|[^\s]", r"[^\s]"):
        spans = [m.span() for m in re.finditer(pattern, text)
                 if not unicodedata.category(m[0][0]).startswith("P")]
        if len(spans) == count:
            return spans
    raise ValueError(f"转写文字与时间戳数量不符（{count} 个时间戳），无法可靠定位剪点。")


def read_words(items):
    words = []
    for item in items:
        text = item["text"]
        times = item.get("timestamp", [])
        spans = timestamp_spans(text, item.get("raw_text"), len(times))
        previous = 0
        for (left, right), (start, end) in zip(spans, times):
            gap = text[previous:left]
            start, end = start / 1000, end / 1000
            if not (0 <= start < end and math.isfinite(end)) or (words and start < words[-1].end):
                raise ValueError("转写时间戳重叠或无效，无法可靠定位剪点。")
            # A single recognizer unit always owns one original time interval.
            token = text[left:right]
            if item.get("raw_text"):
                token = "".join(token.translate(_ASR_DISPLAY_FORMS).split())
            words.append(Word(token, start, end, previous == 0 or any(
                unicodedata.category(c).startswith("P") for c in gap), bool(gap and gap[-1].isspace()),
                any(c in "。！？!?；;\n" for c in gap) or gap.rstrip().endswith(".")))
            previous = right
    if not words:
        raise ValueError("没有识别到带时间戳的人声。")
    return words


def nearby_speech_gaps(intervals):
    """Yield nearby groups with at least 1.5 seconds of unmatched speech in 8 seconds."""
    left, total = 0, 0.
    for right, (_, end, seconds) in enumerate(intervals):
        total += seconds
        while left <= right and end - intervals[left][0] > 8:
            total -= intervals[left][2]
            left += 1
        if left < right and total >= 1.5 - 1e-8:
            yield left, right, total


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


def make_plan(words, quiet, duration, fps, min_pause=0.35, pad=0.12, *, decisions=None):
    if not (math.isfinite(min_pause) and min_pause > 0 and math.isfinite(pad) and pad >= 0):
        raise ValueError("停顿阈值必须大于零，留白必须大于等于零。")
    fps = float(fps)
    frames = round(duration * fps)
    if not words:
        raise ValueError("没有可用于决定剪点的原速逐字稿。")
    if frames < 1 or words[-1].end > duration + 1 / fps:
        raise ValueError("转写时间超出视频范围，请核对素材。")
    quiet_ends = [b for _, b in quiet]
    cuts, edits, dropped = [], [], set()
    if decisions is None:
        decisions = []

    def record(left, right, reason, first=None, last=None):
        cuts.append((left, right))
        decisions.append({"id": f"{left}-{right}", "reason": reason,
                          "frames": [left, right], "source_start": left / fps,
                          "source_end": right / fps, "first_word": first, "last_word": last})

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
        record(left, right, reason, first, last)
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
        if w.text in ("嗯", "呃", "啊", "哦", "哎") and w.new_clause and after_clause:
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

    units = list(lexical_units(words))
    boundaries = {right.start: (left, right) for left, right in zip(units, units[1:])}
    gaps = zip([0.0] + [w.end for w in words], [w.start for w in words] + [duration])
    for i, (lo, hi) in enumerate(gaps):
        gap = hi - lo
        if gap < min_pause or i in dropped or i-1 in dropped:
            continue
        margin = pad
        reason = "片头片尾留白"
        if 0 < i < len(words):
            if hi not in boundaries:
                continue  # even a long hesitation inside a word/name stays
            left_word, right_word = boundaries[hi]
            if left_word.text == right_word.text or boundary_quality(left_word.text, right_word.text) < 0:
                continue
            if right_word.sentence_start:
                threshold, margin, reason = 1.10, max(pad, .36), "句末长停顿"
            elif right_word.new_clause:
                threshold, margin, reason = .90, max(pad, .27), "分句长停顿"
            else:
                threshold, margin, reason = 1.60, max(pad, .38), "句内过长停顿"
            if gap < threshold:
                continue
        # Use one continuous quiet interval; breathing, room sounds or
        # untranscribed speech between quiet runs are never spliced together.
        quiet_span = max(quiet_inside(lo, hi), key=lambda span: span[1]-span[0], default=None)
        if quiet_span:
            a, b = quiet_span
            left = math.ceil((a + margin) * fps)
            right = math.floor((b - margin) * fps)
            if i == 0 and a <= .045:
                left = 0
            if i == len(words) and b >= duration - .056:
                right = frames
            if right - left >= max(1, math.ceil(.16 * fps)):
                record(left, right, reason)

    keep = kept_ranges(frames, cuts)
    if not keep or len(dropped) == len(words):
        raise ValueError("素材中没有可保留的口播。")
    return keep, dropped, edits


def kept_ranges(frames, cuts):
    keep, cursor = [], 0
    for left, right in sorted(cuts):
        if not (0 <= left < right <= frames):
            raise ValueError("剪点超出原片时间。")
        if left > cursor:
            keep.append([cursor, left])
        cursor = max(cursor, right)
    if cursor < frames:
        keep.append([cursor, frames])
    return keep


@lru_cache(maxsize=1)
def caption_tagger():
    # FunASR's punctuation model loads a user dictionary into jieba's shared
    # tokenizer. Layout must agree between a fresh export and an offline replay.
    from jieba.posseg import POSTokenizer
    return POSTokenizer()


def display_tokens(text, tagger=None, *, hmm=True):
    """Tokenize once across ASR punctuation; attach titles to the nouns they qualify."""
    import jieba.posseg
    result = []
    previous_tag = ""
    for token, tag in (tagger or jieba.posseg.dt).cut(text, HMM=hmm):
        if (token in {"哥", "姐", "叔", "嫂", "爷", "姨", "小哥", "老师", "师傅", "先生", "女士", "们"}
                and previous_tag.startswith(("n", "eng"))):
            result[-1] += token
        else:
            result.append(token)
        previous_tag = tag
    return result


def display_width(text):
    # One Chinese character occupies approximately two Latin columns.
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in text)


def lexical_units(words, *, caption_columns=None):
    pieces = [(" " if w.space else "") + w.text for w in words]
    text = "".join(pieces)
    offset = 0
    boundaries = set()
    for token in display_tokens(text, caption_tagger() if caption_columns is not None else None):
        offset += len(token)
        boundaries.add(offset)
    if caption_columns is not None:
        # The unknown-word model sees unpunctuated text and can invent a word
        # spanning two spoken clauses. Recover the acoustic seam unless it is
        # inside a dictionary word, a title, or an indivisible timing anchor.
        seams = set()
        position = 0
        for i, (word, piece) in enumerate(zip(words, pieces)):
            if i and (word.new_clause or word.sentence_start or word.start - words[i-1].end >= .30):
                seams.add(position)
            position += len(piece)
        if seams - boundaries:
            position = 0
            for token in display_tokens(text, caption_tagger(), hmm=False):
                position += len(token)
                if position in seams:
                    boundaries.add(position)
        for match in re.finditer(r"[A-Za-z]+(?: +[A-Za-z]+)+", text):
            if display_width(match[0]) <= 2 * caption_columns:
                boundaries.difference_update(range(match.start() + 1, match.end()))
    start = offset = 0
    for i, piece in enumerate(pieces):
        offset += len(piece)
        if offset in boundaries:
            yield replace(words[start], text="".join(pieces[start:i+1]).lstrip(), end=words[i].end)
            start = i + 1


def boundary_quality(left, right):
    if right in ("的", "地", "得", "了", "吗", "呢", "嘛", "啊", "吧", "呀", "们"):
        return -1
    if right in ("但是", "所以", "因为", "而且", "然后", "不过", "如果"):
        return 1
    if left.endswith(("。", "？", "！", "，", "吧", "啊", "吗", "呢", "嘛", "呀")):
        return 1
    if left in ("是", "就是", "因为", "如果", "在", "把", "被", "对", "给", "从", "向", "很", "最", "这个", "那个"):
        return -1
    return 0


def wrap_caption(text, limit=14, source_text=None, *, tokens=None):
    """Newlines within a cue are simultaneous. Never wrap inside a display token."""
    total = display_width(text)
    if total <= 2 * limit:
        return text
    if tokens is None:
        tokens = display_tokens(text)
    # A corrected name can cross dictionary tokens. Treat the replacement as
    # one unit, together with any token overlapping its edges.
    protected = set()
    if source_text is not None and source_text != text:
        for tag, _, _, a, b in SequenceMatcher(None, source_text, text, autojunk=False).get_opcodes():
            if tag in ("insert", "replace") and display_width(text[a:b]) <= 2 * limit:
                protected.update(range(a+1, b))
    for match in re.finditer(r"[A-Za-z]+(?: +[A-Za-z]+)+", text):
        if display_width(match[0]) <= 2 * limit:
            protected.update(range(match.start()+1, match.end()))
    if protected:
        joined, position = [], 0
        for token in tokens:
            if position in protected:
                joined[-1] += token
            else:
                joined.append(token)
            position += len(token)
        tokens = joined
    offset = 0
    choices = []
    for i, token in enumerate(tokens[:-1], 1):
        offset += display_width(token)
        if max(offset, total - offset) <= 2 * limit:
            choices.append((abs(total - 2 * offset) - 6 * boundary_quality(token, tokens[i]), i))
    if choices:
        split = min(choices)[1]
        return "".join(tokens[:split]).rstrip() + "\n" + "".join(tokens[split:]).lstrip()
    lines, line = [], ""
    for token in tokens:
        if line.strip() and display_width(line + token) > 2 * limit:
            lines.append(line.rstrip())
            line = ""
        line += token if line else token.lstrip()
    if line.strip():
        lines.append(line.rstrip())
    return "\n".join(lines)


def review_groups(words, limit=14):
    events = []

    def emit_sentence(sentence):
        pieces = [(" " if w.space else "") + w.text for w in sentence]
        widths = [display_width(piece) for piece in pieces]
        start = 0
        while start < len(sentence):
            # Fit an entire sentence across two simultaneous lines before making
            # a time cut. A long sentence gets a cut at its best clause boundary.
            end, line_width, line_count = start, 0, 1
            while end < len(sentence):
                if widths[end] > 2 * limit:
                    if end == start:
                        end += 1
                    break
                if line_width and line_width + widths[end] > 2 * limit:
                    if line_count == 2:
                        break
                    line_width, line_count = 0, 2
                line_width += widths[end]
                end += 1
            if end < len(sentence):
                def boundary(i):
                    right = sentence[i]
                    quality = boundary_quality(sentence[i-1].text, right.text)
                    gap = right.start - sentence[i-1].end
                    return (int(right.new_clause) + quality, gap >= 0.35, i)
                end = max(range(start+1, end+1), key=boundary)
            events.append({"text": "".join(pieces[start:end]).strip(),
                           "start": sentence[start].start, "end": sentence[end-1].end})
            start = end

    sentence = []
    for w in lexical_units(words):
        if sentence:
            gap = w.start - sentence[-1].end
            if (w.sentence_start or gap >= 1.5) and (gap >= 3 or boundary_quality(sentence[-1].text, w.text) >= 0):
                emit_sentence(sentence)
                sentence = []
        sentence.append(w)
    emit_sentence(sentence)
    return events


def align_reviewed_words(words, events):
    """Carry approved text onto the word clock. A replacement has only its outer time anchors."""
    midpoints = [(w.start + w.end) / 2 for w in words]
    aligned = []
    for event in events:
        start, end, text = event["start"], event["end"], event["text"]
        group = words[bisect_left(midpoints, start):bisect_left(midpoints, end)]
        if not group:  # A recovered line has no word-level acoustic anchors.
            aligned.append(Word(text, start, end, True, False, True))
            continue
        pieces = [(" " if i and w.space else "") + w.text for i, w in enumerate(group)]
        source = "".join(pieces)
        if source == text:
            aligned.extend(group)
            continue
        offsets = {}
        for tag, a, b, c, d in SequenceMatcher(None, source, text, autojunk=False).get_opcodes():
            if tag == "equal":
                offsets.update((a+i, c+i) for i in range(b-a+1))
            elif tag == "delete":
                offsets.update((i, c) for i in range(a, b+1))
            else:
                offsets[b] = d
        # A changed span can end on the space before the next spoken word.
        # That space takes no time, so allow the cut at the preceding word end.
        for position in range(len(source)-1, -1, -1):
            if source[position] == " " and position+1 in offsets:
                offsets.setdefault(position, offsets[position+1])
        original = approved = first = 0
        for i, piece in enumerate(pieces):
            original += len(piece)
            if original not in offsets:
                continue
            stop = offsets[original]
            part = text[approved:stop]
            if part.strip():
                aligned.append(replace(group[first], text=part.lstrip(" "), end=group[i].end,
                                       space=part.startswith(" ") or (first == 0 and group[0].space)))
            approved, first = stop, i+1
    return aligned


def caption_end(start, end, next_start):
    if end - start < 0.6 and next_start is not None:
        return max(end, min(next_start, start + 0.75))
    return end


def caption_events(words, limit=14, *, wrap=False, end_time=None):
    """Fit each cue to one line. Older callers may still pass the wrap keyword."""
    units = list(lexical_units(words, caption_columns=limit))
    pieces = [(" " if w.space else "") + w.text for w in units]
    widths = [display_width(piece) for piece in pieces]
    n = len(units)
    # A cheap boundary is a good place to finish a thought. Keep false ASR
    # stops after an unfinished predicate together, even across a hesitation.
    costs = [0.0] * (n+1)
    stops = {0, n}
    for i in range(1, n):
        left, right = units[i-1], units[i]
        gap = right.start - left.end
        quality = boundary_quality(left.text, right.text)
        if (right.sentence_start or gap >= 1.0) and (quality >= 0 or gap >= 3):
            stops.add(i)
        tail = "".join(w.text for w in units[max(0, i-4):i])
        head = "".join(w.text for w in units[i:i+4])
        unfinished = left.text in ("不", "的", "就", "都", "可以", "不能", "绝对", "一定", "我", "你", "他", "大")
        # Speech recognizers sometimes put a comma between the subject and
        # an A-not-A question. Keep the predicate with its subject.
        if re.match(r"(.{1,2})[不没]\1", head):
            costs[i] = 12
        elif re.search(r"(.{1,2})[不没]\1$", tail) and right.text in ("我", "你", "他", "她", "我们"):
            costs[i] = -4
        elif tail.endswith(("问我", "问他", "问她", "问你", "问道", "说道")):
            costs[i] = -3
        elif left.text == "的" and i >= 2 and head.startswith(units[i-2].text + "的") and gap >= 0.15:
            costs[i] = -3
        elif right.sentence_start and gap >= 0.18 and quality < 0:
            # A pause after an unfinished predicate is a fallback for a long
            # utterance. Keep short subject/predicate pairs on one screen.
            costs[i] = 4
        elif quality < 0 or unfinished:
            costs[i] = 12
        elif right.new_clause:
            costs[i] = -3 if gap >= 0.18 or quality > 0 or left.text == "了" or left.text == right.text else -2
        elif quality > 0:
            costs[i] = -1
        elif gap >= 0.18:
            costs[i] = 2.5
        elif right.text in ("我", "你", "他", "她", "他们", "我们"):
            costs[i] = 3
        else:
            costs[i] = 8

    events = []
    breaks = sorted(stops)
    for begin, end in zip(breaks, breaks[1:]):
        best = {end: 0.0}
        following = {}
        for a in range(end-1, begin-1, -1):
            width = 0
            for b in range(a+1, end+1):
                piece_width = widths[b-1]
                width += piece_width
                # A single indivisible name can be set smaller by the renderer.
                # Every other overflow needs a new timed cue at a word boundary.
                if b > a+1 and width > 2*limit:
                    break
                duration = units[b-1].end - units[a].start
                # Short calls and interjections need less reading time than a
                # full line. Count only the hold that can actually be displayed.
                next_start = units[b].start if b < n else end_time
                dwell = caption_end(units[a].start, units[b-1].end, next_start) - units[a].start
                reading_time = min(0.85, 0.32 + 0.03*width)
                score = 1.5 + max(0.0, reading_time-dwell)*10 + max(0.0, duration-4.0)
                if b < end:
                    score += costs[b]
                score += best[b]
                if a not in best or score < best[a]:
                    best[a], following[a] = score, b
        a = begin
        while a < end:
            b = following[a]
            # A replayed correction can contain an embedded newline.
            text = " ".join("".join(pieces[a:b]).splitlines()).strip()
            events.append({"text": text, "start": units[a].start, "end": units[b-1].end})
            a = b
    for i, current in enumerate(events):
        next_start = events[i+1]["start"] if i+1 < len(events) else end_time
        current["end"] = caption_end(current["start"], current["end"], next_start)
    return events


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
