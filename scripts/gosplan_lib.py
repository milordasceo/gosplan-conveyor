### ЧТО ДЕЛАЕТ: общая библиотека сканера: HTTP-клиент gosplan.info (слот 10 с, flock, рост интервала на 429),
###   пагинация, вложения 44/223, ссылки еис, клиентская проверка города поставки, seen.json.
### ЗАПУСК: не запускается напрямую — импортирует gosplan_scanner.py / gosplan_panel.pyw.
### ЗАВИСИМОСТИ: python 3.10+, стандартная библиотека + pyyaml (конфиг читает вызывающий скрипт).
### ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ: нет.
### ВХОД/ВЫХОД: функции; выходы видны в поведении сканера (папки tenders\, дедуп seen.json).
#!/usr/bin/env python3
# общая логика разведки gosplan.info: http, поиск, детали закупки, раскладка по папкам.
# используется панелью (gosplan_panel.pyw) и доступно из cli.

import base64
import email.utils
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

HERE = Path(__file__).resolve().parent
UA = "gosplan-scanner/1.0 (local tender recon tool)"
CA_BUNDLE = HERE / "ca" / "russian_trusted_ca.pem"

# госплан: старт 10 с между запросами (6 в минуту с запасом). слот общий у панели и cli.
# на 429 интервал процесса растёт (×1.5, потолок 20 с) и публикуется в слот.
RATE_MIN_INTERVAL = 10.0
RATE_MAX_INTERVAL = 20.0
_SLOT = HERE / "gosplan_api.slot"
_LOCK = HERE / "gosplan_api.slot.lock"
_process_interval = RATE_MIN_INTERVAL
_interval_raised = False
_http_429 = 0

# коды purchase_type из openapi gosplan (enum списков закупок) → короткое русское имя
PURCHASE_TYPE_NAMES = {
    "epNotificationEF2020": "электронный аукцион",
    "epNotificationEZK2020": "запрос котировок в электронной форме",
    "epNotificationEZK": "запрос котировок в электронной форме (до 2020)",
    "epNotificationEOK2020": "открытый конкурс в электронной форме",
    "epNotificationEOK": "открытый конкурс в электронной форме (до 2020)",
    "epNotificationEOKD": "двухэтапный конкурс в электронной форме",
    "epNotificationEOKOU": "конкурс с ограниченным участием в электронной форме",
    "epNotificationEZP": "запрос предложений в электронной форме",
    "fcsNotification111": "закупка по ст. 111 44-фз (особенности)",
    "purchaseNotice": "закупка иным способом (223-фз)",
    "purchaseNoticeOK": "открытый конкурс",
    "purchaseNoticeOA": "открытый аукцион",
    "purchaseNoticeAE": "аукцион в электронной форме",
    "purchaseNoticeAE94FZ": "электронный аукцион (по 94-фз)",
    "purchaseNoticeAESMBO": "аукцион в электронной форме среди субъектов мсп",
    "purchaseNoticeKESMBO": "конкурс в электронной форме среди субъектов мсп",
    "purchaseNoticeZK": "запрос котировок",
    "purchaseNoticeZKESMBO": "запрос котировок в электронной форме среди субъектов мсп",
    "purchaseNoticeZPESMBO": "запрос предложений в электронной форме среди субъектов мсп",
    "purchaseNoticeEP": "закупка у единственного поставщика",
    "pprf615NotificationPO": "предварительный отбор (пп рф 615)",
    "pprf615NotificationEF": "электронный аукцион (пп рф 615)",
}

_page_size = 100
_skip_max = 1000  # API ограничивает skip<=1000


def current_interval():
    # текущий интервал этого процесса (после 429 может быть выше базового)
    return float(_process_interval)


def interval_was_raised():
    return bool(_interval_raised)


def purchase_type_name(code):
    if not code:
        return None
    return PURCHASE_TYPE_NAMES.get(code)


def purchase_type_label(code):
    # «код (имя)» или просто код, если имени нет
    if not code:
        return "—"
    name = PURCHASE_TYPE_NAMES.get(code)
    if not name:
        return str(code)
    return f"{code} ({name})"


def _empty_slot():
    return {"next_ts": 0.0, "interval": 0.0, "penalty_until": 0.0}


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_slot():
    # json нового формата, старый голый float или мусор
    try:
        raw = _SLOT.read_text(encoding="utf-8").strip()
    except OSError:
        return _empty_slot()
    if not raw:
        return _empty_slot()
    if raw[0] in "{[":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return _empty_slot()
        if not isinstance(data, dict):
            return _empty_slot()
        return {
            "next_ts": _as_float(data.get("next_ts")),
            "interval": _as_float(data.get("interval")),
            "penalty_until": _as_float(data.get("penalty_until")),
        }
    try:
        return {"next_ts": float(raw), "interval": 0.0, "penalty_until": 0.0}
    except ValueError:
        return _empty_slot()


def set_state_dir(path):
    # слот и лок api: по умолчанию папка скрипта, иначе GOSPLAN_STATE_DIR / state_dir
    global _SLOT, _LOCK
    path = Path(path)
    _SLOT = path / "gosplan_api.slot"
    _LOCK = path / "gosplan_api.slot.lock"


def atomic_replace(src, dst):
    # google drive на windows иногда держит цель — несколько попыток replace
    src, dst = Path(src), Path(dst)
    last = None
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            last = e
            if attempt < 4:
                time.sleep(0.5)
    raise last


def _write_slot(slot):
    payload = json.dumps({
        "next_ts": _as_float(slot.get("next_ts")),
        "interval": _as_float(slot.get("interval")),
        "penalty_until": _as_float(slot.get("penalty_until")),
    }, ensure_ascii=False)
    try:
        tmp = _SLOT.with_name(_SLOT.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        atomic_replace(tmp, _SLOT)
    except OSError:
        try:
            _SLOT.write_text(payload, encoding="utf-8")
        except OSError:
            pass


def _effective_interval(slot, now):
    # пока штраф жив, берём максимум своего интервала и опубликованного
    own = float(_process_interval)
    if now < _as_float(slot.get("penalty_until")):
        return max(own, _as_float(slot.get("interval")))
    return own


def _close_lock(fh):
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        fh.close()


def _open_lock_once():
    # flock на posix, msvcrt.locking на 1 байт на windows. файл лока не удаляем
    fh = open(_LOCK, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            return fh
        if msvcrt is None:
            return fh
        fh.seek(0, 2)
        if fh.tell() < 1:
            fh.write("\0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        return fh
    except OSError:
        fh.close()
        raise


def _open_lock():
    # windows иногда отвечает oserror, пока файл ещё пустой — короткая пауза и повтор
    last = None
    for attempt in range(40):
        try:
            return _open_lock_once()
        except OSError as e:
            last = e
            time.sleep(0.05)
    raise last


def _slot_critical(update_fn):
    # блокировка только на чтение/запись слота; спать и ходить в сеть — снаружи
    fh = _open_lock()
    try:
        slot = _read_slot()
        new_slot = update_fn(slot)
        if new_slot is not None:
            _write_slot(new_slot)
        return new_slot if new_slot is not None else slot
    finally:
        _close_lock(fh)


def _throttle_api():
    while True:
        wait_box = [None]

        def decide(slot, wait_box=wait_box):
            now = time.time()
            if now >= _as_float(slot.get("next_ts")):
                slot["next_ts"] = now + _effective_interval(slot, now)
                wait_box[0] = 0
                return slot
            wait_box[0] = min(_as_float(slot.get("next_ts")) - now, 2.0)
            return None

        try:
            _slot_critical(decide)
        except OSError:
            return  # слот недоступен — лучше один лишний запрос, чем вечное ожидание
        if wait_box[0] == 0:
            return
        time.sleep(0.5 if wait_box[0] is None else max(wait_box[0], 0.0))


def _retry_after_seconds(headers, fallback):
    # Retry-After: целое число секунд или http-date; иначе fallback
    raw = None
    if headers is not None:
        try:
            raw = headers.get("Retry-After")
        except Exception:
            raw = None
    if raw is None or str(raw).strip() == "":
        return fallback, None
    text = str(raw).strip()
    try:
        return max(0, int(text)), text
    except ValueError:
        pass
    try:
        return max(0.0, float(text)), text
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError, IndexError):
        dt = None
    if dt is None:
        return fallback, text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, dt.timestamp() - time.time()), text


def http_429_count():
    return int(_http_429)


def reset_http_429():
    global _http_429
    _http_429 = 0


def _note_429(headers, log):
    # интервал процесса ×1.5 (потолок 20) и публикация штрафа в общий слот
    global _process_interval, _interval_raised, _http_429
    _http_429 += 1
    old = float(_process_interval)
    new = min(RATE_MAX_INTERVAL, old * 1.5)
    _process_interval = new
    _interval_raised = True
    wait, raw = _retry_after_seconds(headers, new * 2)

    def update(slot, new=new, wait=wait):
        now = time.time()
        slot["interval"] = max(_as_float(slot.get("interval")), new)
        slot["penalty_until"] = now + 600.0
        slot["next_ts"] = max(_as_float(slot.get("next_ts")), now + float(wait))
        return slot

    try:
        _slot_critical(update)
    except OSError:
        pass
    shown = raw if raw is not None else "нет"
    log(f"429: интервал {old:g} -> {new:g} с, пауза {wait:g} с (Retry-After={shown})")
    return wait


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


def _http_error_body(exc):
    try:
        body = exc.read()
    except Exception:
        return b""
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    return body[:200]


def get_with_backoff(url, params, api_key, timeout, log, net_retries=0, net_note=""):
    # 429 госплана — до 4 повторов с ростом интервала, потом ApiError.
    # net_retries > 0 — повторы сетевых сбоев списка (10/20/30 с); 0 — URLError сразу в ApiError.
    # net_note — хвост лога списка, например «skip=100».
    rate_left = 4
    net_left = net_retries
    while True:
        try:
            return http_json(url, params, api_key, timeout)
        except urllib.error.HTTPError as e:
            if e.code == 429 and rate_left > 0:
                wait = _note_429(e.headers, log)
                time.sleep(wait)
                rate_left -= 1
                continue
            if e.code == 429:
                raise ApiError("429 не прошёл за 4 попытки")
            raise ApiError(f"HTTP {e.code}: {_http_error_body(e)}")
        except urllib.error.URLError as e:
            if not _retry_net(e, net_retries, net_left, log, net_note):
                if net_retries == 0:
                    raise ApiError(f"сеть не отвечает: {e.reason}")
                raise
            net_left -= 1
        except (TimeoutError, OSError) as e:
            if not _retry_net(e, net_retries, net_left, log, net_note):
                raise
            net_left -= 1


def _retry_net(exc, net_retries, net_left, log, net_note=""):
    # true = поспали и можно повторить; false = попытки кончились
    suffix = f" ({net_note})" if net_note else ""
    if net_left <= 0:
        if net_retries > 0:
            log(f"сеть не отвечает после {net_retries} попыток{suffix}: {exc}")
        return False
    done = net_retries - net_left  # 0, 1, 2 → пауза 10, 20, 30
    wait = (done + 1) * 10
    log(f"сетевой сбой ({exc}), жду {wait} с и повторяю{suffix}")
    time.sleep(wait)
    return True


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


def zakupki_search_url(number):
    # универсальная ссылка поиска на еис (не /epic/)
    return (
        "https://zakupki.gov.ru/epz/order/extendedSearch/results.html?searchString="
        + urllib.parse.quote(str(number or ""))
    )


_FZ44_CARD = {
    "epNotificationEF2020": "ea20",
    "epNotificationEZK2020": "zk20",
}


def extract_notice_info_id(data):
    # noticeInfoId из urlEIS 223-фз (лк) — для публичной карточки
    if isinstance(data, str) and data:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(data).query)
        ids = q.get("noticeInfoId") or []
        return ids[0] if ids else None
    if not isinstance(data, dict):
        return None
    for key in ("urlEIS", "url_eis"):
        found = extract_notice_info_id(data.get(key) or "")
        if found:
            return found
    for doc in data.get("docs") or []:
        src = (doc or {}).get("source") or {}
        found = extract_notice_info_id(src.get("urlEIS") or src.get("url_eis") or "")
        if found:
            return found
    return None


def zakupki_card_url(source, purchase_type, number, notice_info_id=None):
    # прямая карточка еис, если тип/id известны; иначе None (остаётся поиск)
    if source == "fz44":
        slug = _FZ44_CARD.get(purchase_type or "")
        if slug and number:
            return (
                f"https://zakupki.gov.ru/epz/order/notice/{slug}/view/common-info.html"
                f"?regNumber={urllib.parse.quote(str(number))}"
            )
        return None
    if source == "fz223":
        if not notice_info_id:
            return None
        return (
            "https://zakupki.gov.ru/epz/order/notice/notice223/common-info.html"
            f"?noticeInfoId={urllib.parse.quote(str(notice_info_id))}"
        )
    return None


def gosplan_detail_url(base_url, source, number):
    # публичной html-карточки у gosplan.info нет — это url api детали
    return f"{(base_url or '').rstrip('/')}/{source}/purchases/{number}"


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def extract_attachments(detail):
    # вложения 44-фз (attachmentsInfo.attachmentInfo) и 223-фз (attachments.document),
    # плюс печатные формы извещения: [{url, fileName, description, size}]
    out, seen = [], set()

    def add(url, name, description, size):
        u = (url or "").strip()
        if not u or u in seen:
            return
        seen.add(u)
        out.append({
            "url": u,
            "fileName": name or "attachment.bin",
            "description": description or "",
            "size": size,
        })

    for doc in detail.get("docs") or []:
        src = (doc or {}).get("source") or {}

        ai = src.get("attachmentsInfo")
        items = []
        if isinstance(ai, dict):
            items = ai.get("attachmentInfo") or []
        elif isinstance(ai, list):
            items = ai
        for a in _as_list(items):
            if not isinstance(a, dict):
                continue
            add(
                a.get("url"),
                a.get("fileName"),
                a.get("docDescription") or a.get("description") or "",
                a.get("fileSize") if a.get("fileSize") is not None else a.get("size"),
            )

        att = src.get("attachments")
        docs_items = []
        if isinstance(att, dict):
            docs_items = att.get("document") or att.get("documents") or []
        elif isinstance(att, list):
            docs_items = att
        for a in _as_list(docs_items):
            if not isinstance(a, dict):
                continue
            add(
                a.get("url"),
                a.get("fileName"),
                a.get("description") or a.get("docDescription") or "",
                a.get("fileSize") if a.get("fileSize") is not None else a.get("size"),
            )

        pfi = src.get("printFormInfo") or src.get("printForm")
        pf_url = ""
        if isinstance(pfi, str):
            pf_url = pfi
        elif isinstance(pfi, dict):
            pf_url = pfi.get("url") or ""
        if pf_url:
            add(pf_url, "Печатная форма извещения", "печатная форма извещения", None)

    return out


def download_attachment(att, dst_dir, index):
    global _http_429
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
                if e.code == 429:
                    _http_429 += 1
                time.sleep(5 + attempt * 10)
                continue
            raise
    raise last


def summary_text(rec, detail):
    lines = [
        f"Закупка № {rec['purchase_number']}",
        f"Закон: {rec['source'].upper()}",
        f"Тип: {purchase_type_label(rec.get('purchase_type'))}",
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
    lines.append(f"Поиск на zakupki: {zakupki_search_url(rec['purchase_number'])}")
    card = rec.get("zakupki_card") or zakupki_card_url(
        rec.get("source"), rec.get("purchase_type"), rec.get("purchase_number"),
        extract_notice_info_id(detail),
    )
    if card:
        lines.append(f"Карточка на zakupki: {card}")
    return "\n".join(lines)


def load_seen(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return {}


def save_seen(path, seen):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    atomic_replace(tmp, path)


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

