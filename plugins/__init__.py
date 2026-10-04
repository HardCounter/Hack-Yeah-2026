"""Trusted Layer 1 interception plugins (not consume-plane processors)."""
from plugins.pattern_match import PatternMatch
from plugins.velocity_guard import VelocityGuard

# Code-owned list: adding a plugin requires a reviewed import, never an API-supplied path.
PLUGINS = [PatternMatch, VelocityGuard]
