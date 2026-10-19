"""
IT-новостной бот для Телеграма. Читает RSS русских и казахских IT-источников,
оставляет только новости про выбранные компании (Google, Apple, Microsoft, OpenAI,
Tesla, Samsung, Intel, AMD) и новинки инструментов для разработчиков, и шлёт
по одной новости в формате поста: картинка + заголовок с эмодзи + короткие
абзацы + хэштеги. Без ссылок.

Режимы:
  python news_bot.py          работает постоянно (на компе)
  python news_bot.py --once   одна проверка и выход (для GitHub Actions)
  python news_bot.py --check  проверить ленты, ничего не отправляя
  python news_bot.py --test   тест: по новости раз в минуту, берёт и старые (до 7 дней)

Токен и chat_id берутся из settings.txt рядом со скриптом
или из переменных окружения BOT_TOKEN и CHAT_ID.
chat_id получателя: тот, кому бот должен слать новости (он обязан нажать /start у бота).
Зависимости: pip install requests
"""
import html
import json
import logging
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import requests

os.chdir(Path(__file__).resolve().parent)


def load_settings() -> dict:
    """Читает settings.txt рядом со скриптом: строки вида КЛЮЧ=значение."""
    cfg = {}
    path = Path("settings.txt")
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip().strip('"').strip("'")
    return cfg


_cfg = load_settings()
BOT_TOKEN = _cfg.get("BOT_TOKEN") or os.environ.get("BOT_TOKEN", "")
CHAT_ID = _cfg.get("CHAT_ID") or os.environ.get("CHAT_ID", "")

# ---------------------------- НАСТРОЙКИ ----------------------------
# режим "на компе" (постоянная работа)
CHECK_EVERY = 180            # как часто проверять ленты, сек
GAP_MIN, GAP_MAX = 30, 300   # случайная пауза между сообщениями, сек
FIRST_RUN_PER_FEED = 3       # при самом первом запуске берём по N свежих с ленты
QUEUE_MAX = 60               # если накопилось больше, самые старые выбрасываем
STARTUP_PING = True          # при запуске прислать сообщение "бот запущен"

# режим --once (GitHub Actions: запуск каждые 20-30 минут)
DAILY_MIN, DAILY_MAX = 5, 8  # сколько новостей в день (число на каждый день выбирается случайно)
SEND_WINDOW = (9, 21)        # в какие часы (по UTC_OFFSET) можно слать

# тестовый режим (--test): шлёт новости часто, берёт и старые, основной seen.json не трогает
TEST_GAP = 60                # пауза между постами, сек
TEST_AGE_HOURS = 24 * 7      # насколько старые новости разрешено брать (7 дней)

# общее
MAX_AGE_HOURS = 24           # старше этого не шлём
DESC_MAX = 600               # максимум символов в описании
UTC_OFFSET = 5               # часовой пояс ПОЛУЧАТЕЛЯ относительно UTC (Казахстан: +5)
QUIET_HOURS = (0, 8)         # в это время суток шлём без звука (режим "на компе")

# Если указан "site" вместо "url", адрес ленты бот найдёт сам.
# Фильтр теперь одинаковый для всех лент (см. classify), поэтому поле "filter" не нужно.
FEEDS = [
    {"name": "Хабр", "url": "https://habr.com/ru/rss/news/?fl=ru"},
    {"name": "Хабр", "url": "https://habr.com/ru/rss/best/daily/?fl=ru"},
    {"name": "OpenNet", "url": "https://www.opennet.ru/opennews/opennews_all.rss"},
    {"name": "Proglib", "url": "https://proglib.io/feed"},
    {"name": "Хакер", "url": "https://xakep.ru/feed/"},
    {"name": "3DNews", "url": "https://3dnews.ru/news/rss/"},
    {"name": "Digital Business", "site": "https://digitalbusiness.kz/"},
    {"name": "Digital Business", "site": "https://digitalbusiness.kz/kk/"},
]

# слова, из-за которых новость отбрасывается (не про IT)
NOT_IT = re.compile(
    r"политик|депутат|выбор[ыов]|футбол|хоккей|чемпионат|курс (?:доллара|тенге|валют)|"
    r"погод|\bдтп\b|убит|пожар|приговор|тюрьм|инфляц|пенси|гороскоп|рецепт",
    re.I,
)

# выбранные компании: название -> слова, по которым новость про неё
COMPANY_RE = {name: re.compile(p, re.I) for name, p in {
    "Google":    r"google|гугл|gemini|android|андроид|youtube|ютуб|chrome|deepmind|pixel",
    "Apple":     r"\bapple\b|эппл|айфон|iphone|ipad|macbook|macos|\bios\b|vision pro",
    "Microsoft": r"microsoft|майкрософт|windows|виндовс|copilot|копилот|azure|xbox|github|гитхаб|vs ?code|visual studio",
    "OpenAI":    r"openai|опенэй|chatgpt|чат-?gpt|sam altman|альтман|\bgpt|\bsora\b",
    "Tesla":     r"tesla|тесла|cybertruck|optimus",
    "Samsung":   r"samsung|самсунг|galaxy",
    "Intel":     r"\bintel\b|\bинтел\b|core ultra",
    "AMD":       r"\bamd\b|\bамд\b|ryzen|radeon|epyc",
}.items()}

# инструменты и технологии для айтишников
DEV_TOOLS = re.compile(
    r"github|гитхаб|copilot|копилот|vs ?code|visual studio|jetbrains|intellij|pycharm|cursor|"
    r"\bpython\b|\brust\b|typescript|javascript|golang|docker|kubernetes|linux|ubuntu|"
    r"\bapi\b|\bsdk\b|фреймворк|framework|компилятор|open ?source|открыт\w+ код|devops|"
    r"ии-агент|\bmcp\b|\bide\b",
    re.I,
)
# признак того, что это именно новинка/релиз, а не просто упоминание
NOVELTY = re.compile(
    r"вышел|вышла|вышло|релиз|нов\w+ (?:функци|верси|возможност)|добавил|появил|"
    r"представил|запустил|анонсир|обновлени|обновил|фич|выпустил",
    re.I,
)

# тема -> (эмодзи, хэштег, слова). Первая подошедшая задаёт эмодзи поста.
TOPICS = [
    ("🤖", "#ИИ", r"нейросет|искусственн\w* интеллект|\bии\b|\bai\b|chatgpt|openai|\bllm\b|\bgpt|gemini|claude|жасанды интеллект"),
    ("🔒", "#кибербезопасность", r"кибер|хакер|взлом|утечк|уязвим|вирус|malware|шифровальщик|даркнет"),
    ("🐧", "#opensource", r"linux|open ?source|открыт\w+ код|ядр[оа]|ubuntu|debian|gnome|kde"),
    ("🍎", "#Apple", r"apple|iphone|ipad|macos|\bios\b"),
    ("📱", "#гаджеты", r"смартфон|samsung|xiaomi|pixel|android|телефон"),
    ("🎮", "#игры", r"\bигр[аыуе]\b|playstation|xbox|steam|nintendo"),
    ("🚀", "#стартапы", r"стартап|инвестиц|раунд|венчур"),
    ("🧑‍💻", "#разработка", r"python|javascript|\bjava\b|\brust\b|golang|c\+\+|разработ|программист|программир|фреймворк|\bapi\b|github|devops|docker|kubernetes"),
    ("⚙️", "#железо", r"процессор|видеокарт|nvidia|\bamd\b|intel|\bчип|\bssd\b|оперативн"),
    ("☁️", "#облака", r"облак|\baws\b|azure|дата-центр"),
]
KZ_RE = re.compile(r"казахстан|астан|алматы|шымкент|қазақстан|\.kz\b", re.I)
# --------------------------------------------------------------------

SEEN_FILE = Path("seen.json")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html, */*",
}
IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)

log = logging.getLogger("newsbot")


# ----------------------------- утилиты -----------------------------
def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set) -> None:
    # записи "~sent:..." (счётчик отправленных за день) сортируются в конец и сохраняются
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:]), encoding="utf-8")


def http_get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=(8, 15))
    r.raise_for_status()
    return r


def clean_text(raw: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"(Читать (далее|дальше)|Читать полностью|Подробнее)\W*$", "", text.strip(), flags=re.I)
    return " ".join(text.split())


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_date(s: str):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


# ------------------------- чтение RSS / Atom -------------------------
def parse_feed(content: bytes) -> list:
    root = ET.fromstring(content)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        it = {"title": "", "link": "", "id": "", "desc": "", "body": "", "image": None, "date": None}
        for ch in el:
            name = local(ch.tag)
            text = (ch.text or "").strip()
            if name == "title":
                it["title"] = clean_text(text)
            elif name == "link":
                href = ch.get("href")
                if href:
                    rel = ch.get("rel")
                    if rel in (None, "alternate"):
                        it["link"] = href
                    elif rel == "enclosure" and (ch.get("type") or "").startswith("image"):
                        it["image"] = it["image"] or href
                elif text:
                    it["link"] = text
            elif name in ("guid", "id"):
                it["id"] = text
            elif name in ("description", "summary"):
                it["desc"] = it["desc"] or text
            elif name == "enclosure":
                if (ch.get("type") or "").startswith("image"):
                    it["image"] = it["image"] or ch.get("url")
            elif name in ("content", "thumbnail") and ch.get("url"):
                url = ch.get("url")
                if name == "thumbnail" or ch.get("medium") == "image" \
                        or (ch.get("type") or "").startswith("image") or IMG_EXT.search(url):
                    it["image"] = it["image"] or url
            elif name in ("encoded", "content") and text:
                it["body"] = it["body"] or text
            elif name in ("pubdate", "published", "updated", "date"):
                it["date"] = it["date"] or parse_date(text)

        if not it["title"]:
            continue
        it["id"] = it["id"] or it["link"] or it["title"]
        it["text"] = clean_text(it["desc"] or it["body"])
        if not it["image"]:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)', (it["desc"] or "") + (it["body"] or ""), re.I)
            if m and not m.group(1).startswith("data:"):
                it["image"] = urljoin(it["link"] or "", html.unescape(m.group(1)))
        items.append(it)
    return items


def discover_feed(site: str) -> str:
    """Ищет адрес RSS на главной странице сайта."""
    page = http_get(site).text[:300000]
    for tag in re.findall(r"<link\s[^>]*>", page, flags=re.I):
        if re.search(r'type\s*=\s*["\']application/(rss|atom)\+xml', tag, re.I):
            m = re.search(r'href\s*=\s*["\']([^"\']+)', tag, re.I)
            if m:
                return urljoin(site, html.unescape(m.group(1)))
    for guess in ("feed/", "rss/", "rss", "rss.xml"):
        url = urljoin(site, guess)
        try:
            head = http_get(url).text[:500].lower()
        except requests.RequestException:
            continue
        if "<rss" in head or "<feed" in head or "<?xml" in head:
            return url
    raise RuntimeError(f"не нашёл RSS на {site}")


def load_feed(feed: dict) -> list:
    if not feed.get("url"):
        feed["url"] = discover_feed(feed["site"])
    try:
        return parse_feed(http_get(feed["url"]).content)
    except Exception:
        if feed.get("site"):
            feed["url"] = None  # в следующий раз поищем адрес заново
        raise


# ------------------- отбор: выбранные компании и dev-новинки -------------------
def classify(item: dict) -> bool:
    """Оставляет только новости про выбранные компании или новинки для разработчиков.
    Заодно записывает в item["company"] и item["score"] (чем выше, тем важнее)."""
    title, text = item["title"], item["text"]
    if NOT_IT.search(title):
        return False
    best, best_score = None, 0
    for name, rx in COMPANY_RE.items():
        in_title = len(rx.findall(title))
        in_text = len(rx.findall(text))
        if in_title or in_text >= 2:
            score = 3 * in_title + in_text
            if score > best_score:
                best, best_score = name, score
    if best:
        item["company"], item["score"] = best, best_score
        return True
    dev_hits = len(DEV_TOOLS.findall(title))
    if dev_hits and NOVELTY.search(title + " " + text):
        item["company"], item["score"] = None, 2 * dev_hits
        return True
    return False


def collect_new(seen: set, queued: set, first_run: bool) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    result, ids = [], set()
    for feed in FEEDS:
        t0 = time.time()
        try:
            items = load_feed(feed)
        except Exception as e:
            log.warning("Лента «%s» не читается (%.0f с): %s", feed["name"], time.time() - t0, e)
            continue
        log.info("Лента «%s»: в ней %d шт. (%.0f с)", feed["name"], len(items), time.time() - t0)
        fresh = [
            i for i in items
            if i["id"] not in seen and i["id"] not in queued and i["id"] not in ids
            and (i["date"] is None or i["date"] >= cutoff)
        ]
        fresh = [i for i in fresh if classify(i)]
        fresh.sort(key=lambda i: i["date"] or datetime.now(timezone.utc), reverse=True)
        if first_run:
            fresh = fresh[:FIRST_RUN_PER_FEED]
        for i in fresh:
            i["source"] = feed["name"]
            ids.add(i["id"])
        result.extend(fresh)
    return result


# ------------------------- оформление поста -------------------------
def trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:- ") + "…"


def pick_topics(text: str):
    low = text.lower()
    emoji, tags = "💻", []
    for em, tag, pattern in TOPICS:
        if re.search(pattern, low, re.I):
            if not tags:
                emoji = em
            tags.append(tag)
    tags = tags[:2]
    if KZ_RE.search(low):
        tags.append("#Казахстан")
    return emoji, tags


def build_caption(item: dict) -> str:
    emoji, tags = pick_topics(item["title"] + " " + item["text"])
    if item.get("company"):
        tags = ["#" + item["company"]] + [t for t in tags if t != "#" + item["company"]]
    title = html.escape(trim(item["title"], 250))
    sentences = [s for s in re.split(r"(?<=[.!?…])\s+", item["text"]) if s]
    if sentences and sentences[0].lower().startswith(item["title"].lower()[:40]):
        sentences = sentences[1:]
    footer_tags = " ".join(["#IT"] + tags)
    source = html.escape(item.get("source", ""))

    limit = DESC_MAX
    while True:
        paragraphs, total = [], 0
        for i in range(0, len(sentences), 2):
            chunk = " ".join(sentences[i:i + 2])
            if total + len(chunk) > limit:
                if not paragraphs:
                    paragraphs.append(trim(chunk, limit))
                break
            paragraphs.append(chunk)
            total += len(chunk)
        parts = [f"{emoji} <b>{title}</b>"] + [html.escape(p) for p in paragraphs]
        parts.append(f"{footer_tags}\n📰 {source}" if source else footer_tags)
        caption = "\n\n".join(parts)
        if len(caption) <= 1000 or limit <= 100:
            return caption
        limit -= 100


# ----------------------------- Телеграм -----------------------------
def tg(method: str, payload: dict) -> requests.Response:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code == 429:
        wait = r.json().get("parameters", {}).get("retry_after", 5)
        time.sleep(wait + 1)
        r = requests.post(url, json=payload, timeout=30)
    return r


def is_quiet() -> bool:
    hour = datetime.now(timezone(timedelta(hours=UTC_OFFSET))).hour
    a, b = QUIET_HOURS
    return a <= hour < b


def og_image(link: str):
    try:
        page = http_get(link).text[:300000]
    except requests.RequestException:
        return None
    for tag in re.findall(r"<meta\s[^>]*>", page, flags=re.I):
        if re.search(r'(?:property|name)\s*=\s*["\'](?:og:image|twitter:image)["\']', tag, re.I):
            m = re.search(r'content\s*=\s*["\']([^"\']+)', tag, re.I)
            if m:
                return urljoin(link, html.unescape(m.group(1)))
    return None


def send_post(item: dict) -> None:
    caption = build_caption(item)
    silent = is_quiet()
    image = item.get("image") or (og_image(item["link"]) if item.get("link") else None)

    if image:
        r = tg("sendPhoto", {
            "chat_id": CHAT_ID, "photo": image, "caption": caption,
            "parse_mode": "HTML", "disable_notification": silent,
        })
        if r.ok:
            return
        log.info("Картинка не прошла (%s), шлю текстом: %s", r.status_code, item["title"])

    r = tg("sendMessage", {
        "chat_id": CHAT_ID, "text": caption, "parse_mode": "HTML",
        "disable_web_page_preview": True, "disable_notification": silent,
    })
    r.raise_for_status()


# ------------------------------- режимы -------------------------------
def setup_logging(to_file: bool) -> None:
    handlers = [logging.StreamHandler()]
    if to_file:
        handlers.append(logging.FileHandler("bot.log", encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def require_settings() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Не заданы BOT_TOKEN и CHAT_ID (файл settings.txt или секреты GitHub)")
        sys.exit(2)


def check_feeds() -> None:
    for feed in FEEDS:
        try:
            items = load_feed(feed)
            matched = sum(1 for i in items if classify(i))
            example = items[0]["title"][:70] if items else "-"
            print(f"OK    {feed['name']}: {len(items)} шт., подходит {matched} | {feed['url']} | {example}")
        except Exception as e:
            print(f"FAIL  {feed['name']}: {e}")


def run_once() -> None:
    """Один запуск: слать не больше одного поста, укладываясь в дневную норму."""
    setup_logging(to_file=False)
    require_settings()
    now = datetime.now(timezone(timedelta(hours=UTC_OFFSET)))
    day = now.strftime("%Y-%m-%d")
    start, end = SEND_WINDOW
    if not (start <= now.hour < end):
        log.info("Вне окна отправки, выходим")
        return

    seen = load_seen()
    sent_today = sum(1 for s in seen if s.startswith(f"~sent:{day}:"))
    target = random.Random(day).randint(DAILY_MIN, DAILY_MAX)
    if sent_today >= target:
        log.info("Дневная норма выполнена: %d из %d", sent_today, target)
        return
    # равномерно растягиваем отправку на окно: к концу окна должна набраться норма
    elapsed = (now.hour + now.minute / 60 - start) / (end - start)
    due = min(target, int(target * elapsed) + 1)
    if sent_today >= due:
        log.info("Пока рано: отправлено %d, по графику %d (норма %d)", sent_today, due, target)
        return

    new = collect_new(seen, set(), first_run=False)
    if not new:
        log.info("Подходящих новостей нет")
        return
    floor = datetime.min.replace(tzinfo=timezone.utc)
    new.sort(key=lambda i: (i["score"], i["date"] or floor), reverse=True)
    log.info("Подходящих неотправленных: %d", len(new))

    for item in new:
        seen.add(item["id"])
        try:
            send_post(item)
        except Exception as e:
            log.warning("Не отправилось «%s»: %s", item["title"], e)
            continue
        seen.add(f"~sent:{day}:{sent_today + 1}")
        log.info("Отправлено [%s]: %s", item.get("source"), item["title"])
        break
    save_seen(seen)


def run_test() -> None:
    """Тест: слать по одной новости раз в TEST_GAP секунд, брать и старые (до 7 дней).
    Использует отдельный seen_test.json, основной seen.json не трогает. Остановка: Ctrl+C."""
    global MAX_AGE_HOURS, SEEN_FILE
    MAX_AGE_HOURS = TEST_AGE_HOURS
    SEEN_FILE = Path("seen_test.json")
    setup_logging(to_file=False)
    require_settings()
    seen = load_seen()
    queue = []
    floor = datetime.min.replace(tzinfo=timezone.utc)
    log.info("ТЕСТ: по одной новости раз в %d с, возраст новостей до %d ч. Остановка: Ctrl+C",
             TEST_GAP, TEST_AGE_HOURS)
    while True:
        if not queue:
            queue = collect_new(seen, set(), first_run=False)
            queue.sort(key=lambda i: (i["score"], i["date"] or floor), reverse=True)
            log.info("Подходящих новостей в очереди: %d", len(queue))
            if not queue:
                time.sleep(TEST_GAP)
                continue
        item = queue.pop(0)
        seen.add(item["id"])
        try:
            send_post(item)
            log.info("Отправлено [%s]: %s", item.get("source"), item["title"])
        except Exception as e:
            log.warning("Не отправилось «%s»: %s", item["title"], e)
            save_seen(seen)
            continue
        save_seen(seen)
        time.sleep(TEST_GAP)


def run_forever() -> None:
    setup_logging(to_file=True)
    require_settings()

    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    queue = []
    last_poll = 0.0
    next_send = 0.0
    log.info("Бот запущен")

    if STARTUP_PING:
        try:
            r = tg("sendMessage", {"chat_id": CHAT_ID, "text": "✅ Бот запущен, новости пойдут по одной."})
            if r.ok:
                log.info("Стартовое сообщение отправлено, связь с Телеграмом есть")
            else:
                log.error("Телеграм отклонил сообщение (%s): %s. Проверь токен и chat_id", r.status_code, r.text)
        except Exception as e:
            log.error("Не удалось связаться с Телеграмом: %s", e)

    while True:
        now = time.time()

        if now - last_poll >= CHECK_EVERY:
            last_poll = now
            try:
                new = collect_new(seen, {i["id"] for i in queue}, first_run)
                first_run = False
                if new:
                    queue.extend(new)
                    queue.sort(key=lambda i: i["date"] or datetime.now(timezone.utc))
                    queue = queue[-QUEUE_MAX:]
                    log.info("Новых: %d, в очереди: %d", len(new), len(queue))
                else:
                    log.info("Проверка закончена: новых нет, в очереди %d", len(queue))
            except Exception:
                log.exception("Сбой при опросе лент")

        if queue and time.time() >= next_send:
            item = queue.pop(0)
            try:
                send_post(item)
                log.info("Отправлено [%s]: %s", item.get("source"), item["title"])
            except Exception as e:
                log.warning("Не отправилось «%s»: %s", item["title"], e)
            seen.add(item["id"])
            save_seen(seen)
            next_send = time.time() + random.uniform(GAP_MIN, GAP_MAX)

        time.sleep(5)


if __name__ == "__main__":
    if "--check" in sys.argv:
        check_feeds()
    elif "--once" in sys.argv:
        run_once()
    elif "--test" in sys.argv:
        run_test()
    else:
        run_forever()
