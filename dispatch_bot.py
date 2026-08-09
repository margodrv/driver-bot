import os
import re
import json
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# ---------------------------------------------------------------------------
# Настройки из переменных окружения (задаются в Railway)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
SHEET_TAB_NAME = os.environ.get("SHEET_TAB_NAME", "Orders clean")
LOG_SHEET_TAB_NAME = os.environ.get("LOG_SHEET_TAB_NAME", "Log")
TARIFF_CORRECTIONS_TAB_NAME = os.environ.get("TARIFF_CORRECTIONS_TAB_NAME", "Tariff_corrections")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WEBHOOK_PORT = int(os.environ.get("PORT", "8080"))

_openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ---------------------------------------------------------------------------
# Группы, где бот сейчас активен
# ---------------------------------------------------------------------------
# Группы логистов, где реально приходят заявки (те же 2, что слушает
# parsing-bot) - здесь и только здесь работает тариф-превью (Сцена 2).
LOGIST_CHAT_IDS = {
    "-1003477719320",  # Газели диспетчерская
    "-1003404979004",  # 5-10т Диспетчера
}

# Тестовая группа для Сцены 3 (рассылка карточек заказов водителям, ещё не
# построена). Пока карточки не рассылаются никому - это заглушка на
# будущее: когда Сцену 3 сделаем, все заказы Кужеля временно пойдут сюда
# вместо его личной группы, пока не попросят переключить обратно.
MONITOR_LOGISTOV_CHAT_ID = "-1003490954823"

# Группы водителей - бот в них уже добавлен участником, но пока молчит.
# Подключаем по одной вручную по мере готовности (перенос id сюда).
# Пока пусто - карточки заказов никому не шлются, это Часть 2.2 (roadmap).
ACTIVE_DRIVER_CHAT_IDS = {
    # "-1003633789888",  # Кужель - но реально пойдёт в MONITOR_LOGISTOV_CHAT_ID для теста
}

# Полный справочник групп водителей - здесь просто для памяти/копипаста,
# когда будем переключать по одному. Код эти id не использует, пока они
# не окажутся в ACTIVE_DRIVER_CHAT_IDS выше.
ALL_DRIVER_CHAT_IDS = {
    "Ермаков": "-1003613299776",
    "Кислый Андрей": "-1003765728028",
    "Филипчук": "-1004480475853",
    "Щербань": "-1003570558979",
    "Резерв 2": "-1004204490386",
    "Токовенко": "-1004373204695",
    "Конюхов": "-1004445678396",
    "Кагало": "-1003405851331",
    "Опанасенко": "-1003382134219",
    "Рязанцев": "-1003616223181",
    "Кислый Олег": "-1004393221399",
    "Рыбянец": "-1003614374523",
    "Притыченко": "-1003583160825",
    "Хомлюк": "-1003675483505",
    "Остахов": "-1003335384767",
    "Кугитко": "-1003450690810",
    "Дзядзьо": "-1003506108769",
    "Скорый": "-1003689810880",
    "Редько": "-1004487553227",
    "Трутень": "-1003568744324",
    "Кислый Игорь": "-1003820499678",
    "Макаренко": "-1003590538356",
    "Кужель": "-1003633789888",
    "Степанов": "-1003646914106",
    "Канарян": "-1003867486314",
    "Собчук": "-1003697546846",
}

# Колонки Orders clean, общие с parsing-bot (1-indexed, как в Google Sheets)
COL_J_TEXT = 10       # J - Текст заявки
COL_S_MESSAGE_ID = 19  # S - message_ID заявки-источника
COL_T_CHAT_ID = 20      # T - Chat ID заявки-источника
COL_U_KEY = 21           # U - Z = key

# Названия тарифных колонок - ищутся по заголовку, не по номеру (см.
# ensure_tariff_columns). Порядок здесь не важен для кода, важен только
# для читаемости.
TARIFF_COLUMN_NAMES = [
    "Авто_база",
    "Мин_часов",
    "Авто_доп_час",
    "Грузчики_база",
    "Грузчики_доп_час",
    "Ком_авто",
    "Ком_грузчики",
    "Плательщик",
    "Тип_расчёта",
    "Форма_оплаты",
    "Тариф_JSON",
]


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
def _client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=_SCOPES)
    return gspread.authorize(creds)


def get_sheet():
    return _client().open_by_key(GOOGLE_SHEET_ID).worksheet(SHEET_TAB_NAME)


def get_log_sheet():
    spreadsheet = _client().open_by_key(GOOGLE_SHEET_ID)
    try:
        return spreadsheet.worksheet(LOG_SHEET_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=LOG_SHEET_TAB_NAME, rows=2000, cols=6)
        sheet.append_row(["Время", "Тип", "Событие", "Chat ID", "message_ID", "Строка"])
        return sheet


def log_event(text: str, chat_id="", message_id="", row=""):
    try:
        get_log_sheet().append_row(
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "EVENT",
                text,
                str(chat_id),
                str(message_id),
                str(row),
            ]
        )
    except Exception as e:
        logger.error(f"Не удалось записать в Log: {e}")


def get_tariff_corrections_sheet():
    spreadsheet = _client().open_by_key(GOOGLE_SHEET_ID)
    try:
        return spreadsheet.worksheet(TARIFF_CORRECTIONS_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=TARIFF_CORRECTIONS_TAB_NAME, rows=2000, cols=5)
        sheet.append_row(["Время", "Order_key", "Поле", "Было", "Стало"])
        return sheet


def log_field_correction(order_key: str, field_label: str, old_value: str, new_value: str):
    """Логирует точечную правку конкретного поля тарифа - сырьё для
    будущих few-shot примеров в промпте (см. обсуждение обучения)."""
    try:
        get_tariff_corrections_sheet().append_row(
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                order_key,
                field_label,
                old_value,
                new_value,
            ]
        )
    except Exception as e:
        logger.error(f"Не удалось записать исправление тарифа в {TARIFF_CORRECTIONS_TAB_NAME}: {e}")


def find_row_by_key(sheet, key: str):
    try:
        cell = sheet.find(key, in_column=COL_U_KEY)
    except gspread.exceptions.CellNotFound:
        return None
    if cell is None:
        return None
    return cell.row


# Кэш индексов тарифных колонок на процесс - сбрасывается перезапуском
# сервиса (обычное дело на Railway, см. известные особенности). Если кто-то
# вручную поменяет порядок/названия колонок в Sheets, потребуется рестарт
# сервиса, чтобы кэш обновился - это тот же принцип, что уже применяется
# в info_bot и других ботах экосистемы.
_tariff_col_cache = {}


def ensure_tariff_columns(sheet) -> dict:
    """Сканирует строку заголовков и возвращает {имя_колонки: индекс}.

    Никогда не хардкодим номера тарифных колонок - таблица уже минимум
    трижды ловила баги из-за сдвига колонок при вставке новых (см. Key
    learnings). Если колонка из TARIFF_COLUMN_NAMES не найдена в
    заголовках - явно логируем ошибку, а не тихо падаем дальше.
    """
    global _tariff_col_cache
    if _tariff_col_cache:
        return _tariff_col_cache

    header_row = sheet.row_values(1)
    result = {}
    missing = []
    for name in TARIFF_COLUMN_NAMES:
        try:
            idx = header_row.index(name) + 1  # 1-indexed для gspread
            result[name] = idx
        except ValueError:
            missing.append(name)

    if missing:
        msg = f"Тарифные колонки не найдены в заголовке 'Orders clean': {missing}"
        logger.error(msg)
        log_event(msg)
        raise RuntimeError(msg)

    _tariff_col_cache = result
    logger.info(f"Тарифные колонки резолвлены: {result}")
    return result


def col_letter(col_idx: int) -> str:
    letter = ""
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letter = chr(65 + rem) + letter
    return letter


_author_col_cache = None


_vehicle_type_col_cache = None


def ensure_vehicle_type_column(sheet):
    """Ищет колонку с типом транспорта ('4.Тип транспорта (3т/5т/10т/ГБ/
    будка)') по подстроке в заголовке, не по точному совпадению - в
    реальном листе текст заголовка длинный и может переноситься на
    несколько строк внутри ячейки."""
    global _vehicle_type_col_cache
    if _vehicle_type_col_cache is not None:
        return _vehicle_type_col_cache
    header_row = sheet.row_values(1)
    for i, h in enumerate(header_row, start=1):
        if "транспорт" in h.lower():
            _vehicle_type_col_cache = i
            return i
    _vehicle_type_col_cache = False
    return None


def default_min_hours_for_vehicle(vehicle_type_text: str):
    """Правило базовых часов (подтверждено логистом-владельцем):
    5т/10т -> 3ч, 3т/Газель/Бус -> 2ч. Возвращает None, если тип не
    распознан - тогда часы просто не подставляются, ничего не додумываем."""
    if not vehicle_type_text:
        return None
    t = re.sub(r"\s+", "", vehicle_type_text.lower())
    if "10т" in t or "5т" in t:
        return 3
    if "3т" in t or "бус" in t or "газел" in t:
        return 2
    return None


def ensure_author_column(sheet):
    """Ищет колонку 'Автор_заявки' по заголовку (пишет её parsing-bot).
    Если колонки нет - возвращает None, фолбэк на автора просто не
    сработает, но остальная работа бота не прерывается."""
    global _author_col_cache
    if _author_col_cache is not None:
        return _author_col_cache
    header_row = sheet.row_values(1)
    try:
        _author_col_cache = header_row.index("Автор_заявки") + 1
    except ValueError:
        _author_col_cache = False
    return _author_col_cache or None


# Строка вида "Менеджер Сергій @TK_Gorod" / "Логіст Ірина @Anna_G0R0D" и
# т.п. - берём остаток строки после ключевого слова как имя менеджера.
_MANAGER_LINE_RE = re.compile(r"(?im)^\s*(?:менеджер|логіст|логист)\s*[:\-]?\s*(.+)$")
_USERNAME_RE = re.compile(r"@[A-Za-z0-9_]{4,}")


def extract_order_author(order_text: str, fallback_author: str) -> str:
    """Имя/тег для строки-подписи в превью тарифа: сначала пробуем найти
    явное 'Менеджер ...'/'Логіст ...' в тексте заявки (это тот, кто
    реально ведёт этого клиента - может отличаться от того, кто физически
    прислал сообщение в группу), и только если такой строки нет -
    используем автора сообщения из колонки 'Автор_заявки'.
    """
    m = _MANAGER_LINE_RE.search(order_text)
    if m:
        captured = m.group(1).strip()
        username_match = _USERNAME_RE.search(captured)
        if username_match:
            return username_match.group(0)
        return captured
    return fallback_author or ""


# ---------------------------------------------------------------------------
# GPT-разбор тарифа
# ---------------------------------------------------------------------------
_TARIFF_SYSTEM_PROMPT = """\
Ты разбираешь текст заявки на грузоперевозку и извлекаешь из него тариф
в строго структурированном JSON. Отвечай ТОЛЬКО валидным JSON, без
пояснений, без markdown-разметки, без ```json.

Формат ответа:
{
  "avto_baza": число или null,
  "min_chasov": число или null,
  "avto_dop_chas": число или null,
  "gruzchiki_baza": число или null,
  "gruzchiki_dop_chas": число или null,
  "tip_rascheta": "почасовка" | "фикс",
  "kom_avto": {"znachenie": число, "tip": "%" | "сумма"} или null,
  "kom_gruzchiki": {"znachenie": число, "tip": "%" | "сумма"} или null,
  "platelshik": "Клиент" | "Диспетчер" или null,
  "forma_oplaty": "Нал" | "БН" или null,
  "tariff_json": {
    "gidrobort": {"summa": число, "po_faktu": true} или null,
    "dop_hodka": {"tip": "сумма"|"vhodit_v_tarif"|"utochnit", "summa": число или null} или null,
    "dop_tochka": {"tip": "doplata_fix"|"pereschet_minimalki", "summa": число или null} или null,
    "ves": {"tip": "ploskaya", "stavka": число} или {"tip": "porogovaya", "porogi": [{"ot": число, "stavka": число}]} или null,
    "etazhi_stavka": число или null,
    "prohody_stavka": число или null,
    "prochie_dopy": [{"nazvanie": строка, "summa": число}] или []
  },
  "neponyatno": [строка с кратким описанием, что именно не удалось разобрать]
}

Правила:
- КРИТИЧНО не путать два РАЗНЫХ понятия про грузчиков:
  1) "gruzchiki_baza"/"gruzchiki_dop_chas" - это СОБСТВЕННЫЙ тариф самих
     грузчиков (сколько им платят за работу) - обычно указан как два
     числа через "/" рядом со словом "вантажники"/"грузчики" (например
     "Вантажники: 2400/1200" -> gruzchiki_baza=2400, gruzchiki_dop_chas=1200).
  2) "kom_gruzchiki" - это КОМИССИЯ компании С работы грузчиков (процент
     или фиксированная сумма, которую забирает компания/диспетчер) -
     заполняй ТОЛЬКО если рядом явно есть слово "ком"/"коміс"/"комісія"
     непосредственно у этого числа. Если такого слова нет - это НЕ
     комиссия, это тариф грузчиков (пункт 1), даже если два числа похожи
     по формату на "Авто_база/Авто_доп_час". Никогда не подставляй тариф
     грузчиков в kom_gruzchiki просто потому что больше некуда - для
     этого есть отдельные поля выше.
- Числа через "/" в начале блока тарифа (например "5300/900/20") - это
  авто_база/авто_доп_час/третье число. Третье число НЕ имеет фиксированного
  смысла само по себе - определяй его назначение ТОЛЬКО по ближайшему
  ключевому слову рядом с ним: "км"/"кілометр" -> не входит в текущую
  структуру, помечай в neponyatno; "этаж"/"поверх" -> etazhi_stavka;
  "прохід"/"проход"/"заносов" -> prohody_stavka; "точка"/"Т3" -> dop_tochka.
  Если рядом с числом нет ни одного из этих триггер-слов - НЕ угадывай его
  назначение, оставь соответствующее поле null и опиши число в neponyatno.
- Если тариф - одно число без "/" - это tip_rascheta="фикс", avto_baza =
  это число, avto_dop_chas и min_chasov = null.
- Гидроборт (gidrobort) заполняй ТОЛЬКО если в тексте явно написано что-то
  вида "гідроборт +950 якщо використовують" - то есть оплата по факту
  использования, а не автоматическая доплата. Если гидроборт просто
  упомянут как характеристика авто без суммы "по факту" - не заполняй.
- Любая сумма вида "від X" (например "збір меблів від 500грн") - это
  сумма, известная точно только по факту на месте (её может знать
  водитель, но не логист заранее) - НЕ помечай её в neponyatno (это не
  ошибка распознавания), просто занеси в prochie_dopy с summa=null и
  названием, отражающим что это "по факту, от X грн".
- Комиссия может состоять из нескольких чисел (например "900/100/8" -
  база/точка/км) - раскладывай так же по ключевым словам рядом, аналогично
  правилу выше.
- forma_oplaty оставляй null, если в тексте нет явного "нал"/"готівка"/
  "безнал"/"на карту"/"БН" и т.п.
- Если поле явно не упомянуто в тексте - null, не додумывай значение по
  умолчанию.
"""


def parse_tariff_via_gpt(order_text: str) -> dict:
    """Возвращает распарсенный тариф как dict (см. _TARIFF_SYSTEM_PROMPT).
    При любой ошибке (нет клиента, невалидный JSON, таймаут) возвращает
    dict с пустыми полями и neponyatno=["не удалось разобрать тариф"] -
    превью в этом случае покажет логисту, что нужно исправить руками,
    вместо того чтобы тихо потерять заказ.
    """
    fallback = {
        "avto_baza": None, "min_chasov": None, "avto_dop_chas": None,
        "gruzchiki_baza": None, "gruzchiki_dop_chas": None,
        "tip_rascheta": "почасовка", "kom_avto": None, "kom_gruzchiki": None,
        "platelshik": None, "forma_oplaty": None,
        "tariff_json": {}, "neponyatno": ["не удалось разобрать тариф (GPT недоступен)"],
    }
    if not _openai_client:
        return fallback
    try:
        completion = _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": _TARIFF_SYSTEM_PROMPT},
                {"role": "user", "content": order_text},
            ],
        )
        raw = completion.choices[0].message.content.strip()
        raw = re.sub(r"^```json\s*|\s*```$", "", raw.strip())
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Ошибка разбора тарифа через GPT: {e}")
        fallback["neponyatno"] = [f"не удалось разобрать тариф (ошибка: {e})"]
        return fallback


# ---------------------------------------------------------------------------
# Превью тарифа - текст сообщения по финальному согласованному шаблону
# ---------------------------------------------------------------------------
def _fmt_num(n) -> str:
    if n is None:
        return ""
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    return str(n)


def build_tariff_preview(tariff: dict, author_line: str = "", edited_fields: set = frozenset()) -> str:
    lines = []
    if author_line:
        lines.append(author_line)
    lines.append("📋 Тариф по заказу")

    star = lambda keys: " ⭐️" if edited_fields & set(keys) else ""

    base = tariff.get("avto_baza")
    if tariff.get("tip_rascheta") == "фикс":
        lines.append(f"Авто: {_fmt_num(base)} (фикса){star(['avto', 'tip_rascheta', 'min_chasov'])}")
    elif base is not None:
        min_h = tariff.get("min_chasov")
        dop = _fmt_num(tariff.get("avto_dop_chas"))
        hours_part = f" ({_fmt_num(min_h)}ч)" if min_h is not None else ""
        lines.append(f"Авто: {_fmt_num(base)}{hours_part}/{dop}{star(['avto', 'tip_rascheta', 'min_chasov'])}")

    gr_base = tariff.get("gruzchiki_baza")
    if gr_base is not None:
        gr_dop = _fmt_num(tariff.get("gruzchiki_dop_chas"))
        # Грузчики всегда на 2ч по подтверждённому бизнес-правилу - это
        # константа, не значение из текста заявки или GPT.
        lines.append(f"Грузчики: {_fmt_num(gr_base)} (2ч)/{gr_dop}{star(['gruzchiki'])}")

    kom_avto = tariff.get("kom_avto")
    kom_gruz = tariff.get("kom_gruzchiki")

    def kom_str(k):
        if not k:
            return None
        v = _fmt_num(k.get("znachenie"))
        return f"{v}%" if k.get("tip") == "%" else f"{v} грн"

    a_str, g_str = kom_str(kom_avto), kom_str(kom_gruz)
    kom_star = star(["kom_avto", "kom_gruzchiki"])
    if a_str and g_str:
        if a_str == g_str:
            lines.append(f"Ком. {a_str}{kom_star}")
        else:
            lines.append(f"Ком. авто: {a_str} | грузчики: {g_str}{kom_star}")
    elif a_str:
        lines.append(f"Ком. {a_str}{kom_star}")
    elif g_str:
        lines.append(f"Ком. грузчики: {g_str}{kom_star}")

    if tariff.get("platelshik"):
        lines.append(f"Плательщик: {tariff['platelshik']}{star(['platelshik'])}")

    if not tariff.get("forma_oplaty"):
        lines.append("⚠️ Форма оплаты: не указана")

    for item in tariff.get("neponyatno") or []:
        lines.append(f"⚠️ не понял: {item}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Запись тарифа в Sheets - целиком или отдельными полями
# ---------------------------------------------------------------------------
def _kom_cell(k):
    if not k:
        return ""
    v = _fmt_num(k.get("znachenie"))
    return f"{v}%" if k.get("tip") == "%" else f"{v}"


def build_tariff_row_values(tariff: dict) -> dict:
    """{имя_колонки: значение_для_записи} для всех тарифных колонок -
    общий источник и для полной записи (при ✅), и для точечной записи
    одного поля (при правке через карандаш)."""
    return {
        "Авто_база": _fmt_num(tariff.get("avto_baza")),
        "Мин_часов": _fmt_num(tariff.get("min_chasov")),
        "Авто_доп_час": _fmt_num(tariff.get("avto_dop_chas")),
        "Грузчики_база": _fmt_num(tariff.get("gruzchiki_baza")),
        "Грузчики_доп_час": _fmt_num(tariff.get("gruzchiki_dop_chas")),
        "Ком_авто": _kom_cell(tariff.get("kom_avto")),
        "Ком_грузчики": _kom_cell(tariff.get("kom_gruzchiki")),
        "Плательщик": tariff.get("platelshik") or "",
        "Тип_расчёта": tariff.get("tip_rascheta") or "",
        "Форма_оплаты": tariff.get("forma_oplaty") or "",
        "Тариф_JSON": json.dumps(tariff.get("tariff_json") or {}, ensure_ascii=False),
    }


def write_tariff_to_sheet(sheet, row_num: int, tariff: dict, columns: list = None):
    """Пишет тариф в Sheets. Если columns не задан - пишет все 11 колонок
    (используется при подтверждении ✅). Если задан список названий -
    пишет только их (используется при точечной правке одного поля)."""
    cols = ensure_tariff_columns(sheet)
    values = build_tariff_row_values(tariff)
    names = columns or TARIFF_COLUMN_NAMES
    updates = [
        {"range": f"{col_letter(cols[name])}{row_num}", "values": [[values[name]]]}
        for name in names
    ]
    sheet.batch_update(updates, value_input_option="RAW")


# ---------------------------------------------------------------------------
# Точечная правка полей тарифа (карандаш у каждой строки превью)
# ---------------------------------------------------------------------------
def _parse_two_numbers(text: str):
    parts = text.strip().split("/")
    if len(parts) != 2:
        raise ValueError("нужно два числа через / , например 4750/950")
    try:
        return float(parts[0].strip().replace(",", ".")), float(parts[1].strip().replace(",", "."))
    except ValueError:
        raise ValueError("нужно два числа через / , например 4750/950")


def _parse_one_number(text: str):
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        raise ValueError("нужно одно число, например 3")


def _parse_kom(text: str):
    t = text.strip()
    if not t:
        raise ValueError("нужно число, например 10% или 500")
    if t.endswith("%"):
        try:
            return {"znachenie": float(t[:-1].strip().replace(",", ".")), "tip": "%"}
        except ValueError:
            raise ValueError("нужно число перед %, например 10%")
    try:
        return {"znachenie": float(t.replace(",", ".")), "tip": "сумма"}
    except ValueError:
        raise ValueError("нужно число, например 10% или 500")


def _parse_platelshik(text: str):
    t = text.strip().lower()
    if t.startswith("клі") or t.startswith("кли"):
        return "Клиент"
    if t.startswith("дисп"):
        return "Диспетчер"
    raise ValueError("напишите 'Клиент' или 'Диспетчер'")


def _parse_tip_rascheta(text: str):
    t = text.strip().lower()
    if "фикс" in t or "фікс" in t:
        return "фикс"
    if "почас" in t:
        return "почасовка"
    raise ValueError("напишите 'фикс' или 'почасовка'")


def _parse_forma_oplaty(text: str):
    t = text.strip().lower()
    if t in ("нал", "готівка", "готовка", "cash"):
        return "Нал"
    if t in ("бн", "безнал", "карта", "card"):
        return "БН"
    raise ValueError("напишите 'Нал' или 'БН'")


def _apply_avto(tariff, text):
    t = text.strip()
    if "/" in t:
        a, b = _parse_two_numbers(t)
        tariff["avto_baza"], tariff["avto_dop_chas"] = a, b
        tariff["tip_rascheta"] = "почасовка"
    else:
        tariff["avto_baza"] = _parse_one_number(t)
        tariff["avto_dop_chas"] = None
        tariff["tip_rascheta"] = "фикс"


def _apply_min_chasov(tariff, text):
    tariff["min_chasov"] = _parse_one_number(text)


def _apply_gruzchiki(tariff, text):
    a, b = _parse_two_numbers(text)
    tariff["gruzchiki_baza"], tariff["gruzchiki_dop_chas"] = a, b


def _apply_kom_avto(tariff, text):
    tariff["kom_avto"] = _parse_kom(text)


def _apply_kom_gruzchiki(tariff, text):
    tariff["kom_gruzchiki"] = _parse_kom(text)


def _apply_platelshik(tariff, text):
    tariff["platelshik"] = _parse_platelshik(text)


def _apply_tip_rascheta(tariff, text):
    tariff["tip_rascheta"] = _parse_tip_rascheta(text)


def _apply_forma_oplaty(tariff, text):
    tariff["forma_oplaty"] = _parse_forma_oplaty(text)


# field_key -> {label, hint, apply(tariff, text), columns затрагиваемые в Sheets}
FIELD_DEFS = {
    "avto": {
        "label": "Авто (база/доп.час)", "hint": "4750/950 - почасовка, или просто 1890 - фикс",
        "apply": _apply_avto, "columns": ["Авто_база", "Авто_доп_час", "Тип_расчёта"],
    },
    "min_chasov": {
        "label": "Мин. часов", "hint": "например: 3",
        "apply": _apply_min_chasov, "columns": ["Мин_часов"],
    },
    "gruzchiki": {
        "label": "Грузчики (база/доп.час)", "hint": "например: 2400/1200",
        "apply": _apply_gruzchiki, "columns": ["Грузчики_база", "Грузчики_доп_час"],
    },
    "kom_avto": {
        "label": "Ком. авто", "hint": "например: 10% или 500",
        "apply": _apply_kom_avto, "columns": ["Ком_авто"],
    },
    "kom_gruzchiki": {
        "label": "Ком. грузчики", "hint": "например: 10% или 500",
        "apply": _apply_kom_gruzchiki, "columns": ["Ком_грузчики"],
    },
    "platelshik": {
        "label": "Плательщик", "hint": "Клиент или Диспетчер",
        "apply": _apply_platelshik, "columns": ["Плательщик"],
    },
    "tip_rascheta": {
        "label": "Тип расчёта", "hint": "фикс или почасовка",
        "apply": _apply_tip_rascheta, "columns": ["Тип_расчёта"],
    },
    "forma_oplaty": {
        "label": "Форма оплаты", "hint": "Нал или БН",
        "apply": _apply_forma_oplaty, "columns": ["Форма_оплаты"],
    },
}


# ---------------------------------------------------------------------------
# In-memory состояние: order_key -> распарсенный тариф, ждущий подтверждения.
# Переживает только до рестарта Railway - но это не проблема: если бота
# перезапустят между отправкой превью и нажатием кнопки, get_or_reparse_tariff
# просто пересчитает тариф заново из текста заявки (см. ниже), а не
# заставляет логиста присылать всё вручную.
# ---------------------------------------------------------------------------
# order_key -> распарсенный тариф, ждущий подтверждения (см. комментарий выше)
_pending_tariffs: dict[str, dict] = {}

# order_key -> строка автора/менеджера (нужна, чтобы восстановить её при
# перерисовке превью после отмены правки или рестарта).
_order_author_line: dict[str, str] = {}

# order_key -> множество field_key, которые логист уже поправил вручную -
# используется, чтобы пометить изменённые строки превью звёздочкой ⭐️.
_order_edited_fields: dict[str, set] = {}

# (chat_id, message_id превью-сообщения бота) -> (order_key, field_key).
# Заполняется при нажатии кнопки конкретного поля в меню правки, чтобы
# понять, какое поле правит логист, когда придёт текстовый reply.
_awaiting_field_edit: dict[tuple[int, int], tuple[str, str]] = {}


def tariff_level1_keyboard(order_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Верно", callback_data=f"tariff_ok|{order_key}"),
        InlineKeyboardButton("✏️ Исправить", callback_data=f"tariff_editmenu|{order_key}"),
    ]])


def tariff_edit_menu_keyboard(order_key: str) -> InlineKeyboardMarkup:
    keys = list(FIELD_DEFS.keys())
    rows = []
    for i in range(0, len(keys), 2):
        row_keys = keys[i:i + 2]
        rows.append([
            InlineKeyboardButton(f"✏️ {FIELD_DEFS[k]['label']}", callback_data=f"editfield|{k}|{order_key}")
            for k in row_keys
        ])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"editback|{order_key}")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Telegram: обработка нажатий кнопок ✅ / ✏️
# ---------------------------------------------------------------------------
def apply_default_hours(sheet, row, tariff: dict) -> dict:
    """Если GPT не нашёл явных мин.часов в тексте (и это не фикс) -
    подставляет умолчание по типу авто (см. default_min_hours_for_vehicle).
    Мутирует и возвращает тот же dict для удобства цепочки вызовов."""
    if tariff.get("tip_rascheta") == "фикс" or tariff.get("min_chasov") is not None:
        return tariff
    vt_col = ensure_vehicle_type_column(sheet)
    vehicle_type_text = row[vt_col - 1] if vt_col and len(row) >= vt_col else ""
    default_hours = default_min_hours_for_vehicle(vehicle_type_text)
    if default_hours is not None:
        tariff["min_chasov"] = default_hours
    return tariff


def get_or_reparse_tariff(sheet, row_num: int, order_key: str) -> dict:
    """Возвращает тариф из памяти, а если бот перезапускался и памяти нет -
    молча пересчитывает его заново из текста заявки в Sheets.

    Безопасно, потому что GPT вызывается с temperature=0 (детерминированно)
    и текст заявки в Sheets не меняется между отправкой превью и нажатием
    кнопки - пересчёт даёт тот же результат, что и первый разбор. Это
    временная страховка вместо полноценного fallback-опроса: не требует
    отдельного фонового задания, работает уже сейчас.
    """
    pending = _pending_tariffs.get(order_key)
    if pending:
        return pending

    logger.warning(f"Тариф для key={order_key} не найден в памяти (рестарт?) - пересчитываю заново")
    log_event("Тариф: не найден в памяти, пересчитан заново после рестарта", row=row_num)

    row = sheet.row_values(row_num)
    order_text = row[COL_J_TEXT - 1] if len(row) >= COL_J_TEXT else ""
    tariff = parse_tariff_via_gpt(order_text)
    tariff = apply_default_hours(sheet, row, tariff)
    _pending_tariffs[order_key] = tariff

    if order_key not in _order_author_line:
        author_col = ensure_author_column(sheet)
        fallback_author = row[author_col - 1] if author_col and len(row) >= author_col else ""
        _order_author_line[order_key] = extract_order_author(order_text, fallback_author)

    return tariff


async def handle_tariff_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    action = parts[0]
    msg_key = (query.message.chat_id, query.message.message_id)

    if action == "tariff_ok":
        order_key = parts[1]
        sheet = get_sheet()
        row_num = find_row_by_key(sheet, order_key)
        if not row_num:
            await query.edit_message_text(f"⚠️ Не нашёл строку заказа по ключу {order_key} в таблице.")
            return
        pending = get_or_reparse_tariff(sheet, row_num, order_key)
        try:
            write_tariff_to_sheet(sheet, row_num, pending)
        except Exception as e:
            logger.error(f"Не удалось записать тариф в Sheets, key={order_key}: {e}")
            log_event(f"Тариф: ОШИБКА записи - {e}", row=row_num)
            await query.edit_message_text(f"⚠️ Не удалось записать тариф в таблицу: {e}")
            return
        _pending_tariffs.pop(order_key, None)
        _order_author_line.pop(order_key, None)
        _order_edited_fields.pop(order_key, None)
        log_event("Тариф подтверждён и записан", row=row_num)
        await query.edit_message_text(query.message.text + "\n\n✅ Подтверждено")

    elif action == "tariff_editmenu":
        order_key = parts[1]
        await query.edit_message_reply_markup(reply_markup=tariff_edit_menu_keyboard(order_key))

    elif action == "editback":
        order_key = parts[1]
        _awaiting_field_edit.pop(msg_key, None)
        sheet = get_sheet()
        row_num = find_row_by_key(sheet, order_key)
        pending = get_or_reparse_tariff(sheet, row_num, order_key) if row_num else {}
        author_line = _order_author_line.get(order_key, "")
        edited = _order_edited_fields.get(order_key, set())
        await query.edit_message_text(
            build_tariff_preview(pending, author_line, edited),
            reply_markup=tariff_level1_keyboard(order_key),
        )

    elif action == "editfield":
        field_key, order_key = parts[1], parts[2]
        fdef = FIELD_DEFS[field_key]
        _awaiting_field_edit[msg_key] = (order_key, field_key)
        cancel_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data=f"editback|{order_key}")
        ]])
        await query.edit_message_text(
            query.message.text + f"\n\n✏️ Пришлите новое значение для «{fdef['label']}» "
                                  f"({fdef['hint']}), ответом на это сообщение.",
            reply_markup=cancel_kb,
        )


async def handle_tariff_correction_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит текстовый reply логиста на сообщение 'пришлите новое значение
    для <поле>' - применяет правку СРАЗУ к нужной ячейке в Sheets (не
    перезаписывая весь тариф), обновляет превью и логирует старое/новое
    значение в Tariff_corrections для будущего анализа."""
    msg = update.message
    replied = msg.reply_to_message
    if not replied:
        return

    msg_key = (msg.chat_id, replied.message_id)
    awaiting = _awaiting_field_edit.get(msg_key)
    if not awaiting:
        return  # обычный reply, к правке тарифа отношения не имеет

    order_key, field_key = awaiting
    fdef = FIELD_DEFS[field_key]

    sheet = get_sheet()
    row_num = find_row_by_key(sheet, order_key)
    if not row_num:
        await msg.reply_text(f"⚠️ Не нашёл строку заказа по ключу {order_key} в таблице.")
        return

    pending = get_or_reparse_tariff(sheet, row_num, order_key)
    old_values = build_tariff_row_values(pending)
    old_str = " / ".join(old_values[c] for c in fdef["columns"] if old_values[c]) or "(пусто)"

    try:
        fdef["apply"](pending, msg.text or "")
    except ValueError as e:
        await msg.reply_text(f"⚠️ Не понял значение: {e}")
        return

    try:
        write_tariff_to_sheet(sheet, row_num, pending, columns=fdef["columns"])
    except Exception as e:
        logger.error(f"Не удалось записать правку поля '{field_key}' в Sheets, key={order_key}: {e}")
        await msg.reply_text(f"⚠️ Не удалось записать в таблицу: {e}")
        return

    _pending_tariffs[order_key] = pending
    _awaiting_field_edit.pop(msg_key, None)
    _order_edited_fields.setdefault(order_key, set()).add(field_key)

    new_values = build_tariff_row_values(pending)
    new_str = " / ".join(new_values[c] for c in fdef["columns"] if new_values[c]) or "(пусто)"
    log_field_correction(order_key, fdef["label"], old_str, new_str)
    log_event(f"Правка поля '{fdef['label']}' записана в Sheets", row=row_num)

    author_line = _order_author_line.get(order_key, "")
    edited = _order_edited_fields.get(order_key, set())
    await replied.edit_text(
        build_tariff_preview(pending, author_line, edited),
        reply_markup=tariff_level1_keyboard(order_key),
    )

    # Убираем за собой: подтверждение больше не шлём (обновлённое превью
    # уже всё показывает) и пробуем удалить сообщение логиста со значением
    # - если у бота нет прав на удаление чужих сообщений в группе, просто
    # тихо промолчим, это не критично для самого сохранения данных.
    try:
        await msg.delete()
    except Exception as e:
        logger.info(f"Не удалось удалить сообщение с правкой (нет прав?): {e}")


# ---------------------------------------------------------------------------
# aiohttp: приём вебхука от parsing-bot
# ---------------------------------------------------------------------------
async def handle_new_order_webhook(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    order_key = data.get("order_key")
    if not order_key:
        return web.json_response({"ok": False, "error": "no order_key"}, status=400)

    logger.info(f"Вебхук: новая заявка key={order_key}")

    try:
        await process_new_order(request.app["tg_app"], order_key)
    except Exception as e:
        logger.error(f"Ошибка обработки заявки key={order_key}: {e}")
        log_event(f"Тариф: ОШИБКА обработки вебхука - {e}")
        # Отвечаем 200 в любом случае - parsing-bot не должен ретраить и
        # не должен ничего решать по коду ответа, это fire-and-forget.

    return web.json_response({"ok": True})


async def process_new_order(tg_app: Application, order_key: str):
    sheet = get_sheet()
    row_num = find_row_by_key(sheet, order_key)
    if not row_num:
        log_event(f"Тариф: строка НЕ НАЙДЕНА для key={order_key}")
        return

    row = sheet.row_values(row_num)

    def cell(idx, default=""):
        return row[idx - 1] if len(row) >= idx else default

    order_text = cell(COL_J_TEXT)
    chat_id = cell(COL_T_CHAT_ID)
    message_id = cell(COL_S_MESSAGE_ID)

    if chat_id not in LOGIST_CHAT_IDS:
        # Заявка не из активной группы логистов - пока не обрабатываем.
        return
    if not order_text or not chat_id or not message_id:
        log_event(f"Тариф: недостаточно данных в строке для key={order_key}", chat_id, message_id, row_num)
        return

    author_col = ensure_author_column(sheet)
    fallback_author = cell(author_col) if author_col else ""
    author_line = extract_order_author(order_text, fallback_author)

    tariff = parse_tariff_via_gpt(order_text)
    tariff = apply_default_hours(sheet, row, tariff)

    _pending_tariffs[order_key] = tariff
    _order_author_line[order_key] = author_line

    preview = build_tariff_preview(tariff, author_line)

    await tg_app.bot.send_message(
        chat_id=int(chat_id),
        text=preview,
        reply_to_message_id=int(message_id),
        reply_markup=tariff_level1_keyboard(order_key),
    )
    log_event("Превью тарифа отправлено", chat_id, message_id, row_num)


# ---------------------------------------------------------------------------
# Запуск: одновременно aiohttp-сервер (вебхук) и telegram polling
# ---------------------------------------------------------------------------
async def main():
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CallbackQueryHandler(handle_tariff_callback))
    tg_app.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.TEXT & filters.REPLY,
            handle_tariff_correction_reply,
        )
    )

    aio_app = web.Application()
    aio_app["tg_app"] = tg_app
    aio_app.router.add_post("/new_order", handle_new_order_webhook)

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logger.info(f"Вебхук-сервер запущен на порту {WEBHOOK_PORT}")

    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Бот рассылки запущен, начинаю polling...")
        try:
            # Держим процесс живым, пока не остановят извне (Railway
            # отправляет SIGTERM при рестарте/деплое - штатно ловится
            # asyncio при завершении работы контейнера).
            await asyncio.Event().wait()
        finally:
            await tg_app.updater.stop()
            await tg_app.stop()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
