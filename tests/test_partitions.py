from datetime import date

import pytest

from scalper.backtest.partitions import ExperimentRegistry, HoldoutLocked, date_filter, partition_of


def test_partition_boundaries():
    assert partition_of(date(2024, 12, 31)) == "development"
    assert partition_of(date(2025, 1, 1)) == "validation"
    assert partition_of(date(2026, 3, 2)) == "holdout"
    assert partition_of(date(2026, 10, 5)) == "live_forward"


def test_holdout_locked_until_validation_pass_and_only_once(tmp_path):
    reg = ExperimentRegistry(str(tmp_path / "x.sqlite"))
    with pytest.raises(HoldoutLocked):
        date_filter("holdout")
    with pytest.raises(HoldoutLocked):
        date_filter("holdout", strategy="orb", version="1.0.0", registry=reg)
    reg.record("r1", "H1", "orb", "1.0.0", "R4", "validation", {}, True)
    f = date_filter("holdout", strategy="orb", version="1.0.0", registry=reg)
    assert f(date(2026, 2, 2)) and not f(date(2025, 2, 2))
    reg.record("r2", "H1", "orb", "1.0.0", "R5", "holdout", {}, False)
    with pytest.raises(HoldoutLocked):
        date_filter("holdout", strategy="orb", version="1.0.0", registry=reg)
    assert reg.trials("H1") == 2
