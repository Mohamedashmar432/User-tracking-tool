"""Dashboard authentication — /auth/* routes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import create_token, get_current_user, require_admin
from ..deps import user_storage

router = APIRouter()


class LoginPayload(BaseModel):
    username: str
    password: str


class CreateUserPayload(BaseModel):
    username: str
    password: str
    role:     str = "viewer"


class ChangePasswordPayload(BaseModel):
    password: str


class ChangeRolePayload(BaseModel):
    role: str


@router.post("/auth/login")
async def login(payload: LoginPayload):
    user = user_storage.verify_password(payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_token(user["username"], user["role"])
    return {"token": token, "username": user["username"], "role": user["role"]}


@router.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return user


@router.get("/auth/users")
async def list_auth_users(_: dict = Depends(require_admin)):
    return user_storage.list_users()


@router.post("/auth/users", status_code=201)
async def create_auth_user(payload: CreateUserPayload, _: dict = Depends(require_admin)):
    if payload.role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'viewer'")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    return user_storage.create_user(payload.username, payload.password, payload.role)


@router.put("/auth/users/{username}/password")
async def change_password(
    username: str,
    payload:  ChangePasswordPayload,
    _:        dict = Depends(require_admin),
):
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not user_storage.update_password(username, payload.password):
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@router.put("/auth/users/{username}/role")
async def change_role(
    username: str,
    payload:  ChangeRolePayload,
    actor:    dict = Depends(require_admin),
):
    if payload.role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'viewer'")
    if username == actor["username"]:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    if not user_storage.update_role(username, payload.role):
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@router.delete("/auth/users/{username}")
async def delete_auth_user(username: str, actor: dict = Depends(require_admin)):
    if username == actor["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if not user_storage.delete_user(username):
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}
