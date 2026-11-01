"""Background execution for long-running portfolio AI requests."""

from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable
from functools import partial
from typing import Any

from services.ai import ask_llm_agent

_ANALYSIS_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="portfolio-analysis",
)


def submit_analysis_task(
    function: Callable[..., Any],
    *arguments: Any,
    **keyword_arguments: Any,
) -> Future:
    task = partial(function, *arguments, **keyword_arguments)
    return _ANALYSIS_EXECUTOR.submit(task)


def submit_portfolio_analysis(**arguments: Any) -> Future:
    return submit_analysis_task(ask_llm_agent, **arguments)
