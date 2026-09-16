# pdf2epub-glmflash

[English](README.md) | [中文](README_zh.md)

Convert scanned PDF books into clean, readable EPUB ebooks with the **GLM-5.3-Flash** vision model. Every page is rendered to an image, transcribed to faithful Markdown (code blocks, tables, and lists preserved), and reassembled into a chapter-split EPUB with figures cropped from the original PDF and embedded at their exact positions.

> **⚠️ Intended use — personal learning only.** This is a *format-shifting* tool: use it on PDFs **you legitimately own** (purchased e-books, lecture notes, papers, technical documentation) so you can read them comfortably on an e-reader. It does not provide, host, or distribute any book content. Do not use it to produce or share pirated copies, and respect the copyright laws of your jurisdiction. Converted files are for your personal reading only.

## Features

- **Vision transcription by GLM-5.3-Flash**: faithful per-page Markdown with heading levels, fenced code blocks, tables, and lists. Running headers, page numbers, and scan watermarks are dropped automatically.
- **Colored text carried over**: sentences printed in a clearly different ink color (e.g. blue emphasis lines) are detected and re-emitted with inline CSS colors in the EPUB (`==[蓝]…==` markers → `<span style="color:…">`).
- **Figures preserved and positioned**: illustration regions are detected per page, cropped from the PDF at 300 dpi, and embedded exactly where they appear in the book. Coverage is reported (`markers replaced / total`).
- **Layout awareness**: two-column pages and 2-up spread scans (one PDF page = two book pages) are handled with explicit reading-order rules.
- **Smart chapter splitting**: a dual-tier TOC engine parses the printed table of contents (many page-number formats supported) and falls back to body-heading scanning with noise suppression. An interactive review step lets you confirm chapters before generation.
- **Resumable by design**: per-page transcription checkpoints — interrupted runs (including API quota windows) resume exactly where they stopped.
- **CJK-aware output**: Chinese paragraph joining without stray spaces, language auto-detection (`--language` to override), CJK chapter-title recognition (第N章 / 第N部分 / HACK #N / 术语表 …).
- **Valid XHTML output**: stray `<` escaping, code-fence balancing, and image-render fallback keep every generated EPUB strictly parseable.

## Prerequisites

- Python 3.8+
- A GLM API key (e.g. from a GLM coding plan)

## Getting API Access

Set `GLM_API_KEY` in `.env`, or let the tool discover a key automatically from the local ZCode config (`~/.zcode/v2/config.json` — the coding-plan provider is preferred, then any Anthropic-compatible provider with a key). Optional overrides: `GLM_BASE_URL` (default `https://open.bigmodel.cn/api/anthropic`), `GLM_MODEL` (default `GLM-5.3-Flash`).

> This is an independent open-source project, not affiliated with or endorsed by Zhipu AI / Z.ai. You need your own GLM API access.

## Installation

```bash
git clone <this-repo> pdf2epub-glmflash
cd pdf2epub-glmflash
python3 -m venv .venv
.venv/bin/pip install requests pymupdf EbookLib python-dotenv markdown

cp .env.example .env   # edit if you need to set a key explicitly
```

(`uv` works too: `uv run pdf2epub_glmflash.py ...`)

## Usage

```bash
.venv/bin/python pdf2epub_glmflash.py /path/to/book.pdf

# Skip the interactive prompts
.venv/bin/python pdf2epub_glmflash.py --title "Book Title" --author "Author" --auto-toc book.pdf

# Single-chapter EPUB (no TOC splitting)
.venv/bin/python pdf2epub_glmflash.py --no-toc -o out.epub book.pdf

# Test or retry a page range (checkpoints make this cheap)
.venv/bin/python pdf2epub_glmflash.py --pages 1:60 --auto-toc book.pdf

# Tuning
.venv/bin/python pdf2epub_glmflash.py --dpi 200 --concurrency 6 --glm-max-tokens 16000 book.pdf
```

### CLI Options

| Option | Meaning |
| --- | --- |
| `--title` / `--author` | Metadata; skips the interactive prompt |
| `--output`, `-o` | Output EPUB path (default: `<input>.epub`) |
| `--auto-toc` | Use auto-detected chapters without review |
| `--no-toc` | Single-chapter EPUB (mutually exclusive with `--auto-toc`) |
| `--pages A:B` | Transcribe only this 1-based inclusive range |
| `--language` | EPUB language code (default: auto-detect, e.g. `zh`) |
| `--glm-model` / `--glm-url` / `--glm-max-tokens` | API overrides |
| `--dpi` / `--concurrency` | Rendering DPI and parallel transcriptions |

### Environment Variables

`GLM_API_KEY`, `GLM_BASE_URL`, `GLM_MODEL`, `GLM_DPI`, `GLM_CONCURRENCY`, `GLM_MAX_TOKENS` (16000), `GLM_THINKING` (default `disabled` — OCR needs no deep reasoning; ~5x faster, identical quality).

### Global Command

```bash
sudo ln -s "$(pwd)/epub" /usr/local/bin/epub
epub /path/to/book.pdf
```

### Work Directory & Resume

Each input gets `glmflash_work_<hash>/` holding `glm_pages/` (per-page Markdown checkpoints), `glm_images/` (rendered page PNGs, useful for proofreading), `glm_figures/` (figure-detection results), `images/` (cropped figures), `results.json` (assembled output), and `cover.png`. Re-running the same file resumes automatically; with `--pages A:B` the assembled file is `results_pA_pB.json` so partial runs are never mistaken for complete ones.

## License

[MIT License](LICENSE)
