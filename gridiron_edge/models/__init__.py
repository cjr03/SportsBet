from dataclasses import dataclass


@dataclass
class Prediction:
    game_id: str
    mean_margin: float  # home minus away
    mean_total: float | None
    model_version: str
