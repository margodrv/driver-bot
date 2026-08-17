#!/usr/bin/env python3
"""
Тестовый набор регрессии для dispatch_bot.py.

ЗАЧЕМ: за две недели правок мы несколько раз чинили один кейс и тихо
ломали другой (например: фикс базы авто из строки "Тар" один раз
случайно перетянул на себя постороннее число "250/час" из конца текста).
Этот скрипт прогоняет ВСЮ цепочку детерминированных функций (в ТОЧНО
том же порядке, что в parse_tariff_via_gpt) на реальных текстах заявок
из истории обсуждения - без обращения к OpenAI API (симулируем то, что
GPT БЫ вернул, специально задавая заведомо неверный/пустой вход, чтобы
проверить, что наши защитные функции сами всё исправляют).

КАК ЗАПУСКАТЬ:
    cd dispatch-bot
    python3 test_regression.py

Перед КАЖДОЙ новой правкой dispatch_bot.py - прогонять этот файл. Если
что-то покраснело - значит новая правка тихо сломала старый кейс, чинить
ДО того, как отдавать файл.

КАК ДОБАВЛЯТЬ НОВЫЕ ТЕСТЫ: увидели новый баг на реальном заказе - после
починки добавьте новый вызов run_case(...) с текстом этого заказа и
ожидаемым результатом. Так набор растёт вместе с ботом.
"""
import os
import sys

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GOOGLE_SHEET_ID", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS_JSON", "{}")

import dispatch_bot as db

PASSED = 0
FAILED = 0
FAILURES = []


class FakeSheet:
    """Пустая таблица - используется только для apply_defaults (там читает
    колонку типа авто, которой тут нет, так что часы по типу авто в
    тестах не подставляются само собой - если тест их проверяет, они
    должны быть заданы явно в start_result)."""
    def row_values(self, n):
        return []


def run_deterministic_pipeline(start_result: dict, order_text: str) -> dict:
    """Точная копия цепочки вызовов из parse_tariff_via_gpt (см. код
    дальше по файлу dispatch_bot.py) - БЕЗ реального обращения к GPT.
    start_result - то, что "как будто бы" вернул GPT (можно намеренно
    подставлять неверные/пустые значения, чтобы проверить, что наши
    защитные функции сами всё исправляют)."""
    result = dict(start_result)
    result.setdefault("tariff_json", {})
    result.setdefault("neponyatno", [])

    db._apply_vitaliya_sanobrobka_template(result, order_text)
    db._apply_vitaliya_gruzchiki_template(result, order_text)
    db._apply_combined_avto_gruzchiki_template(result, order_text)
    db._apply_avto_gruzchiki_multiline_template(result, order_text)
    db._apply_keyword_overrides(result, order_text)
    db._recover_avto_dop_chas(result, order_text)
    db._fix_kom_duplicating_dop_chas(result, order_text)
    db._apply_shared_tariff_hours(result, order_text)
    db._reclassify_small_kom_as_percent(result, order_text)
    db._strip_bogus_gruzchiki(result, order_text)
    db._strip_bogus_kom(result, order_text)
    db._strip_client_pays_from_kom(result, order_text)
    db._recover_kom_percent_if_missing(result, order_text)
    db._strip_bogus_km(result, order_text)
    result["neponyatno"] = db._filter_neponyatno(result)
    db._strip_bogus_gidrobort(result)
    db._strip_bogus_dop_tochka_without_word(result, order_text)
    db._strip_duplicate_dop_tochka_km(result, order_text)
    db._strip_implausible_dop_tochka(result, order_text)
    db._strip_implausible_gidrobort(result, order_text)
    db._strip_implausible_etazhi_prohody(result, order_text)
    db._normalize_forma_oplaty(result, order_text)
    db._reconcile_neponyatno(result)
    db._apply_hodka_context_to_dop_tochka(result, order_text)
    db._split_gruzchiki_dopy_by_plausibility(result)
    db._recover_avto_baza_from_tar_line(result, order_text)
    db._normalize_neponyatno_candidates(result)
    db._dedupe_neponyatno_by_number(result)

    result = db.apply_defaults(FakeSheet(), [], result)
    return result


def run_case(name, order_text, start_result, must_contain=(), must_not_contain=()):
    """must_contain/must_not_contain - списки подстрок, которые должны
    (не должны) встретиться в итоговом превью."""
    global PASSED, FAILED
    result = run_deterministic_pipeline(start_result, order_text)
    preview = db.build_tariff_preview(result, "")

    problems = []
    for s in must_contain:
        if s not in preview:
            problems.append(f"ОЖИДАЛОСЬ (но нет): {s!r}")
    for s in must_not_contain:
        if s in preview:
            problems.append(f"НЕ ДОЛЖНО БЫТЬ (но есть): {s!r}")

    if problems:
        FAILED += 1
        FAILURES.append((name, preview, problems))
        print(f"✗ {name}")
    else:
        PASSED += 1
        print(f"✓ {name}")


# ============================================================================
# 1. Детерминированные шаблоны "Виталия"
# ============================================================================

run_case(
    "Виталия+санобработка: позиционный разбор 5 чисел",
    order_text="Тар БН 2ч 2100/600/200/1000/44\n...Санк книжка, Сан. оброботка...\nВиталия заказ",
    start_result={"avto_baza": 999, "avto_dop_chas": 999, "kom_avto": {"znachenie": 200, "tip": "сумма"}},
    must_contain=["Авто: 2100 (2ч)/600", "Доп.точка: +200 грн", "Санобробка: 1000 грн", "Км: 44 грн/км", "Форма оплаты: БН"],
)

run_case(
    "Виталия+грузчики: этаж/проход/вес + Ком=0% при БН",
    order_text="Два грузч\nАвто на 10:00 Тип авто 5т Гідроборт + РОКЛА\nТар БН Авто 3800/800 грузчики 1200/600/10/10/3\nВиталия заказ",
    start_result={"kom_avto": {"znachenie": 15, "tip": "%"}},
    must_contain=["Авто: 3800", "Грузчики: 1200 (2ч)/600", "Вес: 3 грн/кг", "Этажи: 10 грн", "Проходы: 10 грн", "Ком. 0%", "Форма оплаты: БН"],
)

run_case(
    "Многострочный шаблон авто+гідроборт+рокла+вантажники",
    order_text=(
        "Тар авто 5200грн/3години\n1100/Наступна година\n+гідроборт 600\n+рокла 500\n"
        "+заміський 90грн/км\nТар 2вантажника 1600/2години\n800грн/наступна\n"
        "Вагі від 160 по 8грн/кг\nПрохід 20м/поверх з вагою по 50грн на людину\nКом 15%"
    ),
    start_result={},
    must_contain=[
        "Авто: 5200 (3ч)/1100", "Гідроборт: 600", "Км: 90 грн/км", "Рокла: 500 грн",
        "Грузчики: 1600 (2ч)/800", "Вес: від 160кг по 8грн", "Допы с весом: 50 грн", "Ком. 15%",
    ],
)

# ============================================================================
# 2. Общие правила рокла/км/этаж/вес (не привязаны к конкретному шаблону)
# ============================================================================

run_case(
    "Рокла: обратный порядок слов (число ПЕРЕД словом)",
    order_text="5т + Гб + рокла\nТариф: 6750/900 + 400 рокла\nКом ?\nОплата готівкою",
    start_result={"avto_baza": 6750, "avto_dop_chas": 900, "min_chasov": 3},
    must_contain=["Авто: 6750 (3ч)/900", "Рокла: 400 грн"],
    must_not_contain=["Ком."],  # "Ком ?" явно не указана, ничего не выдумываем
)

run_case(
    "Рокла: НЕ путается с посторонним числом через перенос строки",
    order_text="Дубль\n5т + Гб + рокла\n5 палет з пилетами(67шт×15кг)\nТариф: 6750/900 + 400 рокла\nОплата готівкою",
    start_result={"avto_baza": 6750, "avto_dop_chas": 900, "min_chasov": 3,
                  "tariff_json": {"prochie_dopy": [{"nazvanie": "Рокла", "summa": 5, "group": "avto"}]}},
    must_contain=["Рокла: 400 грн"],
    must_not_contain=["Рокла: 5 грн"],
)

run_case(
    "Рокла без цены (спецификация авто) = включена в тариф",
    order_text="Авто:5Т+ГБ+Рокла\nТариф:5500/1500 точка 500грн",
    start_result={"avto_baza": 5500, "avto_dop_chas": 1500, "min_chasov": 3,
                  "tariff_json": {"prochie_dopy": [{"nazvanie": "Рокла", "summa": None, "group": "avto"}]}},
    must_not_contain=["Рокла"],
)

run_case(
    "Км: 'X грн км' без слэша",
    order_text="Тар 2ч 1500/450/50грн км\nКом 10%",
    start_result={"avto_baza": 1500, "avto_dop_chas": 450, "min_chasov": 2},
    must_contain=["Км: 50 грн/км"],
)

run_case(
    "Выдуманный км без слова 'км' в тексте - убирается",
    order_text="Тар 1300/400/22 + 400 нічні,\nКом 10%",
    start_result={"avto_baza": 1300, "avto_dop_chas": 400, "min_chasov": 2,
                  "tariff_json": {"km_stavka": 400}},
    must_not_contain=["Км:"],
    must_contain=["Нічне зберігання: 400 грн"],
)

run_case(
    "Этаж: 'N поверх' приклеено к числу",
    order_text="5т + 4 вантажники\nТар 7800/2400/25поверх (проноси, якщо буде, не платять)\nКом 15%",
    start_result={"avto_baza": 7800, "avto_dop_chas": 2400, "min_chasov": 3,
                  "tariff_json": {"etazhi_stavka": 0}, "neponyatno": ["25: км?"]},
    must_contain=["Этажи: 25 грн"],
    must_not_contain=["Этажи: 0 грн", "не понял: 25"],
)

run_case(
    "Вес: формула умножения через кг",
    order_text="44 вивіски*87 кг=3828*4 грн=15312 грн\nКом 10%",
    start_result={"avto_baza": 13600, "gruzchiki_baza": 14400, "tip_rascheta": "фикс"},
    must_contain=["Вес: 4 грн/кг"],
)

run_case(
    "Вес: голое 'вес N' без порога и без формулы",
    order_text="этажи/проходы 35 грн, вес 5",
    start_result={"avto_baza": 100, "gruzchiki_baza": 100},
    must_contain=["Вес: 5 грн/кг", "Этажи: 35 грн", "Проходы: 35 грн"],
)

run_case(
    "Допы (общее слово) = этажи И проходы вместе",
    order_text="Тариф: 2900\\1200 + 20 допи\nком 10%",
    start_result={"avto_baza": 2900, "avto_dop_chas": 1200, "min_chasov": 2},
    must_contain=["Этажи: 20 грн", "Проходы: 20 грн"],
)

# ============================================================================
# 3. Фантомные сущности (защита от выдумывания GPT)
# ============================================================================

run_case(
    "Фантомные грузчики без единого упоминания в тексте",
    order_text="12.08.2026 11:30 3т + ГБ\nТ1 загрузка на вул. Симиренко 36.\nТар БН 2ч 2100/600/200/1000/44",
    start_result={"avto_baza": 2100, "avto_dop_chas": 600, "min_chasov": 2,
                  "gruzchiki_baza": 200, "gruzchiki_dop_chas": 1000, "gruzchiki_dopy": [44]},
    must_not_contain=["Грузчики:"],
)

run_case(
    "Фантомная комиссия без слова 'ком' в тексте",
    order_text="14.08 22:30 Авто бус\nТар 1300/400/22 + 400 нічні,",
    start_result={"avto_baza": 1300, "avto_dop_chas": 400, "kom_avto": {"znachenie": 15, "tip": "%"}},
    must_not_contain=["Ком."],
)

run_case(
    "Гідроборт как спецификация авто (без цены) = включён в тариф",
    order_text="5т гідроборт + 2 вантажника, вантажники 1600/800",
    start_result={"gruzchiki_baza": 1600, "gruzchiki_dop_chas": 800,
                  "tariff_json": {"gidrobort": {"summa": 0, "po_faktu": False}}},
    must_not_contain=["Гідроборт"],
)

run_case(
    "Доп.точка без слова 'точка' в тексте → рокла (если упомянута)",
    order_text="Авто:5Т+ГБ+Рокла\nТариф:5500/5000 точка 500грн",  # тут "точка" ЕСТЬ - для контраста со следующим тестом
    start_result={"avto_baza": 5500, "avto_dop_chas": 5000,
                  "tariff_json": {"dop_tochka": {"tip": "doplata_fix", "summa": 500}}},
    must_contain=["Доп.точка: +500 грн"],
)

run_case(
    "Фантомная доп.точка БЕЗ слова точка → в Роклу",
    order_text="Авто:5Т+ГБ+Рокла\nТариф: 5200/950//500\nвантажники: 2400/1200\nКом: 800/850",
    start_result={"avto_baza": 5200, "avto_dop_chas": 950, "gruzchiki_baza": 2400, "gruzchiki_dop_chas": 1200,
                  "tariff_json": {"dop_tochka": {"tip": "doplata_fix", "summa": 500}}},
    must_contain=["Рокла: 500 грн"],
    must_not_contain=["Доп.точка"],
)

run_case(
    "Дублирование доп.точка=км без слова 'точка' в тексте",
    order_text="Тариф\nАвто 4000/700\nКм 500\nГрузчики 1800/900\nЭт 1200",
    start_result={"avto_baza": 4000, "avto_dop_chas": 700, "gruzchiki_baza": 1800, "gruzchiki_dop_chas": 900,
                  "tariff_json": {"dop_tochka": {"tip": "doplata_fix", "summa": 500}, "km_stavka": 500}},
    must_not_contain=["Доп.точка"],
    must_contain=["Км: 500 грн/км"],
)

run_case(
    "'Клиент платит X' не путается с комиссией",
    order_text="клиент платит 4900 грн\nком 10%",
    start_result={"kom_avto": {"znachenie": 4900, "tip": "сумма"}, "kom_gruzchiki": {"znachenie": 10, "tip": "%"}},
    must_contain=["Ком. 10%"],
    must_not_contain=["4900"],
)

# ============================================================================
# 4. Явный текстовый триггер важнее диапазона правдоподобия
# ============================================================================

run_case(
    "Явное 'Точка 0' - доверяем, диапазон не проверяем",
    order_text="Авто 3400 на 3ч/900 +ГБ 300+Точка 0",
    start_result={"avto_baza": 3400, "avto_dop_chas": 900, "min_chasov": 3,
                  "tariff_json": {"dop_tochka": {"tip": "doplata_fix", "summa": 0}, "gidrobort": {"summa": 300, "po_faktu": False}}},
    must_contain=["Доп.точка: +0 грн"],
)

run_case(
    "Неправдоподобная доп.точка БЕЗ явного триггера - убирается",
    order_text="Тар авто 1300/400/44",
    start_result={"avto_baza": 1300, "avto_dop_chas": 400,
                  "tariff_json": {"dop_tochka": {"tip": "doplata_fix", "summa": 44}}},
    must_not_contain=["Доп.точка"],
    must_contain=["не понял"],
)

# ============================================================================
# 5. Комиссия: маленькое число = %, разные форматы доп.часа
# ============================================================================

run_case(
    "Маленькое 'Ком N' без % → проценты, не гривны",
    order_text="Ком 10\nТариф на 6 ч\nавто 5400/600",
    start_result={"avto_baza": 5400, "avto_dop_chas": 600, "kom_avto": {"znachenie": 10, "tip": "сумма"}},
    must_contain=["Ком. 10%"],
)

run_case(
    "Большая 'Ком N' без % → остаётся суммой",
    order_text="Ком 700\nТариф 5500/1100",
    start_result={"avto_baza": 5500, "avto_dop_chas": 1100, "kom_avto": {"znachenie": 700, "tip": "сумма"}},
    must_contain=["Ком. 700 грн"],
)

run_case(
    "'Наступна: X грн' отдельной строкой (без слэша)",
    order_text="Тариф:7000- 3 години\nНаступна: 1400 грн\nКом 20%",
    start_result={"avto_baza": 7000, "avto_dop_chas": None, "min_chasov": 3, "tip_rascheta": "фикс",
                  "kom_avto": {"znachenie": 1400, "tip": "сумма"}},
    must_contain=["Авто: 7000 (3ч)/1400", "Ком. 20%"],
    must_not_contain=["фикса", "1400 грн"],
)

run_case(
    "Общий заголовок 'Тариф на Xч' → и авто, и грузчики",
    order_text="Ком 10\nТариф на 6 ч\nавто 5400/600\n2 груз 5400/900/35",
    start_result={"avto_baza": 5400, "avto_dop_chas": 600, "gruzchiki_baza": 5400, "gruzchiki_dop_chas": 900,
                  "gruzchiki_dopy": [35], "kom_avto": {"znachenie": 10, "tip": "сумма"}},
    must_contain=["Авто: 5400 (6ч)/600", "Грузчики: 5400 (6ч)/900"],
)

# ============================================================================
# 6. Форма оплаты
# ============================================================================

run_case(
    "'Б/н счет' в тексте, но GPT не заполнил forma_oplaty",
    order_text="Бус + 2 грузчика\nТариф: 2900\\1200\nб/н счет",
    start_result={"avto_baza": 2900, "avto_dop_chas": 1200, "forma_oplaty": None},
    must_contain=["Форма оплаты: БН"],
)

run_case(
    "'готівкою або на карту' унифицируется в Нал",
    order_text="Оплата готівкою або на карту",
    start_result={"forma_oplaty": "готівкою або на карту"},
    must_contain=["Форма оплаты: Нал"],
)

run_case(
    "'безнал' НЕ путается с 'нал' (подстрока)",
    order_text="Оплата безнал",
    start_result={"forma_oplaty": "безнал"},
    must_contain=["Форма оплаты: БН"],
)

# ============================================================================
# 7. Финальная сверка базы авто со строкой "Тар" (+ регрессия 20.08)
# ============================================================================

run_case(
    "База авто спутана со спецификацией длины кузова",
    order_text="Авто від 4,2\nТариф 1500/500/150пром точка",
    start_result={"avto_baza": 4200, "avto_dop_chas": 500},
    must_contain=["Авто: 1500"],
    must_not_contain=["Авто: 4200"],
)

run_case(
    "РЕГРЕССИЯ 20.08: постороннее 'Тариф N/час' в конце текста НЕ перетягивает базу",
    order_text=(
        "10т+4 грузчика\nТариф\nАвто 8150/1250\nДоп ходка 4350\n1 грузчик 900/450\n"
        "этажи/проходы 35 грн, вес 5\nСостав:\n1.\n2.\n3.\n4.\n\nТариф 250/час + допы 10"
    ),
    start_result={"avto_baza": 250, "avto_dop_chas": 1250, "gruzchiki_baza": 900, "gruzchiki_dop_chas": 450},
    must_contain=["Авто: 8150"],
    must_not_contain=["Авто: 250"],
)

run_case(
    "Многострочный шаблон не задет финальной сверкой (своя логика уже верна)",
    order_text="Тар авто 5200грн/3години\n1100/Наступна година\nТар 2вантажника 1600/2години\n800грн/наступна",
    start_result={},
    must_contain=["Авто: 5200"],
)

run_case(
    "Пассажирское место: с ценой в скобках (авторитетнее базовой)",
    order_text="Авто: бус+ 2 пасажира\nТариф:4000/500+ 1 пас.місце 300грн ( 600 за двох)\nКом:20%",
    start_result={"avto_baza": 4000, "avto_dop_chas": 500, "min_chasov": 2,
                  "kom_avto": {"znachenie": 20, "tip": "%"}},
    must_contain=["Пасажирське місце: 600 грн"],
    must_not_contain=["300 грн"],
)

run_case(
    "Пассажирское место: без цены (99% случай) - бесплатно, не выдумываем",
    order_text="Авто: бус + 1 пасажир\nТариф 3000/500",
    start_result={"avto_baza": 3000, "avto_dop_chas": 500},
    must_not_contain=["Пасажирське місце"],
)

run_case(
    "Комиссия полностью пропала, хотя явно указана 'Ком X%'",
    order_text="Тар авто 5000/1500/700\nТар вантажник 600/300\nКом 10%\nОплата БН",
    start_result={"avto_baza": 5000, "avto_dop_chas": 1500, "gruzchiki_baza": 600, "gruzchiki_dop_chas": 300,
                  "kom_avto": None, "kom_gruzchiki": None, "neponyatno": ["10: км?"]},
    must_contain=["Ком. 10%"],
    must_not_contain=["не понял: 10"],
)

run_case(
    "Число >15 среди лишних чисел грузчиков → этажи+проходы автоматически (без переспроса)",
    order_text="5т гідроборт + 2 вантажника\nТариф авто 5300/900\nвантажники 1600/800/4/20\nКом 800",
    start_result={"avto_baza": 5300, "avto_dop_chas": 900, "min_chasov": 3,
                  "gruzchiki_baza": 1600, "gruzchiki_dop_chas": 800, "gruzchiki_dopy": [4, 20],
                  "kom_avto": {"znachenie": 800, "tip": "сумма"}},
    must_contain=["Вес: 4 грн/кг", "Этажи: 20 грн", "Проходы: 20 грн"],
    must_not_contain=["не понял"],
)

run_case(
    "Маркеры Т3/Т4 в маршруте → неясное число становится доп.точкой",
    order_text="18.08 12:00 + - 3т ГБ\nТ1 Сырецкая 31\nТ2 Гвардейская\nТ3 Сырецкая 31\nТ4 Шелуденко\nТар 2600/700/500",
    start_result={"avto_baza": 2600, "avto_dop_chas": 700, "min_chasov": 2,
                  "neponyatno": ["500: ГБ?/точка?/км?"]},
    must_contain=["Доп.точка: +500 грн"],
    must_not_contain=["не понял"],
)

run_case(
    "Явная 'ходка 1000 грн' → доп.ходка, соседнее число НЕ трогаем",
    order_text="Можливо ходка 1000 грн\nТар 4500/1000/500",
    start_result={"avto_baza": 4500, "avto_dop_chas": 1000, "neponyatno": ["500: ГБ?/точка?/км?"]},
    must_contain=["Доп.ходка: 1000 грн", "не понял: 500"],
    must_not_contain=["Доп.точка"],
)

run_case(
    "'Буде ходка' БЕЗ цены → контекстный сигнал доп.точки (как раньше)",
    order_text="Буде ходка можливо\nТар 4500/1000/500",
    start_result={"avto_baza": 4500, "avto_dop_chas": 1000, "neponyatno": ["500: ГБ?/точка?/км?"]},
    must_contain=["Доп.точка: +500 грн"],
    must_not_contain=["Доп.ходка", "не понял"],
)

# ============================================================================
# Итог
# ============================================================================

def main():
    print()
    print(f"Пройдено: {PASSED} / {PASSED + FAILED}")

    if FAILURES:
        print()
        print("=" * 70)
        print("ПРОВАЛИВШИЕСЯ ТЕСТЫ:")
        print("=" * 70)
        for name, preview, problems in FAILURES:
            print(f"\n--- {name} ---")
            for p in problems:
                print(f"  {p}")
            print(f"  Превью целиком:\n{preview}")
        sys.exit(1)
    else:
        print("Все тесты прошли ✓")
        sys.exit(0)


if __name__ == "__main__":
    main()
