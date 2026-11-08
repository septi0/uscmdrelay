import re
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from uscmdrelay.models.command import CommandModel, HookModel
from uscmdrelay.models.common import NAME_PATTERN
from uscmdrelay.models.key import KeyModel
from uscmdrelay.models.limits import LimitsModel
from uscmdrelay.models.listener import ListenersModel

class UsCmdRelayModel(BaseModel):
    listeners: ListenersModel
    pid_file: str | None = None
    shutdown_timeout: int = Field(30, ge=0)
    limits: LimitsModel = LimitsModel()
    on_failure: HookModel | None = None
    keys: list[KeyModel] = []
    clusters: dict[str, dict[str, CommandModel]] = {}

    model_config = ConfigDict(extra='forbid')

    # a yaml section with no content is read as null
    @field_validator('keys', mode='before')
    @classmethod
    def empty_keys(cls, keys):
        return [] if keys is None else keys

    @field_validator('clusters', mode='before')
    @classmethod
    def empty_clusters(cls, clusters):
        if clusters is None:
            return {}

        if not isinstance(clusters, dict):
            return clusters

        return {name: {} if commands is None else commands for name, commands in clusters.items()}

    @model_validator(mode='after')
    def validate_after(self):
        for name in [*self.clusters, *[command for commands in self.clusters.values() for command in commands]]:
            if not re.fullmatch(NAME_PATTERN, name):
                raise ValueError(f'Invalid cluster or command name "{name}", only letters, digits, ".", "_" and "-" are allowed')

        for cluster, commands in self.clusters.items():
            for name, command in commands.items():
                for field_name, field in command.fields.items():
                    if field.default is not None and len(field.default) > self.limits.max_field_size:
                        raise ValueError(f'The default of field "{field_name}" in "{cluster}::{name}" is longer than limits.max_field_size')

        key_names = [key.name for key in self.keys]
        if len(key_names) != len(set(key_names)):
            raise ValueError('Key names must be unique')

        key_hashes = [key.hash for key in self.keys]
        if len(key_hashes) != len(set(key_hashes)):
            raise ValueError('Key hashes must be unique')

        for key in self.keys:
            unknown_clusters = [name for name in key.clusters if name != '*' and name not in self.clusters]
            if unknown_clusters:
                raise ValueError(f'Key "{key.name}" references inexistent clusters: {", ".join(unknown_clusters)}')

        return self
