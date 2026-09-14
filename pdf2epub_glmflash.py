#!/usr/bin/env python3
"""pdf2epub-glmflash: convert scanned PDF books into EPUB ebooks.

Pages are rendered to PNG and transcribed to Markdown by the GLM-5.3-Flash
vision model via the Anthropic-compatible coding-plan API. Per-page
checkpoints make long runs resumable; figures are detected, cropped from the
PDF at 300 dpi, and embedded at their original positions.
"""

import os
import sys
import argparse
import base64
import json
import re
import random
import threading
import time
import concurrent.futures
from typing import List, Dict, Any, Optional, Tuple
from collections import Counter
from difflib import SequenceMatcher

import requests
import fitz  # PyMuPDF
from ebooklib import epub
from dotenv import load_dotenv

# Load .env file if present (does not override existing env vars)
load_dotenv()

# --- Configuration ---
# Anthropic-compatible endpoint of the GLM coding plan.
GLM_BASE_URL = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
GLM_MODEL = os.getenv("GLM_MODEL", "GLM-5.3-Flash")
GLM_DPI = int(os.getenv("GLM_DPI", "200"))
GLM_CONCURRENCY = int(os.getenv("GLM_CONCURRENCY", "6"))
# Dense code/appendix pages can exceed 8000 output tokens and get truncated.
GLM_MAX_TOKENS_DEFAULT = int(os.getenv("GLM_MAX_TOKENS", "16000"))
# Page transcription needs no deep reasoning; disabling it makes GLM-5.3-Flash
# ~5x faster with identical quality. Set GLM_THINKING=enabled to restore it.
GLM_THINKING = os.getenv("GLM_THINKING", "disabled")
ZCODE_CONFIG_PATH = os.path.expanduser(
    os.getenv("ZCODE_CONFIG_PATH", "~/.zcode/v2/config.json")
)

WORK_DIR_PREFIX = "glmflash_work"


def check_dependencies():
    """Checks if required libraries are installed."""
    missing = []
    try:
        import fitz
    except ImportError:
        missing.append("pymupdf")
    try:
        import ebooklib
    except ImportError:
        missing.append("EbookLib")

    if missing:
        print(f"[!] Missing dependencies: {', '.join(missing)}")
        print(f"    Please run: pip install {' '.join(missing)}")
        return False
    return True


# --- GLM Vision OCR Engine (default) ---

class GlmApiError(Exception):
    """Error calling the GLM API. `retryable` marks transient failures."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


# Bare-domain lines left by ebook-ripper watermarks (e.g. www.TopSage.com).
WATERMARK_LINE_RE = re.compile(
    r"^\s*(?:www\.[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|"
    r"(?:https?://)?[A-Za-z0-9.\-]*topsage[^\s]*)\s*$",
    re.I,
)

# Lines from pirate-archive generator pages (matched on the distinctive
# "…Archive" phrase so real names like "Anna" in acknowledgements survive).
ARCHIVE_BANNER_RE = re.compile(r"Annas?\W{0,3}s?\s*Archive", re.I)

GLM_PAGE_PROMPT = """你是专业的图书数字化录入员。请将这张书籍页面（全书第 {page_no}/{total_pages} 页）逐字转录为 Markdown。

【转录规则】
1. 忠实原文：逐字转录页面上的正文内容。不要翻译、不要总结、不要改写、不要修正原文错别字；原文为繁体则保留繁体。
2. 按正确的阅读顺序转录：若页面为左右双栏排版，先读完左栏再读右栏；若页面是包含左右两个书页的双联扫描页，先读左页再读右页。
3. 标题层级（仅当该行在版式上确实是标题——字号明显更大、加粗或居中独立成行——时才标记）：
   - 全书级大标题（如「第1部分 ……」「前言」「附录」「索引」）用「# 」；
   - 章节级标题（如「HACK #12 ……」「第3章 ……」）用「## 」；
   - 小节标题用「### 」。
4. 代码、命令、终端会话输出一律用 ``` 围栏代码块原样保留（保留缩进和空行）；行内代码/命令用反引号。
5. 列表用 Markdown 列表语法；表格用 Markdown 表格。
6. 每个自然段输出为一行（段内不要手动换行），段落之间空一行；中文段首不要加全角或半角空格缩进。
7. 丢弃：页眉（页顶的书名/章名/页码行）、页脚、独立成行的页码、页面边缘的网站水印以及扫描噪点；双联页中间的书缝阴影不算内容。
8. 正文中的脚注编号（如 注1、[1]）保持原样；当页的脚注文字以单独段落「[注1] ……」的形式放在该页转录结果的最后。
9. 加框的提示/注意事项（TIP/注意/NOTE 等）用 Markdown 引用块（> ）转录。
10. 插图、照片、图表在其位置输出一行「[插图：简要描述内容]」，原有图注文字正常转录。
11. 若整页空白或仅有装饰，只输出「<!-- blank page -->」。
12. 直接以正文内容开头，不要输出任何解释、评论或与转录无关的内容。"""


GLM_FIGURE_PROMPT = """找出这张书籍页面中的所有插图区域（流程图、示意图、原理图、照片、屏幕截图、带边框的图表、思维导图、手写签名、印章/题字），并输出它们的位置。

要求：
1. 以 JSON 数组输出，每个元素形如 {{"bbox": [x1, y1, x2, y2], "desc": "简短描述"}}。
2. bbox 是插图区域的外接矩形，坐标为相对整个页面（含双联页）的归一化值（0~1000，原点在左上角，x 向右、y 向下）。框线、箭头等图形元素要完整包含，但不要包含正文段落和图注文字行。
3. 按阅读顺序排列：双栏排版先左栏后右栏；双联扫描页先左页后右页；每栏/每页内从上到下。
4. 纯文字段落、行内公式、纯文本代码块不算插图；若本页没有插图，输出 []。
5. 只输出 JSON 数组本身，不要输出其他任何内容。"""

FIGURE_MARKER_RE = re.compile(r"\[插图[^\]]*\]")


# ZCode config providers that may carry a usable Anthropic-compatible key,
# in preference order (plans get switched/re-logged occasionally).
PREFERRED_GLM_PROVIDERS = [
    "builtin:bigmodel-coding-plan",
    "builtin:bigmodel",
    "builtin:zai-coding-plan",
    "builtin:zai",
]


def resolve_glm_credentials() -> Tuple[str, str]:
    """Resolves (api_key, base_url): GLM_API_KEY/GLM_BASE_URL env vars first,
    else the local ZCode config — the coding-plan provider when present, and
    as a fallback any Anthropic-compatible provider that still has a key."""
    env_key = os.getenv("GLM_API_KEY", "").strip()
    env_base = os.getenv("GLM_BASE_URL", "").strip()
    if env_key:
        return env_key, env_base or GLM_BASE_URL
    try:
        with open(ZCODE_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return "", env_base or GLM_BASE_URL
    providers = cfg.get("provider", {}) or {}
    ordered = [p for p in PREFERRED_GLM_PROVIDERS if p in providers]
    ordered += [p for p in providers if p not in ordered]
    for pid in ordered:
        opts = (providers.get(pid) or {}).get("options") or {}
        key = (opts.get("apiKey") or "").strip()
        base = (opts.get("baseURL") or "").strip()
        if key and "/api/anthropic" in base:
            return key, env_base or base
    return "", env_base or GLM_BASE_URL


def resolve_glm_api_key() -> str:
    """Back-compat wrapper: key only."""
    return resolve_glm_credentials()[0]


def unwrap_outer_code_fence(text: str) -> str:
    """Removes a wrapping ``` fence if the model enclosed its whole output in one."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        inner = stripped[3:-3]
        # Only unwrap when the outer fence is the only one (real code blocks absent).
        if inner.count("```") == 0:
            first_newline = inner.find("\n")
            if first_newline != -1:
                inner = inner[first_newline + 1 :]
            return inner.strip()
    return text


def postprocess_glm_markdown(md: str) -> str:
    """Cleans transcription artifacts: watermarks, bare page-number lines,
    page-corner markers, pirate-archive generator banner pages."""
    nonempty = [l for l in md.split("\n") if l.strip()]
    # A page that OPENS with a generator banner (e.g. "Document generated by
    # Anna's Archive …") is not book content — drop the whole page.
    if nonempty and ARCHIVE_BANNER_RE.search(nonempty[0]):
        return "<!-- blank page -->"
    cleaned_lines = []
    for line in md.split("\n"):
        stripped = line.strip()
        if WATERMARK_LINE_RE.match(stripped):
            continue
        if ARCHIVE_BANNER_RE.search(stripped):
            continue
        if re.fullmatch(r"\d{1,4}", stripped):
            continue
        if re.fullmatch(r"\d{1,4}\s*[▶▷▼▲◆●]", stripped):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def glm_transcribe_page(
    png_bytes: bytes,
    api_key: str,
    base_url: str = GLM_BASE_URL,
    model: str = GLM_MODEL,
    page_no: int = 0,
    total_pages: int = 0,
    max_tokens: int = 8000,
    max_retries: int = 6,
) -> str:
    """Sends one page image to the GLM vision model and returns its markdown transcription."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": GLM_PAGE_PROMPT.format(
                            page_no=page_no, total_pages=total_pages
                        ),
                    },
                ],
            }
        ],
    }
    if GLM_THINKING == "disabled":
        body["thinking"] = {"type": "disabled"}
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    url = base_url.rstrip("/") + "/v1/messages"

    last_err = ""
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=300)
            if resp.status_code != 200:
                retryable = resp.status_code == 429 or resp.status_code >= 500
                last_err = "HTTP {}: {}".format(resp.status_code, resp.text[:300])
                raise GlmApiError(last_err, retryable=retryable)
            data = resp.json()
            if data.get("type") == "error":
                raise GlmApiError(
                    "API error: {}".format(data.get("error", {})), retryable=False
                )
            text = "\n".join(
                blk.get("text", "")
                for blk in data.get("content", [])
                if blk.get("type") == "text"
            ).strip()
            stop_reason = data.get("stop_reason")
            if stop_reason == "max_tokens":
                raise GlmApiError(
                    "output truncated (stop_reason=max_tokens); "
                    "increase --glm-max-tokens for this page",
                    retryable=False,
                )
            if not text:
                raise GlmApiError("empty response", retryable=True)
            return unwrap_outer_code_fence(text)
        except GlmApiError as e:
            if not e.retryable:
                raise
            last_err = str(e)
        except requests.RequestException as e:
            last_err = f"network error: {e}"

        if attempt < max_retries - 1:
            wait = min(60.0, (2 ** attempt) * 3) + random.uniform(0, 2)
            print(f"    [!] Page {page_no}: {last_err}; retrying in {wait:.0f}s "
                  f"({attempt + 1}/{max_retries})")
            time.sleep(wait)

    raise GlmApiError(f"giving up after {max_retries} attempts: {last_err}")


def parse_figure_boxes(text: str) -> List[Dict]:
    """Parses the JSON array of figure bounding boxes from a model reply."""
    text = text.strip()
    # Tolerate a wrapping code fence.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except Exception:
        return []
    boxes = []
    if not isinstance(data, list):
        return []
    for item in data:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            continue
        # Clamp to page and require a plausible size (>= 2% of the page;
        # button-sized icons on 2-up spreads are ~2-3%).
        x1, y1 = max(0.0, x1), max(0.0, y1)
        x2, y2 = min(1000.0, x2), min(1000.0, y2)
        if x2 - x1 < 20 or y2 - y1 < 20:
            continue
        boxes.append(
            {"bbox": [x1, y1, x2, y2], "desc": str(item.get("desc", ""))[:120]}
        )
    return boxes


def glm_detect_figures(
    png_bytes: bytes,
    api_key: str,
    base_url: str = GLM_BASE_URL,
    model: str = GLM_MODEL,
    page_no: int = 0,
    max_retries: int = 4,
) -> List[Dict]:
    """Asks the GLM vision model for normalized bounding boxes of the figures
    on a page. Returns [] when the page has no figures (or detection fails)."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    body = {
        "model": model,
        "max_tokens": 2000,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": GLM_FIGURE_PROMPT},
                ],
            }
        ],
    }
    if GLM_THINKING == "disabled":
        body["thinking"] = {"type": "disabled"}

    url = base_url.rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    last_err = ""
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=180)
            if resp.status_code != 200:
                retryable = resp.status_code == 429 or resp.status_code >= 500
                raise GlmApiError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=retryable
                )
            data = resp.json()
            text = "\n".join(
                blk.get("text", "")
                for blk in data.get("content", [])
                if blk.get("type") == "text"
            ).strip()
            if not text:
                raise GlmApiError("empty response", retryable=True)
            return parse_figure_boxes(text)
        except GlmApiError as e:
            if not e.retryable:
                print(f"[!] Figure detection failed on page {page_no}: {e}")
                return []
            last_err = str(e)
        except requests.RequestException as e:
            last_err = f"network error: {e}"
        if attempt < max_retries - 1:
            wait = min(60.0, (2 ** attempt) * 3) + random.uniform(0, 2)
            print(
                f"    [!] Figure detection page {page_no}: {last_err}; "
                f"retrying in {wait:.0f}s ({attempt + 1}/{max_retries})"
            )
            time.sleep(wait)
    print(f"[!] Figure detection failed on page {page_no}: {last_err}")
    return []


def crop_page_figure(
    doc: "fitz.Document",
    page_idx: int,
    bbox: List[float],
    out_path: str,
    dpi: int = 300,
    pad: float = 0.02,
) -> bool:
    """Crops a normalized bbox (0-1000) out of a PDF page and saves it as JPEG."""
    page = doc[page_idx]
    w, h = page.rect.width, page.rect.height
    x1, y1, x2, y2 = bbox
    # Padding as a fraction of the page (bbox is normalized 0-1000).
    pw = pad * (x2 - x1) / 1000.0
    ph = pad * (y2 - y1) / 1000.0
    clip = fitz.Rect(
        max(0.0, x1 / 1000.0 - pw) * w,
        max(0.0, y1 / 1000.0 - ph) * h,
        min(1.0, x2 / 1000.0 + pw) * w,
        min(1.0, y2 / 1000.0 + ph) * h,
    )
    if clip.is_empty or clip.width < 5 or clip.height < 5:
        return False
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    pix.save(out_path)
    return True


def embed_page_figures(
    page_md: str,
    page_idx: int,
    figures: List[Dict],
    doc: "fitz.Document",
    images_dir: str,
) -> Tuple[str, Dict[str, str]]:
    """
    Replaces sequential ``[插图：...]`` markers in a page's markdown with
    image references and returns (new_md, images_map). Figures beyond the
    marker count are ignored; markers beyond the figure count stay as text.
    """
    images_map: Dict[str, str] = {}
    counter = {"k": 0}

    def _repl(m: "re.Match") -> str:
        k = counter["k"]
        counter["k"] += 1
        if k >= len(figures):
            return m.group(0)
        rel_path = f"fig_p{page_idx + 1:04d}_{k + 1}.jpg"
        out_path = os.path.join(images_dir, rel_path)
        if not os.path.exists(out_path):
            if not crop_page_figure(doc, page_idx, figures[k]["bbox"], out_path):
                return m.group(0)
        images_map[rel_path] = ""
        # Square brackets in the alt text would break ![alt](src) syntax.
        alt = m.group(0)[1:-1].replace("[", "［").replace("]", "］")
        return f"![{alt}]({rel_path})"

    new_md = FIGURE_MARKER_RE.sub(_repl, page_md)
    return new_md, images_map


def run_glm_ocr(
    pdf_path: str,
    work_dir: str,
    api_key: str,
    base_url: str = GLM_BASE_URL,
    model: str = GLM_MODEL,
    dpi: int = GLM_DPI,
    concurrency: int = GLM_CONCURRENCY,
    page_start: int = 1,
    page_end: Optional[int] = None,
    max_tokens: int = 8000,
) -> Optional[List[Dict]]:
    """
    GLM vision OCR: renders each PDF page to PNG and transcribes it to markdown
    via the Anthropic-compatible GLM API. Per-page artifacts are checkpointed
    under <work_dir>/glm_pages/ so interrupted runs resume for free.
    Results keep the internal shape consumed by the TOC/EPUB stages
    (result.layoutParsingResults[].markdown with text + images).
    """
    render_dir = os.path.join(work_dir, "glm_images")
    pages_dir = os.path.join(work_dir, "glm_pages")
    os.makedirs(render_dir, exist_ok=True)
    os.makedirs(pages_dir, exist_ok=True)

    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    if page_end is None or page_end > total_pages:
        page_end = total_pages
    page_indices = list(range(page_start - 1, page_end))

    # Pre-render page images sequentially (PyMuPDF is not thread-safe); the
    # rendered PNGs double as a visual reference for later proofreading.
    print(f"[*] GLM OCR: rendering pages {page_start}-{page_end} at {dpi} dpi...")
    for page_idx in page_indices:
        img_file = os.path.join(render_dir, f"page_{page_idx + 1:04d}.png")
        if not os.path.exists(img_file):
            pix = doc[page_idx].get_pixmap(dpi=dpi)
            pix.save(img_file)

    todo = [
        i
        for i in page_indices
        if not os.path.exists(os.path.join(pages_dir, f"page_{i + 1:04d}.md"))
    ]
    print(
        f"[*] GLM OCR: transcribing {len(todo)} page(s) with {model} "
        f"({len(page_indices) - len(todo)} already checkpointed, workers={concurrency})"
    )

    failed: List[int] = []
    done_count = 0
    lock = threading.Lock()

    def transcribe_one(page_idx: int):
        nonlocal done_count
        img_file = os.path.join(render_dir, f"page_{page_idx + 1:04d}.png")
        page_file = os.path.join(pages_dir, f"page_{page_idx + 1:04d}.md")
        try:
            with open(img_file, "rb") as f:
                png = f.read()
            text = glm_transcribe_page(
                png,
                api_key,
                base_url=base_url,
                model=model,
                page_no=page_idx + 1,
                total_pages=total_pages,
                max_tokens=max_tokens,
            )
            text = postprocess_glm_markdown(text) or "<!-- blank page -->"
            tmp = page_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, page_file)
        except GlmApiError as e:
            with lock:
                failed.append(page_idx + 1)
                print(f"[!] Page {page_idx + 1} failed: {e}")
            return
        with lock:
            done_count += 1
            if done_count % 10 == 0 or done_count == len(todo):
                print(f"    [+] Transcribed {done_count}/{len(todo)} pages")

    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(transcribe_one, todo))

    # One sequential retry pass for transient failures.
    if failed:
        print(f"[*] Retrying {len(failed)} failed page(s) sequentially...")
        retry_failed = failed[:]
        failed = []
        done_count = 0
        todo = [p - 1 for p in retry_failed]
        for page_idx in todo:
            transcribe_one(page_idx)

    if failed:
        print(
            f"[!] {len(failed)} page(s) still failing: {failed}. "
            "Re-run the same command to retry them (checkpoints are kept)."
        )
        return None

    page_mds = []
    for page_idx in page_indices:
        with open(
            os.path.join(pages_dir, f"page_{page_idx + 1:04d}.md"), "r", encoding="utf-8"
        ) as f:
            md = f.read()
        # Re-apply post-processing so checkpointed pages from older runs pick
        # up any new cleanup rules (the pass is idempotent).
        md = postprocess_glm_markdown(md) or "<!-- blank page -->"
        page_mds.append(balance_code_fences(md))

    # A heading repeated as the FIRST line of >=3 pages is a running-header
    # echo (e.g. "# 索引" on every index page): keep only its first occurrence.
    first_heading = []
    for md in page_mds:
        m = re.match(r"^\s*(#{1,3}\s+.+?)\s*$", md.split("\n", 1)[0])
        first_heading.append(m.group(1).strip() if m else None)
    head_counts = Counter(h for h in first_heading if h)
    echo_pages = set()
    seen_heads = set()
    for i, h in enumerate(first_heading):
        if h and head_counts[h] >= 3:
            if h in seen_heads:
                echo_pages.add(i)
            else:
                seen_heads.add(h)

    # Figure extraction: pages with [插图：...] markers get a figure-detection
    # pass (checkpointed), the regions are cropped from the PDF, and the
    # markers are replaced with image references.
    figures_dir = os.path.join(work_dir, "glm_figures")
    os.makedirs(figures_dir, exist_ok=True)
    images_dir = os.path.join(work_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    def marker_count(page_i: int) -> int:
        return len(FIGURE_MARKER_RE.findall(page_mds[page_i]))

    def detect_one(page_i: int, retries: int = 1):
        fig_file = os.path.join(figures_dir, f"fig_p{page_i + 1:04d}.json")
        if os.path.exists(fig_file):
            return
        img_file = os.path.join(render_dir, f"page_{page_i + 1:04d}.png")
        try:
            with open(img_file, "rb") as f:
                png = f.read()
            boxes = glm_detect_figures(
                png, api_key, base_url=base_url, model=model, page_no=page_i + 1
            )
            # Under-detection vs marker count: retry once so genuine figures
            # are not dropped just because one pass was sloppy.
            attempts = retries
            while attempts > 0 and len(boxes) < marker_count(page_i):
                boxes2 = glm_detect_figures(
                    png, api_key, base_url=base_url, model=model, page_no=page_i + 1
                )
                if len(boxes2) > len(boxes):
                    boxes = boxes2
                attempts -= 1
        except Exception as e:
            print(f"[!] Figure detection error on page {page_i + 1}: {e}")
            boxes = []
        tmp = fig_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(boxes, f, ensure_ascii=False)
        os.replace(tmp, fig_file)

    figure_pages = [i for i, md in enumerate(page_mds) if FIGURE_MARKER_RE.search(md)]
    if figure_pages:
        print(
            f"[*] Extracting figures from {len(figure_pages)} page(s) with "
            "[插图] markers..."
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(detect_one, figure_pages))

    results = []
    markers_total = 0
    markers_replaced = 0
    bare_marker_re = re.compile(r"(?<!!)\[插图[^\]]*\]")
    img_marker_re = re.compile(r"!\[插图[^\]]*\]\(")
    for i, md in enumerate(page_mds):
        if i in echo_pages:
            md = md.split("\n", 1)[1].lstrip("\n") if "\n" in md else ""
        page_images: Dict[str, str] = {}
        if FIGURE_MARKER_RE.search(md):
            n_markers = len(FIGURE_MARKER_RE.findall(md))
            markers_total += n_markers
            fig_file = os.path.join(figures_dir, f"fig_p{i + 1:04d}.json")
            boxes = []
            if os.path.exists(fig_file):
                try:
                    with open(fig_file, "r", encoding="utf-8") as f:
                        boxes = json.load(f)
                except Exception:
                    boxes = []
            md, page_images = embed_page_figures(md, i, boxes, doc, images_dir)
            # Replaced markers live on inside image alt text (![插图...](...));
            # only bare markers count as leftovers.
            markers_replaced += len(img_marker_re.findall(md))
        results.append(
            {
                "result": {
                    "layoutParsingResults": [
                        {"markdown": {"text": md, "images": page_images}}
                    ]
                }
            }
        )

    if markers_total:
        leftovers = markers_total - markers_replaced
        print(
            f"[*] Figure coverage: {markers_replaced}/{markers_total} markers "
            f"replaced with images"
            + (f" ({leftovers} left as text)" if leftovers else "")
        )
    print(f"[+] GLM OCR complete: {len(results)} page(s) transcribed.")
    return results


def parse_pages_arg(pages_arg: Optional[str]) -> Tuple[int, Optional[int]]:
    """Parses a 'START:END' (1-based inclusive) page range argument."""
    if not pages_arg:
        return 1, None
    m = re.fullmatch(r"(\d+)(?::(\d+))?", pages_arg.strip())
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid --pages value: {pages_arg!r} (expected START:END, e.g. 1:60)"
        )
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if end < start:
        raise argparse.ArgumentTypeError("--pages END must be >= START")
    return start, end


def extract_cover_image(pdf_path: str, output_path: str) -> Optional[str]:
    """Renders the first page of a PDF as a PNG image for use as an EPUB cover."""
    doc = fitz.open(pdf_path)
    try:
        if len(doc) == 0:
            print("[!] PDF has no pages; skipping cover extraction.")
            return None
        page = doc.load_page(0)
        mat = fitz.Matrix(2, 2)  # 2x zoom (~144 DPI)
        pix = page.get_pixmap(matrix=mat)
        pix.save(output_path)
        print(f"[*] Cover image extracted to {output_path}")
        return output_path
    except Exception as e:
        print(f"[!] Failed to extract cover image: {e}")
        return None
    finally:
        doc.close()


def extract_metadata_interactive(
    results: List[Dict], default_title: str
) -> Dict[str, Optional[str]]:
    """Shows the first page OCR text and prompts the user for title and author."""
    first_page_text = ""
    try:
        first_page_text = results[0]["result"]["layoutParsingResults"][0]["markdown"][
            "text"
        ]
    except (IndexError, KeyError, TypeError):
        pass

    if first_page_text:
        print("\n--- First page OCR text ---")
        print(first_page_text.strip())
        print("----------------------------\n")
    else:
        print("\n[!] Could not extract text from the first page.\n")

    title = input(
        f"Enter book title (or press Enter to use '{default_title}'): "
    ).strip()
    if not title:
        title = default_title

    author = input("Enter author name (or press Enter to skip): ").strip()

    return {"title": title, "author": author if author else None}


# ==========================================
# --- Smart Table of Contents (TOC) Engine ---
# ==========================================

def normalize_title(s: str) -> str:
    """Normalizes title string by stripping markdown markup, Roman numerals, and punctuation."""
    orig = re.sub(r"^#+\s*", "", s.strip())
    s = re.sub(
        r"^(?:Chapter|Part|Book|Section|Lecture|Volume|Act)\s+(?:[IVXLCDM\d]+[\.\s\-–—:/|]+|[A-Z]\b[\.\s\-–—:/|]+)",
        "",
        orig,
        flags=re.I,
    )
    s = re.sub(r"^[IVXLCDM]+[\.\s\-–—:/|]+\s*", "", s, flags=re.I)
    s = re.sub(r"^\d+[\.\s\-–—:/|]+\s*", "", s)
    raw_s = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", s).lower()
    if not raw_s:
        return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", orig).lower()
    return raw_s


def is_major_chapter_title(title: str) -> bool:
    """Checks if a title represents a top-level major chapter."""
    clean = title.strip()
    clean = re.sub(r"^#+\s*", "", clean)
    clean = re.sub(r"^\|\d+\|\s*", "", clean)
    return bool(
        re.match(
            r"^(?:[IVXLCDM]+(?:\.|\b)|\d+[\.\/](?:\s+|$)|(?:Chapter|Part|Book|Section|Lecture|Volume|Act)\s+[IVXLCDM\d]+|\b(?:Chapter|Part|Book|Section|Lecture|Preface|Introduction|Foreword|Epilogue|Conclusion|Appendix|Index|Prologue|Afterword|Notes|Bibliography|Glossary)\b|HACK\s*#?\s*\d+\b|第\s*[零一二三四五六七八九十百千0-9]+\s*[篇章节讲]|第\s*[零一二三四五六七八九十百千0-9]+\s*部分|序|前言|导论|后记|附录|索引|参考文献|结语|术\s*语\s*表|词\s*汇\s*表)",
            clean,
            re.I,
        )
    )


# Structural major headings (chapter/part numbering). When a printed TOC
# contains these, plain "N. Title" entries are demoted to sub-section level.
STRUCTURAL_MAJOR_RE = re.compile(
    r"^(?:(?:Chapter|Part|Book|Section|Lecture|Volume|Act)\s+[IVXLCDM\d]+"
    r"|HACK\s*#?\s*\d+"
    r"|第\s*[零一二三四五六七八九十百千0-9]+\s*[篇章节讲]"
    r"|第\s*[零一二三四五六七八九十百千0-9]+\s*部分)",
    re.I,
)
# Plain leading-number titles ("4. The Way", "II. Intro") that may be either
# top-level chapters or sub-sections depending on the surrounding TOC.
PLAIN_NUMBER_MAJOR_RE = re.compile(r"^(?:[IVXLCDM]+\.?|\d+[\.\/])", re.I)


def is_toc_page_num_line(line: str) -> bool:
    """Checks if a line ends with a realistic TOC page number (excluding copyright years)."""
    # The page number may follow whitespace, a slash, or dot/diamond leaders.
    m = re.search(r"[\s/·.…]\s*(\d{1,4})$", line.strip())
    if not m:
        return False
    num = int(m.group(1))
    if 1800 <= num <= 2099:
        return False
    if re.search(
        r"(?:Copyright|Published|Reprinted|Printed|ISBN|All rights reserved|Press|Ltd|Inc)\b",
        line,
        re.I,
    ):
        return False
    return True


def clean_toc_line(line: str) -> str:
    """Cleans a line from the printed Table of Contents."""
    clean = line.strip()
    clean = re.sub(r"^#+\s*", "", clean)
    clean = re.sub(r"\$\s*\\underline\{(.+?)\}\s*\$", r"\1", clean).strip()
    clean = re.sub(r"^\|\d+\|\s*", "", clean).strip()
    clean = re.sub(r"^[-*•·]\s+", "", clean).strip()
    # Dot leaders between title and page number ("标题 …… 12" / "Title ... 12"),
    # and slash-separated page numbers ("标题 / 12" / "标题/12")
    clean = re.sub(r"\s*[\.…·]{2,}\s*\d{0,4}\s*$", "", clean).strip()
    clean = re.sub(r"\s*/\s*\d{1,4}\s*$", "", clean).strip()
    clean = re.sub(r"\s+\d+$", "", clean).strip()
    if not re.search(
        r"\b(?:Part|Chapter|Book|Section|Volume|Act|No|Table)\s+[IVXLCDM\d]+$",
        clean,
        re.I,
    ):
        clean = re.sub(r"\s+[ivxlcdm]+$", "", clean, flags=re.I).strip()
    return clean


def is_notes_or_citations_block(lines: List[str]) -> bool:
    """Detects if a block consists of numbered citation notes (e.g. 1. Author, Title)."""
    citation_count = sum(bool(re.match(r"^\d+\.\s+[A-Z]", l.strip())) for l in lines)
    return citation_count >= 3


def is_listing_page(lines: List[str]) -> bool:
    """Detects TOC-like listing pages (printed TOC continuation, index) where
    most lines end with page numbers — matching entries against them is bogus."""
    non_empty = [l for l in lines if l.strip()]
    if len(non_empty) < 5:
        return False
    tocish = sum(1 for l in non_empty if is_toc_page_num_line(l))
    return tocish / len(non_empty) >= 0.5


def raw_match_threshold(*raw_strings: str) -> int:
    """Contiguity-match threshold: CJK strings are information-dense, so a
    shorter substring match is already conclusive (6 CJK chars ≈ 12 latin)."""
    joined = "".join(raw_strings)
    return 6 if re.search(r"[\u4e00-\u9fff]", joined) else 12


def extract_printed_toc(results: List[Dict]) -> Optional[List[Dict]]:
    """
    Tier 1: Detects and parses the printed Table of Contents (TOC) page in the front-matter
    and matches entries against the document body for exact split pages.
    """
    toc_entries = []
    toc_block_ids = set()
    contents_pages = set()

    # 1. Search first 25 pages for printed Contents / Table of Contents / 目录 or dense page-number listings
    for p_idx, r in enumerate(results[:25]):
        for res_idx, res in enumerate(r.get("result", {}).get("layoutParsingResults", [])):
            md = res["markdown"]["text"]
            lines = [l.strip() for l in md.split("\n") if l.strip()]
            has_contents_header = any(
                re.match(
                    r"^#{1,3}\s*(?:Contents|Table of Contents|TABLE OF CONTENTS|目录|目\s*录)\b",
                    l,
                    re.I,
                )
                for l in lines
            )
            toc_like_lines = [
                l
                for l in lines
                if is_toc_page_num_line(l)
                and (is_major_chapter_title(l) or len(l.split()) >= 2)
            ]

            if has_contents_header or len(toc_like_lines) >= 3:
                contents_pages.add(p_idx)
                toc_block_ids.add((p_idx, res_idx))
                i = 0
                while i < len(lines):
                    raw_line = lines[i]
                    # Strip markdown wrappers (list bullets, bold) and dot
                    # leaders so structural checks see the plain title.
                    raw_line = re.sub(r"^\s*[-*+•·]\s+", "", raw_line)
                    raw_line = raw_line.replace("**", "").replace("__", "")
                    raw_line = re.sub(
                        r"\s*[\.…·]{2,}\s*(?=\d{1,4}\s*$)", " ", raw_line
                    )
                    if re.match(
                        r"^#{1,3}\s*(?:Contents|Table of Contents|TABLE OF CONTENTS|目录|目\s*录)\b",
                        raw_line,
                        re.I,
                    ):
                        i += 1
                        continue

                    # Combine Part headers with subtitle: "## PART I" + "Meditation: Its Spirit"
                    is_part_header = bool(
                        re.match(
                            r"^#*\s*(?:Part|Book|Section|Volume)\s+[IVXLCDM\d]+$",
                            raw_line,
                            re.I,
                        )
                    )
                    if is_part_header and i + 1 < len(lines):
                        next_line = lines[i + 1]
                        if (
                            not is_toc_page_num_line(next_line)
                            and not is_major_chapter_title(next_line)
                            and len(next_line) >= 5
                        ):
                            raw_line = f"{raw_line}: {next_line}"
                            i += 1
                            if (
                                i + 1 < len(lines)
                                and not is_toc_page_num_line(lines[i + 1])
                                and not is_major_chapter_title(lines[i + 1])
                            ):
                                i += 1

                    # Split concatenated entries like "4. The Way 90 Epilogue 131"
                    m_multi = re.search(
                        r"([a-zA-Z\.\)]\s+)(\d{1,4})\s+([A-Z][a-zA-Z\s\–\—\-\:\,\'\"]+?\s+\d{1,4})$",
                        raw_line,
                    )
                    if m_multi:
                        sub_lines = [
                            raw_line[: m_multi.start(2) + len(m_multi.group(2))],
                            m_multi.group(3),
                        ]
                    else:
                        sub_lines = [raw_line]

                    for line in sub_lines:
                        # Check if line is a lowercase dangling continuation fragment
                        if (
                            toc_entries
                            and not is_major_chapter_title(line)
                            and re.match(r"^(?:and|or|of|in|to|with|for)\b", line)
                            and is_toc_page_num_line(line)
                        ):
                            clean_line = re.sub(r"\s+\d+$", "", line).strip()
                            toc_entries[-1]["title"] += " " + clean_line
                            continue

                        has_page_num = is_toc_page_num_line(line)
                        is_major = is_major_chapter_title(line)

                        if not (has_page_num or is_major):
                            continue

                        clean = clean_toc_line(line)
                        # CJK titles are information-dense: 2 chars is already
                        # a valid entry (索引, 前言, 序言).
                        is_valid_len = len(clean) >= 3 or (
                            len(clean) >= 2
                            and re.search(r"[\u4e00-\u9fa5]", clean)
                        )
                        if is_valid_len and not clean.isdigit():
                            is_major_clean = is_major_chapter_title(clean)
                            toc_entries.append(
                                {
                                    "title": clean,
                                    "is_major": is_major_clean,
                                    "level": 1 if is_major_clean else 2,
                                }
                            )
                    i += 1

    if len(toc_entries) < 3:
        return None

    # If the TOC contains structural chapter headings (第N章 / Part / HACK #N),
    # plain leading-number entries ("4. Some Topic 90") are sub-sections, not
    # top-level chapters — demote them so EPUB splitting follows the real
    # chapter structure.
    if any(STRUCTURAL_MAJOR_RE.match(e["title"]) for e in toc_entries):
        for e in toc_entries:
            if (
                e["is_major"]
                and not STRUCTURAL_MAJOR_RE.match(e["title"])
                and PLAIN_NUMBER_MAJOR_RE.match(e["title"])
            ):
                e["is_major"] = False
                e["level"] = 2

    # 2. Match each entry to body pages
    matched = []
    start_search_page = min(contents_pages) if contents_pages else 0
    last_page = start_search_page

    for entry in toc_entries:
        target = normalize_title(entry["title"])
        if not target:
            continue
        best_score = 0
        best_page = 0
        best_match_text = None

        is_back_matter_entry = bool(
            re.search(
                r"\b(?:Notes|Bibliography|References|Index|Appendix|后记|附录|索引|参考文献)\b",
                entry["title"],
                re.I,
            )
        )

        search_start = max(start_search_page, last_page - 1)
        for p_idx in range(search_start, len(results)):
            found_strong_header = False
            for res_idx, res in enumerate(
                results[p_idx].get("result", {}).get("layoutParsingResults", [])
            ):
                if (p_idx, res_idx) in toc_block_ids:
                    continue

                page_md = res["markdown"]["text"]
                lines = page_md.split("\n")

                # Skip citation/notes blocks unless this TOC entry is actually Notes/References
                if not is_back_matter_entry and is_notes_or_citations_block(lines):
                    continue

                # On listing pages (index, TOC continuation) only header lines
                # are trustworthy anchors; the dense page-number lines would
                # falsely match almost any entry.
                if is_listing_page(lines):
                    lines = [l for l in lines if l.strip().startswith("#")]
                    if not lines:
                        continue

                for line in lines:
                    line_clean = line.strip()
                    if not line_clean:
                        continue
                    is_header = line_clean.startswith("#")
                    if not is_header and len(line_clean) > 80:
                        continue
                    line_norm = normalize_title(line_clean)
                    if not line_norm:
                        continue

                    # Exact full match check
                    raw_clean = re.sub(
                        r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", line_clean
                    ).lower()
                    raw_entry = re.sub(
                        r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", entry["title"]
                    ).lower()

                    min_len = raw_match_threshold(raw_clean, raw_entry)
                    if raw_clean == raw_entry or (
                        min(len(raw_clean), len(raw_entry)) >= min_len
                        and (raw_clean in raw_entry or raw_entry in raw_clean)
                    ):
                        score = 2.0
                    else:
                        ratio = SequenceMatcher(None, target, line_norm).ratio()
                        if is_header:
                            if ratio > 0.8 or (
                                len(target) >= 6
                                and (
                                    line_norm.startswith(target)
                                    or target.startswith(line_norm)
                                )
                            ):
                                score = ratio + 0.5
                            else:
                                score = 0
                        else:
                            if ratio > 0.88:
                                score = ratio
                            else:
                                score = 0

                    if score > best_score:
                        best_score = score
                        best_match_text = line_clean
                        best_page = p_idx + 1
                        if is_header and best_score >= 1.4:
                            found_strong_header = True
                            break
                if found_strong_header:
                    break
            if found_strong_header:
                break

        if best_page > 0 and best_score >= 0.8:
            last_page = max(last_page, best_page)
            matched.append(
                {
                    "title": entry["title"],
                    "page": best_page,
                    "level": entry["level"],
                    "is_major": entry["is_major"],
                    "matched_text": best_match_text,
                }
            )

    return matched if len(matched) >= 3 else None


def extract_and_filter_body_headings(
    results: List[Dict], book_title: Optional[str] = None
) -> List[Dict]:
    """
    Tier 2 Fallback: Scans document body for headings with intelligent filtering
    against repeating running headers, publisher noise, and generic templates.
    """
    any_header_pattern = re.compile(r"^(#{1,3})\s+(.+)$")
    latex_pattern = re.compile(r"\$\s*\\underline\{(.+?)\}\s*\$")
    candidates = []
    global_page = 0

    # Publisher / front-matter blacklist
    noise_patterns = re.compile(
        r"^(?:PENGUIN BOOKS|HARPERCOLLINS|ROUTLEDGE|SPRINGER|OXFORD UNIVERSITY PRESS|"
        r"CAMBRIDGE UNIVERSITY PRESS|SIMON & SCHUSTER|VINTAGE|MACMILLAN|WILEY|"
        r"Contents|Table of Contents|TABLE OF CONTENTS|目录|目\s*录|"
        r"ISBN\b|Copyright\b|All rights reserved|Printed in|Published by)\b",
        re.I,
    )

    norm_book_title = normalize_title(book_title) if book_title else ""

    for result in results:
        if not result or "result" not in result:
            continue
        for page_res in result["result"].get("layoutParsingResults", []):
            global_page += 1
            page_md = page_res["markdown"]["text"]
            for line in page_md.split("\n"):
                if line.strip().isdigit():
                    continue
                match = any_header_pattern.match(line)
                if match:
                    title = match.group(2).strip()
                    title = latex_pattern.sub("", title).strip()
                    title = re.sub(r"^\d+\s+", "", title).strip()

                    if len(title) < 3 or noise_patterns.match(title):
                        continue

                    candidates.append(
                        {
                            "title": title,
                            "page": global_page,
                            "level": len(match.group(1)),
                            "md_line": line,
                        }
                    )

    if not candidates:
        return []

    # Count frequencies of normalized titles to identify running headers
    title_counts = Counter(normalize_title(c["title"]) for c in candidates)

    filtered = []
    for c in candidates:
        norm = normalize_title(c["title"])
        # Filter if repeated >= 2 times (running header or repeating template)
        if title_counts[norm] >= 2:
            continue
        # Filter if matches book title
        if norm_book_title and norm == norm_book_title:
            continue
        # Filter front-matter noise on first 4 pages
        if c["page"] <= 4 and (len(c["title"]) > 60 or noise_patterns.search(c["title"])):
            continue

        is_major = is_major_chapter_title(c["title"]) or c["level"] == 1
        filtered.append(
            {
                "title": c["title"],
                "page": c["page"],
                "level": 1 if is_major else 2,
                "is_major": is_major,
            }
        )

    return filtered


def detect_chapter_headings(
    results: List[Dict], book_title: Optional[str] = None
) -> Tuple[List[Dict], List[Dict], str]:
    """
    Dual-Tier TOC Engine:
    1. Tries to extract the printed Table of Contents from front-matter.
    2. Falls back to body scanning with noise suppression.
    Returns (major_headings, all_headings, source_str).
    """
    # Tier 1: Printed TOC
    printed_matched = extract_printed_toc(results)
    if printed_matched:
        major = [h for h in printed_matched if h["is_major"]]
        if len(major) < 2:
            major = printed_matched
        return major, printed_matched, "Printed Table of Contents"

    # Tier 2: Body scanning fallback
    body_headings = extract_and_filter_body_headings(results, book_title)
    major = [h for h in body_headings if h["is_major"]]
    if len(major) < 3:
        major = body_headings
    return major, body_headings, "Body Heading Scanner"


def review_toc_interactive(
    major_headings: List[Dict],
    all_headings: List[Dict],
    source: str = "Printed Table of Contents",
) -> List[Dict]:
    """
    Interactive prompt for reviewing and editing the detected TOC.
    Offers major chapters as default and sub-sections via 'all'.
    """
    has_subsections = len(all_headings) > len(major_headings)
    active_headings = list(major_headings)
    viewing_all = False

    def _print_heading_list(headings):
        print(f"  {'#':>3}  | {'Pg':>4} | Level | Heading")
        print(f"  {'---':>3}--+------+-------+{'-' * 44}")
        for i, h in enumerate(headings):
            lvl_str = "H1" if h["level"] == 1 else "  H2"
            indent = "" if h["level"] == 1 else "  "
            print(f"  {i+1:>3}  | {h['page']:>4} |  {lvl_str}  | {indent}{h['title']}")

    def _show_options():
        print()
        print("Options:")
        if has_subsections and not viewing_all:
            print(
                f"  [Enter]    Accept Major Chapters only ({len(major_headings)} chapters)"
            )
            print(
                f"  all        Show ALL {len(all_headings)} chapters and sub-sections"
            )
        else:
            print(f"  [Enter]    Accept current list ({len(active_headings)} chapters)")
            if has_subsections and viewing_all:
                print(f"  major      Show only Major Chapters ({len(major_headings)} chapters)")

        print("  1,3,5      Remove headings by number (comma-separated)")
        print("  +1,3,5     Keep ONLY these headings (comma-separated)")
        print("  none       No chapters (entire book as single chapter)")
        print()

    print(
        f"\n--- Detected Chapter Headings (Source: {source} | {len(major_headings)} Major Chapters, {len(all_headings)} Total) ---"
    )
    _print_heading_list(active_headings)
    _show_options()

    while True:
        choice = input("Your choice: ").strip()

        if choice == "":
            return list(active_headings)
        elif choice.lower() == "none":
            return []
        elif choice.lower() == "all" and has_subsections and not viewing_all:
            active_headings = list(all_headings)
            viewing_all = True
            print(f"\n--- All Headings ({len(active_headings)} chapters & sub-sections) ---")
            _print_heading_list(active_headings)
            _show_options()
            continue
        elif choice.lower() == "major" and has_subsections and viewing_all:
            active_headings = list(major_headings)
            viewing_all = False
            print(f"\n--- Major Chapters ({len(active_headings)}) ---")
            _print_heading_list(active_headings)
            _show_options()
            continue
        else:
            try:
                keep_mode = choice.startswith("+")
                if keep_mode:
                    choice = choice[1:]

                nums = set()
                valid = True
                for part in choice.split(","):
                    part = part.strip().lstrip("-")
                    num = int(part)
                    if 1 <= num <= len(active_headings):
                        nums.add(num)
                    else:
                        print(
                            f"  [!] Invalid number: {num} (must be 1-{len(active_headings)})"
                        )
                        valid = False
                        break

                if not valid:
                    continue

                if keep_mode:
                    confirmed = [
                        h for i, h in enumerate(active_headings) if (i + 1) in nums
                    ]
                else:
                    confirmed = [
                        h for i, h in enumerate(active_headings) if (i + 1) not in nums
                    ]

                if confirmed:
                    print(f"\nUpdated TOC ({len(confirmed)} chapters):")
                    _print_heading_list(confirmed)
                else:
                    print("\n  All headings removed. Book will be a single chapter.")

                confirm = input("\nConfirm? [Y/n]: ").strip().lower()
                if confirm in ("", "y", "yes"):
                    return confirmed
                else:
                    print("\n--- Current Chapter Headings ---")
                    _print_heading_list(active_headings)
                    _show_options()
                    continue

            except ValueError:
                print(
                    "  [!] Invalid input. Use numbers like '1,3,5' or '+1,3,5' for keep mode, or 'all'/'major'."
                )
                continue


def find_matching_heading(
    line_text: str,
    confirmed_headings: List[Dict],
    start_idx: int = 0,
    lookahead_count: int = 2,
    current_page: int = 0,
) -> tuple[Optional[Dict], int]:
    """
    Finds if a line matches any of the expected upcoming confirmed headings
    in sequential order. Returns (matched_heading, next_heading_idx).
    """
    clean_line = line_text.strip()
    if not clean_line or is_toc_page_num_line(clean_line):
        return None, start_idx

    h_text = re.sub(r"^#+\s*", "", clean_line)
    h_text = re.sub(r"\$\s*\\underline\{(.+?)\}\s*\$", "", h_text).strip()
    h_text = re.sub(r"^\d+\s+", "", h_text).strip()

    raw_line = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", h_text).lower()
    norm_line = normalize_title(h_text)

    search_limit = min(start_idx + lookahead_count, len(confirmed_headings))
    for idx in range(start_idx, search_limit):
        target_h = confirmed_headings[idx]
        if current_page > 0 and current_page < target_h.get("page", 0) - 1:
            continue

        raw_h = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", target_h["title"]).lower()
        norm_h = normalize_title(target_h["title"])

        matched = False
        if target_h.get("matched_text") and clean_line == target_h["matched_text"].strip():
            matched = True
        elif raw_line == raw_h or (
            min(len(raw_line), len(raw_h)) >= raw_match_threshold(raw_line, raw_h)
            and (raw_line in raw_h or raw_h in raw_line)
        ):
            matched = True
        elif (
            clean_line.startswith("#")
            and norm_line
            and (
                norm_line == norm_h
                or (
                    len(norm_line) >= 5
                    and SequenceMatcher(None, norm_line, norm_h).ratio() > 0.8
                )
            )
        ):
            matched = True

        if matched:
            return target_h, idx + 1

    return None, start_idx


def detect_language(text: str) -> str:
    """Guesses the EPUB language code from the ratio of CJK characters."""
    if not text:
        return "en"
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return "zh" if cjk > len(text) * 0.1 else "en"


_CJK_CHAR_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")


def is_cjk_char(ch: str) -> bool:
    """True for CJK ideographs, CJK punctuation, and fullwidth forms."""
    return bool(ch) and bool(_CJK_CHAR_RE.match(ch))


def sanitize_markdown_for_xhtml(lines: List[str]) -> List[str]:
    """
    Escapes stray '<' characters outside fenced code blocks and inline code
    spans. Text like disassembly annotations ("8048427 <main+0x5b>") would
    otherwise be passed through by python-markdown as raw HTML, producing
    invalid XHTML (and mangling the text with bogus closing tags).
    """
    out = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code or line.strip().startswith("<!--"):
            out.append(line)
            continue
        # The model sometimes emits <br> for in-cell line breaks; render it
        # as a space instead of literal escaped text.
        if re.search(r"<br\s*/?>", line, re.I):
            line = re.sub(r"<br\s*/?>", " ", line, flags=re.I)
        if "<" in line:
            # Even-indexed segments of a backtick split are outside inline
            # code spans.
            parts = line.split("`")
            for i in range(0, len(parts), 2):
                parts[i] = parts[i].replace("<", "&lt;")
            line = "`".join(parts)
        out.append(line)
    return out


# Markdown image syntax that survived conversion as a literal (python-markdown's
# raw-HTML block parser can leave ![alt](src) unrendered on adversarial input).
LITERAL_IMG_MD_RE = re.compile(r"!\[([^\]\n]*)\]\(\s*([^\s)]+)\s*\)")


def force_render_images(html_text: str) -> str:
    """Converts any surviving literal ![alt](src) markdown into <img> tags."""

    def _repl(m: "re.Match") -> str:
        alt = m.group(1).replace('"', "&quot;")
        return f'<img src="{m.group(2)}" alt="{alt}" />'

    return LITERAL_IMG_MD_RE.sub(_repl, html_text)


def balance_code_fences(md: str) -> str:
    """Appends a closing fence when a page's markdown ends inside a code
    block, so the unclosed fence cannot swallow following pages' content."""
    in_code = False
    for l in md.split("\n"):
        if l.strip().startswith("```"):
            in_code = not in_code
    if in_code:
        md = md.rstrip("\n") + "\n```\n"
    return md


def create_epub(
    title: str,
    results: List[Dict],
    output_file: str,
    image_dir: str,
    cover_image_path: Optional[str] = None,
    author: Optional[str] = None,
    confirmed_headings: Optional[List[Dict]] = None,
    language: Optional[str] = None,
):
    """
    Creates an EPUB file from the aggregated API results.
    """
    book = epub.EpubBook()
    book.set_identifier(f"id_{title}")
    book.set_title(title)
    book.set_language(language or "en")

    if author:
        book.add_author(author)

    # Set cover image
    if cover_image_path and os.path.exists(cover_image_path):
        with open(cover_image_path, "rb") as f:
            cover_data = f.read()
        book.set_cover("cover.png", cover_data)

    chapters = []

    # CSS for the book
    style = """
    body { font-family: serif; line-height: 1.8; text-align: justify; margin: 1em; }
    h1 { text-align: center; margin: 1.5em 0 0.8em 0; font-size: 1.6em; }
    h2 { margin: 1.2em 0 0.6em 0; font-size: 1.3em; }
    h3 { margin: 1em 0 0.5em 0; font-size: 1.1em; }
    p { margin-bottom: 0.8em; text-indent: 0; }
    blockquote { margin: 1em 2em; font-style: italic; }
    pre { background-color: #f4f4f4; padding: 0.6em; white-space: pre-wrap; word-break: break-all; font-size: 0.85em; }
    code { font-family: monospace; font-size: 0.9em; }
    img { max-width: 100%; height: auto; display: block; margin: 1em auto; }
    """
    nav_css = epub.EpubItem(
        uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style
    )
    book.add_item(nav_css)

    full_markdown = ""
    global_page_counter = 0

    for chunk_idx, result in enumerate(results):
        if not result or "result" not in result:
            continue

        layout_results = result["result"].get("layoutParsingResults", [])

        for i, page_res in enumerate(layout_results):
            global_page_counter += 1
            page_md = page_res["markdown"]["text"]
            images_map = page_res["markdown"].get("images", {})

            for rel_path, img_url in images_map.items():
                local_img_path = os.path.join(image_dir, rel_path)
                if os.path.exists(local_img_path):
                    with open(local_img_path, "rb") as img_f:
                        img_data = img_f.read()

                    epub_img = epub.EpubImage()
                    epub_img.file_name = rel_path
                    epub_img.media_type = "image/jpeg"
                    epub_img.content = img_data

                    if rel_path not in [item.file_name for item in book.get_items()]:
                        book.add_item(epub_img)

            # Paragraph reflowing. Fenced code blocks and table rows pass
            # through verbatim as contiguous blocks; bare page-number lines
            # are dropped outside code.
            lines = page_md.split("\n")
            cleaned_lines = []
            in_code_block = False
            for line in lines:
                if line.strip().startswith("```"):
                    in_code_block = not in_code_block
                    cleaned_lines.append(line.rstrip())
                    continue
                if in_code_block:
                    cleaned_lines.append(line)
                    continue
                if line.strip().isdigit():
                    continue
                cleaned_lines.append(line)

            reflowed_paragraphs = []
            current_para = ""
            verbatim_block = []  # consecutive verbatim lines (code/table)

            terminal_chars = (".", "!", "?", '"', "'", ")", "]", "}", ":", ";")
            header_pattern = re.compile(r"^(#{1,6}\s|>|\s*[-*]\s|\d+\.)")
            non_join_endings = (":", ";", ",", "-")

            def flush_para():
                if current_para:
                    reflowed_paragraphs.append(current_para)

            def flush_verbatim():
                if verbatim_block:
                    reflowed_paragraphs.append("\n".join(verbatim_block))

            for line in cleaned_lines:
                stripped = line.rstrip()

                if in_code_block:
                    verbatim_block.append(line)
                    if stripped.startswith("```"):
                        in_code_block = False
                        flush_verbatim()
                        verbatim_block = []
                    continue

                if stripped.startswith("```"):
                    flush_para()
                    current_para = ""
                    flush_verbatim()
                    verbatim_block = []
                    in_code_block = True
                    verbatim_block.append(stripped)
                    continue

                if not stripped:
                    flush_para()
                    current_para = ""
                    flush_verbatim()
                    verbatim_block = []
                    continue

                # Markdown table rows stay contiguous so the tables
                # extension can render them.
                if stripped.startswith("|"):
                    flush_para()
                    current_para = ""
                    verbatim_block.append(stripped)
                    continue
                if verbatim_block:
                    flush_verbatim()
                    verbatim_block = []

                is_special = header_pattern.match(stripped)

                if is_special:
                    flush_para()
                    current_para = ""
                    reflowed_paragraphs.append(stripped)
                elif not current_para:
                    current_para = stripped
                else:
                    current_ends = current_para.rstrip()
                    if current_ends.endswith(terminal_chars):
                        reflowed_paragraphs.append(current_para)
                        current_para = stripped
                    elif current_ends and current_ends[-1] in non_join_endings:
                        reflowed_paragraphs.append(current_para)
                        current_para = stripped
                    elif len(stripped) < 20 and not stripped.endswith(terminal_chars):
                        reflowed_paragraphs.append(current_para)
                        current_para = stripped
                    else:
                        # CJK text joins without an inserted space.
                        prev_last = current_ends[-1] if current_ends else ""
                        if is_cjk_char(prev_last) or is_cjk_char(stripped[0]):
                            current_para = current_ends + stripped
                        else:
                            current_para = current_ends + " " + stripped

            if current_para:
                reflowed_paragraphs.append(current_para)
            flush_verbatim()

            page_markdown = f"<!-- page: {global_page_counter} -->\n" + "\n\n".join(
                reflowed_paragraphs
            )

            if full_markdown and page_markdown:
                full_stripped = full_markdown.rstrip()
                page_lines = page_markdown.split("\n")
                first_page_line = (
                    page_lines[1].strip() if len(page_lines) > 1 else ""
                )

                ends_mid_sentence = full_stripped and not full_stripped.endswith(
                    terminal_chars
                )
                next_is_header = (
                    header_pattern.match(first_page_line) if first_page_line else False
                )

                if ends_mid_sentence and not next_is_header:
                    full_markdown = (
                        full_markdown.rstrip() + "\n" + page_markdown + "\n\n"
                    )
                else:
                    full_markdown += page_markdown + "\n\n"
            else:
                full_markdown += page_markdown + "\n\n"

    md_lines = full_markdown.split("\n")
    if not language:
        language = detect_language(full_markdown)
    current_chapter_title = "Start"
    current_chapter_content = []
    chapter_count = 0
    next_heading_idx = 0
    current_page = 0

    major_header_pattern = re.compile(
        r"^(#{1,3})\s+(?:Chapter|Part|Lecture|Preface|Intro|Appendix|Prologue|Epilogue|Conclusion|Book|Acknowledgements|Contents|Abstract|HACK\s*#?\s*\d+|序|前言|导论|目录|第\s*[零一二三四五六七八九十百千0-9]+\s*[篇章讲]|第\s*[零一二三四五六七八九十百千0-9]+\s*部分).*"
    )
    any_header_pattern = re.compile(r"^(#{1,3})\s+(.+)$")

    for line in md_lines:
        if line.startswith("<!-- page: "):
            m_pg = re.search(r"<!-- page:\s*(\d+)\s*-->", line)
            if m_pg:
                current_page = int(m_pg.group(1))
            continue

        match = any_header_pattern.match(line)
        is_split_point = False
        clean_matched_title = None

        if confirmed_headings is not None:
            matched_h, next_idx = find_matching_heading(
                line,
                confirmed_headings,
                start_idx=next_heading_idx,
                current_page=current_page,
            )
            if matched_h:
                clean_matched_title = matched_h["title"]
                if clean_matched_title != current_chapter_title:
                    is_split_point = True
                    next_heading_idx = next_idx
        elif match:
            heading_text = match.group(2).strip()
            heading_text = re.sub(
                r"\$\s*\\underline\{(.+?)\}\s*\$", "", heading_text
            ).strip()
            heading_text = re.sub(r"^\d+\s+", "", heading_text).strip()
            is_split_point = bool(major_header_pattern.match(line))
            if is_split_point:
                clean_matched_title = heading_text

        # Clean up LaTeX artifacts
        line = re.sub(r"\$\s*\^\{(.+?)\}\s*\$", r"", line)
        line = re.sub(r"\$\s*\\underline\{(.+?)\}\s*\$", "", line)

        if is_split_point:
            # Check if previous chapter has meaningful content before saving
            content_text = "".join(current_chapter_content).strip()
            if current_chapter_content and len(content_text) > 30:
                safe_title = "".join(
                    [
                        c
                        for c in current_chapter_title
                        if c.isalnum() or c in (" ", "_", "-")
                    ]
                ).strip()
                if not safe_title:
                    safe_title = f"chap_{chapter_count}"

                c = epub.EpubHtml(
                    title=current_chapter_title,
                    file_name=f"{safe_title}_{chapter_count}.xhtml",
                    lang=language,
                )

                try:
                    import markdown

                    html_content = force_render_images(
                        markdown.markdown(
                            "\n".join(
                                sanitize_markdown_for_xhtml(current_chapter_content)
                            ),
                            extensions=["fenced_code", "tables"],
                        )
                    )
                except ImportError:
                    html_content = (
                        "<p>"
                        + "</p><p>".join(current_chapter_content)
                        + "</p>"
                    )

                c.content = f"<html><head><link rel='stylesheet' href='style/nav.css'/></head><body>{html_content}</body></html>"
                c.add_item(nav_css)
                book.add_item(c)
                chapters.append(c)
                chapter_count += 1

            current_chapter_title = clean_matched_title or match.group(2)
            current_chapter_content = [line]
        else:
            current_chapter_content.append(line)

    # Add last chapter
    if current_chapter_content:
        safe_title = "".join(
            [c for c in current_chapter_title if c.isalnum() or c in (" ", "_", "-")]
        ).strip()
        if not safe_title:
            safe_title = f"chap_{chapter_count}"

        c = epub.EpubHtml(
            title=current_chapter_title,
            file_name=f"{safe_title}_{chapter_count}.xhtml",
            lang=language,
        )
        try:
            import markdown

            html_content = force_render_images(
                markdown.markdown(
                    "\n".join(sanitize_markdown_for_xhtml(current_chapter_content)),
                    extensions=["fenced_code", "tables"],
                )
            )
        except ImportError:
            html_content = "<p>" + "</p><p>".join(current_chapter_content) + "</p>"

        c.content = f"<html><head><link rel='stylesheet' href='style/nav.css'/></head><body>{html_content}</body></html>"
        c.add_item(nav_css)
        book.add_item(c)
        chapters.append(c)

    if not chapters and full_markdown:
        c = epub.EpubHtml(title="Content", file_name="content.xhtml", lang=language)
        try:
            import markdown

            html_content = force_render_images(
                markdown.markdown(
                    "\n".join(sanitize_markdown_for_xhtml(full_markdown.split("\n"))),
                    extensions=["fenced_code", "tables"],
                )
            )
        except ImportError:
            html_content = "<p>" + "</p><p>".join(full_markdown.split("\n")) + "</p>"
        c.content = f"<html><head><link rel='stylesheet' href='style/nav.css'/></head><body>{html_content}</body></html>"
        book.add_item(c)
        chapters.append(c)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters

    epub.write_epub(output_file, book, {})
    print(f"[*] EPUB saved to {output_file}")


def main():
    if not check_dependencies():
        return

    parser = argparse.ArgumentParser(
        description="Scanned PDF to EPUB Converter (powered by GLM-5.3-Flash vision OCR)"
    )
    parser.add_argument("input_pdf", help="Path to input PDF file")
    parser.add_argument(
        "--output", "-o", help="Path to output EPUB file (default: input_name.epub)"
    )
    parser.add_argument("--title", help="Book title (skips interactive prompt)")
    parser.add_argument("--author", help="Author name (skips interactive prompt)")
    parser.add_argument(
        "--auto-toc",
        action="store_true",
        help="Skip interactive TOC review; use auto-detected headings",
    )
    parser.add_argument(
        "--no-toc",
        action="store_true",
        help="Skip heading detection; produce single-chapter EPUB",
    )
    parser.add_argument(
        "--glm-model",
        default=GLM_MODEL,
        help=f"GLM vision model name (default: {GLM_MODEL})",
    )
    parser.add_argument(
        "--glm-url",
        default=GLM_BASE_URL,
        help=f"GLM Anthropic-compatible API base URL (default: {GLM_BASE_URL})",
    )
    parser.add_argument(
        "--glm-max-tokens",
        type=int,
        default=GLM_MAX_TOKENS_DEFAULT,
        help=f"Max output tokens per page transcription (default: {GLM_MAX_TOKENS_DEFAULT})",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=GLM_DPI,
        help=f"Rendering DPI for page images sent to GLM (default: {GLM_DPI})",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=GLM_CONCURRENCY,
        help=f"Parallel page transcriptions (default: {GLM_CONCURRENCY})",
    )
    parser.add_argument(
        "--pages",
        help="Only transcribe this 1-based inclusive page range, e.g. 1:60 "
        "(useful for testing or retrying a range)",
    )
    parser.add_argument(
        "--language",
        help="EPUB language code, e.g. zh / en (default: auto-detect from content)",
    )
    args = parser.parse_args()

    input_path = args.input_pdf
    if not os.path.exists(input_path):
        print(f"Error: File {input_path} not found.")
        return

    if args.auto_toc and args.no_toc:
        print("[!] Error: --auto-toc and --no-toc are mutually exclusive.")
        return

    glm_key, glm_base = resolve_glm_credentials()
    if not glm_key:
        print("[!] Error: No GLM API key found.")
        print("    Set GLM_API_KEY in the environment or .env, or make sure the")
        print(f"    ZCode coding-plan config exists at {ZCODE_CONFIG_PATH}.")
        return
    # An explicit --glm-url always wins; otherwise prefer the base URL
    # paired with the discovered key.
    if args.glm_url == GLM_BASE_URL:
        args.glm_url = glm_base

    if not args.output:
        args.output = os.path.splitext(input_path)[0] + ".epub"

    # Check total pages
    doc = fitz.open(input_path)
    total_pages = len(doc)
    doc.close()
    print(f"[*] Total pages: {total_pages}")

    # Unique work directory based on filename and size hash
    import hashlib

    file_stat = os.stat(input_path)
    file_sig = f"{os.path.basename(input_path)}_{file_stat.st_size}"
    file_hash = hashlib.md5(file_sig.encode("utf-8")).hexdigest()[:8]
    work_dir = f"{WORK_DIR_PREFIX}_{file_hash}"

    print(f"[*] Work directory: {work_dir}")

    image_dir = os.path.join(work_dir, "images")
    os.makedirs(image_dir, exist_ok=True)

    results_file = os.path.join(work_dir, "results.json")
    pages_range = None
    if args.pages:
        pages_range = parse_pages_arg(args.pages)
        # A partial run must not be mistaken for a completed full run later.
        results_file = os.path.join(
            work_dir, f"results_p{pages_range[0]}_p{pages_range[1]}.json"
        )

    try:
        # Step 1: Extract cover image
        cover_path = extract_cover_image(
            input_path, os.path.join(work_dir, "cover.png")
        )

        # Step 2: OCR via GLM vision (resumes from checkpoints)
        results = []
        if pages_range or not os.path.exists(results_file):
            page_start, page_end = pages_range or (1, None)
            results = run_glm_ocr(
                input_path,
                work_dir,
                glm_key,
                base_url=args.glm_url,
                model=args.glm_model,
                dpi=args.dpi,
                concurrency=args.concurrency,
                page_start=page_start,
                page_end=page_end,
                max_tokens=args.glm_max_tokens,
            )
            if results is None:
                print(
                    "[!] CRITICAL: GLM OCR did not complete. Aborting "
                    "(page checkpoints are kept; re-run to resume)."
                )
                sys.exit(1)
            with open(results_file, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
        else:
            print(
                "    [+] Resuming: Found completed OCR results from previous run."
            )
            with open(results_file, "r", encoding="utf-8") as f:
                results = json.load(f)

        # Step 2.5: Metadata extraction
        default_title = os.path.splitext(os.path.basename(input_path))[0]
        if args.title:
            metadata = {"title": args.title, "author": args.author}
        else:
            metadata = extract_metadata_interactive(results, default_title)

        # Step 2.75: TOC Detection & Review
        if args.no_toc:
            confirmed_headings = []
        elif args.auto_toc:
            major_h, all_h, _ = detect_chapter_headings(results, metadata.get("title"))
            confirmed_headings = major_h
        else:
            print("[-] Step 2.75: Detecting chapter headings...")
            major_h, all_h, src = detect_chapter_headings(
                results, metadata.get("title")
            )
            confirmed_headings = review_toc_interactive(
                major_h, all_h, source=src
            )

        # Step 3: Generation
        print("[-] Generating EPUB...")
        create_epub(
            metadata["title"],
            results,
            args.output,
            image_dir,
            cover_image_path=cover_path,
            author=metadata["author"],
            confirmed_headings=confirmed_headings,
            language=args.language,
        )

    finally:
        print(
            f"[*] Done. Intermediate files are in '{work_dir}'. You can delete this folder if verified."
        )


if __name__ == "__main__":
    main()
