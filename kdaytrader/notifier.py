"""알림: 콘솔 로그 + (선택) 텔레그램."""
from __future__ import annotations

import logging
import threading

import requests

log = logging.getLogger("kdaytrader.signal")


class Notifier:
    def __init__(self, telegram_token: str = "", telegram_chat_id: str = "", console: bool = True):
        self.token = telegram_token or ""
        self.chat_id = telegram_chat_id or ""
        self.console = console
        self.history: list[str] = []

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, level: int = logging.INFO) -> None:
        self.history.append(text)
        if len(self.history) > 500:
            self.history = self.history[-500:]
        if self.console:
            log.log(level, text)
        if self.telegram_enabled:
            threading.Thread(target=self._telegram, args=(text,), daemon=True).start()

    def _telegram(self, text: str) -> None:
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=5,
            )
        except Exception as e:  # pragma: no cover - network
            log.warning("텔레그램 전송 실패: %s", e)
