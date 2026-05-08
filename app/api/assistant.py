from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models import User
from app.schemas.assistant import (
    AssistantConfirmRequest,
    AssistantDiscardRequest,
    AssistantMessageInput,
    AssistantResponse,
    AssistantThreadCreate,
    AssistantThreadDetail,
    AssistantThreadSummary,
)
from app.services.assistant import (
    confirm_draft_action,
    create_thread,
    discard_draft_action,
    get_thread_detail,
    list_threads,
    process_user_message,
)


router = APIRouter(prefix="/api/assistant", tags=["assistant"], include_in_schema=False)


@router.post("/threads", response_model=AssistantThreadSummary)
def create_assistant_thread(
    payload: AssistantThreadCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return create_thread(db, user=current_user, title=payload.title)


@router.get("/threads", response_model=list[AssistantThreadSummary])
def get_assistant_threads(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_threads(db, user=current_user)


@router.get("/threads/{thread_id}", response_model=AssistantThreadDetail)
def get_assistant_thread(
    thread_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_thread_detail(db, user=current_user, thread_id=thread_id)


@router.post("/threads/{thread_id}/messages", response_model=AssistantResponse)
def send_assistant_message(
    thread_id: int,
    payload: AssistantMessageInput,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return process_user_message(
        db,
        user=current_user,
        thread_id=thread_id,
        message=payload.message,
        selected_user_ids=payload.selected_user_ids,
    )


@router.post("/threads/{thread_id}/confirm", response_model=AssistantResponse)
def confirm_assistant_draft(
    thread_id: int,
    payload: AssistantConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return confirm_draft_action(
        db,
        user=current_user,
        thread_id=thread_id,
        draft_action_id=payload.draft_action_id,
    )


@router.post("/threads/{thread_id}/discard", response_model=AssistantResponse)
def discard_assistant_draft(
    thread_id: int,
    payload: AssistantDiscardRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return discard_draft_action(
        db,
        user=current_user,
        thread_id=thread_id,
        draft_action_id=payload.draft_action_id,
    )
