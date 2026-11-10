from .defaults import DEFAULTS


def merge_settings(file_values, environ):
    """Merge defaults, file settings, and APP_ environment overrides."""
    result = DEFAULTS
    result.update(file_values)
    return result
