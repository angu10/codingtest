from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from demo_app.app import app
from demo_app.data import reset_members


@pytest.fixture(autouse=True)
def reset_demo_state() -> None:
    reset_members()
    app.state.detail_delay_seconds = 0.01


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_happy_path_reaches_review_and_create(client: TestClient) -> None:
    search = client.post("/lookup/r7q9x", data={"member_id": "58431"})
    assert search.status_code == 200
    assert "Member Detail" in search.text
    assert 'name="account-frame"' in search.text

    pane = client.get("/account-pane/58431")
    assert pane.status_code == 200
    assert "Savings Account" in pane.text
    assert "Open Sub-Account" in pane.text

    form = client.get("/service/x4m9p/58431/sav-42")
    assert "Continue to Review" in form.text
    review = client.post(
        "/service/x4m9p/58431/sav-42/review",
        data={
            "account_type": "savings",
            "nickname": "Rainy day",
            "opening_deposit": "25.00",
        },
    )
    assert review.status_code == 200
    assert "Review Sub-Account" in review.text
    assert "requires confirmation" in review.text

    created = client.post(
        "/service/q2c8v/create",
        data={
            "member_id": "58431",
            "account_id": "sav-42",
            "account_type": "savings",
            "nickname": "Rainy day",
            "opening_deposit": "25.00",
        },
    )
    assert created.status_code == 200
    assert "Sub-Account Created" in created.text


@pytest.mark.parametrize(
    ("member_id", "path", "status", "marker"),
    [
        ("99999", "/lookup/r7q9x", 404, "No member matches the reference you entered."),
        ("55501", "/account-pane/55501", 403, "restricted and cannot be serviced"),
        ("55502", "/member/55502", 200, "Member Detail"),
        ("55503", "/account-pane/55503", 401, "Sign in again to restore"),
        ("55504", "/account-pane/55504", 200, "Scheduled maintenance"),
        ("55506", "/account-pane/55506", 403, "lacks the servicing entitlement"),
    ],
)
def test_fault_cases_are_reachable(
    client: TestClient, member_id: str, path: str, status: int, marker: str
) -> None:
    if path == "/lookup/r7q9x":
        response = client.post(path, data={"member_id": member_id})
    else:
        response = client.get(path)
    assert response.status_code == status
    assert marker in response.text


def test_maintenance_modal_can_be_dismissed(client: TestClient) -> None:
    blocked = client.get("/account-pane/55504")
    assert "Scheduled maintenance" in blocked.text
    resumed = client.get("/account-pane/55504?maintenance_dismissed=true")
    assert "Scheduled maintenance" not in resumed.text
    assert "Open Sub-Account" in resumed.text


def test_ambiguous_create_lands_before_500_and_is_read_only_reconcilable(
    client: TestClient,
) -> None:
    response = client.post(
        "/service/q2c8v/create",
        data={
            "member_id": "55505",
            "account_id": "sav-55",
            "account_type": "savings",
            "nickname": "Probe me",
            "opening_deposit": "0.00",
        },
    )
    assert response.status_code == 500

    probe = client.get("/api/members/55505/sub-accounts")
    assert probe.status_code == 200
    assert probe.json()["sub_accounts"] == [
        {
            "account_type": "savings",
            "nickname": "Probe me",
            "opening_deposit": "0.00",
        }
    ]

