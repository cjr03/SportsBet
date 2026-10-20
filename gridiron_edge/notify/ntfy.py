"""ntfy.sh push notifications."""
import httpx

from gridiron_edge.notify import NotifyResult


class NtfyNotifier:
    name = "ntfy"

    def __init__(self, topic: str, post=httpx.post):
        self.topic = topic
        self.post = post

    def send(self, text: str, priority: int = 4) -> NotifyResult:
        try:
            r = self.post(f"https://ntfy.sh/{self.topic}", content=text.encode("ascii", "ignore"),
                          headers={"Title": "Gridiron Edge", "Priority": str(priority)}, timeout=15)
            r.raise_for_status()
            return NotifyResult(True, self.name)
        except Exception as e:
            return NotifyResult(False, self.name, str(e))
