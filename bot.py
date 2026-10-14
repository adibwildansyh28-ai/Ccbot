import asyncio
import logging
import os
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------- config ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env var is required")

API_URL = "https://api.chkr.cc/"
HTTP_TIMEOUT = 20.0
MAX_CONCURRENT = 5
MAX_BATCH = 50
MAX_GEN = 50

DB_PATH = os.environ.get("DB_PATH", "/data/bin.db")
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

ALLOWED_IDS = {
    int(x) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("chkr")

# ---------------- regex ----------------
CARD_RE = re.compile(
    r"^\s*(\d{12,19})\s*[|:;,\s]\s*(\d{1,2})\s*[|:;,\s]\s*(\d{2,4})\s*[|:;,\s]\s*(\d{3,4})\s*$"
)
BIN_RE = re.compile(r"^\d{6,8}$")

# ---------------- storage ----------------
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.execute(
    """CREATE TABLE IF NOT EXISTS bins (
        bin TEXT PRIMARY KEY,
        bank TEXT, brand TEXT, category TEXT, country TEXT,
        updated_at TEXT
    )"""
)
DB.commit()


def bin_save(bin_: str, info: dict) -> None:
    DB.execute(
        "INSERT OR REPLACE INTO bins (bin,bank,brand,category,country,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            bin_,
            info.get("bank", "") or "",
            info.get("brand", "") or "",
            info.get("category", "") or "",
            info.get("country", "") or "",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    DB.commit()


def bin_get(bin_: str):
    row = DB.execute(
        "SELECT bank,brand,category,country FROM bins WHERE bin=?", (bin_,)
    ).fetchone()
    if not row:
        return None
    return {"bank": row[0], "brand": row[1], "category": row[2], "country": row[3]}


# ---------------- helpers ----------------
def luhn_complete(prefix: str, length: int = 16) -> str:
    """Fill prefix with random digits, append luhn check digit."""
    prefix = prefix[: length - 1]
    body = prefix + "".join(
        random.choices("0123456789", k=length - len(prefix) - 1)
    )
    s = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    check = (10 - (s % 10)) % 10
    return body + str(check)


def rand_exp() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    y = now.year + random.randint(1, 4)
    m = random.randint(1, 12)
    return f"{m:02d}", f"{str(y)[2:]}"


def rand_cvv(brand: str) -> str:
    n = 4 if brand.lower() in ("amex", "american express") else 3
    return "".join(random.choices("0123456789", k=n))


def bin_from_pan(pan: str) -> str:
    return pan[:6]


def parse_card_line(line: str):
    m = CARD_RE.match(line)
    if not m:
        return None
    cc, mm, yy, cvv = m.groups()
    mm = mm.zfill(2)
    if len(yy) == 2:
        yy = "20" + yy
    return f"{cc}|{mm}|{yy}|{cvv}"


# ---------------- api ----------------
@dataclass
class Result:
    raw: str
    code: int | None
    status: str
    message: str
    bank: str = ""
    brand: str = ""
    category: str = ""
    country: str = ""
    error: str = ""


async def api_check(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, cc_str: str
) -> Result:
    async with sem:
        try:
            r = await client.post(API_URL, json={"data": cc_str})
            r.raise_for_status()
            j = r.json()
        except httpx.HTTPStatusError as e:
            return Result(cc_str, None, "HTTP", "", error=f"http {e.response.status_code}")
        except Exception as e:
            return Result(cc_str, None, "ERR", "", error=str(e)[:120])

        card = j.get("card") or {}
        country = (card.get("country") or {}).get("name", "") or ""
        res = Result(
            raw=cc_str,
            code=j.get("code"),
            status=j.get("status", "?"),
            message=j.get("message", "") or "",
            bank=card.get("bank", "") or "",
            brand=card.get("type", "") or "",
            category=card.get("category", "") or "",
            country=country,
        )
        if res.brand or res.bank:
            bin_save(
                bin_from_pan(cc_str.split("|")[0]),
                {
                    "bank": res.bank,
                    "brand": res.brand,
                    "category": res.category,
                    "country": res.country,
                },
            )
        return res


# ---------------- ui ----------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Check Cards", callback_data="menu_check")],
            [
                InlineKeyboardButton("🔍 Search BIN", callback_data="menu_lookup"),
                InlineKeyboardButton("⚙️ Generate Cards", callback_data="menu_gen"),
            ],
            [InlineKeyboardButton("📊 Stats", callback_data="menu_stats")],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="menu_home")]])


def fmt_result(r: Result) -> str:
    tag = {1: "🟢 LIVE", 0: "🔴 DIE", 2: "🟡 UNK"}.get(r.code, "⚪ ERR")
    if r.error:
        return f"{tag}  `{r.raw}`  _({r.error})_"
    meta = "  ·  ".join(x for x in [r.brand, r.category, r.bank, r.country] if x)
    line = f"{tag}  `{r.raw}`"
    if meta:
        line += f"\n        └ {meta}"
    return line


def allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    u = update.effective_user
    return bool(u and u.id in ALLOWED_IDS)


# ---------------- commands ----------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    ctx.user_data.clear()
    await update.message.reply_text(
        "chkr.cc — *card intelligence bot*\n\n"
        "Pick a mode below.\n\n"
        "• *Check* — paste `CC|MM|YY|CVV` lines\n"
        "• *Search BIN* — issuer lookup from a 6–8 digit BIN\n"
        "• *Generate* — luhn-valid cards from a BIN",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    s = ctx.application.bot_data.setdefault(
        "stats", {"live": 0, "die": 0, "unk": 0, "err": 0}
    )
    await update.message.reply_text(
        f"*session stats*\n\n"
        f"🟢 live  `{s['live']}`\n"
        f"🔴 die   `{s['die']}`\n"
        f"🟡 unk   `{s['unk']}`\n"
        f"⚪ err   `{s['err']}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


# ---------------- callbacks ----------------
async def cb_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if not allowed(update):
        return

    if data == "menu_home":
        ctx.user_data.pop("await_qty", None)
        try:
            await q.edit_message_text("main menu:", reply_markup=main_menu())
        except Exception:
            await q.message.reply_text("main menu:", reply_markup=main_menu())
        return

    if data == "menu_stats":
        s = ctx.application.bot_data.setdefault(
            "stats", {"live": 0, "die": 0, "unk": 0, "err": 0}
        )
        await q.edit_message_text(
            f"*session stats*\n\n"
            f"🟢 live  `{s['live']}`\n"
            f"🔴 die   `{s['die']}`\n"
            f"🟡 unk   `{s['unk']}`\n"
            f"⚪ err   `{s['err']}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return

    if data == "menu_check":
        await q.edit_message_text(
            "send cards, one per line:\n`CC|MM|YY|CVV`\n\nmax 50 / message",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return

    if data == "menu_lookup":
        await q.edit_message_text(
            "send a BIN (6–8 digits). example: `556338`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return

    if data == "menu_gen":
        ctx.user_data["await_qty"] = False
        await q.edit_message_text(
            "send a BIN (6–8 digits) to generate from.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return

    if data == "bin_act_lookup":
        bin_ = ctx.user_data.get("pending_bin")
        if not bin_:
            await q.edit_message_text("lost the BIN, try again.", reply_markup=back_kb())
            return
        await q.edit_message_text("🔎 looking up…")
        info = await do_lookup(ctx, bin_)
        await q.edit_message_text(info, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        return

    if data == "bin_act_gen":
        bin_ = ctx.user_data.get("pending_bin")
        if not bin_:
            await q.edit_message_text("lost the BIN, try again.", reply_markup=back_kb())
            return
        ctx.user_data["gen_bin"] = bin_
        ctx.user_data["await_qty"] = True
        await q.edit_message_text(
            f"BIN `{bin_}`\n\nhow many cards? (1–{MAX_GEN})",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return


# ---------------- text handler ----------------
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    # pending quantity for generator?
    if ctx.user_data.get("await_qty"):
        await handle_qty(update, ctx, text)
        return

    lines = [ln for ln in text.splitlines() if ln.strip()]
    parsed, bad = [], []
    for ln in lines:
        p = parse_card_line(ln)
        if p:
            parsed.append(p)
        else:
            bad.append(ln.strip())

    # bare BIN → action prompt
    if not parsed and len(lines) == 1 and BIN_RE.match(lines[0]):
        await prompt_bin_action(update, ctx, lines[0])
        return

    if not parsed:
        await update.message.reply_text(
            "no valid input.\n\n"
            "• cards: `CC|MM|YY|CVV`\n"
            "• BIN: 6–8 digits",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu(),
        )
        return

    if len(parsed) > MAX_BATCH:
        await update.message.reply_text(f"capping to {MAX_BATCH}.")
        parsed = parsed[:MAX_BATCH]

    notice = await update.message.reply_text(f"⏳ checking {len(parsed)}…")

    client: httpx.AsyncClient = ctx.application.bot_data["client"]
    sem: asyncio.Semaphore = ctx.application.bot_data["sem"]

    results = await asyncio.gather(*[api_check(client, sem, c) for c in parsed])

    s = ctx.application.bot_data.setdefault(
        "stats", {"live": 0, "die": 0, "unk": 0, "err": 0}
    )
    live, die, unk, err = [], [], [], []
    for r in results:
        if r.error or r.code is None:
            err.append(r)
            s["err"] += 1
        elif r.code == 1:
            live.append(r)
            s["live"] += 1
        elif r.code == 0:
            die.append(r)
            s["die"] += 1
        else:
            unk.append(r)
            s["unk"] += 1

    head = (
        f"*check complete*  ·  {len(results)} cards\n\n"
        f"🟢 `{len(live)}`   🔴 `{len(die)}`   🟡 `{len(unk)}`   ⚪ `{len(err)}`"
    )
    if bad:
        head += f"\n_skipped {len(bad)} malformed_"

    try:
        await notice.edit_text(head, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text(head, parse_mode=ParseMode.MARKDOWN)

    for group, label in (
        (live, "🟢 LIVE"),
        (unk, "🟡 UNKNOWN"),
        (die, "🔴 DIE"),
        (err, "⚪ ERRORS"),
    ):
        if not group:
            continue
        for i in range(0, len(group), 12):
            body = f"*{label}*\n" + "\n".join(fmt_result(r) for r in group[i : i + 12])
            if len(body) > 3800:
                body = body[:3800] + "\n…"
            await update.message.reply_text(body, parse_mode=ParseMode.MARKDOWN)


async def prompt_bin_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE, bin_: str):
    ctx.user_data["pending_bin"] = bin_
    info = bin_get(bin_)
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Search BIN", callback_data="bin_act_lookup")],
            [InlineKeyboardButton("⚙️ Generate from this BIN", callback_data="bin_act_gen")],
            [InlineKeyboardButton("◀ Cancel", callback_data="menu_home")],
        ]
    )
    if info and info.get("bank"):
        cap = (
            f"*BIN `{bin_}`*\n"
            f"🏦 `{info['bank'] or '—'}`\n"
            f"💠 `{info['brand'] or '—'}` · `{info['category'] or '—'}`\n"
            f"🌍 `{info['country'] or '—'}`\n\n"
            "_cached — what next?_"
        )
    else:
        cap = f"BIN `{bin_}` — no cache. what do you want to do?"
    await update.message.reply_text(cap, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def handle_qty(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        n = int(text.strip())
    except Exception:
        await update.message.reply_text(f"send a number 1–{MAX_GEN}.")
        return
    if not 1 <= n <= MAX_GEN:
        await update.message.reply_text(f"1–{MAX_GEN} only.")
        return

    bin_ = ctx.user_data.get("gen_bin", "")
    ctx.user_data["await_qty"] = False
    if not bin_:
        await update.message.reply_text("lost the BIN, send /start again.")
        return

    info = bin_get(bin_) or {}
    brand = info.get("brand", "")

    cards = []
    for _ in range(n):
        pan = luhn_complete(bin_, 16)
        mm, yy = rand_exp()
        cvv = rand_cvv(brand)
        cards.append(f"{pan}|{mm}|{yy}|{cvv}")

    header = f"⚙️ *generated {n}*  ·  BIN `{bin_}`"
    body = header + "\n\n" + "\n".join(f"`{c}`" for c in cards)
    if len(body) > 3800:
        await update.message.reply_text(header, parse_mode=ParseMode.MARKDOWN)
        for i in range(0, len(cards), 25):
            chunk = "\n".join(f"`{c}`" for c in cards[i : i + 25])
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(body, parse_mode=ParseMode.MARKDOWN)


async def do_lookup(ctx: ContextTypes.DEFAULT_TYPE, bin_: str) -> str:
    cached = bin_get(bin_)
    if cached and cached.get("bank"):
        return (
            f"🔍 *BIN `{bin_}`* _(cache)_\n\n"
            f"🏦 `{cached['bank'] or '—'}`\n"
            f"💠 `{cached['brand'] or '—'}` · `{cached['category'] or '—'}`\n"
            f"🌍 `{cached['country'] or '—'}`"
        )

    # probe the api with a luhn-valid filler card
    probe = luhn_complete(bin_, 16)
    mm, yy = rand_exp()
    fake = f"{probe}|{mm}|{yy}|000"
    client: httpx.AsyncClient = ctx.application.bot_data["client"]
    sem: asyncio.Semaphore = ctx.application.bot_data["sem"]
    r = await api_check(client, sem, fake)
    if r.error or not (r.bank or r.brand):
        return f"🔍 *BIN `{bin_}`*\n\n_no data returned._"
    bin_save(
        bin_,
        {
            "bank": r.bank,
            "brand": r.brand,
            "category": r.category,
            "country": r.country,
        },
    )
    return (
        f"🔍 *BIN `{bin_}`*\n\n"
        f"🏦 `{r.bank or '—'}`\n"
        f"💠 `{r.brand or '—'}` · `{r.category or '—'}`\n"
        f"🌍 `{r.country or '—'}`"
    )


# ---------------- lifecycle ----------------
async def on_startup(app: Application):
    app.bot_data["client"] = httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (chkr-bot)",
            "Accept": "application/json",
        },
    )
    app.bot_data["sem"] = asyncio.Semaphore(MAX_CONCURRENT)
    app.bot_data.setdefault("stats", {"live": 0, "die": 0, "unk": 0, "err": 0})
    log.info("bot up — db=%s", DB_PATH)


async def on_shutdown(app: Application):
    c = app.bot_data.get("client")
    if c:
        await c.aclose()
    try:
        DB.commit()
        DB.close()
    except Exception:
        pass


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
