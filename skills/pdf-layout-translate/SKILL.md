---
name: pdf-layout-translate
description: 把 PDF 课件、讲义、论文、作业等翻译成简体中文并保留原版式（类似 WPS 文档翻译），输出纯中文版和中外对照版，截图、插图和扫描页里的文字也一并翻译。翻译由当前 agent 完成，不需要翻译 API key，文件不上传。Translate a PDF into Simplified Chinese while keeping its layout (slides, papers, homework; text inside screenshots too), with no translation API. 只想提取文字或转 Markdown 时不要用。
license: AGPL-3.0
metadata:
  version: "1.1.0"
---

# PDF 保版式翻译

排版由 BabelDOC 完成，翻译由你自己完成，分两遍：第一遍收集要翻译的段落，你翻译后，第二遍回填并排版。图片里的文字用本机 OCR（RapidOCR）识别，同样由你翻译，再画回图片。

## 运行方式

入口是本技能目录下的 `scripts/lt.py`。下文的 `${CLAUDE_SKILL_DIR}` 指本 `SKILL.md` 所在目录：Claude Code 会自动替换，其他 agent 请换成实际路径。

```bash
python "${CLAUDE_SKILL_DIR}/scripts/lt.py" check
```

- 用任意 Python 3.9 及以上运行。命令名按系统不同可能是 `python3`、`python` 或 `py -3`。
- **先单独运行一次 `lt.py check`**。显示未安装时运行 `lt.py setup`，它会安装 Python 3.12 私有环境、BabelDOC、版面模型、字体和 OCR 模型：需要联网，约 1.5 GB，耗时 5–15 分钟，之后离线可用。这一步要放到后台运行或给足超时（至少 20 分钟），被中断后重跑即可续装。环境损坏时用 `lt.py setup --force` 重装。
- `prepare` 和 `build` 每次约 1 分钟/16 页，大文件要给足超时（例如 10 分钟以上）。同一个任务的命令不要并行重试：任务被占用时脚本会直接提示。
- 运行环境默认装在用户数据目录：Windows 是 `%LOCALAPPDATA%\pdf-layout-translate`，macOS 是 `~/Library/Application Support/pdf-layout-translate`，Linux 是 `~/.local/share/pdf-layout-translate`。可用环境变量 `LT_HOME` 改位置，任务文件夹用 `LT_JOBS`；也可以在技能目录放 `config.local.json`，写 `{"home": "...", "jobs": "..."}`。
- **沙箱和工作区限制**：
  - 每次运行都会写运行环境目录（缓存、新语言的 OCR 模型）。agent 在沙箱里运行时（例如禁网、只能写工作区），需要放开这个目录的写权限；首次安装还要联网。做不到时，请用户在自己的终端运行一次 `lt.py setup`，或把 `LT_HOME` 设到可写目录。
  - 你的文件工具只能访问工作区时，用 `--job ./pdf-translate/<名称>` 把任务文件夹放进工作区，这样才能写批量译文、查看对照图。
- 终端显示中文、韩文乱码时（例如 Windows PowerShell 的旧代码页），用 `todo --out <文件>` 把待译内容写成 UTF-8 文件再读。成品路径也会写进 `<job>/result.json`。
- 命令报错 “the runtime process was stopped before it finished”：进程被外部结束了，Windows 上多半是 360、腾讯电脑管家、火绒等安全软件把改写 PDF 的 python.exe 当成勒索行为拦截。把报错里列出的两个目录告诉用户，请用户加入安全软件的信任区（你不要自己改安全设置），然后原样重跑。任务锁会自动释放，不用手动删除。

翻译前读一遍 [references/translation-guide.md](references/translation-guide.md)，其中有占位符、哪些内容保留原文、截图文字的处理规则。

## 流程

```bash
python "${CLAUDE_SKILL_DIR}/scripts/lt.py" prepare "/path/课件.pdf"
python "${CLAUDE_SKILL_DIR}/scripts/lt.py" todo "<job>" --limit 60
python "${CLAUDE_SKILL_DIR}/scripts/lt.py" merge "<job>" "<job>/batch1.json"
python "${CLAUDE_SKILL_DIR}/scripts/lt.py" build "<job>" --out "/path/译文"
```

1. **prepare**：把 PDF 复制进任务文件夹（job），判断生成软件和源语言，运行第一遍，最后打印 job 路径和待译数量。原文件不会被修改。
   - `--job <文件夹>`：指定任务文件夹。默认是 `<LT_JOBS>/<文件名>-<哈希>`；同一个文件再次 `prepare` 会使用同一个 job，已有译文按原文自动沿用。
   - `--pages 1-5,8`：只翻译这些页，其余页在成品里保留原文。
   - `--lang-in ko|en|ja|fr|de|ru|...`：自动判断失败时指定源语言。扫描件没有文字层，必须指定。繁体中文原文用 `--lang-in zh`。
   - `--profile`：版面类型判断错误时手动指定，见下文“版式判断”。
   - `--carry-from <job1>,<job2>`：沿用以前任务的译文，节省工作量，也让术语一致。
   - `--no-images`：不处理图片里的文字。
2. **todo / merge**：`todo` 每行输出一个 JSON，`k` 是编号，`src` 是原文，`page` 是页码；以 `i` 开头的编号是图片里的文字，附带图片路径。
   - 把译文写成 UTF-8 的 JSON 对象 `{"12": "译文", "i7": "译文"}`，用 `merge` 合并（文件名写 `-` 时从标准输入读）。重复这一步，直到待译数量为 0。
   - `--kind text|img` 只看一类，`--offset` 翻页。
   - `merge` 报告占位符不一致时，修正后重新合并。合并过的编号可以再次合并来修改译文。
3. **build**：运行第二遍，把图片里的文字画回去，输出 `<原文件名>.中文.pdf` 和 `<原文件名>.中外对照.pdf`，并在 `<job>/review/pNNN.png` 生成逐页左右对照图。
   - 用户指定了保存位置时加 `--out <文件夹>`。
   - 没有指定时，成品留在 `<job>/output`，并告诉用户路径。
   - 不要在用户的原资料文件夹里写文件，除非用户要求。
   - `--compare 3,5-7` 只生成这几页的对照图，`--compare none` 不生成。
   - 成品被 PDF 阅读器打开着时（Windows 会锁文件），会另存为 `… (2).pdf` 并提示。
4. **检查**：查看对照图，优先看带截图、表格、密集列表和公式的页。发现译文问题就修改后再 `merge`、`build`。向用户报告成品路径，以及实际遇到的限制。

`status <job>` 显示任务的设置、待译数量和成品。两遍各需约 1 分钟/16 页（CPU）。

## 版式判断

`prepare` 按 PDF 元数据自动选择，结果不对时用 `--profile` 指定：

| profile | 适用 | 处理 |
|---|---|---|
| keynote | Keynote 导出 | 预处理行内公式和裁剪掉的隐藏文字，用 PDF 自带文本框定版面 |
| slides | Google Slides、PowerPoint、Impress 导出，横版页面 | 版面模型 + 列表、表格单元格细分；保留文字颜色 |
| latex | pdfTeX、LuaTeX、XeTeX 等 | 版面模型 + 细分，字体按原文 |
| generic | 其他 | 同 latex |

## 排版行为与限制

- **分段**：原文手动换行的地方（下一行开头的词本来放得下）和缩进起头的行会另起一段。幻灯片里以句号、冒号结尾的行也会另起一段。同一段内的换行由 BabelDOC 重排，译文里无法强制换行。
- **字号**：译文比原文长时，该段会自动缩小；相邻段落各自缩放，字号可能不一致。
- **样式**：译文统一用常规字重。整段的颜色通常会保留，原文局部的粗体、斜体通常不保留。保持原样的段落（代码、公式行等）保留原来的样式。
- **列表**：原文本身就是多行的列表项，译文换行后不悬挂对齐（BabelDOC 内核的首行缩进规则）。
- **图片里的文字**：很小的字（约 10 像素以下）可能认错或漏认，认不全的行保持原样。中文统一用思源黑体，不还原粗体。截图按 2 倍分辨率重画，文件会变大几倍。
- **扫描件**：整页图片走图片识别；扫描件自带的不可见 OCR 文字层会被忽略。密集正文的扫描件效果一般。
- **范围**：目标语言固定为简体中文。PPTX、DOCX 需要先导出为 PDF。有密码的 PDF 需要先去掉密码。
