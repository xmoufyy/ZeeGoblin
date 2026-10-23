"""Domain errors. Their messages are written to be shown directly to Discord users."""


class ZeeGoblinError(Exception):
    """Base class for expected, user-facing failures."""


class InputError(ZeeGoblinError):
    pass


class PermissionDenied(ZeeGoblinError):
    pass


class NotFound(ZeeGoblinError):
    pass


class InvalidTransition(ZeeGoblinError):
    pass


class Conflict(ZeeGoblinError):
    pass
