from enum import IntEnum
from pydantic import BaseModel

__all__ = ['ResponseCode', 'ResponseModel']

class ResponseCode(IntEnum):
    OK = 0
    AUTH_FAILED = 1000
    UNKNOWN_CLUSTER = 2000
    UNKNOWN_COMMAND = 2001
    INVALID_FIELD = 2002
    WRONG_FORMAT = 2003
    BAD_MESSAGE = 2099
    COMMAND_FAILED = 3001
    COMMAND_TIMEOUT = 3002
    BUSY = 3003
    SERVER_ERROR = 9999

class ResponseModel(BaseModel):
    status: str
    message: str
    code: int
    exit_code: int | None = None
    run_id: str | None = None

    @classmethod
    def error(cls, message: str, code: int, *, exit_code: int | None = None, run_id: str | None = None) -> 'ResponseModel':
        return cls(status='error', message=message, code=code, exit_code=exit_code, run_id=run_id)

    def to_dict(self) -> dict:
        return self.model_dump(exclude_none=True)
