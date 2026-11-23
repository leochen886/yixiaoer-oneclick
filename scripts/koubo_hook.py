"""Fit the spoken video's question and optional setup into the portrait header."""

import os
import re
import unicodedata
from pathlib import Path

from PIL import ImageFont

from koubo_plan import display_width


HOOK_SIZE = 90
HOOK_WIDTH = 1080 * .94
HOOK_CHAR_LIMIT = 24
FONT_PATH = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyhbd.ttc"
QUESTION = re.compile(
    r"为什么|为何|怎么|怎样|如何|凭什么|什么|哪|谁|几|多少|多久|多[远高中低快慢重贵难早晚大深]"
    r"|何时|何处|能否|是否|([\u3400-\u9fff])不\1|有没有|还是.+|吗$|何以|何必"
)
EMPTY_QUESTIONS = {"你知道吗", "你猜怎么着", "结果呢", "到底怎么回事", "你怎么看", "为什么",
                   "怎么办", "什么问题", "到底是什么", "发生了什么", "这是什么", "到底发生了什么",
                   "有毛病吗", "是吗", "能吗", "你敢信吗", "你懂吗", "懂不懂", "是不是"}
PRICE_CHANGE = re.compile(r"^(.+?(?:涨|跌|降|省|花|等|耗时|用时)(?:了)?)(?:百分之)?[0-9一二三四五六七八九十]+")


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
    return (text not in EMPTY_QUESTIONS and not re.fullmatch(r"(?:这|那)?(?:都)?是?什么(?:鬼)?(?:东西|情况|意思)[呀啊呢]?", text)
            and bool(QUESTION.search(text)))


def hidden_quantity(clause):
    match = PRICE_CHANGE.match(clause)
    if match:
        return match[1] + ("多久" if re.search("等|耗时|用时", match[1]) else "多少")
    return ""


def local_question(transcript, fits):
    """Prefer a complete question, then turn a detail from the recording into a question."""
    from jieba.analyse import extract_tags

    clauses = [clean_line(part) for part in re.split(r"[，。！？；,!?;\n]", transcript)]
    clauses = [c for c in clauses if c]
    topics = extract_tags(transcript, topK=12, allowPOS=("n", "nz", "ns", "nr", "eng", "vn"))
    questions = []
    for clause in clauses:
        question = clause.split("因为", 1)[0]
        if is_question(question) and fits(question):
            questions.append(question)
    for index, clause in enumerate(clauses):
        if index and clause.startswith(("因为", "原因是", "原来是")):
            question = "为什么" + clauses[index - 1]
            if fits(question):
                questions.append(question)
        quantity = hidden_quantity(clause)
        if fits(quantity):
            questions.append(quantity)
        if re.search("反而|偏偏|却", clause) and fits("为什么" + clause):
            questions.append("为什么" + clause)
        if re.search("可以|能够|能自动", clause) and fits(clause + "吗"):
            questions.append(clause + "吗")

    if questions:
        return max(questions, key=lambda q: sum(len(t) for t in topics if t in q))
    for clause in clauses:
        if (any(topic in clause for topic in topics) and fits(clause + "吗")
                and re.search("需要|值得|适合|够用|可行|终于|真的|已经|会|还有", clause)):
            return clause + "吗"
    for topic in [*topics, *clauses]:
        question = "为什么提到" + clean_line(topic)
        if clean_line(topic) and fits(question):
            return question
    # Even a single unusually long word must not overflow or lose part of a name.
    if transcript.strip():
        return "这段话想说什么"
    raise ValueError("成片没有可用于生成钩子标题的口播。")


def draw_line(work, filename, text, size, color, top, border=0):
    (work / filename).write_text(text, encoding="utf-8")
    path = FONT_PATH.resolve().as_posix().replace(":", r"\:")
    return (f"drawtext=fontfile='{path}':textfile={filename}:expansion=none:fontsize={size}:"
            f"fontcolor={color}:borderw={border}:bordercolor=black:x=(w-tw)/2:y={top}")


def prepare_hook(work, words, *, hook_title=None, hook_context=None, hook_evidence=None):
    font = ImageFont.truetype(str(FONT_PATH), HOOK_SIZE)
    def fits(title):
        return bool(title) and display_width(title) <= 2 * HOOK_CHAR_LIMIT and font.getlength(title) <= HOOK_WIDTH

    transcript = "".join(("\n" if w.sentence_start else "，" if w.new_clause else " " if w.space else "")
                         + w.text for w in words)
    def speech_key(value):
        return "".join(c for c in value.casefold() if c.isalnum()) if isinstance(value, str) else ""

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
        title, context = local_question(transcript, fits), ""
        if re.match("这一步|这种|这样|这件|它|那一步|那件", title):
            clauses = [clean_line(c) for c in re.split(r"[，。！？；,!?;\n]", transcript)]
            if title in clauses and clauses.index(title) > 0:
                context = clauses[clauses.index(title) - 1]
    if (context and (context in title or title in context or is_question(context)
                     or (evidence and evidence in speech_key(context))
                     or display_width(context + title) > 2 * HOOK_CHAR_LIMIT
                     or ImageFont.truetype(str(FONT_PATH), 54).getlength(context) > HOOK_WIDTH)):
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
