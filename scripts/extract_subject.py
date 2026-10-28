### ЧТО ДЕЛАЕТ: распаковка «тяжёлых» док механизма: конверт .rtf/.doc (Word 97, в том числе utf-16le) и
###   .pdf (в том числе сжатых pypdf) в текст, извлечение предмета закупки, номенклатуры и места поставки
###   в файл «предмет.md» рядом с оригиналами (для агентов-чтецов по документам).
### ЗАПУСК: python extract_subject.py <путь к папке комплекта>   # папка tenders\с найденными\<номер>
### ЗАВИСИМОСТИ: python 3.10+, pip install pypdf; docx/rtf парсят std-модулями (zip/txt).
### ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ: нет.
### ВХОД: папка комплекта с documents\.docx/.doc/.rtf/.pdf внутри.
### ВЫХОД: новые файлы *.txt рядом с оригиналами + предмет.md (предмет, наименования товаров, места поставки).
#!/usr/bin/env python3
# конвертер вложений закупки (.rtf/.doc/.pdf → текст) + вытащивание предмета и места поставки.
# результат: <комплект>/предмет.md — вход для харибды; машинные файлы (tender.json, сводка.txt) не трогаем.
# python extract_subject.py <папка комплекта> [...] [-c config.yaml]   # город для проверки места — из delivery_cities

import argparse
import json
import re
import sys
from pathlib import Path

import olefile

import gosplan_lib as gp

HERE = Path(__file__).resolve().parent


# ---------- конвертация файлов в текст ----------

def rtf_to_text(data):
    # простой съём rtf: выкидываем группы-команды, шифрованные \'hh читаем из cp1251
    out, i, n = [], 0, len(data)
    text = data.decode("cp1251", errors="ignore")
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            m = re.match(r"\\'([0-9a-fA-F]{2})", text[i:])
            if m:  # cp1251 байт
                out.append(bytes([int(m.group(1), 16)]).decode("cp1251", errors="ignore"))
                i += 4
                continue
            m = re.match(r"\\u(-?\d+)\??", text[i:])
            if m:  # utf-16 кодон
                v = int(m.group(2) if False else m.group(1))
                out.append(chr(v & 0xFFFF) if v >= 0 else chr(v + 0x10000))
                i += len(m.group(0))
                continue
            if text[i:i+2] in ("\\{", "\\}", "\\\\"):
                out.append(text[i+1])
                i += 2
                continue
            m = re.match(r"\\([a-zA-Z]+)(-?\d+)? ?", text[i:])
            if m:
                word = m.group(1)
                if word == "par" or word == "line":
                    out.append("\n")
                i += len(m.group(0))
                continue
            i += 1
            continue
        if ch == "{":
            # читаемая группа целиком? пропускаем управляющие {\*...} — просто идём дальше
            i += 1
            continue
        if ch == "}":
            i += 1
            continue
        out.append(ch)
        i += 1
    txt = "".join(out)
    txt = re.sub(r"[ \t]+", " ", txt)
    return txt


def doc_to_text(path):
    # .doc (Word 97): текст в потоке WordDocument, кириллица обычно utf-16le;
    # выбираем декодировку по частоте частотных русских слов (в utf-16 и cp1251 бинарь даёт мусор)
    ole = olefile.OleFileIO(str(path))
    try:
        raw = ole.openstream("WordDocument").read()
    finally:
        ole.close()
    candidates = [raw.decode("utf-16le", errors="ignore"), raw.decode("cp1251", errors="ignore")]
    stop = ("контракт|товар|заказчик|поставщик|закупк|стоимость|договор|новосибирск|услуг|пункт")
    def score(t):
        return sum(len(re.findall(w, t, re.IGNORECASE)) for w in stop.split("|"))
    best = max(candidates, key=score)
    best = re.sub(r"\s+", " ", best)
    return best


def _cyrrillic_score(t):
    # какая из декодировок даёт больше осмысленных русских слов
    words = re.findall(r"[а-яА-ЯёЁ]{4,}", t or "")
    return len(set(w.lower() for w in words))


def pdf_to_text(path):
    from pypdf import PdfReader
    try:
        r = PdfReader(str(path))
        return "\n".join((p.extract_text() or "") for p in r.pages)
    except Exception as e:
        return f"[pdf не прочитался: {e}]"


def convert_file(p):
    data = p.read_bytes()
    if p.suffix.lower() == ".rtf" or data[:32].strip().startswith(b"{\\rtf"):
        return rtf_to_text(data)
    if p.suffix.lower() == ".pdf" or data[:5] == b"%PDF-":
        return pdf_to_text(p)
    if p.suffix.lower() == ".doc" and data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return doc_to_text(p)
    if p.suffix.lower() in (".txt", ".xml"):
        return data.decode("utf-8", errors="ignore")
    return None


# ---------- вытаскивание предмета и места ----------

SUBJECT_PATTERNS = [
    r"(?:наименование\s+(?:объекта\s+)?(?:закупки|контракта)|предмет\s+(?:контракта|договора|закупки)|объект\s+закупки)\s*[:\u2014\u2013-]?\s*\n?\s*([^\n]{5,250})",
]
ITEM_PATTERN = re.compile(r"(?:наименование\s+(?:товара|объекта\s+закупки|позиции))\s*[:\u2014\u2013-]?\s*\n?\s*([^\n]{3,250})")


def _cut_at_article(ln):
    # обрубаем «поставить … 1.2. Наименование …» на границе пункта
    m = re.search(r"\s(\d+\.\d+\.)\s", ln)
    return ln[:m.start()] if m and m.start() > 30 else ln


def dedup_lines(lines, limit=15):
    seen, out = set(), []
    for ln0 in lines:
        ln = re.sub(r"\s+", " ", ln0).strip().strip(";.,")
        if len(ln) < 3 or ln.lower() in seen:
            continue
        seen.add(ln.lower())
        out.append(_cut_at_article(ln))
        if len(out) >= limit:
            break
    return out


def extract_subject_texts(texts):
    hits = []
    for t in texts:
        for pat in SUBJECT_PATTERNS:
            hits += dedup_lines(m.group(1) for m in re.finditer(pat, t, re.IGNORECASE))
    # характерные предметные строки: «поставка …», «выполнение …», «оказание …»
    for t in texts:
        hits += dedup_lines(re.findall(r"(?m)^.{0,6}(?:поставка|выполнение|оказание|изготовление|услуги по)[^\n]{15,200}", t))
    # линии-шум: формулировки статей 43/44, пункты уведомлений
    return [h for h in hits if not re.search(r"стать[ижу]+\s?\d+|подпунктами", h, re.IGNORECASE)]


def extract_item_texts(texts):
    hits = []
    for t in texts:
        hits += dedup_lines(m.group(1) for m in ITEM_PATTERN.finditer(t))
    return dedup_lines(hits, 12)


def extract_places(texts, cities):
    places = []
    for t in texts:
        # окно вокруг маркеров места поставки/доставки
        for m in re.finditer(r"(?:мест[оеа]?\s+поставки|адрес[ыа]? мест[а-я]*|мест[оа-я]{0,64}доставк|место\s+исполнения|по\s+адресу)", t, re.IGNORECASE):
            seg = t[m.start():m.start() + 240]
            seg = re.sub(r"\s+", " ", seg)
            places.append(seg)
        # строки-адреса с индексом «630xxx, …»
        places += re.findall(r"\b\d{6}\s*,\s*[^;{}\n]{10,160}", t)
    uniq = dedup_lines(places, 10)
    verdict = gp.delivery_city_verdict({"delivery_places": uniq}, cities)
    return uniq, verdict


def tender_ktru_names(detail):
    # предметные позиции прямо из tender.json (purchaseObjectsInfo): имена блоков с фильтром служебных строк
    out = []
    def walk(o):
        if isinstance(o, dict):
            nm = o.get("name")
            if isinstance(nm, str) and re.search(r"[а-яА-ЯёЁ]", nm) and len(nm) > 20 \
               and not re.search(r"Закупка|РТС-тендер|бюджет|циркуляр|Закона|РУБ|шт\.|см\[|округу|требования|параметр|Электронн|аптеки|склад|Обоснование|Муниципальные образования|раздел", nm):
                out.append(nm)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(detail)
    return dedup_lines(out, 10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kits", nargs="+")
    ap.add_argument("-c", "--config", default=str(HERE / "config.novosibirsk.yaml"))
    args = ap.parse_args()
    import yaml
    cities = ((yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {})
              .get("filters", {}) or {}).get("delivery_cities") or ["Новосибирск"]

    for kit in args.kits:
        kit = Path(kit)
        docs = kit / "документы"
        texts = []
        if docs.is_dir():
            for p in sorted(docs.iterdir()):
                t = convert_file(p)
                if t and len(t.strip()) > 50:
                    texts.append((p.name, t))
                    txt_path = docs / (p.stem + ".txt")
                    txt_path.write_text(t, encoding="utf-8")
        subjects = extract_subject_texts([t for _, t in texts])
        items = extract_item_texts([t for _, t in texts])
        places, verdict = extract_places([t for _, t in texts], cities)
        detail_path = kit / "tender.json"
        ktru = tender_ktru_names(json.loads(detail_path.read_text(encoding="utf-8"))) if detail_path.exists() else []
        md = "# предмет и место поставки (извлечено скриптом из документов комплекта)\n\n"
        md += "## предмет (по текстам проекта контракта/обоснования)\n"
        md += "\n".join(f"- {s}" for s in subjects) if subjects else "— не найдено по шаблонам"
        md += "\n\n## предмет (покупка объектов из ЕИС, tender.json)\n"
        md += "\n".join(f"- {s}" for s in ktru) if ktru else "—"
        if items:
            md += "\n\n## наименования товара (из проекта контракта)\n"
            md += "\n".join(f"- {s}" for s in items)
        md += "\n\n## место поставки (адреса из документа)\n"
        md += "\n".join(f"- {p2}" for p2 in places) if places else "— не найдено адресов"
        md += f"\n\nгород по месту поставки: {verdict} (None = данных нет; True = {', '.join(cities)})\n"
        (kit / "предмет.md").write_text(md, encoding="utf-8")
        print(f"{kit.name}: файлов прочитано {len(texts)}, предметных строк {len(subjects) or len(ktru)}, мест {len(places)}, город {verdict}")


if __name__ == "__main__":
    sys.exit(main())

