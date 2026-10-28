"""9.x 问卷页面 — /app/questionnaire."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from newboom.boom.schemas import BoomResponse
from newboom.database import get_session
from newboom.model import Device, QuestionnaireAnswer

router = APIRouter(tags=["问卷"])


# --- response / request schemas -------------------------------------------


class Option(BaseModel):
    label: str = Field(..., description="显示名称")
    value: str = Field(..., description="值")


class Question(BaseModel):
    text: str = Field(..., description="问题文本")
    options: list[Option] = Field(default_factory=list, description="问题选项")


class QuestionnaireData(BaseModel):
    userTypes: list[Option] = Field(default_factory=list, description="用户类型选项")
    questions: list[Question] = Field(default_factory=list, description="问卷问题列表")


class Answer(BaseModel):
    question: str = Field(..., description="问题文本")
    selected: str = Field(..., description="用户选择值")


class QuestionnaireSubmitRequest(BaseModel):
    userType: str = Field(..., description="用户类型值:beginner/serious/professional")
    answers: list[Answer] = Field(..., description="用户答案列表")


class QuestionnaireSubmitResponse(BaseModel):
    code: int = Field(0, description="响应代码,0=成功")
    message: str = Field("", description="响应信息")


# --- static content -------------------------------------------------------
#
# The page is device-less and the spec (§9.1) fixes the content, so it lives as
# module constants (the ``_SUMMARY_METRICS`` / ``_UNITS`` precedent in
# ``body_metrics``). Edit in place; the spec's question-02 option set reads like
# placeholder fragments, so it is the first thing to replace with real content.

_USER_TYPES: list[Option] = [
    Option(label="新手爱好者", value="beginner"),
    Option(label="严肃运动者", value="serious"),
    Option(label="职业运动员", value="professional"),
]

_QUESTIONS: list[Question] = [
    Question(
        text="01.前几天连续训练负荷较大，HRV较低但储能情况良好",
        options=[Option(label="是", value="yes"), Option(label="否", value="no")],
    ),
    Question(
        text="02.预计明天HRV上升达到超量恢复黄金窗口",
        options=[
            Option(label="预计明天HRV上升达到超量恢复黄金窗口", value="window"),
            Option(label="超量恢复黄金窗口", value="window2"),
            Option(label="建议降低训练量，注意恢复调整", value="reduce"),
            Option(label="注意恢复调整", value="adjust"),
        ],
    ),
]

# Valid ``userType`` values, derived from the static set so the submit validator
# and the §9.1 payload can never drift.
_USER_TYPE_VALUES: frozenset[str] = frozenset(u.value for u in _USER_TYPES)


# --- endpoints ------------------------------------------------------------


@router.get("/app/questionnaire", response_model=BoomResponse[QuestionnaireData])
async def get_questionnaire() -> BoomResponse[QuestionnaireData]:
    """9.1 获取问卷内容.

    Serves the static questionnaire page (user types + questions) from the
    module constants; the endpoint is device-less and the spec fixes the
    content, so there is no device / DB access.
    """
    return BoomResponse(
        code=0,
        data=QuestionnaireData(userTypes=_USER_TYPES, questions=_QUESTIONS),
    )


@router.post(
    "/app/{address}/questionnaire/submit",
    response_model=QuestionnaireSubmitResponse,
)
async def submit_questionnaire(
    address: str,
    body: QuestionnaireSubmitRequest,
    session: Session = Depends(get_session),
) -> QuestionnaireSubmitResponse:
    """9.2 提交问卷答案.

    Resolves the device by ``mac == address``, creating it (``name = address``)
    when it has not paired yet so an answer is never lost; then validates
    ``userType`` against the static user-type set and persists one
    ``QuestionnaireAnswer`` row per ``answers[]`` pair (``user_type`` /
    ``question`` / ``selected`` plus the submit timestamp). An unknown
    ``userType`` yields ``{code:1, message}`` — a non-zero code keeps the
    ``{code, message}`` envelope, which a FastAPI 422 would bypass.
    """
    if body.userType not in _USER_TYPE_VALUES:
        return QuestionnaireSubmitResponse(
            code=1, message=f"invalid userType: {body.userType}"
        )

    device = session.exec(select(Device).where(Device.mac == address)).first()
    if device is None:
        device = Device(name=address, mac=address)
        session.add(device)
        session.commit()
        session.refresh(device)

    for ans in body.answers:
        session.add(
            QuestionnaireAnswer(
                device_id=device.id or 0,
                user_type=body.userType,
                question=ans.question,
                selected=ans.selected,
            )
        )
    session.commit()
    return QuestionnaireSubmitResponse(code=0, message="提交成功")
