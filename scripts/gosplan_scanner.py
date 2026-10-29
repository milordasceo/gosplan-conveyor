### ЧТО ДЕЛАЕТ: консольный сборщик закупок с API gosplan.info: выборка по фильтрам
###   конфига (и cli-перекрытиям), дедуп по purchase_number через tenders\seen.json.
###   --collect N кладёт комплекты в tenders\с найденными\<номер>\ (сводка.txt, tender.json, документы\).
###   --export DIR — отдельный режим: DIR\<номер>\tender.md + tender.json, без скачивания файлов.
###   в каталоге выгрузки только папки номеров. реестр — в state_dir, не в DIR.
###   --kind берёт коды purchase_type из блока purchase_kinds конфига (не из кода).
###   --limit в --export — сколько папок записать за этот запуск. 0 = все из очереди.
###   --city-strict: вне города не пишем (статус outside). --collect всегда строгий.
### ЗАПУСК: python gosplan_scanner.py -c config.novosibirsk.yaml [--collect N] [--export DIR] [--limit N]
###   [--city-strict] [--dry-run] [--region CODE] [--price-min N] [--price-max N] [--stage N]
###   [--types 44|223] [--kind auction|zk|ok|zp|ep]
###   [--deadline-from ДАТА] [--deadline-to ДАТА] [--since ДАТА|today|auto] [--no-export]
###   без --since берётся filters.since, иначе auto. deadline_from/deadline_to: дата, now или +3d/+10d/+12h
###   от момента запуска (москва, фиксируется один раз на прогон). окно подачи сверяется по свежей карточке
###   перед каждой выгрузкой: прошёл или раньше deadline_from — статус expired (окончательно); позже
###   deadline_to — too_early, нет даты — no_deadline (оба вернутся в очередь при следующем поиске).
###   пропуски по сроку в --limit не входят. export.dir в конфиге включает выгрузку,
###   если не заданы --export, --collect и --no-export.
### ЗАВИСИМОСТИ: python 3.10+, pip install pyyaml. сеть: gosplan.info и zakupki.gov.ru
###   (для zakupki нужен российский CA «Russian Trusted Root CA» — см. runbooks).
### ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ: GOSPLAN_STATE_DIR — каталог реестра и слота api
###   (иначе state_dir конфига, иначе папка скрипта\state).
###   PYTHONIOENCODING=utf-8 желателен на windows. ключ api, если нужен, — в конфиге, поле api_key.
### ВХОД: yaml-конфиг + необязательные cli-фильтры.
### ВЫХОД: в обычном режиме tenders\с найденными\<номер>\* + tenders\run_<timestamp>.json|csv;
###   в --export — только DIR\<номер>\*. реестр и run_report.json — в state_dir и logs\.
###   на stdout ровно три строки: найдено / новых / выгружено; остальное — в stderr.
###   код 0 — очередь пуста, 3 — добор не закончен, 2 — ошибка конфига.
#!/usr/bin/env python3
# разведка gosplan.info: выборка закупок по критериям, дедуп по номеру закупки.
# python >=3.9, зависимости: pyyaml. тестовый сервер gosplan.info работает без api-ключа.

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import gosplan_lib as gp

HERE = Path(__file__).resolve().parent
UA = "gosplan-scanner/1.0 (local tender recon tool; +https://gosplan.info/docs)"
MSK = ZoneInfo("Europe/Moscow")
LAW = {"fz44": "44-ФЗ", "fz223": "223-ФЗ"}
TYPE_TO_SOURCE = {"44": "fz44", "223": "fz223"}
# виды для --kind; коды живут в конфиге (purchase_kinds), здесь только допустимые имена
KIND_ALLOWED = ("auction", "zk", "ok", "zp", "ep")

FIELDS = [
    "purchase_number", "source", "region", "max_price", "currency_code",
    "stage", "purchase_type", "object_info", "okpd2", "published_at",
    "collecting_finished_at", "customers", "delivery_places", "url",
]


def http_get(url, params=None, api_key=None, timeout=30):
    # троттлинг 10 req/min живёт в gosplan_lib.http_json (общий с панелью)
    return gp.http_json(url, params, api_key, timeout)


def moscow_today():
    # даты еис/api — московские, «сегодня» считаем по europe/moscow
    return datetime.now(MSK).date()


def moscow_now():
    # текущий момент по москве, naive, до секунды — в одной шкале с датами api
    return datetime.now(MSK).replace(tzinfo=None, microsecond=0)


# относительный момент: now, +3d, +10d, +12h, now+3d, -1d (дни d, часы h) от текущего момента по москве
RELATIVE_RE = re.compile(r"^(?:now)?\s*(?:([+-])\s*(\d+(?:[.,]\d+)?)\s*([dh]))?$", re.IGNORECASE)


def parse_relative(value, base=None):
    # datetime для относительной записи, иначе None. base — точка отсчёта (момент старта прогона)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    m = RELATIVE_RE.match(s)
    if not m:
        return None
    sign, num, unit = m.groups()
    if not num and s.lower() != "now":
        return None
    base = base or moscow_now()
    if not num:
        return base
    qty = float(num.replace(",", "."))
    delta = timedelta(days=qty) if unit.lower() == "d" else timedelta(hours=qty)
    return base + delta if sign == "+" else base - delta


def relative_label(value):
    # «3 дней» / «12 ч» для логов по записи +3d / +12h; None — граница не относительная
    if not isinstance(value, str):
        return None
    m = RELATIVE_RE.match(value.strip())
    if not m or not m.group(2):
        return None
    num = m.group(2).replace(",", ".")
    num = num[:-2] if num.endswith(".0") else num
    return f"{num} дней" if m.group(3).lower() == "d" else f"{num} ч"


def parse_moment(value):
    # YYYY-MM-DD, iso datetime, «today», «now» или +3d/+12h → date | datetime (naive = москва)
    # дата с поясом переводится в москву, затем пояс снимается
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(MSK).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    s = str(value).strip()
    if s.lower() == "today":
        return moscow_today()
    rel = parse_relative(s)
    if rel is not None:
        return rel
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            if len(s) == 10:
                return datetime.strptime(s, "%Y-%m-%d").date()
            dt = datetime.fromisoformat(s)
            if dt.tzinfo:
                dt = dt.astimezone(MSK).replace(tzinfo=None)
            return dt
    except ValueError:
        pass
    raise ValueError(f"неверная дата: {value}")


def moment_to_param(value):
    # в query: дата как YYYY-MM-DD, иначе iso datetime
    m = parse_moment(value)
    if m is None:
        return None
    if isinstance(m, datetime):
        return m.isoformat(timespec="seconds")
    return m.isoformat()


def _cmp_parts(value, bound):
    v = parse_moment(value)
    b = parse_moment(bound) if not isinstance(bound, (date, datetime)) else bound
    if v is None or b is None:
        return None, None
    if isinstance(v, datetime) and not isinstance(b, datetime):
        v = v.date()
    elif isinstance(b, datetime) and not isinstance(v, datetime):
        v = datetime.combine(v, dtime.min)
    return v, b


def moment_ge(value, bound):
    v, b = _cmp_parts(value, bound)
    if v is None:
        return False
    return v >= b


def moment_le(value, bound):
    v, b = _cmp_parts(value, bound)
    if v is None:
        return False
    return v <= b


def resolve_now_bounds(f, now=None):
    # deadline_from/deadline_to: now, +3d, +10d, +12h фиксируются один раз на старте прогона (москва).
    # исходная запись остаётся в deadline_window — для логов и run_report
    base = now or moscow_now()
    window = dict(f.get("deadline_window") or {})
    for key in ("deadline_from", "deadline_to"):
        v = f.get(key)
        rel = parse_relative(v, base)
        if rel is not None:
            window[key] = v.strip()
            f[key] = rel.isoformat(timespec="seconds")
    if window:
        window["resolved_at"] = base.isoformat(timespec="seconds")
        f["deadline_window"] = window
    return f


def arg_moment(s):
    if str(s).strip().lower() == "auto":
        return "auto"
    try:
        parse_moment(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e
    return s


def api_number(v):
    # в query целое без .0: 30000, не 30000.0
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if f.is_integer():
        return int(f)
    return f


def split_multi(values):
    # --flag a --flag b,c → ["a","b","c"]
    out = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    gp.atomic_replace(tmp, path)


def _relax_stdio():
    # консоль windows (cp866/cp1251) не должна падать на символе вне кодировки
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        enc = getattr(stream, "encoding", None) or "utf-8"
        try:
            reconfigure(encoding=enc, errors="replace")
        except (OSError, ValueError):
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def norm_record(item, source, base_url=""):
    number = item.get("purchase_number") or ""
    customers = item.get("customers")
    if isinstance(customers, str):
        customers = [customers] if customers else []
    elif not customers:
        c = item.get("customer")
        customers = [c] if c else []
    deadline = item.get("collecting_finished_at") or item.get("submission_close_at")
    ptype = item.get("purchase_type")
    notice_id = gp.extract_notice_info_id(item)
    return {
        "purchase_number": number,
        "source": source,
        "region": item.get("region"),
        "max_price": item.get("max_price"),
        "currency_code": item.get("currency_code"),
        "stage": item.get("stage"),
        "purchase_type": ptype,
        "object_info": item.get("object_info"),
        "okpd2": item.get("okpd2") or [],
        "published_at": item.get("published_at"),
        "collecting_finished_at": deadline,
        "customers": customers,
        "delivery_places": item.get("delivery_places") or [],
        "url": gp.zakupki_search_url(number),
        "zakupki_card": gp.zakupki_card_url(source, ptype, number, notice_id),
        "gosplan_url": gp.gosplan_detail_url(base_url, source, number) if base_url else "",
    }


def keyword_ok(text, keywords, require_all):
    if not keywords:
        return True
    hay = (text or "").lower()
    hits = [k for k in keywords if k.lower() in hay]
    return len(hits) == len(keywords) if require_all else bool(hits)


def deadline_moment(raw):
    # окончание подачи как naive-москва datetime; дата без времени — конец этого дня
    m = parse_moment(raw)
    if m is None:
        return None
    if not isinstance(m, datetime):
        m = datetime.combine(m, dtime(23, 59, 59))
    return m


def detail_deadline(detail):
    # окончание подачи из карточки api: 44-ФЗ collecting_finished_at, 223-ФЗ submission_close_at
    d = detail if isinstance(detail, dict) else {}
    return d.get("collecting_finished_at") or d.get("submission_close_at") or None


def window_verdict(raw, f, now=None):
    # окно подачи на выгрузке: "ok"; "expired" — срок уже прошёл; "too_late" — раньше deadline_from
    # (до окончания подачи меньше нижней границы, окончательно); "too_early" — позже deadline_to
    # (рано, вернётся позже); "no_deadline" — даты нет или не разобрать
    try:
        m = deadline_moment(raw)
    except ValueError:
        return "no_deadline"
    if m is None:
        return "no_deadline"
    now = now or moscow_now()
    if m <= now:
        return "expired"
    d_from = (f or {}).get("deadline_from")
    d_to = (f or {}).get("deadline_to")
    try:
        if d_from and not moment_ge(m, d_from):
            return "too_late"
        if d_to and not moment_le(m, d_to):
            return "too_early"
    except ValueError:
        return "ok"
    return "ok"


def window_reason(verdict, f, raw=None):
    # текст для лога и run_report
    win = (f or {}).get("deadline_window") or {}
    if verdict == "expired":
        return "срок подачи истёк"
    if verdict == "no_deadline":
        return "нет даты окончания подачи"
    if verdict == "too_late":
        lab = relative_label(win.get("deadline_from"))
        return (f"до окончания подачи меньше {lab}" if lab
                else f"окончание подачи раньше {f.get('deadline_from')}")
    if verdict == "too_early":
        lab = relative_label(win.get("deadline_to"))
        return (f"до окончания подачи больше {lab} — позже" if lab
                else f"окончание подачи позже {f.get('deadline_to')} — позже")
    return ""


def deadline_verdict(raw, now=None):
    # "ok" — подача ещё идёт, "expired" — срок истёк, "no_deadline" — даты нет или не разобрать
    try:
        m = deadline_moment(raw)
    except ValueError:
        return "no_deadline"
    if m is None:
        return "no_deadline"
    now = now or moscow_now()
    return "ok" if m > now else "expired"


def client_deadline_ok(rec, f, allow_missing=False):
    # повторная проверка окончания подачи: сервер мог проигнорировать фильтр.
    # allow_missing: запись без даты пропускаем дальше — дату возьмёт карточка перед выгрузкой
    d_from = f.get("deadline_from")
    d_to = f.get("deadline_to")
    if not d_from and not d_to:
        return True
    raw = rec.get("collecting_finished_at")
    if not raw:
        return bool(allow_missing)
    try:
        # дата без времени — до конца дня, как на выгрузке
        raw = deadline_moment(raw)
        if d_from and not moment_ge(raw, d_from):
            return False
        if d_to and not moment_le(raw, d_to):
            return False
    except ValueError:
        return bool(allow_missing)
    return True


def client_since_ok(rec, f):
    since = f.get("published_after")
    if not since:
        return True
    raw = rec.get("published_at")
    if not raw:
        return False
    return moment_ge(raw, since)


def build_params(f, api_filters, source=None):
    p = {}
    pt = f.get("purchase_types")
    if pt:
        if isinstance(pt, dict):
            pt = pt.get(source or "")
        if pt:
            p["purchase_type"] = pt
    if f.get("okpd2"):
        p["classifier"] = f["okpd2"]
    if f.get("min_budget") is not None:
        p["max_price_ge"] = api_number(f["min_budget"])
    if f.get("max_budget") is not None:
        p["max_price_le"] = api_number(f["max_budget"])
    if f.get("published_after"):
        p["published_after"] = moment_to_param(f["published_after"])
    if f.get("published_before"):
        p["published_before"] = moment_to_param(f["published_before"])
    if f.get("regions"):
        p["region"] = [int(r) for r in f["regions"]]
    if f.get("stage") is not None and f.get("stage") != "":
        st = f["stage"]
        if not isinstance(st, list):
            st = [st]
        p["stage"] = [str(s) for s in st]
    if f.get("currency"):
        p["currency_code"] = f["currency"]
    # дедлайн: разные имена полей у 44 и 223
    d_from = moment_to_param(f.get("deadline_from"))
    d_to = moment_to_param(f.get("deadline_to"))
    if d_from:
        if source == "fz44":
            p["collecting_finished_after"] = d_from
        elif source == "fz223":
            p["submission_close_after"] = d_from
    if d_to:
        if source == "fz44":
            p["collecting_finished_before"] = d_to
        elif source == "fz223":
            p["submission_close_before"] = d_to
    # серверный поиск по наименованию: работает на сервере, но сбрасывает сортировку — применяем всегда
    if f.get("keywords") and api_filters:
        p["object_info"] = " ".join(f["keywords"])
    return p


def fetch_source(base_url, source, params, api_key, page_size, max_pages, pause_ms, timeout, log):
    out = []
    skip = 0
    net_retries = 3  # тестовый сервер иногда рвёт соединение на середине ответа
    for _ in range(max_pages):
        q = dict(params)
        q["limit"] = page_size
        q["skip"] = skip
        url = f"{base_url}/{source}/purchases"
        # 429 — общий разбор в gosplan_lib (рост интервала); сетевые сбои списка — здесь через net_retries
        batch = gp.get_with_backoff(
            url, q, api_key, timeout, log, net_retries=net_retries, net_note=f"skip={skip}",
        )
        if not isinstance(batch, list):
            raise RuntimeError(f"неожиданный ответ API: {str(batch)[:200]}")
        out.extend(batch)
        if len(batch) < page_size:
            break
        skip += page_size
        if skip > 1000:  # API ограничивает skip<=1000, глубже выборки нет
            log("skip достиг максимума API (1000), остальное — сузь фильтры")
            break
        time.sleep(pause_ms / 1000.0)
    return out


def load_seen(seen_path):
    if seen_path.exists():
        return json.loads(seen_path.read_text(encoding="utf-8-sig"))
    return {}


def save_seen(seen_path, seen):
    tmp = seen_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    gp.atomic_replace(tmp, seen_path)


def cleanup_reports(out_dir, keep_days):
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    for p in out_dir.glob("run_*.json"):
        if p.stat().st_mtime < cutoff:
            p.unlink()


def print_summary(found, new, exported):
    print(f"найдено: {found}")
    print(f"новых: {new}")
    print(f"выгружено: {exported}")


def load_export_state(path):
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_export_state(path, state):
    write_atomic(path, json.dumps(state, ensure_ascii=False, indent=1))


def _state_status(state, number):
    st = state.get(number)
    if isinstance(st, dict):
        return st.get("status")
    return None


def is_new_for_export(number, state, export_dir, strict=False):
    # status outside от строгого прогона в мягком режиме — ещё не выгружено
    status = _state_status(state, number)
    if status == "exported":
        return False
    if status == "outside":
        return not strict
    if (export_dir / number).exists():
        return False
    return True


def merge_places(rec, detail):
    # тексты мест: список записи + разбор детали, без повторов
    out, seen = [], set()
    sources = [rec.get("delivery_places") or []]
    sources.append(gp.extract_delivery_texts(detail or {}))
    for bucket in sources:
        for raw in bucket:
            text = " ".join(str(raw).split())
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            out.append(text)
    return out


def city_verdict(rec, detail, cities):
    if not cities:
        return None
    verdict = gp.delivery_city_verdict(detail or {}, cities)
    if verdict is None and rec.get("delivery_places"):
        verdict = gp.delivery_city_verdict({"delivery_places": rec.get("delivery_places")}, cities)
    return verdict


def city_label(verdict, cities):
    # подпись для tender.json и ключ city в состоянии
    if not cities:
        return "не проверялось", "unknown"
    if verdict is True:
        return "в городе", "inside"
    if verdict is False:
        return "вне города", "outside"
    return "нет данных", "unknown"


def export_payload(rec, detail, base_url, cities=None):
    source = rec["source"]
    number = rec["purchase_number"]
    ptype = rec.get("purchase_type") or (detail or {}).get("purchase_type")
    notice_id = gp.extract_notice_info_id(detail) if detail else None
    card = gp.zakupki_card_url(source, ptype, number, notice_id) or rec.get("zakupki_card")
    customers = rec.get("customers") or []
    docs = []
    for att in gp.extract_attachments(detail or {}):
        docs.append({
            "name": att.get("fileName") or "",
            "description": att.get("description") or "",
            "url": att.get("url") or "",
            "size": att.get("size"),
        })
    d = detail or {}
    places = merge_places(rec, d)
    label, _city_key = city_label(city_verdict(rec, d, cities or []), cities or [])
    return {
        "number": number,
        "law": LAW.get(source, source),
        "purchase_type": ptype,
        "purchase_type_name": gp.purchase_type_name(ptype),
        "subject": rec.get("object_info") or d.get("object_info"),
        "initial_price": rec.get("max_price") if rec.get("max_price") is not None else d.get("max_price"),
        "currency": rec.get("currency_code") or d.get("currency_code"),
        "region": rec.get("region") if rec.get("region") is not None else d.get("region"),
        "stage": rec.get("stage") if rec.get("stage") is not None else d.get("stage"),
        "stage_end": detail_deadline(d) or rec.get("collecting_finished_at"),
        "published_at": rec.get("published_at") or d.get("published_at"),
        "customers": customers,
        "delivery_places": places,
        "city_check": label,
        "zakupki_search": rec.get("url") or gp.zakupki_search_url(number),
        "zakupki_card": card,
        "gosplan": rec.get("gosplan_url") or gp.gosplan_detail_url(base_url, source, number),
        "documents": docs,
    }


def format_tender_md(data, cities=None):
    cust = data.get("customers") or []
    if isinstance(cust, list):
        cust_s = ", ".join(str(c) for c in cust) if cust else "—"
    else:
        cust_s = str(cust) if cust else "—"
    price = data.get("initial_price")
    cur = data.get("currency") or ""
    price_s = f"{price} {cur}".strip() if price is not None else "—"
    card = data.get("zakupki_card") or "—"
    ptype = data.get("purchase_type")
    pname = data.get("purchase_type_name")
    if ptype and pname:
        type_s = f"{ptype} ({pname})"
    elif ptype:
        type_s = str(ptype)
    else:
        type_s = "—"
    places = data.get("delivery_places") or []
    places_s = "; ".join(str(p) for p in places if p) or "—"
    label = data.get("city_check") or "нет данных"
    if cities:
        city_s = f"{label} ({', '.join(str(c) for c in cities)})"
    else:
        city_s = label
    lines = [
        f"# {data.get('number') or ''}",
        "",
        f"- закон: {data.get('law') or '—'}",
        f"- тип закупки: {type_s}",
        f"- предмет: {data.get('subject') or '—'}",
        f"- нмцк: {price_s or '—'}",
        f"- регион: {data.get('region') if data.get('region') is not None else '—'}",
        f"- место поставки: {places_s}",
        f"- город: {city_s}",
        f"- этап: {data.get('stage') if data.get('stage') is not None else '—'}",
        f"- окончание этапа: {data.get('stage_end') or '—'}",
        f"- опубликовано: {data.get('published_at') or '—'}",
        f"- заказчики: {cust_s}",
        f"- поиск на zakupki: {data.get('zakupki_search') or '—'}",
        f"- карточка zakupki: {card}",
        f"- api gosplan: {data.get('gosplan') or '—'}",
        "",
        "## Документы",
        "",
    ]
    docs = data.get("documents") or []
    if not docs:
        lines.append("нет документов")
    else:
        for i, doc in enumerate(docs, 1):
            name = doc.get("name") or "документ"
            url = doc.get("url") or ""
            desc = doc.get("description") or ""
            lines.append(f"{i}. [{name}]({url}) — {desc}")
    lines.append("")
    return "\n".join(lines)


def _parse_law_types(raw):
    mapped = []
    items = raw if isinstance(raw, list) else [raw]
    for t in split_multi(items):
        src = TYPE_TO_SOURCE.get(str(t)) or TYPE_TO_SOURCE.get(str(t).lower())
        if not src:
            raise ValueError(f"неизвестный тип закона: {t} (нужно 44 или 223)")
        if src not in mapped:
            mapped.append(src)
    if not mapped:
        raise ValueError("пустой список законов")
    return mapped


def _parse_kinds(raw):
    kinds = []
    allowed = ", ".join(KIND_ALLOWED)
    items = raw if isinstance(raw, list) else [raw]
    for kind in split_multi(items):
        if kind not in KIND_ALLOWED:
            raise ValueError(f"неизвестный вид {kind!r}: допустимо {allowed}")
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def apply_cli_filters(f, sources, args):
    # cli перекрывает конфиг, конфиг — умолчания. пустой --since: filters.since, иначе auto
    if args.region:
        f["regions"] = [int(x) for x in split_multi(args.region)]
    if args.price_min is not None:
        f["min_budget"] = args.price_min
    if args.price_max is not None:
        f["max_budget"] = args.price_max
    if args.stage is not None:
        st = split_multi([args.stage]) if isinstance(args.stage, str) else [args.stage]
        f["stage"] = st if len(st) > 1 else (st[0] if st else args.stage)
    if not args.types and f.get("types"):
        args.types = f.get("types")
    if args.types:
        sources = _parse_law_types(args.types)
    if not args.kind and f.get("kinds"):
        args.kind = f.get("kinds")
    if args.kind:
        args.kind = _parse_kinds(args.kind)
    if args.deadline_from:
        f["deadline_from"] = args.deadline_from
    if args.deadline_to:
        f["deadline_to"] = args.deadline_to
    if args.since is not None:
        since = args.since
    else:
        since = f.get("since")
        if since is None or str(since).strip() == "":
            since = "auto"
    args.since = str(since).strip()
    # auto уточняется по реестру позже. явная дата и today ставятся сразу
    if args.since.lower() == "auto":
        f["published_after"] = None
    else:
        f["published_after"] = args.since
    return f, sources


def _codes_for_source(f, source):
    # непустой набор кодов purchase_type для источника или None (фильтра нет)
    pt = f.get("purchase_types")
    if not pt:
        return None
    if isinstance(pt, dict):
        codes = pt.get(source) or []
    elif isinstance(pt, list):
        codes = pt
    elif isinstance(pt, str):
        codes = [pt]
    else:
        return None
    if isinstance(codes, str):
        codes = [codes]
    cleaned = [c for c in codes if c]
    if not cleaned:
        return None
    return set(cleaned)


def apply_purchase_kinds(cfg, f, sources, kinds, cfg_path):
    # --kind: объединение кодов из purchase_kinds перекрывает filters.purchase_types.
    # пустой набор по источнику — источник не опрашиваем (запрос без purchase_type вернул бы всё).
    if not kinds:
        return f, sources, []
    mapping = cfg.get("purchase_kinds")
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError(
            f"в конфиге {cfg_path} нет purchase_kinds — добавь блок как в config.novosibirsk.yaml"
        )
    by_source = {src: [] for src in sources}
    for kind in kinds:
        block = mapping.get(kind)
        if not isinstance(block, dict):
            raise ValueError(f"в конфиге {cfg_path} нет вида «{kind}» в purchase_kinds")
        for src in sources:
            codes = block.get(src) or []
            if isinstance(codes, str):
                codes = [codes]
            for code in codes:
                if code and code not in by_source[src]:
                    by_source[src].append(code)
    active, skipped = [], []
    for src in sources:
        if by_source[src]:
            active.append(src)
        else:
            skipped.append(src)
    f = dict(f)
    f["purchase_types"] = {src: by_source[src] for src in active}
    return f, active, skipped


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="разведка закупок через API gosplan.info",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "приоритет: аргумент cli, затем конфиг, затем умолчание.\n"
            "--since: cli, иначе filters.since, иначе auto (Europe/Moscow).\n"
            "--kind и filters.kinds берут коды из purchase_kinds.\n"
            "export.dir в конфиге включает выгрузку, если нет --export, --collect и --no-export.\n"
            "путь export.dir относительный считается от папки файла конфига.\n"
            "--export по умолчанию выгружает и закупки вне города; --city-strict отсекает их."
        ),
    )
    ap.add_argument("-c", "--config", default=str(HERE / "config.yaml"),
                    help="путь к yaml-конфигу (по умолчанию: config.yaml рядом со скриптом)")
    ap.add_argument("--dry-run", action="store_true",
                    help="вывести параметры запросов (stderr) и сводку 0/0/0, без обращений к api")
    ap.add_argument("--collect", type=int, default=0, metavar="N",
                    help="скачать документы первых N новых закупок в tenders/с найденными (по умолчанию: 0)")
    ap.add_argument("--export", metavar="DIR",
                    help="выгрузить новые закупки в DIR/<номер>/tender.md и tender.json без скачивания файлов")
    ap.add_argument("--remote", default=None, metavar="REMOTE",
                    help="вместо локальной папки: rclone-приёмник, например gosplan:Госзакупки/Сток. "
                         "нет флага — export.remote из конфига. бинарь — export.rclone, иначе rclone")
    ap.add_argument("--no-export", action="store_true",
                    help="не включать выгрузку из export.dir конфига: прежний режим seen/collect")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="в --export: набрать N выгруженных по порядку (published_at, номер); "
                         "уже exported входят в окно без повторного запроса; "
                         "без --city-strict «вне города» тоже считается; "
                         "0 = без лимита. нет флага — run.limit из конфига, иначе 0. в --collect игнорируется")
    ap.add_argument("--city-strict", action="store_true", default=None,
                    help="в --export: не выгружать закупки вне города (статус outside, в --limit не входят) "
                         "и отсекать их уже по списку. без флага — export.city_strict из конфига (по умолчанию нет). "
                         "--collect и так строгий")
    ap.add_argument("--kind", action="append", default=None, metavar="KIND",
                    help="вид закупки: auction (аукцион), zk (котировки), ok (конкурс), "
                         "zp (предложения), ep (ед. поставщик); можно повторять и через запятую. "
                         "коды из purchase_kinds конфига перекрывают filters.purchase_types")
    ap.add_argument("--region", action="append", default=None, metavar="CODE",
                    help="код региона kladr; можно повторять и через запятую (по умолчанию: filters.regions)")
    ap.add_argument("--price-min", type=float, default=None, metavar="N",
                    help="нижняя граница нмцк, руб (по умолчанию: filters.min_budget)")
    ap.add_argument("--price-max", type=float, default=None, metavar="N",
                    help="верхняя граница нмцк, руб (по умолчанию: filters.max_budget)")
    ap.add_argument("--stage", default=None, metavar="N",
                    help="этап 1..4, можно через запятую (по умолчанию: filters.stage; 1=подача заявок)")
    ap.add_argument("--types", action="append", default=None, metavar="LAW",
                    help="закон 44 и/или 223; можно повторять и через запятую (по умолчанию: sources из конфига)")
    ap.add_argument("--deadline-from", type=arg_moment, default=None, metavar="ДАТА",
                    help="окончание подачи заявок с: YYYY-MM-DD, iso datetime, now или от момента запуска "
                         "+3d / +12h; сервер+клиент+проверка карточки. нет флага — filters.deadline_from")
    ap.add_argument("--deadline-to", type=arg_moment, default=None, metavar="ДАТА",
                    help="окончание подачи заявок по: то же, напр. +10d; сервер+клиент+проверка карточки. "
                         "нет флага — filters.deadline_to")
    ap.add_argument("--since", type=arg_moment, default=None, metavar="ДАТА",
                    help='дата публикации с: YYYY-MM-DD, iso, "today" или "auto" '
                         "(нет флага — filters.since, иначе auto)")
    args = ap.parse_args(argv)
    if args.kind:
        try:
            args.kind = _parse_kinds(args.kind)
        except ValueError as e:
            ap.error(str(e))
    return args


def resolve_run_options(cfg, args, cfg_path):
    # лимит, строгость города и каталог выгрузки: cli > конфиг > умолчание
    if args.limit is None:
        run = cfg.get("run") if isinstance(cfg.get("run"), dict) else {}
        raw_limit = run.get("limit", 0)
        try:
            args.limit = int(raw_limit or 0)
        except (TypeError, ValueError):
            raise ValueError(f"run.limit в конфиге {cfg_path} должен быть целым числом")
    exp = cfg.get("export") if isinstance(cfg.get("export"), dict) else {}
    if args.city_strict is None:
        args.city_strict = bool(exp.get("city_strict"))
    raw_rclone = exp.get("rclone")
    args.rclone = str(raw_rclone).strip() if raw_rclone else "rclone"
    if not args.rclone:
        args.rclone = "rclone"
    cli_remote = str(args.remote).strip() if args.remote else ""
    cfg_remote = str(exp.get("remote") or "").strip()
    if args.no_export and (args.export or cli_remote):
        raise ValueError("нельзя одновременно --no-export и --export или --remote")
    # collect без явного --export/--remote не включает выгрузку из конфига
    if args.no_export or (args.collect and args.collect > 0 and not args.export and not cli_remote):
        if args.no_export:
            args.export = None
        args.remote = None
        return args
    args.remote = cli_remote or cfg_remote or None
    if args.export:
        args.export = str(Path(args.export).expanduser())
    else:
        raw_dir = exp.get("dir")
        if raw_dir is None or str(raw_dir).strip() == "":
            args.export = None
        else:
            path = Path(str(raw_dir)).expanduser()
            if not path.is_absolute():
                path = Path(cfg_path).resolve().parent / path
            args.export = str(path)
    return args


def registry_path(state_dir, export_dir=None, remote=None):
    # локальный каталог — sha1 пути. remote — sha1 самой строки приёмника
    if remote:
        material = str(remote).strip()
    else:
        material = str(Path(export_dir).resolve())
    key = hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]
    return Path(state_dir) / f"export_{key}.json"


def empty_registry(export_dir=None, remote=None):
    if export_dir:
        shown = str(Path(export_dir).resolve())
    else:
        shown = ""
    return {
        "export_dir": shown,
        "remote": str(remote).strip() if remote else None,
        "tenders": {},
        "pending": [],
        "pending_since": None,
        "last_completed_at": None,
    }


def load_registry(path, export_dir=None, remote=None):
    reg = empty_registry(export_dir, remote)
    if Path(path).exists():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("tenders"), dict):
            reg["tenders"] = data["tenders"]
            pending = data.get("pending")
            reg["pending"] = pending if isinstance(pending, list) else []
            reg["pending_since"] = data.get("pending_since")
            reg["last_completed_at"] = data.get("last_completed_at")
            if remote and not export_dir:
                reg["export_dir"] = data.get("export_dir") or ""
    if remote:
        reg["remote"] = str(remote).strip()
    elif export_dir:
        reg["export_dir"] = str(Path(export_dir).resolve())
    return reg


def save_registry(path, reg):
    # временный файл рядом с реестром, внутри state_dir
    write_atomic(path, json.dumps(reg, ensure_ascii=False, indent=2))


def migrate_legacy_state(export_dir, reg, log):
    # старый реестр лежал в каталоге выгрузки. переносим и удаляем
    legacy = Path(export_dir) / ".export_state.json"
    if not legacy.exists():
        return False
    old = load_export_state(legacy)
    moved = 0
    for num, entry in old.items():
        key = str(num)
        if not key or key in reg["tenders"] or not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if status not in ("exported", "outside"):
            continue
        reg["tenders"][key] = {
            "status": status,
            "city": entry.get("city") or "unknown",
            "at": entry.get("at") or datetime.now(MSK).isoformat(timespec="seconds"),
        }
        moved += 1
    try:
        legacy.unlink()
    except OSError as e:
        log(f"не удалось удалить старый реестр: {e}")
    log(f"перенесён старый реестр .export_state.json: {moved}")
    return True


def _moscow_day(value):
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK).date()


def finalize_since(f, args, reg, log):
    # auto: пустой реестр — без фильтра. иначе дата последнего полного прогона минус день
    if str(args.since).strip().lower() != "auto":
        log(f"since: {args.since}")
        return str(f.get("published_after") or args.since)
    tenders = (reg or {}).get("tenders") or {}
    last = (reg or {}).get("last_completed_at") if reg else None
    if not tenders or not last:
        f["published_after"] = None
        log("since: auto, без фильтра публикации")
        return None
    day = (_moscow_day(last) - timedelta(days=1)).isoformat()
    f["published_after"] = day
    log(f"since: auto -> {day}")
    return day


# не окончательные статусы: следующий поиск снова поставит номер в очередь и спросит карточку.
# no_deadline — в карточке не было даты; too_early — до окончания подачи больше deadline_to (рано)
RETRYABLE_STATUSES = ("no_deadline", "too_early")
SKIP_KEYS = ("expired", "too_late", "too_early", "no_deadline")


def _empty_skipped():
    return {k: [] for k in SKIP_KEYS}


def _due_too_early(reg, f):
    # too_early из реестра, чей срок уже не позже deadline_to: поиск с since auto их больше
    # может не вернуть (публикация старая), поэтому берём запись очереди из реестра
    d_to = f.get("deadline_to")
    out = []
    for num, entry in (reg.get("tenders") or {}).items():
        if not (isinstance(entry, dict) and entry.get("status") == "too_early"):
            continue
        rec = entry.get("rec")
        if not isinstance(rec, dict) or not rec.get("purchase_number"):
            continue
        try:
            m = deadline_moment(entry.get("deadline") or rec.get("collecting_finished_at"))
        except ValueError:
            m = None
        if m is None or not d_to or moment_le(m, d_to):
            out.append(rec)
    return out


def _fresh_candidates(all_norm, reg, f=None):
    known = {
        num for num, entry in (reg.get("tenders") or {}).items()
        if not (isinstance(entry, dict) and entry.get("status") in RETRYABLE_STATUSES)
    }
    fresh = [
        r for r in all_norm
        if r.get("purchase_number") and r["purchase_number"] not in known
    ]
    if f is not None:
        have = {r["purchase_number"] for r in fresh}
        for r in _due_too_early(reg, f):
            if r["purchase_number"] not in have:
                fresh.append(r)
                have.add(r["purchase_number"])
    fresh.sort(key=lambda r: (str(r.get("published_at") or ""), str(r.get("purchase_number") or "")))
    return fresh


def _remote_root(remote):
    return str(remote).strip().rstrip("/")


def _stderr_tail(text, n=8):
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-n:]


def rclone_lsf_dirs(binary, remote, log):
    # один раз за прогон: имена папок уже лежащих на приёмнике
    cmd = [binary, "lsf", _remote_root(remote) + "/", "--dirs-only"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"rclone lsf не выполнился: {e}")
        return None
    err_text = (proc.stderr or "").lower()
    # код 3 — каталога на приёмнике ещё нет. copy создаст его сам
    if proc.returncode == 3 or "directory not found" in err_text:
        log("rclone lsf: каталога на приёмнике нет, список папок пуст")
        return set()
    if proc.returncode != 0:
        log(f"rclone lsf: код {proc.returncode}")
        for line in _stderr_tail(proc.stderr or proc.stdout):
            log(line)
        return None
    names = set()
    for line in (proc.stdout or "").splitlines():
        name = line.strip().strip("/")
        if not name:
            continue
        names.add(name.split("/")[0])
    return names


def rclone_copy_dir(binary, src, remote, number, log):
    dst = _remote_root(remote) + "/" + number
    cmd = [binary, "copy", str(src), dst]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"{number}: rclone copy не выполнился: {e}")
        return False
    if proc.returncode != 0:
        log(f"{number}: rclone copy код {proc.returncode}")
        for line in _stderr_tail(proc.stderr):
            log(line)
        return False
    return True


def _skip_by_window(reg, num, rec, dl, deadline_raw, city_key, now, f, log):
    # expired и too_late — окончательно (статус expired); too_early и no_deadline — вернутся
    reason = window_reason(dl, f)
    if dl == "too_early":
        log(f"{num}: {reason} ({deadline_raw}), не выгружаю — вернётся в очередь")
        entry = {"status": "too_early", "city": city_key, "at": now, "deadline": deadline_raw,
                 "rec": {**rec, "collecting_finished_at": deadline_raw}}
    elif dl == "no_deadline":
        log(f"{num}: {reason} — пропуск, вернётся при следующем поиске")
        entry = {"status": "no_deadline", "city": city_key, "at": now, "deadline": None}
    else:
        log(f"{num}: {reason} ({deadline_raw}) — пропуск")
        entry = {"status": "expired", "city": city_key, "at": now, "deadline": deadline_raw}
        if dl == "too_late":
            entry["reason"] = "too_late"
    reg["tenders"][num] = entry


def drain_pending(reg, reg_path, f, base_url, api_key, timeout, pause_ms, log, limit=0, strict=False,
                  remote=None, rclone="rclone", state_dir=None):
    # очередь pending. лимит — сколько папок записать за этот запуск. 0 = все
    # remote: файлы во временной папке, на приёмник — rclone copy. без remote — как раньше
    remote_names = None
    tmp_root = None
    if remote:
        if reg.get("pending"):
            remote_names = rclone_lsf_dirs(rclone, remote, log)
            if remote_names is None:
                log("rclone lsf не удался, выгрузка не начата")
                log(f"pending: осталось {len(reg.get('pending') or [])}")
                return 0, len(reg.get("pending") or []), True, 0, _empty_skipped()
        tmp_root = Path(state_dir or (HERE / "state")) / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
    else:
        export_dir = Path(reg["export_dir"])
        export_dir.mkdir(parents=True, exist_ok=True)
    cities = list(f.get("delivery_cities") or [])
    pending = list(reg.get("pending") or [])
    exported = 0
    fail_streak = 0
    stopped = False
    rclone_failures = 0
    skipped = _empty_skipped()
    idx = 0
    while idx < len(pending):
        if limit and limit > 0 and exported >= limit:
            break
        rec = pending[idx]
        num = rec.get("purchase_number")
        if not num:
            pending.pop(idx)
            reg["pending"] = pending
            save_registry(reg_path, reg)
            continue
        work = None
        try:
            detail = gp.fetch_detail(base_url, rec["source"], num, api_key, timeout, pause_ms, log)
            verdict = city_verdict(rec, detail, cities)
            label, city_key = city_label(verdict, cities)
            now = datetime.now(MSK).isoformat(timespec="seconds")
            # срок подачи: свежая карточка важнее записи очереди (очередь могла собраться вчера)
            deadline_raw = detail_deadline(detail) or rec.get("collecting_finished_at")
            dl = window_verdict(deadline_raw, f)
            if dl != "ok":
                _skip_by_window(reg, num, rec, dl, deadline_raw, city_key, now, f, log)
                skipped[dl].append(num)
                pending.pop(idx)
                reg["pending"] = pending
                save_registry(reg_path, reg)
                fail_streak = 0
                continue
            if strict and verdict is False:
                log(f"{num}: место поставки вне города ({', '.join(cities)}) — пропуск")
                reg["tenders"][num] = {"status": "outside", "city": city_key, "at": now}
                pending.pop(idx)
                reg["pending"] = pending
                save_registry(reg_path, reg)
                fail_streak = 0
                continue
            data = export_payload(rec, detail, base_url, cities)
            # последняя проверка срока прямо перед записью карточки и rclone copy
            dl = window_verdict(deadline_raw, f)
            if dl != "ok":
                _skip_by_window(reg, num, rec, dl, deadline_raw, city_key, now, f, log)
                skipped[dl].append(num)
                pending.pop(idx)
                reg["pending"] = pending
                save_registry(reg_path, reg)
                fail_streak = 0
                continue
            if remote:
                work = Path(tempfile.mkdtemp(dir=str(tmp_root)))
                folder = work / num
                folder.mkdir(parents=True, exist_ok=True)
            else:
                folder = export_dir / num
                folder.mkdir(parents=True, exist_ok=True)
            write_atomic(folder / "tender.json", json.dumps(data, ensure_ascii=False, indent=2))
            write_atomic(folder / "tender.md", format_tender_md(data, cities))
            if remote and num in remote_names:
                reg["tenders"][num] = {
                    "status": "exported", "dup": "remote", "city": city_key, "at": now,
                }
                pending.pop(idx)
                reg["pending"] = pending
                save_registry(reg_path, reg)
                fail_streak = 0
                log(f"{num}: уже есть на remote, без копирования")
                continue
            if remote and not rclone_copy_dir(rclone, folder, remote, num, log):
                rclone_failures += 1
                fail_streak += 1
                idx += 1
                log(f"{num}: rclone не записал, оставляю в очереди ({fail_streak} подряд)")
                if fail_streak >= 5:
                    stopped = True
                    log("5 сбоев подряд — стоп до следующего прогона")
                    break
                continue
            reg["tenders"][num] = {"status": "exported", "city": city_key, "at": now}
            pending.pop(idx)
            reg["pending"] = pending
            save_registry(reg_path, reg)
            exported += 1
            fail_streak = 0
            log(f"{num}: выгружено ({label})")
        except gp.ApiError as e:
            fail_streak += 1
            idx += 1
            log(f"{num}: ошибка API, оставляю в очереди ({fail_streak} подряд): {e}")
            if fail_streak >= 5:
                stopped = True
                log("5 сбоев API подряд — сервер лежит, стоп до следующего прогона")
                break
        except Exception as e:
            fail_streak += 1
            idx += 1
            log(f"{num}: сбой выгрузки, оставляю в очереди ({fail_streak} подряд): {e}")
            if fail_streak >= 5:
                stopped = True
                log("5 сбоев подряд — стоп до следующего прогона")
                break
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
    reg["pending"] = pending
    if not pending and not stopped:
        reg["last_completed_at"] = reg.get("pending_since") or datetime.now(MSK).isoformat(timespec="seconds")
        save_registry(reg_path, reg)
    if any(skipped[k] for k in SKIP_KEYS):
        log("пропущено по сроку подачи: " + ", ".join(
            f"{window_reason(k, f)} — {len(skipped[k])}" for k in SKIP_KEYS))
    log(f"pending: осталось {len(pending)}")
    return exported, len(pending), stopped, rclone_failures, skipped


def write_run_report(state_dir, report):
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    write_atomic(Path(state_dir) / "run_report.json", text)
    logs = HERE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    write_atomic(logs / "run_report.json", text)


def _apply_state_dir(cfg, cfg_path):
    # env важнее конфига; относительный путь — от файла конфига. иначе папка скрипта/state
    raw = os.environ.get("GOSPLAN_STATE_DIR") or (cfg.get("state_dir") if isinstance(cfg, dict) else None)
    if raw:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = Path(cfg_path).resolve().parent / path
    else:
        path = HERE / "state"
    path.mkdir(parents=True, exist_ok=True)
    gp.set_state_dir(path)
    return path


def main(argv=None):
    _relax_stdio()
    gp.reset_http_429()
    args = parse_args(argv)

    def log(msg):
        print(msg, file=sys.stderr)

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"конфиг не найден: {cfg_path}", file=sys.stderr)
        print("скопируй config.example.yaml в config.yaml и заполни фильтры", file=sys.stderr)
        return 2
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8-sig")) or {}
    state_dir = _apply_state_dir(cfg, cfg_path)
    f = dict(cfg.get("filters") or {})
    lim = cfg.get("limits") or {}
    base_url = (cfg.get("base_url") or "https://v2test.gosplan.info").rstrip("/")
    api_key = cfg.get("api_key") or None
    sources = list(cfg.get("sources") or ["fz44"])
    try:
        resolve_run_options(cfg, args, cfg_path)
        f, sources = apply_cli_filters(f, sources, args)
        f, sources, skipped_sources = apply_purchase_kinds(cfg, f, sources, args.kind, cfg_path)
        resolve_now_bounds(f)
        for key in ("deadline_from", "deadline_to"):
            parse_moment(f.get(key))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    page_size = min(int(lim.get("page_size", 100)), 100)
    max_pages = int(lim.get("max_pages_per_source", 50))
    pause_ms = int(lim.get("pause_ms", 600))
    timeout = int(lim.get("timeout_sec", 30))

    for src in skipped_sources:
        log(f"[{src}] кодов purchase_type для выбранных видов нет — источник пропущен, запрос без фильтра не шлём")
    if args.kind and not sources:
        kinds = ", ".join(args.kind or [])
        print(
            f"нет кодов purchase_type для видов: {kinds} (конфиг {cfg_path}); запрос без фильтра не отправляю",
            file=sys.stderr,
        )
        return 2

    reg = None
    reg_path = None
    exporting = bool(args.export or args.remote)
    if exporting:
        if args.remote:
            reg_path = registry_path(state_dir, remote=args.remote)
            reg = load_registry(reg_path, args.export, remote=args.remote)
        else:
            reg_path = registry_path(state_dir, args.export)
            reg = load_registry(reg_path, args.export)
        if args.export and not args.remote and not args.dry_run and migrate_legacy_state(args.export, reg, log):
            save_registry(reg_path, reg)
    since_effective = finalize_since(f, args, reg, log)
    drain_only = bool(exporting and not args.dry_run and reg.get("pending"))
    search_started = None if drain_only else datetime.now(MSK).isoformat(timespec="seconds")
    if drain_only:
        log("pending не пуст — поиск пропущен, сначала добор")

    all_norm = []
    stats = {}
    if drain_only:
        sources = []
    for src in sources:
        if args.kind:
            codes = (f.get("purchase_types") or {}).get(src) or []
            if not codes:
                log(f"[{src}] кодов purchase_type для выбранных видов нет — источник пропущен, запрос без фильтра не шлём")
                continue
        params = build_params(f, api_filters=True, source=src)
        if args.dry_run:
            log(f"[{src}] {base_url}/{src}/purchases?{urllib.parse.urlencode(params, doseq=True)}")
            continue
        # срыв одного источника не должен терять уже вытянутые записи другого
        try:
            raw = fetch_source(base_url, src, params, api_key, page_size, max_pages, pause_ms, timeout, log)
        except Exception as e:
            log(f"[{src}] сорвался после ретраев: {e}; продолжаю без него, доберёт следующий прогон")
            stats[src] = 0
            continue

        stats[src] = len(raw)
        log(f"[{src}] получено записей: {len(raw)}")
        allowed_types = _codes_for_source(f, src)
        dropped_pt = 0
        dropped_dl = 0
        no_dl = 0
        for item in raw:
            rec = norm_record(item, src, base_url)
            if allowed_types is not None and rec.get("purchase_type") not in allowed_types:
                dropped_pt += 1
                continue
            if f.get("keywords") and not keyword_ok(rec["object_info"], f["keywords"], f.get("keywords_all", False)):
                continue
            # список: город режем в --collect и в --export --city-strict.
            # мягкий --export город не отбрасывает — пометка попадает в tender.json
            if ((not exporting) or args.city_strict) and f.get("delivery_cities") and rec["delivery_places"]:
                if not any(gp.city_match(p, c) for c in f["delivery_cities"] for p in rec["delivery_places"]):
                    continue
            if not client_deadline_ok(rec, f, allow_missing=True):
                dropped_dl += 1
                continue
            if not client_since_ok(rec, f):
                continue
            if not rec.get("collecting_finished_at"):
                # дату окончания проверит карточка (в выгрузке и --collect); без неё не выгружаем
                rec["deadline_missing"] = True
                no_dl += 1
            all_norm.append(rec)
        if dropped_pt:
            log(f"[{src}] отсеяно по purchase_type: {dropped_pt}")
        if dropped_dl:
            log(f"[{src}] отсеяно по окончанию подачи: {dropped_dl}")
        if no_dl:
            log(f"[{src}] без даты окончания подачи в списке: {no_dl} — проверка по карточке перед выгрузкой")
        if pause_ms:
            time.sleep(pause_ms / 1000.0)

    if args.dry_run:
        print_summary(0, 0, 0)
        return 0

    def log_interval():
        if gp.interval_was_raised():
            log(f"интервал запросов повышен до {gp.current_interval():g} с")

    if exporting:
        if drain_only:
            found, new_n = 0, 0
        else:
            fresh = _fresh_candidates(all_norm, reg, f)
            found, new_n = len(all_norm), len(fresh)
            reg["pending"] = fresh
            reg["pending_since"] = search_started
            save_registry(reg_path, reg)
        exported, pending_left, stopped, rclone_failures, skipped_dl = drain_pending(
            reg, reg_path, f, base_url, api_key, timeout, pause_ms, log,
            limit=args.limit or 0, strict=bool(args.city_strict),
            remote=args.remote, rclone=args.rclone, state_dir=state_dir,
        )
        completed = pending_left == 0 and not stopped
        exit_code = 0 if completed else 3
        if exit_code == 3:
            log("Выгрузка не закончена, продолжится при следующем запуске")
        report = {
            "date": datetime.now().astimezone().isoformat(timespec="seconds"),
            "export_dir": str(Path(args.export).resolve()) if args.export else "",
            "remote": args.remote or None,
            "since_effective": since_effective,
            "filters": json.loads(json.dumps(f, ensure_ascii=False, default=str)),
            "found": found,
            "new": new_n,
            "exported": exported,
            "pending_left": pending_left,
            "http_429": gp.http_429_count(),
            "rclone_failures": rclone_failures,
            # отказы по сроку подачи за этот прогон. expired — срок прошёл, too_late — меньше
            # нижней границы окна (оба окончательно), too_early — больше верхней (вернётся), no_deadline
            "expired": len(skipped_dl["expired"]),
            "too_late": len(skipped_dl["too_late"]),
            "too_early": len(skipped_dl["too_early"]),
            "no_deadline": len(skipped_dl["no_deadline"]),
            "skipped_deadline": [
                {"number": n, "reason": window_reason(k, f), "kind": k}
                for k in SKIP_KEYS for n in skipped_dl[k]
            ],
            "completed": completed,
            "exit_code": exit_code,
        }
        write_run_report(state_dir, report)
        log_interval()
        print_summary(found, new_n, exported)
        return exit_code

    found = len(all_norm)

    # ниже — прежний режим: seen.json + run_*.json/csv + опционально --collect
    out_dir = Path(cfg.get("output", {}).get("dir") or "tenders")
    if not out_dir.is_absolute():
        out_dir = HERE / out_dir
    out_dir.mkdir(exist_ok=True)
    seen_path = out_dir / "seen.json"

    # дедуп: «обработано» = у записи есть папка-комплект (или она в карантине «вне города»).
    # записи seen без папки — недобор прошлых прогонов (лимит collect), их добираем
    seen = load_seen(seen_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new, skipped = [], 0
    for rec in all_norm:
        key = rec["purchase_number"]
        if not key:
            skipped += 1
            continue
        old = seen.get(key)
        if old and (old.get("folder") or old.get("city_check") == "outside"
                    or old.get("deadline_check") == "expired"):
            skipped += 1
            continue
        if old is None:
            seen[key] = {"first_seen": now, "source": rec["source"]}
        new.append(rec)
    save_seen(seen_path, seen)

    # раскладка в na-proschyot: полный комплект для конвейера (как у панели)
    if args.collect > 0 and new:
        ready = out_dir / "с найденными"
        ready.mkdir(exist_ok=True)
        collected = 0
        fail_streak = 0
        for rec in new[: args.collect]:
            num = rec["purchase_number"]
            folder = ready / num
            if folder.exists():
                log(f"{num}: папка уже есть, пропуск")
                continue
            try:
                detail = gp.fetch_detail(base_url, rec["source"], num, api_key, timeout, pause_ms, log)
                deadline_raw = detail_deadline(detail) or rec.get("collecting_finished_at")
                dl = window_verdict(deadline_raw, f)
                if dl != "ok":
                    log(f"{num}: {window_reason(dl, f)} ({deadline_raw or '—'}) — пропуск")
                    # too_late — окончательно как expired; too_early и no_deadline — повтор следующим прогоном
                    seen[num]["deadline_check"] = "expired" if dl == "too_late" else dl
                    save_seen(seen_path, seen)
                    fail_streak = 0
                    continue
                verdict = gp.delivery_city_verdict(detail, f.get("delivery_cities") or [])
                if verdict is False:
                    log(f"{num}: место поставки вне города ({', '.join(f.get('delivery_cities') or [])}) — пропуск")
                    seen[num]["city_check"] = "outside"
                    save_seen(seen_path, seen)
                    continue
                seen[num]["city_check"] = "ok"
                docs_dir = folder / "документы"
                docs_dir.mkdir(parents=True, exist_ok=True)
                (folder / "tender.json").write_text(
                    json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
                (folder / "сводка.txt").write_text(gp.summary_text(rec, detail), encoding="utf-8")
                files = 0
                for i, att in enumerate(gp.extract_attachments(detail), 1):
                    try:
                        gp.download_attachment(att, docs_dir, i)
                        files += 1
                    except Exception as e:
                        log(f"{num}: файл не скачался ({att['fileName']}): {e}")
                seen[num]["folder"] = str(folder)
                seen[num]["files"] = files
                save_seen(seen_path, seen)
                collected += 1
                log(f"{num}: собрано, файлов {files}")
            except gp.ApiError as e:
                # сбой на одной закупке не должен хоронить весь срез; добор — следующим прогоном
                fail_streak += 1
                log(f"{num}: ошибка API, пропускаю ({fail_streak} подряд): {e}")
                if fail_streak >= 5:
                    log("5 сбоев API подряд — сервер лежит, стоп до следующего прогона")
                    break
                continue
            fail_streak = 0
        log(f"разложено в na-proschyot: {collected}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_id": run_id,
        "config": str(cfg_path),
        "base_url": base_url,
        "filters": f,
        "stats": {**stats, "after_client_keywords": len(all_norm),
                  "new": len(new), "skipped_dup_or_empty": skipped},
        "new_tenders": new,
    }
    out_json = out_dir / f"run_{run_id}.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    out_csv = out_dir / f"run_{run_id}.csv"
    with out_csv.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for rec in new:
            row = dict(rec)
            for k in ("okpd2", "customers"):
                row[k] = ";".join(map(str, rec[k]))
            w.writerow(row)

    cleanup_reports(out_dir, int(cfg.get("output", {}).get("keep_days", 90)))

    log(f"новых закупок: {len(new)}; пропущено (дубль/пусто): {skipped}")
    log(f"отчёт: {out_json}")
    log(f"таблица: {out_csv}")
    log_interval()
    print_summary(found, len(new), 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
