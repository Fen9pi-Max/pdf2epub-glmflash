# AGENTS.md

## Project Overview

Single-script CLI tool that converts scanned PDF books into EPUB ebooks with the **GLM-5.3-Flash** vision model via the Anthropic-compatible coding-plan API (page image → markdown transcription). All logic lives in `pdf2epub_glmflash.py` (~2000 lines); there is no package structure, no test suite, and no lint/typecheck config. Forked from pdf2epub-paddle (PaddleOCR engine removed; that repo remains the paddle version).

## Commands

```bash
# Run (key auto-discovered from ~/.zcode/v2/config.json or GLM_API_KEY in .env)
.venv/bin/python pdf2epub_glmflash.py /path/to/book.pdf

# Common flags
.venv/bin/python pdf2epub_glmflash.py --title "T" --author "A" --auto-toc book.pdf  # skip prompts
.venv/bin/python pdf2epub_glmflash.py --no-toc -o out.epub book.pdf                 # single-chapter EPUB
.venv/bin/python pdf2epub_glmflash.py --pages 1:60 ... book.pdf                     # partial range (test/retry)

# Environment
python3 -m venv .venv && .venv/bin/pip install requests pymupdf EbookLib python-dotenv markdown
# (`uv` is the documented runner but is NOT installed on this machine; .venv is the working setup)
```

- `--auto-toc` and `--no-toc` are mutually exclusive (enforced in `main()`).
- Env vars: `GLM_API_KEY`, `GLM_BASE_URL` (default `https://open.bigmodel.cn/api/anthropic`), `GLM_MODEL` (default `GLM-5.3-Flash`), `GLM_DPI` (200), `GLM_CONCURRENCY` (6), `GLM_MAX_TOKENS` (16000 — dense code pages truncate at 8000), `GLM_THINKING` (default `disabled` — OCR needs no deep reasoning; ~5x faster, identical quality).
- `resolve_glm_credentials()`: env vars first, else reads `~/.zcode/v2/config.json` — provider `builtin:bigmodel-coding-plan` preferred, then any Anthropic-compatible provider with a key (plans get switched/re-logged; the fallback matters).

## Architecture

Everything is one file; functions run in pipeline order inside `main()`:

1. `extract_cover_image` — first PDF page → cover.png (PyMuPDF/`fitz`)
2. `run_glm_ocr` — renders pages to `glm_images/page_NNNN.png` (sequential; PyMuPDF is not thread-safe), transcribes each via `glm_transcribe_page` (thread pool, retries with backoff on 429/5xx — 1308 quota errors mean wait for the reset window, checkpoints survive), post-processes (`postprocess_glm_markdown`: strips TopSage-style watermarks, bare page-number lines, pirate-archive banner pages), balances code fences per page (`balance_code_fences` — an unclosed ``` must not swallow the next page), strips running-header heading echoes, then extracts figures: pages with `[插图：...]` markers get a bounding-box detection pass (`glm_detect_figures`, checkpointed in `glm_figures/`, retried when under marker count), crops from the PDF at 300 dpi (`crop_page_figure`), and the markers are replaced with image refs (`embed_page_figures`). Results are assembled as `result.layoutParsingResults[].markdown` (text + `images` map).
3. `extract_metadata_interactive` — title/author prompt
4. `detect_chapter_headings` — **dual-tier TOC engine**: Tier 1 `extract_printed_toc` (parse printed TOC in front matter, fuzzy-match page anchors), Tier 2 `extract_and_filter_body_headings` (body scan with noise suppression). Returns `(major, all, source)`. Recognizes `第N部分` / `第N章` (spaces tolerated) / `HACK #N` / `术语表` as major headings.
5. `review_toc_interactive` — interactive TOC confirmation
6. `create_epub` — builds EPUB via EbookLib, converts markdown blocks with the `markdown` lib (`fenced_code`+`tables`); language auto-detected (`detect_language`, CJK ratio) unless `--language` given; CJK paragraph joins insert no space (`is_cjk_char`)

### Checkpointing / work directory

Per-input work dir `glmflash_work_<8-char-md5-of-name+size>/` holds `glm_pages/page_NNNN.md` (per-page transcription checkpoints — re-running resumes and skips existing pages), `glm_images/` (rendered page PNGs, useful for proofreading), `results.json` (assembled OCR output — its presence short-circuits the engine), `glm_figures/`, `images/`, and `cover.png`. With `--pages A:B` the assembled file is `results_pA_pB.json` so a partial run can't be mistaken for a complete one. These dirs are gitignored.

## Conventions & Constraints

- **Python 3.8+ compatibility** (`requires-python = ">=3.8"`): use `typing.List/Optional/Dict`, no `X | Y` unions, no `match` statements, no 3.9+-only dict/str methods.
- Import PyMuPDF as `import fitz`, EbookLib as `from ebooklib import epub`.
- Console output uses `[*]` info / `[+]` success / `[!]` warning / `[-]` step prefixes via plain `print` — no logging framework.
- The GLM transcription prompt (`GLM_PAGE_PROMPT`) is the single source of transcription-format truth (heading levels, code fences, watermark/header dropping, reading order for 2-up/two-column layouts, colored-text markers `==[色]…==`, blank-page marker). Downstream TOC/EPUB code depends on it — change them together. `GLM_FIGURE_PROMPT` must keep returning normalized 0-1000 bboxes as JSON.
- **EPUB HTML hygiene**: chapter markdown goes through `sanitize_markdown_for_xhtml` (escape stray `<` outside code — python-markdown otherwise passes text like `<main+0x5b>` through as raw HTML and produces invalid XHTML), then `render_color_spans` (`==[色]text==` → inline-styled `<span>`, must run AFTER sanitize or the spans get escaped), then `force_render_images` after conversion (python-markdown's raw-HTML block parser can leave `![alt](src)` unrendered on adversarial code content — always re-check rendered `<img>` counts against `images` maps after generation).
- READMEs are bilingual: `README.md` is Chinese (repo default) and `README_en.md` is English — update **both** when documenting user-facing changes.
- Commit style: conventional commits with scope, e.g. `fix(toc): ...`, `feat: ...`.

## Gotchas

- `uv` is not installed on this machine — use `.venv/bin/python` directly (venv already set up).
- The `epub` wrapper script resolves its own directory and prefers `.venv/bin/python` (works from any cwd, no hardcoded paths).
- Printed-TOC page numbers come in many formats (`…… 12`, `... 12`, `/ 12`, `/12`, `· 12`); `is_toc_page_num_line`/`clean_toc_line` must accept all of them or TOC continuation pages silently drop their entries (seen as gaps in the chapter list, e.g. 第6–9章 missing).
- Book layouts seen in the wild: 2-up spreads (one PDF page = two book pages, e.g. 海盗派测试分析), two-column pages (自动化测试最佳实践). `GLM_PAGE_PROMPT`/`GLM_FIGURE_PROMPT` carry explicit reading-order rules for these.
- Metadata/TOC steps prompt interactively unless `--title`/`--auto-toc` etc. are passed; keep that escape hatch when adding new interactive steps.
- Book scans from ebook-ripper sites carry per-page watermarks (`www.TopSage.com`) and pirate-archive generator pages (Anna's Archive) — dropped by prompt rule and `postprocess_glm_markdown` (banner-page detection matches the phrase, so real names like "Anna" in acknowledgements survive); keep prompt and postprocessor in sync.
- GLM sometimes emits `<br>` inside table cells; `sanitize_markdown_for_xhtml` replaces it outside code fences (inside code it is real content). A few may survive inside backtick-wrapped table cells — cosmetic only.
- Always verify book metadata against the transcribed CIP/版权页 rather than guessing from cover or praise pages (author attribution has been wrong there before).
- Converted books live outside the repo (a local `books/` directory); their `glmflash_work_*` checkpoints stay here but are gitignored.
