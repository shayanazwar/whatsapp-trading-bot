"""Live execution is deliberately build-locked; this module cannot submit orders."""
from __future__ import annotations


class LiveExecutionDisabled(RuntimeError):
    pass


LIVE_EXECUTION_BUILD_RELEASED = False


class LiveExecutionAdapter:
    def __init__(self, *, enabled: bool = False, build_released: bool = False) -> None:
        self.enabled = enabled
        self.build_released = build_released

    async def submit_order(self, *_: object, **__: object) -> None:
        # Intentionally no HTTP client, signing code, order endpoint, or credentials in this build.
        if not self.build_released or not LIVE_EXECUTION_BUILD_RELEASED or not self.enabled:
            raise LiveExecutionDisabled("Live execution is build-locked and disabled. No order was submitted.")
        raise LiveExecutionDisabled("Execution adapter has no submit implementation in this release.")
