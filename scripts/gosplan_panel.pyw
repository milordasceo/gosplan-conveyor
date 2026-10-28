### ЧТО ДЕЛАЕТ: графическая панель ручного поиска (diic): регион, бюджет 44/223, ключевые слова,
###   «только первая стадия», скачивание документов вручную на машину (папки tenders\по номеру\<номер>).
###   Содержимое папок то же, что у cli-сканера (сводка.txt, tender.json, документы\).
### ЗАПУСК: pythonw gosplan_panel.pyw   (или открыть двойным щелчком на Windows-машине)
### ЗАВИСИМОСТИ: python 3.10+ с tkinter (в Windows поставляется штатно), pyyaml, gosplan_lib.py.
### ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ: нет (BASE_URL и API_KEY правятся в файле, не в среде).
### ВХОД: параметры из формы; ВЫХОД: папки tenders\ по номеру + лог в консоли-питона.
# панель разведки тендеров gosplan.info
# двойной щелчок → окно → параметры → старт → папки в tenders\к расчёту\<номер закупки>
# python >=3.9, pyyaml не нужен; зависимости — только стандартная библиотека

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

import gosplan_lib as lib

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "tenders"
OUT_READY = OUT_DIR / "с найденными"
SEEN_PATH = OUT_DIR / "seen.json"

BASE_URL = "https://v2test.gosplan.info"   # тестовый сервер без ключа; прод: https://v2.gosplan.info + api_key
API_KEY = None
PAUSE_MS = 700
TIMEOUT = 30

regions = json.loads((HERE / "regions.json").read_text(encoding="utf-8"))["regions"]
ALL_REGIONS = "— все регионы —"


def worker(cfg, ui_queue):
    log = lambda m: ui_queue.put(("log", m))
    try:
        sources = [s for s, on in (("fz44", cfg["fz44"]), ("fz223", cfg["fz223"])) if on]
        if not sources:
            ui_queue.put(("error", "не выбран ни один закон (44-ФЗ или 223-ФЗ)"))
            return
        found = []
        for src in sources:
            params = {}
            if cfg["region"]:
                params["region"] = cfg["region"]
            if cfg["price_from"] is not None:
                params["max_price_ge"] = cfg["price_from"]
            if cfg["price_to"] is not None:
                params["max_price_le"] = cfg["price_to"]
            if cfg["keywords"]:
                params["object_info"] = cfg["keywords"]
            if cfg["stage1_only"]:
                params["stage"] = 1
            for item in lib.iter_purchases(BASE_URL, src, params, API_KEY, TIMEOUT, PAUSE_MS, log):
                rec = {
                    "purchase_number": item.get("purchase_number") or "",
                    "source": src,
                    "object_info": item.get("object_info"),
                    "max_price": item.get("max_price"),
                    "currency_code": item.get("currency_code"),
                    "stage": item.get("stage"),
                    "published_at": item.get("published_at"),
                    "collecting_finished_at": item.get("collecting_finished_at"),
                    "region": item.get("region"),
                    "okpd2": item.get("okpd2") or [],
                    "customers": item.get("customers") or [],
                    "delivery_places": item.get("delivery_places") or [],
                }
                if cfg["keywords"] and not lib.keyword_ok(rec["object_info"], cfg["keywords"].split(), False):
                    continue
                # город поставки по списку записей: известен и вне города — отсекаем без деталей
                cities = [c for c in (cfg.get("delivery_city") or "").split() if c]
                if cities and rec["delivery_places"]:
                    if not any(lib.city_match(p, c) for c in cities for p in rec["delivery_places"]):
                        continue
                if rec["purchase_number"]:
                    found.append(rec)
            ui_queue.put(("found", len(found)))
        log(f"найдено по фильтрам: {len(found)}")

        seen = lib.load_seen(SEEN_PATH)
        OUT_READY.mkdir(parents=True, exist_ok=True)
        fresh = [r for r in found if r["purchase_number"] not in seen]
        log(f"новых (нет в seen.json): {len(fresh)}")
        todo = fresh[: cfg["detail_max"]]
        if len(fresh) > len(todo):
            log(f"лимит загрузки документов: обработаю первые {len(todo)}, остальное — в следующем прогоне")
        errors = 0
        done = 0
        for rec in todo:
            num = rec["purchase_number"]
            folder = OUT_READY / num
            if folder.exists():
                log(f"{num}: папка уже есть, пропуск")
                seen[num] = {"first_seen": "panel", "source": rec["source"]}
                lib.save_seen(SEEN_PATH, seen)
                done += 1
                ui_queue.put(("progress", done, len(todo), errors))
                continue
            try:
                detail = lib.fetch_detail(BASE_URL, rec["source"], num, API_KEY, TIMEOUT, PAUSE_MS, log)
                cities = (cfg.get("delivery_city") or "").split()
                if cities and lib.delivery_city_verdict(detail, [c.strip() for c in cities]) is False:
                    log(f"{num}: место поставки вне города ({', '.join(cities)}) — пропуск")
                    seen[num]["city_check"] = "outside"
                    lib.save_seen(SEEN_PATH, seen)
                    done += 1
                    ui_queue.put(("progress", done, len(todo), errors))
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "tender.json").write_text(
                    json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
                (folder / "сводка.txt").write_text(lib.summary_text(rec, detail), encoding="utf-8")
                docs_dir = folder / "документы"
                docs_dir.mkdir(parents=True, exist_ok=True)
                saved = 0
                for i, att in enumerate(lib.extract_attachments(detail), 1):
                    try:
                        name, size = lib.download_attachment(att, docs_dir, i)
                        saved += 1
                    except Exception as e:
                        errors += 1
                        log(f"{num}: файл не скачался ({att['fileName']}): {e}")
                seen[num] = {"first_seen": "panel", "source": rec["source"],
                             "folder": str(folder), "files": saved}
                lib.save_seen(SEEN_PATH, seen)
                done += 1
                ui_queue.put(("progress", done, len(todo), errors))
                log(f"{num}: готово, файлов {saved}")
            except lib.ApiError as e:
                errors += 1
                log(f"{num}: ошибка API: {e}")
                if "429" in str(e):
                    break
        lib.save_seen(SEEN_PATH, seen)
        ui_queue.put(("done", {
            "found": len(found), "fresh": len(fresh), "done": done, "errors": errors,
            "path": str(OUT_READY),
        }))
    except Exception as e:
        ui_queue.put(("error", str(e)))


class Panel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Разведка тендеров — gosplan.info")
        self.geometry("560x360")
        self.resizable(False, False)
        self.q = queue.Queue()

        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, **pad)

        ttk.Label(frm, text="Регион:").grid(row=0, column=0, sticky="w")
        names = [ALL_REGIONS] + [f"{c} — {n}" for c, n in sorted(regions.items(), key=lambda kv: (len(kv[0]), kv[1]))]
        self.region = ttk.Combobox(frm, values=names, state="readonly", width=42)
        self.region.current(0)
        self.region.grid(row=0, column=1, sticky="we", **pad)

        ttk.Label(frm, text="Цена договора, ₽:").grid(row=1, column=0, sticky="w")
        row1 = ttk.Frame(frm); row1.grid(row=1, column=1, sticky="we", **pad)
        self.price_from = ttk.Entry(row1, width=14); self.price_from.pack(side="left")
        ttk.Label(row1, text="  —  ").pack(side="left")
        self.price_to = ttk.Entry(row1, width=14); self.price_to.pack(side="left")

        row2 = ttk.Frame(frm); row2.grid(row=2, column=1, sticky="we", **pad)
        self.fz44 = tk.BooleanVar(value=True)
        self.fz223 = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="44-ФЗ", variable=self.fz44).pack(side="left")
        ttk.Checkbutton(row2, text="223-ФЗ", variable=self.fz223).pack(side="left", padx=12)

        ttk.Label(frm, text="Ключевые слова (через пробел):").grid(row=3, column=0, sticky="w")
        self.keywords = ttk.Entry(frm, width=44)
        self.keywords.grid(row=3, column=1, sticky="we", **pad)

        ttk.Label(frm, text="Город поставки:").grid(row=4, column=0, sticky="w")
        self.city = ttk.Entry(frm, width=30)
        self.city.insert(0, "Новосибирск")
        self.city.grid(row=4, column=1, sticky="we", **pad)

        self.stage1 = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="только идёт приём заявок", variable=self.stage1).grid(
            row=5, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Загрузить документов (макс):").grid(row=6, column=0, sticky="w")
        self.detail_max = tk.Spinbox(frm, from_=1, to=500, width=8)
        self.detail_max.delete(0, "end"); self.detail_max.insert(0, "50")
        self.detail_max.grid(row=6, column=1, sticky="w", **pad)

        self.start_btn = ttk.Button(frm, text="Старт", command=self.on_start)
        self.start_btn.grid(row=7, column=1, sticky="w", **pad)

        self.status = tk.StringVar(value="настрой параметры и жми старт")
        ttk.Label(frm, textvariable=self.status, wraplength=520, justify="left").grid(
            row=8, column=0, columnspan=2, sticky="we", **pad)

        self.bar = ttk.Progressbar(frm, mode="determinate")
        self.bar.grid(row=9, column=0, columnspan=2, sticky="we", **pad)

        frm.columnconfigure(1, weight=1)
        self.after(200, self.poll)

    def _num(self, widget):
        v = widget.get().strip().replace(" ", "").replace("\xa0", "")
        if not v:
            return None
        return float(v)

    def on_start(self):
        try:
            cfg = {
                "region": None if self.region.get() == ALL_REGIONS else self.region.get().split(" — ")[0],
                "price_from": self._num(self.price_from),
                "price_to": self._num(self.price_to),
                "fz44": self.fz44.get(),
                "fz223": self.fz223.get(),
                "keywords": self.keywords.get().strip(),
                "stage1_only": self.stage1.get(),
                "detail_max": int(self.detail_max.get()),
                "delivery_city": self.city.get().strip(),
            }
        except ValueError:
            messagebox.showerror("Ошибка", "цена и лимит — числа")
            return
        self.start_btn.config(state="disabled")
        self.status.set("ищу...")
        threading.Thread(target=worker, args=(cfg, self.q), daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, *args = self.q.get_nowait()
                if kind == "log":
                    self.status.set(str(args[0]))
                elif kind == "found":
                    self.status.set(f"найдено по фильтрам: {args[0]}")
                elif kind == "progress":
                    done, total, errors = args
                    self.bar["maximum"] = total
                    self.bar["value"] = done
                    self.status.set(f"обработано {done}/{total}, ошибок {errors}")
                elif kind == "done":
                    r = args[0]
                    self.status.set(
                        f"готово: найдено {r['found']}, новых {r['fresh']}, разложено {r['done']}, ошибок {r['errors']}\n"
                        f"папки: {r['path']}")
                    self.start_btn.config(state="normal")
                elif kind == "error":
                    self.status.set(f"ошибка: {args[0]}")
                    self.start_btn.config(state="normal")
        except queue.Empty:
            pass
        self.after(200, self.poll)


if __name__ == "__main__":
    Panel().mainloop()

