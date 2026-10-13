"""Every cell of the paper's Table I (Choi, Brunet & How 2009), plus the
bundle-release rule (eq. 6), timestamps (eq. 5) and tie-breaking.

Receiver is "i", sender is "k", third parties are "m" and "n".
"""
import pytest

from app.cbba.agent import LEAVE, RESET, UPDATE, CbbaAgent, decide_action
from app.cbba.models import CbbaConfig
from app.cbba.network import CbbaMessage
from app.cbba.scoring import best_insertion, evaluate_path
from app.core.models import SafetyConstraints
from tests.cbba.helpers import drone, point

OLD, NEW = {"m": 1.0, "n": 1.0}, {"m": 5.0, "n": 5.0}


def act(z_k, y_k, z_i, y_i, s_k=None, s_i=None):
    return decide_action("i", "k", z_k, y_k, z_i, y_i, s_k or {}, s_i or {})


# --- sender k thinks k won -----------------------------------------------

@pytest.mark.parametrize(
    "y_k,y_i,expected",
    [(10.0, 5.0, UPDATE), (5.0, 10.0, LEAVE)],
)
def test_k_says_k__i_says_i__higher_bid_wins(y_k, y_i, expected):
    assert act("k", y_k, "i", y_i) == expected


def test_k_says_k__equal_bids_go_to_lower_id():
    # "i" < "k", so the receiver keeps the point on a tie.
    assert act("k", 7.0, "i", 7.0) == LEAVE


def test_k_says_k__i_says_k__update():
    assert act("k", 3.0, "k", 9.0) == UPDATE


def test_k_says_k__i_says_nobody__update():
    assert act("k", 3.0, None, 0.0) == UPDATE


def test_k_says_k__i_says_m__update_if_k_has_newer_news_of_m():
    assert act("k", 1.0, "m", 9.0, s_k=NEW, s_i=OLD) == UPDATE


def test_k_says_k__i_says_m__update_if_k_bids_higher():
    assert act("k", 9.0, "m", 1.0, s_k=OLD, s_i=NEW) == UPDATE


def test_k_says_k__i_says_m__otherwise_leave():
    assert act("k", 1.0, "m", 9.0, s_k=OLD, s_i=NEW) == LEAVE


# --- sender k thinks the receiver i won ----------------------------------

def test_k_says_i__i_says_i__leave():
    assert act("i", 5.0, "i", 5.0) == LEAVE


def test_k_says_i__i_says_k__reset():
    assert act("i", 5.0, "k", 5.0) == RESET


def test_k_says_i__i_says_m__reset_only_if_k_newer_about_m():
    assert act("i", 5.0, "m", 5.0, s_k=NEW, s_i=OLD) == RESET
    assert act("i", 5.0, "m", 5.0, s_k=OLD, s_i=NEW) == LEAVE


def test_k_says_i__i_says_nobody__leave():
    assert act("i", 5.0, None, 0.0) == LEAVE


# --- sender k thinks a third drone m won ---------------------------------

def test_k_says_m__i_says_i__update_only_if_newer_and_higher():
    assert act("m", 9.0, "i", 1.0, s_k=NEW, s_i=OLD) == UPDATE
    assert act("m", 9.0, "i", 1.0, s_k=OLD, s_i=NEW) == LEAVE
    assert act("m", 1.0, "i", 9.0, s_k=NEW, s_i=OLD) == LEAVE


def test_k_says_m__i_says_k__update_if_newer_else_reset():
    assert act("m", 5.0, "k", 5.0, s_k=NEW, s_i=OLD) == UPDATE
    assert act("m", 5.0, "k", 5.0, s_k=OLD, s_i=NEW) == RESET


def test_k_says_m__i_says_m__update_only_if_newer():
    assert act("m", 5.0, "m", 4.0, s_k=NEW, s_i=OLD) == UPDATE
    assert act("m", 5.0, "m", 4.0, s_k=OLD, s_i=NEW) == LEAVE


def test_k_says_m__i_says_n__update_if_newer_about_both():
    assert act("m", 1.0, "n", 9.0, s_k={"m": 5, "n": 5}, s_i={"m": 1, "n": 1}) == UPDATE


def test_k_says_m__i_says_n__update_if_newer_about_m_and_higher():
    assert act("m", 9.0, "n", 1.0, s_k={"m": 5, "n": 1}, s_i={"m": 1, "n": 5}) == UPDATE


def test_k_says_m__i_says_n__reset_if_k_newer_about_n_but_i_newer_about_m():
    assert act("m", 1.0, "n", 9.0, s_k={"m": 1, "n": 5}, s_i={"m": 5, "n": 1}) == RESET


def test_k_says_m__i_says_n__otherwise_leave():
    assert act("m", 1.0, "n", 9.0, s_k={"m": 5, "n": 1}, s_i={"m": 1, "n": 5}) == LEAVE


def test_k_says_m__i_says_nobody__update_only_if_newer():
    assert act("m", 5.0, None, 0.0, s_k=NEW, s_i=OLD) == UPDATE
    assert act("m", 5.0, None, 0.0, s_k=OLD, s_i=NEW) == LEAVE


# --- sender k thinks nobody won -------------------------------------------

def test_k_says_nobody__i_says_i__leave():
    assert act(None, 0.0, "i", 5.0) == LEAVE


def test_k_says_nobody__i_says_k__update():
    assert act(None, 0.0, "k", 5.0) == UPDATE


def test_k_says_nobody__i_says_m__update_only_if_newer():
    assert act(None, 0.0, "m", 5.0, s_k=NEW, s_i=OLD) == UPDATE
    assert act(None, 0.0, "m", 5.0, s_k=OLD, s_i=NEW) == LEAVE


def test_k_says_nobody__i_says_nobody__leave():
    assert act(None, 0.0, None, 0.0) == LEAVE


# --- bundle release (eq. 6) and timestamps (eq. 5) ------------------------

def _agent():
    pts = [point(f"p{j}", 37.4270 + j * 0.0005, -122.1690) for j in range(4)]
    a = CbbaAgent(drone("i"), pts, SafetyConstraints(), CbbaConfig(), altitude_m=20.0)
    a.build_bundle()
    return a


def test_outbid_point_and_everything_added_after_it_are_released():
    a = _agent()
    assert len(a.bundle) == 4
    first, lost, later = a.bundle[0], a.bundle[1], a.bundle[2:]
    msg = CbbaMessage(
        sender_id="k",
        round=1,
        winning_bids={**a.y, lost: a.y[lost] + 50},
        winners={**a.z, lost: "k"},
        timestamps={"k": 1.0},
    )
    a.consensus([msg], round_no=1)
    assert a.bundle == [first]
    assert a.z[lost] == "k"
    for pid in later:  # own claims withdrawn so others may take them
        assert a.z[pid] is None and a.y[pid] == 0.0
    assert set(a.path) == set(a.bundle)


def test_timestamps_direct_news_is_this_round_relayed_news_keeps_its_age():
    a = _agent()
    msg = CbbaMessage(sender_id="k", round=7, winning_bids=dict(a.y), winners=dict(a.z), timestamps={"k": 7.0, "m": 3.0})
    a.consensus([msg], round_no=7)
    assert a.s["k"] == 7.0
    assert a.s["m"] == 3.0


def test_published_bids_never_increase_along_a_bundle_even_when_true_gains_do():
    """Bid warping. In this layout p1 and p2 are close together, so after
    p2 is added the TRUE gain of p1 (93.6) is higher than p2's bid (90.8)
    -- the score is not diminishing-gain here. The published bid must
    still be capped, or drones can trade a point back and forth forever."""
    pts = [
        point("p0", 37.4303, -122.1684),
        point("p1", 37.4262, -122.1700),
        point("p2", 37.4266, -122.1694),
        point("p3", 37.4285, -122.1678),
    ]
    a = CbbaAgent(drone("i", 37.4271, -122.1673), pts, SafetyConstraints(), CbbaConfig(), altitude_m=20.0)
    a.build_bundle()
    assert a.bundle == ["p3", "p0", "p2", "p1"]
    bids = [a.y[pid] for pid in a.bundle]
    assert all(earlier >= later for earlier, later in zip(bids, bids[1:]))

    # Warping really happened: p1's true gain, given the path before it
    # was added, is higher than the bid published for it.
    before = [a.points[pid] for pid in a.path if pid != "p1"]
    score_before = evaluate_path(a.drone, before, a.constraints, a.config, a.altitude_m).score
    true_gain, _ = best_insertion(a.drone, before, score_before, a.points["p1"], a.constraints, a.config, a.altitude_m)
    assert true_gain > a.y["p1"] + 1.0
    assert a.y["p1"] == a.y["p2"]
