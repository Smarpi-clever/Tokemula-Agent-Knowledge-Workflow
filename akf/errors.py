"""Shared error type; importing it does not initialize the engine."""


class WorkflowError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
