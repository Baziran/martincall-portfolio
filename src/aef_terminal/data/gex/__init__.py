"""Provider-independent GEX domain package.

Import concrete owners from their modules.  Keeping package initialization empty is
intentional: provider adapters may import GEX contracts while their own acquisition
modules are still initializing.
"""

from __future__ import annotations
