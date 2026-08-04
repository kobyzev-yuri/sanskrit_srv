"""System status: LLM balance / quota warnings."""
from fastapi import APIRouter, Depends

from app.deps import get_current_user
from app.models import User
from app.services.llm_status import llm_status

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/llm-status")
def get_llm_status(_user: User = Depends(get_current_user)):
    return llm_status()
