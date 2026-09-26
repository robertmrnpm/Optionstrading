"""Push notifications to your phone: Discord webhook, Telegram bot, and/or ntfy.sh.

Configure any of these in .env:
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  TELEGRAM_BOT_TOKEN=...   TELEGRAM_CHAT_ID=...
  NTFY_TOPIC=my-secret-topic-name   (install the ntfy app and subscribe to the topic)
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self) -> None:
        self.discord = os.getenv("DISCORD_WEBHOOK_URL")
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID")
        self.ntfy_topic = os.getenv("NTFY_TOPIC")
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh")
        self.dashboard_url = os.getenv("DASHBOARD_PUBLIC_URL", "")

    @property
    def enabled(self) -> bool:
        return bool(self.discord or (self.tg_token and self.tg_chat) or self.ntfy_topic)

    async def send(self, title: str, body: str, priority: str = "default") -> None:
        if not self.enabled:
            return
        text = f"{title}\n{body}"
        if self.dashboard_url:
            text += f"\n{self.dashboard_url}"
        tasks = []
        async with httpx.AsyncClient(timeout=10) as client:
            if self.discord:
                tasks.append(client.post(self.discord, json={"content": f"**{title}**\n{body}"}))
            if self.tg_token and self.tg_chat:
                tasks.append(client.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                         json={"chat_id": self.tg_chat, "text": text}))
            if self.ntfy_topic:
                headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority}
                if self.dashboard_url:
                    headers["Click"] = self.dashboard_url
                tasks.append(client.post(f"{self.ntfy_server}/{self.ntfy_topic}", content=body.encode(),
                                         headers=headers))
            results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.warning("notification failed: %s", r)
