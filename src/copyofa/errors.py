"""Exception hierarchy and exit codes."""

EXIT_PASS = 0
EXIT_ACCEPTED = 10
EXIT_FAIL = 20
EXIT_REJECTED = 21
EXIT_ENVIRONMENT = 22
EXIT_INTEGRITY = 30
EXIT_UNCOMMITTED = 31
EXIT_USAGE = 2
EXIT_INTERNAL = 70


class CoaError(Exception):
    exit_code: int = EXIT_INTERNAL

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class UsageError(CoaError):
    exit_code = EXIT_USAGE


class MediumRejected(CoaError):
    exit_code = EXIT_REJECTED


class EnvironmentFailure(CoaError):
    exit_code = EXIT_ENVIRONMENT


class IntegrityFailure(CoaError):
    exit_code = EXIT_INTEGRITY
