from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
from typing import Optional
import json
import re

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Paths ──────────────────────────────────────────────────────────────────
DATA_DIR      = Path("data")

CONTAINER     = "md_standard"

# ── In-memory state ────────────────────────────────────────────────────────
state = {
    "annotations": [],
    "md_root": "",
    "session_file": DATA_DIR / "session.json",
}

# ── Text matching helpers ──────────────────────────────────────────────────
def normalize_text(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.replace("\u00a0", " ")
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()

def build_norm_map(text: str):
    norm_chars, norm_to_orig = [], []
    prev_space = False
    for i, ch in enumerate(text):
        if ch == "\u00a0": ch = " "
        elif ch in "\u2013\u2014": ch = "-"
        elif ch in "\u201c\u201d": ch = '"'
        elif ch in "\u2018\u2019": ch = "'"
        if ch.isspace():
            if not prev_space:
                norm_chars.append(" ")
                norm_to_orig.append(i)
            prev_space = True
        else:
            norm_chars.append(ch.lower())
            norm_to_orig.append(i)
            prev_space = False
    left, right = 0, len(norm_chars)
    while left < right and norm_chars[left] == " ": left += 1
    while right > left and norm_chars[right-1] == " ": right -= 1
    return "".join(norm_chars[left:right]), norm_to_orig[left:right]

def find_exact_spans(text: str, snippet: str):
    spans, start = [], 0
    while True:
        idx = text.find(snippet, start)
        if idx == -1: break
        spans.append([idx, idx + len(snippet)])
        start = idx + 1
    return spans

def find_norm_span(norm_text: str, norm_map: list, snippet: str):
    norm_snippet = normalize_text(snippet)
    if not norm_snippet: return None
    idx = norm_text.find(norm_snippet)
    if idx == -1: return None
    start_orig = norm_map[idx]
    end_orig   = norm_map[idx + len(norm_snippet) - 1] + 1
    return [start_orig, end_orig]

FUZZY_THRESHOLD = 0.30  # Jaccard threshold for character 4-gram similarity
FUZZY_MAX_WORDS = 200    # skip fuzzy for very long snippets

def find_fuzzy_span(norm_text: str, norm_map: list, snippet: str, threshold: float = FUZZY_THRESHOLD):
    """Sliding-window fuzzy match using character 4-gram Jaccard similarity.

    Why 4-gram Jaccard instead of token SequenceMatcher:
    - SequenceMatcher finds the longest common subsequence of tokens, so windows
      that share many common words (the, of, in, was) in the right order can beat
      the correct window — causing sentence-level span shifts.
    - 4-gram Jaccard is content-weighted: short common words produce few unique
      4-grams, so content words dominate the score.
    - Attached punctuation ('(HR' vs 'HR') differs by 1-2 grams, not a whole token.
    - O(L) per window (L = chars in window). Snippet grams are precomputed once.

    Skipped when the snippet exceeds FUZZY_MAX_WORDS."""
    norm_snippet = normalize_text(snippet)
    if not norm_snippet: return None
    snip_tokens = norm_snippet.split()
    n = len(snip_tokens)
    if n == 0 or n > FUZZY_MAX_WORDS: return None
    doc_tokens = [(m.group(), m.start(), m.end()) for m in re.finditer(r'\S+', norm_text)]
    if len(doc_tokens) < n: return None

    # Precompute snippet 4-grams once — reused for every window comparison.
    if len(norm_snippet) >= 4:
        snip_grams = set(norm_snippet[i:i+4] for i in range(len(norm_snippet) - 3))
    else:
        snip_grams = {norm_snippet}
    n_sg = len(snip_grams)

    best_score, best_i = 0.0, -1
    for i in range(len(doc_tokens) - n + 1):
        window_text = " ".join(t[0] for t in doc_tokens[i:i + n])
        if len(window_text) >= 4:
            win_grams = set(window_text[j:j+4] for j in range(len(window_text) - 3))
        else:
            win_grams = {window_text}
        isect = len(snip_grams & win_grams)
        union = n_sg + len(win_grams) - isect
        score = isect / union if union > 0 else 0.0
        if score > best_score:
            best_score = score
            best_i = i

    if best_score < threshold or best_i == -1: return None
    best_start = doc_tokens[best_i][1]
    best_end   = doc_tokens[best_i + n - 1][2]
    start_orig = norm_map[best_start]
    end_orig   = norm_map[best_end - 1] + 1
    return [start_orig, end_orig]

def compute_span(text: str, surr_text: str):
    if not surr_text or not surr_text.strip():
        return [], "no_surr_text"
    exact = find_exact_spans(text, surr_text)
    if exact:
        match_type = "exact" if len(exact) == 1 else "exact_multiple"
        return [exact[0]], match_type
    # Build norm map once — reused by both normalized and fuzzy
    norm_text, norm_map = build_norm_map(text)
    norm = find_norm_span(norm_text, norm_map, surr_text)
    if norm:
        return [norm], "normalized"
    fuzzy = find_fuzzy_span(norm_text, norm_map, surr_text)
    if fuzzy:
        return [fuzzy], "fuzzy"
    return [], "not_found"

def find_md_file(md_root: str, pmcid: str, file_name: str) -> Optional[Path]:
    p = Path(md_root) / CONTAINER / pmcid / file_name
    return p if p.exists() else None

# ── Session helpers ────────────────────────────────────────────────────────
def load_session() -> dict:
    sf = state["session_file"]
    if sf.exists():
        data = json.loads(sf.read_text(encoding="utf-8"))
        if "decisions" in data:
            return data
        return {"decisions": data, "manual": []}
    return {"decisions": {}, "manual": []}

def save_session():
    decisions = {
        ann.get("loc_id", ann["id"]): {
            "status":          ann["status"],
            "surr_text":       ann["metadata"]["surr_text"],
            "span":            ann["span"],
            "match_type":      ann["match_type"],
            "or_value":        ann["metadata"]["or_value"],
            "doc_type":        ann["metadata"]["doc_type"],
            "section":         ann["metadata"]["section"],
            "subsection":      ann["metadata"]["subsection"],
            "alt_surr_texts":  ann["metadata"].get("alt_surr_texts", []),
            "note":            ann["metadata"].get("note", ""),
        }
        for ann in state["annotations"]
    }
    # save manually added annotations separately so they survive reload
    manual = [ann for ann in state["annotations"] if ann["id"].endswith("_new")]
    state["session_file"].write_text(
        json.dumps({"decisions": decisions, "manual": manual}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

# ── Routes ─────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.on_event("startup")
def startup():
    # nothing to restore yet - state is populated on /load
    pass

@app.post("/upload-jsonl")
async def upload_jsonl(file: "UploadFile"):
    from fastapi import UploadFile
    dest = DATA_DIR / "llm_results" / file.filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    dest.write_bytes(content)
    return {"filename": f"llm_results/{file.filename}"}

@app.post("/load")
def load(jsonl_filename: str, md_root: str):
    # allow subfolders inside data/
    jsonl_path = DATA_DIR / jsonl_filename
    if not jsonl_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found in data/: {jsonl_filename}")

    md_root_path = Path(md_root)
    if not md_root_path.exists():
        raise HTTPException(status_code=404, detail=f"MD root folder not found: {md_root}")

    state["md_root"] = md_root

    jsonl_stem = Path(jsonl_filename).stem
    state["session_file"] = DATA_DIR / f"session_{jsonl_stem}.json"

    rows = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    session_data = load_session()
    session  = session_data.get("decisions", {})
    manual   = session_data.get("manual", [])
    md_cache = {}
    annotations = []

    for i, row in enumerate(rows):
        ann_id    = f"ann_{i:06d}"
        loc_id    = str(row.get("loc_id", "")) or ann_id
        pmcid     = str(row.get("pmcid", ""))
        file_name = str(row.get("file_name", ""))
        surr_text = str(row.get("surr_text", ""))

        md_file   = find_md_file(md_root, pmcid, file_name)
        cache_key = str(md_file) if md_file else None

        if cache_key and cache_key not in md_cache:
            try:
                md_cache[cache_key] = md_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                md_cache[cache_key] = ""

        md_text = md_cache.get(cache_key, "") if cache_key else ""

        if not md_file:
            span, match_type = [], "file_not_found"
        else:
            span, match_type = compute_span(md_text, surr_text)

        saved = session.get(loc_id) or session.get(ann_id, {})
        # For pending annotations the user has never acted on, always use the
        # freshly computed span/match_type so that new matching algorithms
        # (e.g. fuzzy) can improve on stale "not_found" values in old sessions.
        # For accepted/corrected/rejected, keep saved span (user set it manually).
        use_saved_span = saved.get("status", "pending") != "pending"

        annotations.append({
            "id":         ann_id,
            "loc_id":     loc_id,
            "pmcid":      pmcid,
            "file_name":  file_name,
            "file_path":  str(md_file) if md_file else "",
            "span":       saved.get("span", span) if use_saved_span else span,
            "match_type": saved.get("match_type", match_type) if use_saved_span else match_type,
            "status":     saved.get("status", "pending"),
            # Snapshot of original JSONL values + freshly computed span/match_type.
            # Used by /reset to restore an annotation to its pre-edit state.
            "original": {
                "surr_text":  surr_text,
                "or_value":   str(row.get("OR", "")),
                "doc_type":   str(row.get("doc_type", "")),
                "section":    str(row.get("section", "")),
                "subsection": str(row.get("subsection", "")),
                "span":       span,
                "match_type": match_type,
            },
            "metadata": {
                "pgs_id":        str(row.get("Polygenic Score (PGS) ID", "")),
                "or_value":      saved.get("or_value",   str(row.get("OR", "")))      if use_saved_span else str(row.get("OR", "")),
                "doc_type":      saved.get("doc_type",   str(row.get("doc_type", "")))  if use_saved_span else str(row.get("doc_type", "")),
                "section":       saved.get("section",    str(row.get("section", "")))   if use_saved_span else str(row.get("section", "")),
                "subsection":    saved.get("subsection", str(row.get("subsection", ""))) if use_saved_span else str(row.get("subsection", "")),
                "table":         row.get("table", 0),
                "confidence":    row.get("confidence", 0.0),
                "validation_ok": row.get("validation_ok", False),
                "surr_text":     saved.get("surr_text", surr_text) if use_saved_span else surr_text,
                "alt_surr_texts": saved.get("alt_surr_texts", []),
                "note":          saved.get("note", "") if use_saved_span else "",
                "answer":        str(row.get("answer", "")),
            },
        })

    annotations.extend(manual)
    state["annotations"] = annotations
    return {"loaded": len(annotations)}

@app.get("/annotations")
def get_annotations():
    return state["annotations"]

@app.get("/md-files")
def get_md_files():
    md_root = state.get("md_root", "")
    if not md_root:
        return {"pmcids": [], "files": {}}
    container_path = Path(md_root) / CONTAINER
    if not container_path.exists():
        return {"pmcids": [], "files": {}}
    pmcids = sorted([p.name for p in container_path.iterdir() if p.is_dir()])
    files = {}
    for pmcid in pmcids:
        pmcid_path = container_path / pmcid
        files[pmcid] = sorted([f.name for f in pmcid_path.iterdir() if f.is_file() and f.suffix == ".md"])
    return {"pmcids": pmcids, "files": files}

@app.get("/markdown")
def get_markdown(file_path: str):
    p = Path(file_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Markdown file not found")
    return {"content": p.read_text(encoding="utf-8", errors="ignore")}

@app.post("/update/{ann_id}")
def update_annotation(ann_id: str, payload: dict):
    for ann in state["annotations"]:
        if ann["id"] == ann_id:
            if "status"     in payload: ann["status"]                = payload["status"]
            if "surr_text"  in payload: ann["metadata"]["surr_text"] = payload["surr_text"]
            if "span"       in payload: ann["span"]                  = payload["span"]
            if "match_type" in payload: ann["match_type"]            = payload["match_type"]
            if "or_value"       in payload: ann["metadata"]["or_value"]      = payload["or_value"]
            if "doc_type"       in payload: ann["metadata"]["doc_type"]      = payload["doc_type"]
            if "section"        in payload: ann["metadata"]["section"]       = payload["section"]
            if "subsection"     in payload: ann["metadata"]["subsection"]    = payload["subsection"]
            if "alt_surr_texts" in payload: ann["metadata"]["alt_surr_texts"]= payload["alt_surr_texts"]
            if "note"           in payload: ann["metadata"]["note"]           = payload["note"]
            save_session()
            return {"ok": True}
    raise HTTPException(status_code=404, detail=f"Annotation {ann_id} not found")

@app.post("/reset/{ann_id}")
def reset_annotation(ann_id: str):
    for ann in state["annotations"]:
        if ann["id"] == ann_id:
            orig = ann.get("original")
            if orig:
                ann["status"]                 = "pending"
                ann["span"]                   = orig["span"]
                ann["match_type"]             = orig["match_type"]
                ann["metadata"]["surr_text"]  = orig["surr_text"]
                ann["metadata"]["or_value"]   = orig["or_value"]
                ann["metadata"]["doc_type"]   = orig["doc_type"]
                ann["metadata"]["section"]    = orig["section"]
                ann["metadata"]["subsection"] = orig["subsection"]
                ann["metadata"]["alt_surr_texts"] = []
            else:
                ann["status"] = "pending"
            save_session()
            return ann
    raise HTTPException(status_code=404, detail=f"Annotation {ann_id} not found")

@app.post("/add")
def add_annotation(payload: dict):
    new_id = f"ann_{len(state['annotations']):06d}_new"
    pmcid     = payload.get("pmcid", "")
    file_name = payload.get("file_name", "")
    md_file   = find_md_file(state["md_root"], pmcid, file_name)
    ann = {
        "id":        new_id,
        "pmcid":     pmcid,
        "file_name": file_name,
        "file_path": str(md_file) if md_file else "",
        "span":      payload.get("span", []),
        "match_type":"manual",
        "status":    "corrected",
        "metadata": {
            "pgs_id":        payload.get("pgs_id", ""),
            "or_value":      payload.get("or_value", ""),
            "doc_type":      payload.get("doc_type", "main"),
            "section":       payload.get("section", ""),
            "subsection":    payload.get("subsection", ""),
            "table":         payload.get("table", 0),
            "confidence":    payload.get("confidence", 1.0),
            "validation_ok":  True,
            "surr_text":      payload.get("surr_text", ""),
            "alt_surr_texts": [],
            "note":           payload.get("note", ""),
            "answer":         "Yes",
        },
    }
    state["annotations"].append(ann)
    save_session()
    return ann

@app.get("/export")
def export():
    accepted = [a for a in state["annotations"] if a["status"] in ("accepted", "corrected", "rejected")]
    md_cache: dict = {}
    rows = []
    for a in accepted:
        span_start = (a["span"][0][0] if isinstance(a["span"][0], list) else a["span"][0]) if a["span"] else ""
        span_end   = (a["span"][0][1] if isinstance(a["span"][0], list) else a["span"][1]) if a["span"] else ""

        # compute 1-based line number of span_start within the MD file
        row_number = ""
        if span_start != "" and a.get("file_path"):
            fp = a["file_path"]
            if fp not in md_cache:
                try:
                    md_cache[fp] = Path(fp).read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    md_cache[fp] = ""
            if md_cache[fp]:
                row_number = md_cache[fp][:span_start].count("\n") + 1

        rows.append({
            "id":         a["id"],
            "pmcid":      a["pmcid"],
            "file_name":  a["file_name"],
            "status":     a["status"],
            "match_type": a["match_type"],
            "span_start": span_start,
            "span_end":   span_end,
            "row_number": row_number,
            "pgs_id":     a["metadata"]["pgs_id"],
            "or_value":   a["metadata"]["or_value"],
            "doc_type":   a["metadata"]["doc_type"],
            "section":    a["metadata"]["section"],
            "subsection": a["metadata"]["subsection"],
            "table":      a["metadata"]["table"],
            "confidence": a["metadata"]["confidence"],
            "surr_text":      a["metadata"]["surr_text"],
            "alt_surr_texts": [
                {
                    "surr_text":  alt.get("surr_text", ""),
                    "span":       alt.get("span", []),
                    "row_number": (
                        md_cache.get(a.get("file_path", ""), "")[:alt["span"][0]].count("\n") + 1
                        if alt.get("span") and len(alt["span"]) == 2
                        and a.get("file_path") and md_cache.get(a.get("file_path", ""), "")
                        else ""
                    ),
                }
                for alt in a["metadata"].get("alt_surr_texts", [])
            ],
            "answer":         a["metadata"]["answer"],
            "note":           a["metadata"].get("note", ""),
        })
    jsonl_stem = state["session_file"].stem.replace("session_", "")
    export_dir = DATA_DIR / "ground_truth"
    export_dir.mkdir(exist_ok=True)
    export_path = export_dir / f"ground_truth_{jsonl_stem}.jsonl"
    with export_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"exported": len(rows), "accepted": len([a for a in accepted if a["status"]=="accepted"]), "corrected": len([a for a in accepted if a["status"]=="corrected"]), "rejected": len([a for a in accepted if a["status"]=="rejected"]), "file": str(export_path)}
