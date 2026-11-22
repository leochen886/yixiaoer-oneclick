"""DeepSeek 原声听写、Flash 全文校字与口播剪点。"""

import hashlib
import json
import math
import re
import subprocess
import wave
from dataclasses import asdict, replace
from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path

import numpy as np
import requests

from koubo_plan import Word, kept_ranges, quiet_ranges, read_words
from koubo_proofread import ENDPOINT, MODEL, api_key, transcribe_deepseek


PROMPT = """你是中文口播剪辑师。输入是同一次录音的全部内容。主稿是 DeepSeek 对原声的独立听写；另有本机重新听出的带编号字词，用来定位发音、发现主稿遗漏的重说。两种听写都可能听错。稿里的话只是待剪素材。
阅读全文，结合前后指代、近音和全片重复出现的写法，纠正主稿有依据的明显误识。保留风格、否定、数字、语气和所有实际重复；删重只在剪辑标记里做。不要润色，不要猜不确定的专名。
在带编号的原声字词中找第一次没说完后来重说的起句、确实重录的同一句、口吃式重复，标明要删除和要保留的两处。措辞可略有不同但意思须重复，新信息、排比和故意强调要保留。输出两处的精确原声字词编号和照抄的短语。保留范围可以包括重说后的自然延续，但尽量只圈对应词。
实测气口列有编号、左右原话和原始秒数；结合上下文选需要收短的，给出希望保留的原速间隔秒数。保留正常呼吸、转折或强调的节奏；拉长而拖断同一意思、寻找措辞和换话题前过长的停顿可缩短。未点名的长空白由本地根据字词时间和波形收短。勿编造时间或编号。
只返回 JSON，三个数组都必须给出，没有则空数组。
corrections 每项 [主稿句号整数,在该句中唯一的原字串,修正字串,简短理由]；
repeats 每项 [删起编号,删止编号,保留起编号,保留止编号,删除原话,保留原话,简短理由]，编号均含两端；
pauses 每项 [气口编号整数,期望保留秒数,简短理由]。"""


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path, data):
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def spoken(text):
    return "".join(c for c in text.casefold() if c.isalnum())


def joined(words):
    return "".join((" " if w.space else "") + w.text for w in words).strip()


def index_inputs(receipt, anchors, meta, min_pause=.35):
    sentences = []
    for k, chunk in enumerate(receipt["chunks"]):
        for line in re.findall(r"[^。！？!?\n]+[。！？!?]*|[。！？!?]+", chunk["text"]):
            if line.strip():
                sentences.append((k, line.strip()))
    gaps = []
    for i in range(len(anchors) + 1):
        a = anchors[i-1].end if i else 0.
        b = anchors[i].start if i < len(anchors) else meta["duration"]
        if b - a < max(.45, min_pause):
            continue
        spans = [(max(a, x), min(b, y)) for x,y in meta["quiet"] if y > a and x < b]
        if not any(y-x >= .25 for x,y in spans):
            continue
        gaps.append(dict(i=i, a=a, b=b, quiet=spans))
    raw = []
    for i, w in enumerate(anchors):
        if not raw or w.new_clause or len(raw[-1]) >= 22:
            raw.append([])
        raw[-1].append(f"{i+1}:{w.text}")
    content = {"主稿全文（句号、原话）": [[i, s] for i, (_,s) in enumerate(sentences, 1)],
               "本地原声定位（编号:字词，依原片顺序）": [" ".join(row) for row in raw],
               "气口（编号、左侧原话、右侧原话、停顿秒）": [
                    [j, joined(anchors[max(0,g["i"]-8):g["i"]]),
                     joined(anchors[g["i"]:g["i"]+8]), round(g["b"]-g["a"],2)]
                    for j,g in enumerate(gaps, 1)]}
    return sentences, gaps, content


DECISION_TYPES = ("corrections", "repeats", "pauses")


def sum_usage(total, usage):
    for key, value in usage.items():
        if isinstance(value, dict):
            sum_usage(total.setdefault(key, {}), value)
        elif type(value) is int:
            total[key] = total.get(key, 0) + value


def collect_scoped_decisions(request, payload, limits):
    proposal = {key: [] for key in DECISION_TYPES}
    for key in DECISION_TYPES:
        pending = [(1, limits[key])] if limits[key] else []
        while pending:
            first, last = pending.pop()
            scope = f"{key} {first}–{last}"
            instruction = (f"本次只返回 {key} 中第一列编号为 {first} 到 {last}（含）的条目。"
                           "其余数组返回空。保留原始编号，可以引用范围外的上下文和保留的重说。")
            messages = [{"role": "system", "content": PROMPT + "\n" + instruction},
                        payload["messages"][1]]
            batch = request({**payload, "messages": messages}, scope)
            if batch is None:
                if first == last:
                    raise ValueError(f"Flash 在单个决策编号 {scope} 仍返回 length，未生成剪点。")
                mid = (first + last) // 2
                pending.extend([(mid + 1, last), (first, mid)])
                continue
            for row in batch[key]:
                if not isinstance(row, list) or not row or type(row[0]) is not int:
                    raise ValueError(f"Flash 分批决策缺少有效编号（{scope}）。")
                if first <= row[0] <= last:
                    proposal[key].append(row)
    return proposal


def ask_flash(content, records, limits):
    payload = {"model": MODEL, "thinking": {"type":"enabled"}, "reasoning_effort":"low",
               "max_tokens": 16000, "response_format":{"type":"json_object"},
               "messages":[{"role":"system","content":PROMPT},
                           {"role":"user","content":json.dumps(content, ensure_ascii=False)}]}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
    path = records / "flash.json"
    if path.exists():
        result = read(path)
        if result["input_sha256"] != digest:
            raise ValueError("记录中的 Flash 请求与本次盲测输入不同。")
        return result
    save(records / "flash-input.json", payload)
    headers = {"Authorization": "Bearer " + api_key()}
    attempts, usage = [], {}

    def request(body, scope):
        response = requests.post(ENDPOINT, json=body, headers=headers, timeout=(15, 330),
                                 allow_redirects=False)
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        reason = choice["finish_reason"]
        attempts.append({"request_id": data["id"], "model": data["model"], "scope": scope,
                         "thinking": body["thinking"]["type"], "finish_reason": reason,
                         "usage": data["usage"]})
        sum_usage(usage, data["usage"])
        if reason == "length":
            spent = data["usage"].get("completion_tokens")
            reasoning = (data["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens")
            detail = f"，输出 {spent} tokens" if spent is not None else ""
            if reasoning is not None:
                detail += f"，其中思考 {reasoning} tokens"
            print(f"Flash 决策被截断（{scope}{detail}，请求 {data['id']}）。", flush=True)
            return None
        if reason != "stop":
            raise ValueError(f"Flash 未返回完整决策，状态 {reason}（请求 {data['id']}）。")
        proposal = json.loads(choice["message"]["content"])
        if not isinstance(proposal, dict) or not all(isinstance(proposal.get(k), list) for k in DECISION_TYPES):
            raise ValueError(f"Flash 剪辑清单格式不完整（请求 {data['id']}）。")
        return proposal

    proposal = request(payload, "全文")
    if proposal is None:
        # Keep the original transcript, acoustic anchors and prompt. Free the output
        # budget for the decision JSON before resorting to narrower decisions.
        print("沿用本次听写和字词时间，关闭思考模式，重试全文剪辑决策…", flush=True)
        retry = {**payload, "thinking": {"type": "disabled"}}
        del retry["reasoning_effort"]
        proposal = request(retry, "全文")
        if proposal is None:
            print("全文回复再次被截断，按决策类型和编号分批返回，仍提供全文。", flush=True)
            proposal = collect_scoped_decisions(request, retry, limits)
    result = dict(input_sha256=digest, model=attempts[-1]["model"], request_id=attempts[-1]["request_id"],
                  request_ids=[a["request_id"] for a in attempts], attempts=attempts, usage=usage, proposal=proposal)
    save(path, result)
    return result


def apply_corrections(sentences, suggestions):
    reviewed = [s for _,s in sentences]
    audit = []
    for row in suggestions:
        ok = False
        if isinstance(row, list) and len(row) == 4:
            n, orig, new, why = row
            if (type(n) is int and 1 <= n <= len(reviewed) and isinstance(orig,str) and orig
                    and isinstance(new,str) and new.strip() and reviewed[n-1].count(orig) == 1
                    and sentences[n-1][1].count(orig) == 1):
                reviewed[n-1] = reviewed[n-1].replace(orig, new, 1)
                ok = True
        audit.append({"proposal":row,"applied":ok})
    return reviewed, audit


TOKEN = re.compile(r"[A-Za-z0-9]+(?:[.'’%-][A-Za-z0-9]+)*%?|[^\W_]", re.UNICODE)


def clock_transcript(chunks, sentences, reviewed, anchors):
    """Map the cloud transcript onto independent acoustic anchors within each ASR chunk."""
    slots = [[] for _ in anchors]
    for k, chunk in enumerate(chunks):
        ids = [i for i,w in enumerate(anchors) if chunk["start"] <= (w.start+w.end)/2 < chunk["end"]]
        target = []
        for n, (chunk_id, _) in enumerate(sentences):
            if chunk_id != k:
                continue
            last = 0
            for m in TOKEN.finditer(reviewed[n]):
                gap = reviewed[n][last:m.start()]
                target.append((m[0], bool(gap and gap[-1].isspace()), last == 0 or
                               bool(re.search(r"[，,。！？!?：:、；;]", gap)), last == 0))
                last = m.end()
        if not ids:
            if target:
                raise ValueError("一段云端听写没有对应的原声时间。")
            continue
        local = [spoken(anchors[i].text) for i in ids]
        cloud = [spoken(t[0]) for t in target]
        # Matching from the right anchors a collapsed repeated start to its final take.
        ops = SequenceMatcher(None, local[::-1], cloud[::-1], autojunk=False).get_opcodes()
        for _, x, y, u, v in reversed(ops):
            a,b,c,d = len(ids)-y,len(ids)-x,len(cloud)-v,len(cloud)-u
            for j in range(c, d):
                slot = a + (j-c)*(b-a)//(d-c) if b>a else max(0, a-1)
                slots[ids[min(slot,len(ids)-1)]].append(target[j])
    timed, owners = [], []
    for i, tokens in enumerate(slots):
        if not tokens:
            continue
        first, space, clause, sentence = tokens[0]
        text = first + "".join((" " if s else "")+t for t,s,_,_ in tokens[1:])
        timed.append(replace(anchors[i], text=text, space=space, new_clause=clause,
                             sentence_start=sentence))
        owners.append(i)
    expected = "".join(m[0] for s in reviewed for m in TOKEN.finditer(s))
    if "".join("".join(w.text.split()) for w in timed) != expected:
        raise ValueError("云端字幕在绑定原声时间时丢字。")
    return timed, owners


def resolve_span(words, first, last, quote):
    """Match a quote to the numbered local hearing; reject ambiguous occurrences."""
    if type(first) is not int or type(last) is not int or not isinstance(quote,str):
        return None
    start, end, wanted = first-1, last-1, spoken(quote)
    if not wanted or not 0 <= start <= end < len(words):
        return None
    if spoken(joined(words[start:end+1])) == wanted:
        return start, end
    hits = []
    for a in range(max(0,start-7), min(len(words),start+8)):
        text = ""
        for b in range(a, min(len(words), a+len(wanted)+1)):
            text += spoken(words[b].text)
            if text == wanted:
                hits.append((abs(a-start)+abs(b-end),a,b))
            if len(text) >= len(wanted):
                break
    hits.sort()
    if hits and (len(hits) == 1 or hits[0][0] < hits[1][0]):
        return hits[0][1:]
    return None


def choose_seam(samples, fps, lo, hi, desired):
    options = []
    for frame in range(math.ceil(lo*fps-1e-7), math.floor(hi*fps+1e-7)+1):
        pos = round(frame*16000/fps)
        region = samples[max(0,pos-160):pos+160]
        if len(region):
            rms = float(np.sqrt(np.mean(region**2)))
            if rms <= .032:
                options.append((abs(frame/fps-desired) + rms, frame))
    return min(options)[1] if options else None


def pause_trims(gap, target, keep, fps, *, leading, trailing):
    """Spend one trim budget across quiet islands; leave every intervening sound intact."""
    start, end = gap["a"], gap["b"]
    retained = [(max(start, a/fps), min(end, b/fps)) for a,b in keep
                if a/fps < end and b/fps > start]
    remaining = sum(b-a for a,b in retained)
    budget = math.floor((remaining-target)*fps + 1e-7)
    minimum = math.ceil(.12*fps)
    spans = []
    for l,r in gap["quiet"]:
        for a,b in retained:
            left = math.ceil(max(l+.045, a, start if leading else start+.10)*fps-1e-7)
            right = math.floor(min(r-.045, b, end if trailing else end-.10)*fps+1e-7)
            if right-left >= minimum:
                spans.append((left,right))
    cuts = []
    for left,right in sorted(spans, key=lambda pair: pair[1]-pair[0], reverse=True):
        length = min(budget, right-left)
        if length < minimum:
            break
        left += (right-left-length)//2
        cuts.append((left,left+length))
        budget -= length
    return sorted(cuts), remaining


def compact_silences(source, duration, fps, words, keep):
    """Verify quiet independently of ASR boundaries, including inside padded word timestamps."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    pcm = subprocess.check_output([
        "ffmpeg","-nostdin","-v","error","-i",str(source),"-map","0:a:0",
        "-af","aresample=16000:async=1:first_pts=0","-t",str(duration),"-ac","1","-f","s16le","-"])
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    speech = get_speech_timestamps(audio, VadOptions(threshold=.35, min_speech_duration_ms=0,
                                                      min_silence_duration_ms=110, speech_pad_ms=35))
    blocks = audio[:len(audio)//160*160].reshape(-1,160)
    power = np.einsum("ij,ij->i", blocks, blocks) / 160
    quiet = power <= 10**(-36/10)
    if not speech:
        return []  # No independent speech reference for this recording.
    for region in speech:
        quiet[region["start"]//160:(region["end"]+159)//160] = False
    # Retain the strongest part of every word, even for a quiet interjection
    # missed by VAD. A long ASR timestamp no longer protects seconds of silence.
    for w in words:
        a = max(0, math.floor(w.start*100))
        b = min(len(power), math.ceil(w.end*100))
        if a < b:
            peak = a + int(np.argmax(power[a:b]))
            quiet[max(0,peak-4):peak+5] = False
    cuts = []
    edges = np.flatnonzero(np.diff(quiet, prepend=False, append=False)).reshape(-1,2)
    for first,last in edges:
        # Leave 50 ms of quiet at either edge in addition to VAD's speech pad.
        a = math.ceil((int(first)+5)*fps/100)
        b = math.floor((int(last)-5)*fps/100)
        ranges = [(max(a,l),min(b,r)) for l,r in keep if l < b and r > a and a < b]
        if sum(r-l for l,r in ranges) >= math.ceil(.10*fps):
            cuts.extend(ranges)
    return cuts


def select_cuts(anchors, owners, gaps, proposal, meta, wav):
    fps = float(Fraction(meta["fps"]))
    frames = round(meta["duration"] * fps)
    with wave.open(str(wav), "rb") as f:
        if (f.getframerate(), f.getnchannels(), f.getsampwidth()) != (16000,1,2):
            raise ValueError("波形格式与原声时间不符。")
        samples = np.frombuffer(f.readframes(f.getnframes()),dtype="<i2").astype(np.float32)/32768
    cuts, audit, protected = [], [], []

    def record(a,b,reason,first,last,note):
        cuts.append({"id":f"{a}-{b}", "frames":[a,b], "source_start":a/fps, "source_end":b/fps,
                     "first_word":first,"last_word":last,"reason":reason,"model_reason":note})

    for row in proposal["repeats"]:
        item = {"type":"重说","proposal":row,"result":"无法唯一定位两遍原话"}
        audit.append(item)
        if not isinstance(row,list) or len(row)!=7:
            continue
        remove = resolve_span(anchors,row[0],row[1],row[4])
        retain = resolve_span(anchors,row[2],row[3],row[5])
        if not remove or not retain or remove[1] >= retain[0]:
            continue
        i,j = remove
        if (any(not (j<p or i>q) for p,q in protected)
                or any(d["source_end"]>anchors[retain[0]].start and
                       d["source_start"]<anchors[retain[1]].end for d in cuts)):
            item["result"] = "与其他重说的保留位置冲突"
            continue
        lo = anchors[i-1].end if i else 0.
        hi = anchors[j+1].start if j+1<len(anchors) else meta["duration"]
        a = choose_seam(samples,fps,max(lo,anchors[i].start-.22),min(anchors[i].start+.055,anchors[i].end),anchors[i].start-.025)
        b = choose_seam(samples,fps,max(anchors[j].end-.055,anchors[j].start),min(hi,anchors[j].end+.45),min(hi-.12,anchors[j].end+.22))
        cloud_ids = [n for n,o in enumerate(owners) if i<=o<=j]
        if cloud_ids and not any(retain[0]<=o<=retain[1] for o in owners):
            item["result"] = "云端把两遍合成了一遍，字幕位置不够确定"
            continue
        if a is None or b is None or b<=a:
            item["result"] = "两端缺少可分开的低声能剪点"
            continue
        if any(d["frames"][1]>a and d["frames"][0]<b for d in cuts):
            item["result"] = "与已有剪点重叠"
            continue
        record(a,b,"重说",cloud_ids[0] if cloud_ids else None,cloud_ids[-1] if cloud_ids else None,row[-1])
        protected.append(retain)
        item.update(result="已剪", cut_id=cuts[-1]["id"], deleted=joined(anchors[i:j+1]),
                    kept=joined(anchors[retain[0]:retain[1]+1]))

    suggestions = {}
    for row in proposal["pauses"]:
        if (isinstance(row,list) and len(row)==3 and type(row[0]) is int and 1<=row[0]<=len(gaps)
                and type(row[1]) in (int,float) and math.isfinite(row[1]) and row[1]>=0
                and row[0] not in suggestions):
            suggestions[row[0]] = row
        else:
            audit.append({"type":"气口","proposal":row,"result":"标记无效或重复"})

    keep = kept_ranges(frames,[d["frames"] for d in cuts])
    for n,g in enumerate(gaps,1):
        row = suggestions.get(n)
        gap = g["b"]-g["a"]
        leading, trailing = g["i"] == 0, g["i"] == len(anchors)
        edge = leading or trailing
        if not row and gap < (.50 if edge else .65):
            continue
        # All targets are on the original clock. Give sentence transitions more
        # room than within-clause hesitations before the final 1.3x playback.
        if edge:
            target = .25
        elif anchors[g["i"]].new_clause or anchors[g["i"]].sentence_start:
            target = .50
        else:
            target = .36
        if row:
            target = max(.20 if edge else .30, min(row[1], target))
        ranges, remaining = pause_trims(g,target,keep,fps,leading=leading,trailing=trailing)
        item = {"type":"气口","gap_id":n,"source":"Flash" if row else "本地静音",
                "source_gap":round(gap,3),"target":target,"cut_ids":[]}
        if row:
            item["proposal"] = row
        for a,b in ranges:
            record(a,b,"气口",None,None,row[2] if row else "字词之间持续安静")
            item["cut_ids"].append(cuts[-1]["id"])
        if ranges:
            item["cut_id"] = item["cut_ids"][0]
        item.update(result="已剪" if ranges else "可裁静音不足或已达目标",
                    remaining=round(remaining-sum(b-a for a,b in ranges)/fps,3))
        audit.append(item)
    kept_ranges(frames,[d["frames"] for d in cuts])
    return sorted(cuts,key=lambda d:d["frames"]), audit


def build_cut_plan(source, records, duration, rate, *, send_audio, min_pause=.35):
    records.mkdir(exist_ok=True, parents=True)
    source_hash = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            source_hash.update(chunk)
    digest = source_hash.hexdigest()
    wav = records / "原声音轨.wav"
    if (records/"source.json").exists():
        meta = read(records/"source.json")
        if digest != meta["sha256"] or source.name != meta["source"] or not wav.exists():
            raise ValueError("盲测记录与指定原片不匹配。")
    else:
        subprocess.run(["ffmpeg","-y","-nostdin","-v","error","-i",str(source),"-map","0:a:0",
                        "-af","aresample=16000:async=1:first_pts=0","-t",str(duration),"-ac","1",
                        "-c:a","pcm_s16le",str(wav)],check=True)
        meta = {"source":source.name,"sha256":digest,"duration":duration,"fps":str(rate),
                "quiet":quiet_ranges(wav)}
        save(records/"source.json",meta)
    if not (records/"asr.json").is_file():
        if not send_audio:
            raise ValueError("首次云端识别需用 --send-audio 指定本条音轨。")
        print("正在使用 DeepSeek ASR 听写原片音轨…", flush=True)
        save(records/"asr.json",transcribe_deepseek(wav,meta["quiet"]))
    receipt = read(records/"asr.json")
    if not (records/"anchors.json").is_file():
        from transcribe_funasr import transcribe
        print(f"DeepSeek 听写完成（{len(receipt['chunks'])} 段），正在本机定位原声字词与停顿…", flush=True)
        save(records/"anchors.json",[asdict(w) for w in read_words(transcribe(wav,offline=True))])
    anchors = [Word(**w) for w in read(records/"anchors.json")]
    sentences, gaps, content = index_inputs(receipt,anchors,meta,min_pause)
    print(f"正在使用 DeepSeek Flash 核对全文（{len(sentences)} 句），判断重说与气口…", flush=True)
    limits = {"corrections": len(sentences), "repeats": len(anchors), "pauses": len(gaps)}
    flash = ask_flash(content, records, limits)
    proposal = flash["proposal"]
    reviewed, correction_audit = apply_corrections(sentences,proposal["corrections"])
    timed, owners = clock_transcript(receipt["chunks"],sentences,reviewed,anchors)
    cuts, audit = select_cuts(anchors,owners,gaps,proposal,meta,wav)
    proof = {"input_mode":"deepseek_audio_first","model":flash["model"],
             "request_ids":flash.get("request_ids", [flash["request_id"]]),
             "usage":flash["usage"],"audio_asr":receipt["chunks"],
             "input_sha256":flash["input_sha256"],"full_text":reviewed,
             "correction_audit":correction_audit,"cut_audit":audit,"source_sha256":digest}
    if "attempts" in flash:
        proof["flash_attempts"] = flash["attempts"]
    plan = {"source":source.name,"source_size":source.stat().st_size,"fps":str(rate),"duration":duration,
            "cut_decisions":cuts,"source_words":[asdict(w) for w in timed],"proofreading":proof}
    path = records / "原片剪点.json"
    save(path,plan)
    print(f"原声词 {len(anchors)} 个，云端校字 {sum(r['applied'] for r in correction_audit)} 项，"
          f"实际剪点 {len(cuts)} 处。",flush=True)
    return path
