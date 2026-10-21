from dataclasses import dataclass


@dataclass
class Prediction:
    game_id: str
    mean_margin: float  # home minus away
    mean_total: float | None
    model_version: str
    veto: str | None = None  # why the model gets no weight for this game (e.g. a QB change)
