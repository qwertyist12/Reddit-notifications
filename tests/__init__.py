"""Test package.

Quiet the bot's own loggers so the failure paths the tests deliberately exercise
do not print tracebacks. This raises the loggers' levels rather than calling
logging.disable(), which is a global filter that assertLogs cannot see past.
"""

import logging

for name in ("bot.watcher", "bot.reddit", "bot.discord_bot"):
    logging.getLogger(name).setLevel(logging.CRITICAL)
