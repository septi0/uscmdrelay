class UsCmdRelayRuntimeError(Exception):
    pass

class ConditionNotMet(Exception):
    pass

class RelayError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code
