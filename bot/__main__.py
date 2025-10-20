from bot.bot import main
import logging
import asyncio

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger("discord").info("🛑 Bot stopped by user.")