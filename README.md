# pdf-layout-translate

把 PDF 课件、讲义、论文、作业翻译成**简体中文并保留原版式**，效果类似 WPS 的文档翻译。截图、插图和扫描页里的文字也一起翻译。这是一个 [Agent Skill](https://agentskills.io)：翻译由你正在用的 AI agent 自己完成，**不需要翻译 API key，文件不上传**。

[English](#english)

## 特点

- **保版式**：排版引擎是 [BabelDOC](https://github.com/funstory-ai/BabelDOC)。公式、代码、图片、页眉页脚保持原样，列表、表格单元格、手动换行逐段对应。
- **截图里的文字也翻译**：用本机 OCR（[RapidOCR](https://github.com/RapidAI/RapidOCR) + PaddleOCR PP-OCRv5）识别，按原位置、原颜色画回中文。代码、数据、路径保持原样。
- **针对不同来源分别处理**：
  - Keynote：处理行内公式和隐藏文字。
  - Google Slides、PowerPoint：拆分列表和表格单元格，保留文字颜色。
  - LaTeX：保持原段落结构。
  - 扫描件：走图片识别。
- **输出**：纯中文版、中外左右对照版，以及逐页对照图，方便检查。
- **本地运行**：只有首次安装时联网下载依赖和模型。

## 支持的 agent

任何支持 Agent Skills（`SKILL.md`）、并且能执行命令的 agent 都可以用，例如 Claude Code、Codex CLI、Gemini CLI、Antigravity、OpenCode。

支持的系统：
- Windows x64；Windows on ARM 会自动使用 x64 版 Python，通过模拟运行。
- macOS（Intel 和 Apple 芯片）。
- Linux（x86-64 和 ARM64，glibc 发行版）。Alpine 等 musl 系统不支持：BabelDOC 依赖的 onnxruntime 没有 musl 版本。

## 安装

先克隆或下载本仓库，然后任选一种方式安装。

**一键安装到本机已有的 agent**：

```bash
python install.py              # 自动找到 Claude Code / Codex / Gemini / Antigravity / OpenCode 的技能目录
python install.py --list       # 只看会装到哪里
python install.py --agent codex --setup   # 只装 Codex，并立即安装运行环境
```

**Claude Code 插件市场**（把 `OWNER` 换成仓库所有者）：

```
/plugin marketplace add OWNER/pdf-layout-translate
/plugin install pdf-layout-translate@pdf-layout-translate
```

**手动安装**：把 `skills/pdf-layout-translate` 整个文件夹复制到 agent 的技能目录。

| Agent | 个人技能目录 |
|---|---|
| Claude Code | `~/.claude/skills/` |
| Codex CLI | `~/.codex/skills/`（或 `$CODEX_HOME/skills/`），也读 `~/.agents/skills/` |
| Gemini CLI | `~/.gemini/skills/` |
| Antigravity | `~/.gemini/antigravity/skills/` |
| OpenCode | `~/.config/opencode/skills/` |

装好后新开一个 agent 会话，让它重新加载技能。

## 首次运行

第一次使用时会**自动安装私有运行环境**，之后可以离线使用：
- 内容：Python 3.12、BabelDOC、版面模型、字体、OCR 模型，以及 uv 的缓存，约 1.5 GB。全部放在一个文件夹里。
- 耗时 5–15 分钟，需要联网。中断后重跑会接着装。
- 不会改动系统的 Python。

也可以提前手动安装和检查：

```bash
python skills/pdf-layout-translate/scripts/lt.py setup
python skills/pdf-layout-translate/scripts/lt.py check
```

- 需要任意 Python 3.9 以上来启动。没有 [uv](https://github.com/astral-sh/uv) 时会下载固定版本的官方 uv，并校验 sha256，再由它准备 Python 3.12。
- 首次安装会访问这些地方：
  - GitHub：uv 和 Python。
  - PyPI：依赖包。
  - Hugging Face 或 ModelScope：BabelDOC 的模型和字体。
  - ModelScope：OCR 模型。
  - google/fonts 仓库：非 Windows 系统下载 STIX Two 字体，固定版本并校验。
- 网络受限时：需要代理就设置 `HTTPS_PROXY`；用 PyPI 镜像就设置 `UV_DEFAULT_INDEX`，例如 `https://pypi.tuna.tsinghua.edu.cn/simple`。
- 运行环境的位置：

| 系统 | 默认位置 |
|---|---|
| Windows | `%LOCALAPPDATA%\pdf-layout-translate` |
| macOS | `~/Library/Application Support/pdf-layout-translate` |
| Linux | `~/.local/share/pdf-layout-translate` |

- 可以用环境变量 `LT_HOME` 改到别处，任务文件夹用 `LT_JOBS`。也可以在技能目录放一个 `config.local.json`：`{"home": "D:/tools/plt", "jobs": "D:/tools/plt/jobs"}`。
- **在沙箱里运行的 agent**（禁网、只能写工作区，例如 Codex 的默认沙箱）：
  - 首次安装要么允许联网，要么先在自己的终端运行一次 `setup`。
  - 运行环境目录每次使用都要能写，需要把它加进可写目录，或把 `LT_HOME` 放进工作区。
- **Windows 安全软件**：360、腾讯电脑管家、火绒等的“防勒索”功能，可能会把新下载的 Python 改写 PDF 的操作当成可疑行为，静默结束进程。脚本已绕开最容易被拦的 venv 启动器。如果仍报 “the runtime process was stopped before it finished”，把报错里列出的运行环境目录和任务目录加入信任区后重跑即可。

## 使用

直接对 agent 说：

> 把 ~/Downloads/lecture3.pdf 翻译成中文，保留排版，放到 ~/Downloads/译文

agent 会按 [SKILL.md](skills/pdf-layout-translate/SKILL.md) 的流程操作：

| 命令 | 作用 |
|---|---|
| `prepare` | 收集要翻译的段落和截图文字 |
| `todo` / `merge` | 分批翻译并合并 |
| `build` | 生成成品 |

最后它会查看对照图并报告结果。也可以自己按这些命令手动操作，翻译规则见 [translation-guide.md](skills/pdf-layout-translate/references/translation-guide.md)。

## 限制

- 目标语言固定为简体中文。PPTX、DOCX 请先导出为 PDF；有密码的 PDF 请先去掉密码。
- 原文本身就是多行的列表项，译文换行后不悬挂对齐（BabelDOC 内核规则）。
- 译文统一常规字重；整段颜色通常保留，原文局部的粗体、斜体通常不保留。
- 图片里很小的字（约 10 像素以下）可能识别不全。认不全的行保持原样。
- 截图按 2 倍分辨率重画中文，带截图多的 PDF 会变大几倍。
- 两遍排版各约 1 分钟/16 页（CPU）。翻译速度取决于 agent。

## 测试

```bash
python tests/smoke_test.py
```

测试会生成一份带列表和截图的小 PDF，跑完整流程，再检查成品。

## 卸载

删除技能文件夹和运行环境文件夹即可，运行环境的位置可用 `lt.py check` 查看。

## 许可与致谢

本项目以 [AGPL-3.0](LICENSE) 发布，因为它调用并扩展了 BabelDOC（AGPL-3.0）。依赖的第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

感谢 [BabelDOC](https://github.com/funstory-ai/BabelDOC) / [PDFMathTranslate](https://github.com/Byaidu/PDFMathTranslate)、[RapidOCR](https://github.com/RapidAI/RapidOCR)、[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)、[PyMuPDF](https://github.com/pymupdf/PyMuPDF)。

---

## English

**pdf-layout-translate** is an Agent Skill that translates PDFs (slides, lecture notes, papers, homework) into **Simplified Chinese while keeping the original layout**. It also translates text inside screenshots, figures and scanned pages.

The agent you are already using does the translation, so no translation API key is needed and nothing is uploaded.

- **Layout**: [BabelDOC](https://github.com/funstory-ai/BabelDOC) re-typesets the text. Formulas, code and images stay as they are.
- **Text in images**: local OCR (RapidOCR + PP-OCRv5) reads it, and the Chinese is drawn back in place.
- **Output**: a Chinese PDF, a side-by-side bilingual PDF, and per-page review images.
- **Platforms**: Windows, macOS and Linux (glibc; Alpine/musl is not supported because onnxruntime has no musl build), with any agent that supports `SKILL.md` and can run shell commands (Claude Code, Codex CLI, Gemini CLI, Antigravity, OpenCode, …).

**Install**

- Run `python install.py`; it detects your agents' skill folders.
- Or copy `skills/pdf-layout-translate` into your agent's skills folder.
- Claude Code users can also run `/plugin marketplace add OWNER/pdf-layout-translate`.

**First run**

The first run installs a private runtime: Python 3.12, BabelDOC, models, fonts and uv's cache, about 1.5 GB, all in one folder. It needs internet once, and you can also start it yourself with `python skills/pdf-layout-translate/scripts/lt.py setup`. Use `HTTPS_PROXY` / `UV_DEFAULT_INDEX` behind proxies or with PyPI mirrors.

Set `LT_HOME` / `LT_JOBS` to move the runtime and job folders. Sandboxed agents need network and write access for that first setup.

On Windows, anti-ransomware features of some security suites (360, Tencent PC Manager, Huorong, …) may silently stop a freshly downloaded Python while it rewrites PDF files. The scripts avoid the venv redirector that triggers this most often. If a command still reports *"the runtime process was stopped before it finished"*, add the two folders it lists to the suite's trusted list and run the command again.

**Use**

Ask your agent, for example: *"Translate ~/Downloads/lecture3.pdf into Chinese and keep the layout."* The workflow is described in [SKILL.md](skills/pdf-layout-translate/SKILL.md).

**License**

AGPL-3.0, because the skill builds on BabelDOC. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
