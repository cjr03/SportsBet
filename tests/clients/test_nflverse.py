import nflreadpy
import polars as pl

from gridiron_edge.clients.nflverse import NflverseSource


def test_nflverse_fallback(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("down")

    urls = []

    def fake_read(url, *a, **k):
        urls.append(url)
        return pl.DataFrame({"game_id": ["g"], "season": [2024], "week": [1], "spread_line": [-3.0]})

    monkeypatch.setattr(nflreadpy, "load_schedules", boom)
    monkeypatch.setattr(pl, "read_parquet", fake_read)
    df = NflverseSource(tmp_path).schedules([2024])
    assert "spread_line" in df.columns and df.height == 1
    assert urls[0].startswith("https://github.com/nflverse/nflverse-data/releases/download/") and urls[0].endswith(".parquet")


def test_team_epa_aggregates(monkeypatch, tmp_path):
    pbp = pl.DataFrame({
        "game_id": ["g"] * 4, "season": [2024] * 4, "week": [1] * 4,
        "posteam": ["SEA", "SEA", "CHI", None], "defteam": ["CHI", "CHI", "SEA", None],
        "play_type": ["pass", "run", "pass", "kickoff"], "epa": [0.5, 0.1, -0.2, 0.0]})
    monkeypatch.setattr(nflreadpy, "load_pbp", lambda seasons: pbp)
    df = NflverseSource(tmp_path).team_epa([2024]).sort("team")
    chi, sea = df.to_dicts()
    assert sea["off_epa"] == 0.3 and sea["def_epa"] == -0.2 and sea["plays"] == 2
    assert chi["off_epa"] == -0.2 and chi["def_epa"] == 0.3 and chi["plays"] == 1


def test_nflverse_fallback_two_seasons_no_dupes(monkeypatch, tmp_path):
    urls = []

    def fake_read(url, *a, **k):
        urls.append(url)
        return pl.DataFrame({"game_id": ["a", "b", "c"], "season": [2023, 2024, 2022], "week": [1, 1, 1]})

    monkeypatch.setattr(nflreadpy, "load_schedules", lambda s: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(pl, "read_parquet", fake_read)
    df = NflverseSource(tmp_path).schedules([2023, 2024])
    assert len(urls) == 1
    assert sorted(df["game_id"].to_list()) == ["a", "b"] and set(df["season"].to_list()) == {2023, 2024}
