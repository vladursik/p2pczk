import os
import json
import time
import threading
import logging
import requests
import telebot
from concurrent.futures import ThreadPoolExecutor, as_completed
from telebot import types
from collections import deque
from datetime import datetime
from dotenv import load_dotenv

# ==================== ЛОГУВАННЯ ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== КОНФІГ ====================

load_dotenv()

TOKEN    = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TOKEN or not ADMIN_ID:
    logger.critical("TOKEN або ADMIN_ID не задані в .env — вихід")
    raise SystemExit(1)

USERS_FILE = "users.json"

# ==================== ЗБЕРІГАННЯ ЮЗЕРІВ ====================

users_lock = threading.Lock()

def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Не вдалось завантажити users.json: {e}")
    return {}

def save_users(users: dict):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Не вдалось зберегти users.json: {e}")

users: dict = load_users()

def default_user_data(chat_id: int, username: str = "", first_name: str = "") -> dict:
    return {
        "chat_id":      chat_id,
        "username":     username,
        "first_name":   first_name,
        "active":       False,
        "monitoring":   False,
        "registered_at": datetime.now().isoformat(),
        "last_seen":    datetime.now().isoformat(),
        "buy_threshold":   43.50,
        "sell_threshold":  45.90,
        "min_amount_uah":  20000,
        "min_amount_sell": 20000,
        "min_amount_sell_paysend": 20000,
        "check_interval":  20,
        "balance_usdt":    0.0,
        "czk_spread_threshold": 2.5,
        "blacklist_buy":  [],
        "blacklist_sell": [],
        "enabled_banks":  dict(DEFAULT_ENABLED_BANKS),
    }

def get_user(chat_id: int) -> dict | None:
    with users_lock:
        return users.get(str(chat_id))

def upsert_user(chat_id: int, username: str = "", first_name: str = "") -> dict:
    key = str(chat_id)
    with users_lock:
        if key not in users:
            users[key] = default_user_data(chat_id, username, first_name)
            logger.info(f"Новий юзер: {chat_id} (@{username})")
        else:
            users[key]["last_seen"]  = datetime.now().isoformat()
            users[key]["username"]   = username or users[key].get("username", "")
            users[key]["first_name"] = first_name or users[key].get("first_name", "")
        save_users(users)
        return users[key]

def update_user_field(chat_id: int, field: str, value):
    key = str(chat_id)
    with users_lock:
        if key in users:
            users[key][field] = value
            save_users(users)

def is_active(chat_id: int) -> bool:
    u = get_user(chat_id)
    return u is not None and u.get("active", False)

def is_admin(chat_id: int) -> bool:
    return chat_id == ADMIN_ID

# ==================== БЛЕКЛИСТ (per user) ====================

def add_to_blacklist(chat_id: int, merchant: str, trade_type: str) -> bool:
    key   = str(chat_id)
    field = "blacklist_buy" if trade_type == "BUY" else "blacklist_sell"
    with users_lock:
        if key not in users:
            return False
        bl = users[key].setdefault(field, [])
        if merchant in bl:
            return False
        bl.append(merchant)
        save_users(users)
    return True

def remove_from_blacklist(chat_id: int, merchant: str, trade_type: str) -> bool:
    key   = str(chat_id)
    field = "blacklist_buy" if trade_type == "BUY" else "blacklist_sell"
    with users_lock:
        if key not in users:
            return False
        bl = users[key].setdefault(field, [])
        if merchant not in bl:
            return False
        bl.remove(merchant)
        save_users(users)
    return True

def clear_blacklist(chat_id: int, trade_type: str):
    key   = str(chat_id)
    field = "blacklist_buy" if trade_type == "BUY" else "blacklist_sell"
    with users_lock:
        if key not in users:
            return
        users[key][field] = []
        save_users(users)

def blacklist_display_name(key: str) -> str:
    if "::" in key:
        nick, bank_key = key.split("::", 1)
        return f"{nick} ({BANK_LABELS.get(bank_key, bank_key)})"
    return key

def is_blacklisted(chat_id: int, merchant: str, trade_type: str) -> bool:
    u     = get_user(chat_id)
    field = "blacklist_buy" if trade_type == "BUY" else "blacklist_sell"
    return merchant in (u or {}).get(field, [])

# ==================== СТАН ====================

bot     = telebot.TeleBot(TOKEN)
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Content-Type": "application/json",
    "clienttype": "web",
    "lang": "uk-UA",
    "Referer": "https://p2p.binance.com/uk-UA/trade/all-payments/USDT?fiat=UAH",
    "Origin": "https://p2p.binance.com",
})

import re
import unicodedata

def normalize_text(s: str) -> str:
    """
    Прибирає невидимі/нульової ширини символи (zero-width space, joiner,
    RTL/LTR marks тощо) та комбінуючі діакритики, якими іноді "розбивають"
    слова на кшталт "Ф\u200bО\u200bП", щоб обійти прості текстові фільтри.
    Після цього нормалізує unicode (NFKC) і приводить до нижнього регістру.
    """
    if not s:
        return ""
    cleaned = "".join(
        ch for ch in s
        if unicodedata.category(ch) not in ("Cf", "Mn")
    )
    cleaned = unicodedata.normalize("NFKC", cleaned)
    return cleaned.lower()

FOP_KEYWORDS = ["фоп", " тов ", "тов.", "(тов)"]

# Тільки для Monobank (на продаж) діє окреме правило: показуємо оголошення
# лише якщо в описі явно згадано API-токен/ключ monobank. Фільтр
# "тільки фізособа" прибрано — для Mono тепер єдиний критерій це API.
BANK_REQUIRES_API = {"mono"}

# ФОП-оголошення, де в описі явно пояснюють, як створити API-токен/ключ
# monobank для оплати (типовий текст на кшталт "Створити API-токен (я
# допоможу на кожному етапі)" або "Робота ведеться лише через Monobank за
# API-ключем") — для Monobank такі оголошення показуємо (це і є весь сенс
# фільтра вище). Для решти банків ФОП/ТОВ фільтр працює як і раніше,
# без жодних виключень по API.
API_TOKEN_KEYWORDS = [
    "api-токен", "api токен", "апі-токен", "апі токен",
    "api-token", "api token", "апи-токен", "апи токен",
    "api-ключ", "api ключ", "апі-ключ", "апі ключ",
    "апи-ключ", "апи ключ", "api-key", "api key",
]

# ==================== БАНКИ (per user toggle) ====================

BANK_LABELS = {
    "mono":    "Monobank",
    "privat":  "ПриватБанк",
    "abank":   "A-Bank",
    "pumb":    "ПУМБ",
    "ukrgaz":  "Укргазбанк",
    "sense":   "Sense Bank",
    "paysend": "PaySend",
    "iban":    "IBAN",
}
BANK_KEYWORDS = {
    "mono":    ["monobank", "mono"],
    "privat":  ["privat", "приват"],
    "abank":   ["a-bank", "abank", "a bank"],
    "pumb":    ["pumb", "пумб", "fuib"],
    "ukrgaz":  ["ukrgaz", "укргаз"],
    "sense":   ["sense bank", "sensebank", "sense", "сенс банк", "сенсбанк", "сенс"],
    # PaySend майже ніколи не приходить як окремий tradeMethod — мейкери
    # зазвичай прописують його прямо в умовах оголошення (remarks), а як
    # спосіб оплати в Binance може стояти карта іншого банку (Privat тощо).
    # Тому ці ключові слова шукаються і в тексті умов, див. get_binance_p2p.
    "paysend": ["paysend", "pay send", "пейсенд", "пэйсенд", "пайсенд", "пэй сенд", "пей сенд"],
    # IBAN так само практично ніколи не приходить окремим tradeMethod —
    # мейкери пишуть про оплату на IBAN-рахунок прямо в умовах (remarks).
    # Тому, як і PaySend, шукається і в способах оплати, і в тексті умов.
    "iban":    ["iban", "ібан", "ибан"],
}
DEFAULT_ENABLED_BANKS = {"mono": True, "privat": True, "abank": True, "pumb": True, "ukrgaz": True, "sense": True, "paysend": True, "iban": True}
BANK_ORDER = ["mono", "privat", "abank", "pumb", "ukrgaz", "sense", "paysend", "iban"]

# Банки/методи, для яких умови ордера (remarks) шукаємо в доповнення до
# способів оплати Binance, бо як окремий tradeMethod вони майже не зустрічаються.
BANKS_MATCH_IN_REMARKS = {"paysend", "iban"}

def get_enabled_banks(ud: dict) -> dict:
    eb = (ud or {}).get("enabled_banks")
    merged = dict(DEFAULT_ENABLED_BANKS)
    if eb:
        merged.update(eb)
    return merged

MAX_RETRIES  = 3
RETRY_DELAYS = [5, 15, 30]

state_lock  = threading.Lock()
check_count = 0
api_down    = False
start_time  = datetime.now()

HISTORY_MAXLEN = 540
price_history  = deque(maxlen=HISTORY_MAXLEN)

CZK_CHECK_INTERVAL = 60          # сек між перевірками спреду CZK
czk_alert_lock   = threading.Lock()
czk_alert_active = False         # щоб не слати алерт на кожній ітерації, а лише при перетині порогу

user_monitor_state: dict = {}

def get_monitor_state(chat_id: int) -> dict:
    if chat_id not in user_monitor_state:
        user_monitor_state[chat_id] = {
            "seen_buy":    None,
            "seen_sell":   None,
            "last_spread": None,
        }
    return user_monitor_state[chat_id]

# ==================== ДЕТАЛІ ОГОЛОШЕННЯ (реальний remarks) ====================
#
# Endpoint пошуку (/adv/search) завжди повертає remarks=null — Binance просто
# не віддає текст умов мейкера в цьому запиті (перевірено напряму через
# DevTools, і в браузері, і в боті — однаково null). Реальний текст умов
# віддається лише окремим запитом по конкретному оголошенню:
# /bapi/c2c/v2/friendly/c2c/adv/detail-with-advertiser?channel=c2c&advNo=...&area=p2pZone
#
# Кешуємо результат на короткий час, щоб не бомбити Binance зайвими запитами —
# ті самі оголошення часто повторюються між сусідніми перевірками.

ADV_DETAIL_URL   = "https://p2p.binance.com/bapi/c2c/v2/public/c2c/adv/detail-with-advertiser"
_remarks_cache: dict = {}          # adv_no -> (timestamp, remarks_text)
_remarks_cache_lock = threading.Lock()
REMARKS_CACHE_TTL = 90  # секунд

def fetch_adv_remarks(adv_no: str) -> str:
    if not adv_no:
        return ""

    now = time.time()
    with _remarks_cache_lock:
        cached = _remarks_cache.get(adv_no)
        if cached and (now - cached[0]) < REMARKS_CACHE_TTL:
            return cached[1]

    remarks = ""
    try:
        r = session.get(
            ADV_DETAIL_URL,
            params={"channel": "c2c", "advNo": adv_no, "area": "p2pZone"},
            timeout=8,
        )
        if r.status_code == 200:
            payload = r.json()
            remarks = ((payload.get("data") or {}).get("adv") or {}).get("remarks") or ""
        else:
            logger.warning(f"detail-with-advertiser HTTP {r.status_code} для advNo={adv_no}")
    except Exception as e:
        logger.warning(f"Не вдалось отримати remarks для advNo={adv_no}: {e}")

    with _remarks_cache_lock:
        _remarks_cache[adv_no] = (now, remarks)
    return remarks

# ==================== ПАРСИНГ P2P ====================

PAGES_TO_FETCH = 8   # верхня межа "про всяк випадок" — реально зупиняється раніше, щойно сторінка неповна/пуста
ROWS_PER_PAGE   = 20  # максимум, який приймає /adv/search за одну сторінку
PAGE_FETCH_WORKERS = 5  # скільки сторінок тягнути одночасно (паралельно)

def _fetch_binance_page(url: str, trade_type: str, page: int):
    """Тягне одну сторінку оголошень. Повертає (list_or_None, page_was_empty)."""
    data = {
        "asset": "USDT",
        "fiat": "UAH",
        "merchantCheck": False,
        "page": page, "rows": ROWS_PER_PAGE,
        "payTypes": [],  # пусто = забираємо ВСІ методи оплати, фільтруємо самі нижче
        "tradeType": trade_type,
    }

    for attempt in range(MAX_RETRIES):
        try:
            r = session.post(url, json=data, timeout=12)

            if r.status_code == 429:
                wait = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.warning(f"Rate limit Binance P2P ({trade_type}, стор.{page}), чекаю {wait}с")
                time.sleep(wait)
                continue

            if r.status_code != 200:
                logger.warning(
                    f"Binance P2P HTTP {r.status_code} ({trade_type}, стор.{page}), "
                    f"body: {r.text[:300]!r}"
                )
            r.raise_for_status()
            resp = r.json()

            if not resp or not resp.get("data"):
                return [], True  # запит успішний, просто немає (більше) оголошень на цій сторінці

            return resp["data"], False

        except requests.exceptions.Timeout:
            logger.warning(f"Таймаут ({trade_type}, стор.{page}), спроба {attempt+1}/{MAX_RETRIES}")
        except requests.exceptions.ConnectionError:
            logger.warning(f"Нема з'єднання ({trade_type}, стор.{page}), спроба {attempt+1}/{MAX_RETRIES}")
        except Exception as e:
            logger.error(f"Помилка P2P ({trade_type}, стор.{page}): {e}", exc_info=True)

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAYS[attempt])

    return None, False  # усі спроби на цю сторінку провалились


def get_binance_p2p(trade_type: str, user_data: dict):
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

    enabled_banks = get_enabled_banks(user_data)

    chat_id   = user_data["chat_id"]
    my_amount = user_data["min_amount_sell"] if trade_type == "SELL" else user_data["min_amount_uah"]
    balance_usdt = user_data.get("balance_usdt", 0)

    # ---- Збираємо оголошення з кількох сторінок паралельно (швидше, ніж по черзі) ----
    raw_items = []
    seen_adv_no = set()
    any_page_ok = False

    page_results = {}  # page_num -> (items_or_None, was_empty)
    with ThreadPoolExecutor(max_workers=PAGE_FETCH_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_binance_page, url, trade_type, page): page
            for page in range(1, PAGES_TO_FETCH + 1)
        }
        for fut in as_completed(futures):
            page = futures[fut]
            try:
                page_results[page] = fut.result()
            except Exception as e:
                logger.error(f"Помилка отримання сторінки {page} ({trade_type}): {e}", exc_info=True)
                page_results[page] = (None, False)

    # Обробляємо результати СТРОГО по порядку сторінок: щойно якась сторінка
    # виявилась неповною/пустою — далі не враховуємо (там більше нічого немає).
    for page in range(1, PAGES_TO_FETCH + 1):
        page_items, was_empty = page_results.get(page, (None, False))

        if page_items is None:
            logger.warning(f"Сторінку {page} ({trade_type}) не вдалось отримати, пропускаю")
            continue

        any_page_ok = True

        if was_empty or not page_items:
            break

        for it in page_items:
            adv_no = (it.get("adv") or {}).get("advNo") or ""
            if adv_no and adv_no in seen_adv_no:
                continue
            if adv_no:
                seen_adv_no.add(adv_no)
            raw_items.append(it)

        if len(page_items) < ROWS_PER_PAGE:
            break

    if not any_page_ok:
        return None  # жодна сторінка не відповіла — вважаємо, що API впав

    if not raw_items:
        logger.info(f"Binance P2P: порожня відповідь ({trade_type})")
        return []  # запити успішні, просто немає оголошень — це не падіння API

    # ---- Тягнемо "умови угоди" (remarks) для ВСІХ оголошень одразу і паралельно ----
    # (замість того, щоб робити це по черзі всередині циклу фільтрації нижче —
    # саме це раніше й було головною причиною довгого очікування "поточних цін")
    adv_nos = [(it.get("adv") or {}).get("advNo") or "" for it in raw_items]
    adv_nos = [a for a in adv_nos if a]
    if adv_nos:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(fetch_adv_remarks, a): a for a in adv_nos}
            for fut in as_completed(futures):
                try:
                    fut.result()  # результат сам кешується всередині fetch_adv_remarks
                except Exception as e:
                    logger.warning(f"Помилка паралельного fetch_adv_remarks: {e}")

    try:
        results = []
        for item in raw_items:
            adv  = item["adv"]
            user = item["advertiser"]

            price    = float(adv["price"])
            merchant = user["nickName"]

            pay_methods_text = " ".join(
                (m.get("tradeMethodName") or "") for m in adv.get("tradeMethods", [])
            ).lower()

            # Реальний remarks endpoint /search завжди повертає null —
            # тягнемо текст умов мейкера окремим (кешованим) запитом.
            # Тягнемо його ще ДО визначення банку, бо PaySend зазвичай
            # не приходить як tradeMethod, а прописаний саме тут.
            real_remarks = fetch_adv_remarks(adv.get("advNo") or "")
            remarks_lower = real_remarks.lower()

            # Показуємо тільки оголошення з банками, увімкненими у користувача.
            # Збираємо ВСІ банки, що збіглися (а не лише перший по порядку) —
            # інакше оголошення з кількома методами оплати (напр. Укргазбанк + A-Bank)
            # неправильно потрапляє під обмеження "тільки фізособа" через A-Bank,
            # хоча по Укргазбанку воно мало б пройти без обмежень.
            matched_banks = []
            for bank_key in BANK_ORDER:
                if not enabled_banks.get(bank_key, True):
                    continue
                # PaySend та IBAN шукаємо і в способах оплати, і в умовах ордера —
                # решту банків, як і раніше, тільки в способах оплати.
                haystack = (pay_methods_text + " " + remarks_lower) if bank_key in BANKS_MATCH_IN_REMARKS else pay_methods_text
                if any(kw in haystack for kw in BANK_KEYWORDS[bank_key]):
                    matched_banks.append(bank_key)
            if not matched_banks:
                logger.debug(
                    f"Відсіяно (банк не розпізнано/вимкнено): {merchant}, "
                    f"спосіб оплати: {pay_methods_text[:80]!r}"
                )
                continue

            # Пріоритет — PaySend або IBAN, якщо вони серед збігів: якщо в
            # умовах ордера прямо згадано PaySend/IBAN, це і є реальний
            # спосіб оплати, навіть якщо Binance показує карту іншого банку
            # (Privat/тощо) як tradeMethod. Інакше — банк БЕЗ
            # спецправила по API (Monobank), якщо такий є серед збігів.
            if "paysend" in matched_banks:
                matched_bank = "paysend"
            elif "iban" in matched_banks:
                matched_bank = "iban"
            else:
                unrestricted = [b for b in matched_banks if b not in BANK_REQUIRES_API]
                matched_bank = unrestricted[0] if unrestricted else matched_banks[0]

            # Бан прив'язаний до пари мерчант+банк, а не до мерчанта повністю —
            # забанивши оголошення на Mono, не втрачаємо його ж оголошення на Privat
            if is_blacklisted(chat_id, f"{merchant}::{matched_bank}", trade_type):
                continue

            all_text = normalize_text(" ".join([
                real_remarks,
                pay_methods_text,
                (adv.get("asset") or ""),
            ]))

            is_fop      = any(word in all_text for word in FOP_KEYWORDS)
            has_api_tok = any(kw in all_text for kw in API_TOKEN_KEYWORDS)

            if trade_type == "SELL" and matched_bank in BANK_REQUIRES_API:
                # Monobank на продаж: єдиний критерій — явна згадка API-токена/ключа.
                # ФОП/ТОВ і "тільки фізособа" тут більше не перевіряються окремо.
                if not has_api_tok:
                    logger.info(
                        f"Відсіяно (Monobank без згадки API-токена/ключа): "
                        f"{merchant}, remarks: {real_remarks[:150]!r}"
                    )
                    continue
            else:
                # Для решти банків (і Monobank на BUY) — як і раніше відсікаємо ФОП/ТОВ.
                if is_fop:
                    logger.info(
                        f"Відсіяно як ФОП: {merchant} ({matched_bank}), "
                        f"remarks: {real_remarks[:120]!r}"
                    )
                    continue

            ad_min_limit = float(adv.get("minSingleTransAmount", 0))
            ad_max_limit = float(
                adv.get("maxSingleTransAmount")
                or adv.get("dynamicMaxSingleTransAmount")
                or 0
            )

            # Фільтр по мінімальній сумі користувача.
            # Для PaySend на продаж — окремий поріг суми, якщо задано.
            if trade_type == "SELL" and matched_bank == "paysend":
                my_amount_for_ad = user_data.get("min_amount_sell_paysend", my_amount)
            else:
                my_amount_for_ad = my_amount

            if ad_max_limit > 0 and ad_max_limit < my_amount_for_ad:
                continue

            # ✅ НОВИЙ ФІЛЬТР: якщо є баланс USDT — перевіряємо чи влізе вся сума
            if balance_usdt > 0 and trade_type == "SELL":
                my_usdt_in_uah = balance_usdt * price
                if ad_max_limit > 0 and ad_max_limit < my_usdt_in_uah:
                    continue

            stats  = user.get("userStatsRet") or user.get("userStat") or {}
            orders = (
                user.get("monthOrderCount")
                or user.get("orderCount")
                or stats.get("completedOrderNum")
                or stats.get("recentOrderNum")
                or 0
            )
            rate_raw = (
                user.get("monthFinishRate")
                or user.get("positiveRate")
                or stats.get("completionRate")
                or stats.get("recentExecuteRate")
                or 0
            )
            rate = round(float(rate_raw) * 100, 1) if float(rate_raw) <= 1 else round(float(rate_raw), 1)

            results.append({
                "price":         price,
                "merchant":      merchant,
                "advertiser_no": user.get("userNo") or user.get("advertiserNo") or "",
                "adv_no":        adv.get("advNo") or "",
                "min_limit":     ad_min_limit,
                "max_limit":     ad_max_limit,
                "remarks":       real_remarks,
                "orders":        orders,
                "rate":          rate,
                "bank":          matched_bank,
                "bank_label":    BANK_LABELS[matched_bank],
                "api_token":     has_api_tok,
            })

        if not results:
            logger.info(
                f"Binance P2P ({trade_type}): отримано {len(raw_items)} оголошень (усі сторінки), "
                f"жодне не пройшло фільтри (банки/ліміти/бан-лист)"
            )
            return []  # жодне оголошення не пройшло фільтри — API живий, просто зараз нічого підходящого

        results.sort(key=lambda x: x["price"], reverse=(trade_type == "SELL"))
        return results

    except Exception as e:
        logger.error(f"Помилка обробки P2P ({trade_type}): {e}", exc_info=True)
        return None


def fmt_limit(mn, mx):
    def fmt(n): return f"{int(n):,}".replace(",", " ")
    return f"{fmt(mn)} – {fmt(mx)} UAH" if mx and mx != mn else f"{fmt(mn)} UAH"


# ==================== "МОНІТОР ЗАРАЗ" ДЛЯ CZK / PLN ====================
#
# На відміну від UAH (де бот шукає чужі оголошення і показує їх як звичайний
# тейкер), для CZK/PLN користувач торгує СВОЇМИ ордерами (як мейкер), тому
# мітки купівлі/продажу на екрані інвертовані відносно напрямку tradeType:
#
#   tradeType=BUY  на Binance = "хтось хоче купити USDT" -> для користувача
#                    це означає, що USDT ПАДАЄ йому на Binance, а він платить
#                    валютою (CZK/PLN) -> для нього це фактично "🔴 Продаж"
#                    (він купує USDT за валюту).
#   tradeType=SELL на Binance = "хтось хоче продати USDT" -> для користувача
#                    це "🟢 Покупка" (він продає USDT і отримує валюту).
#
# Тягнемо лише першу сторінку (без банківських фільтрів/блеклиста — це
# швидкий довідковий спред, а не список конкретних оголошень для угоди).

FX_ROWS = 20

def get_fx_top_price(trade_type: str, fiat: str):
    """Найкраща ціна (перша сторінка) для пари USDT/{fiat}. None, якщо не вдалось."""
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    data = {
        "asset": "USDT",
        "fiat": fiat,
        "merchantCheck": False,
        "page": 1, "rows": FX_ROWS,
        "payTypes": [],
        "tradeType": trade_type,
    }
    for attempt in range(MAX_RETRIES):
        try:
            r = session.post(url, json=data, timeout=10)
            if r.status_code == 429:
                wait = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.warning(f"Rate limit Binance P2P ({fiat}/{trade_type}), чекаю {wait}с")
                time.sleep(wait)
                continue
            r.raise_for_status()
            resp = r.json()
            items = resp.get("data") or []
            if not items:
                return None
            adv  = items[0]["adv"]
            user = items[0]["advertiser"]
            return {
                "price":    float(adv["price"]),
                "merchant": user.get("nickName", ""),
            }
        except requests.exceptions.Timeout:
            logger.warning(f"Таймаут ({fiat}/{trade_type}), спроба {attempt+1}/{MAX_RETRIES}")
        except Exception as e:
            logger.warning(f"Помилка {fiat}/{trade_type}: {e}")
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAYS[attempt])
    return None


def build_fx_monitor_text() -> str:
    """Блок спредів по CZK і PLN для '📊 Монітор зараз' (мітки інвертовані)."""
    fiats = [("CZK", "🇨🇿 CZK"), ("PLN", "🇵🇱 PLN")]

    fx_results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for fiat, _ in fiats:
            futures[pool.submit(get_fx_top_price, "BUY",  fiat)] = (fiat, "BUY")
            futures[pool.submit(get_fx_top_price, "SELL", fiat)] = (fiat, "SELL")
        for fut in as_completed(futures):
            fiat, tt = futures[fut]
            try:
                fx_results[(fiat, tt)] = fut.result()
            except Exception as e:
                logger.warning(f"Помилка паралельного fetch fx {fiat}/{tt}: {e}")
                fx_results[(fiat, tt)] = None

    blocks = []
    for fiat, fiat_label in fiats:
        buy_ad  = fx_results.get((fiat, "BUY"))   # -> "🔴 Продаж" для юзера
        sell_ad = fx_results.get((fiat, "SELL"))  # -> "🟢 Покупка" для юзера
        if buy_ad and sell_ad:
            spread = round(abs((sell_ad["price"] - buy_ad["price"]) / buy_ad["price"]) * 100, 2)
            blocks.append(
                f"\n\n{fiat_label}\n{'─'*22}\n"
                f"🔴 Продаж (купую USDT): {buy_ad['price']} {fiat}\n"
                f"🟢 Покупка (продаю USDT): {sell_ad['price']} {fiat}\n"
                f"📊 Спред: {spread}%"
            )
        else:
            blocks.append(f"\n\n{fiat_label}: 🚨 недоступно")

    if not blocks:
        return ""

    return (
        "".join(blocks) +
        "\n\nℹ️ Для CZK/PLN мітки інвертовані: я торгую своїми ордерами, "
        "тому 🔴 Продаж = я купую USDT (плачу валютою), "
        "🟢 Покупка = я продаю USDT (отримую валюту)."
    )


# ==================== ВІДПРАВКА АЛЕРТІВ ====================

def make_alert_markup(merchant: str, trade_type: str, advertiser_no: str, adv_no: str, bank: str = "") -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    url = (
        f"https://p2p.binance.com/en/advertiserDetail?advertiserNo={advertiser_no}"
        if advertiser_no else
        "https://p2p.binance.com/uk-UA/trade/all-payments/USDT?fiat=UAH"
    )
    markup.add(types.InlineKeyboardButton("🔗 Відкрити ордер", url=url))
    key = f"{merchant}::{bank}" if bank else merchant
    markup.add(
        types.InlineKeyboardButton("🚫 Бан BUY (цей банк)",  callback_data=f"bl|BUY|{key}"[:64]),
        types.InlineKeyboardButton("🚫 Бан SELL (цей банк)", callback_data=f"bl|SELL|{key}"[:64]),
    )
    return markup


# ✅ ОНОВЛЕНА ФУНКЦІЯ: тепер приймає balance_usdt і показує прибуток для SELL алертів
def send_alert(chat_id: int, trade_type: str, adv: dict, balance_usdt: float = 0):
    header = f"🟢 ЗАКУП: {adv['price']} ₴" if trade_type == "BUY" else f"🔴 ПРОДАЖА: {adv['price']} ₴"

    # Розрахунок прибутку для SELL алертів
    profit_text = ""
    if balance_usdt > 0 and trade_type == "SELL":
        ms = get_monitor_state(chat_id)
        seen_buy = ms.get("seen_buy")
        if seen_buy:
            best_buy_price = min(seen_buy.values())
            profit = round(balance_usdt * (adv["price"] - best_buy_price), 2)
            profit_sign = "+" if profit >= 0 else ""
            profit_text = f"\n💰 Прибуток: {profit_sign}{profit} UAH"

    bank_label   = adv.get("bank_label", "")
    api_tok_text = "\n🔑 ФОП з API-токеном/ключем monobank" if adv.get("api_token") else ""
    text = (
        f"{header}\n"
        f"{'─'*22}\n"
        f"🏦 Банк: {bank_label}\n"
        f"👤 {adv['merchant']}\n"
        f"💸 Лімит: {fmt_limit(adv['min_limit'], adv['max_limit'])}\n"
        f"📊 Угод: {adv['orders']} | Успіх: {adv['rate']}%"
        f"{api_tok_text}"
        f"{profit_text}"
    )
    markup = make_alert_markup(adv["merchant"], trade_type, adv.get("advertiser_no",""), adv.get("adv_no",""), adv.get("bank",""))
    try:
        bot.send_message(chat_id, text, reply_markup=markup)
    except Exception as e:
        logger.error(f"Помилка надсилання алерту {chat_id}: {e}")


# ==================== МОНІТОРИНГ ====================

def monitor_thread():
    global check_count, api_down
    logger.info("Потік моніторингу запущено")

    while True:
        try:
            with users_lock:
                active_users = [u.copy() for u in users.values() if u.get("active") and u.get("monitoring")]

            if not active_users:
                time.sleep(5)
                continue

            for ud in active_users:
                cid          = ud["chat_id"]
                ms           = get_monitor_state(cid)
                balance_usdt = ud.get("balance_usdt", 0)

                buy_list  = get_binance_p2p("BUY",  ud)
                time.sleep(1)
                sell_list = get_binance_p2p("SELL", ud)

                with state_lock:
                    check_count += 1

                    if buy_list is None and sell_list is None:
                        if not api_down:
                            api_down = True
                            try:
                                bot.send_message(cid,
                                    "🚨 Binance P2P недоступний!\n"
                                    f"{'─'*22}\n"
                                    f"⏱ {datetime.now().strftime('%H:%M:%S')}")
                            except Exception: pass
                        continue

                    if api_down:
                        api_down = False
                        try:
                            bot.send_message(cid, "✅ Binance P2P знову доступний!")
                        except Exception: pass

                    buy  = buy_list[0]  if buy_list  else None
                    sell = sell_list[0] if sell_list else None

                    if buy and sell:
                        spread_pct = round(((sell["price"] - buy["price"]) / buy["price"]) * 100, 3)
                        price_history.append((datetime.now(), buy["price"], sell["price"], spread_pct))

                        if ms["last_spread"] is not None:
                            delta = abs(spread_pct - ms["last_spread"])
                            if delta >= 1.0:
                                direction = "📈 виріс" if spread_pct > ms["last_spread"] else "📉 впав"
                                try:
                                    bot.send_message(cid,
                                        f"⚡️ Скачок спреду {direction}!\n"
                                        f"{'─'*22}\n"
                                        f"Було: {ms['last_spread']}%  →  Стало: {spread_pct}%\n"
                                        f"🟢 BUY: {buy['price']} ₴ | 🔴 SELL: {sell['price']} ₴")
                                except Exception: pass
                        ms["last_spread"] = spread_pct

                    new_seen_buy = {}
                    if buy_list:
                        for adv in buy_list:
                            if adv["price"] <= ud["buy_threshold"]:
                                m = adv["merchant"]
                                p = adv["price"]
                                new_seen_buy[m] = p
                                if ms["seen_buy"] is None or m not in ms["seen_buy"] or ms["seen_buy"][m] != p:
                                    # ✅ передаємо balance_usdt в send_alert
                                    send_alert(cid, "BUY", adv, balance_usdt)
                                    time.sleep(0.3)
                    ms["seen_buy"] = new_seen_buy

                    new_seen_sell = {}
                    if sell_list:
                        for adv in sell_list:
                            if adv["price"] >= ud["sell_threshold"]:
                                m = adv["merchant"]
                                p = adv["price"]
                                new_seen_sell[m] = p
                                if ms["seen_sell"] is None or m not in ms["seen_sell"] or ms["seen_sell"][m] != p:
                                    # ✅ передаємо balance_usdt в send_alert
                                    send_alert(cid, "SELL", adv, balance_usdt)
                                    time.sleep(0.3)
                    ms["seen_sell"] = new_seen_sell

                time.sleep(ud.get("check_interval", 20))

        except Exception as e:
            logger.error(f"Критична помилка в monitor_thread: {e}", exc_info=True)
            time.sleep(10)


# ==================== АВТО-АЛЕРТ ПО СПРЕДУ CZK ====================

def czk_alert_thread():
    """Окремий фоновий потік: раз на CZK_CHECK_INTERVAL сек рахує спред
    USDT/CZK і шле адміну алерт, якщо спред перевищив поріг (за замовчуванням
    2.5%, налаштовується адміном через меню). Щоб не спамити на кожній
    ітерації, алерт шлеться лише в момент ПЕРЕТИНУ порогу знизу вгору;
    повторний алерт можливий тільки після того, як спред знову опуститься
    нижче порогу і потім знову підніметься."""
    global czk_alert_active
    logger.info("Потік CZK-алертів запущено")

    while True:
        try:
            admin = get_user(ADMIN_ID)
            threshold = (admin or {}).get("czk_spread_threshold", 2.5)

            buy_ad  = get_fx_top_price("BUY",  "CZK")   # -> "🔴 Продаж" для юзера
            sell_ad = get_fx_top_price("SELL", "CZK")   # -> "🟢 Покупка" для юзера

            if buy_ad and sell_ad and buy_ad["price"]:
                spread = round(abs((sell_ad["price"] - buy_ad["price"]) / buy_ad["price"]) * 100, 2)

                with czk_alert_lock:
                    if spread >= threshold and not czk_alert_active:
                        czk_alert_active = True
                        try:
                            bot.send_message(
                                ADMIN_ID,
                                f"🇨🇿 Спред на CZK перевищив поріг!\n{'─'*22}\n"
                                f"📊 Спред: {spread}% (поріг {threshold}%)\n"
                                f"🔴 Продаж (купую USDT): {buy_ad['price']} CZK\n"
                                f"🟢 Покупка (продаю USDT): {sell_ad['price']} CZK")
                        except Exception:
                            pass
                    elif spread < threshold and czk_alert_active:
                        czk_alert_active = False

        except Exception as e:
            logger.error(f"Критична помилка в czk_alert_thread: {e}", exc_info=True)

        time.sleep(CZK_CHECK_INTERVAL)


# ==================== МЕНЮ ====================

def send_main_menu(chat_id: int):
    ud = get_user(chat_id)
    if not ud:
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    status_btn = "⏹ Зупинити" if ud.get("monitoring") else "▶️ Запустити"
    markup.add(types.KeyboardButton(status_btn),            types.KeyboardButton("📊 Монітор зараз"))
    markup.add(types.KeyboardButton("📉 Поріг покупки"),    types.KeyboardButton("📈 Поріг продажу"))
    markup.add(types.KeyboardButton("💰 Моя сума BUY"),     types.KeyboardButton("💰 Моя сума SELL"))
    markup.add(types.KeyboardButton("💰 Сума SELL Paysend"))
    markup.add(types.KeyboardButton("💎 Баланс USDT"),      types.KeyboardButton("⏱ Інтервал"))
    markup.add(types.KeyboardButton("🚫 Блеклист BUY"),     types.KeyboardButton("🚫 Блеклист SELL"))
    markup.add(types.KeyboardButton("🏦 Банки"),            types.KeyboardButton("📋 Статус"))

    if is_admin(chat_id):
        markup.add(types.KeyboardButton("🇨🇿 Поріг CZK алерту"))
        markup.add(types.KeyboardButton("👥 Адмін панель"))

    balance_text = f"{ud['balance_usdt']} USDT" if ud["balance_usdt"] > 0 else "вимкнено"
    eb = get_enabled_banks(ud)
    banks_text = ", ".join(
        f"{BANK_LABELS[k]} {'✅' if eb.get(k, True) else '❌'}" for k in BANK_ORDER
    )
    bot.send_message(
        chat_id,
        f"⚙️ Налаштування\n"
        f"{'─'*22}\n"
        f"🟢 Закуп до: {ud['buy_threshold']} ₴\n"
        f"🔴 Продаж від: {ud['sell_threshold']} ₴\n"
        f"💵 Моя сума BUY: {ud['min_amount_uah']} UAH\n"
        f"💵 Моя сума SELL: {ud['min_amount_sell']} UAH\n"
        f"💵 Сума SELL Paysend: {ud.get('min_amount_sell_paysend', ud['min_amount_sell'])} UAH\n"
        f"💎 Баланс USDT: {balance_text}\n"
        f"⏱ Інтервал: {ud['check_interval']} сек\n"
        f"🏦 Банки: {banks_text}",
        reply_markup=markup,
    )


# ==================== АДМІН ПАНЕЛЬ ====================

def send_admin_panel(chat_id: int):
    with users_lock:
        all_users = list(users.values())

    if not all_users:
        bot.send_message(chat_id, "👥 Немає зареєстрованих юзерів")
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for u in all_users:
        cid   = u["chat_id"]
        name  = u.get("first_name") or u.get("username") or str(cid)
        uname = f"@{u['username']}" if u.get("username") else f"ID:{cid}"
        status_icon = "✅" if u.get("active") else "🚫"
        mon_icon    = "▶️" if u.get("monitoring") else "⏹"
        label = f"{status_icon}{mon_icon} {name} ({uname})"
        markup.add(types.InlineKeyboardButton(label, callback_data=f"adm_user|{cid}"))

    bot.send_message(
        chat_id,
        f"👥 Адмін панель\n"
        f"{'─'*22}\n"
        f"Всього юзерів: {len(all_users)}\n"
        f"✅ = доступ є  |  🚫 = заблокований\n"
        f"▶️ = моніторинг ON  |  ⏹ = OFF\n\n"
        f"Натисни на юзера для керування:",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_user|"))
def handle_admin_user(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Тільки для адміна")
        return

    cid = int(call.data.split("|")[1])
    u   = get_user(cid)
    if not u:
        bot.answer_callback_query(call.id, "❌ Юзер не знайдений")
        return

    name   = u.get("first_name") or u.get("username") or str(cid)
    uname  = f"@{u['username']}" if u.get("username") else f"ID:{cid}"
    active = u.get("active", False)
    mon    = u.get("monitoring", False)

    text = (
        f"👤 {name} ({uname})\n"
        f"{'─'*22}\n"
        f"Доступ: {'✅ Активний' if active else '🚫 Заблокований'}\n"
        f"Моніторинг: {'▶️ Запущено' if mon else '⏹ Зупинено'}\n"
        f"🟢 BUY поріг: {u['buy_threshold']} ₴\n"
        f"🔴 SELL поріг: {u['sell_threshold']} ₴\n"
        f"💵 Сума BUY: {u['min_amount_uah']} UAH\n"
        f"💵 Сума SELL: {u['min_amount_sell']} UAH\n"
        f"💵 Сума SELL Paysend: {u.get('min_amount_sell_paysend', u['min_amount_sell'])} UAH\n"
        f"🚫 Блеклист BUY: {len(u.get('blacklist_buy', []))}\n"
        f"🚫 Блеклист SELL: {len(u.get('blacklist_sell', []))}\n"
        f"📅 Реєстрація: {u.get('registered_at','')[:10]}"
    )

    toggle_label = "🚫 Заблокувати" if active else "✅ Дати доступ"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(toggle_label, callback_data=f"adm_toggle|{cid}"),
        types.InlineKeyboardButton("◀️ Назад",   callback_data="adm_back"),
    )

    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_toggle|"))
def handle_admin_toggle(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Тільки для адміна")
        return

    cid    = int(call.data.split("|")[1])
    u      = get_user(cid)
    if not u:
        bot.answer_callback_query(call.id, "❌ Юзер не знайдений")
        return

    new_active = not u.get("active", False)
    update_user_field(cid, "active", new_active)

    if new_active:
        bot.answer_callback_query(call.id, f"✅ Доступ надано")
        try:
            bot.send_message(cid, "✅ Адмін надав тобі доступ до бота!\nНатисни /start щоб почати.")
        except Exception: pass
    else:
        bot.answer_callback_query(call.id, f"🚫 Доступ заблоковано")
        try:
            bot.send_message(cid, "🚫 Твій доступ до бота заблоковано адміном.")
        except Exception: pass

    send_admin_panel(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_back")
def handle_admin_back(call):
    if not is_admin(call.from_user.id):
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    send_admin_panel(call.message.chat.id)
    bot.answer_callback_query(call.id)


# ==================== БАНКИ: МЕНЮ + CALLBACK ====================

def _banks_markup(eb: dict) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key in BANK_ORDER:
        icon = "✅" if eb.get(key, True) else "❌"
        markup.add(types.InlineKeyboardButton(f"{icon} {BANK_LABELS[key]}", callback_data=f"bank_toggle|{key}"))
    markup.add(types.InlineKeyboardButton("✔️ Готово", callback_data="bank_done"))
    return markup


def send_banks_menu(chat_id: int):
    ud = get_user(chat_id)
    if not ud:
        return
    eb = get_enabled_banks(ud)
    bot.send_message(
        chat_id,
        f"🏦 Банки для моніторингу\n{'─'*22}\n"
        f"✅ — увімкнено, алерти по цьому банку приходять\n"
        f"❌ — вимкнено, оголошення цього банку ігноруються\n\n"
        f"Натисни на банк щоб перемкнути:",
        reply_markup=_banks_markup(eb)
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("bank_toggle|"))
def handle_bank_toggle(call):
    cid = call.from_user.id
    if not is_active(cid):
        bot.answer_callback_query(call.id, "❌ Немає доступу")
        return
    try:
        key = call.data.split("|", 1)[1]
        ud  = get_user(cid)
        eb  = get_enabled_banks(ud)
        new_val = not eb.get(key, True)

        # запобіжник: хоча б один банк має лишатись увімкненим
        if not new_val and sum(1 for v in eb.values() if v) <= 1 and eb.get(key, True):
            bot.answer_callback_query(call.id, "⚠️ Хоча б один банк має бути увімкнений")
            return

        eb[key] = new_val
        update_user_field(cid, "enabled_banks", eb)

        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=_banks_markup(eb))
        except Exception: pass
        bot.answer_callback_query(call.id, f"{BANK_LABELS[key]}: {'✅ увімкнено' if new_val else '❌ вимкнено'}")
    except Exception as e:
        logger.error(f"Помилка bank_toggle: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "❌ Помилка")


@bot.callback_query_handler(func=lambda call: call.data == "bank_done")
def handle_bank_done(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    bot.answer_callback_query(call.id, "✅ Збережено")


# ==================== БЛЕКЛИСТ: CALLBACK ====================

@bot.callback_query_handler(func=lambda call: call.data.startswith("bl|"))
def handle_blacklist_callback(call):
    cid = call.from_user.id
    try:
        if not is_active(cid):
            bot.answer_callback_query(call.id, "❌ Немає доступу")
            return

        _, trade_type, merchant = call.data.split("|", 2)
        added = add_to_blacklist(cid, merchant, trade_type)
        label = "BUY 🟢" if trade_type == "BUY" else "SELL 🔴"
        disp  = blacklist_display_name(merchant)
        if added:
            try:
                original_text = call.message.text or ""
                bot.edit_message_text(
                    original_text + f"\n\n🚫 Заблоковано для {label}",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=None
                )
            except Exception: pass
            bot.answer_callback_query(call.id, f"✅ {disp} заблоковано для {label}")
        else:
            bot.answer_callback_query(call.id, f"⚠️ {disp} вже в блеклісті")
    except Exception as e:
        logger.error(f"Помилка handle_blacklist_callback: {e}", exc_info=True)
        # Гарантуємо, що Telegram завжди отримає відповідь на callback,
        # інакше кнопка "висить" з крутилкою нескінченно.
        try:
            bot.answer_callback_query(call.id, "❌ Помилка, дивись bot.log")
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("unbl"))
def handle_unblacklist_callback(call):
    cid = call.from_user.id
    if not is_active(cid):
        bot.answer_callback_query(call.id, "❌ Немає доступу")
        return
    try:
        if call.data.startswith("unbl_all|"):
            trade_type = call.data.split("|")[1]
            clear_blacklist(cid, trade_type)
            label = "BUY 🟢" if trade_type == "BUY" else "SELL 🔴"
            bot.answer_callback_query(call.id, f"✅ Блеклист {label} очищено")
            try:
                bot.edit_message_text(f"🚫 Блеклист {label} очищено ✅",
                    chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
            except Exception: pass
            return

        _, trade_type, merchant = call.data.split("|", 2)
        removed = remove_from_blacklist(cid, merchant, trade_type)
        label = "BUY 🟢" if trade_type == "BUY" else "SELL 🔴"
        disp  = blacklist_display_name(merchant)

        if removed:
            bot.answer_callback_query(call.id, f"✅ {disp} видалено")
        else:
            bot.answer_callback_query(call.id, f"⚠️ {disp} не знайдено")

        _show_blacklist(cid, trade_type, call.message)

    except Exception as e:
        logger.error(f"Помилка unblacklist: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "❌ Помилка")


def _show_blacklist(chat_id: int, trade_type: str, message=None):
    u  = get_user(chat_id)
    field = "blacklist_buy" if trade_type == "BUY" else "blacklist_sell"
    bl_copy = sorted(u.get(field, []))
    label = "BUY 🟢" if trade_type == "BUY" else "SELL 🔴"

    if not bl_copy:
        if message:
            try:
                bot.edit_message_text(f"🚫 Блеклист {label} порожній",
                    chat_id=message.chat.id, message_id=message.message_id, reply_markup=None)
            except Exception:
                bot.send_message(chat_id, f"🚫 Блеклист {label} порожній")
        else:
            bot.send_message(chat_id, f"🚫 Блеклист {label} порожній")
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for nick in bl_copy:
        markup.add(types.InlineKeyboardButton(f"❌ {blacklist_display_name(nick)}", callback_data=f"unbl|{trade_type}|{nick}"))
    markup.add(types.InlineKeyboardButton(f"🗑 Очистити весь блеклист {label}", callback_data=f"unbl_all|{trade_type}"))

    text = (f"🚫 Блеклист {label} ({len(bl_copy)} мерчантів):\n"
            f"{'─'*22}\nНатисни ❌ поруч з ніком — щоб видалити.")

    if message:
        try:
            bot.edit_message_text(text, chat_id=message.chat.id, message_id=message.message_id, reply_markup=markup)
        except Exception:
            bot.send_message(chat_id, text, reply_markup=markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)


# ==================== ОНОВЛЕННЯ НАЛАШТУВАНЬ ====================

def update_val(m, param):
    cid = m.chat.id
    ud  = get_user(cid)
    if not ud:
        return
    try:
        v = float(m.text.replace(",", "."))
        if v < 0:
            raise ValueError("Значення не може бути від'ємним")
        if param != "balance" and v == 0:
            raise ValueError("Значення повинно бути більше 0")

        if param == "buy":
            update_user_field(cid, "buy_threshold", v)
            bot.send_message(cid, f"✅ Поріг закупу: {v} ₴")
        elif param == "sell":
            update_user_field(cid, "sell_threshold", v)
            bot.send_message(cid, f"✅ Поріг продажу: {v} ₴")
        elif param == "min_buy":
            update_user_field(cid, "min_amount_uah", int(v))
            bot.send_message(cid, f"✅ Мін. сума BUY: {int(v)} UAH")
        elif param == "min_sell":
            update_user_field(cid, "min_amount_sell", int(v))
            bot.send_message(cid, f"✅ Мін. сума SELL: {int(v)} UAH")
        elif param == "min_sell_paysend":
            update_user_field(cid, "min_amount_sell_paysend", int(v))
            bot.send_message(cid, f"✅ Мін. сума SELL Paysend: {int(v)} UAH")
        elif param == "balance":
            update_user_field(cid, "balance_usdt", v)
            if v == 0:
                bot.send_message(cid, "✅ Фільтр по балансу вимкнено")
            else:
                bot.send_message(cid, f"✅ Баланс: {v} USDT\n💡 Тепер бот фільтрує ордери де ліміт менший за твою суму, і показує потенційний прибуток в алертах.")
        elif param == "interval":
            if v < 5:
                raise ValueError("Мінімальний інтервал — 5 сек")
            update_user_field(cid, "check_interval", int(v))
            bot.send_message(cid, f"✅ Інтервал: {int(v)} сек")
        elif param == "czk_threshold":
            if not is_admin(cid):
                raise ValueError("Тільки для адміна")
            update_user_field(cid, "czk_spread_threshold", v)
            bot.send_message(cid, f"✅ Поріг алерту по CZK: {v}%")

    except ValueError as e:
        bot.send_message(cid, f"❌ Помилка: {e}")
    except Exception as e:
        logger.error(f"Помилка update_val ({param}): {e}", exc_info=True)
        bot.send_message(cid, "❌ Щось пішло не так")

    send_main_menu(cid)


# ==================== ОБРОБНИК ПОВІДОМЛЕНЬ ====================

@bot.message_handler(commands=["start"])
def handle_start(message):
    cid   = message.chat.id
    uname = message.from_user.username or ""
    fname = message.from_user.first_name or ""
    ud    = upsert_user(cid, uname, fname)

    if is_admin(cid) and not ud.get("active"):
        update_user_field(cid, "active", True)
        ud = get_user(cid)

    if not ud.get("active"):
        bot.send_message(cid,
            "👋 Привіт! Ти зареєстрований.\n"
            "⏳ Очікуй поки адмін надасть тобі доступ.")
        try:
            name_disp = fname or uname or str(cid)
            bot.send_message(ADMIN_ID,
                f"🔔 Новий юзер хоче доступ:\n"
                f"👤 {name_disp} (@{uname})\n"
                f"🆔 ID: {cid}\n\n"
                f"Відкрий 👥 Адмін панель щоб дати доступ.")
        except Exception: pass
        return

    send_main_menu(cid)


@bot.message_handler(func=lambda message: True)
def handle_commands(message):
    cid  = message.chat.id
    text = message.text
    uname = message.from_user.username or ""
    fname = message.from_user.first_name or ""

    upsert_user(cid, uname, fname)

    if is_admin(cid):
        update_user_field(cid, "active", True)

    if not is_active(cid):
        bot.send_message(cid, "🚫 Немає доступу. Напиши /start і чекай підтвердження адміна.")
        return

    try:
        _handle(message, text)
    except Exception as e:
        logger.error(f"Помилка обробки '{text}': {e}", exc_info=True)
        bot.send_message(cid, f"❌ Помилка: {e}")


def _handle(message, text):
    cid = message.chat.id
    ud  = get_user(cid)
    if not ud:
        return

    known = [
        "▶️ Запустити", "⏹ Зупинити", "📊 Монітор зараз",
        "📋 Статус", "📉 Поріг покупки", "📈 Поріг продажу",
        "💰 Моя сума BUY", "💰 Моя сума SELL", "💰 Сума SELL Paysend", "💎 Баланс USDT",
        "⏱ Інтервал", "🚫 Блеклист BUY", "🚫 Блеклист SELL",
        "🏦 Банки", "👥 Адмін панель", "🇨🇿 Поріг CZK алерту"
    ]

    if text not in known:
        send_main_menu(cid)
        return

    if text in ["▶️ Запустити", "⏹ Зупинити"]:
        new_mon = not ud.get("monitoring", False)
        update_user_field(cid, "monitoring", new_mon)
        state = "▶️ запущено" if new_mon else "⏹ зупинено"
        bot.send_message(cid, f"Моніторинг {state}")
        send_main_menu(cid)

    elif text == "📊 Монітор зараз":
        ud_fresh = get_user(cid)
        buy_list  = get_binance_p2p("BUY",  ud_fresh)
        sell_list = get_binance_p2p("SELL", ud_fresh)
        buy  = buy_list[0]  if buy_list  else None
        sell = sell_list[0] if sell_list else None
        if buy and sell:
            spread = round(((sell["price"] - buy["price"]) / buy["price"]) * 100, 2)
            now = datetime.now()
            hour_history = [(t,b,s,sp) for t,b,s,sp in price_history if (now-t).total_seconds() <= 3600]
            history_text = ""
            if hour_history:
                history_text = (
                    f"\n\n📈 За останню годину:\n{'─'*22}\n"
                    f"🟢 BUY:  {min(x[1] for x in hour_history)} / {max(x[1] for x in hour_history)} ₴\n"
                    f"🔴 SELL: {min(x[2] for x in hour_history)} / {max(x[2] for x in hour_history)} ₴\n"
                    f"📊 Спред: {min(x[3] for x in hour_history)}% / {max(x[3] for x in hour_history)}%"
                )

            # ✅ НОВИЙ БЛОК: розрахунок потенційного прибутку
            balance = ud_fresh.get("balance_usdt", 0)
            profit_text = ""
            if balance > 0:
                spread_uah   = round(sell["price"] - buy["price"], 2)
                profit_uah   = round(balance * spread_uah, 2)
                profit_sign  = "+" if profit_uah >= 0 else ""
                profit_text  = (
                    f"\n\n💰 Потенційний прибуток:\n{'─'*22}\n"
                    f"{balance} USDT × {spread_uah} ₴ спреду\n"
                    f"= {profit_sign}{profit_uah} UAH"
                )

            fx_text = build_fx_monitor_text()

            bot.send_message(cid,
                f"📊 Поточні ціни\n{'─'*22}\n"
                f"🟢 BUY:  {buy['price']} ₴  ({buy['merchant']}, {buy['bank_label']})\n"
                f"🔴 SELL: {sell['price']} ₴  ({sell['merchant']}, {sell['bank_label']})\n"
                f"📊 Спред: {spread}%"
                f"{profit_text}"
                f"{history_text}"
                f"{fx_text}")
        else:
            bot.send_message(cid, "🚨 Binance P2P недоступний")

    elif text == "📋 Статус":
        uptime = datetime.now() - start_time
        h, rem = divmod(int(uptime.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        with state_lock:
            cnt = check_count
        ud_fresh = get_user(cid)
        bl_buy  = len(ud_fresh.get("blacklist_buy",  []))
        bl_sell = len(ud_fresh.get("blacklist_sell", []))
        balance = ud_fresh.get("balance_usdt", 0)
        balance_text = f"{balance} USDT" if balance > 0 else "вимкнено"
        eb = get_enabled_banks(ud_fresh)
        banks_text = ", ".join(
            f"{BANK_LABELS[k]} {'✅' if eb.get(k, True) else '❌'}" for k in BANK_ORDER
        )
        bot.send_message(cid,
            f"📋 Статус бота\n{'─'*22}\n"
            f"🔄 Моніторинг: {'✅ Активний' if ud_fresh.get('monitoring') else '⏹ Зупинений'}\n"
            f"🌐 Binance API: {'🚨 Недоступний' if api_down else '✅ Онлайн'}\n"
            f"⏱ Аптайм: {h}г {m}хв {s}с\n"
            f"🔢 Перевірок: {cnt}\n"
            f"📊 Спред зараз: {get_monitor_state(cid).get('last_spread') or '—'}\n"
            f"🟢 Поріг закупу: {ud_fresh['buy_threshold']} ₴\n"
            f"🔴 Поріг продажу: {ud_fresh['sell_threshold']} ₴\n"
            f"💵 Сума BUY: {ud_fresh['min_amount_uah']} UAH\n"
            f"💵 Сума SELL: {ud_fresh['min_amount_sell']} UAH\n"
            f"💵 Сума SELL Paysend: {ud_fresh.get('min_amount_sell_paysend', ud_fresh['min_amount_sell'])} UAH\n"
            f"💎 Баланс USDT: {balance_text}\n"
            f"🏦 Банки: {banks_text}\n"
            f"🚫 Блеклист BUY: {bl_buy} мерчантів\n"
            f"🚫 Блеклист SELL: {bl_sell} мерчантів")

    elif text == "🚫 Блеклист BUY":
        _show_blacklist(cid, "BUY")

    elif text == "🚫 Блеклист SELL":
        _show_blacklist(cid, "SELL")

    elif text == "📉 Поріг покупки":
        ud_fresh = get_user(cid)
        msg = bot.send_message(cid, f"🟢 Поточний поріг: {ud_fresh['buy_threshold']} ₴\nВведи нове значення:")
        bot.register_next_step_handler(msg, lambda m: update_val(m, "buy"))

    elif text == "📈 Поріг продажу":
        ud_fresh = get_user(cid)
        msg = bot.send_message(cid, f"🔴 Поточний поріг: {ud_fresh['sell_threshold']} ₴\nВведи нове значення:")
        bot.register_next_step_handler(msg, lambda m: update_val(m, "sell"))

    elif text == "💰 Моя сума BUY":
        ud_fresh = get_user(cid)
        msg = bot.send_message(cid, f"💵 Моя сума BUY: {ud_fresh['min_amount_uah']} UAH\nВведи суму:")
        bot.register_next_step_handler(msg, lambda m: update_val(m, "min_buy"))

    elif text == "💰 Моя сума SELL":
        ud_fresh = get_user(cid)
        msg = bot.send_message(cid, f"💵 Моя сума SELL: {ud_fresh['min_amount_sell']} UAH\nВведи суму:")
        bot.register_next_step_handler(msg, lambda m: update_val(m, "min_sell"))

    elif text == "💰 Сума SELL Paysend":
        ud_fresh = get_user(cid)
        cur = ud_fresh.get("min_amount_sell_paysend", ud_fresh["min_amount_sell"])
        msg = bot.send_message(cid,
            f"💵 Сума SELL Paysend: {cur} UAH\n"
            f"Введи суму (діє тільки для ордерів, де в умовах/оплаті згадано Paysend):")
        bot.register_next_step_handler(msg, lambda m: update_val(m, "min_sell_paysend"))

    elif text == "💎 Баланс USDT":
        ud_fresh = get_user(cid)
        current = f"{ud_fresh['balance_usdt']} USDT" if ud_fresh["balance_usdt"] > 0 else "вимкнено"
        msg = bot.send_message(cid,
            f"💎 Поточний баланс: {current}\n"
            f"Введи баланс в USDT (0 — вимкнути):\n\n"
            f"💡 Якщо задати баланс:\n"
            f"• Бот покаже потенційний прибуток в 📊 Монітор зараз\n"
            f"• В SELL алертах побачиш скільки заробиш\n"
            f"• Бот відфільтрує ордери де ліміт менший за твою суму")
        bot.register_next_step_handler(msg, lambda m: update_val(m, "balance"))

    elif text == "⏱ Інтервал":
        ud_fresh = get_user(cid)
        msg = bot.send_message(cid, f"⏱ Поточний інтервал: {ud_fresh['check_interval']} сек\nВведи нове значення (сек):")
        bot.register_next_step_handler(msg, lambda m: update_val(m, "interval"))

    elif text == "🇨🇿 Поріг CZK алерту":
        if not is_admin(cid):
            bot.send_message(cid, "❌ Тільки для адміна")
            return
        ud_fresh = get_user(cid)
        cur = ud_fresh.get("czk_spread_threshold", 2.5)
        msg = bot.send_message(cid,
            f"🇨🇿 Поточний поріг алерту по CZK: {cur}%\n"
            f"Введи нове значення у % (наприклад 2.5):")
        bot.register_next_step_handler(msg, lambda m: update_val(m, "czk_threshold"))

    elif text == "🏦 Банки":
        send_banks_menu(cid)

    elif text == "👥 Адмін панель":
        if not is_admin(cid):
            bot.send_message(cid, "❌ Тільки для адміна")
            return
        send_admin_panel(cid)


# ==================== СТАРТ ====================

if __name__ == "__main__":
    logger.info("Бот запускається (мультикористувацький режим)...")
    threading.Thread(target=monitor_thread, daemon=True).start()
    threading.Thread(target=czk_alert_thread, daemon=True).start()
    try:
        bot.remove_webhook()
        logger.info("Webhook знято (якщо був встановлений)")
    except Exception as e:
        logger.warning(f"Не вдалось знімати webhook (можливо, і не було потреби): {e}")

    logger.info("Polling запущено")
    bot.infinity_polling()