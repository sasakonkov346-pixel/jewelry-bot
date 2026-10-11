"""Личный ассистент-менеджер для ювелирного бизнеса. Telegram + любой OpenAI-совместимый LLM."""
import asyncio, json, logging, os, re, sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
CHANNEL_ID = os.getenv("CHANNEL_ID", "@raijuv")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
MODELS = [m.strip() for m in os.getenv("LLM_MODEL", "").split(",") if m.strip()]
TZ = os.getenv("TZ", "Europe/Kirov")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ---------------------------------------------------------------- база
db = sqlite3.connect(BASE / "data.db", check_same_thread=False)
db.executescript("""
create table if not exists tasks(id integer primary key autoincrement, title text, cron text, prompt text, enabled int default 1);
create table if not exists history(id integer primary key autoincrement, role text, content text);
create table if not exists facts(id integer primary key autoincrement, text text);
create table if not exists drafts(id integer primary key autoincrement, text text, photo text);
""")
db.commit()

# ---------------------------------------------------------------- системный промпт
STYLE = """Ты — личный ассистент владельца ювелирного бизнеса (Александр) и совмещаешь четыре роли: менеджер, маркетолог, копирайтер, аналитик. Ведёшь себя как толковый живой сотрудник, а не как чат-бот. Отвечай по-русски, по делу, без воды и без лести. Говори прямо, в том числе когда новости плохие.

Как работает менеджер: в конце рабочих ответов предлагай конкретный следующий шаг (одно действие). Задавай не больше одного уточняющего вопроса и только если без ответа нельзя продолжить, иначе делай разумное допущение и пометь его. Если видишь риск (сроки, маржа, юридический момент, слабая идея) — скажи об этом первым.

Как работает маркетолог: опирайся на раздел «Маркетинг» из контекста бизнеса. Думай воронкой: канал/Авито → личка → консультация → эскиз → предоплата → заказ. Любое предложение привязывай к цели (просмотры, заявки, чек) и к тому, как это измерить.

Правила анализа:
- Любую цифру (цену, вес, карат) приводи только если видел её в тексте открытой страницы или в выдаче поиска, и пиши, откуда именно. Если страница не открылась (маркетплейсы часто блокируют) или цены в тексте нет — прямо так и напиши, не подставляй «примерные» значения.
- Формат ответов в чате: обычный текст без markdown (без **, #, таблиц). Для списков используй тире.
- Разделяй факты (с источником и ссылкой) и свои предположения. Ничего не выдумывай: цены, сроки, данные конкурентов бери только из найденного. Если данных нет — так и скажи.
- Справка: вывод в первых строках, затем цифры, затем что делать дальше.

Правила постов для канала:
- Никакого «ИИ-языка»: без «в мире, где», «погрузитесь», «уникальный», «идеальное решение», «не просто X, а Y», без шаблонных вступлений и обилия эмодзи (максимум 1–3 уместных).
- Тон: премиальный, спокойный, живой, без перегруза. Короткие абзацы. Только обычный текст, без markdown-разметки.
- Пост про изделие: полные характеристики (металл и проба, вес, вставка: тип, размер, караты, огранка, размер изделия), примерная цена («от … ₽»), как заказать (написать в личные сообщения). Данные, которых нет, помечай «[уточнить]», не придумывай.
- Муассанит и лабораторные бриллианты называй честно, не выдавай за природные алмазы.
- Текст поста выдавай ТОЛЬКО между метками <POST> и </POST>. Вне меток — короткая реплика и блок «Визуал:».
- Если просят пост про изделие, а фото не приложено, напомни, что нужно фото.
- После поста (вне меток) дай блок «Визуал:» в 2–3 строках: что в кадре, цветовая гамма, какой короткий текст поместить на картинку. Это задание для дизайнера или для Canva.
"""
TOOLS = """
Инструменты. Чтобы вызвать, ответь только строками вызова, без пояснений до и после. Можно до 3 вызовов подряд, каждый на своей строке:
SEARCH: поисковый запрос
PAGE: https://адрес-страницы
CHANNEL: @имя_публичного_канала
После результата можно вызвать ещё инструмент. Когда данных достаточно — дай итоговый ответ без вызова инструмента.
"""

def system_prompt() -> str:
    biz = (BASE / "business.md").read_text(encoding="utf-8") if (BASE / "business.md").exists() else ""
    facts = "\n".join(f"- {r[0]}" for r in db.execute("select text from facts order by id"))
    now = datetime.now(ZoneInfo(TZ)).strftime("%d.%m.%Y %H:%M, %A")
    return f"{STYLE}\nСейчас: {now} ({TZ}).\n\n=== Контекст бизнеса ===\n{biz}\n\n=== Что владелец просил запомнить ===\n{facts or '(пока пусто)'}\n{TOOLS}"

# ---------------------------------------------------------------- LLM
async def llm(messages, temperature=0.7, max_tokens=2000) -> str:
    if not MODELS or not LLM_API_KEY:
        raise RuntimeError("Не заполнены LLM_API_KEY / LLM_MODEL в .env")
    last = None
    async with httpx.AsyncClient(timeout=150) as c:
        for model in MODELS:
            for attempt in range(2):
                try:
                    r = await c.post(f"{LLM_BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                        json={"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens})
                    if r.status_code in (429, 500, 502, 503):
                        last = f"{model}: HTTP {r.status_code}"; await asyncio.sleep(4 * (attempt + 1)); continue
                    r.raise_for_status()
                    text = (r.json()["choices"][0]["message"]["content"] or "").strip()
                    if text: return text
                    last = f"{model}: пустой ответ"
                except Exception as e:
                    last = f"{model}: {e}"; log.warning("LLM error %s", last); break
    raise RuntimeError(f"Модель недоступна ({last})")

# ---------------------------------------------------------------- инструменты
def _search_sync(q: str) -> str:
    from ddgs import DDGS
    with DDGS() as d:
        res = list(d.text(q, region="ru-ru", max_results=7))
    return "\n".join(f"- {r.get('title')}: {r.get('body')} ({r.get('href')})" for r in res) or "ничего не найдено"

async def tool_search(q: str) -> str:
    try: return await asyncio.to_thread(_search_sync, q)
    except Exception as e: return f"ошибка поиска: {e}"

async def tool_page(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header"]): t.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" "))[:4000] or "пусто"
    except Exception as e: return f"ошибка загрузки: {e}"

async def tool_channel(name: str) -> str:
    name = name.strip().lstrip("@").split("/")[-1]
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(f"https://t.me/s/{name}")
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for msg in soup.select(".tgme_widget_message")[-10:]:
            text = msg.select_one(".tgme_widget_message_text")
            views = msg.select_one(".tgme_widget_message_views")
            date = msg.select_one("time")
            if text:
                out.append(f"[{date.get('datetime', '')[:16] if date else ''}] просмотры: {views.get_text() if views else '?'}\n{text.get_text(' ', strip=True)[:700]}")
        return "\n\n".join(out) or "канал закрыт или постов не видно (работают только публичные каналы)"
    except Exception as e: return f"ошибка: {e}"

TOOL_FN = {"SEARCH": tool_search, "PAGE": tool_page, "CHANNEL": tool_channel}

def clean(t: str) -> str:
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S)
    if "</think>" in t: t = t.split("</think>")[-1]
    return t.strip()

TOOL_RE = re.compile(r"^\s*(SEARCH|PAGE|CHANNEL):\s*(\S.*?)\s*$", re.M)

async def agent(user_text: str, history=None, max_steps=6) -> str:
    msgs = [{"role": "system", "content": system_prompt()}] + (history or []) + [{"role": "user", "content": user_text}]
    for _ in range(max_steps):
        out = await llm(msgs)
        calls = TOOL_RE.findall(out.split("</think>")[0])[:3]
        if not calls:
            return clean(out)
        results = []
        for kind, arg in calls:
            if kind != "SEARCH": arg = arg.split()[0]
            log.info("tool %s %s", kind, arg)
            results.append(f"Результат {kind} «{arg}»:\n{await TOOL_FN[kind](arg)}")
        msgs += [{"role": "assistant", "content": out}, {"role": "user", "content": "\n\n".join(results)}]
    msgs.append({"role": "user", "content": "Поиск окончен. Дай итоговый ответ сейчас, без вызова инструментов."})
    out = await llm(msgs)
    return TOOL_RE.sub("", clean(out)).strip() or "Не удалось собрать данные, попробуй уточнить запрос."

# ---------------------------------------------------------------- Telegram
bot = Bot(TOKEN) if TOKEN else None
dp = Dispatcher()
public = Router()
router = Router()
router.message.filter(F.from_user.id == OWNER_ID)
router.callback_query.filter(F.from_user.id == OWNER_ID)
sched = AsyncIOScheduler(timezone=TZ)
pending = {"rewrite": None}
POST_RE = re.compile(r"<POST>(.*?)</POST>", re.S)

async def say(text: str):
    for i in range(0, len(text) or 1, 4000):
        await bot.send_message(OWNER_ID, text[i:i + 4000])

def kb(did: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"pub:{did}"),
        InlineKeyboardButton(text="✏️ Переписать", callback_data=f"rew:{did}"),
        InlineKeyboardButton(text="❌ Удалить", callback_data=f"del:{did}")]])

async def deliver(text: str, photo: str | None = None):
    m = POST_RE.search(text)
    if not m:
        await say(text); return
    post = m.group(1).strip()
    note = (text[:m.start()] + text[m.end():]).strip()
    if note: await say(note)
    did = db.execute("insert into drafts(text,photo) values(?,?)", (post, photo)).lastrowid
    db.commit()
    if photo: await bot.send_photo(OWNER_ID, photo)
    await bot.send_message(OWNER_ID, f"Черновик поста:\n\n{post}", reply_markup=kb(did))

def remember(role: str, content: str):
    db.execute("insert into history(role,content) values(?,?)", (role, content[:6000]))
    db.execute("delete from history where id not in (select id from history order by id desc limit 14)")
    db.commit()

def get_history():
    return [{"role": r, "content": c} for r, c in db.execute("select role,content from history order by id")]

async def respond(text: str, photo: str | None = None):
    await bot.send_chat_action(OWNER_ID, "typing")
    try:
        ans = await agent(text, get_history())
    except Exception as e:
        await say(f"⚠️ {e}"); return
    remember("user", text); remember("assistant", ans)
    await deliver(ans, photo)

# ---- публичная команда (чтобы узнать свой ID при настройке)
@public.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Твой Telegram ID: {m.from_user.id}")

# ---- команды владельца
@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(m: Message):
    await m.answer("Я на связи. Пиши как менеджеру: вопросы, задачи, «сделай пост», «проанализируй рынок».\n\n"
        "Расписание: напиши фразу вроде «Каждый день в 9:00 делай обзор каналов ниши» или «По пятницам в 18:00 готовь пост про кольцо с муассанитом».\n\n"
        "/tasks — список задач по расписанию\n/run N — выполнить задачу сейчас\n/deltask N — удалить задачу\n"
        "/remember текст — запомнить факт о бизнесе\n/facts — что я помню\n/forget N — забыть факт\n/reset — очистить историю диалога")

@router.message(Command("tasks"))
async def cmd_tasks(m: Message):
    rows = db.execute("select id,title,cron,enabled from tasks order by id").fetchall()
    await m.answer("\n".join(f"#{i} · {t}\n   расписание: {c}" for i, t, c, e in rows) or "Задач пока нет.")

@router.message(Command("deltask"))
async def cmd_deltask(m: Message):
    n = (m.text or "").split(maxsplit=1)[-1].strip()
    if not n.isdigit(): return await m.answer("Формат: /deltask 3")
    db.execute("delete from tasks where id=?", (int(n),)); db.commit()
    try: sched.remove_job(f"task{n}")
    except Exception: pass
    await m.answer("Удалено.")

@router.message(Command("run"))
async def cmd_run(m: Message):
    n = (m.text or "").split(maxsplit=1)[-1].strip()
    if not n.isdigit(): return await m.answer("Формат: /run 3")
    await m.answer("Запускаю…")
    await run_task(int(n))

@router.message(Command("remember"))
async def cmd_remember(m: Message):
    t = (m.text or "").partition(" ")[2].strip()
    if not t: return await m.answer("Формат: /remember Референс-канал: @имя")
    db.execute("insert into facts(text) values(?)", (t,)); db.commit()
    await m.answer("Запомнил.")

@router.message(Command("facts"))
async def cmd_facts(m: Message):
    rows = db.execute("select id,text from facts order by id").fetchall()
    await m.answer("\n".join(f"{i}. {t}" for i, t in rows) or "Пока ничего.")

@router.message(Command("forget"))
async def cmd_forget(m: Message):
    n = (m.text or "").split(maxsplit=1)[-1].strip()
    if n.isdigit(): db.execute("delete from facts where id=?", (int(n),)); db.commit(); await m.answer("Забыл.")
    else: await m.answer("Формат: /forget 2")

@router.message(Command("reset"))
async def cmd_reset(m: Message):
    db.execute("delete from history"); db.commit(); await m.answer("История диалога очищена.")

# ---- кнопки под черновиком
@router.callback_query(F.data.startswith(("pub:", "rew:", "del:")))
async def on_draft(c: CallbackQuery):
    act, did = c.data.split(":"); did = int(did)
    row = db.execute("select text,photo from drafts where id=?", (did,)).fetchone()
    if not row:
        return await c.answer("Черновик уже обработан", show_alert=True)
    text, photo = row
    if act == "pub":
        try:
            if photo and len(text) <= 1024: await bot.send_photo(CHANNEL_ID, photo, caption=text)
            elif photo:
                await bot.send_photo(CHANNEL_ID, photo); await bot.send_message(CHANNEL_ID, text)
            else: await bot.send_message(CHANNEL_ID, text)
        except Exception as e:
            return await c.message.answer(f"⚠️ Не удалось опубликовать: {e}\nПроверь, что бот админ канала {CHANNEL_ID}.")
        db.execute("delete from drafts where id=?", (did,)); db.commit()
        await c.message.edit_reply_markup(reply_markup=None)
        await c.message.answer("✅ Опубликовано.")
    elif act == "rew":
        pending["rewrite"] = did
        await c.message.answer("Что поменять? Напиши замечания одним сообщением.")
    else:
        db.execute("delete from drafts where id=?", (did,)); db.commit()
        await c.message.edit_reply_markup(reply_markup=None)
        await c.message.answer("Удалил.")
    await c.answer()

# ---- расписание
SCHEDULE_RE = re.compile(r"^\s*(каждый|каждую|каждое|каждые|ежедневно|еженедельно|по (будням|понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям|выходным)|раз в)", re.I)
PARSE_PROMPT = """Преобразуй просьбу в задачу по расписанию. Верни ТОЛЬКО JSON без пояснений:
{"title": "короткое название", "cron": "минута час день_месяца месяц день_недели", "prompt": "подробная самостоятельная инструкция для ассистента, что подготовить"}
Правила cron: пять полей, дни недели только словами (mon,tue,wed,thu,fri,sat,sun, диапазон mon-fri). Пример «каждый день в 9:00»: "0 9 * * *"; «по будням в 18:30»: "30 18 * * mon-fri".
Если результат — пост для канала, в prompt напиши «подготовь черновик поста для канала» (владелец утвердит перед публикацией). Не указывай время в prompt. Просьба: """

def schedule_job(tid: int, cron: str):
    sched.add_job(run_task, CronTrigger.from_crontab(cron, timezone=TZ), args=[tid], id=f"task{tid}", replace_existing=True, misfire_grace_time=3600)

async def create_task(text: str, m: Message):
    raw = await llm([{"role": "user", "content": PARSE_PROMPT + text}], temperature=0.1, max_tokens=600)
    try:
        d = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        CronTrigger.from_crontab(d["cron"], timezone=TZ)  # проверка
    except Exception:
        return await m.answer("Не смог разобрать расписание. Попробуй так: «Каждый день в 9:00 …».")
    tid = db.execute("insert into tasks(title,cron,prompt) values(?,?,?)", (d["title"], d["cron"], d["prompt"])).lastrowid
    db.commit(); schedule_job(tid, d["cron"])
    await m.answer(f"✅ Задача #{tid} создана\n«{d['title']}»\nРасписание (cron): {d['cron']}\nЧасовой пояс: {TZ}\n\nПроверить сразу: /run {tid}")

async def run_task(tid: int):
    row = db.execute("select title,prompt from tasks where id=?", (tid,)).fetchone()
    if not row: return await say("Задача не найдена.")
    title, prompt = row
    try:
        ans = await agent(f"Плановая задача «{title}». {prompt}\n\nСделай сам, без уточняющих вопросов. Для актуальных данных пользуйся инструментами.")
    except Exception as e:
        return await say(f"⚠️ Задача «{title}» не выполнена: {e}")
    await say(f"🗓 {title}")
    await deliver(ans)

# ---- обычные сообщения
@router.message(F.photo)
async def on_photo(m: Message):
    file_id = m.photo[-1].file_id
    text = m.caption or "Сделай пост для канала про это изделие. Характеристики и цену уточни у меня, если их нет."
    await respond(text, photo=file_id)

@router.message(F.text)
async def on_text(m: Message):
    text = m.text.strip()
    if pending["rewrite"]:
        did, pending["rewrite"] = pending["rewrite"], None
        row = db.execute("select text,photo from drafts where id=?", (did,)).fetchone()
        if row:
            db.execute("delete from drafts where id=?", (did,)); db.commit()
            await bot.send_chat_action(OWNER_ID, "typing")
            try:
                ans = await agent(f"Перепиши этот пост для канала по замечаниям владельца. Замечания: {text}\n\nТекущий текст:\n{row[0]}")
            except Exception as e:
                return await say(f"⚠️ {e}")
            return await deliver(ans, row[1])
    if SCHEDULE_RE.match(text):
        return await create_task(text, m)
    await respond(text)

def seed_tasks():
    f = BASE / "tasks_seed.json"
    if f.exists() and db.execute("select count(*) from tasks").fetchone()[0] == 0:
        for t in json.loads(f.read_text(encoding="utf-8")):
            db.execute("insert into tasks(title,cron,prompt) values(?,?,?)", (t["title"], t["cron"], t["prompt"]))
        db.commit()
        log.info("Стартовые задачи созданы из tasks_seed.json")

# ---------------------------------------------------------------- запуск
async def main():
    if not TOKEN: raise SystemExit("Заполни TELEGRAM_BOT_TOKEN в файле .env")
    dp.include_router(public); dp.include_router(router)
    seed_tasks()
    for tid, cron in db.execute("select id,cron from tasks where enabled=1").fetchall():
        try: schedule_job(tid, cron)
        except Exception as e: log.warning("task %s: %s", tid, e)
    sched.start()
    log.info("Бот запущен. OWNER_ID=%s", OWNER_ID)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
