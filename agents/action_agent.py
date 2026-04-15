"""
ActionAgent
-----------
Executes approved actions from a RiskAssessment.
Each action maps to a handler that calls the appropriate downstream service
(notifications, rerouting, compliance logging, etc.).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from agents.risk_agent import RecommendedAction, RiskAssessment

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    action:     RecommendedAction
    success:    bool
    executed_at: datetime
    details:    str
    payload:    Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "action":      self.action.value,
            "success":     self.success,
            "executed_at": self.executed_at.isoformat(),
            "details":     self.details,
        }


class ActionAgent:
    """
    Dispatches approved actions and collects results.
    Handlers are registered via register_handler() so the agent is
    decoupled from concrete notification/routing implementations.
    """

    def __init__(self):
        self._handlers: Dict[RecommendedAction, Callable] = {}
        self._register_defaults()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_handler(
        self,
        action: RecommendedAction,
        handler: Callable[[RiskAssessment], Dict[str, Any]],
    ) -> None:
        self._handlers[action] = handler

    def execute(
        self,
        assessment: RiskAssessment,
        approved_actions: List[RecommendedAction],
    ) -> List[ActionResult]:
        results: List[ActionResult] = []
        for action in approved_actions:
            result = self._dispatch(action, assessment)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, action: RecommendedAction, assessment: RiskAssessment) -> ActionResult:
        handler = self._handlers.get(action)
        now = datetime.now(timezone.utc)

        if handler is None:
            logger.warning("No handler registered for action %s", action.value)
            return ActionResult(
                action      = action,
                success     = False,
                executed_at = now,
                details     = f"No handler registered for {action.value}",
            )

        try:
            payload = handler(assessment)
            logger.info("[%s] Action %s executed successfully", assessment.shipment_id, action.value)
            return ActionResult(
                action      = action,
                success     = True,
                executed_at = now,
                details     = f"Action {action.value} executed.",
                payload     = payload,
            )
        except Exception as exc:
            logger.error("[%s] Action %s FAILED: %s", assessment.shipment_id, action.value, exc)
            return ActionResult(
                action      = action,
                success     = False,
                executed_at = now,
                details     = f"Action {action.value} failed: {exc}",
            )

    # ------------------------------------------------------------------
    # Default no-op handlers (replaced by real implementations at runtime)
    # ------------------------------------------------------------------

    def _register_defaults(self) -> None:
        for action in RecommendedAction:
            self._handlers[action] = self._make_default_handler(action)

    @staticmethod
    def _make_default_handler(action: RecommendedAction) -> Callable:
        def _handler(assessment: RiskAssessment) -> Dict[str, Any]:
            logger.debug("Default handler invoked for %s on shipment %s",
                         action.value, assessment.shipment_id)
            return {"status": "noop", "action": action.value,
                    "shipment_id": assessment.shipment_id}
        return _handler
