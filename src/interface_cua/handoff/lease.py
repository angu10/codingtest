"""Single-controller session lease state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Controller(StrEnum):
    AUTOMATION = "AUTOMATION"
    HUMAN = "HUMAN"


class LeaseState(StrEnum):
    AUTOMATION_ACTIVE = "AUTOMATION_ACTIVE"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    AUTOMATION_PAUSED = "AUTOMATION_PAUSED"
    HUMAN_CONTROL = "HUMAN_CONTROL"
    HUMAN_RELEASED = "HUMAN_RELEASED"
    RESUME_VALIDATION = "RESUME_VALIDATION"
    ABORT = "ABORT"


class LeaseViolation(RuntimeError):
    pass


@dataclass(slots=True)
class SessionLease:
    state: LeaseState = LeaseState.AUTOMATION_ACTIVE
    controller: Controller = Controller.AUTOMATION

    def assert_mutation_allowed(self, caller: Controller) -> None:
        if caller != self.controller:
            raise LeaseViolation(
                f"{caller.value} cannot mutate a session controlled by {self.controller.value}"
            )
        if self.state not in {LeaseState.AUTOMATION_ACTIVE, LeaseState.HUMAN_CONTROL}:
            raise LeaseViolation(f"mutations are not allowed while lease is {self.state.value}")

    def request_pause(self) -> None:
        self._transition(LeaseState.AUTOMATION_ACTIVE, LeaseState.PAUSE_REQUESTED)

    def mark_automation_paused(self) -> None:
        self._transition(LeaseState.PAUSE_REQUESTED, LeaseState.AUTOMATION_PAUSED)

    def grant_human_control(self) -> None:
        self._transition(LeaseState.AUTOMATION_PAUSED, LeaseState.HUMAN_CONTROL)
        self.controller = Controller.HUMAN

    def release_human_control(self) -> None:
        self._transition(LeaseState.HUMAN_CONTROL, LeaseState.HUMAN_RELEASED)

    def begin_resume_validation(self) -> None:
        self._transition(LeaseState.HUMAN_RELEASED, LeaseState.RESUME_VALIDATION)
        self.controller = Controller.AUTOMATION

    def resume(self) -> None:
        self._transition(LeaseState.RESUME_VALIDATION, LeaseState.AUTOMATION_ACTIVE)

    def abort(self) -> None:
        if self.state != LeaseState.RESUME_VALIDATION:
            raise LeaseViolation(f"cannot abort from {self.state.value}")
        self.state = LeaseState.ABORT

    def _transition(self, expected: LeaseState, target: LeaseState) -> None:
        if self.state != expected:
            raise LeaseViolation(f"expected {expected.value}, found {self.state.value}")
        self.state = target

