from pydantic import BaseModel, ConfigDict, Field, model_validator

class ListenerModel(BaseModel):
    host: str = '127.0.0.1'
    port: int = Field(ge=1, le=65535)

    model_config = ConfigDict(extra='forbid')

class ListenersModel(BaseModel):
    tcp: ListenerModel | None = None
    http: ListenerModel | None = None

    model_config = ConfigDict(extra='forbid')

    @model_validator(mode='after')
    def validate_after(self):
        if not self.tcp and not self.http:
            raise ValueError('At least one listener must be configured')

        if self.tcp and self.http and self.tcp.port == self.http.port:
            if self.tcp.host == self.http.host or {self.tcp.host, self.http.host} & {'0.0.0.0', '::'}:
                raise ValueError('The TCP and HTTP listeners cannot use the same address and port')

        return self
