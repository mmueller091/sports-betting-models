"""Baseball stays paused until explicitly re-enabled by the owner."""
import os


def mlb_enabled():
    return os.getenv('ENABLE_MLB', 'false').lower() in ('true', '1', 'yes')
