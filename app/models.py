from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


# ---- Inbound (device/browser -> server) ----

class TextInput(BaseModel):
    type: Literal["text_input"]
    text: str


class PrintConfirm(BaseModel):
    """Sent by the device after the user accepts the transcription."""
    type: Literal["print_confirm"]


class PrintReject(BaseModel):
    """Sent by the device after the user rejects the transcription."""
    type: Literal["print_reject"]


# Discriminated union of every message a client may send as a text frame.
# Pydantic picks the right model from the "type" tag, so print_confirm /
# print_reject (which carry no other fields) validate cleanly.
ClientMessage = Annotated[
    Union[TextInput, PrintConfirm, PrintReject],
    Field(discriminator="type"),
]
client_message_adapter: TypeAdapter = TypeAdapter(ClientMessage)


# ---- Outbound (server -> device/browser) ----

class ProgressMessage(BaseModel):
    type: Literal["progress"] = "progress"
    stage: str
    message: str


class TranscriptionMessage(BaseModel):
    type: Literal["transcription"] = "transcription"
    text: str


class ResultMessage(BaseModel):
    # Field order is the wire order: {type, raw_mono, width, height}.
    type: Literal["result"] = "result"
    raw_mono: str
    width: int = 384
    height: int


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    stage: str
    message: str
