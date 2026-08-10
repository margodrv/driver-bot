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

ВАЖНО про границы задачи: тебя интересует ТОЛЬКО тариф (цены, ставки,
комиссии, условия оплаты). Точки маршрута (Т1, Т2, Т3...), адреса,
названия груза, контакты, ФИО, номера телефонов, статус "Т2 ?" (адрес
ещё не определён) и всё, что не является частью тарифа - НЕ твоя зона
ответственности, НЕ упоминай это в "neponyatno" ни в каком виде, даже
если что-то там выглядит незаполненным или странным. "neponyatno"
предназначено ИСКЛЮЧИТЕЛЬНО для чисел/условий внутри самого тарифа,
которые ты не смог классифицировать.

Формат ответа:
{
  "avto_baza": число или null,
  "min_chasov": число или null,
  "avto_dop_chas": число или null,
  "gruzchiki_baza": число или null,
  "gruzchiki_dop_chas": число или null,
  "gruzchiki_dopy": [число, ...] или [],
  "tip_rascheta": "почасовка" | "фикс",
  "kom_avto": {"znachenie": число, "tip": "%" | "сумма"} или {"tip": "pochasovka", "baza": число, "dop_chas": число, "dopy": [число,...]} или null,
  "kom_gruzchiki": {"znachenie": число, "tip": "%" | "сумма"} или {"tip": "pochasovka", "baza": число, "dop_chas": число, "dopy": [число,...]} или null,
  "platelshik": "Клиент" | "Диспетчер" или null,
  "forma_oplaty": "Нал" | "БН" или null,
  "tariff_json": {
    "gidrobort": {"summa": число, "po_faktu": true} или null,
    "dop_hodka": {"tip": "сумма"|"vhodit_v_tarif"|"utochnit", "summa": число или null} или null,
    "dop_tochka": {"tip": "doplata_fix"|"pereschet_minimalki", "summa": число или null} или null,
    "km_stavka": число или null,
    "ves": {"tip": "ploskaya", "stavka": число} или {"tip": "porogovaya", "porogi": [{"ot": число, "stavka": число}]} или null,
    "etazhi_stavka": число или null,
    "prohody_stavka": число или null,
    "prochie_dopy": [{"nazvanie": строка, "summa": число, "group": "avto"|"gruzchiki"}] или []
  },
  "neponyatno": [КОРОТКАЯ строка вида "<число>: вариант1?/вариант2?"
    - только про цифры/условия оплаты в САМОМ ТАРИФЕ, никогда про
    адреса/маршрут/груз. Без полных предложений.]
}

Правила:
- КРИТИЧНО не путать "Зайнятість"/"Занятость" (например "Орієнтовна
  зайнятість 2-3 год") с min_chasov: это ОЦЕНКА длительности самой
  работы для планирования графика, а НЕ минимальные часы тарифа
  (сколько минимум оплачивается). Эти два числа обычно РАЗНЫЕ и НЕ
  связаны друг с другом. min_chasov заполняй ТОЛЬКО если явно указано
  рядом с самим тарифом (например "мін 3год", "тариф на 3 години", или
  третье число в тарифной строке с триггером "мін"/"минимум" рядом).
  Строки вида "Зайнятість X-Y год" НИКОГДА не используй как источник
  min_chasov - это разные метрики, даже если формат похож.
- КРИТИЧНО не путать два РАЗНЫХ понятия про грузчиков:
  1) "gruzchiki_baza"/"gruzchiki_dop_chas" - это СОБСТВЕННЫЙ тариф самих
     грузчиков (сколько им платят за работу) - обычно указан как два
     числа через "/" рядом со словом "вантажники"/"грузчики" (например
     "Вантажники: 2400/1200" -> gruzchiki_baza=2400, gruzchiki_dop_chas=1200).
     ЕСЛИ в этой же строке БОЛЬШЕ двух чисел через "/" (например
     "Вантажники 1600/800/4/20") - первые два, как обычно, gruzchiki_baza/
     gruzchiki_dop_chas, а ВСЕ остальные числа (по порядку появления)
     клади в gruzchiki_dopy как массив чисел. НЕ пытайся понять их смысл
     (вес/этаж/проход/что-то ещё) - это не нужно, просто сохрани числа по
     порядку как есть.
  2) "kom_gruzchiki" - это КОМИССИЯ компании С работы грузчиков (процент
     или фиксированная сумма, которую забирает компания/диспетчер) -
     заполняй ТОЛЬКО если рядом явно есть слово "ком"/"коміс"/"комісія"
     непосредственно у этого числа. Если такого слова нет - это НЕ
     комиссия, это тариф грузчиков (пункт 1), даже если два числа похожи
     по формату на "Авто_база/Авто_доп_час". Никогда не подставляй тариф
     грузчиков в kom_gruzchiki просто потому что больше некуда - для
     этого есть отдельные поля выше.
- Комиссия (kom_avto/kom_gruzchiki) МОЖЕТ сама быть ступенчатой (не
  только % или разовая сумма) - если явно видно два числа рядом со
  словом "ком" в формате "база/доп.час" (например "Ком авто 1000/400
  наступний/слідуючий час" -> {"tip": "pochasovka", "baza": 1000,
  "dop_chas": 400}). Если чисел больше двух - остальные (по порядку) в
  "dopy", аналогично правилу для gruzchiki_dopy выше, без попытки понять
  их смысл. Обычная % или разовая сумма (одно число) остаются как раньше.
- tip_rascheta определяется КОЛИЧЕСТВОМ чисел в тарифе - строгое правило:
  - Тариф - ДВА (или больше) числа через "/" (например "4100/900",
    "5300/900/20") -> tip_rascheta="почасовка", ПЕРВОЕ число это
    avto_baza, ВТОРОЕ число это avto_dop_chas (третье, если есть -
    разбирай по правилу ниже про ключевые слова).
  - Тариф - ОДНО число без "/" -> tip_rascheta="фикс", это число -
    avto_baza, avto_dop_chas и min_chasov = null.
  Это правило строгое и не зависит от суммы или контекста - считай
  количество чисел через "/" в самой тарифной строке, не додумывай.
- Числа через "/" в начале блока тарифа (например "5300/900/20") - это
  авто_база/авто_доп_час/третье число. Третье число НЕ имеет фиксированного
  смысла само по себе - определяй его назначение ТОЛЬКО по ближайшему
  ключевому слову рядом с ним: "км"/"кілометр" -> km_stavka; "этаж"/"поверх"
  -> etazhi_stavka; "прохід"/"проход"/"заносов" -> prohody_stavka;
  "точка"/"Т3" -> dop_tochka.
  Если рядом с числом нет ни одного из этих триггер-слов - это ОБЯЗАТЕЛЬНО
  должно попасть в neponyatno, никогда не теряй число молча. Формат КОРОТКИЙ
  - число и кандидаты через "/", БЕЗ полных предложений: "<число>:
  ГБ?/точка?/км?/этаж?/проход?". ВАЖНО - подставляй в кандидаты только
  правдоподобные по размеру варианты: "этаж?"/"проход?" предлагай ТОЛЬКО
  если число ≤100 (ставка за этаж/проход - это обычно десятки, не сотни);
  "вес?" предлагай ТОЛЬКО если число ≤15 (ставка за кг - обычно несколько
  гривен). Если число больше этих порогов - эти варианты вообще не
  включай в список кандидатов, даже если больше нечего предложить.
  Пример: "Тар 6000/1200/600" без слов рядом с "600" -> avto_baza=6000,
  avto_dop_chas=1200, neponyatno=["600: ГБ?/точка?/км?"] (без "этаж?"/
  "проход?" - 600 слишком много для них).
- КРИТИЧНО: если в тарифной строке АВТО (не отдельной строке
  "вантажники") больше 2 чисел через "/" (например "2100/600/200/1000/44"
  - это тариф авто с несколькими доп.числами, а НЕ "тариф авто + тариф
  грузчиков"! НИКОГДА не заполняй gruzchiki_baza/gruzchiki_dop_chas
  просто потому что чисел много - эти поля заполняются ТОЛЬКО если в
  тексте ЕСТЬ отдельное слово "вантажники"/"грузчики" с СОБСТВЕННЫМИ
  числами рядом с этим словом. Если такого слова в тексте вообще нет -
  gruzchiki_baza/gruzchiki_dop_chas/gruzchiki_dopy остаются null/[] всегда,
  сколько бы чисел ни было в тарифе авто. Каждое доп.число классифицируй
  по своему ключевому слову рядом (как в правиле выше) или в neponyatno.
  "Сан. обробка"/"санобробка"/"санітарна обробка" рядом с числом -> клади
  в prochie_dopy с nazvanie "Санобробка".
- Гидроборт (gidrobort) заполняй ТОЛЬКО если в тексте явно написано число
  рядом со словом "гідроборт", означающее доплату (например "гідроборт
  +950 якщо використовують"). ПОДТВЕРЖДЕНО: если "гідроборт" упомянут
  просто как характеристика авто в описании машины (например "5т
  гідроборт + 2 вантажника") - БЕЗ суммы рядом - считается, что
  гідроборт УЖЕ включён в тариф авто, отдельно за него НЕ доплачивают.
  В этом случае gidrobort = null, НИКОГДА не ставь summa=0 или пустую
  структуру "для галочки". Если логист позже решит, что доплата всё же
  нужна - он внесёт её вручную через отдельную кнопку.
- Любая сумма вида "від X" (например "збір меблів від 500грн") - это
  сумма, известная точно только по факту на месте (её может знать
  водитель, но не логист заранее) - НЕ помечай её в neponyatno (это не
  ошибка распознавания), просто занеси в prochie_dopy с summa=null и
  названием, отражающим что это "по факту, от X грн".
- Каждый элемент prochie_dopy ОБЯЗАТЕЛЬНО должен иметь "group": "avto"
  (доплата, относится к машине/маршруту - например "рокла" - тележка/
  оборудование, "заміський" - выезд за город, любые именные допы к
  авто) или "gruzchiki" (доплата, относится к работе грузчиков - любые
  именные допы, завязанные на переноску/вес/этажи). Если сомневаешься -
  используй "avto".
- "км"/"кілометр" рядом с числом - это ВСЕГДА km_stavka, даже если
  формат похож на "доп.точку" (например "+заміський 90грн/км" ->
  km_stavka=90, а НЕ dop_tochka - слово "км" однозначно указывает на
  километраж, приоритет у него выше любых догадок по формату числа).
- "рокла" (тележка/подъёмное оборудование) рядом с числом - это НЕ
  доп.ходка (доп.ходка - это про повторный заезд, а не оборудование) -
  заноси в prochie_dopy с nazvanie "Рокла", group "avto".
- Если число сопровождается "м"/"метр" (единица РАССТОЯНИЯ, не грн) -
  это НЕ цена, не заполняй etazhi_stavka/prohody_stavka из него - это
  просто описание расстояния/ширины прохода, денежного значения тут нет.
  Ищи РЯДОМ отдельное число именно в грн - это и есть настоящая ставка.
- "з вагою по X грн на людину" (доплата, СВЯЗАННАЯ с весом груза, но не
  сама ставка веса) - это отдельная именная допа: prochie_dopy с
  nazvanie "Допы с весом", summa=X, group "gruzchiki". НЕ путай с самим
  полем "ves" (ves - это ставка за кг, например "від 160кг по 8грн/кг" -
  разные вещи, могут встречаться в одном заказе одновременно).
- Комиссия может состоять из нескольких чисел (например "900/100/8" -
  база/точка/км) - раскладывай так же по ключевым словам рядом, аналогично
  правилу выше.
- forma_oplaty оставляй null, если в тексте нет явного "нал"/"готівка"/
  "безнал"/"на карту"/"БН" и т.п.
- Если поле явно не упомянуто в тексте - null, не додумывай значение по
  умолчанию.

Примеры (только фрагмент про тариф, не весь текст заявки):

Вход: "Тариф: 4100/900\nКом 450"
Выход (фрагмент): "avto_baza": 4100, "avto_dop_chas": 900, "tip_rascheta": "почасовка", "kom_avto": {"znachenie": 450, "tip": "сумма"}

Вход: "Тариф 2500\nКом 10%"
Выход (фрагмент): "avto_baza": 2500, "avto_dop_chas": null, "tip_rascheta": "фикс", "kom_avto": {"znachenie": 10, "tip": "%"}

Вход: "Тариф:\nАвто 5000/1200\nВантажники 1600/800\nКом\nАвто 1000/400\nВант 400/200/10"
Выход (фрагмент): "kom_avto": {"tip": "pochasovka", "baza": 1000, "dop_chas": 400}, "kom_gruzchiki": {"tip": "pochasovka", "baza": 400, "dop_chas": 200, "dopy": [10]}
(ВАЖНО: третье число "10" у комиссии грузчиков НЕ теряется - идёт в dopy,
даже если непонятно, за что именно оно - как и с gruzchiki_dopy, не
классифицируем, просто сохраняем.)

ПОДТВЕРЖДЁННЫЙ ПОВТОРЯЮЩИЙСЯ ШАБЛОН - применяй ТОЛЬКО если в тексте
заявки ОДНОВРЕМЕННО есть ОБА условия: 1) упоминание санобработки ("Сан.
оброботка"/"санобробка"/"санітарна обробка") И 2) имя "Виталия" где-либо
в тексте (это конкретный источник заказов с таким форматом тарифа, не
общее правило для любых пяти чисел - у других источников то же
количество чисел может означать совсем другое):
Вход: "Тар БН 2ч 2100/600/200/1000/44\n...Санк книжка, Сан. оброботка...\n...Виталия заказ"
Выход (фрагмент): "avto_baza": 2100, "avto_dop_chas": 600, "forma_oplaty":
"БН", "min_chasov": 2, "kom_avto": null, "kom_gruzchiki": null, "tariff_json":
{"dop_tochka": {"tip": "doplata_fix", "summa": 200}, "prochie_dopy":
[{"nazvanie": "Санобробка", "summa": 1000}], "km_stavka": 44}
(В ЭТОМ конкретном шаблоне "Тар <форма_оплаты> <мін.час>ч
база/доп_час/точка/санобробка/км" - ПЯТЬ чисел подряд - это тариф авто с
тремя доп.платежами (доп.точка/санобробка/км), НИКОГДА не комиссия и
НИКОГДА не грузчики. Санобробка узнаётся по фразе даже если она написана
в другом месте текста, а не рядом с самими числами. Если условие
"санобработка + Виталия" НЕ выполняется - это правило НЕ применяй,
разбирай пять чисел обычными правилами выше (по ключевым словам рядом, а
что не понятно - в neponyatno).)
"""


_NEPONYATNO_ITEM_RE = re.compile(r"^\s*([\d.,]+)\s*:\s*(.+)$")

# Категории, которые имеют смысл только когда в заказе ЕСТЬ грузчики
# (это надбавки к работе грузчиков, не к авто), плюс правдоподобный
# потолок суммы для каждой - подтверждено логистом-владельцем.
_LOADER_ONLY_CANDIDATES = {"этаж": 100, "проход": 50, "вес": 15}


def _filter_neponyatno(result: dict) -> list:
    """GPT не всегда строго следует текстовому правилу про правдоподобные
    величины и про 'нет грузчиков - не предлагай этаж/проход/вес' - даже
    после явного примера в промпте (see 09.08: '500' с этими вариантами
    при заказе без грузчиков вообще). Правим кодом, а не только текстом
    инструкции - так гарантированно, а не 'обычно работает'.
    """
    has_gruzchiki = result.get("gruzchiki_baza") is not None
    filtered = []
    for item in result.get("neponyatno") or []:
        m = _NEPONYATNO_ITEM_RE.match(item)
        if not m:
            filtered.append(item)
            continue
        num_str, cands_str = m.groups()
        try:
            num = float(num_str.replace(",", "."))
        except ValueError:
            filtered.append(item)
            continue
        kept = []
        for cand in (c.strip() for c in cands_str.split("/") if c.strip()):
            low = cand.lower()
            cap = next((v for k, v in _LOADER_ONLY_CANDIDATES.items() if low.startswith(k)), None)
            if cap is not None and (not has_gruzchiki or num > cap):
                continue  # нет грузчиков в заказе или сумма неправдоподобна для этой категории
            kept.append(cand)
        if not kept:
            # Все кандидаты отфильтрованы (были только про грузчиков, а
            # грузчиков в заказе нет / сумма неправдоподобна) - НЕ
            # возвращаем отфильтрованное обратно, подставляем безопасные
            # общие варианты, у которых нет потолка по сумме.
            kept = ["ГБ?", "точка?", "км?"]
        filtered.append(f"{num_str}: " + "/".join(kept))
    return filtered


# field_key -> короткая метка кандидата, как она пишется в "не понял"
# (в нижнем регистре, без "?"). Используется, чтобы при нажатии кнопки
# резолва не переспрашивать число, которое уже написано в предупреждении.
_CANDIDATE_FIELD_LABELS = {
    "gidrobort": "гб",
    "dop_tochka": "точка",
    "km": "км",
    "etazhi": "этаж",
    "prohody": "проход",
    "ves": "вес",
}


def find_pending_neponyatno_value(tariff: dict, field_key: str):
    """Если для этого поля есть подходящее число в 'не понял' (например
    '600: ГБ?/точка?/км?' и поле - gidrobort) - возвращает это число как
    строку, чтобы применить его сразу, без переспроса. Если совпадения
    нет (в том числе если это обычное поле вроде 'Авто база', для
    которого кандидатов не бывает) - возвращает None, обычный флоу."""
    label = _CANDIDATE_FIELD_LABELS.get(field_key)
    if not label:
        return None
    for item in tariff.get("neponyatno") or []:
        m = _NEPONYATNO_ITEM_RE.match(item)
        if not m:
            continue
        num_str, cands_str = m.groups()
        cands = [c.strip().rstrip("?").lower() for c in cands_str.split("/") if c.strip()]
        if label in cands:
            return num_str
    return None


_LOADER_TEXT_RE = re.compile(r"вантаж|грузчик", re.IGNORECASE)


_VITALIYA_RE = re.compile(r"виталия", re.IGNORECASE)
_SANOBROBKA_RE = re.compile(r"сан\.?\s*о?бр[оа]б", re.IGNORECASE)
_FIVE_NUMBERS_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)"
)


def _recover_avto_dop_chas(result: dict, order_text: str):
    """Страховка: если GPT нашёл avto_baza, но НЕ нашёл avto_dop_chas
    (осталось null) - пробуем достать его напрямую регэкспом из текста.
    Реальный случай (11.08): "4750/950БН" - число доп.часа СЛИПЛОСЬ с
    формой оплаты без пробела ("950БН"), GPT не смог вычленить числовую
    часть и заодно ошибочно решил, что раз второго числа нет - это
    "фикс". НЕ пропускаем проверку из-за tip_rascheta="фикс" - как раз в
    этом состоянии чаще всего и нужно восстановление: если в тексте
    реально есть "<база>/<число>", тариф почасовый, что бы GPT ни решил.
    """
    if result.get("avto_dop_chas") is not None:
        return
    baza = result.get("avto_baza")
    if baza is None:
        return
    text = order_text or ""
    # Ищем только рядом со словом "тариф"/"тар" - не по всему тексту,
    # чтобы случайно не зацепить похожее число из адреса/телефона в
    # другом месте заявки.
    kw = re.search(r"тар(?:иф)?", text, re.IGNORECASE)
    search_region = text[kw.start():kw.start() + 120] if kw else text
    baza_str = re.escape(_fmt_num(baza))
    m = re.search(rf"{baza_str}\s*/\s*(\d+(?:[.,]\d+)?)", search_region)
    if not m:
        return
    try:
        result["avto_dop_chas"] = float(m.group(1).replace(",", "."))
        result["tip_rascheta"] = "почасовка"
    except ValueError:
        pass


_TEMPLATE_MARKERS = ("тар авто", "наступна година", "гідроборт", "рокла", "заміський", "вантажник", "вагі від", "прохід")

_RE_AVTO_BAZA_HOURS = re.compile(r"тар\s*авто\s*(\d+(?:[.,]\d+)?)\s*грн\s*/\s*(\d+(?:[.,]\d+)?)\s*годин", re.IGNORECASE)
_RE_AVTO_DOP_CHAS = re.compile(r"(\d+(?:[.,]\d+)?)\s*/\s*наступна\s*година", re.IGNORECASE)
_RE_GIDROBORT = re.compile(r"гідроборт\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_RE_ROKLA = re.compile(r"рокла\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_RE_KM = re.compile(r"заміський\s*(\d+(?:[.,]\d+)?)\s*грн\s*/\s*км", re.IGNORECASE)
_RE_GRUZCHIKI_HEADER = re.compile(r"тар\s*\d+\s*вантажник\w*\s*(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)\s*годин", re.IGNORECASE)
_RE_GRUZCHIKI_DOP_CHAS = re.compile(r"(\d+(?:[.,]\d+)?)\s*грн\s*/\s*наступна", re.IGNORECASE)
_RE_VES = re.compile(r"ваг[іи]\s*від\s*(\d+(?:[.,]\d+)?)\s*по\s*(\d+(?:[.,]\d+)?)\s*грн\s*/\s*кг", re.IGNORECASE)
_RE_VES_DOPY = re.compile(r"прохід\s*\d+\s*м\s*/\s*поверх\s*з\s*вагою\s*по\s*(\d+(?:[.,]\d+)?)\s*грн\s*на\s*людину", re.IGNORECASE)
_RE_KOM_PERCENT = re.compile(r"ком\s*(\d+(?:[.,]\d+)?)\s*%", re.IGNORECASE)


_RE_ROKLA_GENERAL = re.compile(
    r"(?:рокла\s*(\d+(?:[.,]\d+)?))|(?:(\d+(?:[.,]\d+)?)\s*рокла)", re.IGNORECASE
)
_RE_KM_GENERAL = re.compile(r"(\d+(?:[.,]\d+)?)\s*грн\s*/\s*км", re.IGNORECASE)


def _apply_keyword_overrides(result: dict, order_text: str):
    """Общее правило (для ЛЮБОГО заказа, не привязано к конкретному
    шаблону): если рядом с этими словами в тексте стоит число - оно
    ВСЕГДА идёт в соответствующую категорию, что бы GPT ни решил.

    Список НАМЕРЕННО короткий - только слова, где сопоставление
    "слово рядом с числом -> категория" однозначно и без контекстных
    нюансов:
    - "рокла" (тележка/оборудование) - всегда именная допа, никогда не
      доп.ходка/доп.точка
    - "X грн/км" - всегда километраж, даже если по формату похоже на
      доп.точку

    "Этаж"/"проход"/"вес"/"санобробка" сюда НЕ включены - у них есть
    контекстные нюансы (метры vs грн, вес vs допы-с-весом, позиционные
    случаи вроде "Виталия"), где слепое сопоставление слово->число может
    навредить больше, чем помочь - для них остаётся комбинация промпта,
    точечных шаблонов (см. выше) и ручных кнопок правки.
    """
    tj = result.setdefault("tariff_json", {})

    m = _RE_ROKLA_GENERAL.search(order_text or "")
    if m:
        val = float((m.group(1) or m.group(2)).replace(",", "."))
        prochie = [d for d in (tj.get("prochie_dopy") or []) if (d.get("nazvanie") or "").strip().lower() != "рокла"]
        prochie.append({"nazvanie": "Рокла", "summa": val, "group": "avto"})
        tj["prochie_dopy"] = prochie
        # Если это же число ошибочно попало в доп.точку/доп.ходку -
        # убираем оттуда, "рокла" никогда не то и не другое.
        if (tj.get("dop_tochka") or {}).get("summa") == val:
            tj["dop_tochka"] = None
        if (tj.get("dop_hodka") or {}).get("summa") == val:
            tj["dop_hodka"] = None

    m = _RE_KM_GENERAL.search(order_text or "")
    if m:
        val = float(m.group(1).replace(",", "."))
        tj["km_stavka"] = val
        if (tj.get("dop_tochka") or {}).get("summa") == val:
            tj["dop_tochka"] = None


def _apply_avto_gruzchiki_multiline_template(result: dict, order_text: str):
    """Детерминированный разбор для другого повторяющегося многострочного
    шаблона: 'Тар авто X/Yгодини', '.../Наступна година', '+гідроборт',
    '+рокла', '+заміський .../км', затем блок 'Тар N вантажника ...',
    'Вагі від...', 'Прохід ...з вагою по...'. После нескольких повторных
    ошибок GPT в этом же кейсе (доп.точка вместо км, доп.ходка вместо
    рокла, потерянный вес, метры вместо этажей) - не полагаемся на GPT
    для этого шаблона вообще, разбираем прямым регэкспом по фиксированным
    меткам-словам.

    Срабатывает только если в тексте есть ВСЕ характерные маркеры этого
    шаблона одновременно - иначе не трогаем результат.
    """
    text_low = (order_text or "").lower()
    if not all(marker in text_low for marker in _TEMPLATE_MARKERS):
        return

    m = _RE_AVTO_BAZA_HOURS.search(order_text)
    if not m:
        return  # не тот формат, не рискуем
    result["avto_baza"] = float(m.group(1).replace(",", "."))
    result["min_chasov"] = float(m.group(2).replace(",", "."))
    result["tip_rascheta"] = "почасовка"

    m = _RE_AVTO_DOP_CHAS.search(order_text)
    if m:
        result["avto_dop_chas"] = float(m.group(1).replace(",", "."))

    tj = result.setdefault("tariff_json", {})
    # В этом шаблоне доп.точки и доп.ходки не бывает вообще - если GPT их
    # ошибочно насочинял из чисел гідроборта/рокли/км, явно обнуляем, а
    # не просто дополняем результат сверху.
    tj["dop_tochka"] = None
    tj["dop_hodka"] = None

    m = _RE_GIDROBORT.search(order_text)
    if m:
        tj["gidrobort"] = {"summa": float(m.group(1).replace(",", ".")), "po_faktu": True}

    prochie = [d for d in (tj.get("prochie_dopy") or []) if (d.get("nazvanie") or "").strip().lower() not in ("рокла", "допы с весом")]

    m = _RE_ROKLA.search(order_text)
    if m:
        prochie.append({"nazvanie": "Рокла", "summa": float(m.group(1).replace(",", ".")), "group": "avto"})

    m = _RE_KM.search(order_text)
    if m:
        tj["km_stavka"] = float(m.group(1).replace(",", "."))

    m = _RE_GRUZCHIKI_HEADER.search(order_text)
    if m:
        result["gruzchiki_baza"] = float(m.group(1).replace(",", "."))
        result["gruzchiki_chasov"] = float(m.group(2).replace(",", "."))

    m = _RE_GRUZCHIKI_DOP_CHAS.search(order_text)
    if m:
        result["gruzchiki_dop_chas"] = float(m.group(1).replace(",", "."))
    result["gruzchiki_dopy"] = []  # в этом шаблоне все доп.числа классифицированы явно, "хвоста" не остаётся

    m = _RE_VES.search(order_text)
    if m:
        tj["ves"] = {
            "tip": "porogovaya",
            "porogi": [{"ot": float(m.group(1).replace(",", ".")), "stavka": float(m.group(2).replace(",", "."))}],
        }

    m = _RE_VES_DOPY.search(order_text)
    if m:
        prochie.append({"nazvanie": "Допы с весом", "summa": float(m.group(1).replace(",", ".")), "group": "gruzchiki"})

    tj["prochie_dopy"] = prochie
    tj["etazhi_stavka"] = None  # "Прохід 20м" - метры, не грн, это НЕ этажи (см. правило выше)
    tj["prohody_stavka"] = None  # эта фраза целиком уже разобрана как "Допы с весом" выше

    m = _RE_KOM_PERCENT.search(order_text)
    if m:
        result["kom_avto"] = {"znachenie": float(m.group(1).replace(",", ".")), "tip": "%"}

    result["kom_gruzchiki"] = None  # в этом шаблоне комиссия одна общая, не отдельная для грузчиков

    # Все числа этого шаблона теперь классифицированы детерминированно -
    # предупреждения "не понял" по ним больше не нужны.
    result["neponyatno"] = []


def _apply_vitaliya_sanobrobka_template(result: dict, order_text: str):
    """Детерминированный оверрайд для конкретного повторяющегося шаблона
    (источник 'Виталия' + упоминание санобработки): тариф авто из 5 чисел
    ВСЕГДА означает база/доп_час/точка/санобробка/км - подтверждено
    логистом-владельцем как железное правило. Не полагаемся на то, что
    GPT правильно применит инструкцию из промпта (после нескольких
    ошибок в этом же кейсе) - переопределяем результат кодом напрямую,
    если оба условия совпали и в тексте нашлись ровно 5 чисел подряд.

    Применяется ТОЛЬКО при обоих условиях сразу - для других источников/
    заказов те же пять чисел могут означать что угодно другое, трогать
    их нельзя.
    """
    if not (_VITALIYA_RE.search(order_text or "") and _SANOBROBKA_RE.search(order_text or "")):
        return
    m = _FIVE_NUMBERS_RE.search(order_text or "")
    if not m:
        return  # формат не совпал - не рискуем, оставляем как разобрал GPT

    baza, dop_chas, tochka, san, km = (float(g.replace(",", ".")) for g in m.groups())

    result["avto_baza"] = baza
    result["avto_dop_chas"] = dop_chas
    result["tip_rascheta"] = "почасовка"
    result["kom_avto"] = None
    result["kom_gruzchiki"] = None
    result["gruzchiki_baza"] = None
    result["gruzchiki_dop_chas"] = None
    result["gruzchiki_dopy"] = []

    tj = result.setdefault("tariff_json", {})
    tj["dop_tochka"] = {"tip": "doplata_fix", "summa": tochka}
    tj["km_stavka"] = km
    prochie = [d for d in (tj.get("prochie_dopy") or []) if (d.get("nazvanie") or "").strip().lower() != "санобробка"]
    prochie.append({"nazvanie": "Санобробка", "summa": san})
    tj["prochie_dopy"] = prochie

    # Числа теперь классифицированы детерминированно - убираем по ним
    # любые предупреждения "не понял", если GPT успел их туда добавить.
    used = {_fmt_num(n) for n in (baza, dop_chas, tochka, san, km)}
    kept = []
    for item in result.get("neponyatno") or []:
        m2 = _NEPONYATNO_ITEM_RE.match(item)
        if m2 and m2.group(1) in used:
            continue
        kept.append(item)
    result["neponyatno"] = kept


def _strip_bogus_gruzchiki(result: dict, order_text: str):
    """КРИТИЧНАЯ защита: если в самом тексте заявки нет ни слова
    'вантажник', ни 'грузчик' - в заказе НЕТ грузчиков, точка. Обнаружен
    реальный случай (10.08): заказ "3т + ГБ" без единого упоминания
    грузчиков, а GPT всё равно сочинил gruzchiki_baza/gruzchiki_dop_chas
    из лишних чисел строки тарифа АВТО (позиционная путаница - решил,
    что 3-е/4-е/5-е числа это отдельный блок вантажников, хотя это
    просто хвост тарифа авто без явного назначения).

    Форсируем очистку кодом, а не полагаемся на промпт - но числа не
    теряем: переносим их в neponyatno, чтобы логист мог сам разобрать
    через кнопки (Точка/Км/ГБ и т.п.), а не остаться с придуманной
    строкой "Грузчики" в заказе, где грузчиков вообще не было.
    """
    if _LOADER_TEXT_RE.search(order_text or ""):
        return  # текст явно упоминает грузчиков - ничего не трогаем

    stray = []
    if result.get("gruzchiki_baza") is not None:
        stray.append(result["gruzchiki_baza"])
    if result.get("gruzchiki_dop_chas") is not None:
        stray.append(result["gruzchiki_dop_chas"])
    stray.extend(result.get("gruzchiki_dopy") or [])
    if not stray:
        return  # GPT и так не заполнял - нечего чистить

    result["gruzchiki_baza"] = None
    result["gruzchiki_dop_chas"] = None
    result["gruzchiki_dopy"] = []

    neponyatno = list(result.get("neponyatno") or [])
    for n in stray:
        neponyatno.append(f"{_fmt_num(n)}: ГБ?/точка?/км?")
    result["neponyatno"] = neponyatno


_KOM_TEXT_RE = re.compile(r"ком|коміс", re.IGNORECASE)


def _kom_stray_numbers(k):
    if not k:
        return []
    if k.get("tip") == "pochasovka":
        nums = []
        if k.get("baza") is not None:
            nums.append(k["baza"])
        if k.get("dop_chas") is not None:
            nums.append(k["dop_chas"])
        nums.extend(k.get("dopy") or [])
        return nums
    if k.get("znachenie") is not None and k.get("tip") != "%":
        return [k["znachenie"]]  # % без слова "ком" тоже подозрительно, но сумма надёжнее показывает утечку чисел
    return []


def _strip_bogus_kom(result: dict, order_text: str):
    """Аналогичная защита для комиссии: если в тексте заявки вообще нет
    слова 'ком'/'коміс' - kom_avto/kom_gruzchiki не должны быть заполнены
    вообще, даже если GPT попытался впихнуть туда лишние числа из строки
    тарифа авто (реальный кейс 10.08: "2100/600/200/1000/44" без единого
    слова про комиссию - GPT насочинял kom_avto из чисел, которые на
    самом деле доп.точка/санобробка/км)."""
    if _KOM_TEXT_RE.search(order_text or ""):
        return  # текст явно упоминает комиссию - не трогаем

    stray = _kom_stray_numbers(result.get("kom_avto")) + _kom_stray_numbers(result.get("kom_gruzchiki"))
    if not stray:
        return

    result["kom_avto"] = None
    result["kom_gruzchiki"] = None

    neponyatno = list(result.get("neponyatno") or [])
    for n in stray:
        neponyatno.append(f"{_fmt_num(n)}: ГБ?/точка?/км?")
    result["neponyatno"] = neponyatno


def _strip_bogus_gidrobort(result: dict):
    """Упоминание 'гідроборт' как характеристики авто (например '5т
    гідроборт + 2 вантажника') НЕ означает отдельную доплату - по
    умолчанию считаем, что она уже в тарифе. Платная доплата бывает
    ТОЛЬКО с явной суммой 'по факту використання'. GPT иногда всё равно
    создаёт запись с summa=0/пусто просто от упоминания слова - это
    ловим и убираем кодом, а не полагаемся только на текст промпта.
    Если логист вручную решит, что доплата всё же нужна - внесёт через
    кнопку 'ГБ' в меню правки, как обычно.
    """
    tj = result.get("tariff_json") or {}
    gb = tj.get("gidrobort")
    if gb and not gb.get("summa"):
        tj["gidrobort"] = None


_LOADER_STRAY_KEYWORDS = ("ваг", "доп", "вес", "этаж", "поверх", "прохід", "проход")


def _reconcile_neponyatno(result: dict):
    """Две вещи, которые код гарантирует сам, а не полагается на то, что
    GPT в этот раз правильно следовал тексту промпта:

    1) Если число уже успешно классифицировано в другое поле (доп.точка/
       км/этажи/проходы/гідроборт) - оно не должно ТАКЖЕ висеть в
       neponyatno как будто не разобрано. Убираем дубль.
    2) Если в заказе есть грузчики, а среди 'не понял' есть числа с
       явно 'грузчицкими' кандидатами (вага/доп/этаж/проход) - это
       почти наверняка лишние числа из строки 'Вантажники ...', которые
       по правилу должны были просто уйти в gruzchiki_dopy без
       классификации, но GPT иногда всё равно пытается их классифицировать.
       Переносим такие числа в gruzchiki_dopy и убираем предупреждение -
       не теряем число молча и не пугаем логиста лишним вопросом.
    """
    tj = result.get("tariff_json") or {}
    used_numbers = set()
    for key in ("dop_tochka", "gidrobort"):
        d = tj.get(key)
        if d and d.get("summa") is not None:
            used_numbers.add(_fmt_num(d["summa"]))
    for key in ("km_stavka", "etazhi_stavka", "prohody_stavka"):
        v = tj.get(key)
        if v is not None:
            used_numbers.add(_fmt_num(v))
    for dop in tj.get("prochie_dopy") or []:
        if dop.get("summa") is not None:
            used_numbers.add(_fmt_num(dop["summa"]))

    has_gruzchiki = result.get("gruzchiki_baza") is not None
    gruzchiki_dopy = list(result.get("gruzchiki_dopy") or [])

    kept = []
    for item in result.get("neponyatno") or []:
        m = _NEPONYATNO_ITEM_RE.match(item)
        if not m:
            kept.append(item)
            continue
        num_str, cands_str = m.groups()
        if num_str in used_numbers:
            continue  # уже классифицировано в другом поле - дубль убираем

        if has_gruzchiki and any(k in cands_str.lower() for k in _LOADER_STRAY_KEYWORDS):
            try:
                val = float(num_str.replace(",", "."))
                if val not in gruzchiki_dopy:  # не дублируем то, что уже там есть
                    gruzchiki_dopy.append(val)
                continue  # ушло в gruzchiki_dopy, предупреждение не нужно
            except ValueError:
                pass

        kept.append(item)

    result["neponyatno"] = kept
    if gruzchiki_dopy:
        result["gruzchiki_dopy"] = gruzchiki_dopy

    _strip_ves_when_gruzchiki(result)


def _strip_ves_when_gruzchiki(result: dict):
    """GPT дважды подряд путал числа из строки 'Вантажники ...' с
    пороговой таблицей веса - один раз с совпадающими числами (что ловил
    предыдущий дедуп), второй раз со сломанной структурой (порог без
    ставки: 'від 50кг по грн'). Раз даже сравнение чисел ненадёжно -
    просто НЕ доверяем полю 'ves', когда в заказе вообще есть грузчики.
    Подтверждено логистом-владельцем: такие числа - это допы, а не
    структурированный вес, даже если среди них есть правдоподобное
    значение (например действительно похожее на грн/кг) - разбираться,
    что из чисел реально вес, логист будет сам, глядя на исходный текст.
    Реальная пороговая таблица веса (случай F) настолько редка и
    специфично описана в тексте, что подавляющее большинство срабатываний
    этого поля при наличии грузчиков - ошибка, а не находка."""
    if result.get("gruzchiki_baza") is None:
        return
    tj = result.get("tariff_json") or {}
    if tj.get("ves"):
        tj["ves"] = None


def parse_tariff_via_gpt(order_text: str) -> dict:
    """Возвращает распарсенный тариф как dict (см. _TARIFF_SYSTEM_PROMPT).
    При любой ошибке (нет клиента, невалидный JSON, таймаут) возвращает
    dict с пустыми полями и neponyatno=["не удалось разобрать тариф"] -
    превью в этом случае покажет логисту, что нужно исправить руками,
    вместо того чтобы тихо потерять заказ.
    """
    fallback = {
        "avto_baza": None, "min_chasov": None, "avto_dop_chas": None,
        "gruzchiki_baza": None, "gruzchiki_dop_chas": None, "gruzchiki_dopy": [],
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
        result = json.loads(raw)
        _apply_vitaliya_sanobrobka_template(result, order_text)
        _apply_avto_gruzchiki_multiline_template(result, order_text)
        _apply_keyword_overrides(result, order_text)
        _recover_avto_dop_chas(result, order_text)
        _strip_bogus_gruzchiki(result, order_text)
        _strip_bogus_kom(result, order_text)
        result["neponyatno"] = _filter_neponyatno(result)
        _strip_bogus_gidrobort(result)
        _reconcile_neponyatno(result)
        return result
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
    tj = tariff.get("tariff_json") or {}

    # --- Блок "Авто": сама ставка + все доп.начисления, привязанные к
    # авто (доп.точка/гідроборт/доп.ходка/км/именные допы группы avto) ---
    base = tariff.get("avto_baza")
    if tariff.get("tip_rascheta") == "фикс":
        lines.append(f"Авто: {_fmt_num(base)} (фикса){star(['avto_baza', 'avto_dop_chas', 'min_chasov'])}")
    elif base is not None:
        min_h = tariff.get("min_chasov")
        dop = _fmt_num(tariff.get("avto_dop_chas"))
        hours_part = f" ({_fmt_num(min_h)}ч)" if min_h is not None else ""
        lines.append(f"Авто: {_fmt_num(base)}{hours_part}/{dop}{star(['avto_baza', 'avto_dop_chas', 'min_chasov'])}")

    dt = tj.get("dop_tochka")
    if dt:
        if dt.get("tip") == "pereschet_minimalki":
            lines.append(f"Доп.точка: новый минимум {_fmt_num(dt.get('summa'))} грн{star(['dop_tochka'])}")
        else:
            lines.append(f"Доп.точка: +{_fmt_num(dt.get('summa'))} грн{star(['dop_tochka'])}")

    lines.extend(_format_avto_extra_lines(tj, star))

    # --- Блок "Грузчики": сама ставка + всё, что привязано к грузчикам
    # (вес/этажи/проходы/именные допы группы gruzchiki) ---
    gr_base = tariff.get("gruzchiki_baza")
    if gr_base is not None:
        gr_dop = _fmt_num(tariff.get("gruzchiki_dop_chas"))
        # По умолчанию 2ч (подтверждённое бизнес-правило), но теперь
        # можно поправить вручную через кнопку "Часы" у грузчиков.
        gr_chasov = _fmt_num(tariff.get("gruzchiki_chasov")) or "2"
        gr_line = f"Грузчики: {_fmt_num(gr_base)} ({gr_chasov}ч)/{gr_dop}"
        gr_extra = tariff.get("gruzchiki_dopy") or []
        if gr_extra:
            # Доп.числа (вес/этаж/проход и т.п.) НЕ классифицируем - просто
            # дописываем по порядку как в тексте заявки, логист сам знает,
            # что есть что, глядя на исходную заявку рядом.
            gr_line += "/" + "/".join(_fmt_num(n) for n in gr_extra)
        lines.append(gr_line + star(["gruzchiki_baza", "gruzchiki_dop_chas", "gruzchiki_dopy", "gruzchiki_chasov"]))

    lines.extend(_format_gruzchiki_extra_lines(tj, star))

    # --- Комиссия, плательщик, форма оплаты - в конце ---
    kom_avto = tariff.get("kom_avto")
    kom_gruz = tariff.get("kom_gruzchiki")

    def kom_str(k):
        if not k:
            return None
        raw = _kom_cell(k)
        return raw if k.get("tip") == "%" else f"{raw} грн"

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

    if tariff.get("forma_oplaty"):
        lines.append(f"Форма оплаты: {tariff['forma_oplaty']}{star(['forma_oplaty', 'platelshik'])}")
    else:
        lines.append("⚠️ Форма оплаты: не указана")

    for item in tariff.get("neponyatno") or []:
        lines.append(f"⚠️ не понял: {item}")

    return "\n".join(lines)


def _format_avto_extra_lines(tj: dict, star) -> list:
    """Доп.начисления, которые ВСЕГДА относятся к авто (не к грузчикам):
    гідроборт, доп.ходка, км, плюс именные допы с group='avto'."""
    lines = []

    gb = tj.get("gidrobort")
    if gb and gb.get("summa"):
        lines.append(f"Гідроборт: {_fmt_num(gb.get('summa'))} (по факту){star(['gidrobort'])}")

    dh = tj.get("dop_hodka")
    if dh:
        if dh.get("tip") == "сумма":
            lines.append(f"Доп.ходка: {_fmt_num(dh.get('summa'))} грн")
        elif dh.get("tip") == "vhodit_v_tarif":
            lines.append("Доп.ходка: входит в тариф")
        elif dh.get("tip") == "utochnit":
            lines.append("Доп.ходка: уточнить")

    if tj.get("km_stavka") is not None:
        lines.append(f"Км: {_fmt_num(tj['km_stavka'])} грн/км{star(['km'])}")

    for dop in tj.get("prochie_dopy") or []:
        if (dop.get("group") or "avto") != "avto":
            continue
        summa = dop.get("summa")
        summa_str = f"{_fmt_num(summa)} грн" if summa is not None else "по факту"
        nazvanie = dop.get("nazvanie", "Доп")
        dop_star = star(["rokla"]) if nazvanie.strip().lower() == "рокла" else ""
        lines.append(f"{nazvanie}: {summa_str}{dop_star}")

    return lines


def _format_gruzchiki_extra_lines(tj: dict, star) -> list:
    """Доп.начисления, которые ВСЕГДА относятся к грузчикам: вес, этажи,
    проходы, плюс именные допы с group='gruzchiki'."""
    lines = []

    ves = tj.get("ves")
    if ves:
        if ves.get("tip") == "porogovaya" and ves.get("porogi"):
            # Пропускаем сломанные пороги (без начала или без ставки) -
            # лучше не показать вообще, чем показать пустое "по грн".
            parts = [
                f"від {_fmt_num(p.get('ot'))}кг по {_fmt_num(p.get('stavka'))}грн"
                for p in ves["porogi"]
                if p.get("ot") is not None and p.get("stavka") is not None
            ]
            if parts:
                lines.append("Вес: " + ", ".join(parts) + star(["ves"]))
        elif ves.get("stavka") is not None:
            lines.append(f"Вес: {_fmt_num(ves.get('stavka'))} грн/кг{star(['ves'])}")

    if tj.get("etazhi_stavka") is not None:
        lines.append(f"Этажи: {_fmt_num(tj['etazhi_stavka'])} грн{star(['etazhi'])}")

    if tj.get("prohody_stavka") is not None:
        lines.append(f"Проходы: {_fmt_num(tj['prohody_stavka'])} грн{star(['prohody'])}")

    for dop in tj.get("prochie_dopy") or []:
        if dop.get("group") != "gruzchiki":
            continue
        summa = dop.get("summa")
        summa_str = f"{_fmt_num(summa)} грн" if summa is not None else "по факту"
        lines.append(f"{dop.get('nazvanie', 'Доп')}: {summa_str}")

    return lines


# ---------------------------------------------------------------------------
# Запись тарифа в Sheets - целиком или отдельными полями
# ---------------------------------------------------------------------------
def _kom_cell(k):
    """Компактная строка для записи в ячейку Ком_авто/Ком_грузчики.
    Поддерживает три формата комиссии:
    - {"tip": "%", "znachenie": N} -> "10%"
    - {"tip": "сумма", "znachenie": N} -> "500"
    - {"tip": "pochasovka", "baza": N, "dop_chas": N, "dopy": [...]} -> "1000/400/10"
      (ступенчатая комиссия - сама имеет базу+доп.час, как обычный тариф;
      структура при этом полностью сохраняется в Тариф_JSON для будущего
      калькулятора, здесь только читаемая строка для глаз)
    """
    if not k:
        return ""
    if k.get("tip") == "pochasovka":
        parts = [_fmt_num(k.get("baza"))]
        if k.get("dop_chas") is not None:
            parts.append(_fmt_num(k.get("dop_chas")))
        parts += [_fmt_num(n) for n in (k.get("dopy") or [])]
        return "/".join(parts)
    v = _fmt_num(k.get("znachenie"))
    return f"{v}%" if k.get("tip") == "%" else f"{v}"


def build_tariff_row_values(tariff: dict) -> dict:
    """{имя_колонки: значение_для_записи} для всех тарифных колонок -
    общий источник и для полной записи (при ✅), и для точечной записи
    одного поля (при правке через карандаш)."""
    tj = dict(tariff.get("tariff_json") or {})
    gr_dopy = tariff.get("gruzchiki_dopy") or []
    if gr_dopy:
        tj["gruzchiki_dopy"] = gr_dopy
    if tariff.get("gruzchiki_chasov") is not None:
        tj["gruzchiki_chasov"] = tariff["gruzchiki_chasov"]

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
        "Тариф_JSON": json.dumps(tj, ensure_ascii=False),
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
        raise ValueError("нужно число, например 10% или 500 или 1000/400")
    if t.endswith("%"):
        try:
            return {"znachenie": float(t[:-1].strip().replace(",", ".")), "tip": "%"}
        except ValueError:
            raise ValueError("нужно число перед %, например 10%")
    if "/" in t:
        try:
            nums = [float(p.strip().replace(",", ".")) for p in t.split("/")]
        except ValueError:
            raise ValueError("нужны числа через /, например 1000/400")
        result = {"tip": "pochasovka", "baza": nums[0]}
        if len(nums) >= 2:
            result["dop_chas"] = nums[1]
        if len(nums) > 2:
            result["dopy"] = nums[2:]
        return result
    try:
        return {"znachenie": float(t.replace(",", ".")), "tip": "сумма"}
    except ValueError:
        raise ValueError("нужно число, например 10% или 500 или 1000/400")


def _parse_platelshik(text: str):
    t = text.strip().lower()
    if t.startswith("клі") or t.startswith("кли"):
        return "Клиент"
    if t.startswith("дисп"):
        return "Диспетчер"
    raise ValueError("напишите 'Клиент' или 'Диспетчер'")


def _parse_forma_oplaty(text: str):
    t = text.strip().lower()
    if t in ("нал", "готівка", "готовка", "cash"):
        return "Нал"
    if t in ("бн", "безнал", "карта", "card"):
        return "БН"
    raise ValueError("напишите 'Нал' или 'БН'")


def _recompute_tip_rascheta(tariff):
    """Тип расчёта всегда выводится из наличия доп.часа авто - отдельной
    ручной кнопки для него больше нет, чтобы значение не могло разойтись
    с фактическими цифрами (баг 09.08: правка одного числа без второго
    случайно превращала почасовку в фикс)."""
    tariff["tip_rascheta"] = "почасовка" if tariff.get("avto_dop_chas") is not None else "фикс"


_EMPTY_VALUES = {"", "-", "0", "нет", "немає", "net"}


def _apply_avto_baza(tariff, text):
    tariff["avto_baza"] = _parse_one_number(text)
    _recompute_tip_rascheta(tariff)


def _apply_avto_dop_chas(tariff, text):
    t = text.strip().lower()
    tariff["avto_dop_chas"] = None if t in _EMPTY_VALUES else _parse_one_number(text)
    _recompute_tip_rascheta(tariff)


def _apply_min_chasov(tariff, text):
    tariff["min_chasov"] = _parse_one_number(text)


def _apply_gruzchiki_baza(tariff, text):
    t = text.strip().lower()
    # "-" / "0" очищает базу - это единственный способ убрать строку
    # "Грузчики" целиком, если бот ошибочно её насочинял (см. защиту
    # _strip_bogus_gruzchiki - но для уже отправленных превью до фикса
    # нужен ручной способ убрать).
    tariff["gruzchiki_baza"] = None if t in _EMPTY_VALUES else _parse_one_number(text)


def _apply_gruzchiki_dop_chas(tariff, text):
    t = text.strip().lower()
    tariff["gruzchiki_dop_chas"] = None if t in _EMPTY_VALUES else _parse_one_number(text)


def _apply_gruzchiki_chasov(tariff, text):
    tariff["gruzchiki_chasov"] = _parse_one_number(text)


def _apply_kom_avto(tariff, text):
    tariff["kom_avto"] = _parse_kom(text)


def _apply_kom_gruzchiki(tariff, text):
    tariff["kom_gruzchiki"] = _parse_kom(text)


def _apply_platelshik(tariff, text):
    tariff["platelshik"] = _parse_platelshik(text)
    # Подтверждённое правило: диспетчер платит безналом, во всех
    # остальных случаях по умолчанию - нал (см. apply_defaults).
    if not tariff.get("forma_oplaty"):
        tariff["forma_oplaty"] = "БН" if tariff["platelshik"] == "Диспетчер" else "Нал"


def _apply_forma_oplaty(tariff, text):
    tariff["forma_oplaty"] = _parse_forma_oplaty(text)


def _resolve_neponyatno(tariff):
    """При правке любого из 'кандидатных' полей (ГБ/точка/этаж/проход)
    считаем неоднозначность разрешённой и снимаем предупреждения - иначе
    строка '⚠️ не понял: 600 ГБ?/точка?...' зависла бы в превью даже
    после того, как логист явно указал, чем число является."""
    tariff["neponyatno"] = []


def _apply_gidrobort(tariff, text):
    t = text.strip().lower()
    tj = tariff.setdefault("tariff_json", {})
    if t in _EMPTY_VALUES:
        tj["gidrobort"] = None
        _resolve_neponyatno(tariff)
        return
    v = _parse_one_number(text)
    if v == 0:
        raise ValueError("0 не похоже на доплату 'по факту' - если доплаты нет, оставьте пустым ('-')")
    tj["gidrobort"] = {"summa": v, "po_faktu": True}
    _resolve_neponyatno(tariff)


def _apply_dop_tochka(tariff, text):
    tj = tariff.setdefault("tariff_json", {})
    tj["dop_tochka"] = {"tip": "doplata_fix", "summa": _parse_one_number(text)}
    _resolve_neponyatno(tariff)


def _apply_km(tariff, text):
    tj = tariff.setdefault("tariff_json", {})
    tj["km_stavka"] = _parse_one_number(text)
    _resolve_neponyatno(tariff)


def _apply_etazhi(tariff, text):
    v = _parse_one_number(text)
    if v < 20 or v > 100:
        raise ValueError(f"{_fmt_num(v)} не похоже на этаж (обычно 20-100 грн: 20-30 для 1-10 этажа, до 100 выше) - проверьте цифру")
    tj = tariff.setdefault("tariff_json", {})
    tj["etazhi_stavka"] = v
    _resolve_neponyatno(tariff)


def _apply_prohody(tariff, text):
    v = _parse_one_number(text)
    if v < 20 or v > 50:
        raise ValueError(f"{_fmt_num(v)} не похоже на проход/допу (обычно 20-30 без весовых предметов, до 50 с ними) - проверьте цифру")
    tj = tariff.setdefault("tariff_json", {})
    tj["prohody_stavka"] = v
    _resolve_neponyatno(tariff)


def _apply_ves(tariff, text):
    v = _parse_one_number(text)
    if v > 15:
        raise ValueError(f"{_fmt_num(v)} многовато для веса (обычно до 15 грн/кг) - проверьте цифру")
    tj = tariff.setdefault("tariff_json", {})
    tj["ves"] = {"tip": "ploskaya", "stavka": v}
    _resolve_neponyatno(tariff)


def _apply_rokla(tariff, text):
    t = text.strip().lower()
    tj = tariff.setdefault("tariff_json", {})
    prochie = [d for d in (tj.get("prochie_dopy") or []) if (d.get("nazvanie") or "").strip().lower() != "рокла"]
    if t not in _EMPTY_VALUES:
        prochie.append({"nazvanie": "Рокла", "summa": _parse_one_number(text), "group": "avto"})
    tj["prochie_dopy"] = prochie
    _resolve_neponyatno(tariff)


# field_key -> {label, hint, apply(tariff, text), columns затрагиваемые в Sheets}
# Каждое поле правится НЕЗАВИСИМО одним числом - никаких "два числа через
# /" в одном вводе, чтобы нельзя было случайно стереть соседнее значение,
# затронув только одну часть тарифа.
FIELD_DEFS = {
    "avto_baza": {
        "label": "Авто база", "hint": "например: 4100",
        "apply": _apply_avto_baza, "columns": ["Авто_база", "Тип_расчёта"],
    },
    "avto_dop_chas": {
        "label": "Авто доп.час", "hint": "например: 900 (или '-' если фикс без доп.часа)",
        "apply": _apply_avto_dop_chas, "columns": ["Авто_доп_час", "Тип_расчёта"],
    },
    "min_chasov": {
        "label": "Часы", "hint": "например: 3",
        "apply": _apply_min_chasov, "columns": ["Мин_часов"],
    },
    "gruzchiki_baza": {
        "label": "Грузчики база", "hint": "например: 2400",
        "apply": _apply_gruzchiki_baza, "columns": ["Грузчики_база"],
    },
    "gruzchiki_dop_chas": {
        "label": "Грузчики доп.час", "hint": "например: 1200",
        "apply": _apply_gruzchiki_dop_chas, "columns": ["Грузчики_доп_час"],
    },
    "gruzchiki_chasov": {
        "label": "Часы (грузчики)", "hint": "например: 2 (по умолчанию 2, если не поправлено)",
        "apply": _apply_gruzchiki_chasov, "columns": ["Тариф_JSON"],
    },
    "rokla": {
        "label": "Рокла", "hint": "например: 400 (или '-' чтобы убрать)",
        "apply": _apply_rokla, "columns": ["Тариф_JSON"],
    },
    "kom_avto": {
        "label": "Ком. авто", "hint": "10% или 500, или 1000/400 - ступенчато по часам",
        "apply": _apply_kom_avto, "columns": ["Ком_авто"],
    },
    "kom_gruzchiki": {
        "label": "Ком. грузчики", "hint": "10% или 500, или 400/200/10 - ступенчато по часам",
        "apply": _apply_kom_gruzchiki, "columns": ["Ком_грузчики"],
    },
    "platelshik": {
        "label": "Плательщик", "hint": "Клиент или Диспетчер",
        "apply": _apply_platelshik, "columns": ["Плательщик", "Форма_оплаты"],
    },
    "forma_oplaty": {
        "label": "Форма оплаты", "hint": "Нал или БН",
        "apply": _apply_forma_oplaty, "columns": ["Форма_оплаты"],
    },
    "gidrobort": {
        "label": "Гідроборт", "hint": "например: 950 (по факту использования)",
        "apply": _apply_gidrobort, "columns": ["Тариф_JSON"],
    },
    "dop_tochka": {
        "label": "Доп.точка", "hint": "например: 500",
        "apply": _apply_dop_tochka, "columns": ["Тариф_JSON"],
    },
    "km": {
        "label": "Км", "hint": "например: 15 (грн/км)",
        "apply": _apply_km, "columns": ["Тариф_JSON"],
    },
    "etazhi": {
        "label": "Этажи", "hint": "20-30 для 1-10 этажа, до 100 выше",
        "apply": _apply_etazhi, "columns": ["Тариф_JSON"],
    },
    "prohody": {
        "label": "Проходы", "hint": "20-30 обычно, до 50 с весовыми предметами",
        "apply": _apply_prohody, "columns": ["Тариф_JSON"],
    },
    "ves": {
        "label": "Вес", "hint": "например: 4 (грн/кг, до 15)",
        "apply": _apply_ves, "columns": ["Тариф_JSON"],
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


# Раскладка меню правки задана явно (не автоматически по 2) - сгруппирована
# по смыслу: 🚖 авто, 🏋️‍♀️ грузчики, 💲 деньги/оплата.
_EDIT_MENU_ROWS = [
    ["avto_baza", "avto_dop_chas", "min_chasov"],
    ["km", "dop_tochka", "gidrobort"],
    ["rokla", "kom_avto", "kom_gruzchiki"],
    ["gruzchiki_baza", "gruzchiki_dop_chas", "gruzchiki_chasov"],
    ["etazhi", "prohody", "ves"],
    ["platelshik", "forma_oplaty"],
]

_EDIT_MENU_LABELS = {
    "avto_baza": "🚘 Авто",
    "avto_dop_chas": "🚘 Доп час",
    "min_chasov": "🚘 Часы",
    "km": "🚘 Км",
    "dop_tochka": "🚘 Точка",
    "gidrobort": "🚘 ГБ",
    "rokla": "🚘 Рокла",
    "kom_avto": "🚘 Ком авто",
    "kom_gruzchiki": "🏋️\u200d♀️ Ком грузчики",
    "gruzchiki_baza": "🏋️\u200d♀️ Грузчики",
    "gruzchiki_dop_chas": "🏋️\u200d♀️ Доп час",
    "gruzchiki_chasov": "🏋️\u200d♀️ Часы",
    "etazhi": "🏋️\u200d♀️ Этаж",
    "prohody": "🏋️\u200d♀️ Проход",
    "ves": "🏋️\u200d♀️ Вес",
    "platelshik": "🧮 Плательщик",
    "forma_oplaty": "🧮 Нал-Б/Н",
}


def tariff_edit_menu_keyboard(order_key: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(_EDIT_MENU_LABELS[k], callback_data=f"editfield|{k}|{order_key}")
            for k in row_keys
        ]
        for row_keys in _EDIT_MENU_ROWS
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"editback|{order_key}")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Telegram: обработка нажатий кнопок ✅ / ✏️
# ---------------------------------------------------------------------------
def apply_defaults(sheet, row, tariff: dict) -> dict:
    """Подставляет бизнес-умолчания, которые не должны зависеть от того,
    упомянуты ли они явно в тексте заявки. Мутирует и возвращает тот же
    dict для удобства цепочки вызовов.

    1) Мин.часов по типу авто (см. default_min_hours_for_vehicle) - если
       GPT не нашёл явных мин.часов в тексте и это не фикс.
    2) Форма оплаты: если плательщик - Диспетчер и форма не указана явно
       -> БН (платежи от диспетчера у GoroD всегда безналичные). Во ВСЕХ
       остальных случаях, когда форма не указана явно (включая случаи,
       когда плательщик - Клиент или не определён) -> Нал по умолчанию.
       Итог: предупреждение "форма оплаты не указана" теперь не должно
       появляться вообще - у бота всегда есть разумное умолчание.
    """
    if tariff.get("tip_rascheta") != "фикс" and tariff.get("min_chasov") is None:
        vt_col = ensure_vehicle_type_column(sheet)
        vehicle_type_text = row[vt_col - 1] if vt_col and len(row) >= vt_col else ""
        default_hours = default_min_hours_for_vehicle(vehicle_type_text)
        if default_hours is not None:
            tariff["min_chasov"] = default_hours

    if not tariff.get("forma_oplaty"):
        tariff["forma_oplaty"] = "БН" if tariff.get("platelshik") == "Диспетчер" else "Нал"

    return tariff


def read_tariff_from_sheet(sheet, row_num: int):
    """Восстанавливает тариф из уже записанных значений в 'Orders clean'
    (а не заново через GPT) - используется, когда логист открывает правку
    ПОСЛЕ подтверждения. Если бы вместо этого мы заново парсили сырой
    текст через GPT, все прошлые ручные правки логиста откатились бы к
    исходному (возможно ошибочному) разбору - это как раз то, что мы
    хотим избежать.

    Возвращает None, если тариф ещё ни разу не подтверждался (колонка
    Авто_база пуста) - тогда вызывающий код должен разобрать через GPT
    с нуля, как обычно.
    """
    cols = ensure_tariff_columns(sheet)
    row = sheet.row_values(row_num)

    def cell(name):
        idx = cols[name]
        return row[idx - 1] if len(row) >= idx else ""

    avto_baza_raw = cell("Авто_база")
    if not avto_baza_raw:
        return None  # ещё не подтверждали - восстанавливать нечего

    def to_num(s):
        s = (s or "").strip()
        if not s:
            return None
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return None

    tj_raw = cell("Тариф_JSON")
    try:
        tj = json.loads(tj_raw) if tj_raw else {}
    except (json.JSONDecodeError, TypeError):
        tj = {}

    gruzchiki_dopy = tj.pop("gruzchiki_dopy", [])
    gruzchiki_chasov = tj.pop("gruzchiki_chasov", None)

    def parse_kom_cell(s):
        s = (s or "").strip()
        if not s:
            return None
        try:
            return _parse_kom(s)
        except ValueError:
            return None

    return {
        "avto_baza": to_num(avto_baza_raw),
        "min_chasov": to_num(cell("Мин_часов")),
        "avto_dop_chas": to_num(cell("Авто_доп_час")),
        "gruzchiki_baza": to_num(cell("Грузчики_база")),
        "gruzchiki_dop_chas": to_num(cell("Грузчики_доп_час")),
        "gruzchiki_dopy": gruzchiki_dopy,
        "gruzchiki_chasov": gruzchiki_chasov,
        "tip_rascheta": cell("Тип_расчёта") or "почасовка",
        "kom_avto": parse_kom_cell(cell("Ком_авто")),
        "kom_gruzchiki": parse_kom_cell(cell("Ком_грузчики")),
        "platelshik": cell("Плательщик") or None,
        "forma_oplaty": cell("Форма_оплаты") or None,
        "tariff_json": tj,
        "neponyatno": [],
    }


def get_or_reparse_tariff(sheet, row_num: int, order_key: str) -> dict:
    """Возвращает тариф из памяти. Если памяти нет (рестарт бота, или
    логист открыл правку заново уже ПОСЛЕ подтверждения) - сначала
    пробует восстановить уже записанные в Sheets значения
    (read_tariff_from_sheet), сохраняя все прошлые ручные правки. Только
    если тариф вообще ни разу не подтверждался - разбирает текст через
    GPT с нуля, как при первом превью.
    """
    pending = _pending_tariffs.get(order_key)
    if pending:
        return pending

    from_sheet = read_tariff_from_sheet(sheet, row_num)
    if from_sheet is not None:
        logger.info(f"Тариф для key={order_key} восстановлен из уже записанных данных в Sheets")
        _pending_tariffs[order_key] = from_sheet
        return from_sheet

    logger.warning(f"Тариф для key={order_key} не найден в памяти (рестарт?) - пересчитываю заново")
    log_event("Тариф: не найден в памяти, пересчитан заново после рестарта", row=row_num)

    row = sheet.row_values(row_num)
    order_text = row[COL_J_TEXT - 1] if len(row) >= COL_J_TEXT else ""
    tariff = parse_tariff_via_gpt(order_text)
    tariff = apply_defaults(sheet, row, tariff)
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
        # Не стираем _order_author_line/_order_edited_fields - если логист
        # откроет правку заново уже после подтверждения, тег автора и
        # история звёздочек должны сохраниться. _pending_tariffs можно не
        # трогать - при следующем обращении get_or_reparse_tariff всё
        # равно подтянет актуальные данные из Sheets (read_tariff_from_sheet).
        log_event("Тариф подтверждён и записан", row=row_num)
        # "✏️ Исправить" остаётся доступной и после подтверждения - тариф
        # мог измениться уже после того, как заказ подтвердили (см. кейс
        # 10.08: тариф поменялся, а кнопки пропали и поправить было
        # нельзя).
        await query.edit_message_text(
            query.message.text + "\n\n✅ Подтверждено",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✏️ Исправить", callback_data=f"tariff_editmenu|{order_key}")
            ]]),
        )

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

        sheet = get_sheet()
        row_num = find_row_by_key(sheet, order_key)
        if not row_num:
            await query.edit_message_text(f"⚠️ Не нашёл строку заказа по ключу {order_key} в таблице.")
            return
        pending = get_or_reparse_tariff(sheet, row_num, order_key)

        auto_value = find_pending_neponyatno_value(pending, field_key)
        if auto_value is not None:
            # Число уже написано в предупреждении "не понял" - применяем
            # его сразу к выбранной категории, не переспрашивая логиста.
            try:
                fdef["apply"](pending, auto_value)
            except ValueError as e:
                await query.edit_message_text(query.message.text + f"\n\n⚠️ {e}")
                return
            try:
                write_tariff_to_sheet(sheet, row_num, pending)
            except Exception as e:
                logger.error(f"Не удалось записать авто-резолв поля '{field_key}', key={order_key}: {e}")
                await query.edit_message_text(f"⚠️ Не удалось записать в таблицу: {e}")
                return
            _pending_tariffs[order_key] = pending
            _order_edited_fields.setdefault(order_key, set()).add(field_key)
            log_field_correction(order_key, fdef["label"], "(не понял)", auto_value)
            log_event(f"Правка поля '{fdef['label']}' авто-резолв из 'не понял'", row=row_num)

            author_line = _order_author_line.get(order_key, "")
            edited = _order_edited_fields.get(order_key, set())
            await query.edit_message_text(
                build_tariff_preview(pending, author_line, edited),
                reply_markup=tariff_level1_keyboard(order_key),
            )
            return

        # Обычный флоу - число неизвестно, просим прислать значение.
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
        # Пишем ВЕСЬ тариф целиком, а не только тронутое поле - иначе если
        # логист правит одно поле, не дожидаясь ✅ Верно, остальные уже
        # разобранные GPT поля так и останутся пустыми в таблице.
        write_tariff_to_sheet(sheet, row_num, pending)
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
    tariff = apply_defaults(sheet, row, tariff)

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
