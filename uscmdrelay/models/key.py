from pydantic import BaseModel, ConfigDict, Field, IPvAnyNetwork, field_validator
from uscmdrelay.models.common import NAME_PATTERN

class KeyModel(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    hash: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    clusters: list[str] = Field(min_length=1)
    allowed_ips: list[IPvAnyNetwork] | None = Field(None, min_length=1)

    model_config = ConfigDict(extra='forbid')

    @field_validator('clusters', mode='after')
    @classmethod
    def validate_clusters(cls, clusters):
        if '*' in clusters and len(clusters) > 1:
            raise ValueError('When "*" is used, no other clusters can be listed')

        return clusters

    @property
    def digest(self) -> str:
        return self.hash.removeprefix('sha256:')
