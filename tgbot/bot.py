"""Telegram bot UI for sticker-convert.

Run:  python -m tgbot
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import CONFIG
from .converter import TARGETS, Job, detect_link, run_job

log = logging.getLogger("tgbot")

# ---------------------------------------------------------------- state
# Per-user state lives in context.user_data:
#   mode: None | "await_link" | "collect" | "await_title"
#   url, source, target, files(int), job(Job)
ACTIVE: dict[int, Job] = {}          # user_id -> running job
SEM = asyncio.Semaphore(CONFIG.max_concurrent_jobs)
WAITING = 0
STATS_FILE = CONFIG.work_dir / "stats.json"
TG_FILE_LIMIT = 50 * 1024 * 1024


def load_stats() -> dict:
    try:
        return json.loads(STATS_FILE.read_text())
    except Exception:
        return {"users": [], "jobs": 0, "ok": 0, "failed": 0}


def save_stats(s: dict) -> None:
    try:
        STATS_FILE.write_text(json.dumps(s))
    except Exception:
        pass


STATS = load_stats()


def track_user(uid: int) -> None:
    if uid not in STATS["users"]:
        STATS["users"].append(uid)
        save_stats(STATS)


# ---------------------------------------------------------------- keyboards
def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


MAIN_MENU = kb(
    [
        [("🔗 Convert a pack link", "m:link")],
        [("🖼 Make pack from my files", "m:files")],
        [("❓ Help", "m:help"), ("ℹ️ About", "m:about")],
    ]
)


def targets_kb(local: bool) -> InlineKeyboardMarkup:
    keys = ["whatsapp", "telegram", "telegram_emoji", "signal", "discord", "imessage"]
    if not local:
        keys.append("raw")
    rows = [[(TARGETS[k][0], f"t:{k}")] for k in keys]
    rows.append([("✖️ Cancel", "m:cancel")])
    return kb(rows)


COLLECT_KB = kb([[("✅ Done, convert", "c:done"), ("✖️ Cancel", "m:cancel")]])
TITLE_KB = kb([[("⏭ Skip (auto name)", "title:skip")], [("✖️ Cancel", "m:cancel")]])
CANCEL_JOB_KB = kb([[("⛔ Stop job", "j:stop")]])

WELCOME = (
    "👋 <b>Sticker Convert Bot</b>\n\n"
    "Convert sticker packs between <b>Telegram, WhatsApp, Signal, LINE, Kakao, "
    "Viber, Band, OGQ, Discord</b> and <b>iMessage</b> — animated ones too.\n\n"
    "• Send me a <b>pack link</b> or just <b>forward any sticker</b>\n"
    "• Or send your own <b>images / GIFs / videos</b> to make a new pack\n\n"
    "Choose below 👇"
)

HELP = (
    "<b>How to use</b>\n\n"
    "<b>1. Pack link → anything</b>\n"
    "Send a link like:\n"
    "<code>https://t.me/addstickers/xxxx</code>\n"
    "<code>https://store.line.me/stickershop/product/1234/en</code>\n"
    "<code>https://e.kakao.com/t/xxxx</code>\n"
    "<code>https://signal.art/addstickers/#pack_id=..&amp;pack_key=..</code>\n"
    "or forward any Telegram sticker. Then pick the output.\n\n"
    "<b>2. Your files → pack</b>\n"
    "Tap <i>Make pack from my files</i>, send PNG/JPG/WEBP/GIF/MP4/WEBM/TGS "
    "(as file for best quality), tap <b>Done</b>, pick the output.\n\n"
    "<b>Outputs</b>\n"
    "🟢 WhatsApp: you get a <code>.wastickers</code> file — open it with the "
    "<i>Sticker Maker</i> app to add to WhatsApp.\n"
    "🔵 Telegram: a new pack is created in your account, you get the link.\n"
    "🔷 Signal: zip of ready files (upload via Signal Desktop).\n"
    "🍏 iMessage: Xcode project zip.\n\n"
    "Commands: /start /help /cancel /status"
)


# ---------------------------------------------------------------- helpers
async def guard(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    track_user(user.id)
    if CONFIG.is_allowed(user.id):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("🔒 This bot is private.")
    return False


def reset(ud: dict) -> None:
    for k in ("mode", "url", "source", "target", "files", "job", "collect_msg"):
        ud.pop(k, None)


async def safe_edit(msg: Message, text: str, markup=None) -> None:
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                            disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.debug("edit failed: %s", e)
    except TelegramError as e:
        log.debug("edit failed: %s", e)


def clean_title(raw: str) -> str:
    t = re.sub(r"\s+", " ", raw).strip()[:60]
    return t


def slug_from_url(url: str) -> str:
    part = re.split(r"[/#=?&]", url.rstrip("/"))[-1] or "Stickers"
    part = re.sub(r"[^0-9A-Za-z ]+", " ", part).strip()
    return (part or "Stickers")[:40]


async def unique_tg_title(app: Application, title: str) -> str:
    """sticker-convert names TG packs <title>_by_<bot>; with --no-confirm an existing
    pack of the same name would be wiped, so pick a free name first."""
    bot_name = (app.bot.username or "").replace("@", "")
    base = title if re.match(r"[A-Za-z]", title) else f"Pack {title}"
    for i in range(0, 50):
        cand = base if i == 0 else f"{base} {i + 1}"
        short = re.sub(r"[^0-9a-zA-Z]+", "_", cand.replace(" ", "_") + "_by_" + bot_name)
        try:
            await app.bot.get_sticker_set(short)
        except BadRequest:
            return cand  # not found -> free
        except TelegramError:
            return cand
    return f"{base} {int(time.time())}"


# ---------------------------------------------------------------- commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    reset(ctx.user_data)
    # deep link: /start <pack_short_name>
    if ctx.args:
        await handle_link_text(update, ctx, f"https://t.me/addstickers/{ctx.args[0]}")
        return
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    job = ACTIVE.get(uid)
    if job and job.proc and job.proc.returncode is None:
        job.cancelled = True
        job.proc.kill()
        await update.message.reply_text("⛔ Job stopped.")
    else:
        pending: Job | None = ctx.user_data.get("job")
        if pending:
            pending.cleanup()
        reset(ctx.user_data)
        await update.message.reply_text("Cancelled.", reply_markup=MAIN_MENU)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    running = len(ACTIVE)
    mine = "yes ⏳" if uid in ACTIVE else "no"
    await update.message.reply_text(
        f"Your job running: {mine}\nJobs running: {running}/{CONFIG.max_concurrent_jobs}\n"
        f"In queue: {WAITING}"
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    await update.message.reply_text(
        f"👥 Users: {len(STATS['users'])}\n🧩 Jobs: {STATS['jobs']}\n"
        f"✅ OK: {STATS['ok']}   ❌ Failed: {STATS['failed']}\n"
        f"⏳ Running now: {len(ACTIVE)}"
    )


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in CONFIG.admin_ids:
        return
    src = update.message.reply_to_message
    if not src:
        await update.message.reply_text("Reply to a message with /broadcast to send it to all users.")
        return
    sent = failed = 0
    for uid in list(STATS["users"]):
        try:
            await src.copy(uid)
            sent += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"Broadcast done. Sent {sent}, failed {failed}.")


# ---------------------------------------------------------------- menu buttons
async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not CONFIG.is_allowed(q.from_user.id):
        return
    action = q.data.split(":", 1)[1]
    ud = ctx.user_data

    if action == "link":
        reset(ud)
        ud["mode"] = "await_link"
        await safe_edit(q.message, "🔗 Send me a sticker pack <b>link</b>, or forward any "
                        "<b>Telegram sticker</b> from the pack.",
                        kb([[("✖️ Cancel", "m:cancel")]]))
    elif action == "files":
        if q.from_user.id in ACTIVE:
            await q.message.reply_text("⏳ You already have a job running. Wait or /cancel.")
            return
        reset(ud)
        job = Job(user_id=q.from_user.id, target="whatsapp")
        job.prepare()
        ud.update(mode="collect", job=job, files=0)
        await safe_edit(q.message,
                        f"🖼 Send your images / GIFs / videos / stickers (up to "
                        f"{CONFIG.max_files}). Send as <b>file</b> to keep transparency.\n\n"
                        "Tap <b>Done</b> when finished.", COLLECT_KB)
    elif action == "help":
        await q.message.reply_text(HELP, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
    elif action == "about":
        await q.message.reply_text(
            "Built on <a href='https://github.com/laggykiller/sticker-convert'>sticker-convert</a> "
            "(GPL-2.0). Not affiliated with any messaging app.",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    elif action == "cancel":
        pending: Job | None = ud.get("job")
        if pending and q.from_user.id not in ACTIVE:
            pending.cleanup()
        reset(ud)
        await safe_edit(q.message, WELCOME, MAIN_MENU)


# ---------------------------------------------------------------- link input
async def handle_link_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    found = detect_link(text)
    if not found:
        return False
    source, url = found
    ud = ctx.user_data
    reset(ud)
    ud.update(url=url, source=source)
    await update.effective_message.reply_text(
        f"✅ <b>{source}</b> pack detected.\n<code>{html.escape(url)}</code>\n\nConvert to:",
        parse_mode=ParseMode.HTML, reply_markup=targets_kb(local=False),
        disable_web_page_preview=True)
    return True


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    ud = ctx.user_data
    text = update.message.text or ""

    if ud.get("mode") == "await_title":
        title = clean_title(text)
        if not title:
            await update.message.reply_text("Send a name with letters, or tap Skip.")
            return
        await start_job(update, ctx, title)
        return

    if await handle_link_text(update, ctx, text):
        return

    if ud.get("mode") == "collect":
        await update.message.reply_text("Send media files, or tap Done.", reply_markup=COLLECT_KB)
        return

    await update.message.reply_text(
        "🤔 That isn't a supported link. Send a sticker pack link, forward a sticker, "
        "or use the menu.", reply_markup=MAIN_MENU)


# ---------------------------------------------------------------- media input
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov",
             ".tgs", ".apng", ".avif", ".heic", ".bmp", ".svg", ".mkv"}


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    ud = ctx.user_data
    msg = update.message

    # A forwarded/sent sticker outside collect mode -> convert its whole pack
    if msg.sticker and ud.get("mode") != "collect":
        if msg.sticker.set_name:
            kind = "addemoji" if msg.sticker.type == "custom_emoji" else "addstickers"
            await handle_link_text(update, ctx, f"https://t.me/{kind}/{msg.sticker.set_name}")
        else:
            await msg.reply_text("This sticker isn't part of a pack. Use "
                                 "“Make pack from my files” to convert single stickers.",
                                 reply_markup=MAIN_MENU)
        return

    if ud.get("mode") != "collect":
        await msg.reply_text("Want to make a pack from your files? Tap below first.",
                             reply_markup=MAIN_MENU)
        return

    job: Job = ud["job"]
    if ud.get("files", 0) >= CONFIG.max_files:
        await msg.reply_text(f"Limit is {CONFIG.max_files} files. Tap Done.", reply_markup=COLLECT_KB)
        return

    tg_file = None
    ext = ".bin"
    if msg.document:
        tg_file = msg.document
        ext = Path(msg.document.file_name or "").suffix.lower() or ext
        if ext not in MEDIA_EXT:
            await msg.reply_text(f"⚠️ {ext} not supported, skipped.")
            return
    elif msg.photo:
        tg_file, ext = msg.photo[-1], ".jpg"
    elif msg.sticker:
        tg_file = msg.sticker
        ext = ".tgs" if msg.sticker.is_animated else ".webm" if msg.sticker.is_video else ".webp"
    elif msg.animation:
        tg_file, ext = msg.animation, ".mp4"
    elif msg.video:
        tg_file, ext = msg.video, ".mp4"
    if tg_file is None:
        return
    if (getattr(tg_file, "file_size", 0) or 0) > 20 * 1024 * 1024:
        await msg.reply_text("⚠️ File over 20 MB (Telegram bot limit), skipped.")
        return

    n = ud.get("files", 0) + 1
    ud["files"] = n
    f = await tg_file.get_file()
    await f.download_to_drive(job.input_dir / f"{n:03d}{ext}")

    text = f"📥 Received <b>{n}</b> file(s). Send more or tap <b>Done</b>."
    old: Message | None = ud.get("collect_msg")
    if old:
        try:
            await old.delete()
        except TelegramError:
            pass
    ud["collect_msg"] = await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=COLLECT_KB)


async def on_collect_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    ud = ctx.user_data
    if ud.get("mode") != "collect" or not ud.get("files"):
        await q.message.reply_text("Send at least one file first.")
        return
    ud["mode"] = None
    await safe_edit(q.message, f"📦 {ud['files']} file(s) ready. Convert to:", targets_kb(local=True))


# ---------------------------------------------------------------- target + title
async def on_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    ud = ctx.user_data
    target = q.data.split(":", 1)[1]
    if target not in TARGETS:
        return
    if not ud.get("url") and not ud.get("job"):
        await safe_edit(q.message, "Session expired. Start again 👇", MAIN_MENU)
        return
    ud["target"] = target
    label = TARGETS[target][0]

    if target == "raw":
        await safe_edit(q.message, f"Selected: {label}")
        await start_job(update, ctx, None)
        return

    ud["mode"] = "await_title"
    hint = "Send a <b>name</b> for the new pack"
    if ud.get("url"):
        hint += ", or Skip to keep the original name"
    await safe_edit(q.message, f"Selected: <b>{label}</b>\n\n✏️ {hint}.", TITLE_KB)


async def on_title_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if ctx.user_data.get("mode") != "await_title":
        return
    await safe_edit(q.message, "⏭ Using automatic name.")
    await start_job(update, ctx, None)


# ---------------------------------------------------------------- run
async def start_job(update: Update, ctx: ContextTypes.DEFAULT_TYPE, title: str | None) -> None:
    global WAITING
    ud = ctx.user_data
    user = update.effective_user
    chat_id = update.effective_chat.id
    target = ud.get("target")
    if not target:
        return
    if user.id in ACTIVE:
        await ctx.bot.send_message(chat_id, "⏳ You already have a job running. Wait or /cancel.")
        return

    job: Job = ud.get("job") or Job(user_id=user.id, target=target)
    job.target = target
    job.url = ud.get("url")
    local = job.url is None

    if not title:
        if local:
            title = f"{user.first_name or 'My'} Stickers"
        elif target in ("telegram", "telegram_emoji", "whatsapp", "signal", "imessage"):
            # Telegram source: reuse its real title; otherwise derive from link
            title = None
            m = re.search(r"/(?:addstickers|addemoji)/(\w+)", job.url or "")
            if m:
                try:
                    title = (await ctx.bot.get_sticker_set(m.group(1))).title
                except TelegramError:
                    title = None
            title = clean_title(title or slug_from_url(job.url or ""))
    if target in ("telegram", "telegram_emoji") and title:
        title = await unique_tg_title(ctx.application, title)

    job.title = title
    job.author = CONFIG.default_author or (f"@{user.username}" if user.username else user.first_name)
    reset(ud)
    ACTIVE[user.id] = job

    status = await ctx.bot.send_message(
        chat_id,
        "🕐 Queued..." if SEM.locked() else "⚙️ Starting...",
        reply_markup=CANCEL_JOB_KB,
    )
    asyncio.create_task(_run_and_report(ctx, chat_id, status, job))


async def _run_and_report(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, status: Message, job: Job) -> None:
    global WAITING
    started = time.monotonic()
    label = TARGETS[job.target][0]
    try:
        WAITING += 1
        async with SEM:
            WAITING -= 1
            if job.cancelled:
                return
            await safe_edit(status, f"⚙️ Converting → {label}\n<i>this can take a few minutes</i>",
                            CANCEL_JOB_KB)

            async def progress(line: str) -> None:
                elapsed = int(time.monotonic() - started)
                await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
                await safe_edit(
                    status,
                    f"⚙️ Converting → {label}  ({elapsed}s)\n<code>{html.escape(line[:180])}</code>",
                    CANCEL_JOB_KB)

            result = await run_job(job, progress)

        STATS["jobs"] += 1
        if job.cancelled:
            await safe_edit(status, "⛔ Job stopped.")
            return

        if not result.ok:
            STATS["failed"] += 1
            tail = html.escape(result.log_tail[-700:])
            await safe_edit(status, f"❌ Conversion failed.\n\n<pre>{tail}</pre>")
            await ctx.bot.send_message(chat_id, "Try another output or link 👇", reply_markup=MAIN_MENU)
            return

        STATS["ok"] += 1
        secs = int(time.monotonic() - started)
        summary = html.escape(result.summary) if result.summary else ""
        await safe_edit(status, f"✅ Done in {secs}s → {label}\n<code>{summary}</code>")

        for url in result.urls:
            await ctx.bot.send_message(
                chat_id, f"🎉 Your pack is ready:\n{url}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add pack", url=url)]]))

        for path in result.files:
            if path.stat().st_size > TG_FILE_LIMIT:
                await ctx.bot.send_message(chat_id, f"⚠️ {path.name} is over 50 MB, too big to send.")
                continue
            caption = None
            if path.suffix == ".wastickers":
                caption = ("Open this file with the “Sticker Maker” app "
                           "(Android/iOS) to add it to WhatsApp.")
            elif job.target == "signal":
                caption = "Unzip and upload with Signal Desktop → Create sticker pack."
            elif job.target == "imessage":
                caption = "Open in Xcode, build and sideload to your iPhone."
            with path.open("rb") as fh:
                await ctx.bot.send_document(chat_id, fh, filename=path.name, caption=caption,
                                            read_timeout=120, write_timeout=120)

        await ctx.bot.send_message(chat_id, "Convert something else? 👇", reply_markup=MAIN_MENU)
        if CONFIG.log_channel_id:
            try:
                await ctx.bot.send_message(
                    CONFIG.log_channel_id,
                    f"#job ok user={job.user_id} target={job.target} src={job.url or 'files'} {secs}s")
            except TelegramError:
                pass
    except Exception as e:  # keep the bot alive whatever happens
        log.exception("job crashed")
        STATS["failed"] += 1
        await safe_edit(status, f"❌ Error: {html.escape(str(e))[:300]}")
    finally:
        save_stats(STATS)
        ACTIVE.pop(job.user_id, None)
        job.cleanup()


async def on_stop_job(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    job = ACTIVE.get(q.from_user.id)
    if not job:
        await q.answer("No running job.")
        return
    job.cancelled = True
    if job.proc and job.proc.returncode is None:
        job.proc.kill()
    await q.answer("Stopping...")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Update error", exc_info=ctx.error)


# ---------------------------------------------------------------- main
async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Main menu"),
        BotCommand("help", "How to use"),
        BotCommand("cancel", "Cancel / stop current job"),
        BotCommand("status", "Queue status"),
    ])


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    CONFIG.validate()
    (CONFIG.work_dir / "jobs").mkdir(parents=True, exist_ok=True)

    app = (Application.builder().token(CONFIG.bot_token).post_init(post_init)
           .concurrent_updates(True).build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_collect_done, pattern=r"^c:done$"))
    app.add_handler(CallbackQueryHandler(on_target, pattern=r"^t:"))
    app.add_handler(CallbackQueryHandler(on_title_skip, pattern=r"^title:skip$"))
    app.add_handler(CallbackQueryHandler(on_stop_job, pattern=r"^j:stop$"))

    media = (filters.Sticker.ALL | filters.PHOTO | filters.Document.ALL
             | filters.ANIMATION | filters.VIDEO)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & media, on_media))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
