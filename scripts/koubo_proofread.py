"""Check the actual subtitle cues against the available speech transcripts."""

import hashlib
import io
import json
import os
import re
import time
import unicodedata
import wave
from bisect import bisect_left
from collections import deque
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

import requests


MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"
PROTECTED_FACTS = r"[\d零〇一二两三四五六七八九十百千万亿%％不没无未非]"
PROMPT = """你是中文口播字幕听校员。字幕行和斜线是显示边界。先接起相邻行，连读一句完整的话。读起来通顺、与听写相符的直接保留。
每行依次为编号、待校稿、本地底稿、同期本地听写、原声片段复听。只对听起来别扭的搭配作简短判断：把同音或近音的常用说法（包括口语俗称）代回整句话，看说的是谁或什么、前后搭配是否自然、下一句能否接上。仅仅与话题相关不够。若有一种说法既合读音又明显更通顺，即使各份听写都没写出来，也应改正。只输出逐行结果。
保留原话的意思、说话习惯、实际说出的重复、数字和否定。不要润色或删除口吃。不同编号对应不同发音时间，不要移动正常台词，不要在相邻行把词拆开。
仅在读音和上下文均支持时改字。保留说话人的原意和措辞，不补事实。多个说法仍同样合理时标“疑”。
返回 JSON {"rows":[[1,"核对后的整行文字","原"],[2,"修正后的整行文字","改"],[3,"原句","疑"]]}。每个输入编号必须按顺序返回一次。原和疑必须照录，疑处不要编词。"""


def api_key():
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if value:
        return value
    path = Path.home() / ".claude" / "settings.json"
    if path.is_file():
        env = json.loads(path.read_text(encoding="utf-8-sig")).get("env", {})
        url = urlsplit(env.get("ANTHROPIC_BASE_URL", ""))
        if url.scheme == "https" and url.hostname == "api.deepseek.com":
            value = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
            if value:
                return value.strip()
    raise ValueError("缺少 DeepSeek 密钥，请设置 DEEPSEEK_API_KEY。")



def transcribe_deepseek(wav, quiet):
    key = api_key()
    chunks = []
    with wave.open(str(wav), "rb") as audio:
        rate, total = audio.getframerate(), audio.getnframes()
        start = 0
        while start < total:
            end = min(start + 120 * rate, total)
            if end < total:
                pauses = (round((a+b)*rate/2) for a, b in quiet)
                end = max((p for p in pauses if end-10*rate <= p <= end), default=end)
            pcm = io.BytesIO()
            with wave.open(pcm, "wb") as part:
                part.setparams(audio.getparams())
                part.writeframes(audio.readframes(end-start))
            response = requests.post("https://api.deepseek.com/v1/tools/asr",
                files={"file": ("speech.wav", pcm.getvalue(), "audio/wav")},
                headers={"Authorization": "Bearer " + key}, timeout=(15, 180), allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError(f"DeepSeek 原声音轨识别失败（HTTP {response.status_code}）。")
            data = response.json()
            if not isinstance(data.get("text"), str) or not data.get("request_id"):
                raise ValueError("DeepSeek 没有返回可核实的原声听写记录。")
            chunks.append({"start": start/rate, "end": end/rate, "request_id": data["request_id"], "text": data["text"]})
            start = end
    text = "".join(c["text"] for c in chunks)
    if not text.strip():
        raise ValueError("DeepSeek 没有识别到语音。")
    return {"text": text, "request_id": chunks[0]["request_id"], "chunks": chunks}


def read_reference(path, video):
    """Only use a local ASR receipt that belongs to this exact source."""
    if path is None:
        return {}
    record = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    digest = hashlib.sha256()
    with video.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    if record.get("source_sha256") != digest.hexdigest() or not record.get("text"):
        raise ValueError("原声听写文件与当前视频不匹配。")
    return record


class ReviewTruncated(ValueError):
    def __init__(self, result):
        super().__init__("DeepSeek 字幕校对达到输出上限。")
        self.request_id = result["id"]
        self.usage = result["usage"]


def request_review(content, key, model):
    payload = {"model": model, "thinking": {"type": "enabled"}, "reasoning_effort": "low",
               "response_format": {"type": "json_object"}, "max_tokens": 8192,
               "messages": [{"role": "system", "content": PROMPT},
                            {"role": "user", "content": json.dumps(content, ensure_ascii=False)}]}
    response = requests.post(ENDPOINT, json=payload, headers={"Authorization": "Bearer " + key},
                             timeout=(15, 180), allow_redirects=False)
    if response.status_code != 200:
        raise RuntimeError(f"DeepSeek 字幕校对失败（HTTP {response.status_code}）。")
    result = response.json()
    choice = result["choices"][0]
    reason = choice.get("finish_reason")
    if reason == "length":
        raise ReviewTruncated(result)
    if reason != "stop":
        raise ValueError(f"DeepSeek 返回的字幕核对结果不完整（{reason}，请求 {result.get('id')}）。")
    return json.loads(choice["message"]["content"])["rows"], result["usage"], result["id"]


def plain(text):
    # Read a written percentage the same way as its spoken form.
    def percent(m):
        nums = "零一二三四五六七八九"
        n = int(m[1])
        if n >= 100:
            return m[0]
        tens, ones = divmod(n, 10)
        number = (("" if tens == 1 else nums[tens]) + "十" if tens else "")
        if ones or not tens:
            number += nums[ones]
        return "百分之" + number
    text = re.sub(r"(?<![\d.])([0-9]{1,2})%", percent, unicodedata.normalize("NFKC", text))
    return "".join(c for c in text.casefold()
                   if c.isalnum() or c == "%")


def project_reference(segments, keep):
    """Project only audible portions of source-time references onto the cut."""
    result = []
    for segment in segments:
        projected = []
        elapsed = 0
        for a, b in keep:
            left, right = max(a, segment["start"]), min(b, segment["end"])
            if right > left:
                projected.append((elapsed + left - a, elapsed + right - a))
            elapsed += b - a
        if projected:
            result.append({"start": projected[0][0], "end": projected[-1][1], "text": segment["text"]})
    return result


def add_missing_cues(events, reference, hearing):
    # A second timed transcript may hear a whole line the main ASR never timed.
    # Only fill empty intervals corroborated by the recording-specific ASR.
    result, additions = list(events), []
    sentences = [p for p in re.split(r"[，。！？、,!?；;]+", hearing) if plain(p)]
    if not sentences:
        return result, additions
    for row in reference:
        cursor = row["start"]
        gaps = []
        for e in sorted(result, key=lambda item: item["start"]):
            if e["end"] <= cursor or e["start"] >= row["end"]:
                continue
            if e["start"] > cursor:
                gaps.append((cursor, e["start"]))
            cursor = max(cursor, e["end"])
        if row["end"] > cursor:
            gaps.append((cursor, row["end"]))
        if not gaps:
            continue
        left, right = max(gaps, key=lambda pair: pair[1] - pair[0])
        if right - left < max(0.3, 0.75 * (row["end"] - row["start"])):
            continue
        heard = plain(row["text"])
        if len(heard) < 3:
            continue
        candidates = [(SequenceMatcher(None, heard, plain("".join(sentences[i:i+n])), autojunk=False).ratio(),
                       "".join(sentences[i:i+n])) for i in range(len(sentences)) for n in (1, 2)]
        score, matched = max(candidates)
        if score >= 0.60 and len(plain(matched)) <= 18:
            cue = {"start": left, "end": right, "text": matched.strip()}
            trial = sorted([*result, {**cue, "text": row["text"]}], key=lambda e: e["start"])
            slot = next(i for i, e in enumerate(trial) if e["start"] == left)
            if plain(matched) not in align_hearing(trial, hearing)[1][slot]:
                continue
            result.append(cue)
            additions.append(cue)
    return sorted(result, key=lambda e: e["start"]), additions


def align_hearing(events, hearing):
    """Use matching text anchors to locate each cue within the audio transcript."""
    lines = [plain(e["text"]) for e in events]
    source, target = "".join(lines), plain(hearing)
    offsets = [0] * (len(source) + 1)
    for _, a, b, c, d in SequenceMatcher(None, source, target, autojunk=False).get_opcodes():
        for i in range(a, b+1):
            offsets[i] = c + round((i-a)*(d-c)/(b-a)) if b > a else d
    import jieba
    edges = [0] + [end for _, _, end in jieba.tokenize(target)]
    index = last = 0
    spans, windows = [], []
    for i, line in enumerate(lines):
        stop = index + len(line)
        pos = offsets[stop]
        at = bisect_left(edges, pos)
        # Preserve an exact right-hand phrase anchor before considering a
        # statistical word boundary (e.g. “果 / 真的是”, not “果真 / 的是”).
        anchored = (i + 1 < len(lines) and len(lines[i+1]) >= 3
                    and target.startswith(lines[i+1][:3], pos))
        if edges[at] != pos and not anchored:
            left, right = edges[at-1], edges[at]
            # Give a word crossing an inferred boundary to the preceding line
            # only if that line has a matching anchor before the word.
            anchor = SequenceMatcher(None, line, target[last:left], autojunk=False).find_longest_match()
            pos = right if anchor.size >= 2 else left
        pos = max(last, pos)
        spans.append(target[last:pos])
        windows.append(target[max(0, last-5):pos+5])
        last = pos
        index = stop
    return spans, windows


def compatible(old, new, nearby, hearing):
    """Numbers and polarity may change only with independent speech evidence."""
    normalized = plain(new)
    local = plain(nearby)
    if re.findall("[不没无未非]", old) != re.findall("[不没无未非]", new):
        return False
    if re.findall(PROTECTED_FACTS, plain(old)) != re.findall(PROTECTED_FACTS, normalized):
        if normalized in local and normalized in hearing:
            return True
        numbers = re.findall(r"\d+(?:\.\d+)?%?", new)
        if not (numbers and all(plain(x) in local and plain(x) in hearing for x in numbers)
                and re.findall("[不没无未非]", old) == re.findall("[不没无未非]", new)):
            return False
    if normalized in hearing or normalized in local:
        return True
    # Verify the changed portion in this cue's acoustic context, even when
    # the beginning or end of the caption has different wording.
    original = plain(old)
    for tag, left, right, a, b in SequenceMatcher(None, original, normalized, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            return False
        choices = [normalized[a:b]] if b-a >= 2 else [normalized[max(0,a-1):b], normalized[a:b+1]]
        if any(len(s) >= 2 and (s in hearing or s in local) for s in choices):
            continue
        if tag == "replace":
            from pypinyin import lazy_pinyin
            heard, proposed = original[left:right], normalized[a:b]
            if (not hearing and re.fullmatch(r"[\u3400-\u9fff]+", heard + proposed)
                    and lazy_pinyin(heard) == lazy_pinyin(proposed)):
                continue
            # The other recognizer may have heard a whole phrase correctly
            # while also choosing different characters for the same sounds.
            phrase = normalized[max(0, a-2):min(len(normalized), b+2)]
            syllables = lazy_pinyin(phrase)
            local_syllables = lazy_pinyin(hearing or local)
            if len(syllables) >= 3 and any(
                    local_syllables[i:i+len(syllables)] == syllables
                    for i in range(len(local_syllables) - len(syllables) + 1)):
                continue
        # Two independent readings can support different parts of a compound.
        context = normalized[max(0,a-2):b]
        if hearing and local and len(context) >= 3 and context[:-1] in hearing and context[-1] in local:
            continue
        return False
    return True


def review_tokens(text):
    """Keep written numbers and mixed-case Latin terms intact when comparing ASR readings."""
    import jieba
    protected = r"[A-Za-z][A-Za-z0-9]*(?:[ .-][A-Za-z][A-Za-z0-9]*)*|\d+(?:[.,]\d+)*[%％]?|[零〇一二两三四五六七八九十百千万亿]+"
    tokens = []
    end = 0
    for match in re.finditer(protected, text):
        tokens.extend(jieba.lcut(text[end:match.start()]))
        tokens.append(match[0])
        end = match.end()
    tokens.extend(jieba.lcut(text[end:]))
    return tokens


def apply_review_edits(draft, proposed, status, nearby, hearing):
    """Decide each independently aligned word/phrase against the original acoustic context."""
    if status != "改" or proposed == draft or not proposed:
        return draft, []
    if any(not c.isprintable() or c in "<>{}\\" for c in proposed):
        return draft, []
    old, new = review_tokens(draft), review_tokens(proposed)
    # If an independent transcript already contains the complete proposed phrase,
    # its unchanged neighbours can corroborate a deletion as well as a substitution.
    reading = plain(proposed)
    corroborated = (bool(reading) and (reading in plain(nearby) or reading in plain(hearing))
                    and compatible(draft, proposed, nearby, hearing))
    parts, decisions = [], []
    for op, a, b, c, d in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        before, after = "".join(old[a:b]), "".join(new[c:d])
        if op == "equal":
            parts.append(before)
            continue
        prefix, suffix = "".join(old[:a]), "".join(old[b:])
        candidate = prefix + after + suffix
        same_facts = re.findall(PROTECTED_FACTS, plain(before)) == re.findall(PROTECTED_FACTS, plain(after))
        accepted = (bool(candidate.strip()) and len(after) <= max(len(before) + 6, 18)
                    and ((corroborated and same_facts) or compatible(draft, candidate, nearby, hearing)))
        parts.append(after if accepted else before)
        decisions.append({"start": len(prefix), "before": before, "after": after, "accepted": accepted})
    return "".join(parts), decisions


def review_captions(events, reference, receipt=None, spot_checks=(), acoustic_checks=(), model=None,
                    listen_again=None):
    receipt = receipt or {}
    hearing = receipt.get("text", "")
    key = api_key()
    events, added = add_missing_cues(events, reference, hearing)
    spans, windows = align_hearing(events, hearing) if hearing else ([""] * len(events), [""] * len(events))
    removed = [i for i, e in enumerate(events) if hearing and not spans[i] and any(
        not s["text"].strip() and s["start"] <= e["start"] and e["end"] <= s["end"] for s in acoustic_checks)]
    absent = [events[i] for i in removed]
    events = [e for i, e in enumerate(events) if i not in removed]
    spans = [s for i, s in enumerate(spans) if i not in removed]
    windows = [w for i, w in enumerate(windows) if i not in removed]
    model = model or ("deepseek-v4-pro" if hearing else MODEL)
    checked = []
    report = {"model": model, "requests": 0, "request_ids": [], "checked_cues": 0,
              "prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0,
              "source_asr_request_id": receipt.get("request_id"), "edits": [],
              "recovered_cues": added, "removed_cues": absent, "unconfirmed": [], "rows": [], "unmatched": []}
    def record_request(request_id, usage):
        report["requests"] += 1
        report["request_ids"].append(request_id)
        for field in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens"):
            report[field] += int(usage.get(field, 0))

    full_text = " / ".join(e["text"] for e in events)
    batches = deque((start, min(start+32, len(events)), 0) for start in range(0, len(events), 32))
    while batches:
        start, end, retries = batches.popleft()
        batch = events[start:end]
        inputs = []
        for i, event in enumerate(batch, start + 1):
            local = [s["text"] for s in (*reference, *spot_checks)
                     if s["end"] > event["start"] and s["start"] < event["end"]]
            acoustic = [s["text"] for s in acoustic_checks
                        if s["end"] > event["start"] and s["start"] < event["end"] and s["text"]]
            old = event["text"]
            spoken = spans[i-1]
            # A hearing of the complete source may include material that was
            # cut. Do not paste a long unmatched insertion into a short cue.
            draft = old
            if (spoken and plain(old) != spoken and len(spoken) <= max(18, len(old)+6)
                    and compatible(old, spoken, "".join(local), windows[i-1])):
                draft = spoken
            # A timed source reading can disprove a stray ASR ordinal without
            # changing any actual quantity (e.g. an ordinal before a reaction).
            ordinal = re.match(r"^第[一二三四五六七八九十\d]+", old)
            if (ordinal and spoken and plain(old[ordinal.end():]) == spoken and local
                    and ordinal[0] not in "".join(local) and ordinal[0] not in windows[i-1]):
                draft = spoken
            for edit in receipt.get("previous_edits", []):
                if edit["before"] in old and edit["before"] in draft:
                    draft = draft.replace(edit["before"], edit["after"])
            inputs.append([i, draft, old, local, acoustic])
        context = events[max(0, start-4):start+len(batch)+4]
        content = {"连续底稿": " / ".join(e["text"] for e in context),
                   "另一份连续听写": " / ".join(s["text"] for s in reference
                       if s["end"] > context[0]["start"] and s["start"] < context[-1]["end"]),
                   "字幕行（编号、待校稿、本地底稿、同期本地听写、原声片段复听）": inputs}
        if len(full_text) <= 16000:
            content["全片原速转写（核对跨段术语，不能仅按话题猜词）"] = full_text
        else:
            # Preserve cross-batch spellings without exceeding the model's context.
            terms = re.findall(r"[A-Za-z][A-Za-z0-9]*(?: +[A-Za-z][A-Za-z0-9]*)*", full_text)
            content["全片转写已出现的英文写法"] = list(dict.fromkeys(terms))[:256]
        if hearing:
            content["本条视频的原声听写（含未剪原片内容）"] = hearing
        try:
            rows, usage, request_id = request_review(content, key, model)
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError) as exc:
            if retries == 2:
                raise RuntimeError(f"DeepSeek 第 {start+1}–{end} 条字幕校对仍未收到完整响应"
                                   f"（{type(exc).__name__}: {exc}）。") from exc
            report.setdefault("transport_failures", []).append(
                {"first_cue": start+1, "last_cue": end, "error": type(exc).__name__})
            print(f"DeepSeek 第 {start+1}–{end} 条字幕响应中断，正在重试这部分。", flush=True)
            time.sleep(2 ** retries)
            if len(batch) == 1:
                batches.appendleft((start, end, retries+1))
            else:
                mid = (start + end) // 2
                batches.extendleft([(mid, end, retries+1), (start, mid, retries+1)])
            continue
        except ReviewTruncated as exc:
            record_request(exc.request_id, exc.usage)
            if len(batch) == 1:
                raise
            mid = (start + end) // 2
            batches.extendleft([(mid, end, retries), (start, mid, retries)])
            continue
        if (not isinstance(rows, list) or len(rows) != len(inputs)
                or any(not isinstance(row, list) or len(row) != 3 or type(row[0]) is not int
                       or row[0] != item[0] or row[2] not in ("原", "改", "疑")
                       or not isinstance(row[1], str) for row, item in zip(rows, inputs))):
            raise ValueError("DeepSeek 未逐条返回全部字幕编号，已停止导出。")
        record_request(request_id, usage)
        for (idx, new, status), (_, draft, old, local, acoustic), event in zip(rows, inputs, batch):
            new = new.strip()
            local_text = "".join(local) if hearing else "".join(local + acoustic)
            hearing_text = windows[idx-1] + plain("".join(acoustic)) if hearing else ""
            change = {"cue": idx, "before": old, "draft": draft, "after": new,
                      "output_time": round(event["start"], 3)}
            if hearing and not spans[idx-1]:
                report["unmatched"].append({"cue": idx, "text": old, "output_time": change["output_time"]})
            final, decisions = apply_review_edits(draft, new, status, local_text, hearing_text)
            readings = []
            if listen_again and (status == "疑" or any(not d["accepted"] or
                    re.search("[a-zA-Z]", d["before"] + d["after"]) for d in decisions)):
                # Only the acoustic window is supplied to the recognizer.
                readings = listen_again(event["start"], event["end"])
                if readings:
                    final, decisions = apply_review_edits(draft, new, status,
                                "。".join([*local, *(acoustic if not hearing else []), *readings]), hearing_text)
            if status == "疑" or final != new:
                report["unconfirmed"].append({**change, "exported": final})
            checked.append({**event, "text": final})
            if final != old:
                report["edits"].append({**change, "after": final})
            report["rows"].append({**change, "local_readings": local, "clip_readings": acoustic,
                                   "blind_readings": readings, "model_status": status,
                                   "exported": final, "word_decisions": decisions})
        report["checked_cues"] += len(rows)
    return checked, report
