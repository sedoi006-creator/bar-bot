# -*- coding: utf-8 -*-
"""Telegram-бот для барменов: поиск технологических карт по названию коктейля.

Запуск:  python bot.py
Токен берётся из файла token.txt (рядом с этим скриптом) или из переменной BOT_TOKEN.
"""
import asyncio
import json
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      InlineQueryResultArticle, InputTextMessageContent, Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, InlineQueryHandler,
                          MessageHandler, TypeHandler, filters)

BASE = Path(__file__).parent
RECIPES_FILE = BASE / "recipes.json"
TOKEN_FILE = BASE / "token.txt"

# ------------------------------------------------- калькулятор суперджуса
# Формула (едина для всех цитрусов): литр готового суперджуса = 18 × вес цедры.
# лимонная кислота = 0,9 × вес цедры; аскорбиновая = 0,1 ×; вода = 16 ×.
SJ_RATIOS = {"лимонная кислота": 0.9, "аскорбиновая кислота": 0.1, "вода": 16.0}
SJ_CITRUSES = ["Лайм", "Лимон", "Апельсин", "Грейпфрут"]

# ---------------------------------------------------------------- загрузка данных

def load_token() -> str:
    if os.environ.get("BOT_TOKEN"):
        return os.environ["BOT_TOKEN"].strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    raise SystemExit("Не найден токен! Положите его в файл token.txt или задайте переменную BOT_TOKEN.")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.replace("ё", "е")
    return re.sub(r"\s+", " ", s).strip()


RECIPES = json.loads(RECIPES_FILE.read_text(encoding="utf-8"))
for i, r in enumerate(RECIPES):
    r["id"] = i
    r["_n"] = norm(r["name"])

CATEGORIES = []
for r in RECIPES:
    if r["category"] not in CATEGORIES:
        CATEGORIES.append(r["category"])

# ---------------------------------------------------------------- поиск

def search(query: str, limit: int = 8):
    """Точный префикс -> подстрока -> нечёткое совпадение."""
    q = norm(query)
    if not q:
        return []
    exact = [r for r in RECIPES if r["_n"] == q]
    starts = [r for r in RECIPES if r["_n"].startswith(q) and r not in exact]
    contains = [r for r in RECIPES if q in r["_n"] and r not in exact and r not in starts]
    fuzzy = []
    if len(q) >= 3 and len(exact) + len(starts) + len(contains) < 3:
        scored = []
        for r in RECIPES:
            if r in exact or r in starts or r in contains:
                continue
            ratio = SequenceMatcher(None, q, r["_n"]).ratio()
            if ratio >= 0.6:
                scored.append((ratio, r))
        scored.sort(key=lambda t: -t[0])
        fuzzy = [r for _, r in scored[:limit]]
    return (exact + starts + contains + fuzzy)[:limit]


def recipe_buttons(ids):
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        r["name"], callback_data=f"r{r['id']}")] for r in ids])


# ---------------------------------------------------------------- карточка рецепта

def format_recipe(r: dict) -> str:
    lines = [f"<b>{r['name'].upper()}</b>", f"<i>{r['category']}</i>", ""]
    if r["components"]:
        lines.append("<b>Рецептура:</b>")
        for c in r["components"]:
            amt = f" — {c['amount']}" if c["amount"] else ""
            lines.append(f"• {c['name']}{amt}")
        lines.append("")
    if r["method"]:
        lines.append(f"<b>Метод приготовления:</b> {r['method']}")
    if r["ice"]:
        lines.append(f"<b>Лёд:</b> {r['ice']}")
    if r["glass"]:
        lines.append(f"<b>Бокал:</b> {r['glass']}")
    if r["garnish"]:
        lines.append(f"<b>Украшение:</b> {r['garnish']}")
    if r["serving"]:
        lines.append(f"<b>Подача:</b> {r['serving']}")
    if r["output"]:
        lines.append(f"<b>Выход:</b> {r['output']}")
    if r["comment"]:
        lines.append(f"<b>Примечание:</b> {r['comment']}")
    if r["tech"]:
        tech = r["tech"]
        if len(tech) > 900:
            tech = tech[:900] + "…"
        lines.append("")
        lines.append(f"<b>Технология приготовления:</b>\n{tech}")
    return "\n".join(lines)


def category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(cat, callback_data=f"c{i}")]
        for i, cat in enumerate(CATEGORIES)
    ])


# ---------------------------------------------------------------- калькулятор суперджуса

SJ_CITRUS, SJ_PEEL = range(2)

def parse_peel(text: str):
    """«50 г», «0,5 кг», «500» -> масса цедры в граммах."""
    m = re.compile(r"(\d+(?:[.,]\d+)?)\s*(г|грам\w*|гр|кг|kg)?", re.IGNORECASE).search(text.replace("\xa0", " "))
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "г").lower()
    if unit.startswith("к"):
        value *= 1000
    if 0 < value <= 20000:
        return value
    return None


def calc_by_peel(citrus: str, peel_g: float) -> str:
    citric = peel_g * SJ_RATIOS["лимонная кислота"]
    ascorbic = peel_g * SJ_RATIOS["аскорбиновая кислота"]
    water = peel_g * SJ_RATIOS["вода"]
    volume_ml = peel_g * (1 + SJ_RATIOS["лимонная кислота"] + SJ_RATIOS["аскорбиновая кислота"] + SJ_RATIOS["вода"])
    vol_txt = f"{volume_ml / 1000:g} л" if volume_ml >= 1000 else f"{volume_ml:g} мл"
    lines = [
        f"🧮 <b>Суперджус {citrus.lower()} — от {peel_g:g} г цедры</b>",
        "",
        f"• Цедра {citrus.lower()}а — <b>{peel_g:g} г</b>",
        f"• Лимонная кислота — <b>{citric:.1f} г</b>",
        f"• Аскорбиновая кислота — <b>{ascorbic:.1f} г</b>",
        f"• Вода — <b>{water:.0f} мл</b>",
        "",
        f"<b>Выход готового суперджуса:</b> {vol_txt}",
        "<i>Цедра × 18. Кислота 0,9×, аскорбинка 0,1×, вода 16×.</i>",
    ]
    return "\n".join(lines)


async def sj_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(c, callback_data=f"sjc:{c}") for c in SJ_CITRUSES[:2]
    ], [
        InlineKeyboardButton(c, callback_data=f"sjc:{c}") for c in SJ_CITRUSES[2:]
    ], [
        InlineKeyboardButton("❌ Отмена", callback_data="sjcancel")
    ]])
    msg = update.effective_message
    await msg.reply_text(
        "🧮 <b>Калькулятор суперджуса</b>\n\nКакой цитрус готовим?",
        parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return SJ_CITRUS


async def sj_citrus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["sj_citrus"] = query.data.split(":", 1)[1]
    await query.edit_message_text(
        f"🍋 Цитрус: <b>{context.user_data['sj_citrus']}</b>\n\n"
        "Сколько цедры у вас есть? Напишите в граммах (или кг), например:\n"
        "<code>55 г</code>, <code>0,25 кг</code> или просто <code>55</code> (г)",
        parse_mode=ParseMode.HTML)
    return SJ_PEEL


async def sj_peel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    peel = parse_peel(update.message.text)
    if peel is None:
        await update.message.reply_text(
            "😕 Не понял вес цедры. Напишите в граммах или кг — "
            "например: 55 г, 0,25 кг, 250")
        return SJ_PEEL
    citrus = context.user_data.get("sj_citrus", "Лайм")
    text = calc_by_peel(citrus, peel)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔁 Другая цедра", callback_data="sjagain2"),
            InlineKeyboardButton("❌ Готово", callback_data="sjcancel"),
        ]]))
    return SJ_PEEL
    volume = parse_volume(update.message.text)
    if volume is None:
        await update.message.reply_text(
            "😕 Не понял объём. Напишите числом в мл или литрах — "
            "например: 500 мл, 2 л, 0,75")
        return SJ_VOLUME
    citrus = context.user_data.get("sj_citrus", "Лайм")
    text = calc_superjuice(citrus, volume)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔁 Другой объём", callback_data="sjagain"),
            InlineKeyboardButton("❌ Готово", callback_data="sjcancel"),
        ]]))
    return SJ_VOLUME


async def sj_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Сколько цедры есть? Например: 250 г, 1 кг")
    return SJ_PEEL


async def sj_again2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Сколько цедры есть? Например: 250 г, 1 кг")
    return SJ_PEEL


async def sj_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Калькулятор закрыт. Вернуться: /calc")
    return ConversationHandler.END


# ---------------------------------------------------------------- хендлеры

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🍸 <b>Бар-бот: технологические карты</b>\n\n"
        "Напишите название коктейля — покажу рецептуру, метод, "
        "бокал и украшение.\n\n"
        "Или выберите категорию кнопкой ниже.\n"
        "Также работает <b>инлайн-режим</b>: наберите в чате "
        "<code>@ваш_бот_имя название</code>."
    )
    kb_main = category_keyboard()
    rows = [[InlineKeyboardButton("🧮 Калькулятор суперджуса", callback_data="sjopen")]]
    rows += [list(r) for r in kb_main.inline_keyboard]
    kb = InlineKeyboardMarkup(rows)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data[1:])
    cat = CATEGORIES[idx]
    items = [r for r in RECIPES if r["category"] == cat]
    # категория «Полуфабрикаты» большая — делим по буквам
    if len(items) > 25:
        buttons = []
        row = []
        for r in sorted(items, key=lambda x: x["_n"]):
            row.append(InlineKeyboardButton(r["name"], callback_data=f"r{r['id']}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
        await query.edit_message_text(
            f"<b>{cat}</b> — {len(items)} позиций:",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return
    names = "\n".join(f"• {r['name']}" for r in sorted(items, key=lambda x: x["_n"]))
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"r{r['id']}")]
               for r in sorted(items, key=lambda x: x["_n"])]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
    await query.edit_message_text(
        f"<b>{cat}</b> ({len(items)}):\n{names}",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def on_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    r = RECIPES[int(query.data[1:])]
    back_cat = CATEGORIES.index(r["category"])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⬅️ {r['category']}", callback_data=f"c{back_cat}"),
        InlineKeyboardButton("🔁 К категориям", callback_data="back"),
    ]])
    await query.edit_message_text(
        format_recipe(r), parse_mode=ParseMode.HTML,
        reply_markup=kb, disable_web_page_preview=True)


async def sj_open_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await sj_start(update, context)


async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb_main = category_keyboard()
    rows = [[InlineKeyboardButton("🧮 Калькулятор суперджуса", callback_data="sjopen")]]
    rows += [list(r) for r in kb_main.inline_keyboard]
    kb = InlineKeyboardMarkup(rows)
    await query.edit_message_text(
        "🍸 <b>Выберите категорию:</b>",
        parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    results = search(update.message.text)
    if not results:
        kb_main = category_keyboard()
        rows = [[InlineKeyboardButton("🧮 Калькулятор суперджуса", callback_data="sjopen")]]
        rows += [list(r) for r in kb_main.inline_keyboard]
        kb = InlineKeyboardMarkup(rows)
        await update.message.reply_text(
            "😕 Ничего не нашлось. Проверьте название или выберите категорию:",
            reply_markup=kb)
        return
    if len(results) == 1:
        r = results[0]
        back_cat = CATEGORIES.index(r["category"])
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"⬅️ {r['category']}", callback_data=f"c{back_cat}")]])
        await update.message.reply_text(
            format_recipe(r), parse_mode=ParseMode.HTML,
            reply_markup=kb, disable_web_page_preview=True)
        return
    header = f"Найдено {len(results)} — выберите:"
    body = "\n".join(f"• {r['name']} <i>({r['category']})</i>" for r in results)
    await update.message.reply_text(
        f"{header}\n{body}", parse_mode=ParseMode.HTML,
        reply_markup=recipe_buttons(results))


async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query
    results = search(q.query or "", limit=6)
    items = []
    for r in results:
        # сокращённая карточка для инлайн-выдачи
        comp = "; ".join(
            f"{c['name']} {c['amount']}".strip() for c in r["components"][:6])
        desc = (comp[:180] + "…") if len(comp) > 180 else comp
        items.append(InlineQueryResultArticle(
            id=str(r["id"]), title=r["name"],
            description=f"{r['category']} — {desc}" if desc else r["category"],
            input_message_content=InputTextMessageContent(
                format_recipe(r), parse_mode=ParseMode.HTML,
                disable_web_page_preview=True)))
    await q.answer(items, cache_time=30)


# импорты для инлайна (вынесены сюда, чтобы не засорять шапку)


def main():
    token = load_token()
    # Автоперезапуск: если polling падает (сбой сети, Conflict, таймаут) —
    # поднимаем заново. Важно для работы 24/7 в облаке.
    attempt = 0
    while True:
        attempt += 1
        try:
            print(f"[запуск #{attempt}] инициализация бота...", flush=True)
            run_once(token)
            print("[остановлен штатно]", flush=True)
            return
        except Exception as e:
            print(f"[сбой #{attempt}] {type(e).__name__}: {e}", flush=True)
            wait = min(60, 5 * attempt)
            print(f"[повтор через {wait} сек]", flush=True)
            time.sleep(wait)


def run_once(token: str):
    # Application создаём внутри _serve (внутри одного event loop),
    # иначе polling стартует, но обработчики не срабатывают.
    asyncio.run(_serve(token))


async def _serve(token: str):
    app = Application.builder().token(token).build()
    _register_handlers(app)
    print(f"Бот запущен. Рецептов в базе: {len(RECIPES)}, категорий: {len(CATEGORIES)}", flush=True)

    print("[1/4] initialize...", flush=True)
    await app.initialize()
    me = await app.bot.get_me()
    print(f"[2/4] getMe OK: @{me.username}", flush=True)
    await app.start()
    print("[3/4] app.start OK", flush=True)
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    print("[4/4] polling активен", flush=True)
    # держим процесс живым, пока работает polling
    while app.updater.running:
        await asyncio.sleep(5)
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


def _register_handlers(app):
    # диагностика: логируем каждый входящий апдейт (видно, доходят ли сообщения)
    app.add_handler(TypeHandler(Update, _log_update), group=-1)
    # диалог калькулятора регистрируем ДО общего поиска,
    # иначе «76 г» съест обработчик поиска рецептов
    sj_conv = ConversationHandler(
        entry_points=[CommandHandler("calc", sj_start),
                      CallbackQueryHandler(sj_open_cb, pattern="^sjopen$")],
        states={
            SJ_CITRUS: [CallbackQueryHandler(sj_citrus, pattern="^sjc:")],
            SJ_PEEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sj_peel),
                CallbackQueryHandler(sj_again2, pattern="^sjagain2$"),
                CallbackQueryHandler(sj_cancel, pattern="^sjcancel$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(sj_cancel, pattern="^sjcancel$")],
        per_user=True,
        per_message=False,
    )
    app.add_handler(sj_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CallbackQueryHandler(on_category, pattern=r"^c\d+$"))
    app.add_handler(CallbackQueryHandler(on_recipe, pattern=r"^r\d+$"))
    app.add_handler(CallbackQueryHandler(on_back, pattern=r"^back$"))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))


async def _log_update(update, context):
    try:
        kind = update.effective_message.__class__.__name__ if update.effective_message else "?"
        text = (update.effective_message.text or "")[:40] if update.effective_message else ""
        print(f"[апдейт] {kind} от {update.effective_user.id if update.effective_user else '?'}: {text}", flush=True)
    except Exception as e:
        print(f"[апдейт] ошибка логирования: {e}", flush=True)


if __name__ == "__main__":
    main()
