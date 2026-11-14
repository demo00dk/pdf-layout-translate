# Third-party components

This repository contains only the skill's own scripts and instructions. Everything below is
**downloaded at install time** into the runtime folder on the user's machine (see README), not
redistributed here.

| Component | Used for | License |
|---|---|---|
| [BabelDOC](https://github.com/funstory-ai/BabelDOC) 0.7.1 | PDF parsing, layout analysis, re-typesetting | AGPL-3.0 |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | PDF reading / writing | AGPL-3.0 (or commercial) |
| DocLayout-YOLO model (fetched by BabelDOC) | page layout detection | AGPL-3.0 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | OCR runtime for text inside images | Apache-2.0 |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) PP-OCRv5 models | text detection / recognition | Apache-2.0 |
| Source Han Sans, Noto fonts (fetched by BabelDOC) | Chinese text in images, fallbacks | SIL OFL-1.1 |
| [STIX Two](https://github.com/stipub/stixfonts) fonts (non-Windows only, from google/fonts) | inline math in Keynote PDFs | SIL OFL-1.1 |
| [uv](https://github.com/astral-sh/uv) (only when not installed) | creating the Python 3.12 runtime | Apache-2.0 / MIT |
| OpenCV, NumPy, Pillow, fontTools, onnxruntime | image processing, fonts, inference | Apache-2.0 / BSD / HPND / MIT / MIT |

On Windows the inline-math fonts are renamed copies of the system's Cambria fonts, generated
locally on the user's machine and never distributed.

Because the skill's scripts import and extend BabelDOC (AGPL-3.0), the skill itself is released
under the GNU Affero General Public License v3.0. See [LICENSE](LICENSE).

## Examples and images

The documents in `examples/` and the pictures in `docs/images/` were written for this repository;
the slides, the screenshots inside them and the paper are made up. The PDFs contain embedded font
subsets, as any printed PDF does: Segoe UI, Consolas, Malgun Gothic, Times New Roman and Cambria Math
(Microsoft, embedded when the sources were printed to PDF) and, in the translated files, Source Han
Sans/Serif, Noto Sans/Serif and LXGW WenKai (SIL OFL-1.1).
