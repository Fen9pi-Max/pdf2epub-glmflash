# pdf2epub-glmflash

[English](README.md) | [中文](README_zh.md)

使用 **GLM-5.3-Flash** 视觉模型把扫描版 PDF 书籍转换为清晰、易读的 EPUB 电子书。每页渲染成图后逐字转录为忠实的 Markdown（代码块、表格、列表全部保留），再重新组装为按章节切分的 EPUB——插图从原 PDF 裁剪并嵌入原位置。

> **⚠️ 使用声明 —— 仅供个人学习。** 本项目是一个**格式转存**工具：请仅用于转换**你合法拥有**的 PDF 资料（正版购入的电子书、课程讲义、论文、技术文档等），方便在阅读器上学习阅读。本项目不提供、不存储、不传播任何书籍资源本身；转换产物请勿分发，勿用于盗版传播，并请遵守所在国家/地区的版权法律法规。

## 功能特性

- **GLM-5.3-Flash 视觉转录**：逐页转录为 Markdown，保留标题层级、围栏代码块、表格和列表；自动丢弃页眉、页码和扫描水印（如 www.TopSage.com、Anna's Archive 生成页）。
- **颜色同步**：原书中以明显不同颜色印刷的文字（如蓝色强调句）会被识别，并在 EPUB 中以内联 CSS 颜色还原（`==[蓝]…==` 标记 → `<span style="color:…">`）。
- **图片不丢、位置正确**：逐页检测插图区域，从 PDF 以 300 dpi 裁剪，嵌入其在书中的精确位置；结束时输出覆盖率报告。
- **版式适应**：双栏排版与 2-up 双联页扫描（一个 PDF 页 = 两个书页）都有明确的阅读顺序规则。
- **智能章节分割**：双层目录引擎——优先解析印刷目录（支持 `…… 12`、`/ 12`、`· 12` 等多种页码格式），回退到正文标题扫描（带噪声抑制）；交互式确认后再生成。
- **断点续传**：按页保存转录检查点，中断（包括 API 配额窗口）后重跑自动续传。
- **中文友好**：中文段落拼接不引入多余空格；语言自动检测（可用 `--language` 覆盖）；识别 第N章 / 第N部分 / HACK #N / 术语表 等中文标题。
- **XHTML 严格合法**：裸 `<` 转义、代码围栏平衡、图片渲染兜底，生成的 EPUB 全部可解析。

## 前置要求

- Python 3.8+
- GLM API Key（如 GLM Coding Plan）

## 获取 API 访问

在 `.env` 中设置 `GLM_API_KEY`；留空时自动从本机 ZCode 配置发现密钥（`~/.zcode/v2/config.json`，优先 coding-plan 提供商，其次任何带密钥的 Anthropic 兼容提供商）。可选：`GLM_BASE_URL`（默认 `https://open.bigmodel.cn/api/anthropic`）、`GLM_MODEL`（默认 `GLM-5.3-Flash`）。

> 本项目为独立开源项目，与智谱 AI / Z.ai 无隶属或合作关系；使用需自备 GLM API 权限。

## 安装

```bash
git clone <this-repo> pdf2epub-glmflash
cd pdf2epub-glmflash
python3 -m venv .venv
.venv/bin/pip install requests pymupdf EbookLib python-dotenv markdown

cp .env.example .env   # 如需显式设置密钥则编辑
```

（也可用 `uv run pdf2epub_glmflash.py ...`）

## 使用

```bash
.venv/bin/python pdf2epub_glmflash.py /path/to/book.pdf

# 跳过交互提示
.venv/bin/python pdf2epub_glmflash.py --title "书名" --author "作者" --auto-toc book.pdf

# 单章节 EPUB（不拆分章节）
.venv/bin/python pdf2epub_glmflash.py --no-toc -o out.epub book.pdf

# 只处理部分页面（测试/重试，断点保证开销极小）
.venv/bin/python pdf2epub_glmflash.py --pages 1:60 --auto-toc book.pdf

# 调优
.venv/bin/python pdf2epub_glmflash.py --dpi 200 --concurrency 6 --glm-max-tokens 16000 book.pdf
```

### 命令行参数

| 参数 | 含义 |
| --- | --- |
| `--title` / `--author` | 元数据；跳过交互提示 |
| `--output`, `-o` | 输出 EPUB 路径（默认 `<输入名>.epub`） |
| `--auto-toc` | 使用自动检测的章节，跳过确认 |
| `--no-toc` | 单章节 EPUB（与 `--auto-toc` 互斥） |
| `--pages A:B` | 只转录该 1 起始闭区间页码范围 |
| `--language` | EPUB 语言代码（默认自动检测，如 `zh`） |
| `--glm-model` / `--glm-url` / `--glm-max-tokens` | API 覆盖项 |
| `--dpi` / `--concurrency` | 渲染 DPI 与并发转录数 |

### 环境变量

`GLM_API_KEY`、`GLM_BASE_URL`、`GLM_MODEL`、`GLM_DPI`、`GLM_CONCURRENCY`、`GLM_MAX_TOKENS`（16000）、`GLM_THINKING`（默认 `disabled`——OCR 无需深度推理，约 5 倍速且质量不变）。

### 全局命令

```bash
sudo ln -s "$(pwd)/epub" /usr/local/bin/epub
epub /path/to/book.pdf
```

### 工作目录与续传

每个输入对应 `glmflash_work_<hash>/`：`glm_pages/`（逐页 Markdown 检查点）、`glm_images/`（渲染页 PNG，可用于校对）、`glm_figures/`（插图检测结果）、`images/`（裁剪插图）、`results.json`（组装结果）、`cover.png`。重跑同一文件自动续传；`--pages A:B` 时组装文件为 `results_pA_pB.json`，避免部分运行被误认为完整结果。

## 开源协议

[MIT License](LICENSE)
