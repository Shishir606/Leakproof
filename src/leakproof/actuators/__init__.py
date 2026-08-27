"""Guarded, idempotent action execution."""

from leakproof.actuators.base import Actuator, ActuatorRequest, ActuatorResult
from leakproof.actuators.executor import ExecutionResult, due_action_ids, execute_action
from leakproof.actuators.simulator import SimulatorActuator, SimulatorActuatorRegistry

__all__ = [
    "Actuator",
    "ActuatorRequest",
    "ActuatorResult",
    "ExecutionResult",
    "SimulatorActuator",
    "SimulatorActuatorRegistry",
    "due_action_ids",
    "execute_action",
]
