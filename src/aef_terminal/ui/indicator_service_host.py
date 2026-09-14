from __future__ import annotations

import inspect
import logging

from aef_terminal.indicators.registry import indicator_modules
from aef_terminal.indicators.refs import resolve_ref
from aef_terminal.indicators.service_contract import (
    IndicatorServiceContext,
    IndicatorServiceContribution,
    IndicatorServiceTask,
)
from aef_terminal.ui.services.background_tasks import BackgroundTaskRegistry


_CONTRIBUTIONS: dict[str, IndicatorServiceContribution] = {}
_ACTIVE_TASK_NAMES: tuple[str, ...] = ()
_LOGGER = logging.getLogger(__name__)


def configure_indicator_services(context: IndicatorServiceContext) -> None:
    contributions: dict[str, IndicatorServiceContribution] = {}
    task_owners: dict[str, str] = {}
    for module in indicator_modules():
        if not module.service_ref:
            continue
        contribution = resolve_ref(module.service_ref)(context)
        if not isinstance(contribution, IndicatorServiceContribution):
            raise TypeError(
                f"Indicator service {module.id} must return IndicatorServiceContribution"
            )
        if contribution.shutdown is not None and not callable(contribution.shutdown):
            raise TypeError(f"Indicator service {module.id} shutdown must be callable")
        for task in contribution.tasks:
            if not isinstance(task, IndicatorServiceTask):
                raise TypeError(f"Indicator service {module.id} contains an invalid task")
            if not isinstance(task.name, str) or not task.name or task.name != task.name.strip():
                raise ValueError(f"Indicator service {module.id} task name is invalid")
            if not callable(task.factory):
                raise TypeError(
                    f"Indicator service {module.id} task {task.name} factory must be callable"
                )
            name = task.name
            owner = task_owners.get(name)
            if owner is not None:
                raise ValueError(
                    f"Indicator service task {name} is claimed by {owner} and {module.id}"
                )
            task_owners[name] = module.id
        contributions[module.id] = contribution

    global _CONTRIBUTIONS, _ACTIVE_TASK_NAMES, _LOGGER
    _CONTRIBUTIONS = contributions
    _ACTIVE_TASK_NAMES = ()
    _LOGGER = context.logger


def start_indicator_service_tasks(registry: BackgroundTaskRegistry) -> None:
    active_names: list[str] = []
    for contribution in _CONTRIBUTIONS.values():
        for task in contribution.tasks:
            registry.ensure(
                task.name,
                task.factory,
                restart_delay_seconds=task.restart_delay_seconds,
                restart_window_seconds=task.restart_window_seconds,
                max_restarts_per_window=task.max_restarts_per_window,
            )
            active_names.append(task.name)

    global _ACTIVE_TASK_NAMES
    _ACTIVE_TASK_NAMES = tuple(active_names)


async def stop_indicator_services(
    registry: BackgroundTaskRegistry,
) -> None:
    global _ACTIVE_TASK_NAMES
    active_task_names = _ACTIVE_TASK_NAMES
    _ACTIVE_TASK_NAMES = ()
    await registry.cancel_all(list(active_task_names))
    for indicator_id, contribution in reversed(tuple(_CONTRIBUTIONS.items())):
        if contribution.shutdown is None:
            continue
        try:
            result = contribution.shutdown()
            if not inspect.isawaitable(result):
                raise TypeError("Indicator service shutdown must return an awaitable")
            await result
        except Exception as exc:
            _LOGGER.warning(
                "Indicator service shutdown failed: %s: %s",
                indicator_id,
                exc,
            )
