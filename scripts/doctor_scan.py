import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

root = sys.argv[1]
entries = []
for dirpath, _dirnames, filenames in os.walk(root):
    for fn in filenames:
        if fn.endswith(".jsonl"):
            p = os.path.join(dirpath, fn)
            try:
                entries.append((os.path.getmtime(p), p))
            except OSError:
                pass
entries.sort(reverse=True)
entries = entries[:50]

tool_counts = Counter()
skill_counts = Counter()
mcp_counts = Counter()
slash_counts = Counter()
denial_by_target = Counter()
denial_kinds = Counter()
unverified_denials = Counter()
hook_durations = defaultdict(list)
hook_timeouts = Counter()
tool_uses = {}
times = []

def first_words(cmd, n=2):
    parts = str(cmd).strip().split()
    return " ".join(parts[:n])

for mtime, path in entries:
    times.append(mtime)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            typ = obj.get("type")
            if typ == "assistant":
                content = (obj.get("message") or {}).get("content") or []
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            name = item.get("name") or ""
                            inp = item.get("input") or {}
                            tid = item.get("id")
                            if tid:
                                tool_uses[tid] = (name, inp)
                            tool_counts[name] += 1
                            if name == "Skill":
                                skill = inp.get("skill") or inp.get("name") or ""
                                skill_counts[skill] += 1
                            if name.startswith("mcp__"):
                                mcp_counts[name] += 1
            elif typ == "user":
                content = (obj.get("message") or {}).get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        str(x.get("text", "")) for x in content if isinstance(x, dict)
                    )
                for match in re.finditer(r"<command-name>/([^<]+)</command-name>", text):
                    slash_counts[match.group(1)] += 1

                kind = obj.get("toolDenialKind")
                if isinstance(kind, dict):
                    kind = kind.get("kind") or kind.get("type") or ""
                if kind:
                    denial_kinds[str(kind)] += 1
                    if kind in ("interrupted", "cancelled"):
                        continue
                    ids = []
                    if isinstance(content, list):
                        for x in content:
                            if isinstance(x, dict) and x.get("type") == "tool_result":
                                tid = x.get("tool_use_id")
                                if tid:
                                    ids.append(tid)
                    for tid in ids:
                        name, inp = tool_uses.get(tid, ("", {}))
                        if name == "Bash":
                            target = "Bash: " + first_words(inp.get("command", ""))
                        elif name.startswith("mcp__"):
                            target = name
                        else:
                            target = name or "unknown"
                        denial_by_target[target] += 1
                elif isinstance(content, list):
                    for x in content:
                        if not isinstance(x, dict) or x.get("type") != "tool_result":
                            continue
                        if not x.get("is_error"):
                            continue
                        body = str(x.get("content", ""))
                        denial_phrases = (
                            "The user doesn't want to proceed with this tool use",
                            "Permission to use",
                            "Permission for this",
                        )
                        if any(p in body for p in denial_phrases):
                            tid = x.get("tool_use_id")
                            name, inp = tool_uses.get(tid, ("", {}))
                            if name.startswith("mcp__") or not name:
                                continue
                            target = (
                                "Bash: " + first_words(inp.get("command", ""))
                                if name == "Bash"
                                else name
                            )
                            unverified_denials[target] += 1
            elif typ == "attachment":
                att = obj.get("attachment") or {}
                atype = str(att.get("type", ""))
                if atype.startswith("hook_"):
                    key = f"{att.get('hookEvent','?')}:{att.get('hookName','?')}"
                    dur = att.get("durationMs")
                    if isinstance(dur, (int, float)):
                        hook_durations[key].append(float(dur))
                    if att.get("timedOut"):
                        hook_timeouts[key] += 1

def stats(values):
    if not values:
        return None
    return {
        "runs": len(values),
        "median_ms": round(statistics.median(values), 1),
        "max_ms": round(max(values), 1),
    }

summary = {
    "files_scanned": len(entries),
    "window_start": min(times) if times else None,
    "window_end": max(times) if times else None,
    "tool_calls_total": sum(tool_counts.values()),
    "top_tools": tool_counts.most_common(15),
    "skills": skill_counts.most_common(),
    "mcp": mcp_counts.most_common(),
    "slash_commands": slash_counts.most_common(),
    "denials_cli_stamped": denial_by_target.most_common(),
    "denial_kinds": denial_kinds.most_common(),
    "denials_text_matched_unverified": unverified_denials.most_common(),
    "hooks": {k: stats(v) for k, v in hook_durations.items()},
    "hook_timeouts": hook_timeouts.most_common(),
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
