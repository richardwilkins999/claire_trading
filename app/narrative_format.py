"""Turn what an agent wrote into something a human wants to read.

Two shapes exist on disk: the debaters dump a structured object as JSON, and
the analysts dump their raw tool-loop transcript ("[ai] [{...content blocks}]").
Both are faithful records and both are unreadable. This formats at READ time,
so runs already on disk benefit too — the raw file stays the audit record.
"""
import json
import re

ROLE_LABEL = {"human": "Task", "ai": "", "tool": "Tool result",
              "system": "System"}


def _blocks(content):
    """LangChain content is either a string or a list of content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _render_structured(obj) -> str:
    """A DebateCase / Thesis / report dict as markdown."""
    out = []
    if obj.get("side"):
        out.append(f"**{obj['side'].title()} case** · conviction "
                   f"{obj.get('conviction', '—')}")
    if obj.get("direction"):
        out.append(f"**Verdict: {str(obj['direction']).upper()}** · "
                   f"conviction {obj.get('conviction', '—')}")
        levels = [(k.replace('_', ' '), obj.get(k)) for k in
                  ("entry_low", "entry_high", "stop_loss", "take_profit")]
        levels = [f"{k} {v}" for k, v in levels if v is not None]
        if levels:
            out.append("· ".join(levels))
    for key, title in (("key_points", "Key points"),
                       ("rebuttals", "Rebuttals"),
                       ("conditions", "Conditions & caveats"),
                       ("sources", "Sources")):
        items = obj.get(key) or []
        if items:
            out.append(f"\n**{title}**")
            out.extend(f"- {i}" for i in items)
    if obj.get("summary"):
        out.append(f"\n{obj['summary']}")
    return "\n".join(out) if out else json.dumps(obj, indent=2)


def _render_transcript(body: str) -> str:
    """`[role] content` lines from a tool loop → readable narrative."""
    out, step = [], 0
    for line in body.splitlines():
        m = re.match(r"^\[(\w+)\]\s?(.*)$", line, re.S)
        if not m:
            if line.strip():
                out.append(line)
            continue
        role, content = m.group(1), m.group(2)
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError):
            parsed = content
        if role == "human":
            out.append(f"**Task** — {parsed if isinstance(parsed, str) else content}")
            continue
        if role == "tool":
            text = content if isinstance(parsed, str) else json.dumps(parsed)
            out.append(f"  ↳ _result:_ {text[:300]}"
                       + ("…" if len(text) > 300 else ""))
            continue
        for b in _blocks(parsed):
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append(b["text"].strip())
            elif b.get("type") == "tool_use":
                step += 1
                args = b.get("input") or {}
                shown = ", ".join(
                    f"{k}={str(v)[:60]}" for k, v in list(args.items())[:3])
                out.append(f"\n**{step}. called `{b.get('name')}`** "
                           f"({shown})")
    return "\n\n".join(out)


def format_narrative(text: str) -> str:
    if not text:
        return ""
    header, _, body = text.partition("\n")
    header = header if header.startswith("#") else ""
    body = body.strip() or text.strip()
    stripped = body.lstrip()
    if stripped.startswith("{"):
        try:
            return (header + "\n\n" if header else "") + \
                _render_structured(json.loads(stripped))
        except ValueError:
            pass
    return (header + "\n\n" if header else "") + _render_transcript(body)
