"""Lambda shim. The real handler is `attestor.agent.handler`, which is tested.

This file exists because a Lambda needs a module at the root of its package and a
`make package` step vendors `attestor` and its dependencies beside it. Keeping the logic in
the library rather than here is what lets `tests/agent/` exercise the entry point directly.
"""

from attestor.agent.handler import invoke

__all__ = ["invoke"]
