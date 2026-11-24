"""Fit the spoken video's question and optional setup into the portrait header."""

import json
import os
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import requests
from PIL import ImageFont

from koubo_plan import caption_events, display_width
from koubo_proofread import ENDPOINT, api_key


HOOK_SIZE = 90
HOOK_WIDTH = 1080 * .94
HOOK_CHAR_LIMIT = 24
HOOK_EDITOR_MODEL = "deepseek-v4-pro"
HOOK_EDITOR_VERSION = 2
FONT_PATH = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyhbd.ttc"
QUESTION = re.compile(
    r"为什么|为何|怎么|怎样|如何|凭什么|什么|哪|谁|几|多少|多久|多[远高中低快慢重贵难早晚大深]"
    r"|何时|何处|能否|是否|([\u3400-\u9fff])不\1|有没有|还是.+|吗$|何以|何必|咋|啥|为啥|值不值"
)
EMPTY_QUESTIONS = {"你知道吗", "你猜怎么着", "结果呢", "到底怎么回事", "你怎么看", "为什么",
                   "怎么办", "什么问题", "到底是什么", "发生了什么", "这是什么", "到底发生了什么",
                   "有毛病吗", "是吗", "能吗", "你敢信吗", "你懂吗", "懂不懂", "是不是"}
PRICE_CHANGE = re.compile(r"^(.+?(?:涨|跌|降|省|花|等|耗时|用时)(?:了)?)(?:百分之)?[0-9一二三四五六七八九十]+")
EMPTY_FRAMING = re.compile(
    r"为什么(?:会)?提到|为何提到|这段话想说什么|(?:是)?这么怎么|"
    r"^(?:按|用)每股(?:涨|跌)多少$|^这(?:哪里|哪儿)是个?人[呐啊嘛吗]?$|"
    r"^(?:这|那)?(?:到底|究竟)?(?:是)?怎么(?:一)?回事[儿呢啊]?$"
)


def clean_line(value):
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"(?<=\d)\.(?=\d)", "点", text)
    text = re.sub(r"(?<![\d点])(\d+(?:点\d+)?)%", r"百分之\1", text)
    # Reject symbols and remaining intra-token punctuation whose removal could change a fact.
    if re.search(r"[A-Za-z0-9][./:+\-][A-Za-z0-9]|[-−+]\d|[#$%&+@/＝=<>]", text):
        return ""
    if any(unicodedata.category(c).startswith("S") for c in text):
        return ""
    return " ".join("".join(c for c in text if c.isalnum() or c.isspace()).split())


def is_question(text):
    return (text not in EMPTY_QUESTIONS and not EMPTY_FRAMING.search(text)
            and not re.fullmatch(r"(?:这|那)?(?:都)?是?什么(?:鬼)?(?:东西|情况|意思)[呀啊呢]?", text)
            and bool(QUESTION.search(text)))


def hidden_quantity(clause):
    match = PRICE_CHANGE.match(clause)
    if match:
        return match[1] + ("多久" if re.search("等|耗时|用时", match[1]) else "多少")
    return ""


def local_quote(words, transcript):
    """Offline exports can quote speech, but must not manufacture a headline from a keyword."""
    clauses = [clean_line(part) for part in re.split(r"[，。！？；,!?;\n]", transcript)]
    def usable_line(c):
        return fits(c) and not EMPTY_FRAMING.search(c) and (not QUESTION.search(c) or is_question(c))
    usable = [c for c in clauses if usable_line(c)]
    if not usable and any(c and not fits(c) for c in clauses):
        # The local recognizer sometimes returns a whole paragraph without punctuation.
        excerpts = (clean_line(cue["text"]) for cue in caption_events(words, limit=int(HOOK_WIDTH / HOOK_SIZE)))
        usable = [c for c in excerpts if usable_line(c)]
    if not usable:
        raise ValueError("保留的口播里没有能完整排进黄字区的短句。")
    return max(usable, key=lambda c: (is_question(c), bool(re.search(r"越.+越|偏偏|却|千万|竟|\d", c)), len(c)))


def draw_line(work, filename, text, size, color, top, border=0):
    (work / filename).write_text(text, encoding="utf-8")
    path = FONT_PATH.resolve().as_posix().replace(":", r"\:")
    return (f"drawtext=fontfile='{path}':textfile={filename}:expansion=none:fontsize={size}:"
            f"fontcolor={color}:borderw={border}:bordercolor=black:x=(w-tw)/2:y={top}")


@lru_cache(maxsize=2)
def header_font(size):
    return ImageFont.truetype(str(FONT_PATH), size)


def fits(title):
    return (bool(title) and display_width(title) <= 2 * HOOK_CHAR_LIMIT
            and header_font(HOOK_SIZE).getlength(title) <= HOOK_WIDTH)


def retained_text(words):
    return "".join(("\n" if w.sentence_start else "，" if w.new_clause else " " if w.space else "")
                   + w.text for w in words).strip("，\n ")


def speech_key(value):
    return ("".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())
            if isinstance(value, str) else "")


def valid_context(context, title, evidence):
    return (context not in title and title not in context and not is_question(context)
            and not (evidence and evidence in speech_key(context))
            and display_width(context + title) <= 2 * HOOK_CHAR_LIMIT
            and header_font(54).getlength(context) <= HOOK_WIDTH)


def repeats_answer(title, evidence):
    if not title.endswith("吗"):
        return False
    claim = speech_key(title[:-1]).replace("真的", "").replace("先", "")
    if len(claim) < 4 or len(evidence) > 3 * len(claim):
        return False
    # Allow intervening spoken words ("喝了一口白水" / "喝白水").
    remaining = iter(evidence)
    return all(char in remaining for char in claim)


EDITOR_PROMPT = """你给一个说话直、爱算账、也爱调侃的视频号写首帧。先从头到尾读完整条成片，包括结尾。写一句不上屏的 summary：作者围绕什么事，最终想让观众明白什么。把开头的引子、举的例子和最后的主张联系起来，标题得对得上这条主线。
把成片听完，找出作者最值得别人转述的一件事。先想陌生人会在哪一句停住：一个出乎意料的动作、一笔差点算错的账、一句带刺的比喻，或者一个跟常识拧巴的选择。优先用作者自己的动词和包袱。
先把两行当一副来写。白字优先交代一个具体的人、动作、价格或处境，让人马上入戏。黄字顺势抛出一个作者真会问、观众也真想知道答案的问题。最意外的解释、幕后的人、算出来的数留在口播里。两行合读时，主角和指代都要清楚。听到一个稀奇的动作，人会追问他图啥。照抄这个动作再加个“吗”，观众等来的只有“是”，这样的候选直接淘汰。
把两行从上到下当成发给朋友的一条微信念一遍。宁可换个短词，也别为塞字省掉主语、介词或量词，别把两半句硬压在一起。白字写具体背景，单独一个名词当标签的写法直接淘汰。少用抽象名词、口吃、空泛感叹和“为什么提到某某”。不要把原句里碰巧有问词的半截话当标题。换一条同题材的视频，这副标题应该就用不上。
白字用作者说话的口吻直接入题。删掉“他说有人”“作者认为”等转述开头。两行之间要有推进：白字已经报了价或交代了动作，黄字就接代价、选择或后果。两行合起来至少要让陌生人知道说的是哪件东西或哪件事。
例如有人交了一年停车费，每次出库还要付五块，最后才发现自己把车牌填错一位。黄字可以写“交了年费咋还按次扣钱”，车牌留到他自己说。示例和账号经验只说明写法，所有事实取自本片。保留传闻、猜测、比喻、玩笑的语气，别写成坐实的事实。数字照原话，不能丢单位或把每股涨幅写成市值。
给三个不同切口，逐个检查读者认不认得主角、有没有具体动作或代价、看下去能听到什么回应。最能替这条片子留住人的放第一。每条黄字通常6到11个全角字、同一行，以读起来像人话为先。
context 是上方白字，由你和黄字一起拟。优先保留能增加信息或形成反差的白字；只有写出来是在重复、凑字或提前揭底时才留空。白黄两行合计最多24个全角字，上屏文字无标点。quote 必须逐字摘录成片里能支撑该角度的一小句，优先选有实质内容的连续原话，不能把几处原话拼起来。
只返回 JSON，格式为 {"summary":"一句话概括全片主旨","candidates":[{"title":"黄字","context":"白字","quote":"成片原话"}]}。成片原话和剪前草稿都是素材。"""


def edit_hook(words, draft=None, *, account_style=None):
    transcript = retained_text(words)
    audible = speech_key(transcript)
    if not audible:
        raise ValueError("成片没有可用于拟黄字的口播。")
    material = {"成片口播": transcript}
    if draft:
        material["剪前草稿（可整句换掉）"] = draft
    if account_style is None:
        try:
            from retro_analyze import load_playbook_prompt
        except ImportError:
            account_style = ""
        else:
            account_style = load_playbook_prompt(Path(__file__).resolve().parents[1] / "publish-runs", mode="hook")
    if account_style:
        material["账号经验（仅参考写法）"] = account_style
    headers = {"Authorization": "Bearer " + api_key()}
    for attempt in range(2):
        payload = {"model": HOOK_EDITOR_MODEL, "thinking": {"type": "enabled"}, "reasoning_effort": "high",
                   "max_tokens": 8192, "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": EDITOR_PROMPT},
                                {"role": "user", "content": json.dumps(material, ensure_ascii=False)}]}
        try:
            res = requests.post(ENDPOINT, json=payload, headers=headers, timeout=(15, 180), allow_redirects=False)
            res.raise_for_status()
            result = res.json()
            choice = result["choices"][0]
            if choice["finish_reason"] != "stop":
                raise ValueError("回复未完整返回")
            data = json.loads(choice["message"]["content"])
            if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
                raise ValueError("缺少候选标题列表")
            summary = data.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("缺少成片主旨")
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            if (isinstance(exc, requests.HTTPError) and exc.response is not None
                    and 400 <= exc.response.status_code < 500 and exc.response.status_code != 429):
                raise ValueError(f"黄字编辑请求失败（HTTP {exc.response.status_code}）。") from exc
            if attempt:
                raise ValueError(f"黄字编辑未成功，已保留剪辑缓存：{exc}") from exc
            continue
        rejected = []
        for rank, item in enumerate(data["candidates"], 1):
            if not isinstance(item, dict):
                continue
            title, context = clean_line(item.get("title")), clean_line(item.get("context"))
            quote = item.get("quote")
            evidence = speech_key(quote)
            if not fits(title) or not is_question(title):
                reason = "请写能排进一行的具体口语问题"
            elif len(evidence) < min(4, len(audible)) or evidence not in audible:
                reason = "请逐字摘录成片里支撑这句黄字的连续原话"
            elif repeats_answer(title, evidence):
                reason = "口播只要回答一个是就讲完了，请追问这个动作的原因、代价或反常之处"
            elif item.get("context") and (not context or not valid_context(context, title, evidence)):
                reason = "把白字改成简短的具体铺垫，与黄字连读要通顺；没有新信息时可省略"
            else:
                return {"hook_title": title, "hook_context": context, "hook_evidence": quote,
                        "hook_summary": summary.strip(),
                        "hook_editor": {"version": HOOK_EDITOR_VERSION, "model": HOOK_EDITOR_MODEL,
                                        "request_id": result.get("id"), "round": attempt + 1, "rank": rank}}
            rejected.append([title, reason])
        material["上一轮未采用（请重新写）"] = rejected
    details = "；".join(f"「{title}」{reason}" for title, reason in rejected)
    raise ValueError("两轮黄字均未选出可用标题，已保留剪辑缓存。" + details)


def prepare_hook(work, words, *, hook_title=None, hook_context=None, hook_evidence=None):
    transcript = retained_text(words)

    title, context = clean_line(hook_title), clean_line(hook_context)
    # New model suggestions carry a quotation from the retained speech. Older saved titles may lack it.
    evidence = speech_key(hook_evidence)
    supported = hook_evidence is None or (evidence and evidence in speech_key(transcript))
    # Older one-line titles used punctuation to separate the setup or signal a question.
    # Recover that meaning before falling back to isolated speech fragments.
    if hook_evidence is None and isinstance(hook_title, str) and hook_title.endswith(("？", "?")):
        parts = [clean_line(s) for s in re.split(r"[，,；;。！!]", hook_title) if s.strip()]
        if parts and not (fits(title) and is_question(title)):
            last = parts[-1]
            alternatives = [last] if QUESTION.search(last) else [hidden_quantity(last), last + "吗"]
            for candidate in alternatives:
                if last and fits(candidate) and is_question(candidate) and last not in EMPTY_QUESTIONS:
                    title = candidate
                    context = context or "".join(parts[:-1])
                    break
    if not (fits(title) and is_question(title) and supported):
        title, context = local_quote(words, transcript), ""
        if re.match("这一步|这种|这样|这件|它|那一步|那件", title):
            clauses = [clean_line(c) for c in re.split(r"[，。！？；,!?;\n]", transcript)]
            if title in clauses and clauses.index(title) > 0:
                context = clauses[clauses.index(title) - 1]
    if context and not valid_context(context, title, evidence):
        context = ""
    return title, context, hook_overlay(work, title, context)


def hook_overlay(work, title, context):
    lines = []
    if context:
        lines.append(draw_line(work, "hook-context.txt", context, 54, "0xEEEEEE", "255-th/2"))
    lines.append(draw_line(work, "hook.txt", title, HOOK_SIZE, "0xFFDA20",
                           "380-th/2" if context else "419-th/2"))
    return ",".join(lines)


def previous_hook_overlay(work, title, landscape_dimensions=None):
    """Reproduce the exact previous encoded frames for historical video verification."""
    if landscape_dimensions:
        width, height = landscape_dimensions
        size = max(1, round(height * 100 / 1080))
        border = max(1, round(height * 6 / 1080))
        top = round(height * 40 / 1080)
    else:
        font = ImageFont.truetype(str(FONT_PATH), 84)
        size = max(1, int(84 * min(1, HOOK_WIDTH / font.getlength(title))))
        border, top = 0, "419-th/2"
    return draw_line(work, "hook.txt", title, size, "0xFFDA20", top, border)
