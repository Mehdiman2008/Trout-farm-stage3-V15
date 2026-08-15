"""Focused regression checks for Pond Details quick events only."""
from datetime import date

from app import Engine, _add_txn


def _setup(tmp_path):
    E = Engine(str(tmp_path / "pond_quick.db"))
    day = date.today().isoformat()
    cid = "POND-QUICK-TEST"
    E.db.add_txn("egg_purchase", day, cohort_id=cid, quantity=1000,
                 unit_price=6000, amount=6_000_000, data_source="actual",
                 note="pond quick test")
    E.db.add_txn("transfer", day, cohort_id=cid, pond_id="TROUGH",
                 to_pond_id="P01", quantity=800, data_source="actual",
                 note="pond quick test setup")
    return E, day, cid


def _state(E, cid):
    return E.ctx(date.today())[2].cohorts[cid]


def test_transfer_between_two_ponds(tmp_path):
    E, day, cid = _setup(tmp_path)
    _add_txn(E, {"txn_type": "transfer", "txn_date": day, "cohort_id": cid,
                 "pond_id": "P01", "to_pond_id": "P02", "quantity": 120,
                 "payload": {"entry": "pond_panel"}})
    c = _state(E, cid)
    assert c.alloc["P01"] == 680
    assert c.alloc["P02"] == 120


def test_mortality_and_sale_from_same_pond(tmp_path):
    E, day, cid = _setup(tmp_path)
    _add_txn(E, {"txn_type": "mortality", "txn_date": day, "cohort_id": cid,
                 "pond_id": "P01", "quantity": 50,
                 "payload": {"entry": "pond_panel"}})
    _add_txn(E, {"txn_type": "sale", "txn_date": day, "cohort_id": cid,
                 "pond_id": "P01", "quantity": 100, "weight_g": 5,
                 "payload": {"entry": "pond_panel", "price_pending": True}})
    c = _state(E, cid)
    assert c.alloc["P01"] == 650
    assert c.recorded_mortality == 50
    assert c.sold_count == 100


def test_other_adjustment_increase_and_decrease(tmp_path):
    E, day, cid = _setup(tmp_path)
    _add_txn(E, {"txn_type": "count_observation", "txn_date": day,
                 "cohort_id": cid, "pond_id": "P01", "quantity": 825,
                 "note": "سایر موارد — افزایش 25 قطعه · اصلاح شمارش",
                 "payload": {"entry": "pond_panel", "pond_adjustment_delta": 25,
                             "adjustment_direction": "increase",
                             "adjustment_amount": 25,
                             "adjustment_reason": "اصلاح شمارش"}})
    c = _state(E, cid)
    assert c.alloc["P01"] == 825

    _add_txn(E, {"txn_type": "count_observation", "txn_date": day,
                 "cohort_id": cid, "pond_id": "P01", "quantity": 815,
                 "note": "سایر موارد — کاهش 10 قطعه · اصلاح شمارش",
                 "payload": {"entry": "pond_panel", "pond_adjustment_delta": -10,
                             "adjustment_direction": "decrease",
                             "adjustment_amount": 10,
                             "adjustment_reason": "اصلاح شمارش"}})
    c = _state(E, cid)
    assert c.alloc["P01"] == 815


def test_transfer_from_displayed_estimated_pond_auto_confirms_source(tmp_path):
    """A quick event must accept the same Estimated pond stock shown in the UI."""
    from datetime import timedelta
    E = Engine(str(tmp_path / "pond_estimated_transfer.db"))
    day = date.today().isoformat()
    purchase = (date.today() - timedelta(days=170)).isoformat()
    cid = "POND-ESTIMATED-TEST"
    E.db.add_txn("egg_purchase", purchase, cohort_id=cid, quantity=70_000,
                 unit_price=6000, amount=420_000_000, data_source="actual")
    st = E.ctx(date.today())[2]
    pv = next(p for p in st.pond_view()
              if any(o.get("cohort_id") == cid and o.get("basis") == "estimated"
                     for o in p.get("occupants", [])))
    src = pv["pond_id"]
    shown = next(o["count"] for o in pv["occupants"]
                 if o.get("cohort_id") == cid and o.get("basis") == "estimated")
    dst = next(p["pond_id"] for p in E.db.ponds() if p["pond_id"] != src)

    _add_txn(E, {"txn_type": "transfer", "txn_date": day, "cohort_id": cid,
                 "pond_id": src, "to_pond_id": dst, "quantity": 1,
                 "payload": {"entry": "pond_panel"}})

    c = _state(E, cid)
    assert c.alloc.get(src, 0) == shown - 1
    assert c.alloc.get(dst, 0) >= 1
    rows = E.db.q("SELECT * FROM transactions WHERE cohort_id=? AND txn_type='transfer' "
                  "ORDER BY id", (cid,))
    assert len(rows) == 2  # auto-confirm Estimated allocation + requested move
