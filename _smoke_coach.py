"""Throwaway smoke test for the §11 BOOM_COACH routes.

The project has no ``httpx``, so instead of ``TestClient`` we drive the async
endpoint functions directly with a real ``Session`` (the same code path FastAPI
runs, minus the HTTP layer). Verifies device scoping, int<->string id mapping,
the pluggable provider seam, the stream registry, and the full CRUD + feedback
+ voice + interrupt paths.
"""
import asyncio
import io
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="coach_smoke_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/coach.db"
# Keep the voice-upload path off the real store (read at import by storage).
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="coach_uploads_")

from starlette.datastructures import UploadFile  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from newboom.boom.coach import (  # noqa: E402
    CoachProvider,
    CoachContext,
    FeedbackRequest,
    InterruptRequest,
    SendMessageRequest,
    _StreamRegistry,
    delete_conversation,
    delete_message,
    get_welcome,
    interrupt_stream,
    list_conversations,
    list_messages,
    message_feedback,
    send_message,
    send_message_stream,
    set_provider,
    create_conversation,
    upload_voice,
)
from newboom.database import create_db_and_tables, engine  # noqa: E402
from newboom.model import Device, Message  # noqa: E402

create_db_and_tables()

# Seed two devices: A owns the conversation; B must never see it.
with Session(engine) as s:
    da = Device(name="device-A", mac="AA:BB:CC:DD:EE:01")
    db = Device(name="device-B", mac="AA:BB:CC:DD:EE:02")
    s.add(da)
    s.add(db)
    s.commit()
    s.refresh(da)
    s.refresh(db)
    ADDR_A, ADDR_B = da.mac, db.mac

_failures = 0


def check(label, cond, extra=""):
    global _failures
    ok = bool(cond)
    if not ok:
        _failures += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


def session():
    return Session(engine)


# --- 11.5 create conversation (device A) ---
with session() as s:
    r = asyncio.run(create_conversation(address=ADDR_A, session=s))
check("11.5 create code=0", r.code == 0, str(r))
conv_id = r.data.conversationId
check("11.5 id is non-empty str", isinstance(conv_id, str) and conv_id != "")

# --- 11.1 list (device scoping) ---
with session() as s:
    r = asyncio.run(list_conversations(address=ADDR_A, session=s))
data = r.data
check("11.1 A sees its conversation", len(data) == 1 and data[0].conversationId == conv_id, str(data))
check("11.1 has messageCount+times", data[0].messageCount == 0 and bool(data[0].createTime), str(data[0]))
with session() as s:
    r = asyncio.run(list_conversations(address=ADDR_B, session=s))
check("11.1 B sees none (scoping)", r.data == [])

# --- 11.3 send message (default provider seam) ---
with session() as s:
    r = asyncio.run(
        send_message(
            body=SendMessageRequest(content="今天训练什么?", type="text", conversationId=conv_id),
            address=ADDR_A,
            session=s,
        )
    )
check("11.3 send code=0", r.code == 0, str(r))
res = r.data
user_id = res.messageId
ai = res.aiResponse
check("11.3 user id non-empty", bool(user_id))
check("11.3 ai role=ai", ai.role == "ai")
check("11.3 ai content non-empty (placeholder)", ai.content != "")
check("11.3 conversationId echoed", res.conversationId == conv_id)

# --- 11.2 list messages (order + scoping) ---
with session() as s:
    r = asyncio.run(list_messages(conversationId=conv_id, address=ADDR_A, session=s))
msgs = r.data
check("11.2 two turns (user+ai)", len(msgs) == 2, str(msgs))
check("11.2 order user->ai", [m.role for m in msgs] == ["user", "ai"], str(msgs))
with session() as s:
    r = asyncio.run(list_messages(conversationId=conv_id, address=ADDR_B, session=s))
check("11.2 B cannot read A's messages", r.data == [])

# --- 11.11 feedback (persisted) ---
with session() as s:
    r = asyncio.run(message_feedback(body=FeedbackRequest(messageId=user_id, feedback="like"), address=ADDR_A, session=s))
check("11.11 feedback code=0", r.code == 0, str(r))
with session() as s:
    m = s.exec(select(Message).where(Message.id == int(user_id))).first()
    check("11.11 feedback persisted", m is not None and m.feedback == "like", str(getattr(m, "feedback", None)))

# --- 11.8 welcome ---
r = asyncio.run(get_welcome())
check("11.8 welcome", r.code == 0 and r.data.message != "")

# --- provider seam is pluggable ---
class _Fixed(CoachProvider):
    def reply(self, ctx: CoachContext) -> str:
        return "SEAM_OK"

    async def stream(self, ctx: CoachContext):
        for ch in ("SEAM_OK",):
            yield ch

set_provider(_Fixed())
with session() as s:
    r = asyncio.run(
        send_message(
            body=SendMessageRequest(content="x", type="text", conversationId=conv_id),
            address=ADDR_A,
            session=s,
        )
    )
check("provider seam: custom reply used", r.data.aiResponse.content == "SEAM_OK", r.data.aiResponse.content)
# restore the default so 11.4 uses the placeholder
from newboom.boom.coach import DefaultCoachProvider  # noqa: E402
set_provider(DefaultCoachProvider())

# --- 11.4 stream (SSE + provider + persistence), create + consume in one loop ---
async def _run_stream() -> str:
    resp = await send_message_stream(
        body=SendMessageRequest(content="流式测试", type="text", conversationId=conv_id),
        address=ADDR_A,
        session=session(),
    )
    parts: list[str] = []
    async for chunk in resp.body_iterator:
        parts.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(parts)

body = asyncio.run(_run_stream())
check("11.4 SSE message frame", "event: message" in body, body[:160])
check("11.4 SSE done frame", "event: done" in body)
with session() as s:
    ais = s.exec(select(Message).where(Message.conversation_id == int(conv_id), Message.role == "ai")).all()
    check("11.4 ai reply persisted", len(ais) >= 1 and all(x.content for x in ais), str([x.content for x in ais]))

# --- 11.10 interrupt (registry mechanism + endpoint) ---
async def _registry_roundtrip() -> None:
    reg = _StreamRegistry()
    h = await reg.register(7)
    assert not h.cancelled.is_set()
    assert await reg.cancel(7) is True
    assert h.cancelled.is_set()
    assert await reg.cancel(8) is False
    await reg.remove(7)
    assert 7 not in reg._handles

asyncio.run(_registry_roundtrip())
check("11.10 registry register/cancel/remove", True)
with session() as s:
    r = asyncio.run(
        interrupt_stream(
            body=InterruptRequest(conversationId=conv_id), address=ADDR_A, session=s
        )
    )
check("11.10 endpoint code=0", r.code == 0, str(r))

# --- 11.9 voice upload ---
upload = UploadFile(file=io.BytesIO(b"fake-audio-bytes"), filename="voice.m4a")
with session() as s:
    r = asyncio.run(upload_voice(file=upload, address=ADDR_A, session=s))
check("11.9 voice code=0", r.code == 0, str(r))
check("11.9 audioUrl set (stored path)", r.data.audioUrl != "", r.data.audioUrl)

# --- 11.7 delete message, then 11.6 delete conversation ---
with session() as s:
    r = asyncio.run(delete_message(messageId=user_id, address=ADDR_A, session=s))
check("11.7 delete message code=0", r.code == 0, str(r))
with session() as s:
    r = asyncio.run(delete_conversation(conversationId=conv_id, address=ADDR_A, session=s))
check("11.6 delete conversation code=0", r.code == 0, str(r))
with session() as s:
    r = asyncio.run(list_conversations(address=ADDR_A, session=s))
check("11.6 conversation gone", r.data == [])
with session() as s:
    r = asyncio.run(delete_conversation(conversationId=conv_id, address=ADDR_A, session=s))
check("11.6 delete-again no-op", r.code == 0)

# --- unknown device is a clean empty, not a 500 ---
with session() as s:
    r = asyncio.run(list_conversations(address="NO:SUCH:DEVICE", session=s))
check("unknown device -> empty", r.data == [])

print()
if _failures:
    print(f"{_failures} CHECK(S) FAILED")
    raise SystemExit(1)
print("ALL COACH SMOKE CHECKS PASSED")
