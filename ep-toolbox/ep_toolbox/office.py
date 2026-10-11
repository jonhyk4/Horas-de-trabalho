"""Preenchimento dos modelos DOCX da empresa (ROA, carta ANAC, Transmittal Letter).

Os modelos são pacotes .docx já existentes: a aplicação substitui apenas o texto
dos marcadores e preserva o layout, as imagens e os cabeçalhos.
"""
from __future__ import annotations

import copy
import re
import zipfile
from pathlib import Path

from docx import Document
from lxml import etree
from docx.oxml.ns import qn

from .common import MANUAL_TITLES, PrepError, english_date, fleet_label, parse_date
from .history import clean_history
from .paths import models_dir

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
HIGHLIGHT_FIELDS = ("chapter", "description")
MAX_HIGHLIGHT_ROWS = 500
MAX_CHAPTER = 1000
MAX_DESCRIPTION = 10000
MAX_HIGHLIGHT_TOTAL = 150000


def model_path(name: str) -> Path:
    path = models_dir() / name
    return path


def require_model(name: str, message: str) -> Path:
    path = model_path(name)
    if not path.is_file():
        raise PrepError(f"{message} Procurado em: {path}")
    return path


def import_roa(path) -> list[dict]:
    """Lê as linhas de revisão de um ROA em Word (tabela com revisão, data, aprovação)."""
    document = Document(str(path))
    rows = []
    for table in document.tables:
        for row in table.rows:
            values = [cell.text.strip() for cell in row.cells]
            if len(values) < 4 or not re.fullmatch(r"\d+(?:[./-]\d+)*[A-Z]?", values[0]):
                continue
            if not parse_date(values[1]):
                raise PrepError("O ROA contém uma revisão sem data.")
            rows.append(dict(zip(["revision", "date", "approval_date", "approved"], values[:4])))
    if not rows:
        raise PrepError("Não foram encontradas linhas de revisão no Word. Usa uma tabela com "
                        "revisão, data, data de aprovação e aprovação.")
    return clean_history(rows)


def _text_nodes(paragraph):
    return [t for t in paragraph.xpath(".//w:t", namespaces=NS)
            if t.xpath("ancestor::w:p[1]", namespaces=NS)[0] is paragraph]


def _replace_span(nodes, start, end, value):
    positions, offset = [], 0
    for element in nodes:
        text = element.text or ""
        positions.append((element, offset, offset + len(text)))
        offset += len(text)
    covered = [x for x in positions if x[2] > start and x[1] < end]
    if not covered:
        return
    first, first_start, _ = covered[0]
    last, last_start, _ = covered[-1]
    before = (first.text or "")[:start - first_start]
    after = (last.text or "")[end - last_start:]
    if first is last:
        first.text = before + value + after
    else:
        first.text = before + value
        for element, _, _ in covered[1:-1]:
            element.text = ""
        last.text = after
    for element, _, _ in covered:
        element.set(qn("xml:space"), "preserve")


def _replace(paragraph, pattern, value) -> int:
    nodes = _text_nodes(paragraph)
    text = "".join(t.text or "" for t in nodes)
    matches = list(re.finditer(pattern, text))
    for match in reversed(matches):
        _replace_span(nodes, match.start(), match.end(), value)
    return len(matches)


def _line_breaks(tree):
    for node in tree.xpath("//w:t", namespaces=NS):
        if "\n" not in (node.text or ""):
            continue
        chunks = node.text.split("\n")
        node.text = chunks[0]
        parent = node.getparent()
        index = parent.index(node) + 1
        for text in chunks[1:]:
            parent.insert(index, etree.Element(qn("w:br")))
            index += 1
            element = etree.Element(qn("w:t"))
            element.text = text
            element.set(qn("xml:space"), "preserve")
            parent.insert(index, element)
            index += 1


def _update_page_total(tree):
    """Converte o total literal de 'P x/2' em campo NUMPAGES."""
    for paragraph in tree.xpath("//w:p", namespaces=NS):
        nodes = _text_nodes(paragraph)
        for index, node in enumerate(nodes):
            if node.text != "2" or not index or not (nodes[index - 1].text or "").endswith("/"):
                continue
            run = node.getparent()
            if run.tag != qn("w:r"):
                continue
            run.remove(node)
            begin = etree.SubElement(run, qn("w:fldChar"))
            begin.set(qn("w:fldCharType"), "begin")
            instruction = etree.SubElement(run, qn("w:instrText"))
            instruction.set(qn("xml:space"), "preserve")
            instruction.text = " NUMPAGES "
            separate = etree.SubElement(run, qn("w:fldChar"))
            separate.set(qn("w:fldCharType"), "separate")
            etree.SubElement(run, qn("w:t")).text = "2"
            end = etree.SubElement(run, qn("w:fldChar"))
            end.set(qn("w:fldCharType"), "end")


def _values(profile, issue_date, revision="", highlights=""):
    return {
        "#MANUALABV": profile["manual"],
        "#MANUAL": MANUAL_TITLES[profile["manual"]],
        "#FLEET": fleet_label(profile["fleet"]),
        "#REVDATE": english_date(issue_date),
        "#REVISION": str(revision),
        "#HIGHLIGHTS": highlights,
    }


def fill_roa(profile, rows, issue_date, output, blank_page=True):
    """Gera o ROA a partir do modelo, uma linha por revisão."""
    template = require_model("roa_template.docx", "O modelo do ROA não foi encontrado.")
    if not rows:
        raise PrepError("O ROA não tem histórico. Importa o último ROA concluído.")
    values = _values(profile, issue_date)
    with zipfile.ZipFile(template) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as dest:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "word/document.xml":
                tree = etree.fromstring(data)
                body = tree.find("w:body", NS)
                table = body.find("w:tbl", NS)
                table_rows = table.findall("w:tr", NS)
                if len(table_rows) != 3:
                    raise PrepError("O template ROA deve conter dois cabeçalhos e uma linha de exemplo.")
                sample = copy.deepcopy(table_rows[2])
                table.remove(table_rows[2])
                for head in table_rows[:2]:
                    etree.SubElement(head.find("w:trPr", NS), qn("w:tblHeader"))
                for record in rows:
                    row = copy.deepcopy(sample)
                    for element in row.iter():
                        for attr in list(element.attrib):
                            if attr.endswith("}paraId") or attr.endswith("}textId"):
                                del element.attrib[attr]
                    row_props = row.find("w:trPr", NS)
                    if row_props is None:
                        row_props = etree.Element(qn("w:trPr"))
                        row.insert(0, row_props)
                    etree.SubElement(row_props, qn("w:cantSplit"))
                    for cell, key in zip(row.findall("w:tc", NS), ["revision", "date", "approval_date", "approved"]):
                        nodes = _text_nodes(cell.find("w:p", NS))
                        value = str(record.get(key, ""))
                        if key == "date":
                            value = english_date(value)
                        _replace_span(nodes, 0, sum(len(n.text or "") for n in nodes), value)
                    table.append(row)
                if not blank_page:
                    remove = False
                    for child in list(body):
                        if child.xpath(".//w:br[@w:type='page']", namespaces=NS):
                            remove = True
                        if remove and child.tag != qn("w:sectPr"):
                            body.remove(child)
            elif item.filename.startswith(("word/header", "word/footer", "word/settings")):
                if not item.filename.endswith(".xml"):
                    dest.writestr(item, data)
                    continue
                tree = etree.fromstring(data)
            else:
                dest.writestr(item, data)
                continue
            for paragraph in tree.xpath("//w:p", namespaces=NS):
                _replace(paragraph, r"#REVDATE\s*\(DD MMM YY\)", values["#REVDATE"])
                for token, value in values.items():
                    _replace(paragraph, re.escape(token) + r"(?![A-Z])", value)
            if item.filename.startswith("word/footer"):
                _update_page_total(tree)
            if item.filename == "word/settings.xml":
                update = tree.find("w:updateFields", NS)
                if update is None:
                    update = etree.SubElement(tree, qn("w:updateFields"))
                update.set(qn("w:val"), "true")
            _line_breaks(tree)
            dest.writestr(item, etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True))


def normalize_highlights(value, allow_empty=False) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_HIGHLIGHT_ROWS:
        raise PrepError(f"Os highlights devem ser uma tabela com até {MAX_HIGHLIGHT_ROWS} linhas.")
    rows, total = [], 0
    for number, record in enumerate(value, 1):
        if not isinstance(record, dict):
            raise PrepError(f"A linha {number} dos highlights é inválida.")
        if not all(isinstance(record.get(k, ""), str) for k in HIGHLIGHT_FIELDS):
            raise PrepError(f"A linha {number} deve conter texto nas duas colunas.")
        chapter = record.get("chapter", "").strip()
        description = record.get("description", "").strip()
        if not chapter and not description:
            continue
        if not chapter or not description:
            raise PrepError(f"Preenche Revised Chapter e Description na linha {number} dos highlights.")
        if len(chapter) > MAX_CHAPTER or len(description) > MAX_DESCRIPTION:
            raise PrepError(f"A linha {number} excede o limite de texto: "
                            f"{MAX_CHAPTER} caracteres no capítulo e {MAX_DESCRIPTION} na descrição.")
        total += len(chapter) + len(description)
        rows.append({"chapter": chapter, "description": description})
    if not rows and not allow_empty:
        raise PrepError("Preenche pelo menos uma linha de highlights para gerar o ficheiro TAP.")
    if total > MAX_HIGHLIGHT_TOTAL:
        raise PrepError(f"Os highlights excedem {MAX_HIGHLIGHT_TOTAL} caracteres no total.")
    return rows


def fill_transmittal(profile, revision, issue_date, highlights, output, previous_date=None,
                     blank_page=False, page_size=None):
    """Gera a Transmittal Letter com uma linha por highlight na tabela do modelo."""
    template = require_model("transmittal_template.docx", "O modelo da Transmittal Letter não foi encontrado.")
    rows = normalize_highlights(highlights)
    if previous_date is None:
        history = profile.get("history", [])
        previous_date = history[-1].get("date") if history else None
    values = _values(profile, issue_date, revision)
    values["#PREVDATE"] = english_date(previous_date)

    layout_scale = 1.0
    if page_size:
        with zipfile.ZipFile(template) as layout_source:
            section = etree.fromstring(layout_source.read("word/document.xml")).find(".//w:sectPr", NS)
            size = section.find("w:pgSz", NS)
            margins = section.find("w:pgMar", NS)
            old_width = int(size.get(qn("w:w")))
            sides = sum(int(margins.get(qn("w:" + side), "0")) for side in ("left", "right"))
            new_width = round(float(page_size[0]) * 20)
            layout_scale = (new_width - sides) / (old_width - sides)

    count = 0
    with zipfile.ZipFile(template) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as dest:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                tree = etree.fromstring(data)
                for paragraph in tree.xpath("//w:p", namespaces=NS):
                    if not previous_date:
                        _replace(paragraph, r"\s+and replacing\s+last issue dated\s+#PREVDATE", "")
                    for token, value in values.items():
                        _replace(paragraph, re.escape(token) + r"(?![A-Z])", value)
                if item.filename == "word/document.xml":
                    for table in tree.xpath("//w:tbl", namespaces=NS):
                        sample_rows = table.findall("w:tr", NS)
                        if len(sample_rows) < 2:
                            continue
                        labels = ["".join(cell.xpath(".//w:t/text()", namespaces=NS))
                                  for cell in sample_rows[0].findall("w:tc", NS)]
                        if labels != ["Revised Chapter", "Description"]:
                            continue
                        sample = copy.deepcopy(sample_rows[1])
                        for row in sample_rows[1:]:
                            table.remove(row)
                        for record in rows:
                            row = copy.deepcopy(sample)
                            # Permite que descrições longas continuem na página seguinte.
                            for node in row.xpath(".//w:cantSplit | .//w:trHeight", namespaces=NS):
                                node.getparent().remove(node)
                            for props in row.xpath(".//w:pPr", namespaces=NS):
                                for tag in ("keepNext", "keepLines", "pageBreakBefore"):
                                    for node in props.findall("w:" + tag, NS):
                                        props.remove(node)
                                    etree.SubElement(props, qn("w:" + tag)).set(qn("w:val"), "0")
                            for element in row.iter():
                                for attr in list(element.attrib):
                                    if attr.endswith("}paraId") or attr.endswith("}textId"):
                                        del element.attrib[attr]
                            for cell, token, key in zip(row.findall("w:tc", NS),
                                                        ["#REVISED_CHAPTER", "#DESCRIPTION"], HIGHLIGHT_FIELDS):
                                for paragraph in cell.xpath(".//w:p", namespaces=NS):
                                    _replace(paragraph, re.escape(token), record[key])
                            table.append(row)
                        count += 1
                    if not blank_page:
                        body = tree.find("w:body", NS)
                        for paragraph in list(body):
                            if (paragraph.tag == qn("w:p")
                                    and "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
                                    == "Intentionally left blank"):
                                body.remove(paragraph)
                if page_size:
                    for size in tree.xpath("//w:sectPr/w:pgSz", namespaces=NS):
                        size.set(qn("w:w"), str(round(float(page_size[0]) * 20)))
                        size.set(qn("w:h"), str(round(float(page_size[1]) * 20)))
                        size.attrib.pop(qn("w:orient"), None)
                    for node in tree.xpath("//w:tblW | //w:tcW | //w:gridCol | //w:tab", namespaces=NS):
                        attr = "pos" if node.tag == qn("w:tab") else "w"
                        if node.tag in (qn("w:tblW"), qn("w:tcW")) and node.get(qn("w:type")) != "dxa":
                            continue
                        value = node.get(qn("w:" + attr))
                        if value and value.lstrip("-").isdigit():
                            node.set(qn("w:" + attr), str(round(int(value) * layout_scale)))
                _line_breaks(tree)
                data = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
            dest.writestr(item, data)
    if count != 1:
        Path(output).unlink(missing_ok=True)
        raise PrepError("O template da Transmittal Letter deve conter uma tabela Revised Chapter / Description.")


def transmittal_ready() -> bool:
    return model_path("transmittal_template.docx").is_file()


def fill_letter(profile, revision, issue_date, incremental, output, forms, attachment_name=None):
    """Gera a carta ANAC a partir do modelo carta_base.docx."""
    template = require_model("carta_base.docx", "O modelo da carta não foi encontrado.")
    if not forms:
        forms = []
    counts = 0
    with zipfile.ZipFile(template) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as dest:
        section = etree.fromstring(source.read("word/document.xml")).find(".//w:sectPr", NS)
        footer_width = (int(section.find("w:pgSz", NS).get(qn("w:w")))
                        - sum(int(section.find("w:pgMar", NS).get(qn("w:" + side), "0")) for side in ("left", "right")))
        for name in source.namelist():
            data = source.read(name)
            if name.startswith("word/") and name.endswith(".xml"):
                tree = etree.fromstring(data)
                for paragraph in tree.xpath("//w:p", namespaces=NS):
                    text = "".join(t.text or "" for t in _text_nodes(paragraph))
                    if "REV 50" in text:
                        counts += _replace(paragraph, r"REV\s+50" if revision else r"REV\s+50\s*[–-]\s*",
                                           "REV " + str(revision) if revision else "")
                        _replace(paragraph, r"26\s+MAY\s+26", english_date(issue_date))
                        _replace(paragraph, r"\bA330\b", fleet_label(profile["fleet"]))
                        _replace(paragraph, r"\bFCTM\b", profile["manual"])
                        _replace(paragraph, r"OM\(B\)", profile["part"])
                    # Número de correspondência e datas de expedição são preenchidos depois.
                    if re.fullmatch(r"\s*000539\s+JUL\s+16'26\s*", text):
                        _replace(paragraph, r"000539\s+JUL\s+16'26", "")
                    if not incremental and "Manual INC" in text:
                        _replace(paragraph, r"Manual\s+INC", "Manual completo")
                    elif attachment_name and "Manual INC" in text:
                        _replace(paragraph, r"Manual\s+INC", attachment_name)
                    if "FORM ANAC (9.2.6.4.53)" in text:
                        suffix = (" + FORM ANAC (" + forms[0]["code"] + ")") if len(forms) == 1 else (
                            " + FORMULÁRIOS ANAC (ver carta)" if forms else "")
                        _replace(paragraph, r"\s*\+\s*FORM ANAC\s*\(9\.2\.6\.4\.53\)", suffix)
                    if name.startswith("word/footer") and re.search(r"P1/1\s*$", text):
                        _rewrite_footer_page_number(paragraph, footer_width)
                    if text.strip() == "O seguinte formulário ANAC:":
                        if forms:
                            _replace(paragraph, r"O seguinte formulário ANAC:",
                                     "O seguinte formulário ANAC:" if len(forms) == 1 else "Os seguintes formulários ANAC:")
                        else:
                            paragraph.getparent().remove(paragraph)
                    if re.match(r"^9\.2\.6\.4\.53\s*[–-]", text.strip()):
                        parent = paragraph.getparent()
                        position = parent.index(paragraph)
                        for index, form in enumerate(forms):
                            item = copy.deepcopy(paragraph)
                            nodes = _text_nodes(item)
                            _replace_span(nodes, 0, sum(len(n.text or "") for n in nodes),
                                          form["code"] + " – " + form["title"])
                            parent.insert(position + index, item)
                        parent.remove(paragraph)
                # Evita guardar resultados antigos de campos de formulário.
                for node in tree.xpath("//w:default", namespaces=NS):
                    value = node.get(qn("w:val"), "")
                    if "000539" in value or "JUL 16'26" in value:
                        node.set(qn("w:val"), "")
                if name == "word/document.xml":
                    body = tree.find("w:body", NS)
                    for element in reversed(list(body)):
                        if element.tag == qn("w:sectPr"):
                            continue
                        if element.tag != qn("w:p") or element.xpath(
                                ".//w:t[string-length(text())>0] | .//w:drawing | .//w:pict | .//w:sectPr | .//w:br",
                                namespaces=NS):
                            break
                        body.remove(element)
                data = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
            dest.writestr(name, data)
    if counts != 2:
        Path(output).unlink(missing_ok=True)
        raise PrepError("O modelo da carta foi alterado: não foi possível identificar as duas referências da revisão.")


def _rewrite_footer_page_number(paragraph, footer_width):
    """Substitui 'P1/1' do rodapé por uma tabela com PAGE/NUMPAGES alinhada à direita."""
    nodes = _text_nodes(paragraph)
    style = copy.deepcopy(nodes[-1].getparent().find("w:rPr", NS)) if nodes else None
    _replace(paragraph, r"\s*P1/1\s*$", "")
    for tab in paragraph.xpath(".//w:tab", namespaces=NS):
        tab.getparent().remove(tab)
    props = paragraph.find("w:pPr", NS)
    if props is None:
        props = etree.Element(qn("w:pPr"))
        paragraph.insert(0, props)
    for tag in ("ind", "jc", "tabs", "spacing"):
        for node in props.findall("w:" + tag, NS):
            props.remove(node)
    etree.SubElement(props, qn("w:jc")).set(qn("w:val"), "left")
    etree.SubElement(props, qn("w:spacing"), {qn("w:before"): "0", qn("w:after"): "0"})

    table = etree.Element(qn("w:tbl"))
    table_props = etree.SubElement(table, qn("w:tblPr"))
    etree.SubElement(table_props, qn("w:tblW"), {qn("w:w"): str(footer_width), qn("w:type"): "dxa"})
    etree.SubElement(table_props, qn("w:tblLayout")).set(qn("w:type"), "fixed")
    borders = etree.SubElement(table_props, qn("w:tblBorders"))
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        etree.SubElement(borders, qn("w:" + edge)).set(qn("w:val"), "nil")
    margins = etree.SubElement(table_props, qn("w:tblCellMar"))
    for edge in ("top", "left", "bottom", "right"):
        etree.SubElement(margins, qn("w:" + edge), {qn("w:w"): "0", qn("w:type"): "dxa"})
    grid = etree.SubElement(table, qn("w:tblGrid"))
    row = etree.SubElement(table, qn("w:tr"))
    cells = []
    for width in (footer_width - 900, 900):
        etree.SubElement(grid, qn("w:gridCol")).set(qn("w:w"), str(width))
        cell = etree.SubElement(row, qn("w:tc"))
        cell_props = etree.SubElement(cell, qn("w:tcPr"))
        etree.SubElement(cell_props, qn("w:tcW"), {qn("w:w"): str(width), qn("w:type"): "dxa"})
        etree.SubElement(cell_props, qn("w:vAlign")).set(qn("w:val"), "bottom")
        cells.append(cell)
    parent = paragraph.getparent()
    position = parent.index(paragraph)
    parent.remove(paragraph)
    cells[0].append(paragraph)
    parent.insert(position, table)

    page_paragraph = etree.SubElement(cells[1], qn("w:p"))
    page_props = etree.SubElement(page_paragraph, qn("w:pPr"))
    etree.SubElement(page_props, qn("w:jc")).set(qn("w:val"), "right")
    etree.SubElement(page_props, qn("w:spacing"), {qn("w:before"): "0", qn("w:after"): "0"})
    run = etree.SubElement(page_paragraph, qn("w:r"))
    if style is not None:
        run.append(style)
    etree.SubElement(run, qn("w:t")).text = "P"
    for field, separator in (("PAGE", "/"), ("NUMPAGES", "")):
        etree.SubElement(run, qn("w:fldChar")).set(qn("w:fldCharType"), "begin")
        instruction = etree.SubElement(run, qn("w:instrText"))
        instruction.set(qn("xml:space"), "preserve")
        instruction.text = " " + field + " "
        etree.SubElement(run, qn("w:fldChar")).set(qn("w:fldCharType"), "separate")
        etree.SubElement(run, qn("w:t")).text = "1"
        etree.SubElement(run, qn("w:fldChar")).set(qn("w:fldCharType"), "end")
        if separator:
            etree.SubElement(run, qn("w:t")).text = separator
