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


def test_member_detail_renders_regulated_data(client: TestClient) -> None:
    """A servicing console with no PII on screen would not exercise the controls that guard it.

    Every SSN in the seed data is in the 666-xx-xxxx block, which the SSA has never issued.
    """

    response = client.get("/member/58431")
    assert response.status_code == 200
    assert "666-19-4472" in response.text
    assert "1979-11-02" in response.text
    # The member reference stays masked where it is *displayed*. It still appears in the iframe
    # URL, which is the app being an app — masking a route would break the page.
    assert "•••8431" in response.text
    assert ">58431<" not in response.text


@pytest.mark.parametrize(
    ("deposit", "message"),
    [
        ("-5.00", "Opening deposit cannot be negative."),
        ("not-a-number", "Opening deposit must be an amount"),
    ],
)
def test_a_bad_deposit_re_renders_the_form_instead_of_raising(
    client: TestClient, deposit: str, message: str
) -> None:
    """The form comes back with a message beside the field, not a framework error page."""

    response = client.post(
        "/service/x4m9p/58431/sav-42/review",
        data={
            "account_type": "savings",
            "nickname": "Rainy day",
            "opening_deposit": deposit,
        },
    )
    assert response.status_code == 422
    assert message in response.text
    assert "Open Sub-Account" in response.text
    assert "Review Sub-Account" not in response.text
    # A real form re-render keeps what the operator typed.
    assert 'value="Rainy day"' in response.text
    assert f'value="{deposit}"' in response.text


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

