import os

from dotenv import load_dotenv


load_dotenv()

# EMBED COLOR
COLOR = 0xf0319b

# BOT TOKEN
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Add it to the .env file before starting the bot."
    )
