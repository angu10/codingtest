import pytest

from interface_cua.handoff.lease import Controller, LeaseState, LeaseViolation, SessionLease


def test_session_lease_has_exactly_one_controller_through_handoff() -> None:
    lease = SessionLease()
    lease.assert_mutation_allowed(Controller.AUTOMATION)
    with pytest.raises(LeaseViolation):
        lease.assert_mutation_allowed(Controller.HUMAN)

    lease.request_pause()
    lease.mark_automation_paused()
    lease.grant_human_control()
    assert lease.controller == Controller.HUMAN
    assert lease.state == LeaseState.HUMAN_CONTROL
    lease.assert_mutation_allowed(Controller.HUMAN)

    lease.release_human_control()
    lease.begin_resume_validation()
    assert lease.controller == Controller.AUTOMATION
    lease.resume()
    lease.assert_mutation_allowed(Controller.AUTOMATION)

