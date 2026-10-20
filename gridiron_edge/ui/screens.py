"""Modal screens."""
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from gridiron_edge.types import Recommendation


class ConfirmScreen(ModalScreen[tuple[float | None, int | None] | None]):
    """Confirm a recommendation, optionally overriding the fill price and quantity."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, rec: Recommendation):
        super().__init__()
        self.rec = rec

    def compose(self) -> ComposeResult:
        legs = "\n".join(f"{leg.description} ({leg.side}) @ {leg.price:.2f}"
                         for leg in self.rec.legs)
        with Vertical(id="confirm-box"):
            yield Label(f"Confirm #{self.rec.id}  stake ${self.rec.stake_cents / 100:,.2f}")
            yield Static(legs, id="legs")
            yield Label("Price (0-1)")
            yield Input(value=f"{self.rec.legs[0].price:.2f}", id="price")
            yield Label("Quantity")
            yield Input(value="" if self.rec.qty is None else str(self.rec.qty), id="qty")
            yield Static("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Confirm", id="ok", variant="success")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#error").display = False

    def _error(self, msg: str) -> None:
        err = self.query_one("#error", Static)
        err.update(msg)
        err.display = True

    def _parse(self) -> tuple[float | None, int | None]:
        raw_p = self.query_one("#price", Input).value.strip()
        raw_q = self.query_one("#qty", Input).value.strip()
        price = None
        if raw_p:
            try:
                price = float(raw_p)
            except ValueError:
                raise ValueError("price must be a number between 0 and 1") from None
            if not 0 < price < 1:
                raise ValueError("price must be between 0 and 1 (exclusive)")
        qty = None
        if raw_q:
            try:
                qty = int(raw_q)
            except ValueError:
                raise ValueError("quantity must be a whole number") from None
            if qty < 1:
                raise ValueError("quantity must be at least 1")
        return price, qty

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
