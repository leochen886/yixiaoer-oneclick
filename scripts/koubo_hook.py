"""Choose one title from the finished transcript and burn it at the approved size."""

import os
import re
from pathlib import Path

from PIL import ImageFont


def extract_hook(transcript, fits):
    """Build a local hook from a spoken question, a contrast or a concrete detail."""
    from jieba.analyse import extract_tags

    clauses = [re.sub(r"(?:[呵哈]{2,}|我去)$", "", s.strip())
               for s in re.split(r"[，。！？；\n]", transcript)]
    for clause in clauses:
        if re.search(r"为什么|怎么|凭什么|到底|难道|[吗呢]$", clause) and fits(clause + "？"):
            return clause + "？"

    topics = extract_tags(transcript, topK=8, allowPOS=("n", "nz", "ns", "nr", "eng"))
    topics.sort(key=transcript.count, reverse=True)
    if len(topics) >= 2 and re.search(r"但是|反而|却|所以|因为|竟然|居然", transcript):
        first, second = sorted(topics[:2], key=transcript.index)
        for title in (f"{first}跟{second}有啥关系？", f"{first}和{second}？"):
            if fits(title):
                return title
    for clause in clauses:
        if re.search(r"竟然|居然|原来|却|反而|[0-9]", clause) and fits(clause):
            return clause
    for topic in topics:
        for title in (topic + "怎么回事？", topic + "咋了？", topic + "？"):
            if fits(title):
                return title
    plain = [s for s in clauses if fits(s)]
    if plain:
        return max(plain, key=lambda s: len(set(s)))
    # Even a recording with just one unusually long token has a usable excerpt.
    excerpt = transcript.splitlines()[0].strip()
    while excerpt and not fits(excerpt + "…"):
        excerpt = excerpt[:-1]
    if not excerpt:
        raise ValueError("画面过窄，无法容纳口播标题。")
    return excerpt + "…"


def prepare_hook(work, words, width, height, *, suggested_title=None):
    # At 1920x1080 these are the exact drawtext settings of the approved preview.
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyhbd.ttc"
    size = max(1, round(height * 100 / 1080))
    border = max(1, round(height * 6 / 1080))
    top = round(height * 40 / 1080)
    font = ImageFont.truetype(str(font_path), size)
    available = width * .94 - 2 * border

    def fits(title):
        return (isinstance(title, str) and bool(title.strip()) and title.isprintable()
                and any(char.isalnum() for char in title)
                and font.getlength(title.strip()) <= available)

    if fits(suggested_title):
        title = suggested_title.strip()
    else:
        transcript = "".join(("\n" if w.sentence_start else "，" if w.new_clause else " " if w.space else "")
                             + w.text for w in words).lstrip("，\n ").strip()
        if not transcript:
            raise ValueError("成片没有可用于生成钩子标题的口播。")
        title = extract_hook(transcript, fits)
    (work / "hook.txt").write_text(title, encoding="utf-8")
    # textfile + expansion=none keeps punctuation and literal % out of filter syntax.
    path = font_path.resolve().as_posix().replace(":", r"\:")
    draw = (f"drawtext=fontfile='{path}':textfile=hook.txt:expansion=none:fontsize={size}:"
            f"fontcolor=0xFFDA20:borderw={border}:bordercolor=black:x=(w-tw)/2:y={top}")
    return title, draw
