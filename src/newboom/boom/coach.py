"""11.x BOOM_COACH — /coach/*.

The coach is device-scoped: every endpoint takes an ``address`` (device mac)
query param, resolves the :class:`Device`, and scopes its data by
``device_id``. Public conversation/message ids are the decimal string of the
model's integer primary key (the spec's ``conv001`` / ``msg001`` are
illustrative); the helpers validate device ownership so one device never
reads or mutates another's history.

AI replies (§11.3 / §11.4) go through a pluggable :class:`CoachProvider`
seam; the default :class:`DefaultCoachProvider` is a placeholder pending a
real LLM (swap it via :func:`set_provider`). Streamed replies (§11.4) are
tracked by a process-local :class:`_StreamRegistry` so §11.10 can interrupt
an in-flight stream by conversation id.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from newboom.boom.schemas import BoomMessage, BoomResponse
from newboom.database import engine, get_session
from newboom.model import Conversation, Device, Message
from newboom.storage import save_audio

router = APIRouter(prefix="/coach", tags=["BOOM_COACH"])


# --- shared message shape ---
class CoachMessage(BaseModel):
    messageId: str = Field(default="", description="消息唯一标识")
    role: str = Field(default="ai", description="角色:user/ai")
    type: str = Field(default="text", description="消息类型:text/voice")
    content: str = Field(default="", description="文本内容")
    audioUrl: str | None = Field(default=None, description="语音URL(仅语音消息)")
    transcript: str | None = Field(default=None, description="语音转文本(仅语音消息)")
    createTime: str = Field(default="", description="创建时间")


class ConversationSummary(BaseModel):
    conversationId: str = Field(..., description="对话唯一标识")
    title: str = Field(..., description="对话标题(AI自动生成)")
    createTime: str = Field(..., description="创建时间")
    updateTime: str = Field(..., description="最后更新时间")
    messageCount: int = Field(default=0, description="消息数量")


class NewConversation(BaseModel):
    conversationId: str = Field(default="", description="对话唯一标识")
    title: str = Field(default="新对话", description="对话标题(默认 新对话)")
    createTime: str = Field(default="", description="创建时间")


class SendResult(BaseModel):
    conversationId: str = Field(default="", description="对话唯一标识")
    messageId: str = Field(default="", description="用户消息ID")
    aiResponse: CoachMessage = Field(
        default_factory=CoachMessage, description="AI回复"
    )


class WelcomeData(BaseModel):
    title: str = Field(default="", description="欢迎标题")
    message: str = Field(default="", description="欢迎语/开场白")


class VoiceUploadData(BaseModel):
    audioUrl: str = Field(default="", description="语音文件URL")
    duration: int = Field(default=0, description="语音时长(秒)")
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
    audioUrl: str | None = Field(default=None, description="语音URL(仅语音消息)")


class InterruptRequest(BaseModel):
    conversationId: str = Field(..., description="对话唯一标识")


class FeedbackRequest(BaseModel):
    messageId: str = Field(..., description="消息唯一标识")
    feedback: str = Field(..., description="反馈类型:like/dislike")


# --- AI provider seam -------------------------------------------------------
@dataclass
class CoachContext:
    """Snapshot handed to a :class:`CoachProvider` for one reply.

    ``history`` is the conversation's prior turns ``(role, content)`` in order
    (including the turn just sent); ``user_message`` / ``user_type`` are that
    turn.
    """

    conversation_id: int
    device_id: int
    user_message: str
    user_type: str
    history: list[tuple[str, str]] = field(default_factory=list)


class CoachProvider:
    """Pluggable AI backend; swap the module default via :func:`set_provider`."""

    def reply(self, ctx: CoachContext) -> str:
        raise NotImplementedError

    async def stream(self, ctx: CoachContext) -> AsyncGenerator[str, None]:
        raise NotImplementedError


class DefaultCoachProvider(CoachProvider):
    """PENDING a real LLM: a placeholder that echoes a canned acknowledgement.

    ``stream`` re-chunks the same text so §11.4 has observable SSE frames
    today; a real provider overrides both methods.
    """

    def reply(self, ctx: CoachContext) -> str:
        topic = ctx.user_message or (
            "（语音消息）" if ctx.user_type == "voice" else ""
        )
        return f"[占位回复] 已收到你的消息：{topic}。"

    async def stream(self, ctx: CoachContext) -> AsyncGenerator[str, None]:
        text = self.reply(ctx)
        for i in range(0, len(text), 6):
            yield text[i:i + 6]


_provider: CoachProvider = DefaultCoachProvider()


def get_provider() -> CoachProvider:
    """Return the active coach provider (the swap point for a real LLM)."""
    return _provider


def set_provider(provider: CoachProvider) -> None:
    """Install a coach provider (a real LLM backend, or a test double)."""
    global _provider
    _provider = provider


# --- stream registry (§11.4 / §11.10) --------------------------------------
class _StreamHandle:
    def __init__(self) -> None:
        self.cancelled = asyncio.Event()


class _StreamRegistry:
    """Process-local registry of in-flight §11.4 streams keyed by conversation id."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._handles: dict[int, _StreamHandle] = {}

    async def register(self, conversation_id: int) -> _StreamHandle:
        async with self._lock:
            handle = _StreamHandle()
            self._handles[conversation_id] = handle
            return handle

    async def remove(self, conversation_id: int) -> None:
        async with self._lock:
            self._handles.pop(conversation_id, None)

    async def cancel(self, conversation_id: int) -> bool:
        """Cancel the in-flight stream; True when one was actually running."""
        async with self._lock:
            handle = self._handles.get(conversation_id)
            if handle is None:
                return False
            handle.cancelled.set()
            return True


_stream_registry = _StreamRegistry()


# --- helpers ----------------------------------------------------------------
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _fmt(dt: datetime | None) -> str:
    return dt.strftime(_TIME_FMT) if dt else ""


def _to_coach_message(m: Message) -> CoachMessage:
    return CoachMessage(
        messageId=str(m.id or ""),
        role=m.role,
        type=m.type,
        content=m.content,
        audioUrl=m.audio_url,
        transcript=m.transcript,
        createTime=_fmt(m.created_at),
    )


def _device(session: Session, address: str) -> Device | None:
    return session.exec(select(Device).where(Device.mac == address)).first()


def _get_conversation(
    session: Session, device_id: int, conversation_id: str
) -> Conversation | None:
    """Resolve a conversation by public id, asserting it belongs to ``device_id``."""
    try:
        cid = int(conversation_id)
    except (ValueError, TypeError):
        return None
    return session.exec(
        select(Conversation).where(
            Conversation.id == cid, Conversation.device_id == device_id
        )
    ).first()


def _get_message(session: Session, device_id: int, message_id: str) -> Message | None:
    try:
        mid = int(message_id)
    except (ValueError, TypeError):
        return None
    msg = session.exec(select(Message).where(Message.id == mid)).first()
    if msg is None:
        return None
    conv = session.exec(
        select(Conversation).where(
            Conversation.id == msg.conversation_id,
            Conversation.device_id == device_id,
        )
    ).first()
    return msg if conv is not None else None


def _resolve_or_create_conversation(
    session: Session, device: Device, conversation_id: str | None
) -> Conversation:
    """The device's existing conversation, or a fresh one when ``conversation_id`` is None/unknown."""
    if conversation_id:
        existing = _get_conversation(session, device.id, conversation_id)
        if existing is not None:
            return existing
    conv = Conversation(device_id=device.id)
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return conv


def _history(session: Session, conversation_id: int) -> list[tuple[str, str]]:
    msgs = session.exec(
        select(Message).where(Message.conversation_id == conversation_id).order_by(
            Message.id
        )
    ).all()
    return [(m.role, m.content) for m in msgs]


def _sse(event: str, data: StreamEvent) -> str:
    return f"event: {event}\ndata: {data.model_dump_json()}\n\n"


# --- 11.1 获取对话历史列表 ---
@router.get("/conversations", response_model=BoomResponse[list[ConversationSummary]])
async def list_conversations(
    address: str = Query(..., description="设备地址(mac)"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[ConversationSummary]]:
    """11.1 获取对话历史列表 — the device's conversations, most recent first."""
    device = _device(session, address)
    if device is None:
        return BoomResponse(code=0, data=[])
    convs = session.exec(
        select(Conversation).where(Conversation.device_id == device.id)
    ).all()
    convs = sorted(convs, key=lambda c: c.created_at or datetime.min, reverse=True)
    data = [
        ConversationSummary(
            conversationId=str(c.id),
            title=c.title,
            createTime=_fmt(c.created_at),
            updateTime=_fmt(c.updated_at),
            messageCount=len(c.messages),
        )
        for c in convs
    ]
    return BoomResponse(code=0, data=data)


# --- 11.2 获取对话消息列表 ---
@router.get("/messages", response_model=BoomResponse[list[CoachMessage]])
async def list_messages(
    conversationId: str = Query(..., description="对话唯一标识"),
    address: str = Query(..., description="设备地址(mac)"),
    session: Session = Depends(get_session),
) -> BoomResponse[list[CoachMessage]]:
    """11.2 获取对话消息列表 — the conversation's turns, oldest first."""
    device = _device(session, address)
    if device is None:
        return BoomResponse(code=0, data=[])
    conv = _get_conversation(session, device.id, conversationId)
    if conv is None:
        return BoomResponse(code=0, data=[])
    msgs = session.exec(
        select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)
    ).all()
    return BoomResponse(code=0, data=[_to_coach_message(m) for m in msgs])


# --- 11.3 发送消息 ---
@router.post("/messages", response_model=BoomResponse[SendResult])
async def send_message(
    body: SendMessageRequest,
    address: str = Query(..., description="设备地址(mac)"),
    session: Session = Depends(get_session),
) -> BoomResponse[SendResult]:
    """11.3 发送消息 — persist the user turn, generate + persist the AI reply."""
    device = _device(session, address)
    if device is None:
        return BoomResponse(code=0, data=SendResult(conversationId="", messageId=""))
    conv = _resolve_or_create_conversation(session, device, body.conversationId)
    user_msg = Message(
        conversation_id=conv.id,
        role="user",
        type=body.type,
        content=body.content,
        audio_url=body.audioUrl,
    )
    session.add(user_msg)
    session.commit()
    session.refresh(user_msg)
    ctx = CoachContext(
        conversation_id=conv.id,
        device_id=device.id,
        user_message=body.content,
        user_type=body.type,
        history=_history(session, conv.id),
    )
    reply = get_provider().reply(ctx)
    ai_msg = Message(
        conversation_id=conv.id, role="ai", type="text", content=reply
    )
    session.add(ai_msg)
    session.commit()
    session.refresh(ai_msg)
    conv.updated_at = datetime.utcnow()
    session.add(conv)
    session.commit()
    return BoomResponse(
        code=0,
        data=SendResult(
            conversationId=str(conv.id),
            messageId=str(user_msg.id),
            aiResponse=_to_coach_message(ai_msg),
        ),
    )


# --- 11.4 发送消息(流式输出) ---
@router.post("/messages/stream")
async def send_message_stream(
    body: SendMessageRequest,
    address: str = Query(..., description="设备地址(mac)"),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """11.4 发送消息(流式输出) — SSE ``event: message`` chunks, then ``event: done``."""
    device = _device(session, address)
    if device is None:
        async def empty() -> AsyncGenerator[str, None]:
            yield _sse("done", StreamEvent(messageId="", content=None, done=True))

        return StreamingResponse(empty(), media_type="text/event-stream")
    conv = _resolve_or_create_conversation(session, device, body.conversationId)
    user_msg = Message(
        conversation_id=conv.id,
        role="user",
        type=body.type,
        content=body.content,
        audio_url=body.audioUrl,
    )
    session.add(user_msg)
    session.commit()
    session.refresh(user_msg)
    # Capture the ids as plain ints while the request session is open: the SSE
    # generator outlives that session, so it must not touch the ORM objects.
    conv_id = conv.id
    user_id = user_msg.id
    ctx = CoachContext(
        conversation_id=conv_id,
        device_id=device.id,
        user_message=body.content,
        user_type=body.type,
        history=_history(session, conv_id),
    )
    provider = get_provider()
    handle = await _stream_registry.register(conv_id)

    async def events() -> AsyncGenerator[str, None]:
        chunks: list[str] = []
        ai_id = ""
        try:
            async for chunk in provider.stream(ctx):
                if handle.cancelled.is_set():
                    break
                chunks.append(chunk)
                yield _sse(
                    "message",
                    StreamEvent(
                        messageId=str(user_id), content=chunk, done=False
                    ),
                )
            full = "".join(chunks)
            # Persist the completed AI turn in a fresh session: the request
            # session closes when the stream ends, so the writer cannot reuse it.
            with Session(engine) as writer:
                ai = Message(
                    conversation_id=conv_id, role="ai", type="text", content=full
                )
                writer.add(ai)
                writer.commit()
                writer.refresh(ai)
                ai_id = str(ai.id)
                c = writer.get(Conversation, conv_id)
                if c is not None:
                    c.updated_at = datetime.utcnow()
                    writer.add(c)
                    writer.commit()
            yield _sse(
                "done",
                StreamEvent(messageId=ai_id, content=None, done=True),
            )
        finally:
            await _stream_registry.remove(conv_id)

    return StreamingResponse(events(), media_type="text/event-stream")


# --- 11.5 新建对话 ---
@router.post("/conversations", response_model=BoomResponse[NewConversation])
async def create_conversation(
    address: str = Query(..., description="设备地址(mac)"),
    session: Session = Depends(get_session),
) -> BoomResponse[NewConversation]:
    """11.5 新建对话."""
    device = _device(session, address)
    if device is None:
        return BoomResponse(code=0, data=NewConversation())
    conv = Conversation(device_id=device.id)
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return BoomResponse(
        code=0,
        data=NewConversation(
            conversationId=str(conv.id),
            title=conv.title,
            createTime=_fmt(conv.created_at),
        ),
    )


# --- 11.6 删除对话 ---
@router.delete("/conversations/{conversationId}", response_model=BoomMessage)
async def delete_conversation(
    conversationId: str,
    address: str = Query(..., description="设备地址(mac)"),
    session: Session = Depends(get_session),
) -> BoomMessage:
    """11.6 删除对话 — removes the conversation and all of its messages."""
    device = _device(session, address)
    if device is None:
        return BoomMessage(code=0, msg="")
    conv = _get_conversation(session, device.id, conversationId)
    if conv is None:
        return BoomMessage(code=0, msg="")
    for m in session.exec(
        select(Message).where(Message.conversation_id == conv.id)
    ).all():
        session.delete(m)
    session.delete(conv)
    session.commit()
    return BoomMessage(code=0, msg="")


# --- 11.7 删除消息 ---
@router.delete("/messages/{messageId}", response_model=BoomMessage)
async def delete_message(
    messageId: str,
    address: str = Query(..., description="设备地址(mac)"),
    session: Session = Depends(get_session),
) -> BoomMessage:
    """11.7 删除消息."""
    device = _device(session, address)
    if device is None:
        return BoomMessage(code=0, msg="")
    msg = _get_message(session, device.id, messageId)
    if msg is None:
        return BoomMessage(code=0, msg="")
    session.delete(msg)
    session.commit()
    return BoomMessage(code=0, msg="")


# --- 11.8 获取欢迎语 ---
@router.get("/welcome", response_model=BoomResponse[WelcomeData])
async def get_welcome() -> BoomResponse[WelcomeData]:
    """11.8 获取欢迎语."""
    return BoomResponse(
        code=0,
        data=WelcomeData(
            title="欢迎使用 BOOM 训练助手",
            message="今天适合做什么运动？",
        ),
    )


# --- 11.9 上传语音 ---
@router.post("/voice", response_model=BoomResponse[VoiceUploadData])
async def upload_voice(
    file: UploadFile = File(..., description="语音文件(m4a/wav/mp3)"),
    address: str = Query(..., description="设备地址(mac)"),
    session: Session = Depends(get_session),
) -> BoomResponse[VoiceUploadData]:
    """11.9 上传语音 — persist the file; ASR + duration PENDING a decoder/ASR backend."""
    device = _device(session, address)
    if device is None:
        return BoomResponse(code=0, data=VoiceUploadData())
    data = await file.read()
    path = save_audio(device.name, file.filename or "audio", data)
    # audioUrl is the stored path: the app serves no static mount yet, and
    # duration / transcript await a decoder + ASR backend.
    return BoomResponse(
        code=0,
        data=VoiceUploadData(audioUrl=path, duration=0, transcript=""),
    )


# --- 11.10 中断流式输出 ---
@router.post("/interrupt", response_model=BoomMessage)
async def interrupt_stream(
    body: InterruptRequest,
    address: str = Query(..., description="设备地址(mac)"),
    session: Session = Depends(get_session),
) -> BoomMessage:
    """11.10 中断流式输出 — cancel the in-flight §11.4 stream for the conversation."""
    device = _device(session, address)
    if device is None:
        return BoomMessage(code=0, msg="")
    # The registry is keyed by the conversation id, so assert ownership first:
    # a device may only interrupt its own conversations.
    conv = _get_conversation(session, device.id, body.conversationId)
    if conv is None:
        return BoomMessage(code=0, msg="")
    await _stream_registry.cancel(conv.id)
    return BoomMessage(code=0, msg="")


# --- 11.11 消息反馈 ---
@router.post("/feedback", response_model=BoomMessage)
async def message_feedback(
    body: FeedbackRequest,
    address: str = Query(..., description="设备地址(mac)"),
    session: Session = Depends(get_session),
) -> BoomMessage:
    """11.11 消息反馈."""
    device = _device(session, address)
    if device is None:
        return BoomMessage(code=0, msg="")
    msg = _get_message(session, device.id, body.messageId)
    if msg is None:
        return BoomMessage(code=0, msg="")
    msg.feedback = body.feedback
    session.add(msg)
    session.commit()
    return BoomMessage(code=0, msg="")
