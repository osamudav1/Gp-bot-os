# Telegram Group Management Bot

A Python-based Telegram bot built with aiogram 3.3.0 for managing Telegram groups.

## Features

- CAPTCHA verification for new members
- User warning system (auto-mute after max warns)
- Mute / Unmute / Ban / Unban commands
- Anti-forwarding mode per group
- Message counting and level-up system
- Welcome messages with optional photo
- Broadcast to users, groups, or all
- Resource monitoring (CPU/RAM)
- Owner control panel via inline keyboard menu

## Tech Stack

- **Language**: Python 3.12
- **Bot framework**: aiogram 3.3.0
- **Database**: MongoDB (via motor async driver)
- **Config**: Environment variables via python-dotenv

## Required Secrets

| Secret | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `OWNER_ID` | Your Telegram numeric user ID |
| `MONGO_URI` | MongoDB connection string |

Optional:
- `DB_NAME` — Database name (defaults to `group_management_bot`)

## Project Layout

```
bot.py          # Main bot file (all handlers, database, utils)
requirements.txt
Procfile
```

## Running

The workflow runs `python bot.py` as a console background process.
