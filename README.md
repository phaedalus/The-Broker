# The Broker

The Broker is a low-resource Discord voice matchmaking bot for PAYDAY: The Heist, PAYDAY 2, and PAYDAY 3.

## Release

Current version: **1.0.0**

## Requirements

- Python 3.13
- `discord.py==2.7.1`
- A Discord bot token supplied through the `DISCORD_TOKEN` environment variable

## Local startup

```powershell
$env:DISCORD_TOKEN = "YOUR_PRIVATE_TOKEN"
.\.venv\Scripts\python.exe .\bot.py
```

Never place the bot token in `bot.py`, `README.md`, or a public repository.

## PebbleHost startup

Install the packages from `requirements.txt`, then create a private `.env` file in the bot's root directory containing:

```text
DISCORD_TOKEN=your_private_bot_token
```

The `.env` file is excluded from Git and must be created directly in PebbleHost's File Manager. Start the bot with:

```text
python bot.py
```

Run `/broker_setup` as a Discord server administrator after the first launch. It can use the configured channels, accept replacements selected through the command, or create the matchmaking category and Join Queue voice channel if they are missing.

## Status and avatar rotation

The bot rotates its activity status every 15 minutes. To enable avatar rotation, place at least two `.png`, `.jpg`, `.jpeg`, or `.webp` files inside the `avatars` folder. Avatars rotate every six hours to avoid aggressive Discord profile-edit rate limits.

An administrator can run `/rotate_avatar` once to test the next image. Avoid repeatedly using this command because Discord applies dynamic API rate limits to profile changes.

Each avatar filename also becomes the bot's server nickname. An optional numeric ordering prefix is removed, so `01 - Broker Bain.png` becomes **Broker Bain**. Discord nicknames are limited to 32 characters.

## Bot permissions

- View Channels
- Send Messages
- Read Message History
- Connect
- Move Members
- Manage Channels
- Use Application Commands

## Data

Active crew recovery uses `broker_state.json`. Channel configuration created through `/broker_setup` uses `broker_config.json`. Both are generated automatically and contain no bot token.
