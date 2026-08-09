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
# Группы логистов - тариф-превью работает здесь сразу.
LOGIST_CHAT_IDS = {
    "-1003490954823",  # Монитор логистов (сейчас тестовая - все заказы Кужеля)
    "-1003477719320",  # Газели диспетчерская
    "-1003404979004",  # 5-10т Диспетчера
}

# Группы водителей - бот в них уже добавлен участником, но пока молчит.
# Подключаем по одной вручную по мере готовности (перенос id сюда).
# Пока пусто - карточки заказов никому не шлются, это Часть 2.2 (roadmap).
ACTIVE_DRIVER_CHAT_IDS = {
    # "-1003633789888",  # Кужель - тестовая, включим после проверки тарифа
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
        sheet.append_row(["Время", "Order_key", "Текст заявки", "Разбор GPT (JSON)", "Правка логиста (текст)"])
        return sheet


def log_tariff_correction(order_key: str, order_text: str, gpt_tariff: dict, correction_text: str):
    """Сохраняет пару 'как разобрал GPT -> как исправил логист' - сырьё для
    будущих few-shot примеров в промпте (см. обсуждение обучения). Сам
    бот эту правку пока НЕ применяет к столбцам тарифа - это отдельная
    задача (флоу 'применить правку' строится позже).
    """
    try:
        get_tariff_corrections_sheet().append_row(
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                order_key,
                order_text,
                json.dumps(gpt_tariff, ensure_ascii=False),
                correction_text,
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


def build_tariff_preview(tariff: dict) -> str:
    lines = ["📋 Тариф по заказу"]

    base = tariff.get("avto_baza")
    if tariff.get("tip_rascheta") == "фикс":
        lines.append(f"Авто: {_fmt_num(base)} (фикса)")
    else:
        min_h = _fmt_num(tariff.get("min_chasov"))
        dop = _fmt_num(tariff.get("avto_dop_chas"))
        lines.append(f"Авто: {_fmt_num(base)} ({min_h}ч)/{dop}")

    kom_avto = tariff.get("kom_avto")
    kom_gruz = tariff.get("kom_gruzchiki")

    def kom_str(k):
        if not k:
            return None
        v = _fmt_num(k.get("znachenie"))
        return f"{v}%" if k.get("tip") == "%" else f"{v} грн"

    a_str, g_str = kom_str(kom_avto), kom_str(kom_gruz)
    if a_str and g_str:
        if a_str == g_str:
            lines.append(f"Ком. {a_str}")
        else:
            lines.append(f"Ком. авто: {a_str} | грузчики: {g_str}")
    elif a_str:
        lines.append(f"Ком. {a_str}")
    elif g_str:
        lines.append(f"Ком. грузчики: {g_str}")

    if tariff.get("platelshik"):
        lines.append(f"Плательщик: {tariff['platelshik']}")

    if not tariff.get("forma_oplaty"):
        lines.append("⚠️ Форма оплаты: не указана")

    for item in tariff.get("neponyatno") or []:
        lines.append(f"⚠️ не понял: {item}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Запись подтверждённого тарифа в Sheets
# ---------------------------------------------------------------------------
def write_tariff_to_sheet(sheet, row_num: int, tariff: dict):
    cols = ensure_tariff_columns(sheet)

    def kom_cell(k):
        if not k:
            return ""
        v = _fmt_num(k.get("znachenie"))
        return f"{v}%" if k.get("tip") == "%" else f"{v}"

    updates = [
        {"range": f"{col_letter(cols['Авто_база'])}{row_num}", "values": [[_fmt_num(tariff.get('avto_baza'))]]},
        {"range": f"{col_letter(cols['Мин_часов'])}{row_num}", "values": [[_fmt_num(tariff.get('min_chasov'))]]},
        {"range": f"{col_letter(cols['Авто_доп_час'])}{row_num}", "values": [[_fmt_num(tariff.get('avto_dop_chas'))]]},
        {"range": f"{col_letter(cols['Ком_авто'])}{row_num}", "values": [[kom_cell(tariff.get('kom_avto'))]]},
        {"range": f"{col_letter(cols['Ком_грузчики'])}{row_num}", "values": [[kom_cell(tariff.get('kom_gruzchiki'))]]},
        {"range": f"{col_letter(cols['Плательщик'])}{row_num}", "values": [[tariff.get('platelshik') or ""]]},
        {"range": f"{col_letter(cols['Тип_расчёта'])}{row_num}", "values": [[tariff.get('tip_rascheta') or ""]]},
        {"range": f"{col_letter(cols['Форма_оплаты'])}{row_num}", "values": [[tariff.get('forma_oplaty') or ""]]},
        {"range": f"{col_letter(cols['Тариф_JSON'])}{row_num}", "values": [[json.dumps(tariff.get('tariff_json') or {}, ensure_ascii=False)]]},
    ]
    sheet.batch_update(updates, value_input_option="RAW")


# ---------------------------------------------------------------------------
# In-memory состояние: order_key -> распарсенный тариф, ждущий подтверждения.
# Переживает только до рестарта Railway - но это не проблема: если бота
# перезапустят между отправкой превью и нажатием кнопки, get_or_reparse_tariff
# просто пересчитает тариф заново из текста заявки (см. ниже), а не
# заставляет логиста присылать всё вручную.
# ---------------------------------------------------------------------------
# order_key -> распарсенный тариф, ждущий подтверждения (см. комментарий выше)
_pending_tariffs: dict[str, dict] = {}

# (chat_id, message_id превью-сообщения бота) -> order_key. Заполняется при
# нажатии "✏️ Исправить", чтобы понять, к какому заказу относится текстовый
# reply логиста, когда он придёт следующим сообщением.
_awaiting_correction: dict[tuple[int, int], str] = {}


# ---------------------------------------------------------------------------
# Telegram: обработка нажатий кнопок ✅ / ✏️
# ---------------------------------------------------------------------------
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
    _pending_tariffs[order_key] = tariff
    return tariff


async def handle_tariff_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, order_key = query.data.split("|", 1)

    sheet = get_sheet()
    row_num = find_row_by_key(sheet, order_key)
    if not row_num:
        await query.edit_message_text(f"⚠️ Не нашёл строку заказа по ключу {order_key} в таблице.")
        return

    pending = get_or_reparse_tariff(sheet, row_num, order_key)

    if action == "tariff_ok":
        try:
            write_tariff_to_sheet(sheet, row_num, pending)
        except Exception as e:
            logger.error(f"Не удалось записать тариф в Sheets, key={order_key}: {e}")
            log_event(f"Тариф: ОШИБКА записи - {e}", row=row_num)
            await query.edit_message_text(f"⚠️ Не удалось записать тариф в таблицу: {e}")
            return
        _pending_tariffs.pop(order_key, None)
        log_event("Тариф подтверждён и записан", row=row_num)
        await query.edit_message_text(query.message.text + "\n\n✅ Подтверждено")

    elif action == "tariff_fix":
        # Полноценный флоу применения правки (по полям и т.п.) - отдельная
        # задача на будущее. Пока просим прислать верный тариф текстом,
        # реплаем на это же сообщение, и просто ЛОГИРУЕМ пару "как
        # разобрал GPT -> как исправил логист" - сырьё для будущих
        # few-shot примеров в промпте (см. handle_tariff_correction_reply).
        _awaiting_correction[(query.message.chat_id, query.message.message_id)] = order_key
        await query.edit_message_text(
            query.message.text + "\n\n✏️ Пришлите верный тариф текстом, ответом на это сообщение."
        )


async def handle_tariff_correction_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит текстовый reply логиста на сообщение 'пришлите верный тариф' и
    логирует пару (текст заявки, разбор GPT, правка логиста) в
    Tariff_corrections. Правка пока НЕ применяется автоматически к
    столбцам тарифа - это отдельная будущая задача.
    """
    msg = update.message
    replied = msg.reply_to_message
    if not replied:
        return

    key = (msg.chat_id, replied.message_id)
    order_key = _awaiting_correction.get(key)
    if not order_key:
        return  # обычный reply, к правке тарифа отношения не имеет

    pending = _pending_tariffs.get(order_key, {})
    sheet = get_sheet()
    row_num = find_row_by_key(sheet, order_key)
    order_text = ""
    if row_num:
        row = sheet.row_values(row_num)
        order_text = row[COL_J_TEXT - 1] if len(row) >= COL_J_TEXT else ""

    log_tariff_correction(order_key, order_text, pending, msg.text or "")
    _awaiting_correction.pop(key, None)

    await msg.reply_text(
        "Записано, спасибо. Пока сама правка в таблицу не применяется "
        "автоматически - при необходимости внесите верный тариф в "
        "соответствующие столбцы вручную."
    )
    log_event("Правка тарифа записана в Tariff_corrections", row=row_num)


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

    tariff = parse_tariff_via_gpt(order_text)
    _pending_tariffs[order_key] = tariff

    preview = build_tariff_preview(tariff)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Верно", callback_data=f"tariff_ok|{order_key}"),
        InlineKeyboardButton("✏️ Исправить", callback_data=f"tariff_fix|{order_key}"),
    ]])

    await tg_app.bot.send_message(
        chat_id=int(chat_id),
        text=preview,
        reply_to_message_id=int(message_id),
        reply_markup=keyboard,
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
