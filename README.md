# Annotation Validation Tool — Technical Documentation

Version 2.0 — April 2026

---

## 1. Overview

The Annotation Validation Tool is a local web application built with FastAPI (Python backend) and plain HTML/CSS/JavaScript (frontend). It is designed to allow a researcher to validate, correct, and curate annotations produced by a large language model (LLM) from scientific literature.

These annotations consist of metadata values (Odds Ratios, OR) that are identified or extracted from article documents (main text or supplementary material), together with location information: section, subsection, table number, and a surrounding text passage (`surr_text`) that locates the value in the document.

The tool takes as input a raw JSONL file containing the LLM output, automatically locates and highlights the surrounding text in the corresponding Markdown source files, and provides a browser-based interface for reviewing and correcting each annotation. The final validated output (ground truth) can be exported as a flat JSONL file ready for downstream analysis.

### 1.1 Key capabilities

- Load raw LLM output (JSONL) via drag-and-drop or file browser
- Automatically compute character-level spans for surrounding text in Markdown files using a three-stage matching pipeline (exact → normalised → fuzzy)
- Visualise annotations highlighted inside the rendered Markdown document
- Accept, reject, or correct annotations individually
- Edit metadata fields inline (OR value, doc type, section, subsection)
- Correct surrounding text by mouse-highlighting directly in the document
- Add new annotations manually
- Filter annotations by status, match type, PGS ID, and PMCID
- Save progress automatically per JSONL file and restore on reload
- Export accepted, corrected, and rejected annotations as a flat JSONL ground truth file

---

## 2. Technical Architecture

The tool runs entirely on the user's local machine. No data is sent to any external server.

### 2.1 Stack

- **Backend:** Python 3.13, FastAPI, Uvicorn
- **Frontend:** Single-file HTML/CSS/JavaScript (no frameworks)
- **Storage:** JSON session files on disk (one per JSONL file)
- **Port:** 8080 (local only)

### 2.2 Folder structure

```
annotation-tool/
  app/
    main.py          — FastAPI server: all backend logic, matching pipeline
    __init__.py
  static/
    index.html       — Full frontend: UI, styles, JavaScript
  data/
    llm_results/     — Drop JSONL input files here
    output/
      md_standard/   — Markdown source files, organised as <pmcid>/<filename>.md
    ground_truth/    — Exported ground truth files
    session_*.json   — Auto-saved session per JSONL file
  venv/              — Python virtual environment
```

The Markdown files are expected under:

```
<md_root>/md_standard/<pmcid>/<file_name>.md
```

where `<md_root>` is the path entered in the load panel.

---

## 3. Launching the Tool

A double-click launcher is available on the Desktop: **Launch Annotation Tool**. Double-clicking it opens a terminal and starts the server. Once the terminal shows `Application startup complete`, open a browser and navigate to:

```
http://127.0.0.1:8080
```

To stop the tool, press `Ctrl+C` in the terminal window.

> **Note:** if you are connected to a VPN, the launcher automatically frees port 8080 before starting to avoid conflicts.

---

## 4. Text Matching Pipeline

This is the core of the tool. When a JSONL file is loaded, for every annotation the backend searches for its `surr_text` in the corresponding Markdown file and computes a character-level span `[start, end]`. The pipeline has three stages that are tried in order.

### 4.1 Text normalisation

Before any matching, both the document text and the snippet go through a normalisation step to eliminate typographic and encoding differences that are irrelevant to content equality.

#### `normalize_text(s)` — produces a flat string for comparison

Applied to the **snippet** in both normalised and fuzzy matching. Performs the following substitutions in order:

| Input character | Unicode | Replaced with |
|---|---|---|
| Non-breaking space | ` ` | regular space |
| En-dash | `–` | `-` |
| Em-dash | `—` | `-` |
| Curly left double quote | `“` | `"` |
| Curly right double quote | `”` | `"` |
| Curly left single quote / open apostrophe | `‘` | `'` |
| Curly right single quote / close apostrophe | `’` | `'` |

After substitutions, any run of whitespace (spaces, tabs, newlines) is collapsed to a single space. The result is stripped of leading/trailing whitespace and lowercased.

#### `build_norm_map(text)` — normalises the document while preserving position info

Applied to the **document text**. Performs the same character substitutions as `normalize_text`, but does so character by character rather than with regex, building two parallel arrays:

- `norm_chars`: the normalised characters
- `norm_to_orig`: for each position in the normalised string, the corresponding character index in the original text

Special rules:
- Consecutive whitespace characters are collapsed to a single space; only the position of the **first** whitespace character is recorded in `norm_to_orig`
- Leading and trailing spaces are stripped from the result

Returns a tuple `(norm_text, norm_map)` where `norm_text` is the normalised string and `norm_map[i]` is the original-text character index corresponding to position `i` in `norm_text`.

This mapping is what allows all three matching methods to return spans in terms of the **original, unmodified document** rather than the normalised version.

---

### 4.2 Stage 1 — Exact matching (`find_exact_spans`)

```
find_exact_spans(text, snippet) → list of [start, end]
```

The simplest and fastest stage. Performs a plain Python `str.find()` loop on the **raw, un-normalised** document text, searching for the snippet verbatim. All non-overlapping occurrences are returned as `[start, end]` character offset pairs.

**Behaviour in `compute_span`:**
- If exactly one occurrence is found → `match_type = "exact"`
- If more than one occurrence is found → `match_type = "exact_multiple"` (the first occurrence is highlighted; the filter chip shows all of them)
- If no occurrence is found → falls through to Stage 2

**When it works:** whenever the LLM reproduced the surrounding text character-for-character, including punctuation, casing, and spacing.

**When it fails:** any typographic difference — a curly quote instead of a straight one, an en-dash instead of a hyphen, a collapsed double space — will cause an exact miss.

---

### 4.3 Stage 2 — Normalised matching (`find_norm_span`)

```
find_norm_span(norm_text, norm_map, snippet) → [start, end] or None
```

Runs only if exact matching found nothing. The document's `norm_text` and `norm_map` are built once by `compute_span` and reused across Stage 2 and Stage 3.

**Algorithm:**

1. Apply `normalize_text` to the snippet to produce `norm_snippet`.
2. Call `norm_text.find(norm_snippet)` — a plain substring search on both normalised strings.
3. If found at index `idx`:
   - `start_orig = norm_map[idx]`
   - `end_orig = norm_map[idx + len(norm_snippet) - 1] + 1`
   - Return `[start_orig, end_orig]`
4. If not found → return `None`, fall through to Stage 3.

**What normalisation tolerates:** differences in quotation mark style, dash style (en/em vs hyphen), non-breaking spaces, and inconsistent whitespace or newlines. It does **not** tolerate word substitutions, missing words, or different numbers.

**Example:** if the document has `"OR = 1.45 (95% CI: 1.35–1.58)"` and the snippet has `"OR = 1.45 (95% CI: 1.35-1.58)"`, the en-dash vs hyphen difference is eliminated by normalisation and a match is found.

---

### 4.4 Stage 3 — Fuzzy matching (`find_fuzzy_span`)

```
find_fuzzy_span(norm_text, norm_map, snippet,
                threshold=FUZZY_THRESHOLD) → [start, end] or None
```

Runs only if both exact and normalised matching failed. This stage handles cases where the LLM's surrounding text differs from the document in ways that go beyond punctuation and spacing — missing words, attached punctuation changing tokenisation, minor paraphrasing, or PDF extraction artefacts.

**Guard:** if the normalised snippet has more than `FUZZY_MAX_WORDS = 80` words, the function returns `None` immediately without searching. Very long snippets (full table sections, extended passages) are not suitable for fuzzy matching: the search would be slow and the results unreliable.

**Algorithm — character 4-gram Jaccard similarity:**

Why 4-gram Jaccard rather than word-level token comparison? Word-level comparison (e.g. SequenceMatcher on token lists) treats each token as an atomic unit. If the document has `"(95%"` as one token (parenthesis attached) while the snippet has `"95%"` as a separate token, they do not match at all — and an earlier passage that happens to share more plain tokens (common words like "the", "of", "in") can outscore the correct location, placing the highlight above the real text. Character 4-grams avoid this: `"(95%"` and `"95%"` share two 4-grams (`"95% "` and `"5% c"`), so their similarity is high. Short common words produce few unique 4-grams, so content words naturally dominate the score.

Steps:

1. Apply `normalize_text` to the snippet → `norm_snip`.
2. Split `norm_snip` into `snip_tokens` (whitespace-split words). Count `n = len(snip_tokens)`.
3. Tokenise `norm_text` into `doc_tokens` using `re.finditer(r'\S+', norm_text)`, recording each token's text, start position, and end position within `norm_text`.
4. **Precompute snippet 4-grams once:**
   ```
   snip_grams = { norm_snip[i:i+4] for i in range(len(norm_snip) - 3) }
   ```
   If the snippet is shorter than 4 characters, use the whole snippet as a single gram.
5. **Sliding window:** for each position `i` in `doc_tokens` (from `0` to `len(doc_tokens) - n`):
   - Reconstruct the window text by joining the `n` tokens starting at `i` with spaces.
   - Compute the window's 4-grams: `{ window[j:j+4] for j in range(len(window) - 3) }`.
   - Compute Jaccard similarity:
     ```
     score = |snip_grams ∩ win_grams| / |snip_grams ∪ win_grams|
     ```
   - Track the window index with the highest score.
6. If `best_score >= FUZZY_THRESHOLD = 0.30`:
   - `start_orig = norm_map[ doc_tokens[best_i].start ]`
   - `end_orig = norm_map[ doc_tokens[best_i + n - 1].end - 1 ] + 1`
   - Return `[start_orig, end_orig]`
7. Otherwise return `None`.

**Why Jaccard and not ratio-based similarity?** Jaccard is set-based: it counts unique shared 4-grams divided by unique total 4-grams. This means:
- Common words like "the" produce few unique 4-grams (`"the "`, `" the"`) and contribute little to the score
- Rare content tokens like `"1.45"` or `"copd"` produce distinctive 4-grams that strongly anchor the match to the right location
- Attached punctuation (`"(OR"` vs `"OR"`) differs by only 1–2 grams, not a whole token

**Threshold:** `FUZZY_THRESHOLD = 0.30`. This is a Jaccard threshold (not a ratio), so 0.30 means the intersection must be at least 30% of the union of unique 4-grams. Empirically this is appropriate for scientific text where content words are distinctive.

**Performance:** snippet 4-grams are precomputed once. Each window comparison is O(L) where L is the window character length (set intersection of precomputed vs freshly computed 4-grams). For a typical document (~10 000 doc tokens) and a 30-word snippet, the sliding window completes in well under a second.

---

### 4.5 Orchestration (`compute_span`)

```
compute_span(text, surr_text) → (span, match_type)
```

Called once per annotation at load time.

```
if surr_text is empty:
    return [], "no_surr_text"

exact = find_exact_spans(text, surr_text)
if exact:
    return [exact[0]], "exact"  (or "exact_multiple" if len > 1)

norm_text, norm_map = build_norm_map(text)   ← built once, reused below

norm = find_norm_span(norm_text, norm_map, surr_text)
if norm:
    return [norm], "normalized"

fuzzy = find_fuzzy_span(norm_text, norm_map, surr_text)
if fuzzy:
    return [fuzzy], "fuzzy"

return [], "not_found"
```

The norm map is built only once per annotation regardless of how many stages run, avoiding redundant work.

---

### 4.6 Session and span recomputation

When a JSONL file is loaded and a session already exists for it, the tool restores previously saved decisions. However, for **pending** annotations (those the user has not yet accepted, corrected, or rejected), the span and match type are **always recomputed fresh** rather than taken from the session. This ensures that improvements to the matching pipeline (such as the addition of fuzzy matching to an existing session) are automatically applied on the next load without requiring the user to clear the session.

For **accepted, corrected, and rejected** annotations, the saved span and match type are preserved, since these may reflect manual corrections made by the validator.

---

## 5. Match Types

| Match type | Colour | Meaning |
|---|---|---|
| `exact` | green | `surr_text` found verbatim exactly once in the document |
| `exact_multiple` | orange | `surr_text` found verbatim more than once; first occurrence highlighted |
| `normalized` | orange | `surr_text` found after normalising punctuation, dashes, quotes, and whitespace |
| `fuzzy` | purple | `surr_text` located via character 4-gram Jaccard similarity |
| `not_found` | red | `surr_text` could not be located by any method |
| `no_surr_text` | red | The LLM returned no surrounding text for this annotation |
| `file_not_found` | red | The Markdown source file could not be found on disk |
| `manual` | — | Annotation was added manually by the validator |

Annotations with `not_found`, `no_surr_text`, or `fuzzy` match types display the LLM's original surrounding text in a highlighted box in the metadata panel so the validator can compare it with the highlighted passage (or locate it manually).

---

## 6. Loading Data

In the toolbar at the top of the page:

1. **Drag and drop** your JSONL file onto the blue drop zone, or click it to browse. The file is uploaded automatically to `data/llm_results/`.
2. **Enter the full path** to the Markdown root folder (the folder that contains `md_standard/`) in the text field next to it.
3. Click **Load**.

The backend reads all JSONL rows, locates each Markdown file, runs the matching pipeline, and restores any previously saved session. A status message shows the number of annotations loaded.

**Session file:** the session is stored at `data/session_<jsonl_stem>.json`. Each JSONL file has its own independent session. Loading a different JSONL file never overwrites a previous session.

**Span recomputation on reload:** pending annotations are always rematched on load. Accepted, corrected, and rejected annotations keep their saved spans.

---

## 7. Reviewing Annotations

Click any annotation in the left panel to select it. The right side shows:

- **Metadata panel (top):** key fields for the annotation. Editable fields are underlined with a dashed line.
- **Markdown viewer (bottom):** the full source document rendered, with the annotation's surrounding text highlighted in yellow.

For `fuzzy` matches, a purple box shows the LLM's original surrounding text so the validator can verify that the fuzzy highlight corresponds to the correct passage.

For `not_found` and `no_surr_text` matches, a yellow box shows the LLM's original surrounding text as a reference for manual location.

---

## 8. Annotation Statuses

| Status | Colour | Meaning |
|---|---|---|
| Pending | grey | Not yet reviewed |
| Accepted | green | LLM output confirmed correct as-is |
| Corrected | orange | Validator has edited one or more fields |
| Rejected | red | Annotation is wrong |

**Action buttons:**
- **Accept:** marks the annotation as correct as-is.
- **Reject:** marks the annotation as wrong.
- **Reset to pending:** fully restores the annotation to its original state (see below).

`Corrected` status is set automatically whenever the validator edits any field (metadata or surrounding text).

### 8.1 Reset to pending — full state restoration

"Reset to pending" does more than change the status label. It restores the annotation to the exact state it had when the JSONL file was first loaded, before any user editing:

| Field restored | Restored to |
|---|---|
| `status` | `pending` |
| `surr_text` | original value from the JSONL row |
| `or_value` | original value from the JSONL row |
| `doc_type` | original value from the JSONL row |
| `section` | original value from the JSONL row |
| `subsection` | original value from the JSONL row |
| `span` | freshly computed span from the last load |
| `match_type` | freshly computed match type from the last load |
| `alt_surr_texts` | cleared |

This means that any correction made by the validator — a manual re-highlight, an edited OR value, a corrected section name — is completely undone. The annotation returns to its pre-edit baseline.

**Implementation note:** at load time, the backend stores an `"original"` snapshot dict alongside each annotation in memory. This snapshot captures the JSONL field values and the span/match_type computed by the matching pipeline at that load. The snapshot is held only in memory and is never written to the session file, so it adds no disk overhead. When `/reset/{ann_id}` is called, the backend copies the snapshot values back into the annotation and saves the session. The cost of a reset is negligible (a few field copies and a session write).

---

## 9. Filtering Annotations

The left panel provides four independent filters that can be combined:

- **Status:** All / Pending / Accepted / Corrected / Rejected
- **Match type:** All / Exact / Exact multiple / Normalized / Fuzzy / Not found / No surr text / Manual / File not found — with counts per type shown in brackets. The counts across all match type chips always sum to the total number of loaded annotations.
- **PGS ID dropdown:** filter to a specific polygenic score model
- **PMCID dropdown:** filter to a specific paper

---

## 10. Correcting Annotations

### 10.1 Correcting metadata

The following fields are editable by clicking on them in the metadata panel:

- **OR value:** click to open a text input; press Enter or click away to save.
- **Doc type:** click to open a dropdown with options `main`, `suppl`, `other`.
- **Section:** click to open a text input.
- **Subsection:** click to open a text input.

PGS ID, confidence, and match type are read-only.

### 10.2 Correcting surrounding text

To replace the surrounding text of an annotation, select text directly in the Markdown viewer by clicking and dragging. The selection immediately:

- Replaces the stored `surr_text` with the selected text
- Updates the yellow highlight in the document to the new selection
- Saves the new character span
- Sets the annotation status to Corrected

The new selection fully replaces the previous one. There is no merging of selections.

---

## 11. Adding New Annotations

Click **+ Add new annotation** at the bottom of the left panel. A form appears with:

- **PGS ID** (required): type freely or select from suggestions
- **OR value**
- **PMCID** (required): dropdown from available Markdown folders
- **File name** (required): dropdown from files in the selected PMCID folder
- **Doc type:** main / suppl / other
- **Section** and **Subsection**
- **Surrounding text:** optional — can be set by mouse-highlighting after saving

New annotations are saved with `match_type = "manual"` and `status = "corrected"`. They are included in the ground truth export.

---

## 12. Session Saving and Restoration

All decisions are saved automatically to a JSON file in `data/` every time any change is made:

```
data/session_<jsonl_stem>.json
```

Each JSONL file has its own independent session. Loading a different JSONL never overwrites a previous one.

To restore a session, click **Load** with the same JSONL file and Markdown root folder. All decisions, corrections, and manually added annotations are restored automatically.

> A session file is only created after at least one interaction. Loading data without making any change does not create a session file.

---

## 13. Exporting the Ground Truth

Click **Export ground truth** in the toolbar. If there are still pending annotations, the tool asks for confirmation before exporting.

The export includes **all Accepted, Corrected, and Rejected** annotations. Pending annotations are excluded. The output file is saved at:

```
data/ground_truth/ground_truth_<jsonl_stem>.jsonl
```

Exporting multiple times overwrites the previous file for the same JSONL.

### 13.1 Output fields

| Field | Description |
|---|---|
| `id` | Unique annotation identifier |
| `pmcid` | PubMed Central ID of the source paper |
| `file_name` | Name of the Markdown source file |
| `status` | `accepted`, `corrected`, or `rejected` |
| `match_type` | How `surr_text` was located (`exact`, `normalized`, `fuzzy`, `not_found`, `manual`, …) |
| `span_start` | Character offset where `surr_text` starts in the Markdown file |
| `span_end` | Character offset where `surr_text` ends in the Markdown file |
| `row_number` | 1-based line number in the Markdown file where the span starts |
| `pgs_id` | Polygenic Score ID |
| `or_value` | Odds ratio value |
| `doc_type` | `main`, `suppl`, or `other` |
| `section` | Section of the paper |
| `subsection` | Subsection of the paper |
| `table` | Table number where the annotation sits (if any) |
| `confidence` | LLM confidence score (0.0–1.0) |
| `surr_text` | Final validated surrounding text |
| `answer` | LLM answer (`Yes` / `No`) |

---

## 14. Tips and Known Limitations

### Tips

- Start by filtering to `not_found` and `no_surr_text` — these require the most attention.
- Filter to `fuzzy` next and verify each highlight against the purple LLM-text box.
- Use the PGS ID filter to work through all annotations for one model at a time.
- Always click **Load** after restarting the tool to restore your session.

### Known limitations

- **Very long `surr_text` (> 80 words):** fuzzy matching is skipped for these. They are likely full table sections or extended passages that cannot be reliably located by similarity; they remain `not_found` and must be corrected manually.
- **Table highlighting:** selecting text across multiple rows of a rendered Markdown table may not work perfectly in all browsers. Use the raw Markdown view in that case.
- **Single-user, local use only.** The tool is not intended for multi-user or networked deployment.
- **Markdown rendering:** the viewer renders a simplified version of the Markdown. Complex formatting (nested lists, footnotes) may not render perfectly, but the raw text and highlighting remain accurate.
