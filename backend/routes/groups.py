"""Group management — /api/groups/* routes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..aggregator import aggregate_all
from ..auth import get_current_user, require_admin
from ..deps import group_storage, storage

router = APIRouter()


class CreateGroupPayload(BaseModel):
    name: str


class AddMemberPayload(BaseModel):
    username: str


@router.get("/api/groups")
async def list_groups(_: dict = Depends(get_current_user)):
    return group_storage.list_groups()


@router.post("/api/groups", status_code=201)
async def create_group(payload: CreateGroupPayload, actor: dict = Depends(require_admin)):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Group name is required")
    return group_storage.create_group(payload.name, actor["username"])


@router.delete("/api/groups/{group_id}")
async def delete_group(group_id: str, _: dict = Depends(require_admin)):
    if not group_storage.delete_group(group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    return {"ok": True}


@router.post("/api/groups/{group_id}/members")
async def add_group_member(
    group_id: str,
    payload:  AddMemberPayload,
    _:        dict = Depends(require_admin),
):
    all_users = storage.get_all_users()
    canonical = next((u for u in all_users if u.lower() == payload.username.lower()), None)
    if canonical is None:
        raise HTTPException(status_code=404, detail=f"Employee '{payload.username}' not found")
    result = group_storage.add_member(group_id, canonical)
    if result is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return result


@router.delete("/api/groups/{group_id}/members/{username}")
async def remove_group_member(
    group_id: str,
    username: str,
    _:        dict = Depends(require_admin),
):
    result = group_storage.remove_member(group_id, username)
    if result is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return result


@router.get("/api/groups/{group_id}/summary")
async def get_group_summary(
    group_id: str,
    date:     str,
    _:        dict = Depends(get_current_user),
):
    group = group_storage.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    all_users = storage.get_all_users()
    canon_map = {u.lower(): u for u in all_users}

    members_data = []
    for stored_name in group["members"]:
        canonical = canon_map.get(stored_name.lower(), stored_name)
        events    = storage.get_raw_events(canonical, date)
        if events:
            agg = aggregate_all(events)
            members_data.append({
                "username": canonical,
                "summary":  agg["summary"],
                "timeline": agg["timeline"],
            })
        else:
            members_data.append({"username": canonical, "summary": None, "timeline": []})

    return {"group": group, "date": date, "members": members_data}
