"""Modal screens."""
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from gridiron_edge import describe as d
from gridiron_edge.types import Recommendation


class ConfirmScreen(ModalScreen[tuple[float | None, int | None, int | None] | None]):
    """Confirm a recommendation, optionally overriding the fill price and quantity (Kalshi) or the amount
    put in (PrizePicks). Returns (price, contracts, stake in cents); None means cancelled."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, rec: Recommendation, names: d.Names = d.DEFAULT_NAMES):
        super().__init__()
        self.rec = rec
        self.names = names

    def compose(self) -> ComposeResult:
        r = self.rec
        with Vertical(id="confirm-box"):
            if r.kind == "pp_entry":  # no price or contracts on PrizePicks: the amount you put in
                which = f"PrizePicks entry #{r.id}" if r.id is not None else "this PrizePicks entry"
                amount = f"{d.money(r.stake_cents)}, " if r.stake_cents > 0 else ""
                yield Label(f"Did you place {which} ({amount}{d.structure_text(r.structure)})?")
                yield Static(Text(d.bet_text(r, self.names)), id="legs")
                yield Label("Amount you put in, in dollars (e.g. 10)")
                yield Input(value=f"{r.stake_cents / 100:.2f}" if r.stake_cents > 0 else "", id="stake")
            else:
                yield Label(f"Did you place bet idea #{r.id}?  Bet {d.money(r.stake_cents)} at {d.price_text(r)}")
                yield Static(Text(d.bet_text(r, self.names)), id="legs")
                yield Label("Price you got, in dollars (e.g. 0.47)")
                yield Input(value=f"{r.legs[0].price:.2f}", id="price")
                yield Label("How many contracts")
                yield Input(value="" if r.qty is None else str(r.qty), id="qty")
            yield Static("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Yes, placed", id="ok", variant="success")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#error").display = False

    def _error(self, msg: str) -> None:
        err = self.query_one("#error", Static)
        err.update(msg)
        err.display = True

    def _parse(self) -> tuple[float | None, int | None, int | None]:
        if self.rec.kind == "pp_entry":
            raw = self.query_one("#stake", Input).value.strip().lstrip("$").replace(",", "")
            if not raw:
                if self.rec.stake_cents > 0:
                    return None, None, self.rec.stake_cents
                raise ValueError("Type the amount you put in, in dollars (e.g. 10)")
            try:
                cents = round(float(raw) * 100)
            except ValueError:
                raise ValueError("The amount must be a number of dollars, e.g. 10 or 12.50") from None
            if cents < 1:
                raise ValueError("The amount must be more than $0.00")
            return None, None, cents
        raw_p = self.query_one("#price", Input).value.strip()
        raw_q = self.query_one("#qty", Input).value.strip()
        price = None
        if raw_p:
            try:
                price = float(raw_p)
            except ValueError:
                raise ValueError("The price must be a number in dollars between 0 and 1, e.g. 0.47") from None
            if not 0 < price < 1:
                raise ValueError("The price must be in dollars between 0 and 1, e.g. 0.47")
        qty = None
        if raw_q:
            try:
                qty = int(raw_q)
            except ValueError:
                raise ValueError("The number of contracts must be a whole number") from None
            if qty < 1:
                raise ValueError("The number of contracts must be at least 1")
        return price, qty, None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        try:
            self.dismiss(self._parse())
        except ValueError as e:
            self._error(str(e))

    def action_cancel(self) -> None:
        self.dismiss(None)


class WhyScreen(ModalScreen[None]):
    """'Why this bet?': plain-English reasons; the last line holds the internal details."""

    BINDINGS = [("escape", "close", "Close"), ("d", "close", "Close"), ("q", "close", "Close")]

    def __init__(self, lines: list[str], title: str = "Why this bet?"):
        super().__init__()
        self.lines = lines
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical(id="why-box"):
            yield Label(self.title_text, id="why-title")
            yield Static(Text("\n".join(self.lines[:-1])), id="why-body")
            yield Static(Text(self.lines[-1] if self.lines else ""), id="why-details")
            with Horizontal(id="buttons"):
                yield Button("Close", id="close")

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
