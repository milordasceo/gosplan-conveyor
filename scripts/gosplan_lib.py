### ЧТО ДЕЛАЕТ: общая библиотека сканера: HTTP-клиент gosplan.info (rate limit, Retry-After), пагинация,
###   клиентская проверка города поставки (GAR/ОКТМО vs delivery_cities из конфига), чтение/запись seen.json.
### ЗАПУСК: не запускается напрямую — импортирует gosplan_scanner.py / gosplan_panel.pyw.
### ЗАВИСИМОСТИ: python 3.10+, стандартная библиотека + pyyaml (конфиг читает вызывающий скрипт).
### ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ: нет.
### ВХОД/ВЫХОД: функции; выходы видны в поведении сканера (папки tenders\, дедуп seen.json).
#!/usr/bin/env python3
# общая логика разведки gosplan.info: http, поиск, детали закупки, раскладка по папкам.
# используется панелью (gosplan_panel.pyw) и доступно из cli.

import base64
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = "gosplan-scanner/1.0 (local tender recon tool)"
CA_BUNDLE = HERE / "ca" / "russian_trusted_ca.pem"

# госплан: 10 запросов/мин на клиента → не чаще одного запроса в 6.5 с (запас 0.5 с);
# лимитер межпроцессный (файл-слот), чтобы панель и cli вместе не превышали лимит
RATE_MIN_INTERVAL = 6.5
_SLOT = HERE / "gosplan_api.slot"
_LOCK = HERE / "gosplan_api.slot.lock"

_page_size = 100
_skip_max = 1000  # API ограничивает skip<=1000


def _throttle_api():
    while True:
        lock = None
        wait = 0.5
        try:
            lock = open(_LOCK, "x")
        except FileExistsError:
            try:
                if time.time() - _LOCK.stat().st_mtime > 60:  # протухший лок от упавшего процесса
                    _LOCK.unlink()
            except OSError:
                pass
            time.sleep(wait)
            continue
        try:
            try:
                next_ts = float(_SLOT.read_text().strip() or 0)
            except (OSError, ValueError):
                next_ts = 0.0
            now = time.time()
            if now >= next_ts:
                try:
                    _SLOT.write_text(f"{now + RATE_MIN_INTERVAL:.3f}")
                    return
                except OSError:
                    return  # слот недоступен — лучше один лишний запрос, чем вечное ожидание
            wait = min(next_ts - now, 2.0)
        finally:
            if lock:
                lock.close()
                try:
                    _LOCK.unlink()
                except OSError:
                    pass
        time.sleep(wait)


def _ctx_zakupki():
    # zakupki.gov.ru отдаёт цепочку Russian Trusted Root CA — верифицируем своим бандлом
    return ssl.create_default_context(cafile=str(CA_BUNDLE))


def http_json(url, params=None, api_key=None, timeout=30):
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    # троттлинг только для API госплана; zakupki (другой хост) имеет свои лимиты
    if "gosplan" in urllib.parse.urlsplit(url).netloc:
        _throttle_api()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    if api_key:
        req.add_header("X-Api-Key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get_raw(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx_zakupki()) as r:
        return r.read(), r.headers.get("Content-Type", "")


class ApiError(RuntimeError):
    pass


def get_with_backoff(url, params, api_key, timeout, log):
    # до трёх пауз на Retry-After; дальше отдаём наверх
    for attempt in range(4):
        try:
            return http_json(url, params, api_key, timeout)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                try:
                    wait = int(e.headers.get("Retry-After") or 30)
                except (TypeError, ValueError):
                    wait = 30
                log(f"429 rate limit, пауза {wait} c")
                time.sleep(wait)
                continue
            raise ApiError(f"HTTP {e.code}: {e.read()[:200]}")
        except urllib.error.URLError as e:
            raise ApiError(f"сеть не отвечает: {e.reason}")
    raise ApiError("429 не прошёл за 3 попытки")


def iter_purchases(base_url, source, params, api_key, timeout, pause_ms, log, max_pages=50):
    # генератор по страницам: отдаёт записи, соблюдает лимиты и паузы
    skip = 0
    for _ in range(max_pages):
        q = dict(params)
        q["limit"] = _page_size
        q["skip"] = skip
        batch = get_with_backoff(f"{base_url}/{source}/purchases", q, api_key, timeout, log)
        if not isinstance(batch, list):
            raise ApiError(f"неожиданный ответ API: {str(batch)[:200]}")
        yield from batch
        if len(batch) < _page_size:
            return
        skip += _page_size
        if skip > _skip_max:
            log("skip достиг максимума API (1000), сузь фильтры")
            return
        time.sleep(pause_ms / 1000.0)


def fetch_detail(base_url, source, purchase_number, api_key, timeout, pause_ms, log):
    d = get_with_backoff(f"{base_url}/{source}/purchases/{purchase_number}", None, api_key, timeout, log)
    time.sleep(pause_ms / 1000.0)
    return d


def extract_attachments(detail):
    # все вложения из всех документов закупки: [{url, fileName, description, size}]
    out, seen = [], set()
    for doc in detail.get("docs") or []:
        src = (doc or {}).get("source") or {}
        ai = src.get("attachmentsInfo")
        items = []
        if isinstance(ai, dict):
            items = ai.get("attachmentInfo") or []
        elif isinstance(ai, list):
            items = ai
        for a in items:
            u = a.get("url") or ""
            if not u or u in seen:
                continue
            seen.add(u)
            out.append({
                "url": u,
                "fileName": a.get("fileName") or "attachment.bin",
                "description": a.get("docDescription") or "",
                "size": a.get("fileSize"),
            })
    return out


def download_attachment(att, dst_dir, index):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", att["fileName"]).strip() or f"file{index}"
    name = f"{index:02d}_{safe}" if not safe[0].isdigit() else f"{index:02d}_{safe}"
    dst = dst_dir / name
    last = None
    for attempt in range(3):  # zakupki иногда отвечает 429/503 — два ретрая с паузой
        try:
            data, ctype = http_get_raw(att["url"])
            dst.write_bytes(data)
            return dst.name, len(data)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 503) and attempt < 2:
                time.sleep(5 + attempt * 10)
                continue
            raise
    raise last


def summary_text(rec, detail):
    lines = [
        f"Закупка № {rec['purchase_number']}",
        f"Закон: {rec['source'].upper()}",
        f"Объект: {rec.get('object_info') or '—'}",
        f"Начальная цена: {rec.get('max_price') or '—'} {rec.get('currency_code') or ''}",
        f"Этап: {rec.get('stage')} (1=подача заявок)",
        f"Опубликовано: {rec.get('published_at') or '—'}",
        f"Окончание подачи заявок: {rec.get('collecting_finished_at') or '—'}",
        f"Регион: {rec.get('region')}",
        f"ОКПД2: {', '.join(rec.get('okpd2') or []) or '—'}",
        f"Заказчики (ИНН): {', '.join(rec.get('customers') or []) or '—'}",
        f"Обеспечение контракта: {detail.get('contract_guarantee_amount') or '—'}",
    ]
    pf = None
    for doc in detail.get("docs") or []:
        src = (doc or {}).get("source") or {}
        p = src.get("printFormInfo") or {}
        if p.get("url"):
            pf = p["url"]
            break
    if pf:
        lines.append(f"Печатная форма: {pf}")
    lines.append(f"Карточка на zakupki: https://zakupki.gov.ru/epz/order/extendedSearch/commonInfo.html?searchString={rec['purchase_number']}")
    return "\n".join(lines)


def load_seen(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_seen(path, seen):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --- фильтр по городу поставки ---
# серверного параметра «город» у API нет, поэтому проверяем клиентски по месту доставки:
# верхние delivery_places и блоки deliveryPlaceInfo/deliveryPlacesInfo в документах
_PLACE_KEYS = ({"deliveryPlaceInfo", "deliveryPlacesInfo", "deliveryPlaces"})


def _collect_delivery(o, out):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "OKTMO" and isinstance(v, dict) and v.get("name"):
                out.append(v["name"])
            elif k == "GARInfo" and isinstance(v, dict) and v.get("GARAddress"):
                out.append(v["GARAddress"])
            elif k == "deliveryPlace" and isinstance(v, str) and v.strip():
                out.append(v)
            elif isinstance(v, (dict, list)):
                _collect_delivery(v, out)
    elif isinstance(o, list):
        for v in o:
            _collect_delivery(v, out)


def extract_delivery_texts(data):
    # все тексты мест поставки закупки, уникальные
    out = []
    if isinstance(data, dict):
        out.extend(str(p) for p in (data.get("delivery_places") or []) if p)
        walk_place_block(data, out)
    seen, uniq = set(), []
    for t in out:
        t = re.sub(r"\s+", " ", t).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return uniq


def walk_place_block(o, out):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in _PLACE_KEYS:
                _collect_delivery(v, out)
            elif isinstance(v, (dict, list)):
                walk_place_block(v, out)
    elif isinstance(o, list):
        for v in o:
            walk_place_block(v, out)


def city_match(text, city):
    # город именно как поселение: «город Новосибирск», «г. Новосибирск», KLADR «Новосибирск г».
    # «Новосибирская область»/«Новосибирский район» не город — \b отсекает
    t = (text or "").lower()
    c = re.escape(city.lower())
    return bool(
        re.search(rf"\bгород\s+{c}\b", t)
        or re.search(rf"\b{c}\s+г\b", t)
        or re.search(rf"\bг\.?\s*{c}\b", t)
    )


def delivery_city_verdict(data, cities):
    # None — данных о месте поставки нет; True — есть место в одном из городов;
    # False — места есть и ни один не в городе (в т.ч. все вне города)
    texts = extract_delivery_texts(data)
    if not texts:
        return None
    for t in texts:
        for c in cities or []:
            if city_match(t, c):
                return True
    return False


def keyword_ok(text, keywords, require_all):
    if not keywords:
        return True
    hay = (text or "").lower()
    hits = [k for k in keywords if k.lower() in hay]
    return len(hits) == len(keywords) if require_all else bool(hits)

