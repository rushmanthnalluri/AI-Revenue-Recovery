"""Temporal split guarantees: disjoint blocks, chronological order, full
coverage, every class present in every block (the generator interleaves)."""

import pandas as pd
import pytest

from app.services.diagnosis.synthetic import SyntheticConfig, generate_dataset
from app.services.diagnosis.taxonomy import CAUSES
from app.services.diagnosis.training import temporal_split


@pytest.fixture(scope="module")
def df() -> pd.DataFrame:
    return generate_dataset(SyntheticConfig(windows_per_class=10, seed=11))


def test_split_counts_and_disjointness(df):
    train, val, test = temporal_split(df, train_frac=0.6, val_frac=0.2)
    assert (len(train), len(val), len(test)) == (48, 16, 16)
    ids = [set(b["window_id"]) for b in (train, val, test)]
    assert not (ids[0] & ids[1] or ids[0] & ids[2] or ids[1] & ids[2])
    assert len(ids[0] | ids[1] | ids[2]) == len(df)


def test_split_is_temporally_ordered(df):
    train, val, test = temporal_split(df, train_frac=0.6, val_frac=0.2)
    assert train["window_end"].max() <= val["window_end"].min()
    assert val["window_end"].max() <= test["window_end"].min()
    # No window overlaps across blocks at all.
    assert train["window_start"].min() >= df["window_start"].min()
    for block in (train, val, test):
        assert block["window_end"].is_monotonic_increasing


def test_every_class_in_every_block(df):
    train, val, test = temporal_split(df)
    for block in (train, val, test):
        assert set(block["label"]) == set(CAUSES)


def test_split_rejects_bad_fractions(df):
    with pytest.raises(ValueError):
        temporal_split(df, train_frac=0.9, val_frac=0.2)  # sums > 1
    with pytest.raises(ValueError):
        temporal_split(df, train_frac=0.0, val_frac=0.5)
