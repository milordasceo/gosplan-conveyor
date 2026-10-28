### ЧТО ДЕЛАЕТ: идемпотентная разборка комплекта конвейера по адресу поставки (не по номеру): городской
###   (ГАР-адрес/ОКТМО совпадает с delivery_cities) остаётся во входной папке, прочие переезжает в tenders\вне города.
###   Использовать после чистки/обновления фильтра города: повторный прогон переносит 0. Ничего не удаляет.
### ЗАПУСК: python check_cities.py   (работает поверх tenders\с найденными)
### ЗАВИСИМОСТИ: python 3.10+, gosplan_lib.py (для города-проверки), pyyaml.
### ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ: нет.
### ВХОД: комплекты в tenders\ с tender.json (адреса поставки из деталей закупки).
### ВЫХОД: перемещение комплектов в tenders\вне города\ и консольная сводка «перенесено N».
#!/usr/bin/env python3
# проверка комплектов по городу поставки: чужие города переезжают в tenders/вне города.
# правило города берёт из delivery_cities конфига; повторный прогон ничего не меняет.
# python check_cities.py -c config.novosibirsk.yaml

import argparse
import json
import sys
from pathlib import Path

import yaml

import gosplan_lib as gp

HERE = Path(__file__).resolve().parent
PIPE_FOLDERS = ["с найденными", "поиск цен", "на просчёт", "К вниманию"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=str(HERE / "config.novosibirsk.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    cities = (cfg.get("filters", {}) or {}).get("delivery_cities") or []
    if not cities:
        print("в конфиге нет delivery_cities — фильтр города не настроен", file=sys.stderr)
        return 2
    tenders = HERE / ((cfg.get("output", {}) or {}).get("dir") or "tenders")
    dest = tenders / "вне города"
    dest.mkdir(exist_ok=True)
    seen = gp.load_seen(tenders / "seen.json")

    moved, unknown, ok = [], [], 0
    for folder in PIPE_FOLDERS:
        base = tenders / folder
        for kit in sorted(base.iterdir()):
            tj = kit / "tender.json"
            if not tj.is_file():
                continue
            verdict = gp.delivery_city_verdict(json.loads(tj.read_text(encoding="utf-8")), cities)
            if verdict is True:
                ok += 1
            elif verdict is None:
                unknown.append(kit.name)
            else:
                dst = dest / kit.name
                kit.rename(dst)
                moved.append(kit.name)
                entry = seen.get(kit.name)
                if isinstance(entry, dict):
                    entry["city_check"] = "outside"
                    if entry.get("folder"):
                        entry["folder"] = str(dst)
    if moved:
        gp.save_seen(tenders / "seen.json", seen)
    print(f"город {', '.join(cities)}: в городе {ok}, перенесено в «вне города» {len(moved)},"
          f" без данных о поставке {len(unknown)}")
    for n in moved:
        print(f"  → вне города: {n}")
    for n in unknown:
        print(f"  ? без данных о месте поставки: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

