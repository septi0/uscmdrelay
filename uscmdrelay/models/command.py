import os
import re
import shlex
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

__all__ = ['ExecModel', 'HookModel', 'FieldModel', 'ConditionModel', 'CommandModel', 'PLACEHOLDER_PATTERN', 'SAFE_VALUE_PATTERN', 'SECRET_HEADERS']

SECRET_HEADERS = ('authorization', 'proxy-authorization', 'cookie')

_FIELD_NAME = r'[A-Za-z0-9_][A-Za-z0-9_.-]*'
_SHELLS = {'sh', 'bash', 'dash', 'ash', 'zsh', 'ksh', 'csh', 'tcsh', 'fish'}
_WRAPPERS = {'env', 'sudo', 'doas', 'nice', 'ionice', 'nohup', 'setsid', 'stdbuf', 'chroot'}
_HOOK_FIELDS = {'run.command', 'run.exit_code', 'run.id'}

PLACEHOLDER_PATTERN = re.compile(r'\$(\$?)\{(' + _FIELD_NAME + r')\}')
SAFE_VALUE_PATTERN = re.compile(r'(?!.*(?:\.\.|:/))[A-Za-z0-9_][A-Za-z0-9._/:@+-]*')

class ExecModel(BaseModel):
    cmd: list[str] = Field(min_length=1)
    timeout: int = Field(60, ge=1)
    env: dict[str, str] = {}
    cwd: str | None = None
    log_output: bool = False

    model_config = ConfigDict(extra='forbid')

    @field_validator('cmd', mode='before')
    @classmethod
    def split_cmd(cls, cmd):
        return shlex.split(cmd) if isinstance(cmd, str) else cmd

    @field_validator('env', mode='after')
    @classmethod
    def expand_secrets(cls, env, info: ValidationInfo):
        secrets = (info.context or {}).get('secrets', {})

        def replace(match: re.Match) -> str:
            if match.group(1):
                return match.group(0)[1:]

            if match.group(2) not in secrets:
                raise ValueError(f'Unknown secret "{match.group(2)}", define it in secrets.env')

            return secrets[match.group(2)]

        return {name: PLACEHOLDER_PATTERN.sub(replace, value) for name, value in env.items()}

    @field_validator('cwd', mode='after')
    @classmethod
    def validate_cwd(cls, cwd):
        if cwd is not None and not os.path.isabs(cwd):
            raise ValueError('cwd must be an absolute path')

        return cwd

    @model_validator(mode='after')
    def validate_template(self):
        if self._find_placeholders(self.cmd[0]):
            raise ValueError('The program to run cannot contain a placeholder')

        if self.placeholders and any(os.path.basename(arg) in _SHELLS for arg in self.cmd):
            raise ValueError('Placeholders cannot be used in shell commands, move the logic into a script and pass the values as arguments')

        if os.path.basename(self.cmd[0]) in _WRAPPERS:
            wrapped = next((arg for arg in self.cmd[1:] if not arg.startswith('-')), '')

            if self._find_placeholders(wrapped):
                raise ValueError(f'The program started by {os.path.basename(self.cmd[0])} cannot contain a placeholder')

        return self

    @property
    def placeholders(self) -> list[str]:
        return [name for arg in self.cmd for name in self._find_placeholders(arg)]

    def render(self, values: dict[str, str]) -> list[str]:
        def replace(match: re.Match) -> str:
            return match.group(0)[1:] if match.group(1) else values[match.group(2)]

        return [PLACEHOLDER_PATTERN.sub(replace, arg) for arg in self.cmd]

    def _find_placeholders(self, arg: str) -> list[str]:
        return [match.group(2) for match in PLACEHOLDER_PATTERN.finditer(arg) if not match.group(1)]

class HookModel(ExecModel):
    @model_validator(mode='before')
    @classmethod
    def from_string(cls, values):
        return {'cmd': values} if isinstance(values, str) else values

    @model_validator(mode='after')
    def validate_hook(self):
        unknown_fields = [name for name in self.placeholders if name not in _HOOK_FIELDS]

        if unknown_fields:
            raise ValueError(f'on_failure can only use {", ".join(sorted(_HOOK_FIELDS))}, not {", ".join(unknown_fields)}')

        return self

class _PatternModel(BaseModel):
    pattern: re.Pattern | None = None

    model_config = ConfigDict(extra='forbid')

    @model_validator(mode='before')
    @classmethod
    def from_string(cls, values):
        return {'pattern': values} if isinstance(values, str) else values

class FieldModel(_PatternModel):
    default: str | None = None

    @model_validator(mode='after')
    def validate_default(self):
        if self.default is not None and not self.matches(self.default):
            raise ValueError('The default value does not match the field pattern')

        return self

    def matches(self, value: str) -> bool:
        return (self.pattern or SAFE_VALUE_PATTERN).fullmatch(value) is not None

class ConditionModel(_PatternModel):
    exists: bool | None = None

    @model_validator(mode='after')
    def validate_condition(self):
        if (self.pattern is None) == (self.exists is None):
            raise ValueError('A condition needs either a regex or "exists"')

        return self

class CommandModel(ExecModel):
    format: Literal['json', 'env'] = 'json'
    when: dict[str, ConditionModel] = {}
    fields: dict[str, FieldModel] = {}
    background: bool = False
    hide_output: bool = False
    on_failure: HookModel | None = None

    @model_validator(mode='after')
    def validate_fields(self):
        for name in [*self.required_fields, *self.when]:
            if not re.fullmatch(_FIELD_NAME, name):
                raise ValueError(f'Invalid field name "{name}"')

            if name.split('.')[0] == 'auth_key':
                raise ValueError('auth_key is never available to commands')

            if name.startswith('headers.'):
                header = name.removeprefix('headers.')

                if header != header.lower():
                    raise ValueError(f'Header names are lowercase, use "{name.lower()}"')

                if header in SECRET_HEADERS:
                    raise ValueError(f'The {header} header is never available to commands')

        return self

    @property
    def required_fields(self) -> list[str]:
        return list(dict.fromkeys([*self.placeholders, *self.fields]))
