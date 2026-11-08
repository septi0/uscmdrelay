from pydantic import BaseModel, ConfigDict, Field

class LimitsModel(BaseModel):
    max_connections: int = Field(256, ge=1)
    max_connections_per_ip: int = Field(32, ge=1)
    max_message_size: int = Field(2 * 1024 * 1024, ge=1)
    max_field_size: int = Field(4 * 1024, ge=1)
    max_output_size: int = Field(1024 * 1024, ge=1)
    max_running: int = Field(16, ge=1)
    max_queued: int = Field(64, ge=1)

    model_config = ConfigDict(extra='forbid')
