"""11.x BOOM_COACH — /coach/*."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from newboom.boom.schemas import BoomMessage, BoomResponse

router = APIRouter(prefix="/coach", tags=["BOOM_COACH"])


# --- shared message shape ---
class CoachMessage(BaseModel):
    messageId: str = Field(default="", description="消息唯一标识")
    role: str = Field(default="ai", description="角色：user/ai")
    type: str = Field(default="text", description="消息类型：text/voice")
    content: str = Field(default="", description="文本内容")
    audioUrl: str | None = Field(default=None, description="语音URL（仅语音消息）")
    transcript: str | None = Field(default=None, description="语音转文本（仅语音消息）")
    createTime: str = Field(default="", description="创建时间")


class ConversationSummary(BaseModel):
    conversationId: str = Field(..., description="对话唯一标识")
    title: str = Field(..., description="对话标题（AI自动生成）")
    createTime: str = Field(..., description="创建时间")
    updateTime: str = Field(..., description="最后更新时间")
    messageCount: int = Field(default=0, description="消息数量")


class NewConversation(BaseModel):
    conversationId: str = Field(default="", description="对话唯一标识")
    title: str = Field(default="新对话", description="对话标题（默认 新对话）")
    createTime: str = Field(default="", description="创建时间")


class SendResult(BaseModel):
    conversationId: str = Field(default="", description="对话唯一标识")
    messageId: str = Field(default="", description="用户消息ID")
    aiResponse: CoachMessage = Field(
        default_factory=lambda d: CoachMessage(**d), description="AI回复"
    )


class WelcomeData(BaseModel):
    title: str = Field(default="", description="欢迎标题")
    message: str = Field(default="", description="欢迎语/开场白")


class VoiceUploadData(BaseModel):
    audioUrl: str = Field(default="", description="语音文件URL")
    duration: int = Field(default=0, description="语音时长（秒）")
    transcript: str = Field(default="", description="语音转文本结果")


class StreamEvent(BaseModel):
    """SSE 事件：``event: message`` 携带 content/done=false;``event: done`` 携带 done=true."""

    messageId: str = Field(..., description="消息唯一标识")
    content: str | None = Field(default=None, description="增量内容(event: message)")
    done: bool = Field(default=False, description="是否完成(event: done 时 true)")


# --- request bodies ---
class SendMessageRequest(BaseModel):
    content: str = Field(..., description="消息内容")
    type: str = Field(..., description="消息类型:text/voice")
    conversationId: str | None = Field(
        default=None, description="对话ID,不传则创建新对话"
    )
    audioUrl: str | None = Field(default=None, description="语音URL(仅语音消息）")


class InterruptRequest(BaseModel):
    conversationId: str = Field(..., description="对话唯一标识")


class FeedbackRequest(BaseModel):
    messageId: str = Field(..., description="消息唯一标识")
    feedback: str = Field(..., description="反馈类型:like/dislike")


@router.get("/conversations", response_model=BoomResponse[list[ConversationSummary]])
async def list_conversations() -> BoomResponse[list[ConversationSummary]]:
    """11.1 获取对话历史列表.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])


@router.get("/messages", response_model=BoomResponse[list[CoachMessage]])
async def list_messages(
    conversationId: str = Query(..., description="对话唯一标识"),
) -> BoomResponse[list[CoachMessage]]:
    """11.2 获取对话消息列表.

    TODO: implement.
    """
    return BoomResponse(code=0, data=[])


@router.post("/messages", response_model=BoomResponse[SendResult])
async def send_message(body: SendMessageRequest) -> BoomResponse[SendResult]:
    """11.3 发送消息.

    TODO: implement — persist user message, generate AI reply.
    """
    return BoomResponse(code=0, data=SendResult(conversationId="", messageId=""))


@router.post("/messages/stream")
async def send_message_stream(body: SendMessageRequest) -> StreamingResponse:
    """11.4 发送消息（流式输出）— SSE.

    TODO: implement — yield ``event: message`` chunks then a final
    ``event: done`` (see :class:`StreamEvent`).
    """

    async def events() -> AsyncGenerator[str, None]:
        # TODO: emit server-sent events here.
        if False:  # pragma: no cover - keeps the async-generator type
            yield ""

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/conversations", response_model=BoomResponse[NewConversation])
async def create_conversation() -> BoomResponse[NewConversation]:
    """11.5 新建对话.

    TODO: implement.
    """
    return BoomResponse(
        code=0, data=NewConversation(conversationId="", title="", createTime="")
    )


@router.delete("/conversations/{conversationId}", response_model=BoomMessage)
async def delete_conversation(conversationId: str) -> BoomMessage:
    """11.6 删除对话.

    TODO: implement.
    """
    return BoomMessage(code=0, msg="")


@router.delete("/messages/{messageId}", response_model=BoomMessage)
async def delete_message(messageId: str) -> BoomMessage:
    """11.7 删除消息.

    TODO: implement.
    """
    return BoomMessage(code=0, msg="")


@router.get("/welcome", response_model=BoomResponse[WelcomeData])
async def get_welcome() -> BoomResponse[WelcomeData]:
    """11.8 获取欢迎语.

    TODO: implement.
    """
    return BoomResponse(code=0, data=WelcomeData(title="", message=""))


@router.post("/voice", response_model=BoomResponse[VoiceUploadData])
async def upload_voice(
    file: UploadFile = File(..., description="语音文件(m4a/wav/mp3)"),
    conversationId: str | None = None,
) -> BoomResponse[VoiceUploadData]:
    """11.9 上传语音.

    TODO: implement — store file, run ASR, return URL/duration/transcript.
    """
    return BoomResponse(
        code=0,
        data=VoiceUploadData(audioUrl="", duration=0, transcript=""),
    )


@router.post("/interrupt", response_model=BoomMessage)
async def interrupt_stream(body: InterruptRequest) -> BoomMessage:
    """11.10 中断流式输出.

    TODO: implement.
    """
    return BoomMessage(code=0, msg="")


@router.post("/feedback", response_model=BoomMessage)
async def message_feedback(body: FeedbackRequest) -> BoomMessage:
    """11.11 消息反馈.

    TODO: implement.
    """
    return BoomMessage(code=0, msg="")
