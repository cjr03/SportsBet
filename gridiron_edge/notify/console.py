"""In-app fallback channel: appends to a sink the UI shows in its event log."""
from gridiron_edge.notify import NotifyResult


class ConsoleNotifier:
    name = "console"

    def __init__(self, sink: list[str]):
        self.sink = sink

    def send(self, text: str) -> NotifyResult:
        self.sink.append(text)
        return NotifyResult(True, self.name)
