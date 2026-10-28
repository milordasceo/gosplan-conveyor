### ЧТО ДЕЛАЕТ: консольный сборщик закупок с API gosplan.info: скачивает карточки закупок по фильтрам
###   конфига, кладёт их в tenders\с найденными\<номер закупки>\ (сводка.txt, tender.json, документы\),
###   дедуплицирует по purchase_number через tenders\seen.json (идемпотентно: повторный прогон переносит 0).
### ЗАПУСК: python gosplan_scanner.py -c config.novosibirsk.yaml [--collect 20] [--dry-run]
### ЗАВИСИМОСТИ: python 3.10+, pip install pyyaml;.opensource (реестр файлов в ./). Сеть: доступ к gosplan.info и zakupki.gov.ru
###   (для zakupki нужен российский CA «Russian Trusted Root CA» — см. runbooks).
### ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ: нет (ключ API, если используется прод-сервер, задаётся в конфиге, поле api_key).
### ВХОД: yaml-конфиг (фильтры: ключевые слова, бюджет 30–150 тыс. руб., регион 54, stage 1,delivery_cities).
### ВЫХОД: tenders\с найденными\<номер>\* + tenders\run_<timestamp>.json|csv + консольная сводка.
#!/usr/bin/env python3
# разведка gosplan.info: выборка закупок по критериям, дедуп по номеру закупки.
# python >=3.9, зависимости: pyyaml. тестовый сервер gosplan.info работает без api-ключа.

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

import gosplan_lib as gp

HERE = Path(__file__).resolve().parent
UA = "gosplan-scanner/1.0 (local tender recon tool; +https://gosplan.info/docs)"

FIELDS = [
    "purchase_number", "source", "region", "max_price", "currency_code",
    "stage", "purchase_type", "object_info", "okpd2", "published_at",
    "collecting_finished_at", "customers", "delivery_places", "url",
]


def http_get(url, params=None, api_key=None, timeout=30):
    # троттлинг 10 req/min живёт в gosplan_lib.http_json (общий с панелью)
    return gp.http_json(url, params, api_key, timeout)


def norm_record(item, source):
    return {
        "purchase_number": item.get("purchase_number") or "",
        "source": source,
        "region": item.get("region"),
        "max_price": item.get("max_price"),
        "currency_code": item.get("currency_code"),
        "stage": item.get("stage"),
        "purchase_type": item.get("purchase_type"),
        "object_info": item.get("object_info"),
        "okpd2": item.get("okpd2") or [],
        "published_at": item.get("published_at"),
        "collecting_finished_at": item.get("collecting_finished_at"),
        "customers": item.get("customers") or [],
        "delivery_places": item.get("delivery_places") or [],
        "url": "https://zakupki.gov.ru/epic/order/extendedSearch/commonInfo.html?searchString="
               + urllib.parse.quote(item.get("purchase_number") or ""),
    }


def keyword_ok(text, keywords, require_all):
    if not keywords:
        return True
    hay = (text or "").lower()
    hits = [k for k in keywords if k.lower() in hay]
    return len(hits) == len(keywords) if require_all else bool(hits)


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
        p["max_price_ge"] = f["min_budget"]
    if f.get("max_budget") is not None:
        p["max_price_le"] = f["max_budget"]
    if f.get("published_after"):
        p["published_after"] = f["published_after"]
    if f.get("published_before"):
        p["published_before"] = f["published_before"]
    if f.get("regions"):
        p["region"] = f["regions"]
    if f.get("stage") is not None:
        p["stage"] = f["stage"]
    if f.get("currency"):
        p["currency_code"] = f["currency"]
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
        for attempt in range(net_retries + 1):
            try:
                batch = http_get(url, q, api_key, timeout)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 30
                    try:
                        wait = int(e.headers.get("Retry-After") or 30)
                    except (TypeError, ValueError):
                        pass
                    if attempt >= net_retries:
                        log(f"429 не прошёл за {net_retries} попыток (skip={skip})")
                        raise
                    log(f"429 rate limit, жду {wait} c и повторяю (skip={skip})")
                    time.sleep(wait)
                    continue
                log(f"HTTP {e.code} на {url}?skip={skip}: {e.read()[:200]}")
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt >= net_retries:
                    log(f"сеть не отвечает после {net_retries} попыток (skip={skip}): {e}")
                    raise
                wait = (attempt + 1) * 10
                log(f"сетевой сбой ({e}), жду {wait} с и повторяю (skip={skip})")
                time.sleep(wait)
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
        return json.loads(seen_path.read_text(encoding="utf-8"))
    return {}


def save_seen(seen_path, seen):
    tmp = seen_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(seen_path)


def cleanup_reports(out_dir, keep_days):
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    for p in out_dir.glob("run_*.json"):
        if p.stat().st_mtime < cutoff:
            p.unlink()


def main():
    ap = argparse.ArgumentParser(description="разведка закупок через API gosplan.info")
    ap.add_argument("-c", "--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="вывести параметры запросов и выйти")
    ap.add_argument("--collect", type=int, default=0, metavar="N",
                    help="скачать документы первых N новых закупок в tenders/na-proschyot")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"конфиг не найден: {cfg_path}", file=sys.stderr)
        print("скопируй config.example.yaml в config.yaml и заполни фильтры", file=sys.stderr)
        return 2
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    f = cfg.get("filters") or {}
    lim = cfg.get("limits") or {}
    base_url = (cfg.get("base_url") or "https://v2test.gosplan.info").rstrip("/")
    api_key = cfg.get("api_key") or None
    sources = cfg.get("sources") or ["fz44"]
    page_size = min(int(lim.get("page_size", 100)), 100)
    max_pages = int(lim.get("max_pages_per_source", 50))
    pause_ms = int(lim.get("pause_ms", 600))
    timeout = int(lim.get("timeout_sec", 30))
    out_dir = HERE / (cfg.get("output", {}).get("dir") or "tenders")
    out_dir.mkdir(exist_ok=True)
    seen_path = out_dir / "seen.json"

    def log(msg):
        print(msg, file=sys.stderr)

    all_norm = []
    stats = {}
    for src in sources:
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
        for item in raw:
            rec = norm_record(item, src)
            if f.get("keywords") and not keyword_ok(rec["object_info"], f["keywords"], f.get("keywords_all", False)):
                continue
            # город поставки: по спискам записей отсекаем сразу; пустые списки проверим по деталям при сборе
            if f.get("delivery_cities") and rec["delivery_places"]:
                if not any(gp.city_match(p, c) for c in f["delivery_cities"] for p in rec["delivery_places"]):
                    continue
            all_norm.append(rec)
        if pause_ms:
            time.sleep(pause_ms / 1000.0)

    if args.dry_run:
        return 0

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
        if old and (old.get("folder") or old.get("city_check") == "outside"):
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
        w = csv.DictWriter(fh, fieldnames=FIELDS)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())

