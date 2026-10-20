"""iMessage via osascript. Number and text go in as argv, never interpolated into the script."""
import subprocess

from gridiron_edge.notify import NotifyResult

SCRIPT = '''
on run argv
  set targetNumber to item 1 of argv
  set msg to item 2 of argv
  set svcName to item 3 of argv
  tell application "Messages"
    if svcName is "SMS" then
      set svc to 1st account whose service type = SMS
    else if svcName is "RCS" then
      set svc to 1st account whose service type = RCS
    else
      set svc to 1st account whose service type = iMessage
    end if
    send msg to participant targetNumber of svc
  end tell
end run'''


class IMessageNotifier:
    name = "imessage"

    def __init__(self, phone: str, runner=subprocess.run):
        self.phone = phone
        self.runner = runner

    def send(self, text: str) -> NotifyResult:
        try:
            self.runner(["osascript", "-e", SCRIPT, self.phone, text, "iMessage"],
                        timeout=30, capture_output=True, check=True)
            return NotifyResult(True, self.name)
        except Exception as e:
            return NotifyResult(False, self.name, str(e))
