"""Mission lifecycle state machine (docs/IMPLEMENTATION_PLAN.md Phase 3):

    DRAFT -> VALIDATED -> APPROVED -> RUNNING -> COMPLETED
                                              -> FAILED
    any non-final state -> ABORTED

Every rule here is about *permission*, not flight: nothing in this
module sends a command. Phase 5's executor will call start() and only
dispatch if it succeeds.

  - validate() runs the Phase 2 safety validator. A plan with issues
    stays DRAFT (it can be replanned); it is never "partially valid".
  - approve() requires VALIDATED and records a hash of the exact plan.
  - start() requires APPROVED *and* that the plan still hashes to what
    was approved -- a plan edited after approval is refused.
  - abort() is always allowed until the mission has finished: the
    operator's abort wins over everything.
"""
from __future__ import annotations

from app.core.models import (
    ApprovalRecord,
    Event,
    ExecutionStatus,
    Geofence,
    MissionPlan,
    SafetyConstraints,
    ValidationResult,
    Waypoint,
)
from app.safety.validator import make_approval_record, plan_matches_approval, validate_mission_plan

S = ExecutionStatus
FINAL_STATES = {S.COMPLETED, S.FAILED, S.ABORTED}
ALLOWED = {
    S.DRAFT: {S.VALIDATED, S.ABORTED},
    S.VALIDATED: {S.APPROVED, S.ABORTED},
    S.APPROVED: {S.RUNNING, S.ABORTED},
    S.RUNNING: {S.COMPLETED, S.FAILED, S.ABORTED},
}


class IllegalTransition(Exception):
    pass


class PlanChangedSinceApproval(Exception):
    pass


class MissionManager:
    def __init__(self, plan: MissionPlan):
        self.plan = plan
        self.status = S.DRAFT
        self.approval: ApprovalRecord | None = None
        self.last_validation: ValidationResult | None = None
        self.events: list[Event] = [Event(event_type="MISSION_DRAFTED", detail=f"plan {plan.plan_id}")]

    def _move(self, new_status: ExecutionStatus, detail: str) -> None:
        if new_status not in ALLOWED.get(self.status, set()):
            raise IllegalTransition(f"cannot go from {self.status.value} to {new_status.value}")
        self.status = new_status
        self.events.append(Event(event_type=f"MISSION_{new_status.value}", detail=detail))

    def validate(
        self,
        geofence: Geofence,
        constraints: SafetyConstraints,
        vehicle_homes: dict[str, Waypoint],
        vehicle_battery_pct: dict[str, float],
    ) -> ValidationResult:
        if self.status != S.DRAFT:
            raise IllegalTransition(f"only a DRAFT plan can be validated (currently {self.status.value})")
        result = validate_mission_plan(self.plan, geofence, constraints, vehicle_homes, vehicle_battery_pct)
        self.last_validation = result
        if result.valid:
            self._move(S.VALIDATED, "all safety checks passed")
        else:
            self.events.append(
                Event(event_type="MISSION_VALIDATION_FAILED", detail=f"{len(result.issues)} issue(s); stays DRAFT")
            )
        return result

    def approve(self, approved_by: str) -> ApprovalRecord:
        if self.status != S.VALIDATED:
            raise IllegalTransition(f"only a VALIDATED plan can be approved (currently {self.status.value})")
        self.approval = make_approval_record(self.plan, approved_by)
        self._move(S.APPROVED, f"approved by {approved_by}, plan hash {self.approval.plan_hash[:12]}")
        return self.approval

    def start(self, plan_to_run: MissionPlan) -> None:
        """plan_to_run is the plan the executor is about to send -- it
        must be byte-for-byte (by content hash) the plan that was
        approved."""
        if self.status != S.APPROVED or self.approval is None:
            raise IllegalTransition(f"only an APPROVED plan can start (currently {self.status.value})")
        if not plan_matches_approval(plan_to_run, self.approval):
            self.events.append(Event(event_type="MISSION_START_REFUSED", detail="plan changed after approval"))
            raise PlanChangedSinceApproval("the plan was modified after it was approved; re-validate and re-approve")
        self._move(S.RUNNING, "approved plan integrity verified")

    def complete(self) -> None:
        self._move(S.COMPLETED, "all tasks finished")

    def fail(self, reason: str) -> None:
        self._move(S.FAILED, reason)

    def abort(self, reason: str = "operator abort") -> None:
        if self.status in FINAL_STATES:
            raise IllegalTransition(f"mission already finished ({self.status.value})")
        self._move(S.ABORTED, reason)
