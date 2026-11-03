# Sticker Convert — Telegram Bot

A Telegram bot UI on top of [sticker-convert](https://github.com/laggykiller/sticker-convert).
Users convert sticker packs between Telegram, WhatsApp, Signal, LINE, Kakao, Viber, Naver Band,
OGQ, Discord and iMessage straight from Telegram, with inline buttons. No GUI needed.

## Features

- 🔗 **Pack link → any app**: Telegram, LINE, Kakao, Signal, Viber, Band, OGQ links (auto-detected)
- 📨 **Forward any Telegram sticker** → bot picks up its whole pack
- 🖼 **Your files → new pack**: PNG / JPG / WEBP / GIF / MP4 / WEBM / TGS (up to 120 files)
- Outputs:
  - 🟢 WhatsApp `.wastickers` (big packs are split into 30-sticker packs automatically)
  - 🔵 Telegram sticker pack, 😀 Telegram custom-emoji pack (created in the user's own account, link returned)
  - 🔷 Signal (zip, or direct upload if you set Signal creds)
  - 🎮 Discord files, 🍏 iMessage Xcode project, 📦 original files only
- ⏳ Job queue with live progress, ⛔ stop button, per-user single job
- 🔒 Optional private mode (`ALLOWED_USER_IDS`), 👑 admin `/stats` and `/broadcast`, optional log channel
- Auto-safe Telegram pack names (never overwrites an existing pack)

## Setup

### 1. Create the bot
Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.

### 2. Configure
```bash
cp .env.example .env
nano .env        # set BOT_TOKEN and ADMIN_IDS
```

### 3a. Run with Docker (recommended)
```bash
docker compose -f docker-compose.bot.yml up -d --build
docker compose -f docker-compose.bot.yml logs -f
```

### 3b. Run with Python (3.10 – 3.13)
```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements-bot.txt
PYTHONPATH=src python -m tgbot                        # Windows: set PYTHONPATH=src && python -m tgbot
```

## Commands

| Command | Who | What |
|---|---|---|
| `/start` | all | Main menu |
| `/help` | all | Usage guide |
| `/cancel` | all | Cancel input / stop running job |
| `/status` | all | Queue status |
| `/stats` | admin | Users, jobs, success/fail counts |
| `/broadcast` | admin | Reply to any message with it to send to all users |

Deep link: `https://t.me/<yourbot>?start=<pack_short_name>` opens the bot with that Telegram pack ready to convert.

## Hosting notes

- Conversion is CPU heavy. 2 vCPU / 2 GB RAM handles `MAX_CONCURRENT_JOBS=2` comfortably.
  Free tiers that sleep (Render free, etc.) will kill long jobs — a small VPS is better.
- Telegram bots can only receive files up to 20 MB and send up to 50 MB.
- WhatsApp: users open the `.wastickers` file with the **Sticker Maker** app to import.
- Packs created on Telegram are owned by the user who requested them, with names ending in `_by_<yourbot>`.

## Files

```
tgbot/
  config.py      # reads .env
  converter.py   # builds and runs the sticker-convert CLI per job
  bot.py         # Telegram UI: menus, buttons, queue, results
.env.example
requirements-bot.txt
Dockerfile.bot
docker-compose.bot.yml
```

License: GPL-2.0, same as sticker-convert.
