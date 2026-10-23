"""Indexed lookup must retain the legacy matching and ordering semantics."""
import random
from types import SimpleNamespace as NS

from apps.worker.jobs.subscriptions import _PanelSnapshotIndex, _locate_client_in_snapshot


def test_snapshot_index_matches_linear_reference():
    rng = random.Random(24010)
    emails = ["", "User", "USER", "Straße", "STRASSE", "other"]
    ids = [None, "", "a", "b", "c", 17]
    for _ in range(100):
        inbounds = [NS(
            id=rng.choice([None, 1, 2]),
            settings={"clients": [dict(email=rng.choice(emails),
                                      id=rng.choice(ids), uuid=rng.choice(ids))
                                  for _ in range(rng.randrange(20))]},
            client_stats=[dict(email=rng.choice(emails), up=i)
                          for i in range(rng.randrange(20))],
        ) for _ in range(5)]
        index = _PanelSnapshotIndex(inbounds)
        for _ in range(100):
            query = dict(inbound_remote_id=rng.choice([None, 1, 2, 9]),
                         email=rng.choice(emails), client_remote_id=rng.choice(ids),
                         client_uuid=rng.choice(ids))
            expected = _locate_client_in_snapshot(inbounds, **query)
            actual = index.locate(**query)
            assert actual[0] is expected[0]
            assert actual[1] is expected[1]


def test_snapshot_index_preserves_earliest_match_across_email_and_id():
    first = {"email": "old", "id": "same-id"}
    second = {"email": "new", "id": "new-id"}
    stat = {"email": "new", "up": 7}
    index = _PanelSnapshotIndex([NS(id=1, settings={"clients": [first, second]},
                                   client_stats=[stat])])
    result = index.locate(inbound_remote_id=1, email="NEW",
                          client_remote_id="same-id", client_uuid=None)
    assert result[0] is first
    assert result[1] is stat


def test_missing_client_does_not_return_orphan_stats():
    index = _PanelSnapshotIndex([NS(id=1, settings={}, client_stats=[{"email": "x"}])])
    assert index.locate(inbound_remote_id=1, email="x",
                        client_remote_id=None, client_uuid=None) == (None, None)


def test_malformed_later_client_keeps_earlier_valid_match():
    first = {"email": "valid", "id": "a"}
    snapshot = [NS(id=1, settings={"clients": [first, None]}, client_stats=[])]
    index = _PanelSnapshotIndex(snapshot)
    result = index.locate(inbound_remote_id=1, email="valid",
                          client_remote_id=None, client_uuid=None)
    assert result[0] is first
    assert result[1] is None
