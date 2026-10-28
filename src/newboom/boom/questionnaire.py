"""9.x 问卷页面 — /app/questionnaire."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from newboom.boom.schemas import BoomResponse

router = APIRouter(tags=["问卷"])


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


@router.get("/app/questionnaire", response_model=BoomResponse[QuestionnaireData])
async def get_questionnaire() -> BoomResponse[QuestionnaireData]:
    """9.1 获取问卷内容.

    TODO: implement.
    """
    return BoomResponse(code=0, data=QuestionnaireData())


@router.post("/app/questionnaire/submit", response_model=QuestionnaireSubmitResponse)
async def submit_questionnaire(
    body: QuestionnaireSubmitRequest,
) -> QuestionnaireSubmitResponse:
    """9.2 提交问卷答案.

    TODO: implement.
    """
    return QuestionnaireSubmitResponse(code=0, message="提交成功")
