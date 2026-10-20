"""iMessage via osascript. Number and text go in as argv, never interpolated into the script."""
import subprocess

from gridiron_edge.notify import NotifyResult

TIMEOUT = 30

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
        # Errors are reported without argv: str(CalledProcessError/TimeoutExpired) embeds the
        # phone number, and these messages land in the notifications table and the event log.
        try:
            self.runner(["osascript", "-e", SCRIPT, self.phone, text, "iMessage"],
                        timeout=TIMEOUT, capture_output=True, check=True)
            return NotifyResult(True, self.name)
        except subprocess.CalledProcessError as e:
            err = e.stderr or b""
            if isinstance(err, str):
                err = err.encode()
            detail = err[:200].decode(errors="ignore").replace(self.phone, "<phone>").strip()
            return NotifyResult(False, self.name, f"osascript exit {e.returncode}: {detail}")
        except subprocess.TimeoutExpired:
            return NotifyResult(False, self.name, f"osascript timed out after {TIMEOUT}s")
        except Exception as e:  # OSError (no osascript) and the like: no argv in the message
            return NotifyResult(False, self.name, f"{type(e).__name__}: {e}".replace(self.phone, "<phone>"))
