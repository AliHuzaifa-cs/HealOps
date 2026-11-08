import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from healops import HealOps
from healops.events import Observation
from healops.exceptions import StateStoreError
from healops.store import StateStore
from healops.types import ObservationKind

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def fill(path, count=30):
    store = StateStore(path)
    store.add_observations(
        [Observation("app", ObservationKind.METRIC, "latency_ms", float(i), NOW) for i in range(count)]
    )
    return store


def damage(path):
    """Break the first page, the way the real file was broken: the table list is unreadable."""
    store_files = [path, path + "-wal", path + "-shm"]
    data = bytearray(Path(path).read_bytes())
    data[100:200] = b"\xff" * 100
    Path(path).write_bytes(data)
    for extra in store_files[1:]:
        if os.path.exists(extra):
            os.remove(extra)


def count(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("select count(*) from observations").fetchone()[0]
    finally:
        c.close()


def test_backup_is_a_healthy_copy_and_old_ones_are_pruned(tmp_path):
    path = str(tmp_path / "state.db")
    store = fill(path)
    made = []
    for i in range(4):
        store.backup(keep=2)
        time.sleep(1.1)  # the file name has a one-second stamp
        made.append(i)
    files = store.backups()
    assert len(files) == 2
    assert count(str(files[0])) == 30
    store.close()


def test_maybe_backup_waits_for_the_interval(tmp_path):
    store = fill(str(tmp_path / "state.db"))
    assert store.maybe_backup(every_hours=12) is not None
    assert store.maybe_backup(every_hours=12) is None  # one was just made
    assert store.maybe_backup(every_hours=0) is None  # off
    store.close()


def test_a_damaged_file_is_replaced_by_the_newest_backup(tmp_path):
    path = str(tmp_path / "state.db")
    store = fill(path)
    store.backup()
    store.add_observations([Observation("app", ObservationKind.METRIC, "x", 1.0, NOW)])  # newer than the backup
    store.close()
    damage(path)
    probe = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            probe.execute("pragma integrity_check").fetchall()
    finally:
        probe.close()

    reopened = StateStore(path)
    assert reopened.restored_from is not None and reopened.restored_from.startswith("state-")
    assert count(path) == 30  # what the backup held
    assert any(".damaged-" in name for name in os.listdir(tmp_path))  # the damaged file is kept
    reopened.close()


def test_a_damaged_file_without_a_backup_is_reported_not_hidden(tmp_path):
    path = str(tmp_path / "state.db")
    fill(path).close()
    damage(path)
    with pytest.raises(StateStoreError):
        StateStore(path)
    assert os.path.exists(path)  # nothing was moved or deleted


def test_a_reader_never_replaces_the_file(tmp_path):
    path = str(tmp_path / "state.db")
    store = fill(path)
    store.backup()
    store.close()
    damage(path)
    with pytest.raises(StateStoreError):
        StateStore(path, repair=False)
    assert not any(".damaged-" in name for name in os.listdir(tmp_path))


def test_the_monitor_takes_a_backup_on_its_first_tick(tmp_path):
    from healops.testing.toy_app import ToyApp

    path = str(tmp_path / "state.db")
    healops = HealOps(
        {"state_db_path": path, "audit_log_path": None, "state_backup_hours": 1, "dry_run": True}
    )
    healops.register("toy", ToyApp(), component="web")
    healops.tick()
    assert len(healops.store.backups()) == 1
    healops.close()
