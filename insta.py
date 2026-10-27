"""Парсер Instagram: рилсы аккаунта → дословные тексты → корпус. Только стандартная библиотека Python, без ffmpeg.

  python3 insta.py сбор <ник> [--limit 50] [--newer 2026-01-01] [--out папка]
  python3 insta.py звук <папка>        # если reels.json положил коннектор Apify, а не сбор
  python3 insta.py текст <папка> [--model gemini-3.5-flash]
  python3 insta.py корпус <папка>
  python3 insta.py всё <ник> [--limit 50] [--out папка]

Ключи из окружения или .env (папка запуска и папка скрипта): APIFY_TOKEN (apify.com → Settings → API & Integrations),
GEMINI_API_KEY (aistudio.google.com/apikey, бесплатно). Сбор — Apify apify/instagram-reel-scraper (~$0.003 за рилс,
каждый месяц $5 бесплатно). Расшифровка — Gemini напрямую по звуку из Apify (audioUrl, иначе видео).
Повторный запуск не трогает уже расшифрованное.
"""
import base64, json, os, pathlib, sys, time, urllib.error, urllib.parse, urllib.request
import concurrent.futures as cf

HERE = pathlib.Path(__file__).resolve().parent
LIMIT_MAX = 200          # больше за раз не берём: $5 бесплатного лимита Apify хватает примерно на 1500 рилсов в месяц
APIFY_MAX_USD = 1.0      # потолок одного запуска Apify (Apify сам остановит актор)
MAX_MB = 18              # Gemini принимает файл в запросе до ~20 МБ
ACTOR = "apify~instagram-reel-scraper"
PROMPT = ("Это звук короткого ролика из Instagram. Сделай дословную расшифровку речи на языке оригинала "
          "(кыргызский — кириллицей с ң ө ү; русские слова и англицизмы пиши как произнесены). "
          "Ничего не исправляй, не сокращай, не перефразируй. Расставь знаки препинания, каждое предложение с новой строки. "
          "Музыку и шумы не описывай. Если речи нет — напиши ровно: [без речи]. Выдай только текст.")


def key(name):
    if os.environ.get(name):
        return os.environ[name]
    for f in (pathlib.Path.cwd() / ".env", HERE / ".env"):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    sys.exit(f"Нет ключа {name}: добавь в переменные окружения или в .env (строка {name}=...).")


def http(url, data=None, headers=None, timeout=120):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, body, {"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


# ---------- 1. сбор: Apify → reels.json + звук ----------
def collect(nick, out):
    nick = nick.strip().lstrip("@").rstrip("/").split("/")[-1]
    out = pathlib.Path(out or f"instagram/{nick}"); out.mkdir(parents=True, exist_ok=True)
    limit = min(int(arg("--limit", 50)), LIMIT_MAX)
    inp = {"username": [nick], "resultsLimit": limit, "skipPinnedPosts": False}
    if arg("--newer"):
        inp["onlyPostsNewerThan"] = arg("--newer")
    auth = {"Authorization": "Bearer " + key("APIFY_TOKEN")}
    q = urllib.parse.urlencode({"maxItems": limit, "maxTotalChargeUsd": APIFY_MAX_USD})
    run = json.loads(http(f"https://api.apify.com/v2/acts/{ACTOR}/runs?{q}", inp, auth))["data"]
    print(f"Apify: собираю до {limit} рилсов @{nick}…", flush=True)
    while run["status"] in ("READY", "RUNNING"):
        run = json.loads(http(f"https://api.apify.com/v2/actor-runs/{run['id']}?waitForFinish=60", headers=auth))["data"]
    if run["status"] != "SUCCEEDED":
        sys.exit(f"Apify: запуск закончился со статусом {run['status']}. Аккаунт закрытый или ник с ошибкой?")
    items = json.loads(http(f"https://api.apify.com/v2/datasets/{run['defaultDatasetId']}/items?clean=true", headers=auth))
    items = [i for i in items if i.get("shortCode") and not i.get("error")]
    if not items:
        sys.exit("Apify ничего не вернул: аккаунт закрытый, ник с ошибкой или рилсов нет.")
    old = {i["shortCode"]: i for i in json.loads((out / "reels.json").read_text())} if (out / "reels.json").exists() else {}
    old.update({i["shortCode"]: i for i in items})
    (out / "reels.json").write_text(json.dumps(list(old.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    usd = (run.get("usageTotalUsd") or 0)
    print(f"Собрано {len(items)} рилсов (всего в папке {len(old)}), Apify ≈ ${usd:.2f}.", flush=True)
    download(out, items)
    return out


def download(out, items=None):
    """Звук по ссылкам из reels.json. Ссылки Instagram живут несколько часов — качать сразу после сбора."""
    out = pathlib.Path(out); (out / "звук").mkdir(parents=True, exist_ok=True)
    (out / ".gitignore").write_text("звук/\n")   # звук тяжёлый и в репозиторий не идёт, тексты остаются
    items = items or json.loads((out / "reels.json").read_text())
    print("Качаю звук…", flush=True)
    def get(i):
        f = out / "звук" / f"{i['shortCode']}.mp4"
        if f.exists():
            return 0
        for u in (i.get("audioUrl"), i.get("videoUrl")):
            if u:
                try:
                    f.write_bytes(http(u, timeout=180)); return 0
                except Exception:
                    pass
        return 1
    with cf.ThreadPoolExecutor(6) as ex:
        bad = sum(ex.map(get, items))
    print(f"Звук: {len(items) - bad} ок" + (f", {bad} не скачались (ссылки протухли — собери заново)" if bad else ""))


# ---------- 2. текст: Gemini дословно ----------
def gemini(model, mime, raw):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": PROMPT}, {"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode()}}]}],
            "generationConfig": {"temperature": 0}}
    for wait in (0, 20, 60, 120):        # бесплатная квота: на 429 ждём и пробуем ещё
        time.sleep(wait)
        try:
            d = json.loads(http(url, body, {"x-goog-api-key": key("GEMINI_API_KEY")}, timeout=300))
            return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]).strip()
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise SystemExit(f"Gemini {e.code}: {e.read().decode()[:300]}")
    raise SystemExit("Gemini: квота кончилась. Бесплатная обнуляется раз в сутки — запусти завтра, готовое не пропадёт.")


def transcribe(out):
    out = pathlib.Path(out); (out / "текст").mkdir(exist_ok=True)
    model = arg("--model", "gemini-3.5-flash")
    dur = {i["shortCode"]: i.get("videoDuration") or 0 for i in json.loads((out / "reels.json").read_text())}
    todo = [f for f in sorted((out / "звук").glob("*.mp4")) if not (out / "текст" / f"{f.stem}.txt").exists()]
    print(f"Расшифровка: {len(todo)} роликов, модель {model}", flush=True)
    def one(f):
        raw = f.read_bytes()
        if len(raw) > MAX_MB * 2**20:
            return f.stem, "больше 18 МБ, пропущен"
        mime = "audio/mp4" if raw[4:8] == b"ftyp" and b"M4A" in raw[8:12] else "video/mp4"
        text = gemini(model, mime, raw)
        words, sec = len(text.split()), dur.get(f.stem) or 0
        if sec > 15 and words / sec < 1.5 and text != "[без речи]":   # Gemini иногда обрывает текст — переспрашиваем
            text2 = gemini(model, mime, raw)
            text = max(text, text2, key=lambda t: len(t.split()))
        (out / "текст" / f"{f.stem}.txt").write_text(text + "\n", encoding="utf-8")
        return f.stem, f"{len(text.split())} слов"
    with cf.ThreadPoolExecutor(3) as ex:
        for code, msg in ex.map(one, todo):
            print(f"  {code}: {msg}", flush=True)


# ---------- 3. корпус: всё в один файл по убыванию просмотров ----------
def corpus(out):
    out = pathlib.Path(out)
    rows = []
    for i in json.loads((out / "reels.json").read_text()):
        t = out / "текст" / f"{i['shortCode']}.txt"
        rows.append({"код": i["shortCode"], "ссылка": i.get("url"), "дата": (i.get("timestamp") or "")[:10],
                     "просмотры": i.get("videoPlayCount") or i.get("videoViewCount") or 0, "лайки": i.get("likesCount") or 0,
                     "комменты": i.get("commentsCount") or 0, "секунды": round(i.get("videoDuration") or 0),
                     "подпись": (i.get("caption") or "").strip(), "текст": t.read_text(encoding="utf-8").strip() if t.exists() else ""})
    rows.sort(key=lambda r: -r["просмотры"])
    (out / "корпус.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    nick = out.name
    md = [f"# Корпус @{nick}", "", f"{len(rows)} рилсов, по убыванию просмотров. Собрано {time.strftime('%d.%m.%Y')}.", ""]
    for n, r in enumerate(rows, 1):
        md += [f"## {n}. {r['просмотры']:,} просмотров · {r['дата']} · {r['секунды']} с".replace(",", " "),
               f"{r['ссылка']} · ❤ {r['лайки']} · 💬 {r['комменты']}", "", r["текст"] or "_(нет расшифровки)_", ""]
        if r["подпись"]:
            md += ["**Подпись:** " + r["подпись"].replace("\n", " "), ""]
    (out / "корпус.md").write_text("\n".join(md), encoding="utf-8")
    done = sum(1 for r in rows if r["текст"])
    print(f"Корпус: {out / 'корпус.md'} — {len(rows)} рилсов, расшифровано {done}, слов {sum(len(r['текст'].split()) for r in rows)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 2 else ""
    if cmd == "сбор":
        collect(sys.argv[2], arg("--out"))
    elif cmd == "звук":
        download(sys.argv[2])
    elif cmd == "текст":
        transcribe(sys.argv[2])
    elif cmd == "корпус":
        corpus(sys.argv[2])
    elif cmd == "всё":
        o = collect(sys.argv[2], arg("--out")); transcribe(o); corpus(o)
    else:
        sys.exit(__doc__)
