from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.api import deps
from app.core.limiter import limiter
from app.services.uploads import save_upload

router = APIRouter(prefix="/uploads", tags=["Uploads"])


@router.post("")
@router.post("/")
@limiter.limit("20/hour")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    # private=true para documentos sensibles (DNI, firmas, comprobantes): no se
    # publican y solo los descargan el dueño, el admin o su contraparte.
    private: bool = Query(default=False),
    current_user=Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    try:
        url = await save_upload(file, private=private, owner_id=current_user.id, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"url": url}
