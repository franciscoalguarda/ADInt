"""Generate the project report from its editable Markdown source using PyMuPDF."""

import argparse
from html import escape
from pathlib import Path
import re

import pymupdf


DIRECTORY = Path(__file__).resolve().parent
SOURCE = DIRECTORY / "relatorio_parte1.md"
OUTPUT = DIRECTORY / "Relatorio_ADInt_Parte1_Grupo4.pdf"
PAGE = pymupdf.paper_rect("a4")
CONTENT = pymupdf.Rect(48, 73, PAGE.width - 48, PAGE.height - 63)
NAVY = (0.075, 0.16, 0.25)
BLUE = (0.09, 0.42, 0.57)
GRAY = (0.34, 0.41, 0.48)
LIGHT = (0.93, 0.95, 0.97)
GROUP = "Grupo 4"
AUTHORS = (
    "Francisco António Lisboa Guarda (117392)",
    "André Filipe Teigas Vaz (120181)",
)

# Exact heading numbers that should start a new page.
# For example, {"2.", "4."} forces breaks only before chapters 2 and 4.
# Use set() to keep only automatic breaks when a page fills up.
PAGE_BREAK_BEFORE = {
    "2.", "3.", "4.", "5.", "6.", "7.", "8.",
    "3.3.", "3.3.3.", "5.3.", "6.3.",
}

CSS = """
body { font-family: sans-serif; font-size: 10pt; line-height: 1.4; color: #263445; }
h1 { font-size: 22pt; line-height: 1.14; color: #17324d; margin: 0 0 16pt; }
h2 { font-size: 12.3pt; line-height: 1.25; color: #176b91; margin: 14pt 0 6pt;
     page-break-after: avoid; }
h3 { page-break-after: avoid; }
p { margin: 0 0 7pt; }
li { margin: 0 0 5pt; }
ul, ol { padding-left: 19pt; margin: 4pt 0 11pt; }
strong { color: #17324d; }
code { font-family: sans-serif; font-size: 10pt; font-weight: bold; color: #17324d; }
pre { font-family: monospace; font-size: 9pt; line-height: 1.4;
      padding: 10pt; margin: 6pt 0 12pt; }
pre code { font-family: monospace; font-size: 9pt; font-weight: bold; }
table { width: 100%; border-collapse: collapse; margin: 5pt 0 13pt;
        font-size: 8.7pt; line-height: 1.3; page-break-inside: avoid; }
th { color: #17324d; font-weight: bold; text-align: left; }
td, th { border: 0.5pt solid #cdd9e4; padding: 5pt; vertical-align: top; }
td code, th code { font-size: 8.7pt; }
tr { page-break-inside: avoid; }
img { width: 100%; margin: 5pt 0 8pt; }
.caption { font-size: 8pt; color: #576b7c; margin: 0 0 12pt; }
a { color: #176b91; }
"""


def inline(text):
    pieces = re.split(r"(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))", text)
    result = []
    for piece in pieces:
        if piece.startswith("`") and piece.endswith("`"):
            result.append("<code>" + escape(piece[1:-1]) + "</code>")
        elif piece.startswith("**") and piece.endswith("**"):
            result.append("<strong>" + inline(piece[2:-2]) + "</strong>")
        elif match := re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", piece):
            result.append(f'<a href="{escape(match[2], quote=True)}">{escape(match[1])}</a>')
        else:
            result.append(escape(piece))
    return "".join(result)


def render_markdown(source):
    """Render the report's headings, tables, lists, images and fenced code blocks."""
    lines = source.splitlines()
    output = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.startswith("```"):
            index += 1
            code = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            output.append("<pre><code>" + escape("\n".join(code)) + "</code></pre>")
            index += 1
            continue
        if line.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                    rows.append(cells)
                index += 1
            output.append("<table><thead>")
            for number, row in enumerate(rows):
                if number == 1:
                    output.append("</thead><tbody>")
                cell_type = "th" if number == 0 else "td"
                widths = None
                if rows[0][0] == "Endpoint":
                    widths = (44, 15, 41)
                elif rows[0][0] in ("Modelo", "Modelo ou associação"):
                    widths = (25, 26, 49)
                elif rows[0][0] == "Ficheiro de testes":
                    widths = (41, 12, 47)
                output.append("<tr>")
                for column, cell in enumerate(row):
                    style = f' style="width: {widths[column]}%"' if widths else ""
                    output.append(f"<{cell_type}{style}>{inline(cell)}</{cell_type}>")
                output.append("</tr>")
            output.append("</tbody></table>")
            continue
        if image := re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line):
            output.append(f'<p><img src="{escape(image[2], quote=True)}"></p>')
            output.append(f'<p class="caption">{escape(image[1])}</p>')
            index += 1
            continue
        if heading := re.match(r"(#{2,4})\s+(.+)", line):
            level = len(heading[1]) - 1
            number = heading[2].split(" ", 1)[0]
            page_break = number in PAGE_BREAK_BEFORE
            style = ' style="page-break-before: always"' if page_break else ""
            anchor = f' id="section-{escape(number, quote=True)}"' if level == 1 else ""
            output.append(f"<h{level}{anchor}{style}>{inline(heading[2])}</h{level}>")
            index += 1
            continue
        if re.match(r"(?:- |\d+\. )", line):
            ordered = bool(re.match(r"\d+\. ", line))
            tag = "ol" if ordered else "ul"
            output.append(f"<{tag}>")
            pattern = r"\d+\. " if ordered else r"- "
            while index < len(lines) and re.match(pattern, lines[index].strip()):
                output.append("<li>" + inline(re.sub(pattern, "", lines[index].strip(), count=1)) + "</li>")
                index += 1
            output.append(f"</{tag}>")
            continue
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not re.match(
            r"(?:#{2,4}\s|```|\||!\[|- |\d+\. )", lines[index].strip()
        ):
            paragraph.append(lines[index].strip())
            index += 1
        output.append("<p>" + inline(" ".join(paragraph)) + "</p>")
    return "<html><body>" + "\n".join(output) + "</body></html>"


def section_pdf(source, positionfn=None):
    story = pymupdf.Story(
        html=render_markdown(source), user_css=CSS, archive=pymupdf.Archive(str(DIRECTORY))
    )
    return story.write_with_links(
        lambda number, filled: (PAGE, CONTENT, None), positionfn=positionfn
    )


def cover(document):
    page = document.new_page(width=PAGE.width, height=PAGE.height)
    page.draw_rect(pymupdf.Rect(0, 0, PAGE.width, 400), color=NAVY, fill=NAVY)
    remainder = page.insert_textbox(
        pymupdf.Rect(48, 46, PAGE.width - 48, 133),
        "Aplicações Distribuídas\nsobre a Internet",
        fontsize=26, lineheight=1.25, fontname="hebo", color=(1, 1, 1),
    )
    assert remainder >= 0, "Course title does not fit"
    page.draw_rect(pymupdf.Rect(48, 150, 112, 155), color=BLUE, fill=BLUE)
    remainder = page.insert_textbox(
        pymupdf.Rect(48, 180, PAGE.width - 48, 325),
        "Sistema de Gestão\ne Correção de\nExames Nacionais",
        fontsize=30, lineheight=1.28, fontname="hebo", color=(1, 1, 1),
    )
    assert remainder >= 0, "Cover title does not fit"
    page.insert_text((48, 365), "Relatório do Projeto · Parte 1", fontsize=15, color=(0.78, 0.87, 0.93))

    page.draw_rect(pymupdf.Rect(48, 438, PAGE.width - 48, 568), color=(0.8, 0.86, 0.9), fill=LIGHT, width=0.6)
    page.draw_rect(pymupdf.Rect(48, 438, 52, 568), color=BLUE, fill=BLUE)
    page.insert_text((66, 466), "MEEC", fontsize=16, fontname="hebo", color=NAVY)
    page.insert_text((66, 486), GROUP, fontsize=16, fontname="hebo", color=NAVY)
    remainder = page.insert_textbox(
        pymupdf.Rect(66, 506, PAGE.width - 64, 590),
        "\n".join(AUTHORS), fontsize=14, lineheight=1.8, color=NAVY,
    )
    assert remainder >= 0, "Author names do not fit"

    remainder = page.insert_textbox(
        pymupdf.Rect(48, 603, PAGE.width - 48, 735),
        "Descrição consolidada das funcionalidades implementadas, arquitetura, "
        "endpoints, modelos de dados, regras de validação e execução.",
        fontsize=11, lineheight=1.5, color=GRAY,
    )
    assert remainder >= 0, "Cover description does not fit"
    page.insert_text((48, 784), "Documentação do projeto", fontsize=9, color=GRAY)


def table_of_contents(document, entries):
    page = document[1]
    page.insert_text((48, 105), "Índice", fontsize=28, fontname="hebo", color=NAVY)
    page.insert_textbox(
        pymupdf.Rect(48, 132, PAGE.width - 48, 195),
        "O relatório descreve o estado implementado da Parte 1. As secções dos "
        "serviços apresentam as funcionalidades, as regras, os endpoints "
        "e os modelos SQLAlchemy.",
        fontsize=10.5, lineheight=1.45, color=GRAY,
    )
    top = 225
    for title, first_page in entries:
        label = title
        page.insert_text((48, top), label, fontsize=11, color=NAVY)
        number = str(first_page)
        number_width = pymupdf.get_text_length(number, fontname="helv", fontsize=11)
        page.insert_text((547 - number_width, top), number, fontsize=11, color=BLUE)
        page.draw_line((48, top + 12), (547, top + 12), color=(0.82, 0.87, 0.91), width=0.5)
        page.insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(46, top - 16, 549, top + 15), "page": first_page - 1})
        top += 45

def main(output=OUTPUT):
    source = SOURCE.read_text(encoding="utf-8")
    section_starts = list(re.finditer(r"^## (\d+\. .+)$", source, re.M))
    assert section_starts, "Report contains no sections"
    document = pymupdf.open()
    cover(document)
    document.new_page(width=PAGE.width, height=PAGE.height)
    section_positions = {}
    page_offset = len(document)

    def record_section(position):
        if position.open_close & 1 and position.id and position.id.startswith("section-"):
            section_positions.setdefault(
                position.id, (page_offset + position.page_num, position.rect[1])
            )

    # Render the whole body together so chapters can share a page.
    with section_pdf(source[section_starts[0].start():], record_section) as body:
        document.insert_pdf(body)

    entries = []
    for match in section_starts:
        number = match[1].split(" ", 1)[0]
        first_page, _ = section_positions[f"section-{number}"]
        entries.append((match[1], first_page))
        print(f"Section {number}: starting on page {first_page}")
    table_of_contents(document, entries)

    for index in range(1, len(document)):
        page = document[index]
        section = "Índice"
        for title, first in entries:
            number = title.split(" ", 1)[0]
            top = section_positions[f"section-{number}"][1]
            if first < index + 1 or (first == index + 1 and top <= CONTENT.y0 + 30):
                section = title
        page.insert_text((48, 37), "ADInt · Projeto Parte 1", fontsize=8, color=GRAY)
        page.insert_textbox(pymupdf.Rect(210, 27, 547, 44), section, fontsize=8, align=2, color=GRAY)
        page.draw_line((48, 50), (547, 50), color=(0.8, 0.86, 0.9), width=0.6)
        page.draw_line((48, PAGE.height - 44), (547, PAGE.height - 44), color=(0.8, 0.86, 0.9), width=0.5)
        page.insert_textbox(pymupdf.Rect(480, PAGE.height - 38, 547, PAGE.height - 22), f"{index + 1} / {len(document)}", fontsize=8, align=2, color=GRAY)
    document.set_toc([[1, "Índice", 2]] + [[1, title, first] for title, first in entries])
    document.set_metadata({
        "title": "ADInt — Sistema de Gestão e Correção de Exames Nacionais — Parte 1",
        "author": GROUP + " — " + "; ".join(AUTHORS),
        "subject": "Funcionalidades implementadas, endpoints, modelos SQLAlchemy e validação",
        "keywords": "ADInt, Parte 1, Flask, SQLite, XML-RPC, exames, relatório",
        "creator": "docs/gerar_relatorio.py com PyMuPDF",
    })
    output = output.resolve()
    document.save(output, garbage=4, deflate=True)
    print(f"PDF generated: {output} ({len(document)} pages)")
    document.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT, help="Destination PDF path.")
    main(parser.parse_args().output)
