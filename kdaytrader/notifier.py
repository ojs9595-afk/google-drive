"""알림: 콘솔 로그 + (선택) 텔레그램. 텔레그램은 단일 워커 스레드의 큐로 전송한다."""
from __future__ import annotations

import logging
import queue
import threading

import requests

log = logging.getLogger("kdaytrader.signal")


class Notifier:
    def __init__(self, telegram_token: str = "", telegram_chat_id: str = "", console: bool = True):
        self.token = telegram_token or ""
        self.chat_id = telegram_chat_id or ""
        self.console = console
        self.history: list[str] = []
        self._q: queue.Queue[str] = queue.Queue(maxsize=200)
        self._worker: threading.Thread | None = None
        self._last_err = ""

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
            try:
                self._q.put_nowait(text)
            except queue.Full:
                log.warning("텔레그램 전송 큐가 가득 차 메시지를 버립니다")
                return
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, daemon=True, name="telegram")
                self._worker.start()

    def _loop(self) -> None:
        while True:
            text = self._q.get()
            try:
                r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={"chat_id": self.chat_id, "text": text}, timeout=8)
                ok = r.status_code == 200 and (r.json().get("ok") if r.headers.get("content-type", "").startswith("application/json") else False)
                if not ok:
                    err = f"HTTP {r.status_code} {r.text[:120]}"
                    if err != self._last_err:
                        self._last_err = err
                        log.warning("텔레그램 전송 실패: %s", err)
            except Exception as e:  # pragma: no cover - network
                err = f"{type(e).__name__}: {e}"
                if err != self._last_err:
                    self._last_err = err
                    log.warning("텔레그램 전송 실패: %s", err)
            finally:
                self._q.task_done()
