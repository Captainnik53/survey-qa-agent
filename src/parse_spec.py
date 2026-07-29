"""
FR1 — Spec parser.

Turns a Decipher-style questionnaire (.docx) into a structured, per-question JSON model
the checks can reason over. Deterministic-first: no LLM needed for the fields the 5
target-bug checks rely on (options+order, anchor, validation ranges, carry-forward,
strike-through). LLM refinement of fuzzy base-conditions can be layered on later.

Per-question schema:
{
  "id": "S_ProcType",
  "text": "clean question text (struck-through removed = the INTENDED wording)",
  "text_struck": ["if any, "],        # deleted fragments that must NOT appear live
  "type": "multi|single|numeric|text|grid|unknown",
  "order": "as_listed|randomize|alphabetical",
  "options": [{"label": "...", "terminate": false, "anchor": false}, ...],
  "validation": {"min": 1, "max": 19000000} | {"format": "phone"} | {},
  "carry_forward": "Q_EVER" | null,   # displayed options should be a subset of this Q's answer
  "ask_condition": "S_ROLE=8" | null
}
"""
import json
import re
import sys
import zipfile
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# A line starts a new question when it begins with a QID token (S_x, Q_x, HQx).
QID_RE = re.compile(r"^(S_\s?[A-Za-z][\w]*|Q_[A-Za-z][\w]*|HQ[A-Z]+)\b\.?\s*")

# Control / instruction lines that are never question text or answer options.
# NOTE: the trailing \b is essential — without it, bare tokens like END/START/USE match
# option text (e.g. "END" matched "Endpoint", silently dropping that answer option).
INSTRUCTION_RE = re.compile(
    r"^\s*(PROGRAMMER|TERMINATE|ASK|PIPE|SHOW STANDARD|SHOW|IF|ANCHOR|RANDOMIZE|RANDOM|"
    r"CLASSIFY|CALCULATE|STORE|CREATE|REPEAT|NOTE|SCREENER|MAIN SURVEY|END|START|DISPLAY|"
    r"WHEN PRODUCT|USE|Lookup|GRAND TOTAL|Variables|FOR|WITHIN|PRIORITIZE)\b",
    re.IGNORECASE,
)


def _is_qid_header(rest):
    """A real question header is the QID alone, or QID followed by question text — NOT a
    QID referenced inside an ALL-CAPS instruction line (e.g. 'Q_VW_TC MUST BE LESS THAN')."""
    if not rest:
        return True
    first = rest.split()[0]
    return not re.fullmatch(r"[A-Z]{2,}", first)  # reject MUST / ASK / AND / PROGRAMMER ...


def _is_marker_line(line):
    """True for pure control tokens like '[Multi Response]' or '[Rows]' — but NOT for
    question text that merely starts with a bracketed pipe alternate like
    '[In addition to Axonius, which / Which], if any, of the following ...'."""
    if not line.startswith("["):
        return False
    residue = re.sub(r"\[[^\]]*\]", "", line).strip(" ,.-\t")
    return residue == ""


def _extract_paragraphs(docx_path):
    """Yield (clean_text, struck_fragments) per paragraph, preserving strike-through."""
    z = zipfile.ZipFile(docx_path)
    root = ET.fromstring(z.read("word/document.xml"))
    for p in root.iter(W + "p"):
        clean, struck = "", []
        for r in p.iter(W + "r"):
            rpr = r.find(W + "rPr")
            is_struck = rpr is not None and (
                rpr.find(W + "strike") is not None or rpr.find(W + "dstrike") is not None
            )
            txt = "".join(t.text or "" for t in r.iter(W + "t"))
            if not txt:
                continue
            if is_struck:
                struck.append(txt)
            else:
                clean += txt
        yield clean.strip(), struck


def _detect_type(markers):
    m = " ".join(markers).lower()
    if "column" in m:  # rows + columns => grid
        return "grid"
    if "multi" in m:
        return "multi"
    if "single" in m:
        return "single"
    if "numeric" in m:
        return "numeric"
    if "open" in m or "specify" in m:
        return "text"
    return "unknown"


def _detect_order(markers):
    m = " ".join(markers).lower()
    if "random" in m:
        return "randomize"
    if "alphabetic" in m:
        return "alphabetical"
    return "as_listed"


def _is_option_line(line):
    if not line or QID_RE.match(line) or line.startswith("["):
        return False
    if INSTRUCTION_RE.match(line):
        return False
    if re.fullmatch(r"[\d\s\-–]+", line):  # bare scale markers
        return False
    return True


def _make_option(line):
    low = line.lower()
    terminate = "[terminate]" in low or "<terminate" in low
    label = re.sub(r"\[[^\]]*\]|<[^>]*>", "", line).strip(" .\t")
    anchor = bool(re.match(r"^(other|none of the above)\b", label, re.IGNORECASE)) or (
        "[anchor]" in low
    )
    return {"label": label, "terminate": terminate, "anchor": anchor}


def _apply_inline_rules(cur, clean):
    """Pull validation ranges / phone format / carry-forward out of any line."""
    mrange = re.search(r"min\s*([\d,]+).*?max\s*([\d,]+)", clean, re.IGNORECASE)
    if mrange:
        cur["validation"]["min"] = int(mrange.group(1).replace(",", ""))
        cur["validation"]["max"] = int(mrange.group(2).replace(",", ""))
    if re.search(r"phone\s*number", clean, re.IGNORECASE):
        cur["validation"]["format"] = "phone"
    mp = re.match(r"^PIPE\s+([A-Z][\w]*)", clean, re.IGNORECASE)
    if mp:
        cur["carry_forward"] = mp.group(1)


def parse(docx_path):
    paras = list(_extract_paragraphs(docx_path))
    questions = []
    cur = None
    pending_ask = None
    awaiting_text = False

    for clean, struck in paras:
        if not clean:
            continue

        mqid = QID_RE.match(clean)
        if mqid and _is_qid_header(clean[mqid.end():].strip()):
            if cur:
                questions.append(cur)
            qid = re.sub(r"\s+", "", mqid.group(1)).rstrip(".")
            rest = clean[mqid.end():].strip()
            cur = {
                "id": qid,
                "text": rest,
                "text_struck": list(struck),
                "markers": [],
                "type": "unknown",
                "order": "as_listed",
                "options": [],
                "validation": {},
                "carry_forward": None,
                "ask_condition": pending_ask,
            }
            pending_ask = None
            awaiting_text = not rest  # QID alone on its line => text is the next paragraph
            continue

        # ASK-IF conventionally PRECEDES the question it gates, so it applies to the NEXT
        # question header — not the block we're currently inside.
        mask = re.match(r"^ASK\s+IF\s+(.+)$", clean, re.IGNORECASE)
        if mask:
            pending_ask = mask.group(1).strip()
            continue

        if cur is None:
            continue

        # First real content line after a bare QID = the question text (+ its strike-through),
        # even when it starts with a bracketed pipe alternate like '[In addition ... / Which]'.
        if awaiting_text and not _is_marker_line(clean) and not INSTRUCTION_RE.match(clean):
            cur["text"] = clean
            cur["text_struck"] = list(struck)
            awaiting_text = False
            continue

        if _is_marker_line(clean):
            cur["markers"].append(clean)
            _apply_inline_rules(cur, clean)
            continue

        if INSTRUCTION_RE.match(clean):
            _apply_inline_rules(cur, clean)
            continue

        _apply_inline_rules(cur, clean)
        if _is_option_line(clean):
            cur["options"].append(_make_option(clean))

    if cur:
        questions.append(cur)

    for q in questions:
        q["type"] = _detect_type(q["markers"])
        q["order"] = _detect_order(q["markers"])
        if q["type"] == "unknown" and ("min" in q["validation"] or "max" in q["validation"]):
            q["type"] = "numeric"
        del q["markers"]
    return questions


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/home/nik/Downloads/Survey_2.docx"
    qs = parse(path)
    out = {"source": path, "n_questions": len(qs), "questions": qs}
    with open("/home/nik/qa-agent/spec/survey2_spec.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Parsed {len(qs)} questions -> spec/survey2_spec.json")
    for target in ["S_ProcType", "Q_Aware", "Q_Current", "Q_VW_TC", "S_Contact_Info"]:
        q = next((x for x in qs if x["id"] == target), None)
        print(f"\n### {target}: {'FOUND' if q else 'MISSING'}")
        if q:
            slim = {k: q[k] for k in ("text", "text_struck", "type", "order",
                                      "validation", "carry_forward", "ask_condition")}
            slim["n_options"] = len(q["options"])
            slim["options"] = [o["label"] for o in q["options"]]
            print(json.dumps(slim, indent=2, ensure_ascii=False)[:900])
