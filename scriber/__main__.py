"""Scriber entrypoint: runs the Discord bot and the web dashboard in one asyncio loop."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from scriber import config, database
from scriber.bot.client import create_bot
from scriber.memory import MemoryManager
from scriber.summary import Summarizer
from scriber.transcription import WhisperEngine
from scriber.web import create_app

log = logging.getLogger("scriber")


def main() -> None:
    """Initialize configuration, database and services, then run bot + web server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cfg_manager = config.init()
    cfg = cfg_manager.config

    (cfg.data_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "models").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "memory").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "avatars").mkdir(parents=True, exist_ok=True)
    database.init(cfg.data_dir / "scriber.db")

    whisper = WhisperEngine(models_dir=cfg.data_dir / "models")
    summarizer = Summarizer()
    memory = MemoryManager(cfg.data_dir / "memory")

    # Building the bot must never stop the dashboard from coming up. If the
    # token is missing or the bot cannot be constructed, we log it and start
    # web-only so the operator sees the problem in the browser (with the fix
    # and the invite link) instead of nginx returning a bare 502.
    bot = None
    if cfg.discord_token:
        try:
            bot = create_bot(whisper, summarizer, memory)
        except Exception:
            log.exception("Failed to create the Discord bot; starting the web dashboard only.")
    else:
        log.warning("DISCORD_TOKEN not set — running the web dashboard only.")

    app = create_app(bot=bot)

    async def run() -> None:
        server = uvicorn.Server(
            uvicorn.Config(app, host=cfg.web_host, port=cfg.web_port, log_level="info")
        )

        async def run_bot() -> None:
            # A bot failure (bad token, lost gateway, refused sync…) must not
            # take the web dashboard down with it, so swallow and record it here
            # instead of letting it propagate out of gather().
            try:
                await bot.start(cfg.discord_token)
            except Exception as exc:
                log.exception("Discord bot stopped; the web dashboard stays available.")
                bot.setup_error = bot.setup_error or (
                    f"The Discord bot stopped due to an error: {exc}"
                )

        tasks = [server.serve()]
        if bot is not None:
            tasks.append(run_bot())
        await asyncio.gather(*tasks)

    try:
        asyncio.run(run())
    finally:
        database.close()


if __name__ == "__main__":
    main()
